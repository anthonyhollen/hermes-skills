# Evening Weather Forecast Workflow

> **🔄 As of 2026-05-31 this runs as a `no_agent` cron script: `~/.hermes/scripts/evening_weather.py` (job `c967008ca121`).** No LLM agent; skill not loaded at runtime. The script fetches the 24hr + 2hr forecasts, formats per the spec below, and prints to stdout — cron auto-delivers it to #alerts. **`evening_weather.py` is the source of truth.** The `send_message` / double-delivery notes below are historical (agent-mode) context.

Evening weather-only cron sent to Discord at 10pm SGT every Mon/Tue/Wed (job `c967008ca121`).

## Current Config (as of 2026-09-29)

| Field | Value |
|-------|-------|
| Cron ID | `c967008ca121` |
| Schedule | `0 22 * * 1,2,3` (10pm SGT Mon/Tue/Wed) |
| deliver | `discord:1499972245437550602` — cron system auto-delivers; do NOT call `send_message` |
| Mode | `no_agent` → `~/.hermes/scripts/evening_weather.py` (no LLM; the job's prompt/skill fields are not used) |

## Discord Delivery — Correct Pattern (historical, agent-mode era)

**Use `deliver: "discord:channel"` + no Discord API calls in the prompt.** The cron system auto-delivers the agent's final text to Discord. The agent outputs only the formatted report and terminates — no `send_message`, no `requests.post`, no HTTP status echo.

**⚠️ `send_message` is NOT available in the Discord platform toolset.** Only platform-level actions exist: `search_members`, `fetch_messages`, `create_thread`. Any cron prompt that calls `send_message` will fail with `Unknown action: send_message`.

**Why `deliver=local` alone is insufficient:** If you set `deliver=local` and tell the agent to call `send_message` explicitly, the tool call fails because `send_message` doesn't exist in the `discord` platform toolset. The report is never delivered. Fix: use `deliver=discord:channel` (auto-delivery) and remove all explicit Discord API calls from the prompt.

**Double-delivery still possible** if you have BOTH `deliver=discord:` AND an explicit `send_message`/`requests.post` call in the same session. Prevention: never call Discord APIs inside the prompt when using auto-delivery.

## Message Format (v1.11.0)

```
## 🌙 Evening Weather Update *(Pasir Ris)*

Tomorrow's forecast for your commute (6–8am):
• 6am–Midday (East): <period text> <emoji>
• 24h outlook: <general forecast> <emoji>, <low> – <high> °C
• Wind: <dir> <low> – <high> km/h, Humidity: <low> – <high> %

Next 2 hours at home:
• <start> – <end> SGT: <forecast> <emoji>
```

- **The commute line comes from `periods[]`, not `general`.** At 22:00 the 24hr record's `general` block has `validPeriod` 6 PM today → 6 PM tomorrow, so its forecast is dominated by tomorrow afternoon's storms. `commute_period()` picks the period whose `timePeriod.start` is the first 06:00 SGT **after now** (never by list position — the periods slide with the clock) and prints its `regions.east` text (home is in the east). If no such period exists (e.g. a stale early-morning record) or its east text is empty, the line reads `• 6am–Midday (East): forecast not available yet` — the 24h outlook never stands in for it. If only the `general` block fails, the second bullet reads `• 24h outlook unavailable right now.` User-approved format, 2026-09-29.
- Before v1.11.0 the first bullet printed `general.forecast` under the "(6–8am)" heading: "Thundery Showers" on 28 of 40 archived evenings, while the next morning's 6am–Midday forecast was thundery on 9.
- A 2-hour bulletin whose `valid_period.end` is already past gets ` *(stale bulletin — latest available)*`; if Pasir Ris is missing, the first area is used with ` *(Nearest area: X)*`.

## Emoji Mapping (as implemented in `emoji_for`, priority order)

```
"thunder" / "lightning" / "storm"     → 🌧️
"heavy" + ("rain" | "shower")         → 🌧️   ← checked BEFORE the plain rain branch
"drizzle"                             → 🌦️
"light rain"                          → 🌦️
"rain" / "shower"                     → 🌦️
"cloudy" / "overcast"                 → ☁️   ("partly cloudy" → ⛅)
"partly cloudy" / "sunny spells"      → ⛅
"fair" / "sunny"                      → ☀️, or 🌙 when "(Night)"   (v1.11.0)
default                               → 🌙
```
The header is always 🌙 regardless. (Corrected 2026-09-29: this list used to put "heavy" after "rain" — the code has checked it first since 2026-06-06, which is what makes "Heavy Rain" 🌧️.)

## Bug History

### 2026-05-20 — send_message unavailable → switched to auto-delivery

| # | Symptom | Root Cause | Fix |
|---|---------|------------|-----|
| 1 | Forecast never sent (May 18) | `send_message` tool does not exist in `discord` platform toolset — only `search_members`, `fetch_messages`, `create_thread` exist; tool call failed with `Unknown action: send_message` | Changed `deliver` from `local` to `discord:1499972245437550602`; removed all `send_message` references from prompt |

### 2026-05-13 — All 4 bugs fixed

| # | Symptom | Root Cause | Fix |
|---|---------|------------|-----|
| 1 | Sent twice | `deliver=discord:` + `send_message` both fired | Changed `deliver` to `local` |
| 2 | ☀️ in header | No rule forcing 🌙; "sunny" forecast mapped to ☀️ | Added: "header always 🌙 regardless of forecast" |
| 3 | 🏧 for light rain | "light rain" (two words) not in mapping; agent internal emoji picked 🏧 | Added `"light rain" → 🌦️` with priority note |
| 4 | "Sent to Discord (HTTP 200)" in output | Agent echoed send_message return value in response text | Added: "after send_message, output NOTHING else" |

**Lesson (updated)**: Any cron that (a) has `deliver=discord:` AND an explicit `send_message`/`requests.post` call will double-deliver. But if you use `deliver=local` + `send_message` in the prompt, it silently fails because `send_message` is not available in the Discord toolset. The correct pattern: `deliver=discord:channel` + no Discord API calls in the prompt.
