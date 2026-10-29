---
name: token-usage
description: "Use when querying Hermes Agent token usage, cost analytics, session history, or API call statistics from state.db."
version: 1.2.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [token-usage, cost, analytics, sessions, sqlite, insights]
    related_skills: [hermes-agent, hermes-agent-skill-authoring]
---

# Token Usage Analytics

Query Hermes Agent's `state.db` to produce token-usage reports, cost breakdowns, and session/skill analytics, and deliver them to Discord.

> **Maintaining the report pipeline, renderer, or cron?** The deep detail lives in the references below — not in this body. Start with `references/2026-07-delivery-hardening.md` (latest authoritative record) and `CLAUDE.md` (developer guide).

## Data Source

- **DB:** `/home/amh/.hermes/state.db` — use the concrete path (`$HERMES_HOME` may be unset in the `execute_code` sandbox).
- **`sessions`** (one row/session): `id, source, model, user_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count, estimated_cost_usd, actual_cost_usd, started_at` (Unix epoch float)`, ended_at, message_count, tool_call_count`.
- **`messages`**: per-message rows; `tool_calls` (JSON) drives skill-usage introspection.

## Quick Interactive Queries (ad-hoc)

When the user asks "show me token usage" / "how much did I spend" / quick status checks — use the **same image pipeline as the weekly cron** (query DB, render images, send ONE message). The simplest way is to run the production script (see *Running Manually* in CLAUDE.md); `TOKEN_REPORT_TARGET` sets the channel/thread. The user prefers consistent image-based output.

**Delivery rule — exactly ONE message, nothing else** (user decision 2026-10-29; it used to be 4):

| Part | Content |
|------|---------|
| message text | `## 🧠 Token Usage Weekly Report — {Month} {Year}` |
| embed 1 | title `📅 Last 7 Days — Daily Trends` + daily table image |
| embed 2 | title `📆 Month-to-Date Totals — {Month} {Year}` + mtd table image |
| embed 3 | title `🏆 All Time by Model` + alltime table image |

Each section header is its embed's title, so header and table stay paired inside the one post. No extra text summary, no bulleted data, no verbose recap. If the single post is rejected, the script falls back to the old layout (title, then one `###` header + image message per section).

Key queries:
- **Last 7 days daily**: same `strftime` + SGT grouping as the weekly report
- **MTD totals**: same Python-timestamp MTD window
- **All time by model**: same `GROUP BY model` query
- **Today only**: filter `started_at` to today's SGT midnight

## Weekly Report (cron — full image pipeline)

Produced by the **production script `scripts/token_report_no_agent.py`** (self-contained, no LLM) — the source of truth. It is deployed **byte-identical** to `~/.hermes/scripts/token_report_no_agent.py` (the cron-executed copy) and this skill's `scripts/`; keep them in sync. It sends **exactly 3 tables**, in order:

1. **Last 7 Days — Daily Trends** (7 SGT calendar days ending today, window anchored at SGT midnight `today − 6 days`; **zero-filled** so a no-session day still shows a `0` row; est. cost/day)
2. **Month-to-Date Totals** (one row)
3. **All Time by Model**

Do **not** add "Top 5 Sessions" or "MTD by Source" — removed intentionally. Cron: `no_agent: true`, `profile` unset, `deliver: discord:1499972245437550602`; read `references/cron-job-configuration.md` before touching it.

**Message format** (every delivery — cron, manual, test): ONE post.
```
## 🧠 Token Usage Weekly Report — {Month} {Year}        ← message text
┃ 📅 Last 7 Days — Daily Trends                         ← embed title + attachment://section0.png
┃ 📆 Month-to-Date Totals — {Month} {Year}              ← embed title + attachment://section1.png
┃ 🏆 All Time by Model                                  ← embed title + attachment://section2.png
```
`##` for the title. Section headers are embed titles (plain text, no `###`); the fallback layout uses `###` headers — never `**bold**`.

## Querying state.db

Rules that silently corrupt results if violated:
- **Daily grouping:** `strftime('%Y-%m-%d', started_at, 'unixepoch', '+8 hours')` — `started_at` is an epoch float so `'unixepoch'` is required; `'+8 hours'` = SGT day; use the grouped string as the label. **Single `%`** — inside a Python `"""` string `%%` reaches SQLite literally and collapses every row into one bogus group.
- **Daily window:** bound `started_at >= (SGT-midnight-today − 6 days)` (`now_sgt.replace(hour=0,…) - timedelta(days=6)`), **not** a rolling `now − timedelta(weeks=1)` — the rolling instant carves an 8th partial calendar-day bucket at the cron's mid-day fire time; the anchored bound covers exactly 7 SGT calendar days, and the rows are **zero-filled** from a Python date spine so a day with no sessions still renders a `0`/`$0.0000` row (a bare `GROUP BY` drops empty days).
- **MTD window:** compute month-start in Python (`datetime(y, m, 1, tzinfo=SGT).timestamp()`) and pass as a bound — SQL `date('now','start of month')` is text and compares false against the epoch float (0 rows).
- **NULL models:** `WHERE model IS NOT NULL` for the all-time table. **NULL costs:** guard `SUM(estimated_cost_usd)` (can be NULL → format crash).

```python
cur.execute("""
  SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
         SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
  FROM sessions WHERE model IS NOT NULL
  GROUP BY model ORDER BY SUM(input_tokens) DESC
""")
```

### Table column schema (positional — must match the renderer's headers)

| Table | key | columns, in order |
|---|---|---|
| Last 7 Days | `daily` | Date, Input, Output, Cache Read, Cache Write, Sessions, Est. Cost (**7**) |
| MTD | `mtd` | Sessions(=COUNT), Input, Output, Cache Read, Cache Write, Est. Cost (**6 — no `TOTAL` label**) |
| All Time | `alltime` | Model, Sessions, Input, Output, Cache Read, Cache Write, Cost (**7**) |

Header count **must equal** row value count — the shared renderer raises on a mismatch (guard added June 2026). Auto-scale counts: `f"{v/1e6:.1f}M"` if ≥1M else `f"{v/1e3:.1f}K"` (so 15 350 → `15.3K`, not `0.0M`).

## Image Generation

Render with `references/gen_token_report_images.py` (a thin wrapper over `discord-table-format/scripts/render_tables.py`). **PIL is only in system Python, not the `execute_code` sandbox** — run it via `terminal()`. Input JSON must be **flat row arrays**, never `{headers, rows}` (a dict makes tabulate emit garbled/alphabetical output):
```python
data = {"daily": [["2026-06-01","4.7M","0.4M","8.8M","0.4M","6","$0.0000"], ...],
        "mtd": [[...]], "alltime": [[...]]}
```

## Discord Delivery (canonical)

Delivery is `discord_post()` in the production script — **the source of truth**; full code + rationale in `references/2026-07-delivery-hardening.md`. Target from `TOKEN_REPORT_TARGET` (`"channel_id"` or `"channel_id:thread_id"` — a thread id posts to `channels/{thread_id}/messages`; defaults to `1499972245437550602`).

- ONE multipart request: `payload_json` with the title as `content` plus one embed per section (`image.url = attachment://sectionN.png`), and the PNGs as `files[N]`. Fallback, only after Discord definitely rejects it (an HTTP status came back): title, then one `###` header + image request per section. A timeout is not re-sent (it may have landed). Caption via `--form-string payload_json=` + `allowed_mentions:{"parse":[]}` — **never** `-F content=` (curl 7.88.1 truncates at `;`, reads a leading `@`/`<` as a file) and **never** `send_message`/`MEDIA:` (batches images away from text).
- Bounded + status-checked (single post `--max-time 20`, fallback posts `--max-time 8`, one 429 retry each; worst case stays under the scheduler's 120 s); any failure → `os._exit(1)` so the scheduler alerts instead of the report vanishing. Clean up a PNG only after its confirmed send.
- Token from `~/.hermes/.env` (not `hermes-agent/.env` → 401); `[SILENT]` first stdout line; curl output captured, never printed; hold an exclusive lock via stdlib `fcntl.flock`; `os._exit(0/1)`. Deliver target `discord:1499972245437550602`, no thread.

## Skill Usage Introspection

For "what skills do I use most / installed-but-unused / skill leaderboard": combine interactive `skill_view` calls (`messages.tool_calls`) with cron skill loads (`~/.hermes/cron/jobs.json`). **Prefilter in SQL** — `WHERE tool_calls LIKE '%skill_view%'` (~14× faster than `IS NOT NULL`). `<available_skills>` lists **category umbrellas**, not sub-skills — glob `skills/*/*/SKILL.md` to enumerate sub-skills. Full pipeline + worked examples: `references/state-db-skill-introspection.md`.

## Provider Cost Comparison

Current provider: **MiniMax-M2.7** ($0.30/M input, $1.20/M output, cache reads ~free; $10/mo flat sub). When comparing providers, deliver **formatted text, not images** (these are analyses, not state.db reports) and use current model pricing. Authoritative tables, recalculation logic, and the multi-provider annual comparison live in `references/minimax-pricing.md`, `references/deepseek-pricing.md`, `references/xiaomi-mimo-pricing.md` (MiniMax performance benchmarks are in `minimax-pricing.md`).

Note: on MiniMax, `cache_read_tokens` can exceed `input_tokens` 5–10× (accumulated context reuse) — do **not** derive a cache-hit rate from `cache_read/input`; use the user's stated rate.

## Common Pitfalls

1. **`strftime` needs `'unixepoch'` + single `%`** — else NULL dates / one collapsed group (`%%` in `"""` strings reaches SQLite literally).
2. **MTD via a Python timestamp**, not SQL `date('now','start of month')` (text vs epoch float → 0 rows).
3. **Flat-array JSON** for the image generator — `{headers, rows}` → garbled output.
4. **`.env` is `~/.hermes/.env`**, not `~/.hermes/hermes-agent/.env` (stale token → 401).
5. **Filter NULL models; guard NULL costs.**
6. **Never inline `MEDIA:`** for paired text+image; send the caption via `--form-string payload_json=` (curl 7.88.1's `-F content=` truncates at `;` and reads a leading `@`/`<` as a file) — see the delivery section.

## Verification Checklist

Before shipping a change to the pipeline, run through the 7-point checklist in `references/2026-07-delivery-hardening.md` (daily window, MTD 6-value row, header==value counts, auto-scale + NULL guards, flat-array JSON, status-checked send + confirmed-then-cleanup, and the cron `[SILENT]`/`os._exit(0/1)`/filelock/deliver contract).

## Support Files

**Scripts**
- `scripts/token_report_no_agent.py` — **production cron script** (self-contained, no LLM): reads token from `~/.hermes/.env`, holds an exclusive `fcntl.flock` lock, queries the DB, renders 3 tables via the shared renderer, posts each section via a status-checked `payload_json`+file multipart (`discord_post`), cleans up a PNG only after its confirmed send, and `os._exit(0` on success `/ 1` on any delivery failure`)`. Deployed byte-identical to `~/.hermes/scripts/` (cron-executed) and here — keep in sync. Image prefixes `daily_`/`mtd_`/`alltime_*.png`.
- `references/gen_token_report_images.py` — JSON→PNG wrapper over `discord-table-format/scripts/render_tables.py`; run via `terminal()` (system PIL); flat-array JSON only.

**References (loaded on demand)**
- `2026-07-delivery-hardening.md` — ⚠️ latest authoritative record: 7-calendar-day window, hardened `discord_post` (timeout/status/429/payload_json/exit-code), dead-script removal, the delivery pattern + verification checklist.
- `2026-06-consolidation-bugfix-perf.md` — two-copy consolidation, 10 bugfixes, cron fix, renderer width guard, performance review.
- `cron-job-configuration.md` — cron fields, why `no_agent: true`, the LLM-mode trap, profile must stay unset.
- `scheduler-no-agent-python-execution.md` — scheduler `sys.executable`/venv path, why PIL needs the dist-packages insert, `[SILENT]` mechanics, token from `.env`.
  Since Hermes v0.22.0 (2026-10-14) the scheduler runs scripts on a Python 3.14 venv where the renderer's tabulate / system Pillow can't load, so the script re-execs itself under `/usr/bin/python3` before printing anything (1.1.3).
- `token-report-double-fire-fix.md` — filelock / `%%` / fmt-scaling / `os._exit` findings (May 2026).
- `token-report-delivery-log.md` — chronological delivery troubleshooting + the relocated 2026-05-11 end-to-end pipeline log.
- `state-db-skill-introspection.md` — skill-usage leaderboard pipeline (interactive + cron), with the LIKE prefilter.
- `minimax-pricing.md` / `deepseek-pricing.md` / `xiaomi-mimo-pricing.md` — provider pricing + MiniMax benchmarks for cost comparisons.
