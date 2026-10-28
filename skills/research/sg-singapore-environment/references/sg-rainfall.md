# Rainfall Readings

Singapore rainfall readings via NEA / data.gov.sg Real-time API — 5-minute accumulated rainfall totals from 63 stations across Singapore.

**What it measures:** "TB1 Rainfall 5 Minute Total F" — accumulated rainfall in mm over the last 5 minutes, from 63 weather stations island-wide. Updated every 5 minutes.

## API Details

- **Endpoint**: `GET https://api-open.data.gov.sg/v2/real-time/api/rainfall`
- **Auth**: Optional `x-api-key` header for higher rate limits (no key required for basic use)
- **Station coordinates**: WGS84 (`location.latitude`, `location.longitude`)

## Parameters

| Param | In | Description |
|-------|-----|-------------|
| `date` | query | `YYYY-MM-DD` for all readings that day, `YYYY-MM-DDTHH:mm:ss` for specific reading, omit for latest |
| `paginationToken` | query | For paginating through date-range results |
| `x-api-key` | header | Optional API key for higher rate limits |

## Response Shape

```json
{
  "code": 0,
  "errorMsg": "",
  "data": {
    "stations": [
      {
        "id": "S111",
        "deviceId": "S111",
        "name": "Scotts Road",
        "location": { "latitude": 1.31055, "longitude": 103.8365 }
      }
    ],
    "readings": [
      {
        "timestamp": "2026-05-04T22:00:00+08:00",
        "data": [
          { "stationId": "S111", "value": 2 },
          { "stationId": "S112", "value": 0 }
        ]
      }
    ],
    "readingType": "TB1 Rainfall 5 Minute Total F",
    "readingUnit": "mm",
    "paginationToken": "b2Zmc2V0PTI1"
  }
}
```

**Spec vs reality corrections:**
- Spec claims `labelLocation` but actual API returns `location` — use `location.latitude` / `location.longitude`
- Timestamps include SGT timezone offset (`+08:00`), not UTC

## Pitfalls

### ⚠️ `urllib.request` silently returns HTTP 403
`api-open.data.gov.sg` rejects requests without a browser-like `User-Agent`. `urllib.request` sends no `User-Agent` by default → silent 403. **Always use `curl` via `subprocess`**, or add a browser-like `User-Agent` header.

Never do this:
```python
urllib.request.urlopen("https://api-open.data.gov.sg/v2/real-time/api/rainfall")  # silent 403
```

### ⚠️ Stale/partial responses have no error code
The API returns `code: 0` even when the response is incomplete (e.g. 40 stations instead of 63). **Always validate `len(data["data"]["stations"]) >= 60`** — if it's less, the data is stale/partial.

### ⚠️ HTTP errors in curl — use `-f` flag
Without `-f`, `curl` returns exit code 0 even on HTTP 4xx/5xx and writes a partial/error body to the output file. Always use `curl -f`:
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall" -o /tmp/rainfall.json
```

### No API key needed for basic use
Rate limits are ~40 req/min without a key. Use `x-api-key` header for higher limits (request at data.gov.sg).

### Spec vs reality mismatch
OpenAPI spec says `labelLocation` but API actually returns `location` — always check actual response keys.

### Timestamps are SGT
Timestamps include `+08:00` timezone offset, not UTC — no conversion needed for Singapore.

### 5-minute intervals
Readings come every 5 minutes (0, 5, 10, 15... minutes past the hour). A date query returns all readings for the day (~288/day).

### Pagination only for date-range queries
Token is base64-encoded (`"b2Zmc2V0PTEwMA=="` → offset=100).

### Zero rainfall is normal
Most of the time all stations report 0mm. Non-zero values indicate rain at that location.

### Coverage gaps exist
Not all Singapore locations are covered by a station. Bukit Panjang (670238) only has S64 at ~1.2km; Jurong has sparse coverage.

## Usage

### Latest reading (all 63 stations)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall" -o /tmp/rainfall.json
```

### Specific date (all readings that day, paginated — up to ~288 readings/day at 5-min intervals)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall?date=2026-05-04" -o /tmp/rainfall.json
# Follow pagination with:
curl -s "https://api-open.data.gov.sg/v2/real-time/api/rainfall?date=2026-05-04&paginationToken=b2Zmc2V0PTI1" -o /tmp/rainfall_p2.json
```

### Specific date-time (closest reading to that moment)
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall?date=2026-05-04T14:00:00" -o /tmp/rainfall.json
```

### With API key
```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall" \
  -H "x-api-key: YOUR_KEY_HERE" -o /tmp/rainfall.json
```

## Python helper — ALWAYS use curl, NOT urllib.request

**Recommended pattern — fetch via curl in Python:**
```python
import subprocess, json

def get_rainfall(date=None, api_key=None, retries=3):
    """Fetch NEA rainfall data via curl. Returns dict or raises RuntimeError."""
    url = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
    if date:
        url += f"?date={date}"
    for attempt in range(retries):
        proc = subprocess.run(
            ["curl", "-s", "-f", "-o", "/tmp/rainfall.json", url],
            capture_output=True, text=True
        )
        if proc.returncode == 0:
            break
        if attempt < retries - 1:
            import time; time.sleep(2 ** attempt)

    try:
        with open("/tmp/rainfall.json") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raise RuntimeError(f"Failed to fetch rainfall data after {retries} attempts")

    # Validate: API may return partial data (e.g. 40 stations instead of 63) without error code
    if data.get("code") != 0:
        raise RuntimeError(f"API error {data.get('code')}: {data.get('errorMsg')}")
    if len(data["data"]["stations"]) < 60:
        raise RuntimeError(
            f"Station count ({len(data['data']['stations'])}) is suspiciously low — "
            f"data may be stale/partial. Expected 63 stations."
        )
    return data
```

**urllib.request with browser-like User-Agent (alternative):**
```python
import urllib.request, json

def get_rainfall_urllib(date=None, api_key=None, retries=3):
    url = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
    if date:
        url += f"?date={date}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0 (compatible; sg-rainfall/1.0)")
            if api_key:
                req.add_header("x-api-key", api_key)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
                if data.get("code") != 0:
                    raise RuntimeError(f"API error: {data.get('errorMsg')}")
                return data
        except urllib.error.HTTPError as e:
            if attempt < retries - 1:
                import time; time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"HTTP {e.code} after {retries} attempts: {e.url}")
```

## Fetch latest reading + print wet stations

```python
import subprocess, json

def get_rainfall(date=None, retries=3):
    url = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
    if date:
        url += f"?date={date}"
    for attempt in range(retries):
        proc = subprocess.run(["curl", "-s", "-f", "-o", "/tmp/rainfall.json", url],
                             capture_output=True, text=True)
        if proc.returncode == 0:
            break
        import time; time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"Failed after {retries} attempts")
    with open("/tmp/rainfall.json") as f:
        return json.load(f)

data = get_rainfall()

ts = data["data"]["readings"][0]["timestamp"]
print(f"Timestamp: {ts}")

stations = {s["id"]: s["name"] for s in data["data"]["stations"]}

wet = [(d["stationId"], d["value"])
       for d in data["data"]["readings"][0]["data"]
       if d["value"] > 0]
wet.sort(key=lambda x: -x[1])

print(f"\nWet stations (rainfall > 0mm):")
for sid, val in wet:
    print(f"  {stations.get(sid, sid):30s} {val}mm")
```

**Always validate station count** before processing — a partial/stale response (fewer than ~60 stations) silently produces wrong results.

## Find nearest rainfall station to a WGS84 coordinate

Uses haversine to find the closest station:

```python
import subprocess, json, math

def get_rainfall(date=None, retries=3):
    url = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
    if date:
        url += f"?date={date}"
    for attempt in range(retries):
        proc = subprocess.run(["curl", "-s", "-f", "-o", "/tmp/rainfall.json", url],
                             capture_output=True, text=True)
        if proc.returncode == 0:
            break
        import time; time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"Failed after {retries} attempts")
    with open("/tmp/rainfall.json") as f:
        return json.load(f)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

data = get_rainfall()
stations = {s["id"]: s for s in data["data"]["stations"]}
readings = {d["stationId"]: d["value"] for d in data["data"]["readings"][0]["data"]}

TARGET_LAT, TARGET_LON = 1.29843, 103.78870  # e.g. Innovis

results = sorted(
    (haversine(TARGET_LAT, TARGET_LON, s["location"]["latitude"], s["location"]["longitude"]),
     sid, s["name"], readings.get(sid, 0))
    for sid, s in stations.items()
)

print(f"Nearest 5 stations to ({TARGET_LAT}, {TARGET_LON}):")
for dist, sid, name, val in results[:5]:
    print(f"  {dist:7.0f}m | {sid} | {val:4.1f}mm | {name}")
```

## Rainfall Value Interpretation

| Value | Explanation |
|-------|-------------|
| 0.0mm | no rain |
| 0.1–0.5mm | light drizzle |
| 0.6–2.0mm | light rain |
| 2.1–5.0mm | moderate rain |
| 5.1mm+ | heavy downpour |

## Verification

```bash
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall" -o /tmp/rainfall.json && \
  python3 -c "
import json, sys
d = json.load(open('/tmp/rainfall.json'))
print('code:', d['code'])
print('stations:', len(d['data']['stations']))
if len(d['data']['stations']) < 60:
    print('WARNING: Expected 63 stations, got', len(d['data']['stations']), '- data may be stale')
    sys.exit(1)
print('readingType:', d['data']['readingType'])
print('unit:', d['data']['readingUnit'])
wet = [(x['stationId'],x['value']) for x in d['data']['readings'][0]['data'] if x['value']>0]
print('wet stations:', len(wet))
print('sample wet:', wet[:3])
"
```

## Station Directory

The full 63-station directory with coordinates is at `references/stations.md` — no API call needed, pre-computed distances for commute report proximity lookups.