# PM2.5 Readings

Singapore PM2.5 (1-hourly) readings via data.gov.sg Real-time API.

## API Details

- **Endpoint**: `GET https://api-open.data.gov.sg/v2/real-time/api/pm25`
- **Auth**: Optional `x-api-key` header for higher rate limits
- **No API key required** for basic usage

## Parameters

| Param | In | Description |
|-------|-----|-------------|
| `date` | query | `YYYY-MM-DD` for all readings that day, `YYYY-MM-DDTHH:mm:ss` for specific reading, omit for latest |
| `paginationToken` | query | For paginated date-range requests |
| `x-api-key` | header | Optional API key for higher rate limits |

## Returns

```json
{
  "code": 0,
  "errorMsg": "",
  "data": {
    "regionMetadata": [
      { "name": "West", "labelLocation": { "latitude": 1.35735, "longitude": 103.7 } },
      { "name": "East", "labelLocation": { "latitude": 1.35735, "longitude": 103.94 } },
      { "name": "North", "labelLocation": { "latitude": 1.41803, "longitude": 103.82 } },
      { "name": "South", "labelLocation": { "latitude": 1.29636, "longitude": 103.82 } },
      { "name": "Central", "labelLocation": { "latitude": 1.35735, "longitude": 103.82 } }
    ],
    "items": [{
      "date": "2024-07-17T00:00:00.000Z",
      "updatedTimestamp": "2024-07-17T14:15:43+08:00",
      "timestamp": "2024-07-17T06:00:00.000Z",
      "readings": {
        "pm25_one_hourly": {
          "north": 11,
          "south": 8,
          "east": 18,
          "west": 12,
          "central": 7
        }
      }
    }],
    "paginationToken": null
  }
}
```

## PM2.5 Reading Guide (WHO 2021)

| µg/m³ | Level |
|-------|-------|
| 0–9 | Good |
| 10–19 | Moderate |
| 20–49 | Unhealthy (Sensitive) |
| 50–99 | Unhealthy |
| 100+ | Very Unhealthy / Hazardous |

## Key Fields

- **`pm25_one_hourly`**: snake_case — NOT `pm25OneHourly`
- **`readings.pm25_one_hourly.<region>`**: north, south, east, west, central
- **`updatedTimestamp` and `timestamp`**: In SGT (+08:00) — no conversion needed
- **Unit**: µg/m³ (micrograms per cubic meter)

## Pitfalls

- **Timestamps**: `updatedTimestamp` and `timestamp` are in SGT (+08:00) — no conversion needed
- `region_metadata` can be empty on some queries; coordinates for the 5 regions (N/S/E/W/Central) are stable
- Rate limits are tight without an API key — get one at data.gov.sg for higher limits

## Usage

### Latest PM2.5
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/pm25"
```

### Specific date
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/pm25?date=2026-05-02"
```

### With API key
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/pm25" \
  -H "x-api-key: YOUR_KEY_HERE"
```

## Python helper — use curl, NOT urllib.request

`urllib.request` silently gets HTTP 403 from `api-open.data.gov.sg`. Always use `curl` via `subprocess`:

```python
import subprocess, json

def get_pm25():
    proc = subprocess.run(
        ["curl", "-s", "-f", "https://api-open.data.gov.sg/v2/real-time/api/pm25"],
        capture_output=True, text=True
    )
    d = json.loads(proc.stdout)
    return d["data"]["items"][0]["readings"]["pm25_one_hourly"]
```

## Verification

```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/pm25" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); \
  r=d['data']['items'][0]['readings']['pm25_one_hourly']; \
  vals = list(r.values()); avg = sum(vals)/len(vals); \
  print(f'PM2.5 — N:{r[\"north\"]} S:{r[\"south\"]} E:{r[\"east\"]} W:{r[\"west\"]} C:{r[\"central\"]} | Avg: {avg:.1f} µg/m³')"
```