# Token Usage Skill — Developer Guide

Queries Hermes Agent's `state.db` SQLite database to produce weekly token usage reports (daily trends, MTD totals, all-time by model) delivered as table images to Discord.

## Architecture

```
state.db → token_report_no_agent.py → PIL table images → curl multipart → Discord
```

The production flow is fully self-contained — no LLM involvement at runtime.

## Key Files

| File | Purpose |
|---|---|
| `scripts/token_report_no_agent.py` | **Production script.** Self-contained: queries DB, renders PNGs via the shared renderer, posts each section via a status-checked `payload_json`+file multipart (`discord_post`), exits `0`/`1` (1 on any delivery failure). Deployed **byte-identical** to `~/.hermes/scripts/token_report_no_agent.py` (the cron-executed path) and this skill's `scripts/` copy — keep them in sync. |
| `../discord-table-format/scripts/render_tables.py` | Shared table renderer (the production script delegates to it). Raises `ValueError` on header/row width mismatch (2026-06). |
| `references/gen_token_report_images.py` | Generates table PNGs from a JSON file (PIL required; use via terminal(), not execute_code). |
| `references/cron-job-configuration.md` | Required reading before touching the cron job. |
| `references/scheduler-no-agent-python-execution.md` | Required reading before touching any no_agent script. |
| `references/token-report-double-fire-fix.md` | Documents 4 bugs fixed May 24-25, 2026. |
| `references/2026-07-delivery-hardening.md` | **Latest authoritative record** — 7-calendar-day window, hardened `discord_post` (timeout/status/429/`payload_json`/exit-code), dead-script removal. Read before editing the script/cron. |
| `references/2026-06-consolidation-bugfix-perf.md` | Two-copy consolidation, 10 bugfixes, cron fix, perf review. |
| `references/deepseek-pricing.md` | DeepSeek pricing reference. |
| `references/minimax-pricing.md` | MiniMax M2.7 pricing reference. |

## Data Source

- **DB path:** `/home/amh/.hermes/state.db` (always use concrete path — `$HERMES_HOME` may not be set in sandbox)
- **Table:** `sessions`
- **Key columns:** `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `api_call_count`, `estimated_cost_usd`, `actual_cost_usd`, `started_at` (Unix epoch float), `source`, `billing_provider`

## Cron Job Configuration

```json
{
  "job_id": "cbb893c8a65d",
  "name": "Weekly Token Usage Report",
  "no_agent": true,
  "script": "token_report_no_agent.py",
  "schedule": "0 11 * * 0",
  "deliver": "discord:1499972245437550602"
}
```

**`no_agent: true` is mandatory.** LLM mode wraps all output in `# Cron Job:` header/footer which cannot be suppressed.

**Which copy runs:** with `no_agent: true` and `profile: null`, the scheduler resolves `script: token_report_no_agent.py` under `~/.hermes/scripts/`, so the cron executes **`~/.hermes/scripts/token_report_no_agent.py`** — not this skill's `scripts/` copy. Both copies must stay byte-identical. Do NOT set a `profile` (a stray `profile: "pro"` made it look under `~/.hermes/profiles/pro/scripts/` and fail with `Script not found`). The scheduler's `sys.executable` is the gateway venv (`~/.hermes/hermes-agent/venv/bin/python`), where `tabulate` imports but `PIL` needs the dist-packages insert — see `references/2026-06-consolidation-bugfix-perf.md`. ⚠️ The 2026-09-01 Hermes update rebuilt that venv (`uv 0.11.28`, `include-system-site-packages=false`) and **dropped `filelock` from it** (2026-09-06 failure: `ModuleNotFoundError: No module named 'filelock'`). Never assume third-party packages survive a venv rebuild — the production script's double-fire guard now uses stdlib `fcntl.flock`.

## Critical Rules for no_agent Scripts

### 1. sys.path.insert BEFORE all imports

The scheduler invokes `.py` scripts via `sys.executable` (the gateway venv) directly (no shell), so `sys.path` does NOT include system dist-packages. PIL will fail without this:

```python
import sys
_system_pkgs = "/usr/lib/python3/dist-packages"
if _system_pkgs not in sys.path:
    sys.path.insert(0, _system_pkgs)

# NOW import PIL, tabulate, etc.
```

**Note:** the current production script does NOT import PIL directly — it
delegates rendering to `render_tables.py`, which performs this dist-packages
insert itself before importing PIL. So the script only needs to insert the
renderer's directory and `from render_tables import render_table`. This rule
still applies to any no_agent script that imports PIL/tabulate directly.

### 2. [SILENT] must be the absolute first output line

Scheduler captures ALL stdout and delivers it to Discord. `[SILENT]` as the first `print()` suppresses delivery:

```python
print("[SILENT]", flush=True)  # must be first — nothing before this
```

### 3. Bot token is NOT in os.environ for no_agent scripts

Read it directly from the `.env` file:

```python
ENV_FILE = os.path.expanduser("~/.hermes/.env")   # NOT ~/.hermes/hermes-agent/.env — that holds a stale token (401)
TOKEN = None
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break
if not TOKEN:
    os._exit(1)
```

### 4. os._exit(0) on success, os._exit(1) on a delivery failure — never sys.exit()

`sys.exit()` triggers atexit handlers which can produce final output that bypasses `[SILENT]` suppression. Exit **non-zero** when any Discord send fails so the scheduler surfaces a `⚠️ Cron job failed` alert (it does not auto-rerun, so already-posted sections don't duplicate); exit 0 on full success.

### 5. Capture subprocess output — never let it reach the script's own stdout

The scheduler treats the script's stdout as the message body (suppressed by `[SILENT]`). Curl's response body/errors must NOT leak there, so capture them (and don't print them):

```python
r = subprocess.run([...], capture_output=True, text=True, timeout=10)  # captured, not printed
```

## Discord Delivery Rules

**Atomic header+image pairing via `discord_post()`** — content and file in ONE multipart request per section, HTTP-status-checked, bounded, with one 429 retry. Full rationale in `references/2026-07-delivery-hardening.md`:

```python
def discord_post(content, img=None, _retried=False):
    payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    cmd = ['curl', '-sS', '--max-time', '8', '-w', '\n%{http_code}',
           '-X', 'POST', API_URL, '-H', f'Authorization: Bot {TOKEN}',
           '--form-string', f'payload_json={payload}']
    if img and os.path.exists(img):
        cmd += ['-F', f'file=@{img}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False
    body, _, code = r.stdout.rpartition('\n'); code = code.strip()
    if code == '429' and not _retried:
        try: time.sleep(min(float(json.loads(body).get('retry_after', 1)) + 0.25, 3))
        except Exception: time.sleep(1)
        return discord_post(content, img, _retried=True)
    return r.returncode == 0 and code.startswith('2')
```

- **Send the caption via `--form-string payload_json={json}`, NOT `-F content=`.** curl 7.88.1's `-F` truncates a value at the first `;` and reads a leading `@`/`<` as a file; `payload_json` sends the content verbatim (`;`/`@`/`<`/newlines) and rides `allowed_mentions:{"parse":[]}` in the same request to suppress accidental pings. JSON escapes decode back to identical rendered text.
- **Bound + check every send:** `curl --max-time 8` + `subprocess.run(timeout=10)`; treat only `returncode==0 and code.startswith('2')` as success; one 429 retry with a clamped `retry_after`. A failure → `ok=False` → `os._exit(1)`.
- **Clean up a PNG only after its own send is confirmed 2xx** — a failed section keeps its artifact (uniquely named, never re-attached).
- **Never use `send_message` with MEDIA: tags** — the gateway's `extract_media()` batches all images after all text, breaking header+image pairing regardless of order.

## Discord Channel IDs

| Destination | Channel | Thread |
|---|---|---|
| Production (work home) | `1499972245437550602` | none — direct channel |
| Test (playground) | `1499972245437550602` | `1501742338639401071` |

## DB Query Gotchas

### strftime requires 'unixepoch' modifier

`started_at` is a Unix epoch float. Without `'unixepoch'`, SQLite interprets it as a Julian day number and returns NULL for all rows:

```sql
-- CORRECT
strftime('%Y-%m-%d', started_at, 'unixepoch')

-- WRONG — returns NULL for every row
strftime('%Y-%m-%d', started_at)
```

### Double-percent bug in triple-quoted strings

`%%` inside `"""..."""` is a literal percent sign — SQLite receives `%%Y` instead of `%Y`. Use single `%` inside triple-quoted Python strings.

### MTD filtering — use Python timestamp, not SQL date()

`date('now', 'start of month')` returns text `'2026-05-01'`. Comparing text to a Unix float silently returns 0 rows:

```python
# CORRECT
mtd_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
cur.execute("... WHERE started_at >= ?", (mtd_start,))
```

### Filter NULL models

Some rows have `model IS NULL`. Always filter: `WHERE model IS NOT NULL AND input_tokens IS NOT NULL`.

### Daily "Last 7 Days" window — anchor at SGT midnight

Bound the daily query at SGT-midnight-today minus 6 days, not a rolling 168h instant:

```python
today_sgt   = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)  # keeps tzinfo=SGT
week_ago_ts = (today_sgt - timedelta(days=6)).timestamp()                 # today + 6 prior = 7 SGT days (zero-filled → always 7 rows)
```

The old `now_sgt - timedelta(weeks=1)` starts mid-day, so `GROUP BY strftime(...'+8 hours')` produced up to 8 partial calendar-day buckets. SGT is a fixed +8 offset (no DST), so the anchored bound is unambiguous across month/year rollovers.

The daily rows are then **zero-filled** over the full 7-day spine so a day with no sessions still renders a `0`/`$0.0000` row (a bare `GROUP BY` drops empty days):

```python
_by_day = {r[0]: r for r in days}
_expected = [(today_sgt - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]
# for each _expected day: use _by_day[d] if present, else a zeros row
```

## Table Image Format

JSON passed to `gen_token_report_images.py` must be flat row arrays — NOT nested `{headers, rows}`:

```python
# CORRECT
data = {
    "daily": [["2026-05-18", "4.7M", "0.4M", ...], ...],
    "mtd": [[...]],
    "alltime": [[...]]
}

# WRONG — tabulate receives a dict, produces garbled/alphabetical output
data = {"daily": {"headers": [...], "rows": [[...]]}}
```

Column order is positional and must match `gen_token_report_images.py` hardcoded headers:

| Table | JSON key | Column order |
|---|---|---|
| Last 7 Days | `daily` | date, input, output, cache_read, cache_write, sessions, est_cost (7 values) |
| MTD | `mtd` | sessions_count, input, output, cache_read, cache_write, est_cost (6 values — no TOTAL label) |
| All Time | `alltime` | model, sessions, input, output, cache_read, cache_write, total_cost (7 values) |

Header count must equal value count exactly — mismatches shift columns visually.

## Number Formatting

```python
def fmt(v, suffix=''):
    if v is None: return '0'
    if suffix == 'M':
        return f"{v/1e6:.1f}M" if v >= 1_000_000 else f"{v/1e3:.1f}K"
    if suffix == 'K':
        return f"{v/1e3:.1f}K"
    return str(v)
```

`fmt(15350, 'M')` → `15.3K`, not `0.0M`. (Sub-1M values render in K; the ≥1M cutoff rolls into M.)

## Double-fire prevention (stdlib fcntl.flock)

```python
import fcntl
LOCK_FILE = "/tmp/hermes_cache/token_report.lock"
_lock_fd = open(LOCK_FILE, "a+")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # non-blocking exclusive
except OSError:
    os._exit(0)  # another tick is already running — exit silently
```

`os._exit(0)` closes all file descriptors and releases the OS lock. The lock file persists on disk but has no effect until next acquire. `fcntl.flock` is stdlib — no package dependency — so a gateway-venv rebuild (see the 2026-09-01 `filelock` drop under Cron Job Configuration) cannot break the script at import. (Before 2026-09-06 the guard used `filelock.FileLock`.)

## Running Manually

```bash
# Run the cron-executed copy the way the scheduler does (gateway venv interpreter).
# The script reads ~/.hermes/.env itself, so no env sourcing is needed.
# WARNING: this sends a real report to the production channel — stub curl to dry-run.
/home/amh/.hermes/hermes-agent/venv/bin/python /home/amh/.hermes/scripts/token_report_no_agent.py

# Dry-run (no Discord send): stub curl on PATH so it emits an HTTP 200.
# A bare `exit 0` stub is NOT enough now — discord_post checks the status via
# `-w '%{http_code}'`, so with no "200" every send reads as failed → exit 1.
mkdir -p /tmp/stubbin && printf '#!/bin/sh\nprintf "{}\\n200"\n' > /tmp/stubbin/curl && chmod +x /tmp/stubbin/curl
PATH=/tmp/stubbin:$PATH /home/amh/.hermes/hermes-agent/venv/bin/python /home/amh/.hermes/scripts/token_report_no_agent.py
echo "exit: $?"   # expect 0 (all sends "2xx"); a bare-exit-0 stub yields 1

# Run image generator standalone
python3 /home/amh/.hermes/skills/productivity/token-usage/references/gen_token_report_images.py /tmp/hermes_cache/token_report_data.json
```

## PIL Availability

PIL is at `/usr/lib/python3/dist-packages/PIL`. It is **NOT** available in `execute_code` sandbox environments. Image generation must run via `terminal()` using system Python, with `sys.path.insert` as described above.

## Performance Notes (measured June 2026)

Full detail in `references/2026-06-consolidation-bugfix-perf.md`. Headlines:

- **The report pipeline is already fast** — ~0.5 s compute on a weekly cadence (daily/MTD queries are index-driven via `idx_sessions_started`; all-time is a 56 ms full scan). Wall-clock is dominated by the intentional `4 × sleep(0.5)` Discord rate-limit. Don't optimize it.
- **Cold vs warm cache dominates** on the Pi: first touch of the 24.5k-row `messages` table is disk-bound (a scan measured ~10.5 s cold vs ~0.85 s warm).
- **Skill-introspection queries: prefilter in SQL.** `WHERE tool_calls LIKE '%skill_view%'` instead of `IS NOT NULL` is ~14× faster (854 ms → 60 ms warm) and exact. Applied to `SKILL.md` Query 1 and `state-db-skill-introspection.md`.
- `state.db` is **not bloated** (freelist 0.4%) — VACUUM is not worth it.
- `render_tables.py` is already the optimized retina path — do not re-add the old upscale renderer.

## Cost Calculation Reference

**MiniMax M2.7:** input $0.30/M, output $1.20/M (cache reads have no separate cost, included in input price)

**DeepSeek V4 Pro:** input $1.74/M, output $3.48/M, cache_read $0.0145/M

**DeepSeek V4 Flash:** ~50% of V4 Pro pricing

Cache read tokens on MiniMax can exceed input tokens by 5–10x (accumulated context reuse). Do not compute cache hit rate as `cache_read / input` — it will yield 500%+.
