---
name: sg-commute
description: Singapore parking / car park finder (live HDB, LTA and URA lots + EV chargers near a postal code or address, posted to Discord) plus commute routes, weather/rain/PM2.5 and the morning/evening commute reports.
version: 1.14.0
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [Singapore, Parking, Carpark, EV Charging, Commute, Route Planning, Weather, Traffic, Discord]
    related_skills: [sg-singapore-environment, discord-messaging]
---

# Singapore Commute

## Parking / car park near an address — run ONE command
When the user asks for parking, a car park or EV charging near a Singapore postal code or address, run this (not the `maps` skill, not hand-made API calls):

```bash
PYTHONUNBUFFERED=1 python3 /home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py "<postal or address>" --send
```

- It checks all five systems (HDB live, LTA live, URA + JTC static, EV chargers ≤1 km), renders the table images + map, and **posts the report itself into the conversation you are in** — `--send` with no id resolves the current Discord thread/channel from the Hermes session. Don't pass ids: a *message* id is not a thread id (404). (`--send <channel_id>` / `--thread <thread_id>` only to post somewhere else.)
- **`[SILENT]` + `Sent N/N …` = already posted.** Reply with one short line (or nothing). Never re-post it, never curl Discord yourself, never read the bot token, never use `send_message`/`MEDIA:` for this report.
- `DELIVERY FAILED …` (exit 1) = nothing was posted — follow its hint (e.g. rate-limited → wait a minute) and re-run once. `PARTIAL DELIVERY` = do **not** re-run (it would duplicate); tell the user which part is missing.
- **Answer follow-ups from the `AGENT DIGEST` block** it prints (every shown row: distance, free lots or *static capacity*, live/static, code, address; EV bays). The progress lines (`HDB live feed: 1983 carparks`, `EV: 8 cached … within 5 km`) are feed sizes, not lots or nearby chargers. A `⚠️ … live data unavailable` note means a source failed — say so, don't claim "no parking".
- A carpark **code** (e.g. `HG22`) is not an address, and if a place won't geocode, ask the user for the postal code — never guess one. `--help` shows usage; plain run (no `--send`) prints the report + digest to the terminal only; `--json` = manifest with the rows. Runtime ~2–20 s (a never-seen area does a one-off EV scan).
- Innovis = `2 Fusionopolis Way, Singapore 138634` (use directly, don't geocode). AMK = `12 Ang Mo Kio Street 64, Singapore 569088`. Home = Pasir Ris Grove.

## Scheduled reports — `no_agent` scripts (no LLM; this skill is not loaded for them)
| Job | ID | When (SGT) | Script (`~/.hermes/scripts/`) |
|-----|----|-----------|--------|
| Morning Commute Report | `b617d89da15b` | 07:25 Tue/Wed/Thu (all → Innovis; AMK on file, unscheduled) | `morning_commute.py` |
| Evening Weather Forecast | `c967008ca121` | 22:00 Mon/Tue/Wed | `evening_weather.py` |

Both deliver to #alerts (`deliver: discord:1499972245437550602`). The evening script prints and the scheduler posts. The morning script (no args) posts report + map itself as **one** message and prints `[SILENT]`; if that post fails, it prints the report + `MEDIA:` and the scheduler delivers instead (two messages).
- **To change what a report says, edit the script** — the job's `prompt` is not read for `no_agent` jobs. To change a schedule/target use `hermes cron edit <id> …`; never hand-edit `~/.hermes/cron/jobs.json` (the gateway rewrites it).
- **Test off-schedule without posting:** `morning_commute.py innovis|amk` (a destination argument = print only) and `evening_weather.py`. **Never run `morning_commute.py` with no argument by hand on Tue–Thu: that is cron mode and posts to #alerts.** Outside Tue–Thu it is silent, which is not a failure. To post a test copy, add `--post-to <channel_id>` (e.g. the playground).
- **Destination by day = `SCHEDULE` in `morning_commute.py`** (weekday → `"innovis"`/`"amk"`); the addresses, rain stations and route rules live in `DEST_PROFILES`. Wed moved AMK → Innovis on 2026-10-14; to put AMK back on a day, set that day to `"amk"` in `SCHEDULE` (a day outside Tue–Thu also needs `hermes cron edit b617d89da15b --schedule …`). Before re-sending a "missing" report, check `~/.hermes/cron/output/<job_id>/` for a fresh run (double-delivery).
- Formats and per-section logic: `references/commute-report.md` (morning) and `references/evening-weather-workflow.md` (evening).

## Still-live rules (details in the references)
- **Innovis route:** forced onto ECP→MCE with two mainline `via:` points (`INNOVIS_VIA` in `morning_commute.py`), a `route_has_detour()` guard and distance floors (Innovis ≥25 km, AMK ≥8 km). Never a single or address-based waypoint; re-derive coords with `~/.hermes/scripts/verify_ecp_route.py`. AMK (when scheduled) is routed direct.
- **Static maps:** `format=png32` (PNG8 drops the polyline in Discord); each side ≤640 px (Google clamps larger); traffic colour from the same Directions response (🟢 <1.10 / 🟡 1.10–1.35 / 🔴 ≥1.35 / 🔵 no data).
- **data.gov.sg:** use `curl`, send `DATAGOV_API_KEY` as `x-api-key` (to data.gov.sg only) — "section unavailable" is usually a 429 whose `data:null` body looks empty. The rainfall value is a **5-minute** total. `urllib` gets 403 from data.gov.sg / OneMap / LTA.
- **Keys** live in `~/.hermes/.env` (`GOOGLE_API_KEY`, `DATAGOV_API_KEY`, `LTA_DATAMALL_KEY`, `DISCORD_BOT_TOKEN`) — never hardcode, print or paste one.
- **Test images in #playground** (`1501742338639401071`) before changing map/table rendering.

## References
| File | Read when |
|------|-----------|
| `references/carpark-availability.md` | Changing the parking tool: the five systems, data files, output format (user-specified column order), verified results |
| `references/ev-charging.md` | EV API shape, the postal-sector discovery scan, the location cache/anchors, `--refresh` |
| `references/discord-image-delivery.md` | How `--send` posts (header+image pairs, target resolution, nonce retries, exit contract) |
| `references/commute-report.md` | The morning report: sections, format, route/waypoint logic |
| `references/evening-weather-workflow.md` | The evening report: format, commute-period selection, emoji |
| `references/weather-api-response-shapes.md` | Verified data.gov.sg v2 shapes (24hr, 2hr, rainfall, flood-alerts, PM2.5) |
| `references/google-routes.md` | Ad-hoc route work: Directions vs Routes v2, polyline, Static Maps |
| `references/traffic-duration-troubleshooting.md` | `duration_in_traffic` missing / blue polyline |
| `references/developer-guide.md` | Maintenance internals (APIs, field paths, algorithms, pitfalls) — the long form of `CLAUDE.md` |
| `references/changelog.md` | Full dated history (why things are the way they are) — read before changing route/waypoint/delivery/EV-scan logic |
| `references/geocoding-troubleshooting.md`, `lta-carpark-availability.md`, `jtc-carpark-info.md`, `ura-carpark-geojson.md`, `api-response-corrections.md` | Source-specific notes |
| `references/traffic-images-api.md`, `commute-camera-filtering.md` | Traffic cameras along a route. Historical only: the traffic-camera skill was **retired 2026-10-29** (the provider shut down most cameras); don't build on these |
| `references/static-map-discord-rendering.md`, `traffic-colour-update-2026-05-28.md`, `cron-prompt-delivery-patterns.md`, `silent-delivery-failure.md`, `run-deviation-diagnosis.md` | Historical diagnoses (mostly from the agent-mode era before 2026-05-31) |
