---
name: traffic-camera
description: Use when user asks about Singapore traffic cameras — list camera locations by region (North/South/East/West), show addresses/postal codes, or send live camera images with map locations to Discord.
version: 1.9.3
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [traffic, cameras, singapore, transport, maps]
    related_skills: [maps, sg-commute]
---

# Traffic Camera

Singapore traffic camera images from Data.gov.sg, with reverse geocoding and Google Static Maps.

> **⚠️ Service status (since 30 Jun 2026):** LTA discontinued most live camera feeds (ERP 2 transition). Only 8 cameras remain: Woodlands checkpoint area (2701/2702/2704), Tuas checkpoint area (4703/4712/4713), Sentosa Gateway (4798/4799). data.gov.sg `/traffic-images` and LTA DataMall `Traffic-Imagesv2` serve this same 8-camera set. A route query outside those corridors returning 0 cameras is EXPECTED — tell the user the feeds were discontinued 30 Jun 2026; don't re-investigate, don't force-lower `--threshold`. History via `?date_time=` still works (90 cameras through Jan 2026 → 8 by Aug 2026).

## When to Use

- "List traffic cameras" / "Show camera locations" → list cameras by region with addresses
- "Send camera 1005" / "Show cam 2701" → fetch live image + map, send to Discord
- "Send camera 3" / "Show cam B" → route-label lookup: cross-reference the label (1-9, A-Z) against the most recent route output to get the actual CamID, then proceed as above
- "How many cameras in East?" / "Cameras near Tampines" → filtered camera queries
- "Show cameras on my route to work" / "Cameras along commute" → use `commute_camera_map.py`

## Scripts

- `~/.hermes/scripts/traffic_images.py` — Fetch camera data from Data.gov.sg API (stdlib only)
- `~/.hermes/scripts/traffic_cameras_map.py` — Generate region maps with numbered markers, reverse geocoding
- `~/.hermes/scripts/commute_camera_map.py` — Route map with cameras pinned along commute path + reverse-geocoded `# / CamID / Dist / Address` table

## Workflow 1: List Cameras by Region

Run the all-in-one script (fetches, geocodes, groups, maps, prints tables):

```bash
python3 ~/.hermes/scripts/traffic_cameras_map.py --output /tmp/traffic_maps
# Single region: --region east | --region north | --region south | --region west
# Skip geocoding (faster): --no-geocode
```

Regions are non-overlapping lat/lon boundaries — see `references/region-boundaries.md`.

## Workflow 2: Send Specific Camera

When user names a CamID or route label (e.g. "camera 1005", "cam 2701", "camera E"):

1. If label (1-9, A-Z): resolve to CamID using the most recent route table. Run `commute_camera_map.py --start X --end Y --no-geocode` to get the JSON, then cross-reference.
2. Download the camera image + read lat/lon/timestamp using `traffic_images.py`:
   ```bash
   python3 ~/.hermes/scripts/traffic_images.py --camera 1005 --download /tmp
   ```
3. Build a single-camera Static Map URL (see `references/google-static-maps-numbered-markers.md` "Single marker" snippet) and download the PNG.
4. Send to Discord using **direct REST multipart** (`content` + `file` in one `curl` POST — see `references/delivery-pattern.md`). **Never** use `MEDIA:` tags or `hermes send` — `extract_media()` loses text↔image pairing.

### Deliver format (two messages)

**Message 1** — caption + camera image:
```
## 📸 Camera <label> (<CamID>) — <Dist>m from route
Image timestamp: <YYYY-MM-DD HH:MM> SGT
<camera feed image>
```

**Message 2** — map image:
```
📍 Camera location
<map image>
```

5. Delete temp files after confirmed delivery.

## Workflow 3: Cameras Along a Route

**Preferred: self-delivery.** Run one command — the script generates the map + camera table AND delivers both into the current thread itself.

```bash
# ALWAYS pass --thread-id with the current thread ID from session context.
# HERMES_SESSION_THREAD_ID can be stale for Discord auto-threads (gateway bug).
python3 ~/.hermes/scripts/traffic_camera_report.py --start "Home" --end "670238" \
    --thread-id <THREAD_ID_FROM_SESSION_CONTEXT>
#   --threshold 300   max camera distance from route (m, default 500)
#   --no-geocode      skip street-address lookup (faster)
```

`traffic_camera_report.py` runs the route pipeline, renders the `# / CamID / Dist / Address` table, and posts the map + table to **the thread you were asked in**. The `--thread-id` flag takes highest precedence and bypasses stale session env vars. **Do NOT call `send_message` or post the images yourself**: sending without a target falls back to the `#alerts` home channel, which is exactly the misfire this avoids. After it prints `[delivered] …`, just tell the user it's done.

**⚠️ FALLBACK: if the user says "nothing got delivered" or the script targeted the wrong thread/channel**, switch to manual delivery:

1. Run the underlying pipeline for data + map:
   ```bash
   python3 ~/.hermes/scripts/commute_camera_map.py --start "Home" --end "449308" --output /tmp/traffic_route
   ```
2. Generate the camera table image with Pillow (see `references/camera-table-render.md` or inline: dark-themed monospace table with columns `# | CamID | Dist | Address`).
3. Copy the map to `.png` extension: `cp /tmp/traffic_route /tmp/traffic_route.png`
4. Deliver using `send_message` with explicit thread target:
   ```
   send_message(target="discord:<channel_id>:<thread_id>", message="## 🚗 Traffic camera route map\n...\nMEDIA:/tmp/traffic_route.png")
   send_message(target="discord:<channel_id>:<thread_id>", message="### 📋 List of cameras along route\nMEDIA:/tmp/camera_table.png")
   ```
5. Clean up temp files.

**Why `--thread-id` is required:** See `references/discord-thread-delivery-bug.md` for the full root cause. Short version: gateway env vars can be stale for Discord auto-threads.

### Deliver format (two messages)

**Message 1** — header + route map:
```
## 🚗 Traffic camera route map
**<Origin>** → **<Destination>**  X.X km · ~Y min
📸 X cameras along route
<route map image>
```

**Message 2** — camera table:
```
### 📋 List of cameras along route
<table image>
```

**Shortcuts:** `home` → Pasir Ris Grove, `innovis` → 2 Fusionopolis Way. A bare carpark/postal code (e.g. `670238`) works directly; for a more precise endpoint, resolve it to a street address first with `~/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py "670238"`.

**Underlying pipeline (raw data, no delivery):** `commute_camera_map.py --start X --end Y [--no-geocode]` prints the camera JSON (each with `address`) and saves the map PNG to `--output`; markers are orange **S** (start), blue **E** (end), red **1-9/A-Z** cameras sorted along the route. `traffic_camera_report.py` wraps this — call `commute_camera_map.py` directly only when you need the JSON without sending anything.

## Common Pitfalls

1. **API key must be set in env.** Scripts load `GOOGLE_API_KEY` from `~/.hermes/.env` (or environment). Running from a plain terminal that doesn't source `.env` will fail; the `_load_env_key` helper reads the `.env` file directly as fallback.
8. **Clipped start/end markers.** Google Static Maps zoom is integer-only (0-21). When a route fills >90% of the viewport, edge markers get clipped. `commute_camera_map.py` handles this with viewport-aware center shifting + auto-downgrade zoom. Start marker uses orange (green blends with park POI icons). See `build_map_url()` for the logic.
2. **Stale thread targeting (gateway bug).** `HERMES_SESSION_THREAD_ID` and `HERMES_SESSION_KEY` can both contain the parent channel ID instead of the actual thread ID for Discord auto-threads. Always pass `--thread-id` explicitly with the thread ID from the session context (system prompt). The thread ID appears in the session source header as `Source: Discord (<channel_id>/<thread_id>)`.
3. **Image download 403:** Must include `User-Agent` and `Referer` headers for `images.data.gov.sg`.
4. **Center gap cameras:** Lat 1.28-1.38, lon 103.80-103.88 zone → assigned to nearest region center. See `references/region-boundaries.md`.
5. **Geocode cache:** Cached in `geocode_cache.json`. Delete if stale. Cache uses atomic writes (safe on crash).
6. **URL length:** ~80-100 chars per numbered marker. 30 cameras ≈ 3-4k chars (well under 8192 char limit).
7. **Edge marker clipping (start/end S/E pins):** Google Static Maps clips markers that fall near the viewport boundary. The marker icon is ~30-40px tall; at zoom 13 that's ~600-760m. If the start/end marker is within a few hundred meters of the map edge, it vanishes silently — no error, just missing. **Root cause:** `build_map_url` computes center/zoom from the route polyline alone, and start/end markers can sit outside that extent. **Fix in `commute_camera_map.py`** (three parts):
   - Include `start_lat/lon` and `end_lat/lon` in the `all_lats`/`all_lons` lists for center/zoom calculation.
   - After choosing zoom, compute the actual viewport size in degrees (`deg_per_px = 360 / (2^zoom * 256)`). Check if content (extreme points + 5% margins on each side) exceeds the viewport — if so, **auto-downgrade zoom by 1**. Then shift the center if any extreme point is still within 5% of a viewport edge.
   - **Do NOT use zoom padding multipliers** (e.g. `span * 1.3`). Zoom is integer-only (0–21), so padding just flips between two discrete levels with no in-between. The user will see either "too zoomed in" or "too zoomed out" with no sweet spot.
8. **Start marker color:** Use `color:orange` (not green) for the S marker — green blends with Google Maps' park/POI icons, especially in nature-heavy areas like Bukit Timah. Blue for E marker works fine.
9. **Marker z-order:** Google Static Maps renders markers in URL order — later markers draw on top. Add camera markers first, then S/E markers last with `size:mid`. See `references/google-static-maps-numbered-markers.md`.
10. **`--no-geocode` produces empty addresses:** When resolving labels (Workflow 2 step 1) or testing, `--no-geocode` is fine for JSON/cross-reference. But if you then show the table to the user, all addresses will be `?`. Always run a second geocoded pass before delivering.
11. **`traffic_camera_report.py` is self-delivering** — use `--thread-id` to target the correct thread. Do NOT call `send_message` or post the images yourself after running it; the script handles delivery.
12. **"0 cameras along route" is normal since 30 Jun 2026** — LTA switched off most feeds (ERP 2); only checkpoint/Sentosa cameras remain (see Service status note). Don't debug the pipeline; report the discontinuation.

## Data Sources

| Source | Endpoint | Notes |
|--------|----------|-------|
| **Camera data** | `api.data.gov.sg/v1/transport/traffic-images` | Updated every 20s. ~90 cameras. |
| **Reverse geocoding** | Google Geocoding API | Cached per lat/lon. |
| **Static maps** | Google Static Maps API | `label` param for numbered markers. |

## References

| File | When to read |
|------|-------------|
| `references/region-boundaries.md` | Region boundary logic, center-gap assignment |
| `references/google-static-maps-numbered-markers.md` | Numbered marker pattern + failed approaches |
| `references/delivery-pattern.md` | Discord delivery (multipart curl, never MEDIA: tags) |
| `references/discord-thread-delivery-bug.md` | Gateway bug: self-delivery misfires in Discord threads |
