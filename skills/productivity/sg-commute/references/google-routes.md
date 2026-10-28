# Google Routes API v2

Route computation and static map generation for Singapore commute.

## Compute Route

**Endpoint:** `POST https://routes.googleapis.com/directions/v2:computeRoutes`

**Headers (HTTP, NOT body):**
```
X-Goog-Api-Key: <API_KEY>
X-Goog-FieldMask: routes.distanceMeters,routes.duration,routes.polyline
Content-Type: application/json
```

**Body:**
```json
{
  "origin": {"address": "Pasir Ris Grove, Singapore 518141"},
  "destination": {"address": "2 Fusionopolis Way, Singapore 138634"},
  "travelMode": "DRIVE"
}
```

**For traffic-aware:**
```json
{
  "origin": {"address": "Pasir Ris Grove, Singapore 518141"},
  "destination": {"address": "2 Fusionopolis Way, Singapore 138634"},
  "travelMode": "DRIVE",
  "routingPreference": "TRAFFIC_AWARE",
  "departureTime": "2026-05-07T07:00:00+08:00"
}
```

**To avoid tolls** (user preference for commute routes):
```json
{
  "origin": {"address": "Pasir Ris Grove, Singapore 518141"},
  "destination": {"address": "2 Fusionopolis Way, Singapore 138634"},
  "travelMode": "DRIVE",
  "routingPreference": "TRAFFIC_AWARE",
  "departureTime": "2026-05-07T07:00:00+08:00",
  "routeModifiers": {"avoidTolls": true}
}
```
Note: `avoidTolls: true` adds ~3–7km vs the tolled KPE/PIE route but may save ~$1.60–$2.20 in ERP/toll fees. *(Correction 2026-09-29: the morning report does NOT avoid tolls — `morning_commute.py` uses the Directions API with the two Innovis `via:` points, `departure_time=now`, `traffic_model=best_guess`. This Routes-v2 section is for ad-hoc route work only.)*

**Use `departureTime=now`** for traffic-aware calls — this ensures `duration_in_traffic` is returned. Do NOT use a fixed future departure time as the API skips traffic data for scheduled future trips.

**FieldMask is required** — without it you get 400 error. Include `routes.polyline` explicitly — it's NOT in the default mask.

**DRIVE mode only** — TRANSIT durations are inflated 2–3× vs real-world experience.

## Duration Parsing

**⚠️ Google Routes v2 returns SECONDS format (`"1866s"`), NOT ISO 8601 `PT30M`:**

```python
# WRONG regex (expects PT30M format):
re.search(r'(\d+)M', duration)  → returns None → "0 min"

# CORRECT: handle both "1866s" (seconds) and "PT30M" (ISO 8601) formats
duration = route["duration"]   # e.g. "1866s"
if duration.endswith("s"):
    minutes = int(int(duration[:-1]) / 60)
elif duration.startswith("PT"):
    minutes = int(re.search(r'(\d+)M', duration).group(1))
else:
    minutes = int(re.search(r'(\d+)', duration).group(1))
# Always produces an integer, never "0 min" or "?"
```

## Traffic-Aware Fallback

If TRAFFIC_AWARE returns 400/403/500/network error:
1. Fall back to normal call (no `routingPreference`)
2. Append `*(traffic data unavailable)*` to Section 5 duration line
3. **Never retry** — if it fails immediately, it will keep failing

**400 Bad Request** is the most common failure mode — `departureTime` format may not be accepted even when valid.

## Static Map Generation

**⚠️ Innovis forces ECP→MCE with two mainline `via:` points; AMK uses none (restored 2026-07-02).** Google won't choose ECP→MCE unaided (KPE/PIE is a few km shorter). A single `via:` near the ECP↔MCE interchange snaps to the parallel East Coast Park Service Rd and detours off ECP (exit 13 → Marina East Dr → U-turn → MCE — the old bug). Two coordinates bracketing the interchange, lifted from a real route's ECP/MCE step geometry, keep it on the expressway: `via:1.30860,103.93347|via:1.27616,103.86845` (verified ECP → exit 14B → MCE, 34.9 km, no detour). Re-derive with `verify_ecp_route.py`. AMK is ~14 km direct on the TPE/PIE/AME corridor.

**⚠️ Waypoint snapping matters:** address-based waypoints resolve to a named location and **stop there**, truncating the polyline; coordinate `via:` waypoints pass through but, on dense expressway networks, snap to the nearest exit ramp — a single mid-interchange via caused the morning report's old ECP U-turn, fixed by using **two** mainline-derived coords that bracket the interchange (changelog 2026-07-02). Always derive `via:` coords from a real route's step geometry rather than guessing lat/lngs. (Also relevant to the separate `commute_camera_map.py` route-camera feature.)

```python
import urllib.parse

polyline_enc = urllib.parse.quote(polyline, safe='')
# safe='' encodes ALL special chars including {} and \

# ⚠️ DO NOT use urllib.parse.urlencode() on the full params dict
# urlencode encodes : and | which breaks "enc:" and "color:" values
# WRONG:  url = "https://maps.googleapis.com/maps/api/staticmap?" + urllib.parse.urlencode(params, safe='')
#   → produces enc%3AwvjGq... (broken — Google can't parse)

# CORRECT: join query string manually so : and | stay unencoded
query_parts = [
    "size=640x450",
    "scale=2",
    f"markers=color:green|label:H|{urllib.parse.quote('Pasir Ris Grove', safe='')}",
    f"markers=color:red|label:W|{urllib.parse.quote(dest_address, safe='')}",
    f"path=color:{polyline_color}|weight:7|enc:{polyline_enc}",
    f"key={API_KEY}",
]
url = "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(query_parts)
# polyline_color from traffic_color() — see commute-report.md
# 0x00C853 green (ratio<1.10) / 0xFFAB00 amber (1.10–1.35) / 0xD50000 red (≥1.35) / 0x1565C0 blue (no data)
```

**Validation:** If `len(url) < 500`, the polyline was not encoded — fix and retry.
If `"enc:" not in url`, path delimiters were encoded — use manual join.

## Working Python Code (verified May 2026)

```python
import requests, urllib.parse, os

API_KEY = os.environ.get("GOOGLE_API_KEY") or ""   # falls back to ~/.hermes/.env via _env_val in the live scripts

# Normal route
headers = {
    "X-Goog-Api-Key": API_KEY,
    "X-Goog-FieldMask": "routes.distanceMeters,routes.duration,routes.polyline",
    "Content-Type": "application/json",
}
body = {
    "origin": {"address": "Pasir Ris Grove, Singapore 518141"},
    "destination": {"address": "2 Fusionopolis Way, Singapore 138634"},
    "travelMode": "DRIVE",
}
r = requests.post(
    "https://routes.googleapis.com/directions/v2:computeRoutes",
    headers=headers, json=body, timeout=15,
)
route = r.json()["routes"][0]
distance_m = route["distanceMeters"]
duration = route["duration"]          # "1866s"
polyline = route["polyline"]["encodedPolyline"]   # string — NO ["points"] sub-key

# URL-encode polyline
polyline_enc = urllib.parse.quote(polyline, safe='')
params = {
    "size": "640x450", "scale": "2",
    "markers": [
        "color:green|label:H|Pasir Ris Grove",
        "color:red|label:W|2 Fusionopolis Way, Singapore 138634",
    ],
    "path": f"color:0x1E88E5|weight:5|enc:{polyline_enc}",
    "key": API_KEY,
}
# Manual join (NOT urlencode)
url = "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(
    f"{k}={urllib.parse.quote(str(v), safe='')}" if not isinstance(v, list) else
    "&".join(f"{k}={urllib.parse.quote(str(item), safe='')}" for item in v)
    for k, v in [("size","640x450"), ("scale","2"),
                 ("markers","color:green|label:H|Pasir Ris Grove"),
                 ("markers","color:red|label:W|2 Fusionopolis Way, Singapore 138634"),
                 ("path",f"color:{polyline_color}|weight:7|enc:{polyline_enc}"),
                 ("key",API_KEY)]
)
# Simpler:
query_parts = [
    "size=640x450", "scale=2", "format=png32",    # ← png32 required for Discord
    f"markers=color:green|label:H|{urllib.parse.quote('Pasir Ris Grove', safe='')}",
    f"markers=color:red|label:W|{urllib.parse.quote('2 Fusionopolis Way, Singapore 138634', safe='')}",
    f"path=color:{polyline_color}|weight:7|enc:{polyline_enc}",
    f"key={API_KEY}",
]
url = "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(query_parts)
# polyline_color = traffic_color(normal_secs, traffic_secs)
# 0x00C853 green (<1.10) / 0xFFAB00 amber (1.10–1.35) / 0xD50000 red (≥1.35) / 0x1565C0 blue (no data) — see commute-report.md
```

## API Verification

| API | Endpoint | Key? | Status |
|-----|----------|------|--------|
| Routes API v2 | `https://routes.googleapis.com/directions/v2:computeRoutes` | ✅ | ✅ Working |
| Static Maps | `https://maps.googleapis.com/maps/api/staticmap` | ✅ | ✅ Working |

API key: loaded from `GOOGLE_API_KEY` in `~/.hermes/.env` (never hardcoded).

## Pitfalls

1. **FieldMask required** — without `X-Goog-FieldMask` header you get 400
2. **`routes.polyline` not in default mask** — must add explicitly
3. **Polyline encoding mandatory** — unencoded `{}` silently breaks Static Maps
4. **urlencode() breaks path delimiters** — use manual string join
5. **DRIVE mode only** — TRANSIT is inflated 2–3×
6. **`departureTime` in past → 400** — set to 7:00am SGT for morning reports
7. **TRAFFIC_AWARE fails immediately → don't retry** — fall back to normal