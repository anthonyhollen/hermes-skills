#!/usr/bin/env python3
"""pi_sync.py - two-way sync between the live Hermes install and this git repo.

Runs on amhpi5 from the system crontab every 10 minutes (and by hand). One pass:

  1. capture  - copy live edits (Hermes / SSH) into the repo and commit them as "pi: ..."
  2. rebase   - fetch origin and rebase onto origin/main (edits pushed from the Mac)
  3. push     - push the Pi's commits
  4. check    - py_compile / bash -n / SKILL.md frontmatter on every incoming file
  5. apply    - write incoming files (last_applied..HEAD) into the live install

Safety rules:
  - A rebase conflict aborts the rebase, applies nothing and alerts Discord.
  - A failed check applies nothing, marks the sync BLOCKED and alerts. While blocked,
    capture is skipped (so live code never reverts the bad commit); each run fetches
    again and resumes once a fixed commit arrives.
  - Only files changed in git since the last apply are written to live, so runtime
    files and gitignored state are never touched. If a live file was edited locally
    after the last apply, it is saved as <file>.bak-pisync-<ts> before overwriting.
  - Deletions on the Pi are captured only when the repo is in a fully-applied state.
  - Skips the run if any tracked live file changed in the last 60 s (mid-edit).

Usage: pi_sync.py [--dry-run] [--no-alert] [-v]
"""
import argparse
import fcntl
import fnmatch
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".hermes")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_DIR = os.path.join(REPO, ".sync")
STATE_FILE = os.path.join(SYNC_DIR, "state.json")
LOG_FILE = os.path.join(SYNC_DIR, "sync.log")
BRANCH = "main"
CONFLICT_BRANCH = "pi-conflict"
APPLIED_TAG = "pi-applied"
ALERT_CHANNEL = "1501742338639401071"  # Discord playground
ALERT_REPEAT_S = 6 * 3600
QUIET_S = 60

# repo prefix -> live directory
ROOTS = {
    "skills": os.path.join(HERMES, "skills"),
    "scripts": os.path.join(HERMES, "scripts"),
    "data/morning-brief": os.path.join(HERMES, "morning-brief"),
}
# New top-level files in ~/.hermes/scripts matching these are picked up automatically...
SCRIPT_INCLUDE = ["*.py", "*.sh"]
# ...except these (not part of Ham's skills: gateway ops, one-offs, unrelated jobs).
SCRIPT_EXCLUDE = [
    "backup_hermes.py", "calendar_check.py", "cholesterol_reminder.py",
    "gateway_restart*.sh", "restart_gateway*.sh", "restartgw_*",
    "warframe_*", "verify_*.py", "gmail_state_heal_once.py",
    "token_report_debug.py",  # stale debug copy with a hardcoded bot token
]

VERBOSE = False


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if VERBOSE or sys.stdout.isatty():
        print(line)


def git(*args, check=True, input=None):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    cutoff = time.time() - 7 * 86400
    state["alerts"] = {k: t for k, t in state.get("alerts", {}).items() if t > cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _discord_token():
    # ~/.hermes/.env is authoritative (same convention as backup_cleaner.py).
    try:
        with open(os.path.join(HERMES, ".env")) as f:
            for line in f:
                line = line.strip()
                for key in ("DISCORD_BOT_TOKEN=", "DISCORD_TOKEN="):
                    if line.startswith(key):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def alert(state, key, text, enabled=True):
    """Post to Discord once per key per ALERT_REPEAT_S."""
    log("ALERT " + text.replace("\n", " | "))
    if not enabled:
        return
    sent = state.setdefault("alerts", {})
    if time.time() - sent.get(key, 0) < ALERT_REPEAT_S:
        return
    token = _discord_token()
    if not token:
        log("no Discord token; alert not posted")
        return
    body = json.dumps({"content": ("**hermes-skills sync** " + text)[:1990],
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{ALERT_CHANNEL}/messages", data=body,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (hermes-skills-sync, 1.0)"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        sent[key] = time.time()
    except Exception as e:  # never let alerting break the sync
        log(f"alert post failed: {e}")


def live_path(rel):
    for prefix, live in sorted(ROOTS.items(), key=lambda kv: -len(kv[0])):
        if rel == prefix or rel.startswith(prefix + "/"):
            return os.path.join(live, rel[len(prefix) + 1:])
    return None


def tracked_files(rev="HEAD"):
    out = git("ls-tree", "-r", "--name-only", rev, "--", *ROOTS).stdout
    return [p for p in out.splitlines() if p]


def not_ignored(paths):
    if not paths:
        return []
    r = git("check-ignore", "--no-index", "--stdin", check=False, input="\n".join(paths) + "\n")
    ignored = set(r.stdout.splitlines())
    return [p for p in paths if p not in ignored]


def capture_candidates(tracked):
    """Repo-relative paths whose live copies should be mirrored into the repo."""
    cands = set(tracked)
    # Every file inside each tracked skill directory (new files included).
    skill_dirs = {os.path.dirname(p) for p in tracked
                  if p.startswith("skills/") and os.path.basename(p) == "SKILL.md"}
    for sd in skill_dirs:
        base = live_path(sd)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "logs", "cache")
                           and not d.startswith(".venv")]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if not os.path.islink(full):
                    cands.add(sd + "/" + os.path.relpath(full, base))
    # New top-level scripts.
    sdir = ROOTS["scripts"]
    for fn in os.listdir(sdir):
        full = os.path.join(sdir, fn)
        if (os.path.isfile(full) and not os.path.islink(full)
                and any(fnmatch.fnmatch(fn, p) for p in SCRIPT_INCLUDE)
                and not any(fnmatch.fnmatch(fn, p) for p in SCRIPT_EXCLUDE)):
            cands.add("scripts/" + fn)
    return sorted(not_ignored(sorted(cands)))


def same(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def copy_atomic(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".pisync-")
    os.close(fd)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, os.stat(src).st_mode & 0o777)
    os.replace(tmp, dst)


def blob_bytes(rev, rel):
    r = subprocess.run(["git", "-C", REPO, "show", f"{rev}:{rel}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def check_file(rel):
    """Return an error string, or None if the incoming file looks safe to apply."""
    path = os.path.join(REPO, rel)
    if rel.endswith(".py"):
        with tempfile.TemporaryDirectory() as td:
            try:
                py_compile.compile(path, cfile=os.path.join(td, "x.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                return str(e.msg).strip().splitlines()[-1]
    elif rel.endswith(".sh"):
        r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        if r.returncode:
            return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "bash -n failed"
    elif os.path.basename(rel) == "SKILL.md":
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if not text.startswith("---\n") or "\n---" not in text[4:]:
            return "SKILL.md must start with a --- frontmatter block"
    elif rel.endswith(".json"):
        try:
            with open(path) as f:
                json.load(f)
        except ValueError as e:
            return f"invalid JSON: {e}"
    return None


def recently_modified(paths):
    """First live file that differs from the repo and changed in the last QUIET_S
    (likely mid-edit). Files the sync itself just wrote match the repo, so don't count."""
    now = time.time()
    for rel in paths:
        lp = live_path(rel)
        try:
            if now - os.path.getmtime(lp) < QUIET_S and not same(lp, os.path.join(REPO, rel)):
                return rel
        except OSError:
            pass
    return None


def mark_applied(state, sha):
    """Record sha as live and publish it as tag pi-applied, so the Mac can see what's deployed."""
    state.update(last_applied=sha, blocked=None, last_ok=time.time())
    if state.get("tag") != sha:
        if git("push", "-q", "-f", "origin", f"{sha}:refs/tags/{APPLIED_TAG}", check=False).returncode == 0:
            state["tag"] = sha
    save_state(state)


def run(args):
    state = load_state()
    alerts_on = not args.no_alert
    head = git("rev-parse", "HEAD").stdout.strip()
    last_applied = state.get("last_applied")
    blocked = state.get("blocked")

    if os.path.isdir(os.path.join(REPO, ".git", "rebase-merge")) or \
            os.path.isdir(os.path.join(REPO, ".git", "rebase-apply")):
        git("rebase", "--abort", check=False)
        log("aborted a leftover rebase")

    tracked = tracked_files()
    cands = capture_candidates(tracked)
    busy = recently_modified(cands)
    if busy:
        log(f"skip: {busy} modified <{QUIET_S}s ago")
        return 0

    # 1. capture
    if blocked:
        log(f"blocked at {blocked['sha'][:8]}; capture skipped")
    else:
        changed = []
        for rel in cands:
            lp, rp = live_path(rel), os.path.join(REPO, rel)
            if os.path.isfile(lp) and not same(lp, rp):
                changed.append(rel)
                if not args.dry_run:
                    copy_atomic(lp, rp)
        removed = []
        if last_applied == head:
            removed = [rel for rel in tracked if not os.path.exists(live_path(rel))]
            if removed and not args.dry_run:
                git("rm", "-q", "--", *removed)
        if changed or removed:
            log(f"capture: {len(changed)} changed, {len(removed)} removed: "
                + ", ".join((changed + removed)[:10]))
            if not args.dry_run:
                if changed:
                    git("add", "-A", "--", *changed)
                summary = ", ".join(os.path.basename(p) for p in (changed + removed)[:5])
                more = len(changed) + len(removed) - 5
                msg = f"pi: live edits - {summary}" + (f" (+{more} more)" if more > 0 else "")
                git("commit", "-q", "-m", msg)

    if args.dry_run:
        log("dry-run: stopping before fetch/rebase")
        return 0

    # 2. rebase onto origin
    pre = git("rev-parse", "HEAD").stdout.strip()  # == live, for every captured file
    git("fetch", "-q", "origin", BRANCH)
    r = git("rebase", "-q", f"origin/{BRANCH}", check=False)
    if r.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        git("rebase", "--abort", check=False)
        # Publish the Pi's side so the conflict can be resolved from the Mac:
        # merge origin/pi-conflict into main there, push, and the next run fast-forwards.
        git("push", "-q", "-f", "origin", f"HEAD:{CONFLICT_BRANCH}", check=False)
        alert(state, "conflict", "rebase conflict between Pi live edits and GitHub; nothing applied. "
              f"Files: {', '.join(conflicts) or '?'}. The Pi's commits are on branch "
              f"`{CONFLICT_BRANCH}`: merge it into main (git merge, not rebase) and push.", alerts_on)
        save_state(state)
        return 1
    new = git("rev-parse", "HEAD").stdout.strip()

    # 3. push
    ahead = git("rev-list", "--count", f"origin/{BRANCH}..HEAD").stdout.strip()
    if ahead != "0":
        p = git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False)
        if p.returncode:
            alert(state, "push", f"push failed (will retry): {p.stderr.strip()[:300]}", alerts_on)
        else:
            log(f"pushed {ahead} commit(s)")
            state.get("alerts", {}).pop("push", None)

    if not last_applied:
        last_applied = head
    if new == last_applied:
        mark_applied(state, new)
        return 0

    # 4. check incoming
    diff = git("diff", "--name-status", "--no-renames", last_applied, new, "--", *ROOTS).stdout
    incoming, deleted = [], []
    for line in diff.splitlines():
        status, rel = line.split("\t", 1)
        (deleted if status == "D" else incoming).append(rel)
    failures = [f"{rel}: {err}" for rel in incoming if (err := check_file(rel))]
    if failures:
        state["blocked"] = {"sha": new, "failures": failures}
        alert(state, f"blocked-{new}", f"commit {new[:8]} NOT applied - checks failed:\n"
              + "\n".join(failures[:8]) + "\nPush a fix; the sync resumes automatically.", alerts_on)
        save_state(state)
        return 1

    # 5. apply
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backed_up = []
    for rel in incoming:
        lp, rp = live_path(rel), os.path.join(REPO, rel)
        if os.path.exists(lp) and not same(lp, rp):
            # Live matches `pre` for everything capture saw; anything else is an edit
            # made since (or while blocked) that git doesn't have yet - keep a copy.
            old = blob_bytes(pre, rel)
            with open(lp, "rb") as f:
                if f.read() != old:
                    shutil.copy2(lp, f"{lp}.bak-pisync-{stamp}")
                    backed_up.append(rel)
        if not same(lp, rp):
            copy_atomic(rp, lp)
    for rel in deleted:
        lp = live_path(rel)
        if os.path.isfile(lp):
            os.remove(lp)
    log(f"applied {new[:8]}: {len(incoming)} written, {len(deleted)} deleted")
    if backed_up:
        alert(state, f"overwrote-{new}", "applied GitHub changes over un-synced live edits; "
              "live copies kept as .bak-pisync-" + stamp + ": " + ", ".join(backed_up[:8]), alerts_on)
    mark_applied(state, new)
    return 0


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report live edits; change nothing")
    ap.add_argument("--no-alert", action="store_true", help="log alerts instead of posting")
    ap.add_argument("-v", action="store_true", help="print the log to stdout")
    args = ap.parse_args()
    VERBOSE = args.v
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(os.path.join(SYNC_DIR, "lock"), "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            return run(args)
        except Exception as e:
            state = load_state()
            alert(state, "error", f"sync error: {e}"[:600], not args.no_alert)
            save_state(state)
            return 2


if __name__ == "__main__":
    sys.exit(main())
