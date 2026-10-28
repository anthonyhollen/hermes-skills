# Google Static Maps Numbered Markers

## Correct approach: `label` parameter

Use Google's built-in `label` parameter with **one `&markers=` param per marker**:

```python
for i, c in enumerate(cameras, 1):
    if i <= 9:
        label_part = f"label:{i}%7C"
    elif i <= 35:
        label_part = f"label:{chr(ord('A') + i - 10)}%7C"
    else:
        label_part = ""          # >35: Google labels are single alphanumeric — place unlabeled
    url += f"&markers=color:red%7C{label_part}{c['latitude']:.6f},{c['longitude']:.6f}"
url += f"&key={api_key}"
```

**Why separate params:** Grouping multiple labeled markers in a single `markers=` param causes all to render with the same label (Google uses the first label for all markers in a group).

## Label mapping
- 1-9: digits `1` through `9`
- 10-35: letters `A` (10), `B` (11), ..., `Z` (35)
- Max **35** labeled markers per map. `chr(ord("A") + i - 10)` at index ≥36 falls past `Z` into `[`, `\`, … (an invalid label that breaks the marker), so the code places index >35 **unlabeled**. A single region tops out ≈29 cameras, so this is a guard, not the normal case.

## Failed approaches (do not retry)

| Approach | Result |
|----------|--------|
| KML paddle icons (`maps.google.com/mapfiles/kml/paddle/{N}.png`) | Renders inconsistently — 23/29 visible, some unlabeled |
| Data URIs (`data:image/png;base64,...`) | 400 Bad Request |
| Google Charts API (`chart.googleapis.com/chart?chst=d_map_pin_letter`) | Deprecated, 404 |
| Multiple different `icon:` in one `markers=` param | All render with first icon |
| Custom Pillow-generated markers as data URIs | Not supported by Google Static Maps API |

## Marker rendering order (critical)

Google Static Maps renders markers **in the order they appear in the URL** — later markers draw on top of earlier ones. If start/end markers (S/E) are added *before* camera markers (1-9, A-Z), the camera markers render on top and hide S/E.

**Correct order in URL:**
1. `&path=` (route polyline)
2. `&markers=color:red|label:1|...` ... camera markers first
3. `&markers=size:mid|color:orange|label:S|...` start marker **last** (on top)
4. `&markers=size:mid|color:blue|label:E|...` end marker **last** (on top)

Use `size:mid` for S/E markers to ensure they're visible even without overlap.

**Start marker color:** Use `orange` (not green) — green blends with Google Maps' own park/POI icons, making the S marker invisible in nature-heavy areas like Bukit Timah.

## Viewport clipping (critical)

Google Static Maps zoom is **integer-only** (0–21). The marker icon is ~30-40px tall; at zoom 13 that's ~600-760m. If a marker sits within that distance of the map edge, it gets clipped silently.

**Common trigger:** A route polyline that fills >90% of the viewport. The start/end marker coordinates (especially geocoded postal codes) may sit at the polyline's extreme, leaving zero margin.

**Do NOT fix with padding multipliers** (e.g. `span * 1.3`) — zoom is discrete, so padding just flips between two levels. The user sees either "too zoomed in" (markers clipped) or "too zoomed out" (too much whitespace) with no in-between.

**Correct fix (three parts):**
1. Include start/end coords in the `all_lats`/`all_lons` lists used for center/zoom calculation.
2. After choosing zoom, compute actual viewport size: `deg_per_px = 360 / (2^zoom * 256)`, `visible_lat = height * deg_per_px`. If content (extreme points + 5% margin each side) exceeds viewport, **auto-downgrade zoom by 1**.
3. Then shift center if any extreme point is still within 5% of a viewport edge.

## Single marker (for "send camera X" workflow)

```python
url = f"https://maps.googleapis.com/maps/api/staticmap?center={lat},{lon}&zoom=15&size=640x480&maptype=roadmap&markers=color:red%7Clabel:{label}%7C{lat},{lon}&key={api_key}"
```
