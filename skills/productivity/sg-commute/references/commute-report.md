# Morning Commute Report

> **🔄 As of 2026-05-31 this report runs as a `no_agent` cron script: `~/.hermes/scripts/morning_commute.py` (job `b617d89da15b`).** No LLM agent is involved and this skill is not loaded at runtime. The script implements everything documented below and, since v1.13.0, posts the report + route map to #alerts itself as **one** Discord message, then prints `[SILENT]` (+ the report, kept in the cron archive) so the scheduler delivers nothing. The scheduler can't combine them: it sends the text, then each `MEDIA:` file as a separate message (`_send_media_via_adapter`). If the self-post fails it falls back to printing the report + `MEDIA:` for the scheduler (two messages, not lost). **`morning_commute.py` is the source of truth** — this reference documents the logic/format it implements. The `send_message` / `execute_code` delivery patterns below now apply only to manual/ad-hoc runs.

Daily morning commute report sent to Discord channel `1499972245437550602` (#alerts) at **07:25 SGT** on Tue/Wed/Thu (cron `25 7 * * 2,3,4`).

## Destinations

| Day | Destination | Address |
|-----|-------------|---------|
| Tuesday | Innovis | 2 Fusionopolis Way, Singapore 138634 |
| Wednesday | Innovis *(AMK until 2026-10-14)* | 2 Fusionopolis Way, Singapore 138634 |
| Thursday | Innovis | 2 Fusionopolis Way, Singapore 138634 |
| *(unscheduled — on file)* | AMK | 12 Ang Mo Kio Street 64, Singapore 569088 |

Day-of-week comes from the Pi clock (`datetime.now().weekday()`, TZ Asia/Singapore) and is mapped through `SCHEDULE` (weekday → profile slug) to a profile in `DEST_PROFILES` (label, address, rain station). **AMK is kept as a profile so it can be restored to any day:** set that weekday to `"amk"` in `SCHEDULE` (e.g. `2: "amk"` restores the old Wednesday); for a day outside Tue–Thu also extend the cron (`hermes cron edit b617d89da15b --schedule "25 7 * * …"`). For a manual run pass `innovis`/`amk` (or `DEST=`); with no argument the script is silent outside Tue–Thu.

## Report Format

The exact current output (all variants of each line) is in `developer-guide.md` → *Morning Report Format*. In short:

```
## ☀️ Morning Commute Update

**1. Today's Weather** - Forecast: *<forecast>*, Temp: *<low>–<high>°C*, Humidity: *<low>–<high>%*, Wind: *<dir> <low>–<high> km/h*
• 6am–Midday: <text> <emoji>      ← one line per 24hr period, labelled from each period's OWN timePeriod
• Midday–6pm: <text> <emoji>         (the periods slide with the clock — never hardcode labels)
• 6pm–6am (tonight): <text> <emoji>

**2. Next 2 Hours Weather** *(Pasir Ris — Home)*
• Valid period: <HHMM start> – <HHMM end> SGT
• Pasir Ris: <forecast> <emoji>

**3. Rain Check near <X>** - *S71 - Kent Ridge Rd (1.1km away): 0.0mm in last 5 min - no rain ☁️*
• No flood alerts currently in effect

**4. Air Quality** - East region: *<value> µg/m³ (<level> <emoji>)*, Regional avg: *<avg> µg/m³ (<level> <emoji>)*
• WHO PM2.5 scale: 🟢 0–9 Good / 🟡 10–19 Moderate / 🟠 20–49 Unhealthy (Sensitive) / 🔴 50–99 Unhealthy / 🟣 100+ Very Unhealthy

**5. Commute to <DEST>** *(<address>)*
• Distance: <X.X> km
• Normal: ~<YY> min
• Current: ~<ZZ> min (traffic-aware) <traffic emoji>

*(Map polyline: 🟢 light / 🟡 moderate / 🔴 heavy / 🔵 no data)*
MEDIA:/tmp/route_<dest>.png
```
(The `MEDIA:` line appears only in print-only runs and the self-post fallback; the scheduler strips it and attaches the PNG as a separate message. The map is used only when curl exited 0 and the file has both the PNG signature and the IEND trailer.)

## Section Details

### Section 1 — Today's Weather (24hr forecast)

Source: `https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast`

**⚠️ Response wrapped in `data` key** — `w24["data"]["records"][0]["general"]`
Fields: `temperature.low/high`, `relativeHumidity.low/high`, `wind.direction`, `wind.speed.low/high`
Periods: `w24["data"]["records"][0]["periods"]` — each has `timePeriod.start/end/text` and `regions.<region>.text`
Timestamps carry `+08:00` already (the scripts' `hhmm()` converts any other offset to SGT) — do NOT add 8 h.

Use East region (`periods[].regions.east.text`) for Pasir Ris forecast.

Emoji: 🌧️ Thundery Showers, ☁️ Cloudy, ⛅ Partly Cloudy, 🌦️ Showers

### Section 2 — Next 2 Hours (2hr nowcast)

Source: `https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast`

**⚠️ Response wrapped in `data` key** — `w2["data"]["items"][0]`
Valid period: `w2["data"]["items"][0]["valid_period"]` (snake_case — NOT camelCase)
Forecasts: `w2["data"]["items"][0]["forecasts"][]` — each has `.area` and `.forecast`

**Pasir Ris matching** (fuzzy):
1. Exact: `"Pasir Ris"`
2. Partial: starts with `"Pasir Ris"` (e.g. `"Pasir Ris Central"`)
3. Fallback: first available area, note: *"Nearest area: <name>"*

**⚠️ Always use `curl`** — urllib silently returns HTTP 403 on this endpoint.

### Section 3 — Rain Check

Source: `https://api-open.data.gov.sg/v2/real-time/api/rainfall`

**⚠️ Use v2 endpoint ONLY — v1 (`/v1/weather/rainfall`) requires an API token and returns "Missing Authentication Token".** The correct endpoint is `https://api-open.data.gov.sg/v2/real-time/api/rainfall` with no authentication.

Station mapping:
| Day | Destination | Station | Station Name | Distance |
|-----|-------------|---------|--------------|----------|
| Tue / Wed / Thu | Innovis | S71 | Kent Ridge Rd | ~1.1km |
| *(when scheduled)* | AMK | S109 | Ang Mo Kio Ave 5 | ~1.3km |

Rainfall value interpretation — **the value is a 5-MINUTE total** (`readingType` "TB1 Rainfall 5 Minute Total F"; the gauge ticks in 0.2 mm), so the script converts ×12 to mm/h and prints `X.Xmm in last 5 min (~Y mm/h) - <label>`:
| Rate (mm/h) | Label | Emoji |
|-------|-------------|-------|
| 0 | no rain (no rate shown) | ☁️ |
| < 2.5 | light rain | 🌦️ |
| 2.5 – 7.6 | moderate rain | 🌧️ |
| ≥ 7.6 | heavy rain | 🌧️ |

(Changed 2026-09-29, v1.11.0 — the old table applied hourly-style bands to the raw 5-minute value, so 1.2 mm/5 min ≈ 14 mm/h shipped as "light rain". If `readingType` ever stops saying "5 Minute", the script shows the raw value + readingType without bands.)

**⚠️ Validate station count** — must be ≥60 (89 live on 2026-09-29). If <60, the section says no live data.

**Flood line:** only the NEWEST 2-minute snapshot of `weather/flood-alerts` counts as "in effect" (its `readings` = the active alerts); readings only in older snapshots of the ~48-min window → "reported in the last ~45 min … may have cleared"; a failed fetch/429 → "Flood alert status unavailable" (never a false all-clear). Before v1.11.0 the line counted snapshots, so one alert read as "25 active station readings" and a cleared one as current.

### Section 4 — Air Quality (PM2.5)

Source: `https://api-open.data.gov.sg/v2/real-time/api/pm25`

**⚠️ Response wrapped in `data` key** — `pm["data"]["items"][0]`
East reading: `pm["data"]["items"][0]["readings"]["pm25_one_hourly"]["east"]` (snake_case key)
Regional average: average of N/S/E/W/Central values
Timestamps: already SGT (+08:00) — no conversion needed

Scale (the user's chosen bands, printed in full in the legend since v1.11.0; every band has its own emoji since v1.12.1): 🟢 0–9 Good, 🟡 10–19 Moderate, 🟠 20–49 Unhealthy (Sensitive), 🔴 50–99 Unhealthy, 🟣 100+ Very Unhealthy. Only numeric regions are averaged (a null region used to blank the section).

### Section 5 — Commute

**Traffic emoji** — derive from the same ratio used for polyline colour:
| Ratio | Emoji | Meaning |
|-------|-------|---------|
| no data | 🔵 | Traffic data unavailable |
| < 1.10 | 🟢 | Light traffic |
| 1.10–1.35 | 🟡 | Moderate congestion |
| ≥ 1.35 | 🔴 | Heavy congestion |

**Use Google Directions API** (not Routes API v2). **Innovis uses two `via:` points to force ECP→MCE; AMK uses none** (see below).

**⚠️ AMK uses a direct route (no waypoints).** Adding ECP/MCE/AYE waypoints for AMK causes Google to route through Marina Bay + city center, adding ~46km and producing a completely wrong polyline. AMK is on the TPE→PIE→AME corridor — a direct route is ~14km.

**⚠️ Innovis is forced onto ECP→MCE with TWO mainline `via:` points (`INNOVIS_VIA`, restored 2026-07-02).** Google won't pick ECP→MCE unaided (KPE/PIE is shorter), and a *single* via near the interchange snaps to the parallel East Coast Park Service Rd → exit + U-turn (the old `via:1.292,103.885` bug). Two coordinates that bracket the ECP↔MCE interchange — derived from a real route's ECP/MCE step geometry so they land on the mainline — hold the route on the expressway (verified: ECP → exit 14B → MCE, 34.9 km, no detour). Current value: `via:1.30860,103.93347|via:1.27616,103.86845`. Re-derive with `~/.hermes/scripts/verify_ecp_route.py` if Google's geometry shifts. A `route_has_detour()` guard falls back to the no-waypoint route if the forced route ever weaves.

| Day | Destination | Waypoints | Expected distance |
|-----|-------------|-----------|------------------|
| Tue / Wed / Thu | Innovis | 2× `via:` ECP+MCE mainline | ~35 km (gate > 25) |
| *(when scheduled)* | AMK | **none** (direct only) | 13–15 km |

**Route computation (Python):**
```python
is_innovis = dest_address == "2 Fusionopolis Way, Singapore 138634"
# Innovis: two via points on the ECP + MCE mainline force the ECP→MCE corridor
# (restored 2026-07-02; derive/verify with verify_ecp_route.py). AMK: no waypoint.
waypoints_str = "via:1.30860,103.93347|via:1.27616,103.86845" if is_innovis else None

url = (
    "https://maps.googleapis.com/maps/api/directions/json"
    f"?origin={urllib.parse.quote('Pasir Ris Grove Singapore', safe='')}"
    f"&destination={urllib.parse.quote(dest_address, safe='')}"
    + (f"&waypoints={waypoints_str}" if waypoints_str else "")
    + "&mode=driving"
    + "&departure_time=now"
    + "&traffic_model=best_guess"
    + f"&key={API_KEY}"
)
```

Total distance = sum of all `legs[].distance.value`
Total duration = sum of all `legs[].duration.value` (seconds)
Polyline = `routes[0].overview_polyline.points` (use for Static Map)

**⚠️ Validate distance + detour after computing.** Keep the forced Innovis route only if `route_has_detour()` finds no U-turn/service-road weave **and** distance ≥ 25 km; otherwise refetch with no waypoint and append `*(ECP route unavailable — showing fastest route)*`. A final safety net flags whatever ships with `*(route may be inaccurate — verify manually)*` if it's still < 25 km (AMK < 8 km) **or** an Innovis route still weaves — so a weaving/short fallback is never delivered unflagged.

If traffic-aware fails (400/403/500/network error), fall back to normal only and append `*(traffic data unavailable)*`.

**Use `departure_time=now`** for traffic-aware calls — this ensures `duration_in_traffic` is returned.

**⚠️ Duration parsing**: Google Routes v2 returns `"1866s"` format (seconds), NOT ISO 8601 `PT30M`.
```python
duration = route["duration"]   # e.g. "1866s"
if duration.endswith("s"):
    minutes = int(int(duration[:-1]) / 60)
elif duration.startswith("PT"):
    minutes = int(re.search(r'(\d+)M', duration).group(1))
else:
    minutes = int(re.search(r'(\d+)', duration).group(1))
```

**Static Map** (generate after route computation):
```python
# Traffic-based polyline colour
# normal_secs = sum of legs[].duration.value
# traffic_secs = sum of legs[].duration_in_traffic.value (None if unavailable)
def traffic_color(normal_secs, traffic_secs):
    if not traffic_secs or normal_secs == 0:
        return "0x1565C0"  # blue — no traffic data
    ratio = traffic_secs / normal_secs
    if ratio < 1.10:
        return "0x00C853"  # green — light traffic (<10% slower)
    elif ratio < 1.35:
        return "0xFFAB00"  # amber — moderate congestion (10–35% slower)
    else:
        return "0xD50000"  # red — heavy congestion (≥35% slower)

polyline_color = traffic_color(normal_secs, traffic_secs)
polyline_enc = urllib.parse.quote(polyline, safe='')
query_parts = [
    "size=640x450", "scale=2",   # ≤640 per side — Google clamps larger
    f"markers=color:green|label:H|{urllib.parse.quote('Pasir Ris Grove', safe='')}",
    f"markers=color:red|label:W|{urllib.parse.quote(dest_address, safe='')}",
    f"path=color:{polyline_color}|weight:7|enc:{polyline_enc}",
    f"key={API_KEY}",
]
url = "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(query_parts)
# Validate: if len(url) < 500, polyline was not encoded
```

## Discord Delivery

The cron job has `deliver: "discord:1499972245437550602"`. Since v1.13.0 the script, run with no arguments (cron mode), posts the report + route map to #alerts itself as **one** Discord message, then prints `[SILENT]` (+ the report, kept in the cron archive) so the scheduler delivers nothing. The scheduler can't combine them: it sends the text, then each `MEDIA:` file as a separate message (`_send_media_via_adapter`). If the self-post fails it falls back to printing the report + `MEDIA:` for the scheduler (two messages, not lost). `ALERTS_CHANNEL` in the script must match the job's deliver target. To look at a report without posting it, run `morning_commute.py innovis|amk` (a destination argument = print only). To post a test copy somewhere else, run `morning_commute.py innovis --post-to <channel_id>`. The agent-mode delivery patterns that used to be documented here (`send_message`, the two-call `execute_code` + `requests` pattern) were removed on 2026-09-29 — they are archived in `changelog.md` → *Relocated 2026-09-29*.
