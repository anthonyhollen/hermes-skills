# Token Report — Delivery Troubleshooting Log

## May 25 — Extra empty report (LLM-mode trap)

**Symptom:** Weekly token report sent an extra empty report.

**Root cause identified:**  
The cron job `cbb893c8a65d` runs in **LLM mode** (no `no_agent: true`). Previous fixes updated the skill documentation and delivery target, but never actually converted the job to `no_agent: true`. The skill contains BOTH the script-mode delivery pattern AND the LLM-mode `Cron Prompt Section` — and the job keeps running in LLM mode, triggering the scheduler's unwrappable `# Cron Job:` header/footer wrapping.

The extra empty report comes from the LLM-mode scheduler path producing output that gets delivered alongside any other output.

**Fix:** The job MUST be converted to `no_agent: true` AND the dispatch script must be self-contained (DB query + image generation + delivery in one script, since `no_agent` means no LLM). See `references/cron-job-configuration.md`.

---

## May 18 — `send_message` also affected by `extract_media()` (non-cron)

**Symptom:** Report sent interactively in a Discord thread via `send_message` with inline MEDIA: tags. Images arrived BEFORE section headers — the opposite of correct ordering.

**Root cause:** `extract_media()` in the gateway runs on every `send_message` call, not just cron job outputs. It collects ALL MEDIA: tags from the full response text and sends them as a batch AFTER all text. This is not a cron-only behavior.

**Fix:** For ALL Discord sends (interactive AND cron), use Python subprocess curl with content+file in one multipart request per section. Never use `send_message` with inline MEDIA: tags.

**Key lesson:** The skill's framing as "cron delivery" was misleading. `extract_media()` is a gateway-level behavior on ALL sends. The fix is the same for both cases: programmatic curl delivery.

---

## May 17 — Wrong thread + duplicate report

**Symptom:** Weekly report sent to playground channel thread (`1501742338639401071`) instead of main work channel (`1499972245437550602`). Extra duplicate report without images also appeared.

**Root cause:** Cron job had `deliver: "discord:1499972245437550602:1501743328125911070"` — a thread ID from the playground channel was appended to the work channel ID. The scheduler interprets `channel_id:thread_id` as sending to that thread within that channel. When the thread ID belongs to a different channel, delivery goes to the wrong place entirely.

Additionally, when `no_agent: true` script output fails to deliver properly via `extract_media()` (which batches all MEDIA tags separately from all text), the scheduler may trigger a fallback LLM agent run as a separate delivery path, producing a second text-only report.

**Fix:** Cron job deliver target updated to `discord:1499972245437550602` (no thread). Always send cron reports to main channel without thread for maximum reliability.

**Key lesson:** The `deliver` field format is `platform:channel_id` or `platform:channel_id:thread_id`. Thread IDs from other channels (e.g., playground) must never be mixed into channels they don't belong to. Always use `discord:1499972245437550602` with no thread for token usage cron delivery.

---

## May 11 — JSON format: nested `{headers, rows}` vs flat array

**Symptom:** Numbers in token report table images appeared as garbled alphabetical characters (random Unicode letters instead of "4.7M", "$0.0451").

**Root cause:** `gen_token_report_images.py` reads `data.get('daily', [])` expecting a flat list of row arrays. The pipeline was saving `{headers, rows}` nested objects. When the script gets a dict, tabulate iterates over dict keys (`['headers', 'rows']`) producing garbage.

**Correct JSON format:**
```json
{"daily": [["2026-05-04","4.7M","0.4M",...], ...],
 "mtd":   [["MiniMax-M2.7","35.8M",...], ...],
 "alltime": [["MiniMax-M2.7","35.8M",...]]}
```
**Wrong JSON format (produced garbled output):**
```json
{"daily": {"headers":["Date","Input",...], "rows":[["2026-05-04",...], ...]}}
```

**A/B test:** Both formats sent to Discord side-by-side. Wrong format → alphabetical garbage. Correct format → numbers rendered correctly.

**Fix in pipeline:** Save flat arrays directly from Python. The image generator has hardcoded header lists (`["Date","Input","Output",...]`) — JSON only needs the row data.

---

## May 11 — `extract_media()` Root Cause

**Symptom:** Cron job response structure (header+MEDIA on consecutive lines, `---` separators) appeared correct, but Discord still batched all headers together, then all images together.

**Root cause:** `gateway/platforms/base.py` lines 1888–1943 — `extract_media()` collects ALL `MEDIA:<path>` tags from the *entire* `final_response` string into a list, sends all text in one batch, then sends all media files as a second batch. The scheduler's newline splitting is irrelevant.

**Confirmed fix:** Programmatic delivery via `no_agent: true` script that calls `send_message` directly per section.

**Files:**
- `/home/amh/.hermes/scripts/token_report.py` — programmatic report generator
- `/home/amh/.hermes/scripts/token_report_dispatch.sh` — shell wrapper
- Cron job `cbb893c8a65d` — `no_agent: true`, script: token_report_dispatch.sh
- **Delivery (FIXED):** `discord:1499972245437550602` (no thread — main work channel)

---

## May 10 — MTD SQL returns 0 rows

**Symptom:** MTD table shows 0 sessions despite data existing.

**Root cause:** `date('now', 'start of month')` returns `'2026-05-01'` (ISO string), but `started_at` is a Unix timestamp float. SQLite `>=` comparison between string and float silently returns 0 rows.

**Fix in jobs.json:** `date('now', 'start of month')` → `unixepoch('now', 'start of month')`.

**Verification:**
```sql
-- WRONG (0 rows):
SELECT COUNT(*) FROM sessions WHERE started_at >= date('now', 'start of month');

-- CORRECT (322+ rows):
SELECT COUNT(*) FROM sessions WHERE started_at >= unixepoch('now', 'start of month');
```

---

## May 10 — Image excess space

**Symptom:** Rendered table images had too much empty space at right and bottom edges.

**User signal:** "image width should just be slightly wider than table width. Same goes for height." / "none - all of them looks bad"

**Fix:** Bounding-box crop — render on 6000×2000 canvas, track `max_x` via `textbbox()`, crop to `(BORDER, BORDER, max_x + BORDER, y + BORDER)`, then `resize(2x, Image.LANCZOS)`.

**Final params:** FONT_SIZE=26, LINE_SPACING=2, BORDER=4, SCALE=2.

---

## May 11 — `\n` in curl `-F content=...` becomes literal string

**Symptom:** Discord messages showed `**Last 7 Days — Daily Trends**\n\n` with the two characters backslash-n instead of a blank line between sections.

**Root cause:** When using `curl -F "content=**Header**\n\nMEDIA:..."`, the shell expands `\n` to literal backslash-n before curl sees it, OR curl's multipart parser treats `\n` as literal text. Either way, Discord receives `\` and `n` as separate characters, not a newline control code.

**Fix:** Pass content as a Python string variable (no shell involved). The Python subprocess script works because `content={content}` in the curl command array passes the Python string's actual newlines directly — the shell never interprets them.

```python
# CORRECT: content as Python string — newlines stay as actual newlines
result = subprocess.run([
    'curl', '-s', '-X', 'POST',
    f'https://discord.com/api/v10/channels/{THREAD}/messages',
    '-H', f'Authorization: Bot {TOKEN}',
    '-F', f'content={content}',   # Python string with real \n chars
    '-F', f'file=@{img_path}'
], ...)
```

Alternative shell fix: `printf '%s' "multi\nline" > /tmp/msg.txt` then `-F "content@/tmp/msg.txt"` — but the Python script is cleaner.

---

## May 10 — Batching headers+images

**Symptom:** All section headers sent in one message, all images in subsequent messages.

**User signal:** "These 3 section headers below got sent out all at once, then all 3 table images sent out one by one."

**Fix:** Send one header+image pair, confirm, delete, repeat. Never batch.

---

> _Relocated from SKILL.md (June 2026) to keep the skill body lean. This is the
> historical execute_code → terminal → manual-send pipeline; the current production
> path is the self-contained `scripts/token_report_no_agent.py`._

## End-to-End Execution Log (Session 2026-05-11)

This documents the exact working pipeline used to deliver a corrected token usage report with readable numbers (fixing the "alphabet instead of numbers" bug).

### What was broken (four bugs)

1. **JSON format** — Passed nested `{headers, rows}` instead of flat `[[...]]` → tabulate received a dict → garbled/alphabetical output instead of numbers.
2. **MTD date() string vs Unix timestamp** — `date('now', 'start of month')` returns text `'2026-05-01'`; compared against `started_at` (float) → 0 rows silently.
3. **strftime missing unixepoch modifier** — `strftime('%Y-%m-%d', started_at)` without `'unixepoch'` returns `NULL` for all dates (started_at is a Unix epoch float, not a text timestamp) → daily table showed no rows.
4. **Column header-count mismatch** — MTD had 7 data values but 7 headers (including spurious `"Count"`), so tabulate treated the first data column as header and shifted everything. Fixed by removing the redundant `"Count"` header from MTD and all-time tables to make header count = data value count.
5. **`\n` in curl `-F content=...` becomes literal string** — Using `-F "content=**Header**\n\nMEDIA:..."` sends the two characters `\` and `n` to Discord instead of a blank line. Discord's multipart parser does not interpret `\n` as a control character in form-data content fields. Fix: write content to a temp file with `printf '%s' "multi\nline\ntext" > /tmp/msg.txt` and use `-F "content@/tmp/msg.txt"`. Or pass content as a Python string variable (no shell involved).

### Working pipeline (execute_code → terminal → Discord)

#### Step 1 — Query DB and prepare flat JSON (execute_code)

```python
import sqlite3, json
from datetime import datetime

conn = sqlite3.connect('/home/amh/.hermes/state.db')
cur = conn.cursor()

# --- Last 7 days ---
N = 7
now_ts = datetime.now().timestamp()
n_days_ago = now_ts - N * 86400

cur.execute("""
    SELECT strftime('%Y-%m-%d', started_at, 'unixepoch') as day,
           SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens),
           COUNT(*), SUM(estimated_cost_usd)
    FROM sessions
    WHERE started_at >= ? AND input_tokens IS NOT NULL
    GROUP BY day ORDER BY day
""", (n_days_ago,))

daily_rows = []
for row in cur.fetchall():
    day, inp, out, cr, cw, cnt, cost = row
    daily_rows.append([
        day,
        f"{inp/1e6:.1f}M" if inp else "0",
        f"{out/1e6:.1f}M" if out else "0",
        f"{cr/1e6:.1f}M" if cr else "0",
        f"{cw/1e6:.1f}M" if cw else "0",
        str(cnt),
        f"${cost:.4f}" if cost else "$0.0000"
    ])

# --- MTD (unixepoch for timestamp comparison!) ---
mtd_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

cur.execute("""
    SELECT
        COUNT(*), SUM(input_tokens), SUM(output_tokens),
        SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
    FROM sessions
    WHERE started_at >= ? AND input_tokens IS NOT NULL
""", (mtd_start,))

row = cur.fetchone()
# 6 values: COUNT, input, output, cache_read, cache_write, cost — positionally aligned to MTD headers [Sessions, Input, Output, Cache Read, Cache Write, Est. Cost]
mtd_rows = [[
    str(row[0]),  # COUNT(*) → "Sessions" column (position 0)
    f"{row[1]/1e6:.1f}M" if row[1] else "0",  # input_tokens → "Input"
    f"{row[2]/1e6:.1f}M" if row[2] else "0",  # output_tokens → "Output"
    f"{row[3]/1e6:.1f}M" if row[3] else "0",  # cache_read_tokens → "Cache Read"
    f"{row[4]/1e6:.1f}M" if row[4] else "0",  # cache_write_tokens → "Cache Write"
    f"${row[5]:.4f}" if row[5] else "$0.0000"  # estimated_cost_usd → "Est. Cost"
]]

# --- All-time by model ---
cur.execute("""
    SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens),
           SUM(estimated_cost_usd)
    FROM sessions
    WHERE model IS NOT NULL AND input_tokens IS NOT NULL
    GROUP BY model ORDER BY SUM(input_tokens) DESC
""")

alltime_rows = []
for row in cur.fetchall():
    alltime_rows.append([
        row[0], str(row[1]),
        f"{row[2]/1e6:.1f}M" if row[2] else "0",
        f"{row[3]/1e6:.1f}M" if row[3] else "0",
        f"{row[4]/1e6:.1f}M" if row[4] else "0",
        f"{row[5]/1e6:.1f}M" if row[5] else "0",
        f"{row[6]:.4f}" if row[6] else "0"
    ])

data = {
    "daily": daily_rows,
    "mtd":   mtd_rows,
    "alltime": alltime_rows
}

json_path = "/tmp/hermes_cache/token_report_data.json"
with open(json_path, 'w') as f:
    json.dump(data, f)
print(f"JSON saved to {json_path}")
print(json.dumps(data, indent=2))
```

**Critical format:** `{"daily": [[row1], [row2], ...]}` — flat array of row arrays, NO `headers` key, NO `rows` wrapper.

#### Step 2 — Generate images (terminal, system Python with PIL)

```bash
python3 /home/amh/.hermes/skills/productivity/token-usage/references/gen_token_report_images.py /tmp/hermes_cache/token_report_data.json
```

Output (JSON printed to stdout): `{"daily": "/tmp/hermes_cache/daily_1746930123456.png", "mtd": "...", "alltime": "..."}`

#### Step 3 — Send to Discord (terminal, Python script)

**⚠️ Use the Python script below — not raw curl with embedded `\n`.** Raw curl `-F "content=..."` with newlines produces literal `\n` in Discord.

```python
# /tmp/send_report.py — sends one header+image pair per request with real newlines
import subprocess, os, time

TOKEN = os.environ['DISCORD_BOT_TOKEN']
THREAD = "1503414583212249158"  # playground for test; use production thread for cron

sections = [
    ("**🧠 Token Usage Report — May 11, 2026**\n⚠️ Tables rendered as images.\n\n**Last 7 Days — Daily Trends**",
     "/tmp/hermes_cache/daily_1746930123456.png"),
    ("**Month-to-Date Totals — May 2026**",
     "/tmp/hermes_cache/mtd_1746930123456.png"),
    ("**All Time by Model**",
     "/tmp/hermes_cache/alltime_1746930123456.png"),
]

for content, img_path in sections:
    result = subprocess.run([
        'curl', '-s', '-X', 'POST',
        f'https://discord.com/api/v10/channels/{THREAD}/messages',
        '-H', f'Authorization: Bot {TOKEN}',
        '-F', f'content={content}',
        '-F', f'file=@{img_path}'
    ], capture_output=True, text=True)
    print(result.stdout[:200] if result.stdout else 'OK')
    time.sleep(0.5)

print("Done")
```

Run it:
```bash
set -a && source ~/.hermes/.env && set +a && python3 /tmp/send_report.py
```

**Critical rules:**
- Each curl sends `content` AND `file` in the SAME multipart request. This is the ONLY way to ensure header + image arrive as one atomic Discord message.
- **NEVER use cron scheduler's `extract_media()`** for this — it batches all images separately and breaks the pairing.
- For test runs: `THREAD="1501742338639401071"` (test channel)
- For production cron: `THREAD="1499972245437550602"` (main work channel — no thread)

#### Step 4 — Cleanup

```bash
rm -f /tmp/hermes_cache/*.png /tmp/hermes_cache/*.json
```

### Lessons learned

1. **JSON format is the #1 failure mode.** The image generator hardcodes its own column headers inside the script. External JSON must be flat row arrays only.
2. **MTD must use Python timestamp, not SQL date().** `date('now', 'start of month')` is text `'2026-05-01'`; compare with Unix epoch float.
3. **Atomic delivery:** header + image in ONE curl request = one Discord message with the image as an attachment.
4. **Test before delivering to production channel** — use `<#1501742338639401071>` (test channel) to validate image quality and number readability.

### Test vs Production routing

| Destination | Channel ID | Thread ID | Use for |
|---|---|---|---|
| Test | `1499972245437550602` | `1501742338639401071` | Validating image quality, JSON format |
| Production | `1499972245437550602` | **(none — main channel, no thread)** | Weekly cron delivery to work home |
| Work home | `1499972245437550602` | (none) | Direct channel messages |

**⚠️ Delivery target must always be `discord:1499972245437550602` (no thread).** A thread ID in the deliver target causes the message to go to the wrong channel. The scheduler interprets `channel_id:thread_id` as sending to that thread in that channel — but if the thread ID is from a different channel (e.g., playground thread used in work channel context), delivery goes to the wrong place entirely. Keep it simple: no thread for cron delivery.

