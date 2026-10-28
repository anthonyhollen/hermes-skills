# Filtering Cameras Along a Commute Route

Find traffic cameras near a Google Directions polyline — for overlaying cameras on a route map.

## Pattern

1. **Get route** via Google Directions API with waypoint (`via:` coordinate, not address)
2. **Decode polyline** → list of (lat, lon) points
3. **For each camera**, compute min haversine distance to any route point
4. **Keep cameras** within threshold (500m recommended)
5. **Sort by distance** and label for map

## Polyline Decoder

Google's encoded polyline format: pairs of (lat, lon) deltas, each varint-encoded.

```python
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
```

⚠️ The decoder iterates `coord_idx in range(2)` — first iteration decodes lat delta, second decodes lon delta. Don't use `shift` to distinguish lat/lon (unreliable).

## Distance Check

```python
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# For each camera, check distance to sampled route points
along = []
for cam in cameras:
    min_dist = min(
        haversine_km(cam["lat"], cam["lon"], rp[0], rp[1]) * 1000
        for rp in route_pts[::5]  # sample every 5th point for speed
    )
    if min_dist <= 500:  # 500m threshold
        along.append(cam)
```

## Static Map with Route + Camera Markers

```python
# Route polyline + labeled camera markers on one map
url = (f"https://maps.googleapis.com/maps/api/staticmap"
       f"?center={center}&zoom=12&size=700x500&maptype=roadmap"
       f"&format=png32"
       f"&path=color:0x1565C0|weight:5|enc:{polyline_enc}")
# Home (green H) and destination (blue W)
url += "&markers=color:green%7Clabel:H%7C{home_lat},{home_lon}"
url += "&markers=color:blue%7Clabel:W%7C{dest_lat},{dest_lon}"
# Camera markers (labeled 1-9, A-Z)
for i, cam in enumerate(cameras, 1):
    label = str(i) if i <= 9 else chr(ord("A") + i - 10)
    url += f"&markers=color:red%7Clabel:{label}%7C{cam['lat']:.6f},{cam['lon']:.6f}"
url += f"&key={api_key}"
```

⚠️ Use `format=png32` — PNG8 drops polylines in Discord's image pipeline.

## Verified Results (2026-06-08)

Home→Innovis route (35.7 km via TPE→PIE→ECP→MCE→AYE): **18 cameras** within 500m. Camera 1111 (Pasir Ris St 11) was closest at 28m from route.
