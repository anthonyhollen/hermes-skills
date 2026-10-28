#!/usr/bin/env python3
"""
Fetch Singapore traffic cameras, reverse geocode addresses, and plot on
Google Static Maps split into 4 regions: North, South, East, West.

Usage:
    python3 traffic_cameras_map.py                     # all 4 maps + tables
    python3 traffic_cameras_map.py --output /tmp/maps  # custom output dir
    python3 traffic_cameras_map.py --no-geocode        # skip geocoding (faster)
    python3 traffic_cameras_map.py --region east        # single region only
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from traffic_images import fetch_cameras, parse_cameras

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

# ─── Region definitions (non-overlapping) ───────────────────────────
# North:  lat >= 1.38
# South:  lat <  1.28
# East:   1.28 <= lat < 1.38, lon >= 103.88
# West:   1.28 <= lat < 1.38, lon <  103.80
# Center gap (1.28-1.38, 103.80-103.88) → assigned to nearest region

CENTER_GAP = {
    "lat_min": 1.28, "lat_max": 1.38,
    "lon_min": 103.80, "lon_max": 103.88,
}

REGION_CENTERS = {
    "north": (1.43, 103.80),
    "south": (1.22, 103.84),
    "east":  (1.33, 103.96),
    "west":  (1.33, 103.70),
}


def assign_region(lat, lon):
    """Assign a camera to exactly one region. Returns region short name."""
    if lat >= 1.38:
        return "north"
    elif lat < 1.28:
        return "south"
    elif lat >= 1.28 and lat < 1.38:
        if lon >= 103.88:
            return "east"
        elif lon < 103.80:
            return "west"
        else:
            # Center gap — assign to nearest region center
            best, best_d = None, float("inf")
            for name, (rlat, rlon) in REGION_CENTERS.items():
                d = math.hypot(lat - rlat, lon - rlon)
                if d < best_d:
                    best, best_d = name, d
            return best
    return "unknown"


def group_cameras(cameras):
    """Group cameras into regions. Returns dict of short_name → [camera]."""
    groups = {"north": [], "south": [], "east": [], "west": [], "unknown": []}
    for c in cameras:
        lat, lon = c["latitude"], c["longitude"]
        if lat is None or lon is None:
            groups["unknown"].append(c)
            continue
        region = assign_region(lat, lon)
        groups[region].append(c)
    return groups


def auto_center_and_zoom(cameras):
    """Calculate map center and zoom from camera positions."""
    if not cameras:
        return "1.35,103.82", 11
    lats = [c["latitude"] for c in cameras if c["latitude"] is not None]
    lons = [c["longitude"] for c in cameras if c["longitude"] is not None]
    if not lats:
        return "1.35,103.82", 11
    lat_c = (min(lats) + max(lats)) / 2
    lon_c = (min(lons) + max(lons)) / 2
    lat_span = max(lats) - min(lats)
    lon_span = max(lons) - min(lons)
    # Pick zoom based on span (for 640x480 map)
    span = max(lat_span, lon_span)
    if span > 0.15:
        zoom = 11
    elif span > 0.08:
        zoom = 12
    elif span > 0.04:
        zoom = 13
    else:
        zoom = 14
    return f"{lat_c:.5f},{lon_c:.5f}", zoom


def reverse_geocode(lat, lon, api_key):
    """Google Geocoding API → (formatted_address, postal_code)."""
    url = (f"https://maps.googleapis.com/maps/api/geocode/json"
           f"?latlng={lat:.6f},{lon:.6f}&key={api_key}")
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-traffic/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "OK" or not data.get("results"):
            return None, None
        results = data["results"]
        # Prefer a real street address / named route over a bare Plus Code
        # (Google returns a plus_code as results[0] for some expressway points).
        result = next((r for r in results if "plus_code" not in r.get("types", [])), results[0])
        addr = result.get("formatted_address", "")
        postal = None
        for ac in result.get("address_components", []):
            if "postal_code" in ac.get("types", []):
                postal = ac["long_name"]
                break
        clean_addr = addr.replace(", Singapore", "").replace("Singapore", "").strip(", ")
        return clean_addr, postal
    except Exception:
        return None, None


def geocode_cameras(cameras, api_key, cache_path=None):
    """Reverse geocode all cameras."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, ValueError):
            print(f"  Warning: corrupt geocode cache, starting fresh", file=sys.stderr)
            cache = {}

    total = len(cameras)
    for i, c in enumerate(cameras):
        if c["latitude"] is None or c["longitude"] is None:
            continue
        key = f"{c['latitude']:.6f},{c['longitude']:.6f}"
        if key in cache:
            c["address"] = cache[key].get("address", "")
            c["postal"] = cache[key].get("postal", "")
            continue
        addr, postal = reverse_geocode(c["latitude"], c["longitude"], api_key)
        cache[key] = {"address": addr or "", "postal": postal or ""}
        c["address"] = addr or ""
        c["postal"] = postal or ""
        if (i + 1) % 10 == 0:
            print(f"  Geocoded {i+1}/{total}...")
        time.sleep(0.05)

    if cache_path:
        tmp = cache_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(cache, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cache_path)
        except Exception as e:
            # Don't leave a half-written .tmp behind; the live cache stays intact.
            print(f"  Warning: failed to persist geocode cache: {e}", file=sys.stderr)
            try:
                os.remove(tmp)
            except OSError:
                pass
    return cache


def marker_label(i):
    """1-based index → marker label: 1-9, A-Z (10-35), '' beyond.
    Matches build_map_url so the table label and the map marker agree."""
    if i <= 9:
        return str(i)
    if i <= 35:
        return chr(ord("A") + i - 10)
    return ""


def build_map_url(cameras, center, zoom, api_key, size="640x480", numbered=True):
    """Build Google Static Maps URL with labeled markers.
    Each marker gets its own &markers= param with a label (sg-commute pattern)."""
    url = (f"https://maps.googleapis.com/maps/api/staticmap"
           f"?center={center}&zoom={zoom}"
           f"&size={size}&maptype=roadmap")

    if numbered:
        for i, c in enumerate(cameras, 1):
            if c.get("latitude") is None or c.get("longitude") is None:
                continue  # can't place a marker without coordinates
            ml = marker_label(i)
            label_part = f"label:{ml}%7C" if ml else ""
            url += f"&markers=color:red%7C{label_part}{c['latitude']:.6f},{c['longitude']:.6f}"
    else:
        # Simple red dots in one param
        markers = [f"color:red|{c['latitude']:.6f},{c['longitude']:.6f}" for c in cameras]
        url += f"&markers={urllib.parse.quote('|'.join(markers), safe='|=,')}"

    url += f"&key={api_key}"
    return url


def download_image(url, path):
    """Download map image. Returns file size or None on error."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Chrome/120.0.0.0",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        # Verify PNG magic bytes
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            print(f"  Warning: response is not a PNG image ({len(data)} bytes)", file=sys.stderr)
            return None
        with open(path, "wb") as f:
            f.write(data)
        return os.path.getsize(path)
    except Exception as e:
        print(f"  Error downloading map: {e}", file=sys.stderr)
        return None


def print_table(region_name, cameras):
    """Print a formatted camera table."""
    print(f"\n{'='*60}")
    print(f"  {region_name} — {len(cameras)} cameras")
    print(f"{'='*60}")
    print(f"{'#':>3}  {'CamID':>6}  {'Postal':>7}  {'Lat':>10}  {'Lon':>11}  Address")
    print(f"{'-'*3}  {'-'*6}  {'-'*7}  {'-'*10}  {'-'*11}  {'-'*30}")
    for i, c in enumerate(cameras, 1):
        lat = f"{c['latitude']:.5f}" if c.get("latitude") is not None else "?"
        lon = f"{c['longitude']:.5f}" if c.get("longitude") is not None else "?"
        label = marker_label(i)
        postal = c.get("postal", "") or ""
        addr = c.get("address", "") or ""
        if len(addr) > 40:
            addr = addr[:37] + "..."
        print(f"{label:>3}  {c['camera_id']:>6}  {postal:>7}  {lat:>10}  {lon:>11}  {addr}")


REGION_NAMES = {
    "north": "North Singapore",
    "south": "South Singapore",
    "east": "East Singapore",
    "west": "West Singapore",
}


def main():
    parser = argparse.ArgumentParser(description="Traffic cameras on Google Static Maps")
    parser.add_argument("--output", default="/tmp/traffic_maps")
    parser.add_argument("--date-time", help="SGT datetime (YYYY-MM-DDTHH:mm:ss)")
    parser.add_argument("--no-geocode", action="store_true", help="Skip reverse geocoding")
    parser.add_argument("--region", choices=["north", "south", "east", "west"], help="Single region only")
    parser.add_argument("--api-key", default=GOOGLE_KEY)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    cache_path = os.path.join(args.output, "geocode_cache.json")

    # Fetch
    raw = fetch_cameras(date_time=args.date_time)
    cameras = parse_cameras(raw)
    ts = (raw.get("items") or [{}])[0].get("timestamp", "unknown") if raw.get("items") else "unknown"
    print(f"Fetched {len(cameras)} cameras @ {ts}")

    # Geocode
    if not args.no_geocode:
        print("Reverse geocoding camera locations...")
        geocode_cameras(cameras, args.api_key, cache_path)
        print("Done.\n")

    # Group by region
    groups = group_cameras(cameras)

    # Generate maps + tables
    regions_to_run = [args.region] if args.region else ["north", "south", "east", "west"]
    all_results = []

    for short in regions_to_run:
        cams = groups.get(short, [])
        name = REGION_NAMES.get(short, short.title())
        print(f"\n{name}: {len(cams)} cameras")

        print_table(name, cams)

        if cams:
            center, zoom = auto_center_and_zoom(cams)
            url = build_map_url(cams, center, zoom, args.api_key)
            out = os.path.join(args.output, f"traffic_{short}.png")
            sz = download_image(url, out)
            if sz:
                print(f"\n  Map saved: {out} ({sz:,} bytes)")
                all_results.append({"region": name, "cameras": len(cams), "path": out})
            else:
                print(f"\n  Map download failed for {name}")
        else:
            print("  (no cameras)")

    # Summary
    total = sum(r["cameras"] for r in all_results)
    print(f"\n{'='*60}")
    print(f"  SUMMARY — {total} cameras total")
    print(f"{'='*60}")
    for r in all_results:
        print(f"  {r['region']}: {r['cameras']} cameras → {r['path']}")

    return all_results


if __name__ == "__main__":
    main()
