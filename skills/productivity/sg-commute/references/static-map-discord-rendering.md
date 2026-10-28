# Static Map Polyline — Discord Rendering Fix

> **Note (2026-09-29):** each size dimension is capped at 640 px (Google silently clamps; the scripts now request `640x450` for the route map), and since 2026-05-25 the polyline is traffic-coloured, so the "blue pixels" check below only applies to a 🔵 no-data map.

## Root Cause

Default `format=png` → PNG8 (256-color palette). Discord re-encodes images and PNG8 palette mode causes blue polyline to be stripped or blended into map background. **Polyline IS in the file but invisible in Discord.**

## Fix

Always use in Static Map URL:
- `format=png32` → forces full RGB rendering
- `color:<traffic_color>` → traffic-based: `0x00C853` green / `0xFFAB00` amber / `0xD50000` red / `0x1565C0` blue (no data). All are saturated colours that survive PNG32 re-encoding — do not use pale/pastel hex values.
- `weight:7` → thicker line (vs default `weight:5`)
- File size >500KB confirms PNG32 was returned

## Verification Checklist

| Check | Pass | Fail |
|-------|------|------|
| File size >500KB | PNG32 ✅ | PNG8 likely — re-request with `format=png32` |
| Blue pixels in raw file >1,000 | Polyline in file ✅ | Polyline not drawn |
| Mode == RGBA | PNG32 ✅ | Mode P = PNG8 |

## Full Static Map URL Pattern

```
https://maps.googleapis.com/maps/api/staticmap
?size=640x450&scale=2&format=png32
&markers=color:green|label:H|Pasir Ris Grove Singapore
&markers=color:red|label:W|<destination address>
&path=color:<traffic_color>|weight:7|enc:<polyline>
&key=${GOOGLE_API_KEY}
```

## Confirmed Working (May 2026)

- AMK direct: 14.8km, 17 min, polyline visible in Discord ✅
- Innovis direct: 28.8km, 31 min, polyline visible in Discord ✅
- Test images sent to playground: `/tmp/route_amk_playground.png`, `/tmp/route_innovis_playground.png`