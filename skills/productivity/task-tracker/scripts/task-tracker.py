#!/usr/bin/env python3
"""
Task Tracker — standalone no-agent status/delivery script.

Reads task-tracker.json, computes overdue status, renders tables as images
(via render_tables.py), and posts them to Discord — each section's header and
its image in ONE message — by calling the Discord REST API directly with curl
multipart (content + file in a single request). This is the only reliable way
to pair text with an image on Discord; `hermes send` / `MEDIA:` tags get
re-batched by the gateway's extract_media() and lose ordering (see the
token-usage and sg-commute skills, which use this same pattern).

No LLM calls. Reads DISCORD_BOT_TOKEN from ~/.hermes/.env (the cron scheduler
does NOT pass it via os.environ to no_agent scripts). Run as cron or manually:
  python3 .../task-tracker.py
  python3 .../task-tracker.py --target discord:<channel_or_thread_id>
  python3 .../task-tracker.py --text-only   # text summary, no images
  python3 .../task-tracker.py --json        # sections JSON to stdout, no delivery

Write subcommands (the ONLY supported way to modify task-tracker.json — the
agent must never hand-edit the file):
  python3 .../task-tracker.py done  <items> [--date D]  # items: "2,5,car wash,all weekly"
  python3 .../task-tracker.py set   <item> <D|null>     # correct one task's last_done
  python3 .../task-tracker.py add   "<name>" <category> [--last-done D]
  python3 .../task-tracker.py remove <item> --yes
  python3 .../task-tracker.py rename <item> "<new name>"
  python3 .../task-tracker.py move  <items> <category>  # e.g. move 14,16 quarterly
  python3 .../task-tracker.py undo  [--dry-run]         # revert the most recent write
  (D = YYYY-MM-DD | today | yesterday; every write accepts --dry-run)

Every write is atomic: lock → read → backup (pre-change state) → modify →
validate → tmp + fsync → os.replace → prune backups to the newest 10. A row
NUMBER resolves against the last table this script DELIVERED to Discord (saved
in task-tracker.last-shown.json), i.e. the table the user actually saw — not a
fresh sort, which every write reshuffles. Names / ids resolve against the data.

Status/diagnostics go to stderr so stdout stays clean for cron (an auto-deliver
scheduler treats stdout as the message body). --json and the write
subcommands' result lines are the exceptions (stdout).
"""
import json, os, re, sys, time, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────
# TASK_TRACKER_DATA_DIR overrides the data location (lets tests run on a copy).
DATA_DIR         = Path(os.environ.get("TASK_TRACKER_DATA_DIR") or Path.home() / ".hermes" / "data")
DATA_FILE        = DATA_DIR / "task-tracker.json"
BACKUP_DIR       = DATA_DIR / "task-tracker backups"
SNAPSHOT_FILE    = DATA_DIR / "task-tracker.last-shown.json"  # {row: id} of the last delivered table
LOCK_FILE        = DATA_DIR / "task-tracker.lock"
UNDO_FILE        = DATA_DIR / "task-tracker.undo.json"  # undo stack + outcome of the last write command
BACKUP_KEEP      = 10      # regular (pre-write) backups
UNDO_BACKUP_KEEP = 10      # `_undo` backups (state an undo replaced), pruned separately from regular ones
# task-tracker.<YYYY-MM-DD_HHMMSS>[_<n>][_undo].json — `_undo` marks the backup an undo
# takes of the state it replaced (kept for recovery, never itself picked by undo)
BACKUP_RE        = re.compile(r"^task-tracker\.(\d{4}-\d{2}-\d{2}_\d{6})(?:_(\d+))?(_undo)?\.json$")
MAX_RETRY_AFTER  = 10      # seconds — a longer Discord 429 wait fails instead of stalling the run
RENDERER         = Path.home() / ".hermes" / "skills" / "productivity" / "discord-table-format" / "scripts" / "render_tables.py"
ENV_FILE         = Path.home() / ".hermes" / ".env"
DEFAULT_TARGET   = "discord:1510503169405485236"  # ham's task tracker thread
DISCORD_API      = "https://discord.com/api/v10/channels/{cid}/messages"
SEND_TIMEOUT     = 20      # seconds per curl request (curl --max-time)
RATE_DELAY       = 0.5     # seconds between sends — Discord allows 5 msgs / 5s / channel
RENDER_TIMEOUT   = 60
DEFAULT_INTERVAL = 30
CATEGORY_ORDER   = ["weekly", "fortnightly", "monthly", "quarterly"]
CATEGORY_LABELS  = {"weekly": "Weekly", "fortnightly": "Fortnightly",
                    "monthly": "Monthly", "quarterly": "Quarterly"}
CATEGORY_DEFAULT_INTERVALS = {"weekly": 7, "fortnightly": 14, "monthly": 30, "quarterly": 90}

# ── Data ────────────────────────────────────────────────────────────
def load_data():
    """Load task-tracker.json, failing cleanly (non-zero exit) on missing/corrupt data."""
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"ERROR: data file not found: {DATA_FILE}")
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"ERROR: could not read {DATA_FILE}: {e}")

def category_intervals(data):
    """{category: interval_days}. A missing / non-positive-int interval falls back
    to the category's standard interval (7/14/30/90, else 30) with a warning —
    one bad `categories` entry must not crash the report or the number resolver."""
    cats = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(cats, dict):
        cats = {}
    out = {}
    for k in set(cats) | set(CATEGORY_DEFAULT_INTERVALS):
        v = cats.get(k)
        n = v.get("interval_days") if isinstance(v, dict) else None
        default = CATEGORY_DEFAULT_INTERVALS.get(k, DEFAULT_INTERVAL)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            if k in cats:
                print(f"WARNING: category {k!r} has invalid interval_days {n!r}; "
                      f"using {default}", file=sys.stderr)
            n = default
        out[k] = n
    return out

def task_list(data):
    """The task dicts, ignoring a non-list `tasks` or non-dict entries (warned)."""
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        print("WARNING: `tasks` is missing or not a list", file=sys.stderr)
        return []
    good = [t for t in tasks if isinstance(t, dict)]
    if len(good) != len(tasks):
        print(f"WARNING: ignored {len(tasks) - len(good)} non-object task entries", file=sys.stderr)
    return good

def build_sections(data, today):
    """Build renderer sections sorted by last_done (oldest first) per category.
    Each section also carries `ids` (task id per row, parallel to `rows`) — the
    renderer ignores it; the snapshot and --json consumers use it."""
    intervals = category_intervals(data)

    # Group tasks by category. A single malformed task (unparseable last_done,
    # or a missing category/name) must NOT crash the whole report — this script
    # is also the number-resolver the agent runs for "done N", so one bad row
    # can't be allowed to take down every task. Bad rows stay visible (shown as
    # "Bad date"), keeping the running counter stable, and a warning goes to
    # stderr. Each tuple is (sort_date, task, kind) where kind ∈ dated/never/bad.
    groups = {}
    for t in task_list(data):
        cat = str(t.get("category") or "uncategorized")
        ld = t.get("last_done")
        if not ld:
            groups.setdefault(cat, []).append((date.max, t, "never"))  # null → sort last
            continue
        try:
            parsed = date.fromisoformat(ld)
        except (ValueError, TypeError):
            print(f"WARNING: task {t.get('id') or t.get('name') or '?'!r} has an "
                  f"unparseable last_done {ld!r}; shown as 'Bad date'", file=sys.stderr)
            groups.setdefault(cat, []).append((date.max, t, "bad"))  # sort last
            continue
        groups.setdefault(cat, []).append((parsed, t, "dated"))

    # Never silently drop tasks: emit known categories in fixed order, then any
    # unrecognized categories appended (sorted) with a warning so the running
    # counter stays stable and no task disappears from the report.
    unknown = sorted(c for c in groups if c not in CATEGORY_ORDER)
    if unknown:
        print(f"WARNING: tasks with unrecognized categories appended at end: {unknown}",
              file=sys.stderr)
    ordered_cats = [c for c in CATEGORY_ORDER if c in groups] + unknown

    sections = []
    running = 0
    for cat in ordered_cats:
        items = sorted(groups[cat], key=lambda x: x[0])
        interval = intervals.get(cat, DEFAULT_INTERVAL)
        rows, ids = [], []
        for ld, t, kind in items:
            running += 1
            if kind == "dated":
                # An absurd far-future last_done (near date.max, year 9999) parses
                # fine but overflows `ld + timedelta` — treat as a bad date rather
                # than let one row crash the whole report (and the --json resolver).
                try:
                    days_ago_val = max(0, (today - ld).days)  # clamp to 0 — last_done may be in the future if system clock lags
                    next_due = ld + timedelta(days=interval)
                    overdue_val = max(0, (today - next_due).days)  # clamp — prevent negative from clock lag
                    last_done_cell = ld.strftime("%b %-d")
                    days_ago = f"{days_ago_val}d"
                    next_due_str = next_due.strftime("%b %-d")
                    status = f"{overdue_val}d overdue" if overdue_val > 0 else "On track"
                except OverflowError:
                    print(f"WARNING: task {t.get('id') or t.get('name') or '?'!r} has an "
                          f"out-of-range last_done {t.get('last_done')!r}; shown as 'Bad date'",
                          file=sys.stderr)
                    kind = "bad"
            if kind == "bad":
                last_done_cell = str(t.get("last_done"))
                days_ago = "?"
                next_due_str = "?"
                status = "Bad date"
            elif kind == "never":
                last_done_cell = "Never"
                days_ago = "—"
                next_due_str = "—"
                status = "Never done"
            rows.append([
                str(running), t.get("name") or t.get("id") or "(unnamed)",
                last_done_cell, days_ago, next_due_str, status,
            ])
            ids.append(t.get("id"))
        if rows:
            sections.append({
                # title from the interval actually used, so it can't disagree with Next Due
                "title": f"{CATEGORY_LABELS.get(cat, cat)} (every {interval} days)",
                "headers": ["#", "Task", "Last Done", "Days Ago", "Next Due", "Status"],
                "rows": rows,
                "ids": ids,
            })
    return sections

def summary_text(sections, today):
    """Build a text summary of overdue items (used by --text-only)."""
    lines = []
    total_overdue = 0
    for sec in sections:
        for row in sec["rows"]:
            if "overdue" in row[5]:
                total_overdue += 1
                lines.append(f"  - {row[1]}: {row[5]}")
    header = f"## 📋 Task Tracker (as of {today.strftime('%b %-d, %Y')})"
    count = f"**{total_overdue} tasks overdue**" if total_overdue else "✅ All on track"
    body = "\n".join(lines) if lines else ""
    return f"{header}\n\n{count}\n{body}" if body else f"{header}\n\n{count}"

# ── Render ──────────────────────────────────────────────────────────
def render_tables(sections):
    """Call render_tables.py once for all sections, return {title: path} dict ({} on any failure)."""
    payload = json.dumps(sections)
    try:
        result = subprocess.run(
            [sys.executable, str(RENDERER)],
            input=payload, capture_output=True, text=True, timeout=RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: render_tables.py timed out after {RENDER_TIMEOUT}s", file=sys.stderr)
        return {}
    except OSError as e:
        print(f"ERROR: could not run render_tables.py: {e}", file=sys.stderr)
        return {}
    if result.returncode != 0:
        print(f"ERROR: render_tables.py failed:\n{result.stderr}", file=sys.stderr)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: render_tables.py returned non-JSON: {result.stdout[:200]!r}", file=sys.stderr)
        return {}

# ── Discord delivery (direct REST multipart — no hermes dependency) ──
def load_token():
    """Read DISCORD_BOT_TOKEN from ~/.hermes/.env (scheduler does not export it)."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None

def channel_id(target):
    """Extract the numeric channel/thread id from 'discord:<id>' or 'discord:<chan>:<thread>'.
    A thread is itself a channel for the messages endpoint, so use the last segment."""
    t = target.split("discord:", 1)[-1]
    return t.split(":")[-1].strip()

class FatalSend(Exception):
    """A send error that will fail identically for every later message (bad
    token / no access / not a channel) — abort the run instead of repeating it."""

def discord_post(token, cid, content, img_path=None):
    """POST one Discord message: `content` (caption) + optional `file` (image) in
    a SINGLE multipart request — the only reliable header+image pairing. Returns
    True on 2xx.

    The message body is sent as a JSON `payload_json` field built with
    `json.dumps` and passed via `--form-string` (NOT `-F content=`). curl's `-F`
    treats a `;` in the value as a form-part parameter separator — silently
    truncating any caption containing `;` — and a leading `@`/`<` as a file-read
    directive; `--form-string` + `json.dumps` sends the content verbatim
    (newlines, `;`, `@`, `<` all literal). `allowed_mentions:{"parse":[]}` rides
    in the same request so a task name containing `@everyone`/`<@id>` can never
    ping the channel. The file part stays `-F file=@…` (the `@` is intended
    there). Proven pattern — see references/discord-delivery.md."""
    url = DISCORD_API.format(cid=cid)
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    cmd = [
        "curl", "-sS", "--max-time", str(SEND_TIMEOUT), "-w", "\n%{http_code}",
        "-X", "POST", url,
        "-H", f"Authorization: Bot {token}",
        "--form-string", f"payload_json={payload}",
    ]
    if img_path and os.path.exists(img_path):
        cmd += ["-F", f"file=@{img_path}"]

    for attempt in range(2):  # one retry, for 429 rate-limit
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT + 5)
        except subprocess.TimeoutExpired:
            print("ERROR: Discord POST timed out", file=sys.stderr)
            return False
        except OSError as e:
            print(f"ERROR: could not run curl: {e}", file=sys.stderr)
            return False
        if r.returncode != 0:
            print(f"ERROR: curl failed (rc={r.returncode}): {r.stderr.strip()}", file=sys.stderr)
            return False
        body, _, code = r.stdout.rpartition("\n")  # last line is the HTTP status code
        code = code.strip()
        if code == "429" and attempt == 0:  # rate limited — honor a short retry_after, try once more
            try:
                retry = max(0.0, float(json.loads(body).get("retry_after", 1)))
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                retry = 1.0
            if retry > MAX_RETRY_AFTER:
                print(f"ERROR: rate-limited by Discord for {retry}s (> {MAX_RETRY_AFTER}s); "
                      f"giving up", file=sys.stderr)
                return False
            print(f"WARNING: rate-limited by Discord; retrying after {retry}s", file=sys.stderr)
            time.sleep(retry + 0.25)
            continue
        if code.startswith("2"):
            return True
        print(f"ERROR: Discord HTTP {code or '???'}: {body[:200]}", file=sys.stderr)
        if code in ("401", "403", "404"):
            hint = (" — the target is not a channel/thread id (a Discord MESSAGE id was probably "
                    "passed as --target)" if code == "404" else "")
            raise FatalSend(f"Discord HTTP {code} for channel {cid}{hint}")
        return False
    return False

CARD_COLORS = [0x5865F2, 0x57F287, 0xFEE75C, 0xEB459E, 0xED4245, 0x3BA55C]

def discord_post_cards(token, cid, content, cards):
    """POST ONE message: `content` + one embed per (title, img_path) card, the images
    uploaded as files[N] and shown via attachment://cardN.png, so every category
    header stays paired with its table inside a single post (user decision
    2026-10-29). Returns "ok", "rejected" (Discord answered non-2xx: nothing was
    posted, safe to fall back) or "unknown" (timeout/transport error: it may have
    landed, so don't re-send). Raises FatalSend on 401/403/404 like discord_post."""
    url = DISCORD_API.format(cid=cid)
    embeds, files = [], []
    for title, img in cards:
        e = {"title": title[:256], "color": CARD_COLORS[len(embeds) % len(CARD_COLORS)]}
        if img and os.path.exists(img):
            e["image"] = {"url": f"attachment://card{len(files)}.png"}
            files.append(img)
        embeds.append(e)
    payload = json.dumps({"content": content, "embeds": embeds, "allowed_mentions": {"parse": []},
                          "attachments": [{"id": i, "filename": f"card{i}.png"} for i in range(len(files))]})
    cmd = ["curl", "-sS", "--max-time", str(SEND_TIMEOUT * 2), "-w", "\n%{http_code}",
           "-X", "POST", url, "-H", f"Authorization: Bot {token}",
           "--form-string", f"payload_json={payload}"]
    for i, img in enumerate(files):
        cmd += ["-F", f"files[{i}]=@{img};filename=card{i}.png;type=image/png"]
    for attempt in range(2):  # one retry, for 429 rate-limit
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT * 2 + 5)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"ERROR: Discord POST failed ({e.__class__.__name__})", file=sys.stderr)
            return "unknown"
        if r.returncode != 0:
            print(f"ERROR: curl failed (rc={r.returncode}): {r.stderr.strip()}", file=sys.stderr)
            return "unknown"
        body, _, code = r.stdout.rpartition("\n")
        code = code.strip()
        if code == "429" and attempt == 0:
            try:
                retry = max(0.0, float(json.loads(body).get("retry_after", 1)))
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                retry = 1.0
            if retry > MAX_RETRY_AFTER:
                return "rejected"
            time.sleep(retry + 0.25)
            continue
        if code.startswith("2"):
            return "ok"
        print(f"ERROR: Discord HTTP {code or '???'}: {body[:200]}", file=sys.stderr)
        if code in ("401", "403", "404"):
            raise FatalSend(f"Discord HTTP {code} for channel {cid}")
        return "rejected"
    return "rejected"

def cleanup(paths):
    for p in paths:
        if not p:
            continue
        try:
            os.remove(p)
        except OSError:
            pass

# ── Writes (the only supported way to modify task-tracker.json) ──────
class WriteError(Exception):
    pass

QUOTES = "\"'`‘’“”"

def slugify(name):
    """Task id from a name: lowercase, '&' → 'and', apostrophes dropped, any other
    run of non-alphanumerics → '-'. "Lock'n'lock water jug wash" →
    locknlock-water-jug-wash; "Toilet bowl & sink wash" → toilet-bowl-and-sink-wash."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")

def clean_name(name):
    """User-supplied name → stored form: surrounding quotes stripped, whitespace collapsed."""
    return " ".join(name.strip().strip(QUOTES).split())

def norm(text):
    """Comparison key: clean_name + casefold, trailing . ! ? dropped."""
    return clean_name(str(text)).rstrip(".!?").casefold()

def parse_date(value, today):
    """'today' / 'yesterday' / zero-padded ISO YYYY-MM-DD → ISO string. Rejects
    anything else, and dates more than a day in the future (clock-lag slack)."""
    v = value.strip().lower()
    if v == "today":
        d = today
    elif v == "yesterday":
        d = today - timedelta(days=1)
    else:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v, re.ASCII):
            raise WriteError(f"date {value!r} is not YYYY-MM-DD / today / yesterday")
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise WriteError(f"date {value!r} is not a real date")
    if d > today + timedelta(days=1):
        raise WriteError(f"date {d.isoformat()} is in the future")
    return d.isoformat()

def is_iso(v):
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v, re.ASCII)):
        return False
    try:
        date.fromisoformat(v)
        return True
    except ValueError:
        return False

def validate(data, before):
    """Schema check run on every write before anything touches disk. Tasks the
    write created or changed must be fully valid; untouched legacy rows are not
    re-judged (one odd old field must not block every write). Ids must be unique."""
    if not isinstance(data.get("tasks"), list):
        raise WriteError("data has no `tasks` list")
    old = {t.get("id"): t for t in before.get("tasks", []) if isinstance(t, dict)} \
        if isinstance(before.get("tasks"), list) else {}
    seen = set()
    for t in data["tasks"]:
        if not isinstance(t, dict):
            raise WriteError(f"non-object task entry {t!r}")
        tid = t.get("id")
        if tid in seen:
            raise WriteError(f"duplicate task id {tid!r}")
        seen.add(tid)
        if old.get(tid) == t:
            continue  # untouched by this write
        missing = [k for k in ("id", "name", "category", "last_done", "added") if k not in t]
        if missing:
            raise WriteError(f"task {tid or t.get('name')!r} is missing {missing}")
        if t["last_done"] is not None and not is_iso(t["last_done"]):
            raise WriteError(f"task {tid!r} has non-ISO last_done {t['last_done']!r}")
        if tid not in old and not is_iso(t["added"]):
            raise WriteError(f"new task {tid!r} has non-ISO added {t['added']!r}")

def list_backups(undo=False):
    """Regular (or, with undo=True, `_undo`-tagged) backup paths, oldest → newest
    by the timestamp in the FILENAME (not mtime — copies can carry old mtimes)."""
    try:
        names = os.listdir(BACKUP_DIR)
    except FileNotFoundError:
        return []
    keyed = []
    for n in names:
        m = BACKUP_RE.match(n)
        if m and bool(m.group(3)) == undo:
            keyed.append(((m.group(1), int(m.group(2) or 0)), BACKUP_DIR / n))
    return [p for _, p in sorted(keyed)]

def next_backup_name(tag=""):
    """A backup path that sorts AFTER every existing backup of its kind — never
    reuses a name (even one pruned a moment ago, within the same second)."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    existing = list_backups(undo=bool(tag))
    if existing:  # clock stepped back / future-dated copies: never sort before them
        stamp = max(stamp, BACKUP_RE.match(existing[-1].name).group(1))
    n = 0
    for p in existing:
        m = BACKUP_RE.match(p.name)
        if m.group(1) == stamp:
            n = max(n, int(m.group(2) or 0) + 1)
    name = f"task-tracker.{stamp}{'_' + str(n) if n else ''}{tag}.json"
    while (BACKUP_DIR / name).exists():  # belt and braces
        n += 1
        name = f"task-tracker.{stamp}_{n}{tag}.json"
    return BACKUP_DIR / name

def write_synced(path, data):
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

def sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()

def load_undo():
    """{"stack": [{backup, pre, post, cmd, at}, ...], "last": {cmd, wrote, at}}."""
    try:
        u = json.loads(UNDO_FILE.read_text(encoding="utf-8"))
        if isinstance(u, dict) and isinstance(u.get("stack"), list):
            return u
    except (OSError, ValueError):
        pass
    return {"stack": [], "last": None}

def save_undo(u):
    tmp = UNDO_FILE.with_name(UNDO_FILE.name + ".tmp")
    try:
        write_synced(tmp, (json.dumps(u, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        os.replace(tmp, UNDO_FILE)
    except OSError as e:
        print(f"WARNING: could not update {UNDO_FILE.name}: {e}", file=sys.stderr)

def record_outcome(cmd, wrote):
    """Remember whether the most recent write COMMAND changed anything, so
    "undo that" after a no-op / failed command can't silently revert an older write."""
    import fcntl
    try:
        with open(LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            u = load_undo()
            u["last"] = {"cmd": cmd, "wrote": wrote, "at": datetime.now().isoformat(timespec="seconds")}
            save_undo(u)
    except OSError:
        pass

def atomic_write(mutate, dry_run=False, tag="", cmd=""):
    """lock → read → mutate(data, raw, undo) → [unchanged? stop] → validate →
    backup → tmp+fsync → os.replace → undo-stack update → prune, all under one
    lock. `mutate` edits the dict in place and returns result lines; for undo it
    sets out["restore_bytes"] to write those exact bytes instead of re-serializing. The data file is only ever replaced whole;
    if the replace fails, the backup made for it is removed again."""
    import fcntl
    if not DATA_FILE.exists():
        raise WriteError(f"data file not found: {DATA_FILE}")
    with open(LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            raw = DATA_FILE.read_bytes()
            data = json.loads(raw)
            before = json.loads(raw)
        except (OSError, ValueError) as e:
            raise WriteError(f"could not read {DATA_FILE}: {e}")
        if not isinstance(data, dict):
            raise WriteError(f"{DATA_FILE} is not a JSON object")
        undo = load_undo()
        out = {}
        lines = mutate(data, raw, undo, out)
        new = out.get("restore_bytes") or \
            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if json.loads(new) == before:  # nothing changed → no backup, no write
            if "pop" in out and not dry_run:
                # undo whose target state is already in place (crash mid-undo, or a
                # hand revert): consume the entry so the stack can't wedge on it
                if out.get("pop_stack"):
                    undo["stack"].pop()
                undo["last"] = {"cmd": cmd, "wrote": True, "at": datetime.now().isoformat(timespec="seconds")}
                save_undo(undo)
                return lines + ["(file was already in that state — undo entry consumed, nothing written)"
                                if out.get("pop_stack") else "(no change — nothing written)"], True
            if "pop" in out and out.get("pop_stack"):
                return lines + ["(dry run — the file is already in that state; the undo entry would be consumed)"], False
            return lines + ["(no change — nothing written)"], False
        if not out.get("skip_validate"):
            validate(json.loads(new), before)
        if dry_run:
            return ["(dry run — nothing written)"] + lines, False
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = next_backup_name(tag)
        tmp = DATA_FILE.with_name(DATA_FILE.name + f".tmp{os.getpid()}")
        try:
            write_synced(backup, raw)  # exact pre-change bytes
            write_synced(tmp, new)
            os.replace(tmp, DATA_FILE)
        except OSError as e:
            for p in (tmp, backup):
                try:
                    p.unlink()
                except OSError:
                    pass
            raise WriteError(f"write failed: {e}")
        if "pop" in out:  # undo: drop the entry it reverted (its backup file stays, for recovery)
            if out.get("pop_stack"):
                undo["stack"].pop()
        else:
            undo["stack"].append({"backup": backup.name, "pre": sha(raw), "post": sha(new),
                                  "cmd": cmd, "at": datetime.now().isoformat(timespec="seconds")})
        undo["last"] = {"cmd": cmd, "wrote": True, "at": datetime.now().isoformat(timespec="seconds")}
        save_undo(undo)  # before pruning, so a prune error can't lose the undo entry
        for old in list_backups()[:-BACKUP_KEEP] + list_backups(undo=True)[:-UNDO_BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError as e:
                print(f"WARNING: could not prune {old.name}: {e}", file=sys.stderr)
        kept = [e for e in undo["stack"] if (BACKUP_DIR / str(e.get("backup"))).exists()]
        if kept != undo["stack"]:
            undo["stack"] = kept
            save_undo(undo)
        return lines + [f"backup: {backup.name}"], True

def load_snapshots():
    """{target: {shown_at, rows: {"<row#>": {"id", "name"}}}} — one table per thread."""
    try:
        snaps = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    good = {}
    for target, snap in (snaps.items() if isinstance(snaps, dict) else []):
        rows = snap.get("rows") if isinstance(snap, dict) else None
        if isinstance(rows, dict) and all(isinstance(k, str) and isinstance(v, dict)
                                          and isinstance(v.get("id"), str) for k, v in rows.items()):
            good[target] = snap
    if isinstance(snaps, dict) and len(good) != len(snaps):
        print(f"WARNING: ignored malformed entries in {SNAPSHOT_FILE.name}", file=sys.stderr)
    return good

def snapshot_target(snap):
    return next((k for k, v in load_snapshots().items() if v == snap), "?")

def load_snapshot(target):
    """The table last delivered to `target` (a discord:<id> string) — or, when
    target is None, the most recently delivered table anywhere."""
    snaps = load_snapshots()
    if target:
        return snaps.get(target)
    return max(snaps.values(), key=lambda s: str(s.get("shown_at")), default=None)

def save_snapshot(sections, target):
    """Record {row: {id, name}} for the sections actually delivered to `target`
    — what the user now sees there. Rows of sections NOT delivered keep their
    previous mapping (the user still sees the older message for those)."""
    snaps = load_snapshots()
    rows = dict(snaps.get(target, {}).get("rows", {}))
    for sec in sections:
        for row, tid in zip(sec["rows"], sec["ids"]):
            if isinstance(tid, str):
                rows[row[0]] = {"id": tid, "name": row[1]}
    snaps[target] = {"shown_at": datetime.now().isoformat(timespec="seconds"), "rows": rows}
    tmp = SNAPSHOT_FILE.with_name(SNAPSHOT_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(snaps, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, SNAPSHOT_FILE)
    except OSError as e:
        print(f"WARNING: could not save row snapshot: {e}", file=sys.stderr)

def target_arg(args):
    """--target in the same normalized form deliver() stores: discord:<channel id>."""
    t = getattr(args, "target", None)
    return f"discord:{channel_id(t)}" if t else None

def split_items(parts):
    """Args are re-joined with spaces, then split on commas only — so an unquoted
    `done kitchen mitts wash` is ONE item and `2 and 5` is one (unmatched) item,
    never three word-by-word matches."""
    return [i.strip() for i in " ".join(parts).split(",") if i.strip()]

def resolve(item, tasks, snapshot, notes, fuzzy=True):
    """One item → task dict, or raise WriteError.
    - a number → the last DELIVERED table (snapshot), i.e. what the user saw;
    - an exact id, or an exact name (case/space/quote/trailing-punctuation-insensitive);
    - if fuzzy: a unique task whose name contains the item's words as a
      contiguous whole-word run (slug tokens, so '&'/'and' and apostrophes match),
      item ≥ 3 chars. Anything shorter, absent, or ambiguous is an error."""
    by_id = {t.get("id"): t for t in tasks}
    item = item.strip().strip(QUOTES).strip()
    if item.lstrip("#").rstrip(".!?").isdigit():
        item = item.lstrip("#").rstrip(".!?")
        if snapshot is None:
            raise WriteError(f"#{item}: no table has been shown in this thread yet — show the tracker first, or use the task name")
        row = snapshot["rows"].get(item)
        if row is None:
            raise WriteError(f"#{item}: no such row in the table shown at {snapshot.get('shown_at')}")
        t = by_id.get(row["id"])
        if t is None or norm(t.get("name", "")) != norm(row.get("name", "")):
            raise WriteError(f"#{item}: that row's task ({row.get('name')}) was renamed or removed "
                             f"since the table was shown — use its name")
        return t
    if item in by_id:
        return by_id[item]
    key = norm(item)
    exact = [t for t in tasks if norm(t.get("name", "")) == key or t.get("id") == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise WriteError(f"'{item}' is ambiguous: " + ", ".join(t.get("name", "?") for t in exact))
    if not fuzzy:
        raise WriteError(f"'{item}': no task with exactly that name or id")
    words = slugify(item).split("-")
    if len(key) < 3 or not words[0] or all(w in STOP_WORDS for w in words):
        raise WriteError(f"'{item}': too short to match — use the full task name")
    def has_run(t):
        tw = slugify(str(t.get("name", ""))).split("-")
        return any(tw[i:i + len(words)] == words for i in range(len(tw) - len(words) + 1))
    hits = [t for t in tasks if has_run(t)]
    if len(hits) == 1:
        notes.append(f"'{item}' → {hits[0].get('name')}")
        return hits[0]
    if not hits:
        raise WriteError(f"'{item}': no task matches")
    raise WriteError(f"'{item}' is ambiguous: " + ", ".join(t.get("name", "?") for t in hits))

STOP_WORDS = {"and", "the", "of", "a", "an", "to", "in", "on", "for", "with", "n"}

def check_new_name(name):
    if not slugify(name):
        raise WriteError("task name is empty")
    if name.isdigit() or re.fullmatch(r"all(\s+\w+)?", name, re.I):
        raise WriteError(f"'{name}' can't be a task name (it would read as a row number / 'all')")
    if "," in name:
        raise WriteError(f"'{name}': task names can't contain commas (commas separate items)")

def cmd_done(args, today):
    when = parse_date(args.date, today)
    items = split_items(args.items)
    snapshot = load_snapshot(target_arg(args))
    problems = []
    def mutate(data, raw, undo, out):
        tasks = task_list(data)
        intervals = category_intervals(data)
        cats = set(CATEGORY_ORDER) | set(intervals)
        picked, notes = [], []
        for item in items:
            key = norm(item)
            m = re.fullmatch(r"all(?:\s+(\w+))?", key)
            exact = any(norm(t.get("name", "")) == key for t in tasks)
            try:
                if m and not exact and (m.group(1) is None or m.group(1) in cats):
                    if m.group(1) is None and not args.yes:
                        raise WriteError("bare 'all' marks EVERY task — confirm with the user, then rerun with --yes")
                    found = [t for t in tasks if m.group(1) is None or t.get("category") == m.group(1)]
                    if not found:
                        raise WriteError(f"'{item}': no tasks in that category")
                else:
                    found = [resolve(item, tasks, snapshot, notes)]
            except WriteError as e:
                problems.append(str(e))
                continue
            for t in found:
                if not any(t is p for p in picked):
                    picked.append(t)
        lines = []
        for t in picked:
            old = t.get("last_done")
            label = f"{t.get('name')} ({t.get('id')})"
            if old == when:
                lines.append(f"= {label} was already done on {when}")
                continue
            if is_iso(old) and old > when:
                problems.append(f"{label} was already done more recently ({old}); not moved back "
                                f"to {when} — use `set` if that is really intended")
                continue
            t["last_done"] = when
            nxt = date.fromisoformat(when) + timedelta(days=intervals.get(t.get("category"), DEFAULT_INTERVAL))
            lines.append(f"✓ {label}: {old or 'never'} → {when} (next due {nxt.strftime('%b %-d')})")
        if not lines:
            raise WriteError("nothing to mark — " + "; ".join(problems))
        if snapshot and any(i.strip().strip(QUOTES).lstrip("#").rstrip(".!?").isdigit() for i in items):
            notes.append(f"row numbers resolved against the table shown in {snapshot_target(snapshot)} "
                         f"at {snapshot.get('shown_at')}")
        return notes + lines
    return atomic_write(mutate, args.dry_run, cmd=f"done {' '.join(args.items)}"), problems

def cmd_set(args, today):
    when = None if args.date.strip().lower() in ("null", "never", "none") else parse_date(args.date, today)
    snapshot = load_snapshot(target_arg(args))
    def mutate(data, raw, undo, out):
        notes = []
        t = resolve(args.item, task_list(data), snapshot, notes)
        old, t["last_done"] = t.get("last_done"), when
        return notes + [f"✎ {t.get('name')} ({t.get('id')}): last done {old or 'never'} → {when or 'never'}"]
    return atomic_write(mutate, args.dry_run, cmd=f"set {args.item} {args.date}"), []

def cmd_move(args, today):
    cat = args.category.strip().lower()
    items = split_items(args.items)
    snapshot = load_snapshot(target_arg(args))
    problems = []
    def mutate(data, raw, undo, out):
        valid = set(category_intervals(data)) | set(CATEGORY_ORDER)
        if cat not in valid:
            raise WriteError(f"invalid category {args.category!r} — use one of {', '.join(CATEGORY_ORDER)}")
        tasks = task_list(data)
        notes, picked = [], []
        for item in items:
            try:
                t = resolve(item, tasks, snapshot, notes)
            except WriteError as e:
                problems.append(str(e))
                continue
            if not any(t is p for p in picked):
                picked.append(t)
        lines = []
        for t in picked:
            old = t.get("category")
            label = f"{t.get('name')} ({t.get('id')})"
            if old == cat:
                lines.append(f"= {label} is already {cat}")
                continue
            t["category"] = cat
            lines.append(f"↔ {label}: {old} → {cat}")
        if not lines:
            raise WriteError("nothing to move — " + "; ".join(problems))
        if snapshot and any(i.strip().strip(QUOTES).lstrip("#").rstrip(".!?").isdigit() for i in items):
            notes.append(f"row numbers resolved against the table shown in {snapshot_target(snapshot)} "
                         f"at {snapshot.get('shown_at')}")
        return notes + lines
    return atomic_write(mutate, args.dry_run, cmd=f"move {' '.join(args.items)} {cat}"), problems

def cmd_add(args, today):
    cat = args.category.strip().lower()
    last = None if args.last_done is None else parse_date(args.last_done, today)
    name = clean_name(args.name)
    def mutate(data, raw, undo, out):
        check_new_name(name)
        valid = set(category_intervals(data)) | set(CATEGORY_ORDER)
        if cat not in valid:
            raise WriteError(f"invalid category {args.category!r} — use one of {', '.join(CATEGORY_ORDER)}")
        tid = slugify(name)
        tasks = data.setdefault("tasks", [])
        for t in task_list(data):
            if t.get("id") == tid or norm(t.get("name", "")) == norm(name):
                raise WriteError(f"a task '{t.get('name')}' ({t.get('id')}) already exists")
        tasks.append({"id": tid, "name": name, "category": cat,
                      "last_done": last, "added": today.isoformat()})
        return [f"+ {name} ({tid}) added to {cat}, last done {last or 'never'}"]
    return atomic_write(mutate, args.dry_run, cmd=f"add {name}"), []

def cmd_remove(args, today):
    snapshot = load_snapshot(target_arg(args))
    def mutate(data, raw, undo, out):
        t = resolve(args.item, task_list(data), snapshot, [], fuzzy=False)
        if not args.yes:
            raise WriteError(f"refusing to remove '{t.get('name')}' ({t.get('category')}) without --yes "
                             f"— confirm with the user first")
        data["tasks"].remove(t)
        return [f"🗑 removed {t.get('name')} ({t.get('id')}) from {t.get('category')}"]
    return atomic_write(mutate, args.dry_run, cmd=f"remove {args.item}"), []

def cmd_rename(args, today):
    snapshot = load_snapshot(target_arg(args))
    name = clean_name(args.new_name)
    def mutate(data, raw, undo, out):
        check_new_name(name)
        t = resolve(args.item, task_list(data), snapshot, [], fuzzy=False)
        tid = slugify(name)
        for o in task_list(data):
            if o is not t and (o.get("id") == tid or norm(o.get("name", "")) == norm(name)):
                raise WriteError(f"'{name}' collides with existing task '{o.get('name')}' ({o.get('id')})")
        old_name, old_id = t.get("name"), t.get("id")
        t["name"], t["id"] = name, tid
        return [f"✎ renamed {old_name} ({old_id}) → {name} ({tid}) in {t.get('category')}"]
    return atomic_write(mutate, args.dry_run, cmd=f"rename {args.item} → {name}"), []

def undo_diff(cur_data, restored):
    cur = {t.get("id"): t for t in task_list(cur_data)}
    res = {t.get("id"): t for t in restored.get("tasks", []) if isinstance(t, dict)}
    gone = [tid for tid in cur if tid not in res]
    back = [tid for tid in res if tid not in cur]
    lines = []
    # a rename shows up as one id gone + one id back with the same other fields
    for g in list(gone):
        for bk in list(back):
            c, r = cur[g], res[bk]
            if {k: v for k, v in c.items() if k not in ("id", "name")} == \
               {k: v for k, v in r.items() if k not in ("id", "name")}:
                lines.append(f"  ↺ renamed back: {c.get('name')} → {r.get('name')}")
                gone.remove(g); back.remove(bk)
                break
    for tid in sorted(set(cur) & set(res), key=str):
        c, r = cur[tid], res[tid]
        if c == r:
            continue
        if {k for k in set(c) | set(r) if c.get(k) != r.get(k)} == {"last_done"}:
            lines.append(f"  ↺ {r.get('name')} ({tid}): {c.get('last_done') or 'never'} → {r.get('last_done') or 'never'}")
        else:
            lines.append(f"  ↺ {tid}: {c} → {r}")
    lines += [f"  - {cur[g].get('name')} ({g}) removed (not in the restored state)" for g in gone]
    lines += [f"  + {res[bk].get('name')} ({bk}) back (in the restored state)" for bk in back]
    return lines

def cmd_undo(args, today):
    """Revert the most recent scripted write by popping the undo stack: restore
    the exact bytes of that write's backup, so the next undo steps one write
    further back. There is no redo — re-run the undone command instead. Refuses (unless overridden) when:
      - the last write COMMAND changed nothing (no-op / failed) — "undo that"
        would otherwise revert an older write the user didn't mean (--previous);
      - the file no longer matches what that write left (hand-edited since —
        undo would silently revert those edits too) (--force);
      - there is no scripted write left to undo (never falls back to "newest
        backup" — that could revert or re-apply the wrong writes).
    All of it runs under the write lock. The restore's own backup is tagged
    `_undo` (the replaced state, kept for manual recovery, never picked by
    undo); the restored backup itself is kept too."""
    def mutate(data, raw, undo, out):
        stack, last = undo["stack"], undo.get("last")
        top = stack[-1] if stack else None
        if last and last.get("wrote") is False and not (args.previous or args.force):
            what = f"{top.get('cmd')} at {top.get('at')}" if top else "none"
            raise WriteError(f"the last command ({last.get('cmd')} at {last.get('at')}) changed nothing, "
                             f"so there's nothing of it to undo. Most recent real write: {what} — "
                             f"rerun with --previous only if THAT is what the user wants undone")
        if top:
            src = BACKUP_DIR / str(top.get("backup"))
            if not src.exists():
                if not args.dry_run:  # older entries can't be reached without also reverting this one
                    undo["stack"] = []
                    save_undo(undo)
                raise WriteError(f"the backup for the last write ({top.get('cmd')} at {top.get('at')}) is "
                                 f"gone, so it can't be undone — its changes remain in the file"
                                 f"{'' if args.dry_run else '; the undo history was cleared'}. Correct "
                                 f"individual tasks with `set` instead (do NOT use undo --force)")
            already = sha(raw) == top.get("pre")  # e.g. a crash after the restore, before the stack pop
            if sha(raw) != top.get("post") and not already and not args.force:
                raise WriteError(f"the data file was changed outside this script after the last write "
                                 f"({top.get('cmd')} at {top.get('at')}) — undo would revert that too. "
                                 f"Review with `undo --dry-run --force`; use --force only if intended")
        else:
            # deliberately no fallback to "newest backup": with no stack entry, any
            # backup restore could revert or re-apply the wrong writes
            raise WriteError("nothing left to undo with the script (it can only undo its own writes, "
                             "newest first, up to 10). Correct individual tasks with `set`; restoring an "
                             "older backup is a by-hand recovery (references/data-and-writes.md)")
        try:
            restored_bytes = src.read_bytes()
            restored = json.loads(restored_bytes)
        except (OSError, ValueError) as e:
            raise WriteError(f"backup {src} is unreadable ({e}) — move it into "
                             f"'{BACKUP_DIR / '_archive'}' and retry")
        if not (isinstance(restored, dict) and isinstance(restored.get("tasks"), list)):
            raise WriteError(f"backup {src} is not a task-tracker file — move it into "
                             f"'{BACKUP_DIR / '_archive'}' and retry")
        if sha(restored_bytes) != top.get("pre") and not args.force:
            raise WriteError(f"backup {src.name} was modified after it was taken — refusing (--force to override)")
        out.update(restore_bytes=restored_bytes, pop=src, pop_stack=True, skip_validate=True)
        what = f" (undoing: {top.get('cmd')} at {top.get('at')} — to redo, re-run that command)"
        return [f"restoring {src.name}{what}:"] + (undo_diff(data, restored) or ["  (no task differences)"])
    return atomic_write(mutate, args.dry_run, tag="_undo", cmd="undo"), []

def write_main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="task-tracker.py", description="Task Tracker writes")
    sub = p.add_subparsers(dest="cmd", required=True)
    def add(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
        return sp
    def add(name, _add=add, **kw):
        sp = _add(name, **kw)
        if name in ("done", "set", "move", "remove", "rename"):
            sp.add_argument("--target", default=None,
                            help="discord:<thread id> the user is looking at — row numbers resolve against the table shown there")
        return sp
    sp = add("done", help="mark tasks done")
    sp.add_argument("items", nargs="+", help='comma-separated: row numbers (as last shown), names, ids, "all <category>"')
    sp.add_argument("--date", default="today", help="YYYY-MM-DD | today | yesterday (default today)")
    sp.add_argument("--yes", action="store_true", help="required for bare 'all' (every task)")
    sp = add("set", help="set one task's last_done (corrections)")
    sp.add_argument("item"); sp.add_argument("date", help="YYYY-MM-DD | today | yesterday | null")
    sp = add("move", help="move tasks to another category (row numbers/names/ids)")
    sp.add_argument("items", nargs="+", help='comma-separated: row numbers (as last shown), names, ids')
    sp.add_argument("category", help="weekly | fortnightly | monthly | quarterly")
    sp = add("add", help="add a task")
    sp.add_argument("name"); sp.add_argument("category")
    sp.add_argument("--last-done", default=None, help="YYYY-MM-DD | today | yesterday (default: never)")
    sp = add("remove", help="remove a task (exact name/id/row; needs --yes)")
    sp.add_argument("item"); sp.add_argument("--yes", action="store_true")
    sp = add("rename", help="rename a task (exact name/id/row)")
    sp.add_argument("item"); sp.add_argument("new_name")
    sp = add("undo", help="revert the most recent write")
    sp.add_argument("--force", action="store_true", help="undo the last scripted write even though the file was changed outside the script since")
    sp.add_argument("--previous", action="store_true", help="undo the last real write even though the last command changed nothing")
    args = p.parse_args(argv)
    today = date.today()
    handler = {"done": cmd_done, "set": cmd_set, "move": cmd_move, "add": cmd_add,
               "remove": cmd_remove, "rename": cmd_rename, "undo": cmd_undo}[args.cmd]
    label = " ".join(argv)
    try:
        (lines, wrote), problems = handler(args, today)
    except WriteError as e:
        if not args.dry_run and args.cmd != "undo":
            record_outcome(label, False)
        sys.exit(f"ERROR: {e} (nothing written)")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        if not args.dry_run and args.cmd != "undo":
            record_outcome(label, False)
        sys.exit(f"ERROR: task-tracker.json is malformed ({type(e).__name__}: {e}) (nothing written)")
    if not wrote and not args.dry_run and args.cmd != "undo":
        record_outcome(label, False)
    print("\n".join(lines))
    for prob in problems:
        print(f"SKIPPED: {prob}")
    if problems:
        sys.exit(3)  # partial: the ✓ items were written; the SKIPPED ones need the user

# ── Main ────────────────────────────────────────────────────────────
WRITE_COMMANDS = {"done", "set", "add", "move", "remove", "rename", "undo"}

def main():
    if len(sys.argv) > 1 and sys.argv[1] in WRITE_COMMANDS:
        return write_main(sys.argv[1:])
    import argparse
    parser = argparse.ArgumentParser(description="Task Tracker — status + delivery (see docstring for write subcommands)")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Delivery target: discord:<channel_or_thread_id> (default: task tracker thread)")
    parser.add_argument("--json", action="store_true", help="Print sections JSON to stdout, no delivery")
    parser.add_argument("--text-only", action="store_true", help="Send text summary only, no images")
    args = parser.parse_args()

    today = date.today()
    data = load_data()
    sections = build_sections(data, today)

    if args.json:
        print(json.dumps(sections, indent=2))
        return

    token = load_token()
    if not token:
        sys.exit(f"ERROR: DISCORD_BOT_TOKEN not found in {ENV_FILE}")
    if not args.target.startswith("discord:"):
        sys.exit(f"ERROR: target must be discord:<channel_or_thread_id>, got {args.target!r}")
    cid = channel_id(args.target)
    if not cid.isdigit():
        sys.exit(f"ERROR: no numeric Discord channel/thread id in target {args.target!r}")

    try:
        if args.text_only:
            if not discord_post(token, cid, summary_text(sections, today)):
                sys.exit(1)
            print(f"Sent text summary to channel {cid}", file=sys.stderr)
            return
        deliver(token, cid, sections, today, f"discord:{cid}")
    except FatalSend as e:
        sys.exit(f"ERROR: {e} — aborted before sending the remaining messages")

def deliver(token, cid, sections, today, target):
    title = f"## 📋 Task Tracker (as of {today.strftime('%b %-d, %Y')})"
    if not sections:  # nothing to render — say so instead of failing with "no images"
        if not discord_post(token, cid, title + "\n\nNo tasks are being tracked."):
            sys.exit(1)
        print(f"Delivered empty tracker to channel {cid}", file=sys.stderr)
        return

    # Render all tables in one process
    rendered = render_tables(sections)
    if not rendered:
        sys.exit("ERROR: no images rendered")

    def header(sec):
        overdue_count = sum(1 for r in sec["rows"] if "overdue" in r[5])
        return sec["title"] + (f" — {overdue_count} overdue" if overdue_count else "")

    # ONE post: title + one embed card per category (≤10 embeds per message).
    if len(sections) <= 10:
        status = discord_post_cards(token, cid, title,
                                    [(header(sec), rendered.get(sec["title"])) for sec in sections])
        if status in ("ok", "unknown"):
            # "unknown" may well have landed: never re-send (duplicates), but record the
            # row numbers the user is most likely looking at.
            save_snapshot(sections, target)
            if status == "unknown":
                sys.exit(f"ERROR: the post to channel {cid} timed out and may or may not have "
                         "arrived — check the channel before re-running")
            cleanup(list(rendered.values()))
            print(f"Delivered {len(sections)} sections to channel {cid} (one post)", file=sys.stderr)
            return
        print("WARNING: single post rejected; falling back to one message per category", file=sys.stderr)

    # Fallback (or more than 10 categories): the previous layout.
    failures = 0
    sent = []
    images = []
    try:
        # Title (text-only message)
        if not discord_post(token, cid, title):
            failures += 1
        time.sleep(RATE_DELAY)

        # One message per category: header as caption + image as native attachment,
        # both in a single multipart request so they always arrive paired and ordered.
        for sec in sections:
            img_path = rendered.get(sec["title"])
            if discord_post(token, cid, f"**{header(sec)}**", img_path):
                sent.append(sec)
                if img_path:
                    images.append(img_path)  # confirmed sent → safe to delete
            else:
                failures += 1
            time.sleep(RATE_DELAY)
    finally:
        # even on an abort, anything the user saw now defines the row numbers
        cleanup(images)  # delete only confirmed-sent images; keep failures for retry/debug
        if sent:
            save_snapshot(sent, target)  # only the sections that actually arrived
    if failures:
        sys.exit(f"ERROR: {failures} message(s) failed to send to channel {cid}")
    print(f"Delivered {len(sections)} sections to channel {cid}", file=sys.stderr)

if __name__ == "__main__":
    main()
