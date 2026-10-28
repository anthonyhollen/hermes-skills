#!/usr/bin/env python3
"""
backup_cleaner.py — find and (optionally) delete stale edit-backup files under the
Hermes skill/script tree, using a strict "the update was validated by a successful
run" criterion.

A backup is DELETABLE only when the live file it shadowed has since been exercised
by a cron job that ran successfully *after* the backup was made:

    a cron job whose `skill` field matches the backup's owning skill dir,
    OR whose `script` is exactly the backup's live file at ~/.hermes/scripts/,
    has last_status == "ok" AND last_run_at > backup.mtime (disabled jobs are ignored).

Anything that can't be tied to such a run (doc-only backups, interactive skills with
no cron, backups newer than their live file, orphans) is REPORT-ONLY and never
auto-deleted.

Safe by default: prints a report and changes nothing. `--delete` removes only the
DELETABLE set and appends every removal to a manifest log. Pure stdlib, zero LLM tokens.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DEFAULT_ROOTS = [os.path.join(HOME, ".hermes", "skills"),
                 os.path.join(HOME, ".hermes", "scripts")]
DEFAULT_JOBS = os.path.join(HOME, ".hermes", "cron", "jobs.json")
MANIFEST = os.environ.get("HERMES_BACKUP_CLEANER_LOG") or os.path.join(HOME, ".hermes", "scripts", ".backup_cleaner_log.json")
SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
HERMES_DIR = os.path.join(HOME, ".hermes")
# Dir where no_agent crons actually run their `script` (overridable for testing).
HERMES_SCRIPTS = os.environ.get("HERMES_SCRIPTS_DIR") or os.path.join(HERMES_DIR, "scripts")
# discord-table-format skill renderer (system python + tabulate + PIL); produces a PNG.
RENDERER = "/home/amh/.hermes/skills/productivity/discord-table-format/scripts/render_tables.py"

# Each strips a trailing backup marker to yield a candidate live filename; the real live
# file is the LONGEST candidate that exists (see live_candidates — longest = most specific =
# most conservative). A bare `.<word>.bak` marker is deliberately ABSENT: it also matches
# real extensions (`SKILL.md.bak`→`SKILL`, `run.sh.bak`→`run`) and could resolve a backup to
# an unrelated shorter file, so `.<word>.bak` backups whose live file has a different name are
# left ORPHAN/report-only rather than risk validating against the wrong live file.
BACKUP_MARKERS = [
    r"\.bak-[\w.\-]+$",            # .bak-tuneup-20260704 , .bak-audit
    r"\.\d{8}(?:_\d{6})?\.bak$",   # .20260606_014010.bak , .20260603.bak
    r"\.pre-[\w.\-]+\.bak$",       # .pre-vafeedback.bak , .pre-tuneup.bak
    r"\.bak$",                     # plain .bak (also handles SKILL.md.bak → SKILL.md)
    r"\.orig$",
    r"~$",
]
_MARKER_RE = re.compile("|".join(f"(?:{m})" for m in BACKUP_MARKERS))


def is_backup_name(name: str) -> bool:
    return bool(_MARKER_RE.search(name))


def live_candidates(bak_path: str) -> list:
    """Candidate live-file paths for a backup, LONGEST (most-specific) first. Longest =
    closest to the backup name = most conservative: prefers `X.pre-tuneup` over `X` for
    `X.pre-tuneup.bak` (a backup-of-a-backup shadows the intermediate, not X) and
    `SKILL.md` over `SKILL` for `SKILL.md.bak`. (The ambiguous `.<word>.bak` marker was
    removed, so no candidate over-strips to an unrelated *shorter* file like `run.sh.bak`→`run`.)"""
    d, name = os.path.split(bak_path)
    cands = set()
    for m in BACKUP_MARKERS:
        stripped, n = re.subn(m, "", name)
        if n and stripped and stripped != name:
            cands.add(stripped)
    return [os.path.join(d, c) for c in sorted(cands, key=len, reverse=True)]


def resolve_live(bak_path: str):
    """Return the existing live file this backup shadows (longest/most-specific existing
    candidate), or None (orphan)."""
    for cand in live_candidates(bak_path):
        if os.path.isfile(cand):
            return cand
    return None


def owning_skill(path: str):
    """Nearest ancestor dir containing a SKILL.md → (skill_dir, skill_name), else (None, None)."""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(12):  # bounded walk up to the filesystem root
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            return d, os.path.basename(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, None


def load_jobs(jobs_path: str) -> list:
    try:
        with open(jobs_path) as f:
            data = json.load(f)
    except Exception:
        return []
    jobs = data if isinstance(data, list) else (data.get("jobs", data) if isinstance(data, dict) else [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)]


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        # A tz-naive timestamp is interpreted as SYSTEM LOCAL time (the Pi's scheduler
        # writes +08:00 stamps), not UTC — assuming UTC would read a run up to the local
        # offset LATER than reality and could flip a NOT_YET into a (wrong) DELETABLE.
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


def build_indexes(jobs):
    by_skill, by_script = {}, {}
    for j in jobs:
        if j.get("enabled") is False:
            continue  # a stale `ok` from a disabled job is not evidence the file still works
        sk = j.get("skill")
        sc = j.get("script")
        if sk:
            by_skill.setdefault(sk, []).append(j)
        if sc:
            by_script.setdefault(os.path.basename(sc), []).append(j)
    return by_skill, by_script


def validating_run(jobs_for_unit, bak_mtime):
    """Return (job_id, last_run_at) of a job that ran OK after bak_mtime, else None."""
    best = None
    for j in jobs_for_unit:
        if j.get("last_status") != "ok":
            continue
        lr = parse_dt(j.get("last_run_at"))
        if lr and lr > bak_mtime:
            if best is None or lr > best[1]:
                best = (j.get("id"), lr)
    return best


def classify(bak_path, by_skill, by_script):
    """Return dict: status in {DELETABLE, NOT_YET, NO_CRON, ORPHAN, ANOMALY} + detail."""
    st = os.stat(bak_path)
    bak_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    info = {"path": bak_path, "size": st.st_size, "mtime": bak_mtime.isoformat()}

    live = resolve_live(bak_path)
    if not live:
        info.update(status="ORPHAN", reason="no live file found for this backup")
        return info
    info["live"] = live

    live_mtime = datetime.fromtimestamp(os.path.getmtime(live), tz=timezone.utc)
    if bak_mtime > live_mtime:
        info.update(status="ANOMALY",
                    reason="backup is NEWER than the live file — not a stale backup")
        return info

    skill_dir, skill_name = owning_skill(bak_path)
    jobs_for_unit, matched_by = [], []
    if skill_name and skill_name in by_skill:
        jobs_for_unit += by_skill[skill_name]; matched_by.append(f"skill={skill_name}")
    live_base = os.path.basename(live)
    # script-match ONLY when the live file IS the file a no_agent cron runs
    # (~/.hermes/scripts/<script>); a same-basename file elsewhere is a different unit.
    if live_base in by_script and os.path.dirname(os.path.abspath(live)) == HERMES_SCRIPTS:
        jobs_for_unit += by_script[live_base]; matched_by.append(f"script={live_base}")
    info["skill"] = skill_name

    if not jobs_for_unit:
        info.update(status="NO_CRON",
                    reason="no cron job references this skill/script — can't verify a post-update run")
        return info

    v = validating_run(jobs_for_unit, bak_mtime)
    if v:
        info.update(status="DELETABLE", matched_by=matched_by,
                    validated_by=v[0], validated_run_at=v[1].isoformat())
    else:
        info.update(status="NOT_YET", matched_by=matched_by,
                    reason="matching cron(s) found but none ran OK after this backup's mtime")
    return info


def scan(roots, jobs_path):
    jobs = load_jobs(jobs_path)
    by_skill, by_script = build_indexes(jobs)
    results = []
    manifest_abs = os.path.abspath(MANIFEST)
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if not is_backup_name(fn):
                    continue
                p = os.path.join(dirpath, fn)
                if os.path.abspath(p) == manifest_abs:
                    continue
                try:
                    results.append(classify(p, by_skill, by_script))
                except OSError:
                    continue
    return results


def _sz(n):
    for u, d in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024), ("B", 1)):
        if n >= d or u == "B":
            return f"{n/d:.1f}{u}" if u != "B" else f"{n}B"


def append_manifest(entries):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    log = []
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST) as f:
                log = json.load(f)
            if not isinstance(log, list):
                raise ValueError("manifest is not a JSON list")
        except Exception:
            # Don't silently discard deletion history — preserve the bad file for inspection.
            try:
                os.replace(MANIFEST, MANIFEST + ".corrupt-"
                           + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            except OSError:
                pass
            log = []
    log.extend(entries)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, MANIFEST)


def do_delete(deletable):
    deleted, entries = [], []
    now = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)  # so provenance can always be written
    for r in deletable:
        try:
            os.remove(r["path"])
        except OSError as e:
            print(f"  ! failed to delete {r['path']}: {e}", file=sys.stderr)
            continue
        deleted.append(r)
        entries.append({"deleted_at": now, "path": r["path"], "size": r["size"],
                        "mtime": r["mtime"], "live": r.get("live"),
                        "validated_by": r.get("validated_by"),
                        "validated_run_at": r.get("validated_run_at")})
    if entries:
        try:
            append_manifest(entries)
        except Exception as e:
            # Files are already gone — never lose the record; dump provenance to stderr.
            print(f"[backup-cleaner] manifest write FAILED ({e}); deleted entries:\n"
                  + json.dumps(entries, indent=2), file=sys.stderr)
    return deleted


def _group(results):
    groups = {}
    for r in results:
        groups.setdefault(r["status"], []).append(r)
    return groups


DISCORD_CAP = 1800  # headroom under Discord's 2000-char message limit


def _action_line(deletable, deleted):
    """Trailing action text, shared by the renderers. When `deleted` is non-empty the run
    already auto-removed the validated set (the cron path) → report what was removed;
    otherwise prompt to run --delete. Handles a partial-failure delete honestly."""
    if deleted:
        dsz = _sz(sum(r["size"] for r in deleted))
        if len(deleted) < len(deletable):
            failed = len(deletable) - len(deleted)
            return (f"🗑️ Auto-removed {len(deleted)} of {len(deletable)} validated backup(s) "
                    f"({failed} could not be removed — see logs); freed {dsz}. Logged to the manifest.")
        return (f"🗑️ Auto-removed {len(deleted)} validated backup(s), freed {dsz}. "
                f"Logged to the manifest.")
    return (f"Run `python3 ~/.hermes/scripts/backup_cleaner.py --delete` to remove these "
            f"and free {_sz(sum(r['size'] for r in deletable))}.")


def render_summary(results, deleted=()):
    """Discord-safe: LISTS the validated items (truncating with an '…and N more'
    note if they'd overflow) + report-only counts + the action line. `deleted` non-empty
    → past-tense 'removed' wording (the cron auto-deleted them)."""
    g = _group(results)
    deletable = sorted(g.get("DELETABLE", []), key=lambda x: x["path"])
    total = sum(r["size"] for r in deletable)
    label, count, sz = (("removed", len(deleted), _sz(sum(r["size"] for r in deleted)))
                        if deleted else ("deletable", len(deletable), _sz(total)))
    header = f"## 🧹 Backup Cleaner — {count} {label} ({sz})"
    counts = (f"Report-only (kept): {len(g.get('NOT_YET', []))} waiting · "
              f"{len(g.get('NO_CRON', []))} no-cron · {len(g.get('ORPHAN', []))} orphan · "
              f"{len(g.get('ANOMALY', []))} anomaly.")
    if not deletable:
        return f"{header}\n{len(results)} backups scanned. {counts}"

    cmd = _action_line(deletable, deleted)
    list_label = ("**Removed**" if deleted else "**Deletable**") + " (validated by a successful post-update run):"
    lines = [header, list_label]
    # reserve room for the command + counts + a possible overflow note
    used = sum(len(x) + 1 for x in lines) + len(cmd) + len(counts) + 90
    shown = 0
    for r in deletable:
        line = f"- `{_txt(_short(r['path']))}` ({_sz(r['size'])}, mod {r['mtime'][:10]})"
        if used + len(line) + 1 > DISCORD_CAP:
            break
        lines.append(line); used += len(line) + 1; shown += 1
    if shown < len(deletable):
        lines.append(f"…and {len(deletable) - shown} more — run `backup_cleaner.py` (no args) for the full list.")
    lines.append(cmd)
    lines.append(counts)
    return "\n".join(lines)


def render_full(results, deleted=()):
    """Full human report for the terminal (unbounded): validated list + report-only groups.
    `deleted` non-empty → past-tense 'removed' wording (the run auto-deleted them)."""
    g = _group(results)
    deletable = g.get("DELETABLE", [])
    total = sum(r["size"] for r in deletable)
    head_label, count, sz = (("removed", len(deleted), _sz(sum(r["size"] for r in deleted)))
                             if deleted else ("deletable", len(deletable), _sz(total)))
    lines = [f"## 🧹 Backup Cleaner — {count} {head_label} ({sz}), "
             f"{len(results)} backup files scanned"]
    if deletable:
        lines.append("\n**" + ("Removed" if deleted else "Deletable")
                     + "** (validated by a successful post-update run):")
        for r in sorted(deletable, key=lambda x: x["path"]):
            lines.append(f"- `{_txt(_short(r['path']))}` ({_sz(r['size'])}, mod {r['mtime'][:10]}) "
                         f"→ ok run {r['validated_run_at'][:10]} [{r.get('validated_by')}]")
    for status, label in (("NOT_YET", "Waiting for a successful run"),
                          ("NO_CRON", "No cron to verify (manual review)"),
                          ("ORPHAN", "Orphan — live file gone (manual review)"),
                          ("ANOMALY", "Backup newer than live file (manual review)")):
        gg = g.get(status, [])
        if gg:
            lines.append(f"\n**{label}** — {len(gg)}:")
            for r in sorted(gg, key=lambda x: x["path"]):
                lines.append(f"- `{_txt(_short(r['path']))}` (mod {r['mtime'][:10]}) — {r.get('reason','')}")
    if deletable:
        lines.append("\n" + _action_line(deletable, deleted))
    return "\n".join(lines)


def _short(p):
    """Path relative to ~/.hermes (the scan scope) — the ~/.hermes/ prefix is implied."""
    try:
        return os.path.relpath(p, HERMES_DIR)
    except ValueError:
        return p


def _txt(s):
    """Neutralize Discord formatting/injection chars in an interpolated string for the
    TEXT post path (a crafted backup filename mustn't ping/format-break/inject a link).
    The image path is safe separately: the renderer draws cells as pixels."""
    return (s.replace("`", "'").replace("\r", " ").replace("\n", " ")
             .replace("@", "@​").replace("](", "]​(").replace("://", ":​//"))


def _load_discord_token():
    # ~/.hermes/.env is authoritative and the only source: the cron scheduler does NOT
    # export env vars to no_agent scripts, so never rely on os.environ here.
    try:
        with open(os.path.join(HOME, ".hermes", ".env")) as f:
            for line in f:
                line = line.strip()
                for key in ("DISCORD_BOT_TOKEN=", "DISCORD_TOKEN="):
                    if line.startswith(key):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def post_discord(channel, content):
    token = _load_discord_token()
    if not token:
        print("no Discord token available", file=sys.stderr)
        return False
    body = json.dumps({"content": content[:1990], "allowed_mentions": {"parse": []}})
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "20", "-w", "\n%{http_code}", "-X", "POST",
            f"https://discord.com/api/v10/channels/{channel}/messages",
            "-H", f"Authorization: Bot {token}",
            "-H", "Content-Type: application/json",
            "-H", "User-Agent: DiscordBot (hermes-backup-cleaner, 1.0)",
            "-d", body], capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        print("Discord post timed out", file=sys.stderr)
        return False
    code = (r.stdout or "").rpartition("\n")[2].strip()  # real HTTP status, not just curl exit
    return r.returncode == 0 and code.startswith("2")


def _render_table_png(headers, rows, prefix="backup_cleaner"):
    """Render a table image via the discord-table-format skill renderer. Returns a
    validated PNG path, or None on any failure (caller falls back to text)."""
    if not os.path.isfile(RENDERER):
        return None
    try:
        p = subprocess.run(["/usr/bin/python3", RENDERER, "--one",
                            json.dumps(headers), json.dumps(rows), prefix],
                           capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (p.stdout or "").strip().splitlines()
    png = out[-1].strip() if out else ""
    if p.returncode != 0 or not png or not os.path.isfile(png):
        return None
    try:  # confirm it is really a PNG, not an error string written to a path
        with open(png, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
    except OSError:
        return None
    return png


def _upload_image(channel, png, caption):
    """Attach an image to a Discord message in one REST multipart call (no MEDIA: tag —
    the scheduler reorders those). content + file go together."""
    token = _load_discord_token()
    if not token:
        print("no Discord token available", file=sys.stderr)
        return False
    payload = json.dumps({"content": caption[:1990], "allowed_mentions": {"parse": []}})
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "45", "-X", "POST",
            f"https://discord.com/api/v10/channels/{channel}/messages",
            "-H", f"Authorization: Bot {token}",
            "-H", "User-Agent: DiscordBot (hermes-backup-cleaner, 1.0)",
            "-F", "payload_json=" + payload,
            "-F", f"files[0]=@{png};type=image/png"],
            capture_output=True, text=True, timeout=50)
    except subprocess.TimeoutExpired:
        print("Discord image upload timed out", file=sys.stderr)
        return False
    return r.returncode == 0 and '"id"' in (r.stdout or "")


def post_report_image(channel, results, deleted=()):
    """Render the FULL validated list as a table image and upload it (caption carries the
    counts + the action line). Falls back to a text summary if rendering/upload fails.
    `deleted` non-empty → caption reports what was auto-removed (the cron path)."""
    g = _group(results)
    deletable = sorted(g.get("DELETABLE", []), key=lambda x: x["path"])
    total = sum(r["size"] for r in deletable)
    label, count, sz = (("removed", len(deleted), _sz(sum(r["size"] for r in deleted)))
                        if deleted else ("deletable", len(deletable), _sz(total)))
    caption = f"## 🧹 Backup Cleaner — {count} {label} ({sz})"
    if deletable:
        caption += " — full list attached"
    caption += (f"\nReport-only (kept): {len(g.get('NOT_YET', []))} waiting · "
                f"{len(g.get('NO_CRON', []))} no-cron · {len(g.get('ORPHAN', []))} orphan · "
                f"{len(g.get('ANOMALY', []))} anomaly.")
    if deletable:
        caption += "\n" + _action_line(deletable, deleted)
    if not deletable:
        return post_discord(channel, caption)  # nothing to tabulate

    headers = ["File (under ~/.hermes)", "Size", "Modified", "Validated (ok run)"]
    rows = [[_short(r["path"]), _sz(r["size"]), r["mtime"][:10],
             f"{r['validated_run_at'][:10]} {r.get('validated_by')}"] for r in deletable]
    png = _render_table_png(headers, rows)
    if not png:
        print("table render failed — falling back to text summary", file=sys.stderr)
        return post_discord(channel, render_summary(results, deleted=deleted))
    ok = _upload_image(channel, png, caption)
    try:
        os.remove(png)  # clean up temp image after delivery
    except OSError:
        pass
    if not ok:
        return post_discord(channel, render_summary(results, deleted=deleted))
    return ok


def main():
    ap = argparse.ArgumentParser(description="Find/delete validated stale edit-backups.")
    ap.add_argument("--delete", action="store_true", help="delete the DELETABLE set (default: report only)")
    ap.add_argument("--quiet", action="store_true", help="suppress the stdout report on success (for the cron); a FAILED --post still dumps it")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--post", metavar="CHANNEL_ID", help="post the report to a Discord channel as a table image (full deletable list); text fallback if rendering fails")
    ap.add_argument("--roots", nargs="+", default=DEFAULT_ROOTS, help="dirs to scan (testing)")
    ap.add_argument("--jobs", default=DEFAULT_JOBS, help="path to cron jobs.json (testing)")
    args = ap.parse_args()

    results = scan(args.roots, args.jobs)
    deletable = [r for r in results if r["status"] == "DELETABLE"]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    # Delete BEFORE reporting (when --delete is set) so the report/caption states what was
    # removed rather than telling the reader to run a --delete that has already happened —
    # this is the cron's auto-delete path. do_delete removes ONLY the validated DELETABLE
    # set and logs each removal to the manifest; report-only classes are never touched.
    deleted = do_delete(deletable) if (args.delete and deletable) else []

    post_failed = False
    if args.post and deletable:
        post_failed = not post_report_image(args.post, results, deleted=deleted)

    # stdout report: full report unless --quiet succeeded. A FAILED --post ALWAYS dumps the
    # report to stdout (even under --quiet) so a deliver:local cron still preserves it.
    if post_failed or not args.quiet:
        print(render_full(results, deleted=deleted))
    elif deletable and not args.post:
        print(render_summary(results, deleted=deleted))  # --quiet, no post → bounded text summary

    if deleted:
        print(f"\n🗑️  Deleted {len(deleted)} backup file(s), "
              f"freed {_sz(sum(r['size'] for r in deleted))}. Logged to {_short(MANIFEST)}.")

    if post_failed:
        print("[backup-cleaner] Discord delivery FAILED — the report above was NOT posted.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
