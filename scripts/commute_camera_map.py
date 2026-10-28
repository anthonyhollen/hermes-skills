#!/usr/bin/env python3
"""
Generate a route map with traffic cameras pinned along the path.

Usage:
    python3 commute_camera_map.py --start "Pasir Ris Grove" --end "2 Fusionopolis Way"
    python3 commute_camera_map.py --start "518141" --end "138634"
    python3 commute_camera_map.py --start "Home" --end "Changi Airport"
    python3 commute_camera_map.py  # defaults: Home → Innovis
"""

import argparse
import json
import math
import os
import sys
import urllib.request
import urllib.parse

SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
sys.path.insert(0, SCRIPT_DIR)
from traffic_images import fetch_cameras, parse_cameras
from traffic_cameras_map import geocode_cameras

def _load_env_key(name):
    """Load a key from environment or ~/.hermes/.env file."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

GOOGLE_KEY = _load_env_key("GOOGLE_API_KEY") or _load_env_key("GOOGLE_MAPS_API_KEY")

# Default locations
DEFAULT_START = "Pasir Ris Grove, Singapore 518141"
DEFAULT_END = "2 Fusionopolis Way, Singapore 138634"

# Known shortcuts
SHORTCUTS = {
    "home": DEFAULT_START,
    "innovis": DEFAULT_END,
}


def resolve_address(addr):
    """Resolve shortcut names to full addresses."""
    return SHORTCUTS.get(addr.lower().strip(), addr)


def get_route(origin, destination, waypoints=None):
    """Fetch route from Google Directions API. Returns (polyline, dist_km, dur_min, legs)."""
    url = (
        f"https://maps.googleapis.com/maps/api/directions/json"
        f"?origin={urllib.parse.quote(origin)}"
        f"&destination={urllib.parse.quote(destination)}"
        f"&key={GOOGLE_KEY}"
    )
    if waypoints:
        url += f"&waypoints={urllib.parse.quote(waypoints)}"

    req = urllib.request.Request(url, headers={"User-Agent": "hermes/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.request.URLError, urllib.request.HTTPError, OSError) as e:
        print(f"Directions API error: {e}", file=sys.stderr)
        sys.exit(1)

    if data["status"] != "OK":
        print(f"Directions API error: {data['status']}", file=sys.stderr)
        if "error_message" in data:
            print(f"  {data['error_message']}", file=sys.stderr)
        sys.exit(1)

    if not data.get("routes"):
        print("Directions API error: no routes found", file=sys.stderr)
        sys.exit(1)
    route = data["routes"][0]
    polyline = route["overview_polyline"]["points"]
    legs = route["legs"]
    dist_km = sum(l["distance"]["value"] for l in legs) / 1000
    dur_min = sum(l["duration"]["value"] for l in legs) // 60

    # Get start/end addresses and coordinates from response
    start_addr = legs[0]["start_address"]
    end_addr = legs[-1]["end_address"]
    start_loc = legs[0]["start_location"]
    end_loc = legs[-1]["end_location"]

    return polyline, dist_km, dur_min, start_addr, end_addr, start_loc, end_loc


def decode_polyline(enc):
    """Google encoded polyline → [(lat, lon), ...]"""
    pts, idx, lat, lon = [], 0, 0, 0
    while idx < len(enc):
        for coord_idx in range(2):  # 0=lat, 1=lon
            result, shift = 0, 0
            while True:
                b = ord(enc[idx]) - 63
                idx += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if coord_idx == 0:
                lat += d
            else:
                lon += d
        pts.append((lat / 1e5, lon / 1e5))
    return pts


def haversine_km(lat1, lon1, lat2, lon2):
    """Haversine distance in km."""
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_cameras_along_route(route_pts, cameras, threshold_m=500):
    """Find cameras within threshold_m of the route polyline, sorted by position along route."""
    along = []
    for cam in cameras:
        if cam["latitude"] is None or cam["longitude"] is None:
            continue
        # Find nearest route point (sample every 5th for speed)
        sampled = route_pts[::5]
        best_dist, best_idx = float("inf"), 0
        for idx, rp in enumerate(sampled):
            d = haversine_km(cam["latitude"], cam["longitude"], rp[0], rp[1]) * 1000
            if d < best_dist:
                best_dist, best_idx = d, idx
        if best_dist <= threshold_m:
            cam["_dist_m"] = best_dist
            cam["_route_idx"] = best_idx  # position along route
            along.append(cam)
    # Sort by position along route (start → end)
    along.sort(key=lambda c: c["_route_idx"])
    return along


def build_map_url(polyline_enc, cameras, start_lat, start_lon, end_lat, end_lon,
                  api_key, size="700x500"):
    """Build Static Map URL with route polyline and labeled camera markers."""
    # Calculate center from route, INCLUDING start/end markers (they can be
    # slightly outside the polyline extent and get clipped at the map edge)
    route_pts = decode_polyline(polyline_enc)
    all_lats = [p[0] for p in route_pts] + [start_lat, end_lat]
    all_lons = [p[1] for p in route_pts] + [start_lon, end_lon]
    center_lat = (min(all_lats) + max(all_lats)) / 2
    center_lon = (min(all_lons) + max(all_lons)) / 2

    # Auto zoom based on span (no padding — we adjust center instead)
    lat_span = max(all_lats) - min(all_lats)
    lon_span = max(all_lons) - min(all_lons)
    span = max(lat_span, lon_span)
    if span > 0.5:
        zoom = 10
    elif span > 0.2:
        zoom = 11
    elif span > 0.1:
        zoom = 12
    elif span > 0.05:
        zoom = 13
    else:
        zoom = 14

    # Adjust center so start/end markers aren't clipped at the map edge.
    # At this zoom, calculate visible degrees from the map pixel size.
    map_w, map_h = (int(x) for x in size.split("x"))
    deg_per_px = 360 / (2 ** zoom * 256)
    visible_lat = map_h * deg_per_px
    visible_lon = map_w * deg_per_px
    # Margin = 5% of viewport (enough for the marker icon)
    margin_lat = visible_lat * 0.05
    margin_lon = visible_lon * 0.05

    # If the content (all extreme points + margins) doesn't fit in the viewport,
    # drop one zoom level so there's room for everything.
    content_lat = (max(all_lats) - min(all_lats)) + 2 * margin_lat
    content_lon = (max(all_lons) - min(all_lons)) + 2 * margin_lon
    if content_lat > visible_lat or content_lon > visible_lon:
        zoom -= 1
        deg_per_px = 360 / (2 ** zoom * 256)
        visible_lat = map_h * deg_per_px
        visible_lon = map_w * deg_per_px
        margin_lat = visible_lat * 0.05
        margin_lon = visible_lon * 0.05

    # Shift center if any extreme point is too close to a viewport edge
    top_edge = center_lat + visible_lat / 2
    bot_edge = center_lat - visible_lat / 2
    if max(all_lats) + margin_lat > top_edge:
        center_lat += (max(all_lats) + margin_lat - top_edge)
    elif min(all_lats) - margin_lat < bot_edge:
        center_lat -= (bot_edge - (min(all_lats) - margin_lat))
    right_edge = center_lon + visible_lon / 2
    left_edge = center_lon - visible_lon / 2
    if max(all_lons) + margin_lon > right_edge:
        center_lon += (max(all_lons) + margin_lon - right_edge)
    elif min(all_lons) - margin_lon < left_edge:
        center_lon -= (left_edge - (min(all_lons) - margin_lon))

    url = (
        f"https://maps.googleapis.com/maps/api/staticmap"
        f"?center={center_lat:.5f},{center_lon:.5f}"
        f"&zoom={zoom}&size={size}&maptype=roadmap&format=png32"
        # Green polyline route
        f"&path=color:0x00C853|weight:5|enc:{urllib.parse.quote(polyline_enc, safe='')}"
    )

    # Camera markers first (rendered below S/E so they don't cover them)
    for i, c in enumerate(cameras, 1):
        if c.get("latitude") is None or c.get("longitude") is None:
            continue
        ml = marker_label(i)
        label_part = f"label:{ml}%7C" if ml else ""
        url += f"&markers=color:red%7C{label_part}{c['latitude']:.6f},{c['longitude']:.6f}"

    # Start/End markers LAST so they render on top of camera markers
    # Use size:mid for better visibility (default is "normal" which can be small)
    # Start marker — orange/red (green blends with Google Maps park/POI icons)
    url += f"&markers=size:mid%7Ccolor:orange%7Clabel:S%7C{start_lat:.6f},{start_lon:.6f}"
    # End marker (blue E)
    url += f"&markers=size:mid%7Ccolor:blue%7Clabel:E%7C{end_lat:.6f},{end_lon:.6f}"

    url += f"&key={api_key}"
    return url


def marker_label(i):
    """1-based index → marker label: 1-9, A-Z (10-35), '' beyond.
    Matches build_map_url so the table label and the map marker agree."""
    if i <= 9:
        return str(i)
    if i <= 35:
        return chr(ord("A") + i - 10)
    return ""


def main():
    parser = argparse.ArgumentParser(description="Route map with traffic cameras")
    parser.add_argument("--start", default=DEFAULT_START, help="Start address or shortcut")
    parser.add_argument("--end", default=DEFAULT_END, help="End address or shortcut")
    parser.add_argument("--waypoint", help="Optional waypoint (e.g. 'via:1.292,103.885')")
    parser.add_argument("--threshold", type=int, default=500, help="Max camera distance from route (meters)")
    parser.add_argument("--output", default="/tmp/commute_cameras.png", help="Output image path")
    parser.add_argument("--no-geocode", action="store_true", help="Skip reverse-geocoding camera addresses")
    parser.add_argument("--api-key", default=GOOGLE_KEY)
    args = parser.parse_args()

    # Resolve shortcuts
    start = resolve_address(args.start)
    end = resolve_address(args.end)

    print(f"Start: {start}")
    print(f"End:   {end}")

    # Get route
    polyline, dist_km, dur_min, start_addr, end_addr, start_loc, end_loc = get_route(start, end, args.waypoint)
    print(f"Route: {dist_km:.1f} km, ~{dur_min} min")

    # Use start/end coordinates from Directions response (no re-geocoding needed)
    if not (start_loc and "lat" in start_loc and "lng" in start_loc
            and end_loc and "lat" in end_loc and "lng" in end_loc):
        print("Error: Directions response missing start/end coordinates", file=sys.stderr)
        sys.exit(1)
    start_lat, start_lon = start_loc["lat"], start_loc["lng"]
    end_lat, end_lon = end_loc["lat"], end_loc["lng"]

    # Decode route polyline
    route_pts = decode_polyline(polyline)
    print(f"Route points: {len(route_pts)}")

    # Find cameras along route
    raw = fetch_cameras()
    cameras = parse_cameras(raw)
    along = find_cameras_along_route(route_pts, cameras, args.threshold)
    print(f"Cameras within {args.threshold}m: {len(along)}")

    # Reverse-geocode each camera's street address (cached) unless skipped
    if along and not args.no_geocode:
        cache_path = os.path.join(
            os.path.dirname(os.path.abspath(args.output)), "geocode_cache.json")
        if not cache_path:
            cache_path = "/tmp/geocode_cache.json"
        print("Reverse geocoding cameras along route...")
        geocode_cameras(along, args.api_key, cache_path)

    # Table: # | CamID | Dist | Address
    print()
    print(f"{'#':>2}  {'CamID':>6}  {'Dist':>6}  Address")
    for i, c in enumerate(along, 1):
        addr = (c.get("address") or "").strip() or f"{c['latitude']:.5f},{c['longitude']:.5f}"
        print(f"{marker_label(i):>2}  {c['camera_id']:>6}  {c['_dist_m']:>5.0f}m  {addr}")

    # Build and download map
    url = build_map_url(polyline, along, start_lat, start_lon, end_lat, end_lon,
                        args.api_key)
    print(f"URL length: {len(url)} chars")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            print(f"Error: response is not a PNG image", file=sys.stderr)
            sys.exit(1)
        with open(args.output, "wb") as f:
            f.write(data)
        print(f"Saved: {args.output} ({os.path.getsize(args.output):,} bytes)")
    except Exception as e:
        print(f"Error downloading map: {e}", file=sys.stderr)
        sys.exit(1)

    # Output result JSON for programmatic use
    result = {
        "start": start_addr,
        "end": end_addr,
        "distance_km": round(dist_km, 1),
        "duration_min": dur_min,
        "cameras_count": len(along),
        "cameras": [
            {"label": marker_label(i),
             "camera_id": c["camera_id"],
             "distance_m": round(c["_dist_m"]),
             "address": c.get("address", ""),
             "latitude": c["latitude"],
             "longitude": c["longitude"]}
            for i, c in enumerate(along, 1)
        ],
        "output": args.output,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
