# sg-commute — Developer Guide (maintenance internals)

> Relocated from `CLAUDE.md` on 2026-09-29 (v1.11.0). `CLAUDE.md` is auto-attached to the agent's context on every run of a script in this dir (Hermes "subdirectory context"), i.e. on every parking query, so it is now a short pointer and the long form lives here. Corrected in the same change: geocoding order, `departure_time`, how to edit cron jobs, report formats, runtimes, and the carpark/EV behaviour of v1.11.0.

Singapore commute planner delivering a daily morning report and evening weather forecast to Discord. Covers routes, traffic, weather, rain, PM2.5, and carpark/EV availability.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/nearest_carpark.py` | Interactive carpark + EV finder (system `/usr/bin/python3`). Run: `PYTHONUNBUFFERED=1 python3 .../nearest_carpark.py "<postal or address>" --send` · also `--json`, `--help`, and the monthly `--refresh` (system crontab `0 4 1 * *`, log `data/ev_refresh.log`) |
| `~/.hermes/scripts/morning_commute.py` | Morning report — the `no_agent` cron `b617d89da15b` (gateway venv Python) |
| `~/.hermes/scripts/evening_weather.py` | Evening report — the `no_agent` cron `c967008ca121` (gateway venv Python) |
| `~/.hermes/scripts/verify_ecp_route.py` | Manual helper: derive/verify the Innovis `INNOVIS_VIA` points |
| `data/ev_locations.csv`, `data/ev_anchors.csv` | EV location cache + discovered areas (written atomically under `data/.ev_cache.lock`) |
| `data/carpark_info.csv` | HDB carpark coordinates — SVY21 x/y |
| `data/ura_carpark_info.csv` | URA static carpark data — WGS84, lowercase field names |
| `data/jtc_carpark_info.csv` | JTC industrial carparks — SVY21 |
| `data/uraparking.geojson` | URA GeoJSON source (777 features) |

## Shared Locations

| Label | Address | WGS84 | SVY21 |
|-------|---------|-------|-------|
| Home | Pasir Ris Grove, Singapore 518141 | 1.37290, 103.95060 | — |
| Innovis | 2 Fusionopolis Way, Singapore 138634 | 1.29834, 103.78972 | 23148.2, 31188.9 |
| AMK | 12 Ang Mo Kio Street 64, Singapore 569088 | 1.39037, 103.84650 | 29466.6, 41365.8 |

**Innovis shortcut:** Never geocode — use address `2 Fusionopolis Way, Singapore 138634` directly.

## Cron Jobs

| Job | ID | Schedule | Deliver | Type |
|-----|----|----------|---------|------|
| Morning Commute Report | `b617d89da15b` | 07:25 SGT Tue/Wed/Thu | `discord:1499972245437550602` | `no_agent` → `~/.hermes/scripts/morning_commute.py` |
| Evening Weather Forecast | `c967008ca121` | 22:00 SGT Mon/Tue/Wed | `discord:1499972245437550602` | `no_agent` → `~/.hermes/scripts/evening_weather.py` |

**As of 2026-05-31 both jobs run as `no_agent` scripts (no LLM, skill not loaded at runtime).** The scripts live in **`~/.hermes/scripts/`** (the cron scripts dir — NOT the skill's `scripts/`, which holds only `nearest_carpark.py`); the scheduler runs them with the gateway venv interpreter. The evening script prints the report and cron delivers it to #alerts. The morning script self-posts text + map as one message and prints `[SILENT]` (v1.13.0; see *Crons* below). The notes below about agent output/`send_message` apply only to manual runs.

**Destination by day:** Tuesday / Wednesday / Thursday → Innovis (Wednesday was AMK until 2026-10-14). Set in `SCHEDULE` (weekday → slug) in `morning_commute.py`; the per-destination data (label, address, rain station) is in `DEST_PROFILES`, where AMK stays on file — restore it to a day with e.g. `2: "amk"` (a day outside Tue–Thu also needs the cron schedule extended).

**Day-of-week is derived from the system clock** (`datetime.now().weekday()` → `SCHEDULE` in `morning_commute.py`); for manual runs pass `innovis`/`amk` as an argument.

## APIs

| API | Endpoint | Auth |
|-----|----------|------|
| Google Directions (preferred) | `https://maps.googleapis.com/maps/api/directions/json` | `key=${GOOGLE_API_KEY}` |
| Google Routes v2 (ad-hoc only — the scripts don't use it) | `https://routes.googleapis.com/directions/v2:computeRoutes` | Header: `X-Goog-Api-Key` |
| Google Static Maps | `https://maps.googleapis.com/maps/api/staticmap` | `key=...` |
| Google Geocoding | `https://maps.googleapis.com/maps/api/geocode/json` | `key=...` |
| OneMap (postal only) | `https://www.onemap.gov.sg/api/common/elastic/search` | None (postal search) |
| HDB Carpark Availability | `https://api.data.gov.sg/v1/transport/carpark-availability` | None |
| LTA CarPark Availability | `https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2` | `AccountKey: ${LTA_DATAMALL_KEY}` |
| LTA EV Charging Points | `https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode=<P>` | `AccountKey: ${LTA_DATAMALL_KEY}` |
| Weather / Rain / Flood / PM2.5 | `https://api-open.data.gov.sg/v2/real-time/api/*` | `x-api-key: ${DATAGOV_API_KEY}` (lifts the ~5 req/10 s anonymous limit; send it to data.gov.sg only) |

**API keys load from `~/.hermes/.env`, never hardcoded.** `morning_commute.py` reads `GOOGLE_API_KEY` + `DATAGOV_API_KEY`; `nearest_carpark.py` reads `GOOGLE_API_KEY` + `LTA_DATAMALL_KEY` (`_load_env_key`/`_env_val`, which fall back to parsing `~/.hermes/.env`; since Hermes 0.2x the scheduler also loads `.env` before a no_agent job, and the terminal tool strips Hermes secrets from the agent's subprocess env, so keep the file fallback). Never paste a literal key into a script or a doc — it leaks into the skill files and `.backups/`.

**CRITICAL: Never use `urllib.request` for data.gov.sg, LTA, or OneMap** — all return HTTP 403 silently. Always use `curl` via `subprocess.run(["curl", "-s", url], capture_output=True, text=True)`.

**No `&` background in `execute_code`** — raises "Foreground command uses '&' backgrounding" error. Use `subprocess.run()` sequentially or `terminal(background=true)`.

## Route Computation

Use **Google Directions API** (preferred) — supports waypoints, returns `legs[].duration.value` (seconds int) and `overview_polyline.points`.

### Waypoint rules

| Destination | Waypoint | Expected distance |
|-------------|----------|------------------|
| Innovis (Tue/Wed/Thu) | `via:1.30860,103.93347\|via:1.27616,103.86845` (two mainline via-points) | ~35 km |
| AMK (on file; unscheduled since 2026-10-14) | none — direct route only | 13–15 km |

**Never use address-based waypoints for Innovis** — they cause two distinct failures:
1. STOP waypoints (e.g. `ECP+near+Marina+Bay+Singapore|1+Ayer+Rajah+Ave+Singapore`) truncate the polyline at the first stop — Innovis is never reached.
2. Text `via:` waypoints (e.g. `East+Coast+Parkway+Singapore`) geocode near Marine Parade surface streets, causing a ~1.6km detour through Marine Parade Road.

A **single** coordinate `via:` near the ECP↔MCE interchange also fails — Google snaps it to the parallel East Coast Park Service Rd and routes off ECP (exit 13 → Marina East Dr → U-turn → MCE). Use **two** coordinate `via:` points that *bracket* the interchange — `INNOVIS_VIA = "via:1.30860,103.93347|via:1.27616,103.86845"` (one on the ECP mainline east of the junction, one on the MCE mainline west of it) — for a clean TPE→PIE→ECP→exit 14B→MCE→AYE route (~35 km). The coords are **derived from a real route's step geometry** (`verify_ecp_route.py`), so they land on the mainline, not the service road. `section5` runs a `route_has_detour()` guard that auto-falls back to the no-waypoint route (and flags it) if the forced route ever weaves. See `references/changelog.md` 2026-07-02.

**Validate distance after every computation:**

| Destination | Minimum valid distance |
|-------------|----------------------|
| Innovis | > 25 km |
| AMK | > 8 km |

If below minimum: retry without waypoints as fallback. If fallback also fails: append `*(route may be inaccurate — verify manually)*` to Section 5.

### Duration parsing

```python
# Directions API — legs[].duration.value is seconds int
dur_min = sum(leg["duration"]["value"] for leg in legs) // 60

# Routes API v2 — duration is "1866s" string
dur_min = int(route["duration"].rstrip("s")) // 60
```

**`duration_in_traffic` is often absent at 6:30am** — Google traffic data is limited before morning peak. Accept normal duration when unavailable. Use `departure_time=now` (what the script sends) or an integer Unix timestamp — **not** ISO 8601, which is the Routes v2 format — plus `&traffic_model=best_guess`. (Corrected 2026-09-29: a Unix-timestamp Directions call returned `OK` with `duration_in_traffic`; the old "Unix → INVALID_REQUEST" note was wrong.)

### Static Map — always PNG32

```
https://maps.googleapis.com/maps/api/staticmap
?size=640x450&scale=2&format=png32
&markers=color:green|label:H|Pasir Ris Grove Singapore
&markers=color:red|label:W|<destination>
&path=color:<traffic_color>|weight:7|enc:<encoded_polyline>
&key=${GOOGLE_API_KEY}
```

**Size ≤640 px per side** (Google silently clamps larger — the old `700x450` always came back 640 wide). **`format=png32` is mandatory.** Default PNG8 (256-color palette) causes Discord to strip or render the blue polyline invisibly after re-encoding. PNG32 forces full RGB; file size >500KB confirms it was returned.

**Polyline colour is traffic-based** (weight: `7`):
| Colour | Hex | Condition |
|--------|-----|-----------|
| 🔵 Blue | `0x1565C0` | No traffic data |
| 🟢 Green | `0x00C853` | Light (ratio < 1.10) |
| 🟡 Amber | `0xFFAB00` | Moderate (ratio 1.10–1.35) |
| 🔴 Red | `0xD50000` | Heavy (ratio ≥ 1.35) |

`ratio = traffic_duration_secs / normal_duration_secs`

```python
def traffic_color(normal_secs, traffic_secs):
    if not traffic_secs or normal_secs == 0:
        return "0x1565C0"  # blue — no traffic data
    ratio = traffic_secs / normal_secs
    if ratio < 1.10:   return "0x00C853"  # green
    elif ratio < 1.35: return "0xFFAB00"  # amber
    else:              return "0xD50000"  # red

polyline_color = traffic_color(normal_secs, traffic_secs)
# use: f"path=color:{polyline_color}|weight:7|enc:{polyline_enc}"
```

## Environmental Data (Weather / Rain / PM2.5)

All v2 API responses are wrapped in a `data` key. Use `curl` not `urllib.request`.

### 24hr Forecast — corrected field paths

```python
# Endpoint: https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast
general = response["data"]["records"][0]["general"]
forecast_text  = general["forecast"]["text"]
low_temp       = general["temperature"]["low"]
high_temp      = general["temperature"]["high"]
low_humid      = general["relativeHumidity"]["low"]
high_humid     = general["relativeHumidity"]["high"]
wind_dir       = general["wind"]["direction"]
low_wind       = general["wind"]["speed"]["low"]
high_wind      = general["wind"]["speed"]["high"]

periods = response["data"]["records"][0]["periods"]
# Each period: period["regions"]["east"]["text"], period["timePeriod"]["start/end"]
```

Period index mapping (at the 07:25 cron time): `0` = 06:00–12:00, `1` = 12:00–18:00, `2` = 18:00–06:00 next day. The periods are anchored to the 06:00/12:00/18:00 boundaries and slide with the clock (`period[0]` is the *current* block), so index→wall-clock is NOT fixed across run times. **Do not hardcode period labels** — `morning_commute.py` `section1()` derives each line's label from that period's own `timePeriod.text` (`_period_label`), which is why it is correct at 07:25 (`6am–Midday / Midday–6pm / 6pm–6am (tonight)`) *and* in the evening. Exactly 3 periods exist (no `periods[3]`); the report shows those 3 real periods rather than proxying a 4th "tomorrow" line. (A prior hardcoded label list shifted all three by one every morning — see `references/weather-api-response-shapes.md` correction 2026-07-12.)

### 2hr Forecast

```python
# Endpoint: https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast
item = response["data"]["items"][0]
valid_period = item["valid_period"]   # key is valid_period (underscore, not camelCase)
forecasts = item["forecasts"]         # list of {"area": "Pasir Ris", "forecast": "..."}
```

### Rainfall

```python
# Endpoint: https://api-open.data.gov.sg/v2/real-time/api/rainfall
rain_data = response["data"]
stations = rain_data["stations"]
reading_list = rain_data["readings"][0]["data"]   # readings is a list-of-one, not flat
# Each entry: {"stationId": "S71", "value": 0.0}
s71 = next((d for d in reading_list if d["stationId"] == "S71"), None)
```

**Rain station by destination:** Innovis (Tue/Wed/Thu) → S71 (Kent Ridge Rd, 1.1km), AMK (when scheduled) → S109 (Ang Mo Kio Ave 5, 1.3km).

**The value is a 5-MINUTE total** (`data.readingType` = "TB1 Rainfall 5 Minute Total F"). `morning_commute.py` converts ×12 to mm/h and classifies <2.5 light / <7.6 moderate / ≥7.6 heavy (v1.11.0 — the old bands were applied to the raw 5-min value and under-reported ~12×).

### Flood alerts

`data.records` is a ~48-minute history of 2-minute snapshots (newest first, with a `paginationToken`); each is `{"datetime", "item": {"type": "observation", "readings": [...], "isStationData"}}`. Only the NEWEST snapshot says what is in effect now — see `weather-api-response-shapes.md`.

### PM2.5

```python
# Endpoint: https://api-open.data.gov.sg/v2/real-time/api/pm25
readings = response["data"]["items"][0]["readings"]["pm25_one_hourly"]
# readings is a dict: {"east": 17, "south": 10, "west": 9, "north": 6, "central": 10}
east_pm25 = readings["east"]
avg_pm25  = round(sum(readings.values()) / len(readings))
```

**PM2.5 scale** (the user's chosen bands, implemented in `morning_commute.py` `pm_level`; the report's legend lists all five since v1.11.0, each with its own emoji since v1.12.1 — the cron prompt is not read):
```
0–9 µg/m³:   Good 🟢
10–19 µg/m³: Moderate 🟡
20–49 µg/m³: Unhealthy (Sensitive) 🟠
50–99 µg/m³: Unhealthy 🔴
100+ µg/m³:  Very Unhealthy 🟣
```

## Geocoding

Priority order (as implemented in `geocode()`):
1. **6-digit postal → OneMap first** — free, no auth; building-level ADDRESS + native SVY21 `X`/`Y`. Google is the fallback.
2. **Street address → Google Geocoding** — restricted with `components=country:SG`; returns `loc["lng"]` (not `"lon"`).
Any result outside Singapore's bounding box (`SG_BBOX`) is rejected (v1.11.0 — an unrestricted lookup of "HG22" matched Harrogate, UK).

SVY21 ↔ WGS84 conversion via pyproj (`always_xy=True` means pass `lon, lat` order):

```python
from pyproj import Transformer
_fwd = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
_inv = Transformer.from_crs("EPSG:4326", "EPSG:3414", always_xy=True)

def svy21_to_wgs84(x, y): lon, lat = _fwd.transform(x, y); return lat, lon
def wgs84_to_svy21(lat, lon): x, y = _inv.transform(lon, lat); return x, y
```

**Never auto-derive postal codes** — if Google returns no postal, ask the user.

## Carpark Systems

| System | Coverage | Live? | Coordinates | Filter |
|--------|----------|-------|-------------|--------|
| HDB | HDB estates | Yes | SVY21 in CSV | `lot_type == "C"` only |
| LTA | Malls/commercial (Agency LTA), URA live lots (Agency URA), and ~2,000 HDB carparks (Agency HDB) | Yes | WGS84 `"lat lon"` string | `LotType == "C"` only |
| URA | URA-managed | No (static) | WGS84 in CSV | `no_car > 0` only |
| JTC | Industrial estates | No (static) | SVY21 in CSV | All car-only |

**Car lots only — always filter.** All systems contain H (heavy vehicle) and Y (motorcycle) rows. Skip them. LTA may have the same `CarParkID` appearing multiple times for the same `LotType` — sum `AvailableLots` per ID.

**v1.11.0 feed handling:**
- **HDB:** the `C` entry can be anywhere in `carpark_info` (15 carparks list `H`/`Y` first) — search for it. A sensor whose `update_datetime` is >48 h old shows Live `N` (the timestamp only moves when the count changes, so keep the threshold generous).
- **LTA:** DataMall returns ≤500 rows per call — `lta_live()` pages `$skip=0,500,…` concurrently (the feed is ~2,600 rows / 6 pages; page 0 holds all LTA + URA rows today, pages 1+ are HDB). An empty/failed page 0 = feed unavailable; a failed or short page with data after it = PARTIAL (rows kept, "may be incomplete" note, progress line `LTA live feed: PARTIAL — …`). HDB-agency rows the HDB feed can place are dropped (the HDB feed has totals); they are kept, labelled HDB, only when the HDB feed failed or lacks that code.
- **Outage ≠ "no parking":** `hdb_live()`/`lta_live()` return `(dict, ok)`; a failed feed or a body without `items`/`value` (a 429/401 parses as JSON too) sets `ok=False`, and the PARKING section then says `*⚠️ <src> live data unavailable right now — this list may be incomplete.*` instead of the radius note.

**Deduplication:** If the same carpark code appears in both URA (static) and LTA (live), drop the URA entry — live data takes priority.

### HDB API response shape (corrected — spec is wrong)

```python
# carpark_info is nested, not flat; lot counts are strings
for item in response["items"]:
    for cp in item["carpark_data"]:
        for info in cp["carpark_info"]:
            if info["lot_type"] != "C": continue
            lots = int(info["lots_available"])
            total = int(info["total_lots"])
```

### URA CSV field names

CSV uses **lowercase** names: `pp_code`, `parking_pl`, `no_car`, `no_h_vehicle`, `no_mcycle`, `longitude`, `latitude`. The GeoJSON source uses uppercase (`PP_CODE`, `NO_CAR`, etc.) — do not confuse them.

### LTA location parsing

```python
loc = row["Location"]     # "1.29115 103.85728"
lat, lon = map(float, loc.split())
```

## EV Charging

Endpoint: `https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode=<P>`

**Bay count formula** — `len(plugTypes)` is wrong; one plugType can have multiple bay IDs:

```python
for cp in loc["chargingPoints"]:
    for pt in cp["plugTypes"]:
        total_bays += len(pt.get("evIds", []))
        available  += sum(1 for ev in pt.get("evIds", []) if str(ev.get("status","")) == "1")
```

A status that is neither `"1"` nor `"0"` (e.g. `""`, seen live 2026-09-29) makes the location's availability unknown → `?/N` (v1.11.0).

**Failure handling (v1.11.0):** `ev_by_postal()` returns `None` when the FETCH failed (vs `[]` = no charger there), so `_scan_postals` counts failures; a discovery scan with >20% failed probes (`EV_SCAN_MAX_FAIL`) does **not** record an anchor (the area is re-scanned next time instead of serving "no chargers" for a month) and the EV section says data is unavailable. Cache writes are atomic (temp + `os.replace`) and the read-merge-write runs under an `flock` on `data/.ev_cache.lock`, re-reading the files inside the lock (`_commit_ev`); the ~300-request scan itself runs outside the lock. `--refresh` prints a start timestamp, merges once at the end, and exits 1 if any anchor scan was unreliable.

**`evIds[]` availability is live-only; the cache stores 0 as a placeholder.** `evId` itself is often `null` in the response but each entry's `status` ("1"/"0") is populated — that's what the count reads (verified live). Cached locations load `available=0` + `available_live=False`; `refresh_ev_live()` fills the real count and sets `available_live=True` for the ≤3 displayed chargers. The bays cell shows `avail/total` **only** when a live refresh confirmed it — otherwise `?/total` (bays known, availability unconfirmed), so an unconfirmed placeholder never reads as a genuinely-full `0/total`. (v1.9.9.)

**Multi-postal scan** — the API returns only exact postal matches, not a radius. `_narrow_variants` sweeps the 2-digit postal **sector** (first two digits, ±1) × the 4-digit **delivery point** (last four, ±50) ≈ 303 candidates:

```python
pi = int(postal)
sector = pi // 10000        # first TWO digits (postal sector → district)
dp     = pi % 10000         # last four digits (delivery point)
for so in (-1, 0, 1):       # neighbouring sectors
    s = sector + so
    if not (0 <= s <= 99): continue
    for do in range(-50, 51):
        td = dp + do
        if 0 <= td <= 9999:
            variants.add(f"{s:02d}{td:04d}")
```

The old single-digit split (`pi // 100000`) probed scattered sectors (03/13/23 for 138634) and missed the true sector-13 neighbours — fixed to the 2-digit sector.

**Deduplicate** by `(round(lat,5), round(lon,5))` — same location appears across multiple postals.

**EV marker overlap with parking markers:** If an EV point lands within 0.0001° of a parking marker coordinate, apply a ~4m NE offset (`+0.00004° lat, +0.00004° lon`) before adding to the Static Map marker list. This prevents Google from rendering only one of two stacked markers.

**1 km display radius cap (`MAX_RADIUS_M = 1000`):** every `nearest_*` helper (`nearest_hdb/lta/ura/jtc/ev`) filters candidates to `d <= MAX_RADIUS_M` *before* ranking, so a parking lot or EV charger farther than 1 km from the destination is never shown — not even to fill an otherwise-empty top-3 slot. Distances are metres in both metrics (SVY21-euclidean for HDB/JTC, haversine for LTA/URA/EV), so the single cap applies to every source. This is separate from `CACHE_QUERY_RADIUS = 5000` / `ANCHOR_COVER_RADIUS = 1000`, which govern EV *cache* retrieval/reuse breadth (intentionally wider); the 1 km filter is the authoritative display limit downstream. When a section has <3 results within range, `_radius_note()` adds a body note under the header (`*No other parking/EV charging within 1km radius.*`; `*No …*` when 0 — the empty section is now emitted with the note instead of being dropped). Each source caps its own candidates at 3 within the radius, so a combined total <3 genuinely means that few exist within 1 km. Fixes a report where postal 569088 (Ang Mo Kio) surfaced an EV "Spot C" 4.4 km away in Hougang. (v1.10.0.)

## Discord Delivery Rules

**Crons:** both use `deliver: "discord:1499972245437550602"`. `evening_weather.py` prints the report to stdout and the scheduler posts it. `morning_commute.py` (no args = cron mode) posts the report + route map to #alerts itself as **one** Discord message, then prints `[SILENT]` (+ the report, kept in the cron archive) so the scheduler delivers nothing. The scheduler can't combine them: it sends the text, then each `MEDIA:` file as a separate message (`_send_media_via_adapter`). If the self-post fails it falls back to printing the report + `MEDIA:` for the scheduler (two messages, not lost). Non-zero exit → the scheduler posts a failure alert; empty stdout or a `[SILENT]` first line → nothing delivered. A cron script that calls Discord itself **must** print `[SILENT]` first on success, or the scheduler double-delivers. The scheduler does no magic-byte check on `MEDIA:` files, so the script validates the PNG.

**Interactive parking reports:** `nearest_carpark.py … --send` is the only delivery path — see `references/discord-image-delivery.md` (target resolution from `HERMES_SESSION_KEY`, nonce-safe retries, the `[SILENT]` / `DELIVERY FAILED` / `PARTIAL DELIVERY` contract). The agent must not hand-post with curl or read the bot token (a 2026-07-16 session did, printing the token into the session log).

**Historical (agent-mode era, before 2026-05-31):** `send_message` was not available in the Discord platform toolset (`Unknown action: send_message`); `deliver: "discord:…"` plus an explicit Discord call in the prompt double-delivered.

**Before manual re-sending after a missing report:** check `~/.hermes/cron/output/<job_id>/` for a fresh timestamp. If a new output file appeared, the cron already delivered — do not re-send.

**Triggering the job runs it on the next tick — and the morning script is SILENT off-schedule.** A manual trigger (`trigger_job`) sets `next_run_at=now`; the scheduler runs the script on its next tick and updates `last_run_at` + writes an output doc. `morning_commute.py` only reports Tue/Wed/Thu — triggered on any other day with no destination override it returns silently (`Status: silent (empty output)`). To see a report off-schedule, force a destination: `morning_commute.py innovis|amk` or `DEST=innovis`.

## Morning Report Format (as printed by `morning_commute.py`, v1.11.0)

```
## ☀️ Morning Commute Update

**1. Today's Weather** - Forecast: *<forecast>*, Temp: *<low>–<high>°C*, Humidity: *<low>–<high>%*, Wind: *<dir> <low>–<high> km/h*
• 6am–Midday: <text> <emoji>          ← one line per 24hr period, labelled from each period's own
• Midday–6pm: <text> <emoji>             timePeriod (07:25 order shown)
• 6pm–6am (tonight): <text> <emoji>

**2. Next 2 Hours Weather** *(Pasir Ris — Home)*
• Valid period: <HHMM> – <HHMM> SGT
• Pasir Ris: <forecast> <emoji>[ *(nearest area: X)*][ *(stale bulletin — latest available)*]

**3. Rain Check near <station name>** - *S71 - Kent Ridge Rd (1.1km away): 1.2mm in last 5 min (~14 mm/h) - heavy rain 🌧️*
• No flood alerts currently in effect      | ⚠️ Flood alert in effect (N active readings) | ⚠️ Flood alert reported in the last ~45 min (…may have cleared) | Flood alert status unavailable

**4. Air Quality** - East region: *<v> µg/m³ (<level> <emoji>)*, Regional avg: *<avg> µg/m³ (<level> <emoji>)*
• WHO PM2.5 scale: 🟢 0–9 Good / 🟡 10–19 Moderate / 🟠 20–49 Unhealthy (Sensitive) / 🔴 50–99 Unhealthy / 🟣 100+ Very Unhealthy

**5. Commute to <DEST>** *(<address>)*
• Distance: <X.X> km[ *(ECP route unavailable — showing fastest route)*][ *(route may be inaccurate — verify manually)*]
• Normal: ~<YY> min
• Current: ~<ZZ> min (traffic-aware) <🟢|🟡|🔴>      (or "*(traffic data unavailable)*")

*(Map polyline: 🟢 light / 🟡 moderate / 🔴 heavy / 🔵 no data)*
MEDIA:/tmp/route_<innovis|amk>.png                   (print-only/fallback; cron mode attaches it to the same message)
```
A section that fails renders `**N. …** - *unavailable (<short reason>)*` — the reason goes through `_safe_err` (exception text can contain the key-bearing request URL). Directions retries only a transient status (`OVER_QUERY_LIMIT`/`UNKNOWN_ERROR`/no response); a definitive error shows as e.g. `route unavailable (no route available — Directions REQUEST_DENIED)`. Rain bands apply only when `readingType` says "5 Minute"; the flood line says "status unavailable" on a failed/empty/odd-shaped payload.

## Evening Report Format (as printed by `evening_weather.py`, v1.11.0)

```
## 🌙 Evening Weather Update *(Pasir Ris)*

Tomorrow's forecast for your commute (6–8am):
• 6am–Midday (East): <period text> <emoji>          ← the 24hr period whose timePeriod.start is the first 06:00 SGT after now
                                                       ("forecast not available yet" if there is none)
• 24h outlook: <general forecast> <emoji>, <low> – <high> °C
• Wind: <dir> <low> – <high> km/h, Humidity: <low> – <high> %

Next 2 hours at home:
• <start> – <end> SGT: <forecast> <emoji>[ *(Nearest area: X)*][ *(stale bulletin — latest available)*]
```
Header is always `## 🌙 Evening Weather Update` — never ☀️. (Before v1.11.0 the first bullet printed `general.forecast`, whose validPeriod is 6 PM today → 6 PM tomorrow, as the 6–8am forecast: it said "Thundery Showers" on 28 of 40 archived evenings.)

**Emoji mapping, evening `emoji_for` (priority order):** thunder/lightning/storm → 🌧️; "heavy"+rain/shower → 🌧️; drizzle → 🌦️; "light rain" → 🌦️; rain/shower → 🌦️; cloudy/overcast → ☁️ (partly cloudy → ⛅); partly cloudy / sunny spells → ⛅; fair/sunny → ☀️, or 🌙 if "(Night)"; default → 🌙. `morning_commute.py`'s `wx_emoji()` reimplements it inline (default ⛅) — keep the two in sync.

## Changing the cron jobs

**Use `hermes cron edit <job_id> …`** (`--schedule`, `--deliver`, `--script`, `--failure-deliver`, `--clear-skills`, …) — never hand-edit `~/.hermes/cron/jobs.json`: the gateway rewrites that file when it persists run state, so a hand edit races the scheduler and can be clobbered. **To change what a report says, edit the script** — `no_agent` jobs never read their `prompt`. (This section used to prescribe a Python `json.load`/`json.dump` edit of `jobs.json`; superseded 2026-09-29.)

## Running nearest_carpark.py

```bash
PYTHONUNBUFFERED=1 python3 /home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py "238879"
PYTHONUNBUFFERED=1 python3 /home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py "2 Fusionopolis Way Singapore"
```

`PYTHONUNBUFFERED=1` keeps the progress lines live. A warm query takes ~2 s; a never-seen area adds a one-off ~300-postal EV scan (a few seconds). (The old "~90 s" dated from before the 2026-06-06 EV-scan rewrite.) Output ends with an `AGENT DIGEST` block (the shown rows as text — agent-only, never posted) and, with `--send`, the delivery summary.

## Common Pitfalls

| Trap | Symptom | Fix |
|------|---------|-----|
| `urllib.request` for data.gov.sg/LTA/OneMap | Silent HTTP 403 | Use `subprocess.run(["curl", ...])` |
| `&` background in `execute_code` | "Foreground command uses '&'" error | Use `subprocess.run()` sequentially |
| `format=png` (default) for Static Maps | Polyline invisible in Discord | Always use `format=png32` |
| Address/text/single-coord waypoints for Innovis | Polyline truncates, Marine Parade detour, OR ECP↔MCE U-turn | Use the two mainline via-points (`INNOVIS_VIA`) — see Route Computation |
| Waypoints for AMK | 60km via-city route | No waypoints — direct route only |
| Directions API `departure_time` as ISO 8601 | Not the Directions format (that's Routes v2) | Use `now` or an integer Unix timestamp (both verified OK 2026-09-29) |
| HDB `lot_type` filtering missing | Heavy-vehicle/motorcycle lots counted | Filter `lot_type == "C"` only |
| URA CSV uppercase field names | `KeyError` | CSV uses lowercase: `pp_code`, `no_car` |
| LTA same CarParkID multiple rows | Overcounted availability | Sum `AvailableLots` per `CarParkID` |
| EV `len(plugTypes)` for bay count | Wrong count (e.g. 2/1) | `sum(len(pt["evIds"]) for pt in plugTypes)` |
| EV single-postal search | Misses most nearby chargers | 2-digit sector±1 × delivery-point±50 scan (`_narrow_variants`) |
| Parking/EV ranked with no distance cap | Far lot/charger fills a top-3 slot (4.4 km "Spot C" for 569088) | `MAX_RADIUS_M = 1000` filter in every `nearest_*` |
| PM2.5 `readings` not a flat list | `TypeError` | `readings["pm25_one_hourly"]` is a dict |
| Rainfall `readings[0]["stationId"]` | `KeyError` | Readings is `[{"timestamp", "data":[...]}]` — index `[0]["data"]` |
| `two_hr["items"][0]["validPeriod"]` | `KeyError` | Key is `valid_period` (underscore) |
| Google geocode `loc["lon"]` | `KeyError` | Google uses `"lng"` not `"lon"` |
| `send_message` in Discord cron prompt | `Unknown action: send_message` | Use `deliver: "discord:channel"` — auto-delivery |
| `deliver=discord:` + explicit Discord API call | Double message | Remove API calls; let auto-delivery handle it |
| Triggering the morning job off-schedule shows nothing | `Status: silent (empty output)`, no delivery | Non-commute day (runs Tue/Wed/Thu only) — pass a destination: `morning_commute.py innovis\|amk` or `DEST=` |
| Hand-editing `jobs.json` (any tool) | Clobbered by the gateway / corrupted JSON | `hermes cron edit <id> …` |
| Editing the cron prompt (or a reference doc) to change report text | No-op — `no_agent` jobs don't read the prompt | Edit `~/.hermes/scripts/<script>.py` |
| HDB `carpark_info[0]` assumed to be the C entry | 15 carparks silently missing | Search `carpark_info` for `lot_type == "C"` |
| One LTA DataMall call | Only the first 500 of ~2,600 rows | Page with `$skip` |
| Rainfall value read as hourly | "light rain" for a heavy downpour | It's a 5-minute total — ×12 for mm/h |
| Counting flood-alert snapshots | One alert ×25, cleared alerts "in effect" | Newest snapshot's `readings` only |
