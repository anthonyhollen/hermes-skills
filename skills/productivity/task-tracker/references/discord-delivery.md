# Discord Delivery & Display Format

The standalone `scripts/task-tracker.py` already does everything below for "show status" (read → sort → compute → render → send → cleanup) in one no-LLM call. Only follow this when you must deliver tables by hand.

## Display format
- **Title** — `##` heading, not bold: `## 📋 Task Tracker (as of May 30, 2026)`.
- One table per **non-empty** category; empty categories are hidden.
- **Single running row counter across all categories** (not reset per category), so the user can say `"done 3, 7, 15"`.
- Within a category, sort by `last_done` oldest-first; `null` (never done) sorts **last** and shows "Never" / "Never done".
- Columns: `# | Task | Last Done | Days Ago | Next Due | Status`.
- Status column values (as the script emits them): `Xd overdue` (`overdue_by > 0`), `On track`, `Never done` (`last_done` is `null`), and `Bad date` (an unparseable or out-of-range `last_done` — the script shows the raw value and warns to stderr rather than crashing). Days Ago is `Nd`, or `—` for never-done, or `?` for a bad date.
- **No ✅ / ⚠️ / 🔴 icons in the Status column** — they break table alignment. Use plain text, and **bold** for emphasis.

Example category table:
```
**Weekly** (every 7 days)
| # | Task | Last Done | Days Ago | Next Due | Status |
|---|------|-----------|----------|----------|--------|
| 1 | Car wash | May 4 | 26d | May 11 | 19d overdue |
| 2 | Toothbrush charge | May 24 | 6d | May 31 | On track |
```

## Image pipeline (preferred over raw markdown)
The user prefers rendered image tables (`discord-table-format` skill), not markdown.

Build **one sections list** (one entry per non-empty category) and render them all in a **single** call — one process for the whole tracker, not one per category:
```bash
echo '[{"title":"Weekly","headers":["#","Task","Last Done","Days Ago","Next Due","Status"],
        "rows":[["1","Car wash","May 4","26d","May 11","19d overdue"], ...]},
       {"title":"Fortnightly","headers":[...],"rows":[...]}]' \
  | python3 .../discord-table-format/scripts/render_tables.py
# -> {"Weekly":"/tmp/hermes_cache/weekly_..png", ...}
```
The renderer auto-bolds rows matching its default `bold_match` (includes `"overdue"`), so no per-row font handling is needed. Never hand-construct rows — compute sort/status programmatically.

## Sending — direct Discord REST multipart (the only reliable method)
Deliver as **ONE post** (since 1.9.0, user decision 2026-10-29): `## Title` as the message text, then one embed per non-empty category with its header as the embed title and its table as the embed image (`attachment://cardN.png`, file part `files[N]`), in `weekly → fortnightly → monthly → quarterly` order. Never merge a category header into the overall title, and never omit a header. More than 10 categories (Discord's per-message embed limit), or a rejected single post, falls back to the layout below: title, then one header+image message per category.

**The standalone script already does this** — for "show status", just run it; it posts the single carded message (`discord_post_cards`) via the Discord REST API. Only deliver by hand if the user explicitly needs something the script can't do, and then use the **same** method:

> ⚠️ **Do NOT use `hermes send` / `send_message` with `MEDIA:` tags.** The gateway's `extract_media()` scans the whole reply, batches all text, then batches all images separately — text↔image pairing and order are lost. (The same lesson is documented in the `token-usage` and `sg-commute` skills.)

The reliable pattern is **one curl multipart POST per section**, with the caption and `file` (image) in the **same** request so Discord attaches them to one message. Send the caption as a JSON `payload_json` field (built with `json.dumps`) via `--form-string`, **not** `-F content=`:
```python
import os, subprocess, time, json
TOKEN = next(l.split("=",1)[1].strip() for l in open(os.path.expanduser("~/.hermes/.env"))
             if l.startswith("DISCORD_BOT_TOKEN="))   # scheduler does not export it
URL = f"https://discord.com/api/v10/channels/{thread_id}/messages"
for header, img in [("**Weekly (every 7 days)**", weekly_png), ...]:
    payload = json.dumps({"content": header, "allowed_mentions": {"parse": []}})
    cmd = ["curl","-sS","--max-time","20","-w","\n%{http_code}","-X","POST",URL,
           "-H",f"Authorization: Bot {TOKEN}","--form-string",f"payload_json={payload}"]
    if img: cmd += ["-F", f"file=@{img}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    code = r.stdout.rpartition("\n")[2].strip()
    if code in ("401", "403", "404"):
        raise SystemExit(f"HTTP {code} — stop (404 usually = a message id was used as the channel)")
    time.sleep(0.5)                                        # Discord rate limit: 5 msgs / 5s / channel
```
The script itself additionally retries one 429 (honouring `retry_after` up to 10 s, else fails), aborts the run on the first 401/403/404 instead of repeating it for every section, and records the delivered rows in `task-tracker.last-shown.json` so `done N` resolves against what the user saw.
**Why `payload_json`, not `-F content=`:** curl's `-F` treats a `;` in the value as a form-part parameter separator (silently truncating any caption containing `;`) and a leading `@`/`<` as a file-read directive (breaks the send, or reads a local file). `--form-string` + `json.dumps` sends the text verbatim (newlines, `;`, `@`, `<` all literal), and `allowed_mentions:{"parse":[]}` rides in the same atomic request so a task name containing `@everyone`/`<@id>` can never ping the channel. The file part stays `-F file=@…` (the `@` is intended there). A thread id works directly as the channel in the URL. Delete each image only after its POST returns 2xx.
