# Flood Alerts

Singapore flood alerts via PUB / data.gov.sg Real-time API.

## API Details

- **Endpoint**: `GET https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts`
- **Auth**: Optional `x-api-key` header for higher rate limits
- **No API key required** for basic usage

## Parameters

| Param | In | Description |
|-------|-----|-------------|
| `date` | query | `YYYY-MM-DD` for all alerts that day, `YYYY-MM-DDTHH:mm:ss` for alerts up to that time. Omit for latest. |
| `paginationToken` | query | For paginated date-range requests |
| `x-api-key` | header | Optional API key for higher rate limits |

## Returns

```json
{
  "code": 0,
  "errorMsg": "",
  "data": {
    "records": [{
      "datetime": "2025-05-22T09:55:00+08:00",
      "updatedTimestamp": "2025-05-22T10:03:18+08:00",
      "item": {
        "type": "observation",
        "isStationData": false,
        "msgType": "Alert",
        "references": "pub_joint_ops_ctr@pub.gov.sg, 2.49.0.0.702.2-BCM-17586117885024-DYOONG, 2025-09-23T15:16:28+08:00",
        "sender": "pub_joint_ops_ctr@pub.gov.sg",
        "scope": "Public",
        "status": "Actual",
        "readings": [{
          "area": {
            "areaDesc": "at Bt Timah Rd from Wilby Rd to Blackmore Dr",
            "circle": [1.33201, 103.87015, 1]
          },
          "category": "Met",
          "certainty": "Observed",
          "description": "Flash flood at Bt Timah Rd from Wilby Rd to Blackmore Dr. Please avoid the area. Issued 1705 hrs.",
          "event": "Flood",
          "eventCode": { "value": "OET-081", "valueName": "OET:v1.2" },
          "headline": "Flash Flood Alert",
          "instruction": "Please avoid this area for the next one (1) hour.",
          "responseType": "Avoid",
          "senderName": "PUB",
          "severity": "Minor",
          "urgency": "Immediate"
        }]
      }
    }]
  }
}
```

## Key Fields

- **`msgType`**: `"Alert"` = active flood, `"Cancel"` = flood has subsided (every Alert has a matching Cancel)
- **`readings[].severity`**: `Minor` / `Moderate` / `Severe` / `Extreme`
- **`readings[].area.circle`**: `[lat, lng, radius_km]` — radius is broadcast radius, NOT flood extent
- **`readings[].area.areaDesc`**: Human-readable street location
- **Timestamps**: In SGT (+08:00) already — no conversion needed

## Pitfalls

- Rate limits are tight without an API key (~100 req/day per IP). Get a key at data.gov.sg
- When `readings` array is empty, there are no active floods (every historical alert has a corresponding Cancel)
- The `date` filter returns ALL records for that day, including Cancel alerts — filter by `msgType == "Alert"` and check `readings` is non-empty for active alerts
- `paginationToken` exists when there are more than ~25 records for a date query

## Usage

### Latest (no active floods currently)
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts"
```

### Today's alerts
```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts?date=2026-05-02"
```

### Active alerts only (Python — uses `records`, NOT `items`)
```python
import subprocess, json

def get_active_flood_alerts(date=None, api_key=None):
    url = "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts"
    if date:
        url += f"?date={date}"
    proc = subprocess.run(["curl", "-s", "-f", url], capture_output=True, text=True)
    d = json.loads(proc.stdout)
    # Active alerts: msgType=Alert AND non-empty readings
    # The root is response["data"]["records"] — NOT response["data"]["items"]
    active = [
        r for r in d["data"]["records"]
        if r.get("item", {}).get("msgType") == "Alert" and r.get("item", {}).get("readings")
    ]
    return active
```

## Verification

```bash
curl -s "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); \
  alerts = [r for r in d['data']['records'] if r['item'].get('msgType')=='Alert' and r['item']['readings']]; \
  print(f'Active floods: {len(alerts)}'); \
  for a in alerts: print(f'  - {a[\"item\"][\"readings\"][0][\"area\"][\"areaDesc\"]} ({a[\"item\"][\"readings\"][0][\"severity\"]})')"
```