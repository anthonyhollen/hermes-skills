---
name: sg-singapore-environment
description: Singapore environmental data — weather (2hr/24hr), flood alerts, PM2.5, and rainfall from data.gov.sg and NEA APIs. Use when asked about Singapore weather, rain, air quality, or floods.
version: 1.0.0
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [Singapore, Weather, NEA, data.gov.sg, Flood, PM2.5, Rainfall, Air Quality]
    related_skills: [sg-commute-report, sg-commute-planner, sg-carpark-availability]
---

# Singapore Environmental Data

Covers five Singapore environmental data APIs from `api-open.data.gov.sg`. All APIs share:
- **No API key needed** for basic use (~100 req/day per IP)
- **Optional `x-api-key` header** for higher rate limits (request at data.gov.sg)
- **Timestamps**: Most v2 endpoints return UTC; add 8h for SGT. Check each endpoint.
- **⚠️ Critical: Use `curl`, NOT `urllib.request`** — `api-open.data.gov.sg` returns HTTP 403 silently when called via Python's urllib (it rejects urllib's default User-Agent). Always pipe through `curl` or add a browser-like `User-Agent` header.

## Quick Reference

| What you need | Skill to load | Key endpoint |
|---|---|---|
| 24-hour forecast | `references/sg-24hr-weather.md` | `twenty-four-hr-forecast` |
| 2-hour nowcast | `references/sg-2hr-weather.md` | `two-hr-forecast` |
| Active flood alerts | `references/sg-flood-alerts.md` | `weather/flood-alerts` |
| PM2.5 readings | `references/sg-pm25.md` | `pm25` |
| 5-min rainfall | `references/sg-rainfall.md` | `rainfall` |

## Shared Patterns

### Fetch via curl (Python subprocess — avoids 403)
```python
import subprocess, json

def fetch(url, out_path="/tmp/env.json"):
    proc = subprocess.run(
        ["curl", "-s", "-f", "-o", out_path, url],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr}")
    with open(out_path) as f:
        return json.load(f)
```

### Fetch via curl (shell)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/<endpoint>" -o /tmp/data.json
```

### Timestamps
- `sg-24hr-weather` → UTC → add 8h for SGT
- `sg-2hr-weather` → **already SGT (+08:00)** — do NOT add 8h
- `sg-flood-alerts` → already SGT (+08:00)
- `sg-pm25` → already SGT (+08:00)
- `sg-rainfall` → already SGT (+08:00)

### Rate Limits
Without an API key: ~100 req/day per IP. With `x-api-key`: higher limits. Request at data.gov.sg.

## Data Response Wrapper

**All `api-open.data.gov.sg/v2` responses are wrapped in a `data` key:**
```python
response = fetch(url)
root = response["data"]  # NOT a flat object
```

Field naming: snake_case (`valid_period`, `time_period`, `pm25_one_hourly`) — NOT camelCase.

## Per-Endpoint References

Detailed documentation, response shapes, pitfalls, and Python helpers for each endpoint are in the `references/` subdirectory:

- `references/sg-24hr-weather.md` — 24-hour forecast: general conditions, temperature, humidity, wind, per-period regional forecasts
- `references/sg-2hr-weather.md` — 2-hour nowcast: area-by-area forecasts, valid period,Pasir Ris matching
- `references/sg-flood-alerts.md` — Active flood alerts: msgType filtering, severity levels, area locations
- `references/sg-pm25.md` — PM2.5 readings: 5 regions, WHO scale, unit (µg/m³)
- `references/sg-rainfall.md` — 5-minute rainfall: 63 stations, station validation, nearest station finder, wet/dry interpretation

## Station Directory

`references/stations.md` — pre-computed distances for 63 rainfall stations (useful for commute report proximity lookups).

## Common Pitfalls

1. **`urllib.request` → 403** — Always use `curl` via subprocess, or add `User-Agent: Mozilla/5.0` header
2. **No `data` key check** — Always access response via `response["data"]["records"]` or `response["data"]["items"]`
3. **UTC timestamps** — 24hr and 2hr weather return UTC; add 8h for SGT
4. **SGT timestamps** — Flood alerts, PM2.5, rainfall already include +08:00
5. **Station count validation (rainfall)** — Response with <60 stations is stale/partial; retry or skip
6. **Field name casing** — Use snake_case: `valid_period`, `pm25_one_hourly`, `time_period`