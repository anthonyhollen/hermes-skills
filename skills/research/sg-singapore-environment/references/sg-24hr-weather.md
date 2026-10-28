# 24-Hour Weather Forecast

Singapore 24-hour weather forecast via data.gov.sg Real-time API.

## API Details

- **Endpoint**: `GET https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast`
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
  "errorMsg": "",
  "data": {
    "area_metadata": [...],
    "records": [{
      "date": "2024-07-15T00:00:00.000Z",
      "updatedTimestamp": "2024-07-15T15:04:00.000Z",
      "timestamp": "2024-07-15T15:04:00.000Z",
      "general": {
        "validPeriod": { "start": "...", "end": "...", "text": "3pm to 4pm" },
        "temperature": { "low": 26, "high": 34, "unit": "Degrees Celsius" },
        "relativeHumidity": { "low": 55, "high": 90, "unit": "Percentage" },
        "forecast": { "code": "...", "text": "Thundery Showers" },
        "wind": { "speed": { "low": 15, "high": 30 }, "direction": "SSE" }
      },
      "periods": [{
        "timePeriod": { "start": "...", "end": "...", "text": "12am to 6am" },
        "regions": {
          "west": { "code": "...", "text": "Cloudy" },
          "east": { "code": "...", "text": "Thundery Showers" },
          "central": { "code": "...", "text": "Light Showers" },
          "north": { "code": "...", "text": "Showers" },
          "south": { "code": "...", "text": "Partly Cloudy" }
        }
      }]
    }],
    "paginationToken": null
  }
}
```

## Key Fields

- **`general`**: 24h summary — `forecast.text`, `temperature.low/high`, `relativeHumidity.low/high`, `wind.direction`, `wind.speed.low/high`
- **`periods`**: sub-forecast time slices (e.g. 12am-6am, 6am-12pm, 12pm-6pm, 6pm-12am) with regional breakdowns (N/S/E/W/Central)
- **`area_metadata`**: lat/lon for 47 areas (NOT used in 24hr response — only in 2hr forecast)
- **Timestamps**: UTC — add 8h for SGT

## Pitfalls

- No API key needed for basic use, but rate limits are tight (~100 req/day per IP)
- Request an API key at data.gov.sg for higher limits
- Timestamps are in UTC — add 8h for SGT
- `records` is an array (multiple forecasts per day); `general` is the 24h summary for the whole day

## Usage

### Latest 24hr forecast
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
```

### Specific date
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast?date=2026-05-02"
```

### With API key
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast" \
  -H "x-api-key: YOUR_KEY_HERE"
```

### Extract East-region per-period forecasts (Python)
```python
import subprocess, json

def fetch(url):
    subprocess.run(["curl", "-s", "-f", "-o", "/tmp/24hr.json", url])
    with open("/tmp/24hr.json") as f:
        return json.load(f)

d = fetch("https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast")
records = d['data']['records']
for r in records:
    for p in r['periods']:
        print(f"{p['timePeriod']['text']}: {p['regions']['east']['text']}")
gen = records[0]['general']
print(f"General: {gen['forecast']['text']}, {gen['temperature']['low']}-{gen['temperature']['high']}°C")
```

## Python helper

```python
import subprocess, json

def get_24hr_forecast(date=None, api_key=None):
    url = "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
    if date:
        url += f"?date={date}"
    subprocess.run(["curl", "-s", "-f", "-o", "/tmp/24hr.json", url],
                   capture_output=True)
    with open("/tmp/24hr.json") as f:
        return json.load(f)
```

## Verification

```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); r=d['data']['records'][0]['general']; \
  print('Forecast:', r['forecast']['text']); \
  print('Temp:', r['temperature']['low'], '-', r['temperature']['high'], '°C'); \
  print('Humidity:', r['relativeHumidity']['low'], '-', r['relativeHumidity']['high'], '%'); \
  print('Wind:', r['wind']['direction'], r['wind']['speed']['low'], '-', r['wind']['speed']['high'], 'km/h'); \
  print('Periods:', len(d['data']['records'][0]['periods']))"
```