#!/usr/bin/env python3
"""
token_report_no_agent.py — Weekly Token Usage Report (no_agent mode)

Queries state.db, renders three table images via the shared discord-table-format
renderer, and posts them to Discord. Prints [SILENT] first so the scheduler does
not re-deliver this script's stdout, holds an exclusive OS-level file lock
(stdlib fcntl.flock — no third-party dependency) so overlapping scheduler
ticks cannot double-fire, and hard-exits via os._exit(0 on success / 1 on a
delivery failure so the scheduler surfaces a "⚠️ Cron job failed" alert).

SINGLE SOURCE OF TRUTH — this exact file is deployed, byte-for-byte, to BOTH:
  * /home/amh/.hermes/scripts/token_report_no_agent.py              (run by the cron job)
  * /home/amh/.hermes/skills/productivity/token-usage/scripts/...   (the documented copy)
Keep them identical. The cron job (id cbb893c8a65d) must stay no_agent:true with
profile unset, so `script: token_report_no_agent.py` resolves under ~/.hermes/scripts/.
"""
import sys, sqlite3, subprocess, os, time, json, fcntl
from datetime import datetime, timezone, timedelta

# Rendering is consolidated in the discord-table-format skill's shared renderer.
# Importing it also makes PIL + tabulate importable (render_tables inserts the
# system dist-packages path before importing them), so no PIL import is needed
# here. The double-fire guard below deliberately uses stdlib fcntl instead of
# the third-party filelock package: a Hermes update (2026-09-01) rebuilt the
# gateway venv and dropped filelock from it, which used to break this script
# at import time (ModuleNotFoundError).
# Hermes v0.22.0 (2026-10-14) runs no_agent scripts on its Python 3.14 dependency venv,
# where the renderer's tabulate (~/.local) and system Pillow (dist-packages, built for
# 3.11) can't load — the Sunday report would fail at the import below. Re-run this
# script under the system python, which has both, before anything is printed.
_SYS_PY = "/usr/bin/python3"
if (os.path.exists(_SYS_PY) and os.path.realpath(sys.executable) != os.path.realpath(_SYS_PY)
        and not os.environ.get("TOKEN_REPORT_REEXEC")):
    os.environ["TOKEN_REPORT_REEXEC"] = "1"
    os.execv(_SYS_PY, [_SYS_PY, os.path.abspath(__file__)] + sys.argv[1:])

sys.path.insert(0, "/home/amh/.hermes/skills/productivity/discord-table-format/scripts")
from render_tables import render_table

# ── 1. Suppress scheduler delivery (MUST be the very first stdout line) ───────
print("[SILENT]", flush=True)

# ── 1b. Prevent double-firing: hold an exclusive lock for the whole run. ──────
CACHE_DIR = "/tmp/hermes_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
LOCK_FILE = f"{CACHE_DIR}/token_report.lock"
# fcntl.flock is stdlib: the lock lives on the OS-level fd (held until this
# process exits — no explicit close/GC needed) and has no package dependency,
# so it survives gateway-venv rebuilds that drop pip-installed packages.
_lock_fd = open(LOCK_FILE, "a+")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # non-blocking exclusive
except OSError:
    os._exit(0)  # another tick is already running — exit silently

# ── 2. Config ─────────────────────────────────────────────────────────────────
SGT     = timezone(timedelta(hours=8))

# Dynamic delivery target via TOKEN_REPORT_TARGET env var.
# Format: "channel_id" (regular message) or "channel_id:thread_id" (threaded reply).
# Falls back to the production home channel when unset.
_default_target = "1499972245437550602"
_target = os.environ.get("TOKEN_REPORT_TARGET", "").strip() or _default_target
if ":" in _target:
    CHANNEL, THREAD_ID = _target.split(":", 1)
else:
    CHANNEL, THREAD_ID = _target, None

# The scheduler does NOT pass DISCORD_BOT_TOKEN to no_agent scripts, and
# ~/.hermes/hermes-agent/.env holds a stale token (401). Read ~/.hermes/.env.
ENV_FILE = os.path.expanduser("~/.hermes/.env")
TOKEN = None
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break
if not TOKEN:
    os._exit(1)


def fmt(v, suffix=''):
    """Format a token count, auto-scaling to K below 1M (e.g. 15_350 -> '15.3K')."""
    if v is None:
        return '0'
    if suffix == 'M':
        return f"{v/1e6:.1f}M" if v >= 1_000_000 else f"{v/1e3:.1f}K"
    if suffix == 'K':
        return f"{v/1e3:.1f}K"
    return str(v)


def money(v):
    """Format a cost; SUM(estimated_cost_usd) is NULL when all rows are NULL."""
    return f"${v:.4f}" if v is not None else "$0.0000"


# ── 3. Timestamps (SGT — the month/day boundaries must match the user's tz) ───
now_sgt     = datetime.now(SGT)
# "Last 7 Days" = exactly 7 SGT calendar days ending today. Anchor the window at
# SGT midnight (today − 6 days) rather than a rolling 168h instant, so the daily
# table always covers whole calendar days (at most 7 buckets), never a partial
# 8th day carved out by the run's time-of-day.
today_sgt   = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
week_ago_ts = (today_sgt - timedelta(days=6)).timestamp()
mtd_ts      = datetime(now_sgt.year, now_sgt.month, 1, tzinfo=SGT).timestamp()
month_name  = now_sgt.strftime('%B %Y')

# ── 4. DB queries ───────────────────────────────────────────────────────────────
conn = sqlite3.connect('/home/amh/.hermes/state.db')
c = conn.cursor()

# Daily: started_at is a Unix epoch float, so 'unixepoch' is required; '+8 hours'
# buckets by SGT calendar day. The grouped strftime string is also the displayed
# label, so the label always matches its bucket. Single % (NOT %%) — %% would be
# passed to SQLite literally and collapse every row into one bogus group.
c.execute("""
    SELECT strftime('%Y-%m-%d', started_at, 'unixepoch', '+8 hours') AS day,
           SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens),
           COUNT(*), SUM(estimated_cost_usd)
    FROM sessions WHERE started_at >= ?
    GROUP BY day ORDER BY day
""", (week_ago_ts,))
days = c.fetchall()

# MTD: use a Python timestamp, NOT SQL date()/unixepoch('now',...) — comparing a
# text date to the epoch-float started_at silently returns zero rows.
c.execute("""
    SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
    FROM sessions WHERE started_at >= ?
""", (mtd_ts,))
mtd = c.fetchone()

c.execute("""
    SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
    FROM sessions WHERE model IS NOT NULL
    GROUP BY model ORDER BY SUM(input_tokens) DESC
""")
alltime = c.fetchall()
conn.close()

# ── 5. Build rows ───────────────────────────────────────────────────────────────
# Zero-fill the daily table over the full 7-day SGT calendar spine so a day with
# no sessions still shows a row (0 tokens / 0 sessions / $0.0000) instead of being
# dropped. The query only emits days that have rows; `expected` is every SGT day
# in the window (today − 6 … today), oldest first, matching the strftime labels.
_by_day = {r[0]: r for r in days}
_expected = [(today_sgt - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
rows_daily = []
for _d in _expected:
    r = _by_day.get(_d)
    if r:
        rows_daily.append([r[0], fmt(r[1], 'M'), fmt(r[2], 'M'), fmt(r[3], 'M'),
                           fmt(r[4], 'M'), r[5], money(r[6])])
    else:  # no sessions that day — explicit zero row
        rows_daily.append([_d, fmt(0, 'M'), fmt(0, 'M'), fmt(0, 'M'),
                           fmt(0, 'M'), 0, money(0.0)])

# MTD is one row of 6 values aligned to 6 headers: COUNT -> "Sessions", then the
# four token sums, then cost. No "TOTAL" label and no spurious "Count" header.
rows_mtd = [[
    mtd[0], fmt(mtd[1], 'M'), fmt(mtd[2], 'M'),
    fmt(mtd[3], 'M'), fmt(mtd[4], 'M'), money(mtd[5]),
]]

rows_alltime = [
    [r[0] or 'unknown', r[1], fmt(r[2], 'M'), fmt(r[3], 'M'),
     fmt(r[4], 'M'), fmt(r[5], 'M'), money(r[6])]
    for r in alltime
]

# ── 6. Render images (render_table returns None when a table has no rows) ──────
p_daily = render_table(
    ['Date', 'Input', 'Output', 'Cache Read', 'Cache Write', 'Sessions', 'Est. Cost'],
    rows_daily, out_prefix="daily", bold_match=())
p_mtd = render_table(
    ['Sessions', 'Input', 'Output', 'Cache Read', 'Cache Write', 'Est. Cost'],
    rows_mtd, out_prefix="mtd", bold_match=())
p_alltime = render_table(
    ['Model', 'Sessions', 'Input', 'Output', 'Cache Read', 'Cache Write', 'Cost'],
    rows_alltime, out_prefix="alltime", bold_match=())

# ── 7. Send to Discord ──────────────────────────────────────────────────────────
# When THREAD_ID is set, post directly to the thread channel endpoint — this is
# the only reliable way to produce threaded replies via the Discord API.
API_URL = f'https://discord.com/api/v10/channels/{THREAD_ID or CHANNEL}/messages'


def discord_post(content, img=None, _retried=False):
    """Post content (+ optional image) in ONE multipart request. Returns True on 2xx.

    Hardened over a bare fire-and-forget curl:
      * bounded timeout — a hung/slow connection can no longer tie up the run
        (and the filelock) until the scheduler's 120s SIGKILL;
      * HTTP-status check + one 429 retry honoring a (clamped) retry_after;
      * body sent as `--form-string payload_json=` — curl's `-F` truncates a
        value at the first ';' and reads a leading '@'/'<' as a file to upload;
      * allowed_mentions:{"parse":[]} blocks any accidental @mention ping.
    The bot token is only ever in argv; curl's stdout/stderr are captured (never
    printed), so the [SILENT] stdout-suppression contract is preserved.
    """
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    # Caps chosen so the whole run (4 messages, each up to one 429 retry) stays
    # comfortably under the scheduler's global 120s SIGKILL: worst case
    # 4 × (2×10s + 3s) + sleeps ≈ 94s. A hung/slow connection can no longer
    # tie up the run — and the filelock — until that SIGKILL.
    cmd = ['curl', '-sS', '--max-time', '8', '-w', '\n%{http_code}',
           '-X', 'POST', API_URL, '-H', f'Authorization: Bot {TOKEN}',
           '--form-string', f'payload_json={payload}']
    if img and os.path.exists(img):
        cmd += ['-F', f'file=@{img}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False
    body, _, code = r.stdout.rpartition('\n')
    code = code.strip()
    if code == '429' and not _retried:
        try:
            time.sleep(min(float(json.loads(body).get('retry_after', 1)) + 0.25, 3))
        except Exception:
            time.sleep(1)
        return discord_post(content, img, _retried=True)
    return r.returncode == 0 and code.startswith('2')


# Title (text only) first.
ok = discord_post(f'## 🧠 Token Usage Weekly Report — {month_name}')
time.sleep(0.5)

# Then each section: header + image in ONE multipart request (the only reliable
# way to keep a header paired with its image on Discord). Skip a section whose
# image is None (that table had no rows); clean up a PNG only once its send is
# confirmed, so a failed delivery keeps its artifact instead of vanishing.
sections = [
    ("### 📅 Last 7 Days — Daily Trends", p_daily),
    (f"### 📆 Month-to-Date Totals — {month_name}", p_mtd),
    ("### 🏆 All Time by Model", p_alltime),
]
for header, img_path in sections:
    if not img_path:
        continue
    sent = discord_post(header, img_path)
    ok = ok and sent
    if sent and os.path.exists(img_path):
        os.remove(img_path)
    time.sleep(0.5)

# ── 8. Exit ───────────────────────────────────────────────────────────────────
# Non-zero on any delivery failure so the scheduler surfaces a "⚠️ Cron job
# failed" alert rather than the weekly report silently disappearing.
if not ok:
    print("token-report: one or more Discord sends failed", file=sys.stderr)
os._exit(0 if ok else 1)
