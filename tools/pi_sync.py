#!/usr/bin/env python3
"""pi_sync.py - two-way sync between the live Hermes install and this git repo.

Runs on amhpi5 from the system crontab every 10 minutes (and by hand). Cron runs the
promoted copy .sync/pi_sync_stable.py, never tools/pi_sync.py directly, so a broken
push of this file can't take the sync down (see promote_self). One pass:

  1. capture  - commit live edits (Hermes / SSH) as "pi: live edits - ..."
  2. rebase   - fetch origin and rebase onto origin/main (edits pushed from the Mac)
  3. push     - push the Pi's commits
  4. check    - py_compile / bash -n / SKILL.md frontmatter / JSON on files about to be written
  5. apply    - write incoming changes (last_applied..HEAD) into the live install

Baseline: state["last_applied"] is the commit the live install matches. A live file
is a *live edit* only if it differs from both HEAD and the baseline. A file equal to
the baseline but not HEAD is just waiting to be applied, so it is never captured
(capturing it would commit a revert).

Safety rules:
  - Rebase conflict: nothing applied; the Pi's side goes to branch pi-conflict; alert.
  - Failed check: nothing applied, BLOCKED, alert. Capture keeps running, and each run
    re-checks and resumes once a fix arrives (from GitHub, or a live edit on the Pi).
  - Capture refuses files over 1 MB, binary files, anything containing a secret, and
    mass deletions (more than 5 files, or a SKILL.md, or a missing root dir); it alerts.
  - Before overwriting or deleting a live file that holds an edit git doesn't have,
    keep it as <file>.bak-pisync-<ts>. Live file modes are preserved.
  - Skips the run if a live file with an un-synced change was modified < 60 s ago.

Usage: pi_sync.py [--dry-run] [--no-alert] [-v] [--self-test]
"""
import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".hermes")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF_SRC = os.path.join(REPO, "tools", "pi_sync.py")
SYNC_DIR = os.path.join(REPO, ".sync")
STABLE = os.path.join(SYNC_DIR, "pi_sync_stable.py")
STATE_FILE = os.path.join(SYNC_DIR, "state.json")
LOG_FILE = os.path.join(SYNC_DIR, "sync.log")
BRANCH = "main"
CONFLICT_BRANCH = "pi-conflict"
APPLIED_TAG = "pi-applied"
REPO_URL = "https://github.com/anthonyhollen/hermes-skills"
ALERT_CHANNEL = "1501742338639401071"  # Discord playground
ALERT_REPEAT_S = 6 * 3600
QUIET_S = 60
HUNG_S = 2 * 3600          # lock held and no successful run for this long -> alert
MAX_FILE_BYTES = 1_000_000
MAX_DELETES = 5
GIT_TIMEOUT_S = 120

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
PRUNE_DIRS = {"__pycache__", "logs", "cache", "node_modules", "venv"}
SECRET_PATTERNS = [re.compile(p) for p in (
    rb"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",  # Discord bot token
    rb"\bsk-[A-Za-z0-9_-]{20,}", rb"\bghp_[A-Za-z0-9]{36}", rb"\bgithub_pat_[A-Za-z0-9_]{30,}",
    rb"\bAKIA[0-9A-Z]{16}\b", rb"\bAIza[0-9A-Za-z_-]{35}", rb"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)]

VERBOSE = False


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    if VERBOSE or sys.stdout.isatty():
        print(line)


def git(*args, check=True, input=None, timeout=GIT_TIMEOUT_S):
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", REPO, *args],
                           capture_output=True, text=True, input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args[:2])} timed out after {timeout}s")
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


# ---------------------------------------------------------------- Discord

def _env_pairs():
    # ~/.hermes/.env is authoritative (same convention as backup_cleaner.py).
    try:
        with open(os.path.join(HERMES, ".env")) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    yield k.strip(), v.strip().strip('"').strip("'")
    except OSError:
        return


def _discord_token():
    env = dict(_env_pairs())
    return env.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_TOKEN")


def post_discord(text):
    """Post one message to the playground channel; True on success. Never raises."""
    token = _discord_token()
    if not token:
        log("no Discord token; message not posted")
        return False
    body = json.dumps({"content": text[:1990], "allowed_mentions": {"parse": []},
                       "flags": 4}).encode()  # 4 = SUPPRESS_EMBEDS: no link previews
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{ALERT_CHANNEL}/messages", data=body,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (hermes-skills-sync, 1.0)"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:  # never let Discord break the sync
        log(f"Discord post failed: {e}")
        return False


def alert(state, key, text, enabled=True):
    """Post to Discord once per key per ALERT_REPEAT_S."""
    log("ALERT " + text.replace("\n", " | "))
    if not enabled:
        return
    sent = state.setdefault("alerts", {})
    if time.time() - sent.get(key, 0) < ALERT_REPEAT_S:
        return
    if post_discord("**hermes-skills sync** " + text):
        sent[key] = time.time()


def commit_lines(shas):
    lines = []
    for sha in shas:
        subject = git("log", "-1", "--format=%s", sha).stdout.strip()
        files = [f for f in git("show", "-z", "--name-only", "--format=", sha).stdout.split("\0") if f.strip()]
        where = sorted({"/".join(f.strip().split("/")[:3]) if f.strip().startswith("skills/") else f.strip()
                        for f in files})
        lines.append(f"- `{sha[:7]}` {subject} — {', '.join(where[:4])}"
                     + (f" (+{len(where) - 4})" if len(where) > 4 else "")
                     + f" <{REPO_URL}/commit/{sha}>")
    return lines


def notify_commits(pi_side, github_side, enabled=True):
    """One Discord message per run that moved commits: the Pi's own edits and the
    GitHub commits now live on the Pi."""
    parts = []
    if pi_side:
        parts.append(f"**Pi → GitHub** ({len(pi_side)}): live edits on the Pi")
        parts += commit_lines(pi_side[:10])
    if github_side:
        parts.append(f"**GitHub → Pi** ({len(github_side)}): now live")
        parts += commit_lines(github_side[:10])
    if not parts:
        return
    log("NOTIFY " + " | ".join(parts))
    if enabled:
        post_discord("📦 **hermes-skills**\n" + "\n".join(parts))


# ---------------------------------------------------------------- git trees & files

def live_path(rel):
    for prefix, live in sorted(ROOTS.items(), key=lambda kv: -len(kv[0])):
        if rel.startswith(prefix + "/"):
            return os.path.join(live, rel[len(prefix) + 1:])
    return None


def tree(rev):
    """{repo path: (mode, blob sha)} for the mirrored roots at rev."""
    out = git("ls-tree", "-r", "-z", rev, "--", *ROOTS).stdout
    t = {}
    for entry in out.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            mode, _, sha = meta.split()
            t[path] = (mode, sha)
    return t


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def read_live(rel):
    """(bytes, blob sha) of the live file, or (None, None) if it isn't a regular file."""
    lp = live_path(rel)
    if not lp or os.path.islink(lp) or not os.path.isfile(lp):
        return None, None
    with open(lp, "rb") as f:
        data = f.read()
    return data, blob_sha(data)


def sha_of(t, rel):
    return t[rel][1] if rel in t else None


def not_ignored(paths):
    if not paths:
        return []
    r = git("check-ignore", "-z", "--no-index", "--stdin", check=False, input="\0".join(paths) + "\0")
    ignored = set(r.stdout.split("\0"))
    return [p for p in paths if p not in ignored]


def capture_candidates(tracked):
    """Repo paths whose live copies should be mirrored: every tracked file, plus new
    non-ignored files in tracked skill dirs and new top-level scripts."""
    new = set()
    skill_dirs = {os.path.dirname(p) for p in tracked
                  if p.startswith("skills/") and os.path.basename(p) == "SKILL.md"}
    for sd in skill_dirs:
        base = live_path(sd)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".venv")]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = sd + "/" + os.path.relpath(full, base)
                if rel not in tracked and not os.path.islink(full):
                    new.add(rel)
    sdir = ROOTS["scripts"]
    for fn in os.listdir(sdir):
        full = os.path.join(sdir, fn)
        if (os.path.isfile(full) and not os.path.islink(full)
                and any(fnmatch.fnmatch(fn, p) for p in SCRIPT_INCLUDE)
                and not any(fnmatch.fnmatch(fn, p) for p in SCRIPT_EXCLUDE)
                and "scripts/" + fn not in tracked):
            new.add("scripts/" + fn)
    # Tracked files are always candidates, even if a later .gitignore rule matches them.
    return sorted(set(tracked) | set(not_ignored(sorted(new))))


def load_secrets():
    """Byte strings that must never be committed: credential values from ~/.hermes/.env
    and token-like fields of the auth JSON files. IDs and URLs are left out (several
    already appear in tracked code, harmlessly)."""
    vals = set()
    for k, v in _env_pairs():
        if (re.search(r"TOKEN|SECRET|KEY|PASSWORD|PASS\b|PAT\b", k) and not k.endswith("_ID")
                and len(v) >= 16 and not v.isdigit() and not v.startswith("http")):
            vals.add(v.encode())

    def walk(o, key=""):
        if isinstance(o, dict):
            for k2, v2 in o.items():
                yield from walk(v2, k2)
        elif isinstance(o, list):
            for v2 in o:
                yield from walk(v2, key)
        elif (isinstance(o, str) and len(o) >= 20 and not o.startswith("http")
              and re.search(r"token|secret|key|password|refresh|access", key, re.I)
              and "client_id" not in key.lower()):
            yield o.encode()

    for name in ("auth.json", "google_token.json", "google_client_secret.json"):
        try:
            with open(os.path.join(HERMES, name)) as f:
                vals.update(walk(json.load(f)))
        except (OSError, ValueError):
            pass
    return vals


def capture_problem(data, secrets):
    if len(data) > MAX_FILE_BYTES:
        return f"larger than {MAX_FILE_BYTES // 1_000_000} MB"
    if b"\0" in data[:8192]:
        return "binary file"
    if any(s in data for s in secrets) or any(p.search(data) for p in SECRET_PATTERNS):
        return "contains a secret (credential value or token pattern)"
    return None


def write_file(dst, data, mode):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".pisync-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def live_mode(lp, git_mode):
    """Keep the live file's mode (skill files are 600 by convention; a 644 git checkout
    would loosen it), with exec bits matching git. New files: 600, or 700 if executable."""
    exe = git_mode == "100755"
    try:
        mode = os.stat(lp).st_mode & 0o777
    except FileNotFoundError:
        return 0o700 if exe else 0o600
    return (mode | ((mode & 0o444) >> 2)) if exe else (mode & ~0o111)


def check_file(rel):
    """Return an error string, or None if the file (in the repo working tree) is safe to apply."""
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


def twin_mismatches(t, paths):
    """scripts/<name> and skills/**/scripts/<name> are two copies of one file that must
    stay byte-identical (cron runs the first). Pairs touched by `paths` that differ in t."""
    top = {os.path.basename(p): p for p in t if p.count("/") == 1 and p.startswith("scripts/")}
    pairs = [(top[os.path.basename(p)], p) for p in t
             if p.startswith("skills/") and os.path.basename(os.path.dirname(p)) == "scripts"
             and os.path.basename(p) in top]
    touched = set(paths)
    return [f"{a} != {b}" for a, b in pairs
            if (a in touched or b in touched) and sha_of(t, a) != sha_of(t, b)]


def mark_applied(state, sha):
    """Record sha as live and publish it as tag pi-applied, so the Mac can see what's deployed."""
    state.update(last_applied=sha, blocked=None, last_ok=time.time())
    state.get("alerts", {}).pop("conflict", None)
    if state.get("tag") != sha:
        if git("push", "-q", "-f", "origin", f"{sha}:refs/tags/{APPLIED_TAG}", check=False).returncode == 0:
            state["tag"] = sha
    save_state(state)


def promote_self(state, enabled):
    """Cron runs STABLE. Replace it with tools/pi_sync.py only once the new version
    compiles and passes --self-test, so a broken push can't stop the sync."""
    try:
        with open(SELF_SRC, "rb") as f:
            src = f.read()
        with open(STABLE, "rb") as f:
            if f.read() == src:
                return
    except FileNotFoundError:
        pass
    r = subprocess.run([sys.executable, SELF_SRC, "--self-test"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
        alert(state, f"selftest-{blob_sha(src)[:8]}", "new tools/pi_sync.py failed its self-test, "
              f"still running the previous version: {err[0][:300]}", enabled)
        return
    write_file(STABLE, src, 0o700)
    log(f"promoted tools/pi_sync.py ({blob_sha(src)[:8]}) to the stable copy")


# ---------------------------------------------------------------- one pass

def run(args):
    state = load_state()
    alerts_on = not args.no_alert

    # Leftovers from a crashed run: an unfinished rebase or uncommitted changes. Live
    # still holds any edits, so capture below simply redoes them.
    if any(os.path.isdir(os.path.join(REPO, ".git", d)) for d in ("rebase-merge", "rebase-apply")):
        git("rebase", "--abort", check=False)
        log("aborted a leftover rebase")
    if git("status", "--porcelain", "-z", "--untracked-files=all", "--", *ROOTS).stdout.strip("\0"):
        git("reset", "-q", "--hard", "HEAD")
        git("clean", "-fdq", "--", *ROOTS)
        log("reset a dirty working tree")

    for prefix, live in ROOTS.items():
        if not os.path.isdir(live):
            alert(state, f"root-{prefix}", f"live dir {live} is missing; sync paused (nothing captured).", alerts_on)
            save_state(state)
            return 1

    head = git("rev-parse", "HEAD").stdout.strip()
    base = state.get("last_applied") or head
    if git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode:
        log(f"baseline {base[:8]} missing; using HEAD")
        base = head
    t_head, t_base = tree(head), tree(base)

    cands = capture_candidates(t_head)
    live = {rel: read_live(rel) for rel in cands}
    now = time.time()
    for rel, (data, sha) in live.items():
        if data is not None and sha not in (sha_of(t_head, rel), sha_of(t_base, rel)):
            if now - os.path.getmtime(live_path(rel)) < QUIET_S:
                log(f"skip: {rel} modified <{QUIET_S}s ago")
                return 0

    # 1. capture
    secrets = load_secrets()
    changed = []
    for rel, (data, sha) in live.items():
        if data is None or sha in (sha_of(t_head, rel), sha_of(t_base, rel)):
            continue  # unchanged, or just waiting for an incoming change to be applied
        problem = capture_problem(data, secrets)
        if problem:
            alert(state, f"refused-{rel}-{sha[:8]}", f"not capturing `{rel}`: {problem}. "
                  "The live file is untouched; it stays out of git until fixed.", alerts_on)
            continue
        changed.append(rel)
    missing = [rel for rel in t_head if live.get(rel, (None, None))[0] is None]
    removed = [rel for rel in missing if rel in t_base]  # was live at baseline, now gone
    if removed and (len(removed) > MAX_DELETES or any(r.endswith("/SKILL.md") for r in removed)):
        alert(state, "mass-delete", f"{len(removed)} tracked file(s) vanished from live, e.g. "
              f"{', '.join(removed[:5])}. Not capturing the deletion. If it's intended, delete "
              "them in git from the Mac; if not, restore them.", alerts_on)
        removed = []
    if changed or removed:
        log(f"capture: {len(changed)} changed, {len(removed)} removed: " + ", ".join((changed + removed)[:10]))
        if args.dry_run:
            log("dry-run: stopping before commit")
            return 0
        for rel in changed:
            lp = live_path(rel)
            write_file(os.path.join(REPO, rel), live[rel][0], os.stat(lp).st_mode & 0o777)
        if changed:
            git("add", "-A", "--", *changed)
        if removed:
            git("rm", "-q", "--cached", "--ignore-unmatch", "--", *removed)
        summary = ", ".join(os.path.basename(p) for p in (changed + removed)[:5])
        more = len(changed) + len(removed) - 5
        git("commit", "-q", "-m", f"pi: live edits - {summary}" + (f" (+{more} more)" if more > 0 else ""))
        git("reset", "-q", "--hard", "HEAD")  # drop working-tree copies of removed files
    elif args.dry_run:
        log("dry-run: nothing to capture")
        return 0

    # 2. rebase onto origin
    pre = git("rev-parse", "HEAD").stdout.strip()
    t_pre = tree(pre)
    git("fetch", "-q", "origin", BRANCH, timeout=180)
    r = git("rebase", "-q", f"origin/{BRANCH}", check=False)
    if r.returncode:
        conflicts = [p for p in git("diff", "-z", "--name-only", "--diff-filter=U", check=False).stdout.split("\0") if p]
        git("rebase", "--abort", check=False)
        if not conflicts:
            raise RuntimeError(f"rebase failed without conflicts: {(r.stderr or r.stdout).strip()[:300]}")
        # Publish the Pi's side so the conflict can be resolved from the Mac:
        # merge origin/pi-conflict into main there, push, and the next run fast-forwards.
        git("push", "-q", "-f", "origin", f"HEAD:{CONFLICT_BRANCH}", check=False, timeout=180)
        state["conflict"] = True
        alert(state, "conflict", "rebase conflict between Pi live edits and GitHub; nothing applied. "
              f"Files: {', '.join(conflicts)}. The Pi's commits are on branch "
              f"`{CONFLICT_BRANCH}`: merge it into main (git merge, not rebase) and push.", alerts_on)
        save_state(state)
        return 1
    new = git("rev-parse", "HEAD").stdout.strip()
    if state.pop("conflict", None):
        git("push", "-q", "origin", "--delete", CONFLICT_BRANCH, check=False, timeout=180)
        log(f"conflict resolved; deleted branch {CONFLICT_BRANCH}")

    # 3. push
    pushed = git("rev-list", "--reverse", f"origin/{BRANCH}..HEAD").stdout.split()
    if pushed:
        p = git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False, timeout=180)
        if p.returncode:
            alert(state, "push", f"push failed (will retry): {p.stderr.strip()[:300]}", alerts_on)
            pushed = []
        else:
            log(f"pushed {len(pushed)} commit(s)")
            state.get("alerts", {}).pop("push", None)

    if new == base:
        notify_commits(pushed, [], alerts_on)
        mark_applied(state, new)
        return 0

    # 4. check what would actually be written (Pi-origin content is already live)
    t_new = tree(new)
    out = git("diff", "-z", "--name-status", "--no-renames", base, new, "--", *ROOTS).stdout.split("\0")
    incoming, deleted = [], []
    for status, rel in zip(out[0::2], out[1::2]):
        if live_path(rel):
            (deleted if status == "D" else incoming).append(rel)
    to_write = []
    for rel in incoming:
        data, sha = read_live(rel) if rel not in live else live[rel]
        lp = live_path(rel)
        mode_ok = data is not None and (os.stat(lp).st_mode & 0o111 != 0) == (t_new[rel][0] == "100755")
        if sha != sha_of(t_new, rel) or not mode_ok:
            to_write.append(rel)
    failures = [f"{rel}: {err}" for rel in to_write if (err := check_file(rel))]
    if failures:
        state["blocked"] = {"sha": new, "failures": failures}
        alert(state, f"blocked-{new}", f"commit {new[:8]} NOT applied - checks failed:\n"
              + "\n".join(failures[:8]) + "\nPush a fix; the sync resumes automatically.", alerts_on)
        notify_commits(pushed, [], alerts_on)
        save_state(state)
        return 1
    # Warn only: Hermes often edits just the cron copy, and blocking on that would wedge the sync.
    twins = twin_mismatches(t_new, incoming)
    if twins:
        alert(state, f"twins-{new}", f"commit {new[:8]} applied, but twin copies now differ "
              "(make both byte-identical):\n" + "\n".join(twins[:8]), alerts_on)

    # 5. apply
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backed_up = []

    def keep_unsynced(rel):
        """Back up a live file holding content git has never seen (not baseline, not captured)."""
        data, sha = read_live(rel)
        if data is not None and sha not in (sha_of(t_base, rel), sha_of(t_pre, rel), sha_of(t_new, rel)):
            shutil.copy2(live_path(rel), f"{live_path(rel)}.bak-pisync-{stamp}")
            backed_up.append(rel)

    for rel in to_write:
        lp = live_path(rel)
        keep_unsynced(rel)
        with open(os.path.join(REPO, rel), "rb") as f:
            write_file(lp, f.read(), live_mode(lp, t_new[rel][0]))
    for rel in deleted:
        lp = live_path(rel)
        if os.path.isfile(lp):
            keep_unsynced(rel)
            os.remove(lp)
    log(f"applied {new[:8]}: {len(to_write)} written, {len(deleted)} deleted")
    if backed_up:
        alert(state, f"overwrote-{new}", "applied GitHub changes over un-synced live edits; "
              "live copies kept as .bak-pisync-" + stamp + ": " + ", ".join(backed_up[:8]), alerts_on)
    commits = [c for c in git("rev-list", "--reverse", f"{base}..{new}").stdout.split() if c not in pushed]
    pi_side = pushed + [c for c in commits if git("log", "-1", "--format=%s", c).stdout.startswith("pi: ")]
    notify_commits(pi_side, [c for c in commits if c not in pi_side], alerts_on)
    mark_applied(state, new)
    return 0


def self_test():
    """Read-only smoke test used before promoting a new version (see promote_self)."""
    t = tree("HEAD")
    assert t, "empty tree"
    cands = capture_candidates(t)
    assert all(live_path(c) for c in cands), "unmapped candidate"
    load_secrets()
    print(f"self-test ok: {len(t)} tracked, {len(cands)} candidates")
    return 0


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report live edits; change nothing")
    ap.add_argument("--no-alert", action="store_true", help="log alerts/notices instead of posting")
    ap.add_argument("--self-test", action="store_true", help="read-only smoke test, then exit")
    ap.add_argument("-v", action="store_true", help="print the log to stdout")
    args = ap.parse_args()
    VERBOSE = args.v
    if args.self_test:
        return self_test()
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(os.path.join(SYNC_DIR, "lock"), "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state = load_state()
            if time.time() - state.get("last_ok", time.time()) > HUNG_S:
                alert(state, "hung", "sync lock held and no successful run for over "
                      f"{HUNG_S // 3600} h; a run may be hung (check `ps` on the Pi).", not args.no_alert)
                save_state(state)
            return 0
        try:
            rc = run(args)
        except Exception as e:
            state = load_state()
            alert(state, "error", f"sync error: {e}"[:600], not args.no_alert)
            save_state(state)
            return 2
        if rc == 0 and not args.dry_run:
            state = load_state()
            promote_self(state, not args.no_alert)
            save_state(state)
        return rc


if __name__ == "__main__":
    sys.exit(main())
