---
name: google-route-map
description: "Generate a static route map image with turn-by-turn polyline drawn using Google Routes API + Static Maps API."
version: 1.1.0
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [Google, Maps, Routes, Static Maps, Route Optimization]
---

# Google Route Map Generator

Generate a static map image showing a driving route with the actual turn-by-turn polyline drawn on it.

## Usage

Set `GOOGLE_API_KEY` (with **both** Routes API and Static Maps API enabled), then give two addresses and get a PNG:

```bash
export GOOGLE_API_KEY=...
python3 scripts/route_map.py "<origin>" "<destination>" [output_path]   # default: /tmp/route_map.png
```

`route_map.py` calls Routes API → extracts the polyline → renders the Static Map in a single process, so the long encoded polyline never leaves Python (keeps it out of the caller's token budget). It prints only the route distance/duration and a `Saved …` line.

If you *already* hold an encoded polyline, render it directly:
`python3 scripts/download_static_map.py "<polyline>" "<origin>" "<dest>" [output_path]`.

## Gotchas (read before changing the scripts)

- **`format=png32` is mandatory for Discord.** Default PNG8 (256-color) can silently drop or misrender the blue polyline in Discord's image pipeline; PNG32 forces full RGB. Already set in the scripts.
- **Image size cap:** 640 px per dimension on the standard tier; `scale=2` doubles the output (max 1280 px). The scripts use `640x500 @2x = 1280x1000`. A larger `size` returns HTTP 400.
- **URL length limit ~8KB.** Long routes can exceed it; the scripts check `len(url)` and abort with a clear error. Percent-encoding every value (which the scripts do) keeps the URL valid but does NOT make an over-length URL legal.
- **Routes API FieldMask is required** — omitting it returns HTTP 400. Use `routes.polyline.encodedPolyline,routes.distanceMeters,routes.duration`.
- **Waypoints are NOT supported in Routes API v2** (returns `INVALID_ARGUMENT`). For corridor-forcing waypoints, use the **Directions API** (`https://maps.googleapis.com/maps/api/directions/json`, `&waypoints=`). See the `sg-commute` skill.
- **DRIVE** mode is the most reliable; TRANSIT can over-report duration.
- **API key scope:** a geocoding-only key returns `PERMISSION_DENIED` for `computeRoutes` — enable "Routes API" in the GCP Console. If Routes is blocked, fall back to the [maps skill (OSRM)](../maps/SKILL.md) for routing, then Geocoding + Static Maps.
- **Never hardcode the key** — it comes from `GOOGLE_API_KEY`. Rotate any key that was ever committed.

## Output

- PNG saved to the output path (default `/tmp/route_map.png`), `1280×1000` (`640×500` at `scale=2`).
- Blue route polyline (`0x1E88E5`, weight 5); green **H** marker at origin, red **W** marker at destination.
