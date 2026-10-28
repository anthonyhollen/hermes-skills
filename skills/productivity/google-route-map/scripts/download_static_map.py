#!/usr/bin/env python3
"""
Render a Google Static Map from an ALREADY-encoded route polyline.
Usage: python3 download_static_map.py <polyline> <origin_label> <dest_label> [output_path]
Example: python3 download_static_map.py "cwjGs{lyR..." "Pasir Ris Grove" "12 Ang Mo Kio Street 64" /tmp/route_map.png

The polyline comes from Routes API v2:computeRoutes response['routes'][0]['polyline']['encodedPolyline'].
For the end-to-end flow (addresses -> route -> map) use route_map.py, which keeps the long
polyline inside Python instead of round-tripping it through the caller.
The Google Maps API key is read from the GOOGLE_API_KEY environment variable.
"""
import os, sys, urllib.request, urllib.parse, urllib.error

# Static Maps standard tier caps `size` at 640 px per dimension; scale=2 doubles the
# output resolution for retina-sharp rendering. 640x500 @2x => 1280x1000 px (1280 is the
# hard width ceiling the standard API allows). format=png32 forces full RGB so the blue
# polyline overlay is not dropped by Discord's image pipeline (default PNG8 can misrender it).
MAX_URL_BYTES = 8192  # Static Maps rejects URLs longer than ~8KB.


def get_api_key():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("ERROR: set the GOOGLE_API_KEY environment variable (export GOOGLE_API_KEY=...).")
    return api_key


def make_static_map_url(polyline, origin_label, dest_label, api_key, size="640x500", scale=2):
    params = {
        "size": size,
        "scale": str(scale),
        "format": "png32",
        "markers": [
            f"color:green|label:H|{origin_label}",
            f"color:red|label:W|{dest_label}",
        ],
        "path": f"color:0x1E88E5|weight:5|enc:{polyline}",
        "key": api_key,
    }
    qs_parts = []
    for k, v in params.items():
        if isinstance(v, list):
            for item in v:
                qs_parts.append(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(item, safe='')}")
        else:
            qs_parts.append(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}")
    return "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(qs_parts)


def download_map(url, output_path):
    """Fetch a Static Maps URL, confirm it is a PNG, and write it. Exits with a clear error otherwise."""
    print(f"URL length: {len(url)} bytes")
    if len(url) > MAX_URL_BYTES:
        sys.exit(
            f"ERROR: URL is {len(url)} bytes, over the ~{MAX_URL_BYTES}-byte Static Maps limit. "
            "The route polyline is too long for a single request — simplify the polyline or use the "
            "OSRM/Directions fallback described in SKILL.md."
        )

    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()
        sys.exit(f"ERROR: Static Maps API returned HTTP {e.code} {e.reason}: {body}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not reach Static Maps API: {e.reason}")

    # Google can return HTTP 200 with a non-image error body; confirm the bytes are really a PNG.
    if not content_type.startswith("image/") or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        snippet = data[:200].decode("utf-8", "replace").strip()
        sys.exit(f"ERROR: response was not a PNG (Content-Type: {content_type or 'unknown'}): {snippet}")

    with open(output_path, "wb") as f:
        f.write(data)
    print(f"Saved {output_path}: {len(data)/1024:.0f} KB  (HTTP {status}, {content_type})")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    api_key = get_api_key()
    polyline = sys.argv[1]
    origin_label = sys.argv[2]
    dest_label = sys.argv[3]
    output_path = sys.argv[4] if len(sys.argv) > 4 else "/tmp/route_map.png"

    url = make_static_map_url(polyline, origin_label, dest_label, api_key)
    download_map(url, output_path)
