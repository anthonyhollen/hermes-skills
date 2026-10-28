# Data.gov.sg Traffic Images API

Traffic camera images from LTA Datamall, served via `api.data.gov.sg`.

## Endpoint

```
GET https://api.data.gov.sg/v1/transport/traffic-images
```

**No API key needed** for basic use. Optional `date_time` query param (SGT, `YYYY-MM-DDTHH:mm:ss`).

## Response Shape

⚠️ The OpenAPI spec is incomplete — the actual response includes `location` (lat/lon) per camera, which the spec omits.

```json
{
  "items": [{
    "timestamp": "2026-06-07T23:21:10+08:00",
    "cameras": [{
      "timestamp": "2026-06-07T23:21:10+08:00",
      "camera_id": "2701",
      "image_id": null,
      "image": "https://images.data.gov.sg/api/traffic-images/2026/06/<uuid>.jpg",
      "location": {
        "latitude": 1.447023728,
        "longitude": 103.7716543
      },
      "image_metadata": {
        "height": 1080,
        "width": 1920,
        "md5": "ea888c31d6abe9cb3b5f87ebd90723aa"
      }
    }]
  }]
}
```

- ~90 cameras, updated every 20 seconds
- `camera_id` is a string (e.g. `"2701"`), NOT a number as the spec claims
- `image_id` is always null in the actual response
- Camera images: 1920×1080 (most) or 320×240 (older cameras)

## Fetching Camera Images

⚠️ The image CDN (images.data.gov.sg) blocks bare `urllib.request` with HTTP 403. Always send a browser-like User-Agent and Referer:

```python
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.data.gov.sg/",
})
with urllib.request.urlopen(req, timeout=15) as resp:
    data = resp.read()
```

## Reverse Geocoding Camera Locations

OneMap reverse geocode now requires auth (returns `{"message": "Unauthorized"}`). Use **Google Geocoding API** instead:

```
GET https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&key={key}
```

Returns `formatted_address` (street-level) and `postal_code` from `address_components`. API key: `${GOOGLE_API_KEY}`.

**Cache geocode results** — 90 cameras × API calls adds up. Cache to JSON keyed by `"lat,lon"`.

## Plotting Cameras on Google Static Maps

### URL construction

```python
import urllib.parse

markers = [f"color:red|{c['latitude']:.6f},{c['longitude']:.6f}" for c in cameras]
markers_str = "|".join(markers)
encoded = urllib.parse.quote(markers_str, safe="|=,")

url = (f"https://maps.googleapis.com/maps/api/staticmap"
       f"?center={lat_center},{lon_center}&zoom={zoom}&size=640x480"
       f"&maptype=roadmap&key={api_key}&markers={encoded}")
```

### Batch markers for URL length

Static Maps URL limit is ~8192 chars. With ~50 chars per marker, split into batches of 50:

```python
batch_size = 50
for i in range(0, len(markers), batch_size):
    batch = markers[i:i+batch_size]
    url += f"&markers={urllib.parse.quote('|'.join(batch), safe='|=,')}"
```

### Numbered/labeled markers

Each marker needs its **own `&markers=` param** with `label:X` — Google groups markers sharing a single param under one style. Labels: `1`-`9` as digits, `10`+ as `A`-`Z`. Use `%7C` (encoded pipe) in the marker spec.

```python
for i, c in enumerate(cameras, 1):
    label = str(i) if i <= 9 else chr(ord("A") + i - 10)
    url += f"&markers=color:red%7Clabel:{label}%7C{c['latitude']:.6f},{c['longitude']:.6f}"
url += f"&key={api_key}"
```

**Failed approaches:** KML paddle icons (`maps.google.com/mapfiles/kml/paddle/`) render inconsistently; data URIs and Google Charts API are unsupported/deprecated. Use `label:X` only.

### Auto-calculate center and zoom

Don't hardcode center/zoom — derive from actual camera spread:

```python
lats = [c['latitude'] for c in cameras]
lons = [c['longitude'] for c in cameras]
center_lat = (min(lats) + max(lats)) / 2
center_lon = (min(lons) + max(lons)) / 2
span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.6)  # aspect ratio
zoom = {0.02: 14, 0.05: 13, 0.1: 12, 0.2: 11, 0.5: 10}.get(
    min(z for z in [0.02, 0.05, 0.1, 0.2, 0.5] if z >= span), 10)
```

## Splitting Cameras into Regions (N/S/E/W Singapore)

Use **strict bounding boxes** — no "nearest-center" leakage that puts cameras in wrong regions.

```
North:  lat >= 1.38
South:  lat <  1.28
East:   1.28 <= lat < 1.38 AND lon >= 103.88
West:   1.28 <= lat < 1.38 AND lon <  103.80
Center gap (1.28–1.38, 103.80–103.88): assign to nearest region center
```

⚠️ Don't use overlapping bounding boxes — cameras at boundaries appear on multiple maps.

## Temp File Cleanup

Always delete downloaded images after sending to Discord to avoid accumulating temp files.

```python
import os
os.remove(image_path)
```

## Commute Route Camera Map

Script: `~/.hermes/scripts/commute_camera_map.py`

Generates a route map with traffic cameras pinned along the path. Green polyline from H (start) to W (end), with red labeled markers (1-9, A-Z) for cameras within 500m of the route.

**Camera ordering:** Sorted by position along route (camera 1 nearest start, last camera nearest end), NOT by distance from route. Each camera's nearest route point index determines its label.

```bash
python3 ~/.hermes/scripts/commute_camera_map.py --start "Home" --end "Innovis"
python3 ~/.hermes/scripts/commute_camera_map.py --start "Changi Airport" --end "Jurong East"
python3 ~/.hermes/scripts/commute_camera_map.py --start "Home" --end "Innovis" --waypoint "via:1.292,103.885"
# ⚠️ (2026-09-29) this single via is the one that detours off ECP (exit 13 → U-turn) in the
# commute report; for an ECP→MCE route use the two mainline points in morning_commute.py INNOVIS_VIA.
```

Shortcuts: `home` → Pasir Ris Grove, `innovis` → 2 Fusionopolis Way.
