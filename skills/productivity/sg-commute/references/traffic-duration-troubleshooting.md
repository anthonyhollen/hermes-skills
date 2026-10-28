# Traffic Duration — Verified Patterns for `duration_in_traffic`

**Updated: 2026-05-29**

## The Correct Pattern (verified 2026-05-28)

```python
url = (
    f"https://maps.googleapis.com/maps/api/directions/json"
    f"?origin={urllib.parse.quote(origin)}"
    f"&destination={urllib.parse.quote(dest)}"
    + (f"&waypoints={waypoint}" if waypoint else "")
    + "&mode=driving"
    + "&departure_time=now"          # ← string "now", not a timestamp
    + "&traffic_model=best_guess"
    f"&key={API_KEY}"
)
# Returns: distance, duration (normal), duration_in_traffic (traffic-aware)
```

**`departure_time=now` as a literal string works.** Earlier guidance to omit `departure_time` entirely was **wrong** — live API test confirms:

| Call | Normal | Traffic returned |
|------|--------|-----------------|
| No `departure_time` | 39 min | **None** |
| `departure_time=now` + `traffic_model=best_guess` | 44 min | **52 min** ✅ |

The two produce **different routes** (39 km vs 44 km) and only the second returns `duration_in_traffic`.

## What Does NOT Work

- ~~**Unix timestamp** as `departure_time` → `INVALID_REQUEST`~~ — **wrong (corrected 2026-09-29):** a Directions call with `departure_time=<now+900 as an int>` returned `OK` with `duration_in_traffic` (`raw/g_amk_unixts.json` in the tune-up kit). The Directions API takes `now` or integer seconds; ISO 8601 is the Routes v2 format. The old failure was most likely a timestamp in the past (also `INVALID_REQUEST`).
- **ISO 8601 fixed time** (e.g. `2026-05-21T07:00:00+08:00`) → route returned but `duration_in_traffic` absent at 6–7am
- **Omitting `departure_time`** → `duration_in_traffic` absent (the `traffic_model` param is ignored without explicit `departure_time`)

## Polyline Colour Thresholds

Updated 2026-05-28 based on morning rush ratio observations:

| Colour | Hex | Condition |
|--------|-----|-----------|
| 🔵 Blue | `0x1565C0` | No `duration_in_traffic` returned |
| 🟢 Green | `0x00C853` | ratio < **1.10** |
| 🟡 Amber | `0xFFAB00` | ratio **1.10 – 1.35** |
| 🔴 Red | `0xD50000` | ratio ≥ **1.35** |

```python
def traffic_color(normal_secs, traffic_secs):
    if not traffic_secs or normal_secs == 0:
        return "0x1565C0"  # blue
    ratio = traffic_secs / normal_secs
    if ratio < 1.10:      return "0x00C853"  # green
    elif ratio < 1.35:   return "0xFFAB00"  # amber
    else:                return "0xD50000"  # red
```

## Routes API v2 — `durationInTraffic` Not Available

Even with `routingPreference: TRAFFIC_AWARE` and a `departureTime`, adding `routes.durationInTraffic` to the field mask causes:

```
Cannot find matching fields for path 'routes.durationInTraffic'
```

Field mask accepted values: `routes.distanceMeters`, `routes.duration`, `routes.polyline`. Use Directions API (not Routes API v2) for traffic-aware calls.

## Verified Route Data

| Date | Destination | Normal | Traffic | Ratio | Polyline |
|------|-------------|--------|---------|-------|----------|
| 2026-05-28 07:05 | Innovis (via ECP) | 39 min | 44 min | 1.128 | Blue (no `departure_time`) |
| 2026-05-29 07:30 | Innovis (via ECP) | 44 min | 52 min | 1.182 | Amber (`departure_time=now`) |

## Cron Prompt Design — Critical

The cron prompt must include the **full Directions API call** — not just the Static Map generator. If the agent has to construct the URL from skill references, parameter choices may vary. Embed the exact URL construction in the cron prompt so `departure_time=now` + `traffic_model=best_guess` is guaranteed.

The Static Map colour must use the **same** `normal_secs` and `traffic_secs` values fetched by the agent — not a separate re-fetch that may use different parameters and lose traffic data.