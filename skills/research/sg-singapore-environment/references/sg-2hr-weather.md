# 2-Hour Weather Nowcast

Singapore 2-hour weather forecast via data.gov.sg Real-time API.

## API Details

- **Endpoint**: `GET https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast`
- **Auth**: Optional `x-api-key` header for higher rate limits
- **No API key required** for basic usage (rate-limited)

## Parameters

| Param | In | Description |
|-------|-----|-------------|
| `date` | query | `YYYY-MM-DD` for all readings that day, `YYYY-MM-DDTHH:mm:ss` for a specific reading, or omit for latest |
| `paginationToken` | query | For paginated date-range requests |
| `x-api-key` | header | Optional API key for higher rate limits |

## Returns

```json
{
  "code": 0,
  "data": {
    "area_metadata": [{ "name": "Ang Mo Kio", "label_location": { "latitude": 1.375, "longitude": 103.839 } }],
    "items": [{
      "update_timestamp": "2026-05-28T06:20:49+08:00",
      "timestamp": "2026-05-28T06:15:00+08:00",
      "valid_period": {
        "start": "2026-05-28T06:00:00+08:00",
        "end": "2026-05-28T08:00:00+08:00",
        "text": "6.00 am to 8.00 am"
      },
      "forecasts": [
        { "area": "Pasir Ris", "forecast": "Partly Cloudy" },
        { "area": "Ang Mo Kio", "forecast": "Partly Cloudy (Day)" }
      ]
    }]
  }
}
```

## Key Fields

- **`valid_period`**: snake_case — NOT `validPeriod`
- **`forecasts[]`**: each has `.area` and `.forecast` — area names include "Pasir Ris", "Ang Mo Kio", etc.
- **`area_metadata`**: provides lat/lon for each area — used for proximity/distance lookups
- **`valid_period.start/end`**: Already in SGT (+08:00) — no UTC conversion needed
- **`update_timestamp` / `timestamp`**: also already in SGT (+08:00)

### ⚠️ Timestamps are SGT, not UTC
The `sg-2hr-weather` API returns timestamps already in SGT (+08:00), unlike most other data.gov.sg v2 endpoints which return UTC. Do NOT add 8h — use the values as-is.

## Pitfalls

### ⚠️ `urllib.request` silently returns HTTP 403
`api-open.data.gov.sg` rejects requests without a browser-like `User-Agent`. `urllib.request` sends no `User-Agent` by default → silent 403. **Always use `curl` via `subprocess`**, or add a browser-like `User-Agent` header.

Never do this:
```python
urllib.request.urlopen("https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast")  # silent 403
```

### ⚠️ HTTP errors in curl — use `-f` flag
Without `-f`, `curl` returns exit code 0 even on HTTP 4xx/5xx and writes a partial/error body. Always use `curl -f`:
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" -o /tmp/forecast.json
```

### No API key needed for basic use
Rate limits are ~100 req/day per IP. Request an API key at data.gov.sg for higher limits.

### Timestamps are in UTC
`update_timestamp` = NEA acquisition time, `timestamp` = when NEA issued the forecast. `valid_period.start` / `valid_period.end` are in UTC, not SGT — **add 8h** to convert to Singapore time.

## Usage

### Latest forecast (all areas)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" -o /tmp/2hr.json
```

### Specific date (all readings that day)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast?date=2025-01-15" -o /tmp/2hr.json
```

### Specific date-time (closest reading to that moment)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast?date=2025-01-15T14:00:00" -o /tmp/2hr.json
```

### With API key
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" \
  -H "x-api-key: YOUR_KEY_HERE" -o /tmp/2hr.json
```

## Pasir Ris Matching

The 2hr forecast covers ~47 areas. For Pasir Ris:
1. Try exact match on area name `"Pasir Ris"`
2. If not found, try partial match: any area whose name starts with `"Pasir Ris"` (e.g. `"Pasir Ris Central"`)
3. If still not found, use the first available area and note: *"Nearest area: <name>"*

## Python helper — use curl, NOT urllib.request

`urllib.request` silently gets HTTP 403 from `api-open.data.gov.sg`. Always use `curl` via `subprocess`:

```python
import subprocess, json

def get_2hr_forecast(date=None, api_key=None, retries=3):
    url = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"
    if date:
        url += f"?date={date}"
    for attempt in range(retries):
        proc = subprocess.run(
            ["curl", "-s", "-f", "-o", "/tmp/2hr.json", url],
            capture_output=True, text=True
        )
        if proc.returncode == 0:
            break
        import time; time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"Failed after {retries} attempts")
    with open("/tmp/2hr.json") as f:
        return json.load(f)
```

## Verification

```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" -o /tmp/2hr.json && \
  python3 -c "import json; d=json.load(open('/tmp/2hr.json')); \
  print('code:', d['code']); \
  print('areas:', len(d['data']['area_metadata'])); \
  print('sample:', d['data']['items'][0]['forecasts'][0])"
```