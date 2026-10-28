#!/usr/bin/env python3
"""
skill_usage_report.py — self-delivering "most-used skills" report.

Collects skill load counts + last-used dates from state.db (successful
skill_view loads of SKILL.md only — supporting-file reads, failed loads and
same-session dedup stubs are not uses) and last-updated dates / version /
changelog note from the skills tree (~/.hermes/skills/), renders the
standard Discord table image (shared render_tables.py renderer) — the top N
plus, below a divider, every other skill loaded in the last --recent-days — and
posts it to the CURRENT Discord thread ITSELF (direct REST multipart curl).
The agent's only job is to run it once and relay the confirmation — the
script does all delivery, so the agent must never hand-build this table.

Usage (run exactly this; do NOT add a channel):
    python3 ~/.hermes/skills/productivity/skill-usage-report/scripts/skill_usage_report.py

Preferred when the agent knows the thread (session env can be stale):
    ... --thread-id 1554483444430676065

Test override (post to an explicit channel instead of the current thread):
    ... --to discord:1501742338639401071

Dry run (render only; print the PNG path; nothing posted):
    ... --dry-run

Rows: --top N (default 15), then lower-ranked skills loaded in the last
--recent-days N days (default 7; 0 = off) below a divider row.
Conventions (see no-agent-cron-scripts skill): '[SILENT]' absolute first
line, os._exit(0)/os._exit(1), stdlib fcntl.flock run guard, noisy-failure
traceback post to the home channel, secrets read from ~/.hermes/.env.
Ad-hoc only (deliberately no cron job).
"""

import argparse
import datetime
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import Counter

HOME_CHANNEL = "1499972245437550602"        # error posts only
PLAYGROUND_CHANNEL = "1501742338639401071"  # live tests (docs use this)
STATE_DB = os.path.expanduser("~/.hermes/state.db")
SKILLS_ROOT = os.path.expanduser("~/.hermes/skills")
RENDERER_DIR = os.path.expanduser(
    "~/.hermes/skills/productivity/discord-table-format/scripts")
CACHE_DIR = "/tmp/hermes_cache"
LOCK_FILE = os.path.join(CACHE_DIR, "skill_usage_report.lock")
DEFAULT_TOP = 15
DEFAULT_RECENT_DAYS = 7   # skills loaded this recently are listed even below the top N
RESULT_CHUNK = 400        # tool_call_ids per IN (...) lookup (well under SQLite's variable cap)

SKIP_DIRS = {"__pycache__", ".git", ".archive"}
EXCLUDE_SUFFIXES = (".pyc", ".db", ".log", ".json", ".csv", ".png", ".jpg",
                    ".jpeg", ".gif", ".sqlite", ".wal", ".shm")


class TargetNotResolved(Exception):
    """No Discord target — a flag/config issue, not a crash (no home post)."""


# ---------- Discord ----------

def load_token():
    """Discord bot token from ~/.hermes/.env (NOT hermes-agent/.env — stale)."""
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def resolve_discord_target(explicit=None, thread_id=None):
    """Return the Discord channel/thread id to POST into.

    Precedence: --to -> --thread-id -> SKILL_USAGE_TARGET (test kill-switch)
    -> HERMES_SESSION_KEY parts[4] -> HERMES_SESSION_THREAD_ID/CHAT_ID.
    Never falls back to a fixed channel — returns None so the caller fails
    loud instead of misfiring (pattern from traffic_camera_report.py).
    """
    if explicit:
        ref = explicit.split("discord:", 1)[-1] if explicit.startswith("discord:") else explicit
        parts = [p for p in ref.split(":") if p]
        return parts[-1] if parts else None  # thread id if given, else channel id
    if thread_id:
        return thread_id.strip()
    env_target = os.environ.get("SKILL_USAGE_TARGET", "").strip()
    if env_target:
        return env_target
    # Session key format: agent:main:<platform>:<chat_type>:<chat_id>[:...]
    # parts[4] (chat_id) is the channel/thread to post into.
    sk = os.environ.get("HERMES_SESSION_KEY", "")
    parts = sk.split(":")
    if len(parts) >= 5 and parts[2] == "discord" and parts[4]:
        return parts[4]
    tid = os.environ.get("HERMES_SESSION_THREAD_ID", "").strip()
    cid = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    return tid or cid or None


def discord_post(token, channel, content, img=None, _retried=False):
    """One content (+ optional image) message via REST multipart curl.
    Returns (ok, detail). One 429 retry with a clamped retry_after."""
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    cmd = ["curl", "-sS", "--max-time", "8", "-w", "\n%{http_code}",
           "-X", "POST", url, "-H", f"Authorization: Bot {token}",
           "--form-string", f"payload_json={payload}"]
    if img and os.path.exists(img):
        cmd += ["-F", f"file=@{img}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "curl timeout"
    body, _, code = r.stdout.rpartition("\n")
    code = code.strip()
    if code == "429" and not _retried:
        try:
            time.sleep(min(float(json.loads(body).get("retry_after", 1)) + 0.25, 3))
        except Exception:
            time.sleep(1)
        return discord_post(token, channel, content, img, _retried=True)
    ok = r.returncode == 0 and code.startswith("2")
    detail = f"http={code or '?'} rc={r.returncode}"
    if not ok:
        detail += f" body={body[:200]!r}"
    return ok, detail


# ---------- data: state.db usage ----------

def make_canon(skills_fs, aliases, categories):
    """name -> report key (the skill's DIRECTORY name), so one skill never splits into rows.

    Frontmatter names map to their directory ('ideation' -> 'creative-ideation'); a
    leading category is dropped ('productivity:task-tracker' / 'productivity/task-tracker'
    -> 'task-tracker') only when it IS a category of the skills tree, so a
    'plugin:skill' name is kept as-is instead of merging into a local 'skill'."""
    def canon(name):
        n = str(name).strip().strip("/:")  # ':' / '/' -> '' (dropped); 'task-tracker/' -> 'task-tracker'
        if not n or n in skills_fs:
            return n
        if n in aliases:
            return aliases[n]
        prefix, _, base = n.replace(":", "/").strip("/").rpartition("/")
        if prefix and base and any(c == prefix or c.endswith("/" + prefix) for c in categories):
            n = base
        return aliases.get(n, n)
    return canon


def _fetch_results(con, call_ids):
    """{tool_call_id: content} of the tool-result rows for these calls. Matched by id —
    older result rows have tool_name NULL, so a tool_name filter would miss them."""
    out = {}
    for i in range(0, len(call_ids), RESULT_CHUNK):
        chunk = call_ids[i:i + RESULT_CHUNK]
        out.update(con.execute(
            "SELECT tool_call_id, content FROM messages WHERE role='tool' "
            f"AND tool_call_id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    return out


_PLACEHOLDER = re.compile(r"^\[skill_view\] name=")          # compaction: '[skill_view] name=X (N chars)'
_TRUNCATED_OK = re.compile(r'^\s*\{\s*"success"\s*:\s*true\b')  # JSON cut off after a success flag


def _result_status(content):
    """('ok', canonical name or None) | ('failed', None) | ('dedup', None) for one result.

    No result row counts as ok. Non-JSON content counts as ok only when it is a
    compaction placeholder or JSON truncated after '"success": true' — anything else
    ('Skipped: another tool call…', a truncated '"success": false' error) is no load."""
    if content is None:
        return "ok", None
    content = str(content)
    try:
        d = json.loads(content)
    except Exception:
        if _PLACEHOLDER.match(content) or _TRUNCATED_OK.match(content):
            return "ok", None
        return "failed", None
    if not isinstance(d, dict) or d.get("success") is not True:
        return "failed", None
    if d.get("dedup"):  # repeat view of an unchanged, already-loaded skill
        return "dedup", None
    return "ok", d.get("name")


def collect_usage(canon):
    """(counts, last_used, total, first_ts, stats) from successful SKILL.md loads.

    A use = a skill_view call WITHOUT file_path whose result succeeded and is not a
    dedup stub, keyed by canon(result `name`, else the arg) — see make_canon.
    stats counts what was skipped: file_reads / failed / dedup / parse_errors."""
    stats = Counter()
    calls = []  # (timestamp, tool_call_id, raw name)
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            """SELECT timestamp, tool_calls FROM messages
               WHERE role='assistant' AND tool_calls LIKE '%"skill_view"%'"""
        ).fetchall()
        for ts, tcs in rows:
            try:
                tc_list = json.loads(tcs) if tcs else []
            except Exception:
                stats["parse_errors"] += 1
                continue
            for c in tc_list if isinstance(tc_list, list) else []:
                fn = c.get("function") if isinstance(c, dict) else None
                if not isinstance(fn, dict) or fn.get("name") != "skill_view":
                    continue
                raw_args = fn.get("arguments")
                try:
                    args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
                except Exception:
                    stats["parse_errors"] += 1
                    continue
                if not isinstance(args, dict) or not args.get("name"):
                    continue
                if args.get("file_path"):
                    stats["file_reads"] += 1
                    continue
                calls.append((ts, c.get("id"), args["name"]))
        results = _fetch_results(con, [cid for _, cid, _ in calls if cid])
    finally:
        con.close()

    counts, last_used, first_ts = Counter(), {}, None
    for ts, cid, raw in calls:
        status, canonical = _result_status(results.get(cid))
        if status != "ok":
            stats[status] += 1
            continue
        name = canon(canonical or raw)
        if not name:
            continue
        counts[name] += 1
        if name not in last_used or ts > last_used[name]:
            last_used[name] = ts
        if first_ts is None or ts < first_ts:
            first_ts = ts
    return counts, last_used, sum(counts.values()), first_ts, stats


# ---------- data: skills tree ----------

def _is_content_file(fname):
    if ".bak" in fname:
        return False
    return not fname.endswith(EXCLUDE_SUFFIXES)


def _content_mtimes(skill_dir):
    latest = 0.0
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not _is_content_file(f):
                continue
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return latest


def _frontmatter_fields(skill_md):
    """(name, version) from the SKILL.md frontmatter; '' for a missing field."""
    try:
        with open(skill_md, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return "", ""
    if not head.startswith("---"):
        return "", ""
    end = head.find("\n---", 3)
    block = head[:end] if end != -1 else head
    m = re.search(r"^name:[ \t]*(.*?)[ \t]*$", block, re.M)  # whole value: names may hold spaces
    name = re.sub(r"\s+#.*$", "", m.group(1)).strip("\"'").strip() if m else ""  # drop a YAML comment
    m = re.search(r'^version:\s*["\']?([^"\'\s]+)', block, re.M)
    return name, (m.group(1) if m else "")


def _changelog_note(changelog_path):
    try:
        with open(changelog_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    for ln in lines[:80]:
        m = re.match(r"^##\s+(.*)", ln)
        if m:
            note = re.sub(r"\s+", " ", m.group(1)).strip()
            return note[:44] + ("…" if len(note) > 44 else "")
    return ""


def scan_skills():
    """(skills, aliases, categories, installed) from the SKILL.md dirs.

    skills: {dir_name: {last_updated, version, note}}; aliases: {frontmatter name: dir_name}
    where they differ (skill_view results report the frontmatter name, e.g.
    creative/creative-ideation -> 'ideation'); categories: the dirs that hold skills
    ('productivity', 'mlops/evaluation'); installed: the SKILL.md dir count."""
    skills, aliases, categories, installed = {}, {}, set(), 0
    for root, dirs, files in os.walk(SKILLS_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        if "SKILL.md" not in files:
            continue
        installed += 1
        name = os.path.basename(root)
        rel_parent = os.path.dirname(os.path.relpath(root, SKILLS_ROOT))
        while rel_parent:
            categories.add(rel_parent)
            rel_parent = os.path.dirname(rel_parent)
        last_updated = _content_mtimes(root)
        fm_name, version = _frontmatter_fields(os.path.join(root, "SKILL.md"))
        note = ""
        cl = os.path.join(root, "CHANGELOG.md")
        if os.path.exists(cl):
            try:
                cl_mtime = os.path.getmtime(cl)
            except OSError:
                cl_mtime = 0
            # Only trust the changelog heading when the changelog itself was
            # part of the newest edit (a stale changelog must not supply the note).
            # Same update episode: changelog written within a day of the newest edit.
            if last_updated and cl_mtime >= last_updated - 86400:
                note = _changelog_note(cl)
        if not note:
            note = f"v{version}" if version else "-"
        skills[name] = {"last_updated": last_updated, "version": version, "note": note}
        if fm_name and fm_name != name:
            aliases[fm_name] = name
    for alias in [a for a in aliases if a in skills]:
        del aliases[alias]  # a real directory name always wins
    return skills, aliases, categories, installed


# ---------- report ----------

def _fmt_date(ts):
    if not ts:
        return "-"
    d = datetime.datetime.fromtimestamp(ts)
    return d.strftime("%m-%d") if d.year == datetime.datetime.now().year else d.strftime("%Y-%m-%d")


def _row(rank, name, n, last_used, skills_fs):
    fs = skills_fs.get(name)
    if fs and fs["last_updated"]:
        updated, note = _fmt_date(fs["last_updated"]), fs["note"]
    else:
        updated, note = "-", "(not on disk)"
    return [str(rank), name, str(n), _fmt_date(last_used[name]), updated, note]


def _days(n):
    return f"{n} day" if n == 1 else f"{n} days"


def build_rows(counts, last_used, skills_fs, top, recent_days, now=None):
    """(rows, n_recent): the top `top` skills by uses, then — below a divider row — every
    lower-ranked skill loaded in the last `recent_days` days (0 = off), in rank order,
    numbered with its true rank. A just-used low-count skill must still show up."""
    # Ties broken by most recent use, then name — deterministic at the top-N cut.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -last_used[kv[0]], kv[0]))
    rows = [_row(i, name, n, last_used, skills_fs)
            for i, (name, n) in enumerate(ranked[:top], 1)]
    recent = []
    if recent_days > 0:
        cutoff = (time.time() if now is None else now) - recent_days * 86400
        recent = [_row(i, name, n, last_used, skills_fs)
                  for i, (name, n) in enumerate(ranked, 1)
                  if i > top and last_used[name] >= cutoff]
    if recent:
        rows.append(["", f"— used in last {_days(recent_days)} —", "", "", "", ""])
        rows += recent
    return rows, len(recent)


def render_png(rows):
    if RENDERER_DIR not in sys.path:
        sys.path.insert(0, RENDERER_DIR)
    dist = "/usr/lib/python3/dist-packages"
    if dist not in sys.path:
        sys.path.insert(0, dist)
    import render_tables  # shared renderer (discord-table-format skill)
    headers = ["#", "Skill", "Uses", "Last used", "Last updated", "Last change"]
    return render_tables.render_table(headers, rows, out_prefix="skills_usage")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description='Self-delivering "most-used skills" report')
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help=f"Number of skills to list (default {DEFAULT_TOP})")
    ap.add_argument("--recent-days", type=int, default=DEFAULT_RECENT_DAYS,
                    help=f"Also list lower-ranked skills loaded in the last N days "
                         f"(default {DEFAULT_RECENT_DAYS}; 0 = off)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render the table only; print the PNG path; post nothing")
    ap.add_argument("--to", help="Override target: discord:<channel_id>[:<thread_id>] (testing)")
    ap.add_argument("--thread-id", help="Explicit Discord thread ID (bypasses stale session env)")
    args = ap.parse_args()

    skills_fs, aliases, categories, installed = scan_skills()
    counts, last_used, total, first_ts, stats = collect_usage(
        make_canon(skills_fs, aliases, categories))
    if stats["parse_errors"]:
        print(f"WARN: {stats['parse_errors']} unparseable tool_call rows skipped", file=sys.stderr)

    rows, n_recent = build_rows(counts, last_used, skills_fs, max(args.top, 1),
                                max(args.recent_days, 0))
    if not rows:
        print("No skill_view loads recorded yet — nothing to report.", file=sys.stderr)
        return 0

    if args.dry_run:
        png = render_png(rows)
        if not png:
            print("ERROR: renderer returned no image", file=sys.stderr)
            return 1
        print(png, flush=True)  # flush: os._exit() drops unflushed buffered stdout
        return 0

    # Resolve token + target BEFORE rendering so a config failure leaves no artifact.
    token = load_token()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not found in ~/.hermes/.env", file=sys.stderr)
        return 1

    channel = resolve_discord_target(args.to, args.thread_id)
    if not channel:
        raise TargetNotResolved(
            "could not resolve a Discord target — pass --thread-id <thread_id>, "
            "or run with HERMES_SESSION_KEY set")

    png = render_png(rows)
    if not png:
        print("ERROR: renderer returned no image", file=sys.stderr)
        return 1

    since = datetime.datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d") if first_ts else "?"
    caption = (f"**Most-used skills — loads & last update**\n"
               f"{total} loads across {len(counts)} skills since {since} "
               f"(of {installed} installed)")
    if n_recent:
        caption += f" · plus {n_recent} more used in the last {_days(args.recent_days)}"

    ok, detail = discord_post(token, channel, caption, img=png)
    if not ok:
        print(f"ERROR: Discord post failed ({detail}); PNG kept at {png}", file=sys.stderr)
        return 1
    try:
        os.remove(png)  # cleanup only after a confirmed 2xx
    except OSError:
        pass
    shown = len(rows) - (n_recent + 1 if n_recent else 0)
    print(f"Posted skill-usage report to {channel} ({total} loads, top {shown}"
          f" + {n_recent} recent; skipped {stats['file_reads']} file reads, "
          f"{stats['failed']} failed, {stats['dedup']} repeat views).",
          file=sys.stderr, flush=True)
    return 0


def _post_error(tb):
    """Best-effort crash notice to the home channel (noisy-failure rule)."""
    token = load_token()
    if not token:
        return
    try:
        discord_post(token, HOME_CHANNEL,
                     "⚠️ skill-usage-report failed:\n```\n" + tb[-1500:] + "\n```")
    except Exception:
        pass


if __name__ == "__main__":
    print("[SILENT]", flush=True)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        _lock_fd = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os._exit(0)  # another run holds the lock — exit silently
        _rc = main()
    except TargetNotResolved as e:
        print(f"ERROR: {e}", file=sys.stderr)
        _rc = 1
    except Exception:
        _tb = traceback.format_exc()
        print(_tb, file=sys.stderr)
        _post_error(_tb)
        _rc = 1
    os._exit(_rc)
