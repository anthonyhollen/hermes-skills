> **Historical (2026-09-29):** the thresholds here are still current (🟢 <1.10 / 🟡 1.10–1.35 / 🔴 ≥1.35); the advice to omit `departure_time` is superseded by `traffic-duration-troubleshooting.md` (`departure_time=now`).

# Traffic Colour Thresholds — Updated 2026-05-28

**File:** `references/traffic-duration-troubleshooting.md`  
**Status:** Authoritative — these thresholds override any other documentation in this skill

## Traffic Colour Decision Table (verified 2026-05-28)

| Colour | Hex | Condition |
|--------|-----|-----------|
| 🔵 Blue | `0x1565C0` | No `duration_in_traffic` returned by API |
| 🟢 Green | `0x00C853` | Traffic ratio < **1.10** |
| 🟡 Amber | `0xFFAB00` | Traffic ratio **1.10 – 1.35** |
| 🔴 Red | `0xD50000` | Traffic ratio ≥ **1.35** |

`ratio = duration_in_traffic_secs / normal_duration_secs`

## Why the Thresholds Changed (May 28)

At 07:05 cron run, route returned `normal=39min, traffic=44min` → ratio = 44/39 = **1.128**.  
Old threshold `<1.15` → green. But 1.128 is borderline moderate (closer to 1.10).  
New threshold `<1.10` correctly treats 1.128 as 🟡 **amber**, reflecting genuine morning traffic load.

## Python Implementation

```python
def traffic_color(normal_secs, traffic_secs):
    if not traffic_secs or normal_secs == 0:
        return "0x1565C0"  # blue — no traffic data
    ratio = traffic_secs / normal_secs
    if ratio < 1.10:     return "0x00C853"  # green
    elif ratio < 1.35:  return "0xFFAB00"  # amber
    else:                return "0xD50000"  # red
```

## Why the Polyline Was Blue Despite Traffic Data (May 28 run)

The cron agent at 07:05 fetched and displayed `Current: ~44 min (traffic-aware)` — confirming `duration_in_traffic` was returned. However the Static Map generation code that coloured the polyline received `traffic_secs = None`. This is a **temporal + code path discrepancy**:

- Route API call: made without `departure_time` parameter → returned traffic data (ratio ~1.13)  
- Static Map inline code: ran in a separate `execute_code` block that re-fetched the route with different parameters → got no traffic data → blue polyline

**Fix:** Ensure the same route response object (or its parsed values) is used for both the duration display AND the Static Map colour. Pass `normal_secs` and `traffic_secs` as explicit variables into the Static Map URL construction, not re-fetching.

## API Behaviour at 6–7am

Google Directions API `duration_in_traffic` at 6:30–7:00am:
- Without `departure_time`: may return traffic data (Google uses "current" traffic implicitly)
- With `departure_time` in past/near-past (ISO 8601): returns `INVALID_REQUEST`
- With future `departure_time`: may return no data if Google doesn't have predictive model for that departure window
- With `departure_time` omitted: Google appears to use current clock time → returns real-time traffic → good for morning runs

**Recommendation:** Omit `departure_time` for morning (6–8am) runs. Use it only for pre-scheduling future departure times (e.g., "what's my commute if I leave at 8am tomorrow").