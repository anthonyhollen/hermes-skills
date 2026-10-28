> **Current behaviour (v1.11.0, 2026-09-29) — read with the rest of this file:**
> - Run `nearest_carpark.py "<postal or address>" --send` (posts into the current Discord conversation), plain (terminal), `--json`, `--help`, or `--refresh` (monthly). See `discord-image-delivery.md` for `--send`.
> - Every mode prints an **`AGENT DIGEST`** — the shown rows as text (distance, `N/total lots free (live)` or `capacity N (static — no live count)`, code, system, address; EV `free/total bays`) — for the agent's reply only; `--json` carries it as `rows` plus `live_sources_ok`. Posted Discord messages are unchanged (table PNGs, no text rows), except when the table image fails to render: then `*(table image unavailable)*` + the rows as text.
> - A failed HDB/LTA feed (incl. a 429 JSON body) → `*⚠️ <src> live data unavailable right now — this list may be incomplete.*` instead of `No parking within 1km`; a failed EV scan (>20 % probes) → the EV equivalent, and the area is not cached as covered.
> - HDB: the `C` lot entry is found anywhere in `carpark_info` (15 carparks were being dropped). LTA: all pages; HDB-agency rows deferred to the HDB feed.

# Singapore Carpark Availability

Five independent carpark availability systems cover Singapore.

## Systems Overview

| System | Source | Coverage | Live? | Coordinates | Key Field | Total Lots? |
|--------|--------|----------|-------|-------------|-----------|-------------|
| **HDB** | data.gov.sg | HDB estate carparks | ✅ | SVY21 (x/y) | `carpark_number` | Yes |
| **LTA** | LTA DataMall | Malls/commercial (Agency LTA), URA live lots (Agency URA) + ~2,000 HDB carparks (Agency HDB) | ✅ | WGS84 (`"lat lon"` string) | `CarParkID` + `Development` | No (avail only) |
| **JTC** | JTC / manual CSV | Industrial estate carparks | ❌ | SVY21 (x/y) | policy info only | No live avail |
| **URA** | data.gov.sg GeoJSON | URA-managed public carparks | ❌ | WGS84 (lon/lat) | lot counts only | No live avail |
| **EV** | LTA DataMall | EV charging points at carparks | ✅ | WGS84 (lat/lon) | postal-code search | — |

**Lot types:** C=Car, H=Heavy vehicle, S=Motorcycle w/ sidecar, Y=Motorcycle

## Data Files

| File | Contents | Location |
|------|----------|----------|
| HDB live CSV | 2,265 HDB carparks with SVY21 coords + parking policy | `~/.hermes/skills/productivity/sg-commute/data/carpark_info.csv` |
| JTC CSV | 28 JTC industrial carparks with SVY21 coords | `~/.hermes/skills/productivity/sg-commute/data/jtc_carpark_info.csv` |
| URA CSV | 777 URA public carparks with WGS84 + lot counts | `~/.hermes/skills/productivity/sg-commute/data/ura_carpark_info.csv` |

## HDB Carparks (live)

**API:** `https://api.data.gov.sg/v1/transport/carpark-availability` (no key required, ~40 req/min)

**⚠️ urllib.request gets HTTP 403** — always use `curl -o /tmp/hdb.json`.

**Response shape** (corrected from spec — `carpark_info` is a nested array, not flat):
```json
{
  "items": [{
    "timestamp": "2026-05-03T23:51:37+08:00",
    "carpark_data": [
      {
        "carpark_number": "HE12",
        "update_datetime": "2026-05-03T23:51:21",
        "carpark_info": [
          { "lots_available": "101", "total_lots": "105", "lot_type": "C" },
          { "lots_available": "12",  "total_lots": "100", "lot_type": "H" }
        ]
      }
    ]
  }]
}
```

**Python helper:**
```python
import subprocess, json

def get_hdb_availability():
    subprocess.run(["curl", "-s", "-o", "/tmp/hdb.json",
                    "https://api.data.gov.sg/v1/transport/carpark-availability"],
                   capture_output=True)
    with open("/tmp/hdb.json") as f:
        return json.load(f)
```

**Join key:** `carpark_number` (API) ↔ `car_park_no` (CSV)

**Nearest HDB carpark** (OneMap geocode → SVY21 → Euclidean distance):
```bash
# Step 1: Geocode to SVY21
curl -s "https://www.onemap.gov.sg/api/common/elastic/search?searchVal=238+Bukit+Panjang+Ring+Road&returnGeom=Y&getAddrDetails=Y&pageNum=1"
# Returns: { "X": "21027.30", "Y": "40196.31", ... }

# Step 2: Euclidean distance in SVY21 (1 unit ≈ 1 metre)
python3 -c "
import csv, math
TARGET_X, TARGET_Y = 21027.3, 40196.3
carparks = {}
with open('/home/amh/.hermes/skills/productivity/sg-commute/data/carpark_info.csv') as f:
    for row in csv.DictReader(f):
        carparks[row['car_park_no']] = row
# ... (see sg-carpark-availability skill for full script)
"
```

## LTA Carparks (live)

**Endpoint:** `https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2`
**Auth:** `AccountKey: ${LTA_DATAMALL_KEY}`
**⚠️ Must use `CarParkAvailabilityv2` (with 'v2') — non-v2 endpoint returns 404**

**Location field** is a string `"lat lon"` (space-separated) — split on space to parse:
```python
lat, lon = float(item["Location"].split()[0]), float(item["Location"].split()[1])
```

**LTA provides `AvailableLots` only** — no capacity, cannot compute % fullness.

**⚠️ Car lots only (C) — skip H/Y rows:** The API returns one row per (CarParkID, LotType) pair. A single CarParkID may appear with LotType=C (car), H (heavy vehicle), or Y (motorcycle). The `lta_live()` function filters to `LotType == "C"` only and sums `AvailableLots` per CarParkID — H/Y rows are discarded entirely. Verified fix applied May 2026.

**Haversine for nearest LTA carpark:**
```python
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2-lat1)
    dlon = math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))
```

## JTC Carparks (static)

28 industrial estate carparks with coupon parking — no live availability API.
Useful for policy lookups (free parking windows, night parking availability):
```python
import csv
with open('/home/amh/.hermes/skills/productivity/sg-commute/data/jtc_carpark_info.csv') as f:
    for row in csv.DictReader(f):
        if 'ANG MO KIO' in row['address'].upper():
            print(f"{row['car_park_no']} | {row['address'][:50]} | {row['short_term_parking']} | {row['free_parking']} | night={row['night_parking']}")
```

**Nearest JTC by SVY21 distance:**
```python
import csv, math
TARGET_X, TARGET_Y = 29466.6, 41365.8  # AMK SVY21
with open('/home/amh/.hermes/skills/productivity/sg-commute/data/jtc_carpark_info.csv') as f:
    carparks = list(csv.DictReader(f))
results = []
for cp in carparks:
    cx, cy = float(cp['x_coord']), float(cp['y_coord'])
    dist = math.sqrt((TARGET_X-cx)**2 + (TARGET_Y-cy)**2)
    results.append((dist, cp))
results.sort()
for dist, cp in results[:5]:
    print(f'{dist:.0f}m | {cp["car_park_no"]} | {cp["address"][:40]} | {cp["short_term_parking"]} | night={cp["night_parking"]}')
```

## URA Carparks (static)

777 URA-managed public carparks — on-street and off-street, no live availability.
**URA fields:** `pp_code`, `parking_pl` (name), `no_car`, `no_h_vehicle`, `no_mcycle`, `longitude`, `latitude`

**URA nearest filter:** `nearest_ura()` skips rows where `no_car == 0` — these are motorcycle-only carparks (BL021, BL022, etc.) with `no_car=0` in the CSV. Only car-capable lots are considered for distance ranking.
```

**Known nearest to offices:**
- Innovis → A0043 (Ayer Rajah Crescent, 592m, 17 cars)
- AMK → A0037 (Ang Mo Kio St 64, 408m, 11 cars)

## EV Charging Points (live)

**Endpoint:** `https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode=<POSTAL>`
**Auth:** same `AccountKey: ${LTA_DATAMALL_KEY}`

**⚠️ Coverage is extremely narrow** — single postal returns 1-2 locations even in dense EV areas.
**Multi-postal scan strategy** (v1.9.0+): 2-digit postal sector±1 × delivery-point±50 (~303 postals), run over a per-thread keep-alive `requests.Session` (12 workers, retry-5xx) → ~1.4s on a cache miss. Cache hit = instant. See `references/ev-charging.md` for the full scan/cache design.

**Response schema (ACTUAL — verified May 2026):**
```json
{
  "value": {
    "evLocationsData": [{
      "address": "2 FUSIONOPOLIS WAY SINGAPORE 138634",
      "name": "INNOVIS",
      "longitude": 103.788698,
      "latitude": 1.298426,
      "locationId": "788698138634",
      "chargingPoints": [{
        "operator": "COMFORTDELGRO ENGIE PTE. LTD.",
        "position": "B3 145",
        "plugTypes": [{
          "plugType": "Type 2",
          "powerRating": "AC",
          "chargingSpeed": "22",
          "price": "0.6750",
          "priceType": "kWh",
          "evIds": [{ "id": "", "evCpId": "R102528F-001", "status": "1" }]
        }]
      }]
    }]
  }
}
```

**⚠️ status field is INSIDE evIds[0], NOT at plugTypes level.**
Available bays = count of `chargingPoints[].plugTypes[].evIds[].status == "1"`.
Values: `"1"` = available, `"0"` = occupied.

**Multiple chargingPoints at same location:**
Aggregate ALL chargingPoints at a location (same rounded lat/lon) before counting bays:
- `total` = sum of `sum(len(pt.get("evIds", [])) for pt in cp.get("plugTypes", []))` across ALL CPs at this location
- `available` = count of plugs where `evIds[0].status == "1"` across ALL CPs
- Dedup by `(round(lat,5), round(lon,5))` before selecting top-3 nearest

**⚠️ EV bay total_bays bug: count evIds, not plugTypes.** One `plugTypes` entry may contain multiple `evIds` (e.g. Combo 2 DC charger with 2 bay IDs). Wrong: `total_bays += len(plugTypes)` → shows 2/1 when there are 2 bays. Correct: `total_bays += sum(len(pt.get("evIds", [])) for pt in plugTypes)`.

**EV bay model (corrected):**
```python
# Aggregate all chargingPoints at same location
from collections import defaultdict
loc_bays = defaultdict(lambda: {"total": 0, "available": 0})
for loc in ev_locations:
    key = (round(loc["latitude"], 5), round(loc["longitude"], 5))
    for cp in loc.get("chargingPoints", []):
        for pt in cp.get("plugTypes", []):
            loc_bays[key]["total"] += len(pt.get("evIds", []))  # count evIds, not plugTypes
            if pt.get("evIds", [{}])[0].get("status") == "1":
                loc_bays[key]["available"] += 1
    loc_bays[key]["name"] = loc.get("name", "")
    loc_bays[key]["address"] = loc.get("address", "")
```

## Geocoding Summary

| Input | Tool | Output |
|-------|------|--------|
| Postal code | OneMap API (free, no token) | SVY21 X, Y + WGS84 lat/lon |
| Street address | Google Geocoding API (primary, stored key) | SVY21 X, Y + WGS84 lat/lon |
| HDB carpark | CSV | SVY21 X, Y |
| LTA carpark | LTA API response | WGS84 lat/lon string |
| URA carpark | CSV | WGS84 (lon/lat) |
| EV charging | CSV | WGS84 (lat/lon) |

**Priority** *(corrected 2026-09-29 — this line said Google first)*: 6-digit postal → **OneMap first** (building-level address, native SVY21), Google fallback; street address → Google (`components=country:SG`); anything outside Singapore's bounding box is rejected. SVY21 computed from WGS84 via `pyproj` (`Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)`).

**⚠️ NO POSTAL CODE → ASK USER** — do not attempt to derive or guess. Nominatim returns 138543 for Innovis coords but actual postal is 138634 — guess misses EV charger locations by ~700m.

## Pre-cached Office SVY21 Coordinates

For fast carpark lookups near known offices, use these verified SVY21 coordinates:

| Location | SVY21 X | SVY21 Y | WGS84 lat | WGS84 lon | Source |
|----------|---------|---------|-----------|-----------|--------|
| Innovis (2 Fusionopolis Walk) | 23148.2 | 31188.9 | 1.29834 | 103.78972 | Google Geocoding (May 2026) |
| AMK (12 Ang Mo Kio St 64) | 29466.6 | 41365.8 | 1.39037 | 103.84650 | OneMap postal 569088 |
| Home (Pasir Ris Grove) | — | — | 1.37290 | 103.95060 | — |

For any other address, geocode via OneMap postal code or Google Geocoding API.

## nearest_carpark.py Script

Full end-to-end carpark query script at:
```
/home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py
```

```bash
python3 /home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py \
  "2 Fusionopolis Walk, Singapore 138634"
```

What it does:
1. Geocode address via OneMap (postal code → SVY21 + WGS84)
2. HDB: SVY21 Euclidean distance, live availability from API, CSV for coords/address
3. LTA: Haversine (WGS84), live availability from API
4. URA: Haversine (WGS84), static lot counts from CSV
5. JTC: SVY21 Euclidean distance, no live availability
6. EV: **cache-first** — haversine the local `data/ev_locations.csv` for the nearest 3; live discovery scan only on a cache miss (new area), then availability for the displayed 3 is re-fetched live. See `references/ev-charging.md` (cache-first block). Monthly cron: `nearest_carpark.py --refresh`.
7. Google Static Map with `color:red|label:YOU` for user address and numbered pins
8. EV markers use `A`/`B`/`C` labels (single letters render correctly; numeric-with-suffix like `E1` dropped by Google)

**EV scan performance (v1.9.0+):** EV postal queries run over a per-thread keep-alive `requests.Session` (`ThreadPoolExecutor`, **12 workers**, retry on transient 5xx) — the scan is I/O-bound and connection-reuse-bound. Combined with the location cache, repeat queries are ~1s and a cold-area scan of ~303 postals is ~1.4s. (Pre-v1.9.0 it was 64 curl-subprocess-spawns → ~15-20s, and silently dropped chargers on the LTA API's intermittent 500s; see `references/ev-charging.md` and `changelog.md` 2026-06-06.)

**EV cache files** (auto-created): `data/ev_locations.csv` (static charger locations), `data/ev_anchors.csv` (areas already discovered, used to decide cache-hit vs re-scan).

**Table formatting** *(2026-09-29: the tables are now rendered by the `discord-table-format` skill's `render_tables.py`, one call for both tables, and the Live column shows `Y`/`N` — the ✅/❌ glyphs and `render_table()` described next are the older in-script renderer)*: both tables are rendered by `render_table()`, which computes each column's width from the data (header + every cell) and builds the rule from those same widths, so header/rule/rows always align. Cell content is kept ASCII for general use, but the **Live column in the parking table uses `✅` (live: HDB / LTA) and `❌` (static: URA / JTC)** — these are east-asian-width-2 glyphs but `_dwidth()` accounts for them correctly, so the table still aligns. The Avail cell is plain ASCII (`33/57` or `4`). Width is measured with `_dwidth()` (east-asian-width aware) as a safety net — only the Live column carries wide glyphs.

**Parking table column order (2026-06-01 — user-specified, do not change without asking):**
`# | Dist | Avail | Live | Code | System | Address`
- `#` — entry number (1, 2, 3), right-aligned
- `Dist` — straight-line distance from user, format `{d:.0f}m` (e.g. `230m`), right-aligned
- `Avail` — `<lots>/<total>` for HDB/LTA, plain `<lots>` for URA/JTC, right-aligned
- `Live` — `Y` for live HDB/LTA rows, `N` for static URA/JTC rows and for an HDB sensor that hasn't reported for >48 h (was `✅`/`❌`), left-aligned
- `Code` — system-specific carpark code (HDB `HE12`, LTA `A0007`, URA `C0036`, JTC `JTCxx`), left-aligned
- `System` — `HDB` / `LTA` / `URA` / `JTC`, left-aligned
- `Address` — human-readable location, left-aligned

This order is the user's explicit preference — reordering is a class-level format change and must not be made ad-hoc. If updating the script's `render_table()` headers, the row data tuple must be reshuffled in lockstep.

**⚠️ Python path for image generation:**
- Tabulate available: `~/.hermes/hermes-agent/venv/bin/python3`
- PIL available: `/usr/lib/python3/dist-packages/PIL`
- Use both together: `sys.path.insert(0, '/usr/lib/python3/dist-packages')` before importing PIL

**⚠️ pyproj requirement:** SVY21→WGS84 conversion via pyproj. Install with:
```bash
pip install --user --break-system-packages pyproj
```

**⚠️ Execute via terminal, NOT execute_code sandbox:** The script uses pyproj and runs in `/home/amh/.hermes/skills/productivity/sg-commute/` with `sys.path.insert(0, '/home/amh/.local/lib/python3.11/site-packages')`. The execute_code sandbox has no pyproj installed — it will fail with `ModuleNotFoundError: No module named 'pyproj'`. Always run via:
```bash
cd /home/amh/.hermes/skills/productivity/sg-commute && /usr/bin/python3 scripts/nearest_carpark.py "<address>"
```
The terminal tool has pyproj available system-wide.

## Execution (always use terminal, not execute_code sandbox)

**pyproj is only available in system python3** — the execute_code sandbox lacks it and will fail with `ModuleNotFoundError: No module named 'pyproj'`.

```bash
cd /home/amh/.hermes/skills/productivity/sg-commute && \
  PYTHONUNBUFFERED=1 /usr/bin/python3 scripts/nearest_carpark.py "<postal or address>"
```

**Run examples:**
```bash
PYTHONUNBUFFERED=1 /usr/bin/python3 scripts/nearest_carpark.py "518073"   # Home
PYTHONUNBUFFERED=1 /usr/bin/python3 scripts/nearest_carpark.py "238879"   # Orchard
PYTHONUNBUFFERED=1 /usr/bin/python3 scripts/nearest_carpark.py "569088"   # AMK
```

## Verified End-to-End Results

| Postal | Location | Parking Result | EV Result | Runtime (warm cache, v1.9.0+) |
|--------|----------|---------------|-----------|----------------|
| 569088 | 12 Ang Mo Kio Street 64 | 3 URA static, dist 409-690m | 2 pts, 120kW + 22kW | ~2s |
| 518073 | 27 Pasir Ris Grove (Home) | 3 HDB live ✅, 210-354m | 3 within 1 km (442/521/586 m, 2026-09-29) | ~2s |
| 238879 | 442 Orchard Road | 2 URA + 1 LTA live ✅ (A0007=224), 230-306m | 21 pts total, top 3 shown | ~2s |

**Pipeline:** geocode (OneMap postal / Google street) → load HDB/LTA/URA/JTC static CSVs → fetch live HDB (~0.3s) + LTA (~0.1s) → EV lookup (cache hit = instant; cold-area miss runs a one-time scan ~1.4s, v1.9.0+) → merge + dedupe (LTA live > URA static) → top-3 → render parking+EV tables (one renderer call) + Google static map. Warm queries ~2s; cold-area first query +~1.4s. (Pre-v1.9.0 the cold EV scan made the whole run ~90s — see `changelog.md` 2026-06-06.)

**Script completes fine** — the LTA feed is ~2,600 rows over 6 pages of 500 (fetched concurrently since v1.11.0; one call used to see only the first 500). The `PYTHONUNBUFFERED=1` flag is required because bare `print()` without `flush=True` delays stdout, making the LTA phase appear as a hang.

## Verification Checklist

- [ ] HDB: `curl -s https://api.data.gov.sg/v1/transport/carpark-availability | python3 -m json.tool | head` returns JSON with `items[].carpark_data`
- [ ] HDB CSV: carpark info CSV exists at `~/.hermes/skills/productivity/sg-commute/data/carpark_info.csv`
- [ ] OneMap postal code geocoding: `curl -s "https://www.onemap.gov.sg/api/common/elastic/search?searchVal=138634&returnGeom=Y&getAddrDetails=Y&pageNum=1"` returns X/Y fields (no token needed for postal codes)
- [ ] HDB nearest carpark: OneMap SVY21 + Euclidean distance correctly identifies BJ10 as nearest to 238 Bukit Panjang Ring Rd (~95m)
- [ ] LTA: `curl -s "https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2" -H "AccountKey: ${LTA_DATAMALL_KEY}" -H "accept: application/json"` returns JSON with `value[].Location`
- [ ] LTA nearest: haversine distance correctly identifies nearby malls within 2km of any address
- [ ] OneMap street address geocoding requires API token — if `Authentication token missing` error, use Google Geocoding API instead