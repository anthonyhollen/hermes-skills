# Geocoding Troubleshooting Reference

## OneMap Token Expiry (May 2026)
**Symptom:** `geocode("2 Fusionopolis Walk, Singapore")` returns `None` with no warning.
**Root cause:** `https://www.onemap.gov.sg/api/common/elastic/search` now requires an API token for **street address** searches. Returns `{"error": "Authentication token missing..."}` for non-postal queries. Postal codes (6-digit) still work without a token.

**Reproduction:**
```bash
curl -s "https://www.onemap.gov.sg/api/common/elastic/search?searchVal=2+Fusionopolis+Walk&returnGeom=Y&getAddrDetails=Y&pageNum=1"
# → {"error": "Authentication token missing...", "found": 0, "results": []}
```

**Fix applied (May 2026):**
- `geocode()` now tries **Google Geocoding API** first (handles any address format)
- Falls back to **OneMap postal-only search** (no token needed for 6-digit postals)
- SVY21 coordinates computed from Google WGS84 via `pyproj` inverse transform (EPSG:4326→3414)

## `loc["lon"]` vs `loc["lng"]` Bug
**Symptom:** `WARNING: Google geocode failed for '...': 'lon'`
**Root cause:** Google Geocoding JSON uses `lng` (not `lon`) for longitude key. pyproj's `always_xy=True` also means transformer expects `(lon, lat)` order, not `(lat, lon)`.

**Wrong:**
```python
lat, lon = float(loc["lat"]), float(loc["lon"])  # KeyError: 'lon'
x, y = wgs84_to_svy21(lat, lon)  # pyproj expects lon first
```

**Correct:**
```python
lat, lon = float(loc["lat"]), float(loc["lng"])  # Google uses "lng"
x, y = wgs84_to_svy21(lat, lon)  # pyproj always_xy: (lon, lat) → (x, y)
```

## SVY21 Inverse Transform
Google returns WGS84 (lat, lon). HDB/JTC carpark CSVs use SVY21 (EPSG:3414). Distance to those carparks requires Euclidean in SVY21 space.

```python
from pyproj import Transformer
_t_inv = Transformer.from_crs("EPSG:4326","EPSG:3414",always_xy=True)

def wgs84_to_svy21(lat, lon):
    x, y = _t_inv.transform(lon, lat)  # always_xy: lon first, lat second
    return x, y
```

**Test:** `wgs84_to_svy21(1.2983356, 103.7897228)` → `(23148.2, 31188.9)` (verified against OneMap for 2 Fusionopolis Walk)

## API Response Shapes

### Google Geocoding (primary)
```json
{"status": "OK", "results": [{
  "formatted_address": "2 Fusionopolis Walk, Singapore",
  "geometry": {"location": {"lat": 1.2983356, "lng": 103.7897228}},
  "address_components": [{"types": ["postal_code"], "long_name": "138603"}]
}]}
```

### OneMap Postal Fallback
```json
{"found": 1, "results": [{
  "ADDRESS": "2 FUSIONOPOLIS WALK",
  "POSTAL": "138603",
  "LATITUDE": "1.2983356",
  "LONGITUDE": "103.7897228",
  "X": "23148.2", "Y": "31188.9"
}]}
```

## Geocoding Priority (as of May 2026)
1. **Google Geocoding API** — handles street addresses, place names, partial queries; returns WGS84
2. **OneMap postal** — free, no auth, only for 6-digit Singapore postal codes; returns SVY21 + WGS84

> **Correction (2026-07-12):** the order above holds only for **street addresses**. The live `geocode()` is **input-dependent**: a **6-digit postal → OneMap FIRST** (authoritative SG geocoder; returns the building-level `ADDRESS` + native SVY21 X/Y, and stops Google's terse "Singapore <postal>" from winning), with Google as the fallback; a **street address → Google** (OneMap street search now needs a token). So "Google first" is true for street input, "OneMap first" for postal input — don't reorder the code to make it unconditionally Google-first.


> **Update (2026-09-29, v1.11.0):** Google geocoding is now restricted with `components=country:SG`, and any geocode (OneMap or Google) outside Singapore's bounding box is rejected — an unrestricted lookup of the carpark code "HG22" had matched Harrogate, UK. A failed geocode now prints `ERROR: Could not geocode address (no match in Singapore)`; the agent should ask for a postal code.
