# 2026-07-12 — Delivery hardening + 7-calendar-day window tune-up

**Latest authoritative record.** Supersedes `2026-06-consolidation-bugfix-perf.md` for the delivery path and the daily window. Read this before editing `token_report_no_agent.py`. Method: `hermes-skill-tuneup` (bug hunt → perf → delivery → terminal gate → docs).

## Summary of changes (version 1.0.0 → 1.1.0)

1. **Daily window → exactly 7 SGT calendar days.** Was a rolling 168h instant (`now − timedelta(weeks=1)`) which — because the cron fires mid-day — could carve out up to **8** partial SGT calendar-day buckets. Now anchored at SGT midnight:
   ```python
   today_sgt   = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
   week_ago_ts = (today_sgt - timedelta(days=6)).timestamp()
   ```
   Covers today + the 6 prior calendar days; each label still equals its `strftime` bucket. The query's `GROUP BY` drops days with no sessions, so the daily rows are **zero-filled** from a Python date spine → always exactly **7 rows** (a no-session day renders `0`/`$0.0000`; see the 2026-07-13 follow-up below). `.replace()` preserves `tzinfo=SGT`, and SGT is a fixed +8 offset (no DST), so the window is unambiguous across month/year rollovers.

2. **Hardened Discord delivery (`discord_post` helper).** The old two inline `curl -s … -F content=… -F file=@…` blocks were fire-and-forget: stdout/stderr → `DEVNULL`, no timeout, no HTTP-status check, an unconditional final PNG-cleanup loop, and `os._exit(0)` always. Failure modes fixed (bug-classes from `hermes-skill-tuneup/references/bug-hunt.md`):
   - **Silent total failure** (*swallowed subprocess failure* + *exit-1-vs-0 contract*): a 401/403/429/5xx on every POST left the script exiting 0 with `[SILENT]` stdout → the scheduler posted **nothing** AND raised **no** `⚠️ Cron job failed` alert. Now every POST is HTTP-status-checked; any failure sets `ok=False` → `os._exit(1)`, so the scheduler surfaces a failure alert (it does not auto-rerun — verified in `cron/scheduler.py` — so the already-posted sections don't duplicate).
   - **Unbounded hang holding the filelock** (*uncaught hang*): an untimed curl could block until the scheduler's 120s SIGKILL while holding `token_report.lock`. Now `curl --max-time 8` + `subprocess.run(timeout=10)` + catch `TimeoutExpired`. Caps chosen so the whole run (4 messages, each ≤1 retry) stays well under 120s (worst case ≈ 94s).
   - **Premature cleanup**: the final loop deleted all PNGs regardless of send outcome. Now a PNG is removed only after **its own** send is confirmed 2xx — a failed section keeps its artifact for inspection (leftovers are uniquely named `{prefix}_{ms}_{seq}.png`, never re-attached to a later run).
   - **`-F content=` fragility** (*curl -F truncation*): curl 7.88.1's `-F` truncates a value at the first `;` and reads a leading `@`/`<` as a file. Content is now sent as `--form-string payload_json={json}` with `allowed_mentions:{"parse":[]}` — verbatim content (`;`/`@`/`<`/newlines safe) + accidental-ping suppression. Delivered message text is **byte-identical** to before (JSON escapes decode back to the same string; validated by identical rendered-PNG md5s and content strings).
   - **One 429 retry** honoring a clamped `retry_after` (`min(retry_after + 0.25, 3)`), bounded to a single retry via `_retried`.

3. **Removed the deprecated `token_report_dispatch.py`.** Dead code (no cron referenced it) that read the bot token from `os.environ` (→ `KeyError` under a no_agent cron, which the scheduler does not export it to) and hardcoded a stale "May 2026" label; the auditor flagged it HIGH every run. Deleted (backed up as `token_report_dispatch.py.bak-removed-20260712`). Historical mentions in `cron-job-configuration.md` / `scheduler-no-agent-python-execution.md` / `token-report-delivery-log.md` are dated records and were left intact.

## The canonical delivery pattern (source of truth: `scripts/token_report_no_agent.py`)

`token_report_no_agent.py` reads `TOKEN_REPORT_TARGET` (`"channel_id"` or `"channel_id:thread_id"`; defaults to home channel `1499972245437550602`). When a thread id is set it POSTs to `channels/{thread_id}/messages` (a thread *is* a channel for this endpoint — the only reliable threaded-reply path). Title text-only first, then each `###` header + its image in one request:

```python
def discord_post(content, img=None, _retried=False):
    """content (+ optional image) in ONE multipart request. Returns True on 2xx."""
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

Hard rules (still load-bearing):
- **Header + its image in ONE request** — the only reliable way to keep them paired on Discord. `##` title / `###` section headers, never `**bold**`.
- **Bot token from `~/.hermes/.env`** — NOT `~/.hermes/hermes-agent/.env` (stale → 401); the scheduler doesn't pass it via env to no_agent scripts.
- **`[SILENT]` first stdout line; `os._exit(0` on success `/ 1` on any delivery failure`)`; hold an exclusive `fcntl.flock` (stdlib; before 2026-09-06 it was `filelock.FileLock` — dropped from the gateway venv by the 2026-09-01 Hermes update) against double-firing; curl output is captured (never printed) so `[SILENT]` still suppresses re-delivery.**
- **Never `send_message` / `MEDIA:` for paired text+image** — `extract_media()` batches all images after all text, breaking pairing.
- **Deliver target:** `discord:1499972245437550602`, no thread.

Why (scheduler internals, `[SILENT]`, double-fire, venv interpreter): `scheduler-no-agent-python-execution.md`, `cron-job-configuration.md`, `2026-06-consolidation-bugfix-perf.md`; war stories in `token-report-delivery-log.md`.

## Verification checklist (relocated from SKILL.md 2026-07-12)

- [ ] Daily shows exactly 7 SGT calendar days (today + 6 prior), **zero-filled** (a no-session day is a `0` row), labels match buckets, `strftime` has `'unixepoch'` + single `%`
- [ ] MTD window via Python timestamp; **MTD row = exactly 6 values, no `TOTAL`**
- [ ] Header count == value count per table (daily 7, MTD 6, all-time 7)
- [ ] Counts auto-scale (`15.3K` / `35.9M`); NULL models filtered; NULL costs guarded
- [ ] JSON is flat arrays; images via `terminal()` (system PIL)
- [ ] Each section sent as one `payload_json`+file multipart, **HTTP-status-checked**; a PNG is deleted only after its send is confirmed
- [ ] (cron) `[SILENT]` first; `os._exit(0/1)`; `fcntl.flock` held; deliver to `1499972245437550602`

## Validation performed (2026-07-12)
- Both deployed copies (`~/.hermes/scripts/` + skill `scripts/`) md5-identical; `py_compile` clean under the gateway venv.
- Happy path (curl stubbed → HTTP 200): exit 0, `[SILENT]` first, 4 POSTs in order (title, daily, mtd, alltime), and the 3 rendered PNG md5s **byte-identical** to the pre-change baseline → content/images unchanged.
- Failure path (curl stubbed → HTTP 500): exit 1, stderr diagnostic `token-report: one or more Discord sends failed`, PNGs retained.
- Read-only `GET /channels/1499972245437550602` → HTTP 200 (auth + target reachable), nothing posted.
- Independent blind reviewers (3 finders + an adversarial refuter) on the original code, then a 2-reviewer terminal gate over the cumulative diff → SHIP, no regressions; the gate's flagged worst-case-429-runtime edge was closed by tightening the caps to 8/10/3.


## 2026-07-13 follow-up — zero-fill empty days (version 1.1.0 → 1.1.1)

The 7-calendar-day change (above) still relied on the query's `GROUP BY`, which emits **only** days that have ≥1 session — so a zero-session day was dropped and the "Last 7 Days" table rendered fewer than 7 rows (reported live: Jul 8 had no sessions and vanished). Fixed by **zero-filling from a Python date spine** in the row-build step:

```python
_by_day = {r[0]: r for r in days}
_expected = [(today_sgt - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(6, -1, -1)]  # oldest→newest
rows_daily = []
for _d in _expected:
    r = _by_day.get(_d)
    if r:
        rows_daily.append([r[0], fmt(r[1],'M'), fmt(r[2],'M'), fmt(r[3],'M'), fmt(r[4],'M'), r[5], money(r[6])])
    else:  # no sessions that day — explicit zero row
        rows_daily.append([_d, fmt(0,'M'), fmt(0,'M'), fmt(0,'M'), fmt(0,'M'), 0, money(0.0)])
```

`_expected` labels are SGT calendar days built the same way as the `strftime('…','+8 hours')` bucket keys, so they match exactly; the query bound (`>= today−6 midnight`) guarantees every returned day is within `_expected`, so no query row is orphaned. Result: **always exactly 7 rows**, width 7 (unchanged, no renderer mismatch); a no-session day renders `2026-07-08 | 0.0K | 0.0K | 0.0K | 0.0K | 0 | $0.0000`.

**Validated (2026-07-13):** both copies md5-identical + `py_compile` clean under the venv; mirror of the row-build over live `state.db` produced 7 rows with Jul 8 zero-filled; end-to-end stubbed-curl run exit 0. Method note (fed back to `hermes-skill-tuneup/bug-hunt.md`): a `GROUP BY` over a calendar window silently drops empty buckets — if the report should be a fixed-height N-day table, zero-fill from a date spine; this is a foreseeable follow-up whenever you anchor a windowed report, so surface it with the window change.

## 2026-09-13 follow-up — title line cleanup (v1.1.1 → v1.1.2)

Per user request, the `⚠️ *Tables rendered as images for best display quality.*` line is removed from the title message. The title is now the single line `## 🧠 Token Usage Weekly Report — {Month Year}`; section messages (`###` header + image) are unchanged.

- Both deployed copies updated and kept byte-identical (`~/.hermes/scripts/` + skill `scripts/`); the unused May-era scripts (`token_report.py`, `token_report_debug.py`, `token_report_dispatch.py`) had the same line stripped so no live or resurrectable code path re-emits it. Historical dated logs keep their original quotes.
- SKILL.md message-format block updated; skill version bumped to 1.1.2.
- Validated 2026-09-13 (stubbed-curl method, same as below): `py_compile` clean under the gateway venv; end-to-end stub run → exit 0, 4 POSTs in order (title, daily, MTD, all-time), title payload carries no ⚠️ line, each section still sends header+image as one multipart; `/channels/...` read-only GET → HTTP 200; nothing posted.
