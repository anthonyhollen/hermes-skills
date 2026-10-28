#!/usr/bin/env python3
"""
Fetch Singapore traffic camera images from Data.gov.sg API.

Usage:
    python3 traffic_images.py                    # all cameras, JSON output
    python3 traffic_images.py --bbox 1.30,103.75,1.45,103.90  # filter by bounding box
    python3 traffic_images.py --lat 1.35 --lon 103.85 --radius 2  # filter by radius (km)
    python3 traffic_images.py --download /tmp/traffic  # download images to dir
    python3 traffic_images.py --camera 2701     # single camera by ID
"""

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta


API_URL = "https://api.data.gov.sg/v1/transport/traffic-images"
SGT = timezone(timedelta(hours=8))


def fetch_cameras(date_time=None, api_key=None):
    """Fetch traffic camera data from Data.gov.sg API."""
    url = API_URL
    params = {}
    if date_time:
        params["date_time"] = date_time
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    headers = {"User-Agent": "hermes-traffic-images/1.0"}
    if api_key:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.request.URLError, urllib.request.HTTPError, OSError) as e:
        print(f"Error fetching traffic cameras: {e}", file=sys.stderr)
        sys.exit(1)

    if "items" not in data or not data["items"]:
        print("No data returned from API.", file=sys.stderr)
        sys.exit(1)

    return data


def haversine_km(lat1, lon1, lat2, lon2):
    """Haversine distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_cameras(raw_data):
    """Extract flat list of camera dicts from API response."""
    cameras = []
    for item in raw_data.get("items", []):
        ts = item.get("timestamp", "")
        for cam in item.get("cameras", []):
            loc = cam.get("location", {})
            meta = cam.get("image_metadata", {})
            cameras.append({
                "camera_id": cam.get("camera_id"),
                "image_id": cam.get("image_id"),
                "timestamp": cam.get("timestamp", ts),
                "image_url": cam.get("image", ""),
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
                "width": meta.get("width"),
                "height": meta.get("height"),
                "md5": meta.get("md5"),
            })
    return cameras


def filter_bbox(cameras, south, west, north, east):
    """Keep cameras within a bounding box (lat/lon)."""
    return [c for c in cameras
            if c["latitude"] is not None and c["longitude"] is not None and
            south <= c["latitude"] <= north and
            west <= c["longitude"] <= east]


def filter_radius(cameras, lat, lon, radius_km):
    """Keep cameras within radius_km of a point."""
    return [c for c in cameras
            if c["latitude"] is not None and c["longitude"] is not None and
            haversine_km(lat, lon, c["latitude"], c["longitude"]) <= radius_km]


def download_images(cameras, output_dir):
    """Download camera images to output_dir, named by camera_id."""
    os.makedirs(output_dir, exist_ok=True)
    downloaded = []
    for cam in cameras:
        url = cam["image_url"]
        if not url:
            continue
        fname = f"{cam['camera_id']}.jpg"
        fpath = os.path.join(output_dir, fname)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.data.gov.sg/",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(fpath, "wb") as f:
                    f.write(resp.read())
            downloaded.append({"camera_id": cam["camera_id"], "path": fpath})
        except Exception as e:
            print(f"Warning: failed to download camera {cam['camera_id']}: {e}", file=sys.stderr)
    return downloaded


def main():
    parser = argparse.ArgumentParser(description="Fetch Singapore traffic camera images")
    parser.add_argument("--date-time", help="SGT datetime (YYYY-MM-DDTHH:mm:ss)")
    parser.add_argument("--api-key", help="Optional API key for higher rate limits")
    parser.add_argument("--bbox", help="Bounding box: south,west,north,east (lat,lon)")
    parser.add_argument("--lat", type=float, help="Center latitude for radius filter")
    parser.add_argument("--lon", type=float, help="Center longitude for radius filter")
    parser.add_argument("--radius", type=float, default=2.0, help="Radius in km (default: 2)")
    parser.add_argument("--camera", help="Single camera ID to fetch")
    parser.add_argument("--download", help="Download images to directory")
    parser.add_argument("--json", action="store_true", help="Output raw JSON (default)")
    parser.add_argument("--summary", action="store_true", help="Output human-readable summary")
    args = parser.parse_args()

    raw = fetch_cameras(date_time=args.date_time, api_key=args.api_key)
    cameras = parse_cameras(raw)

    api_ts = (raw.get("items") or [{}])[0].get("timestamp", "unknown") if raw.get("items") else "unknown"
    api_status = raw.get("api_info", {}).get("status", "unknown")

    # Apply filters
    if args.bbox:
        parts = [float(x) for x in args.bbox.split(",")]
        if len(parts) != 4:
            print("Error: --bbox needs 4 values: south,west,north,east", file=sys.stderr)
            sys.exit(1)
        cameras = filter_bbox(cameras, *parts)

    if args.lat is not None and args.lon is not None:
        cameras = filter_radius(cameras, args.lat, args.lon, args.radius)

    if args.camera:
        cameras = [c for c in cameras if str(c["camera_id"]) == str(args.camera)]

    # Download if requested
    downloads = []
    if args.download:
        downloads = download_images(cameras, args.download)

    # Output
    result = {
        "api_timestamp": api_ts,
        "api_status": api_status,
        "total_cameras": len(cameras),
        "cameras": cameras,
    }
    if downloads:
        result["downloads"] = downloads

    if args.summary:
        print(f"Traffic Images — {api_ts}")
        print(f"Status: {api_status} | Cameras: {len(cameras)}")
        print()
        for i, c in enumerate(cameras, 1):
            lat = f"{c['latitude']:.6f}" if c.get("latitude") is not None else "?"
            lon = f"{c['longitude']:.6f}" if c.get("longitude") is not None else "?"
            w = c.get("width") or "?"
            h = c.get("height") or "?"
            print(f"  {i:>3}. cam={c['camera_id']}  ({lat}, {lon})  "
                  f"{w}x{h}  {c['timestamp']}")
        if downloads:
            print(f"\nDownloaded {len(downloads)} images to {args.download}")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
