#!/usr/bin/env python3
"""
Generate a route map PNG end-to-end from two addresses.

The encoded polyline is fetched, used, and discarded entirely inside this process, so it
never has to be passed back through the caller (e.g. an LLM's context / token budget) — the
caller only provides two addresses and receives a one-line "Saved ..." result.

Usage: python3 route_map.py "<origin>" "<destination>" [output_path]
Example: python3 route_map.py "Pasir Ris Grove, Singapore" "12 Ang Mo Kio Street 64" /tmp/route_map.png

Requires GOOGLE_API_KEY with BOTH "Routes API" and "Static Maps API" enabled on the key.
"""
import json, sys, urllib.request, urllib.error
from download_static_map import get_api_key, make_static_map_url, download_map

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def compute_route(origin, destination, api_key):
    """Call Routes API v2 and return (encoded_polyline, distance_meters, duration_str)."""
    body = json.dumps({
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "DRIVE",
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # FieldMask is REQUIRED by Routes API — omitting it returns HTTP 400.
        "X-Goog-FieldMask": "routes.polyline.encodedPolyline,routes.distanceMeters,routes.duration",
    }
    req = urllib.request.Request(ROUTES_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        sys.exit(f"ERROR: Routes API returned HTTP {e.code} {e.reason}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not reach Routes API: {e.reason}")

    routes = result.get("routes")
    if not routes:
        sys.exit(f"ERROR: Routes API returned no route: {json.dumps(result)[:300]}")
    route = routes[0]
    return route["polyline"]["encodedPolyline"], route.get("distanceMeters"), route.get("duration")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    api_key = get_api_key()
    origin = sys.argv[1]
    destination = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) > 3 else "/tmp/route_map.png"

    polyline, distance_m, duration = compute_route(origin, destination, api_key)
    if distance_m is not None:
        print(f"Route: {distance_m / 1000:.1f} km, {duration}")

    url = make_static_map_url(polyline, origin, destination, api_key)
    download_map(url, output_path)
