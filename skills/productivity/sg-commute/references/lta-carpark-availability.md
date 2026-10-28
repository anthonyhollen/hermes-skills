# LTA DataMall CarPark Availability

## Endpoint

```
GET https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2
```

**Note:** The v2 suffix is required. Without it, the endpoint returns 404.

## Authentication

| Header | Value |
|--------|-------|
| `AccountKey` | `${LTA_DATAMALL_KEY}` |
| `Accept` | `application/json` |

```bash
curl -s "https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2" \
  -H "AccountKey: ${LTA_DATAMALL_KEY}" \
  -H "accept: application/json"
```

> **Paging (2026-09-29):** DataMall returns **at most 500 rows per call**; the feed is ~2,600 rows / 6 pages (`$skip=0,500,…,2500`). Page 0 holds all `Agency: LTA` (malls) and `URA` rows plus ~300 HDB rows; pages 1+ are all `Agency: HDB`. `nearest_carpark.py` pages through all of them (v1.11.0) and prefers the HDB feed for HDB-agency codes.

## Response Shape

```json
{
  "odata.metadata": "https://datamall2.mytransport.sg/ltaodataservice/$metadata#CarParkAvailability",
  "value": [
    {
      "CarParkID": "2",
      "Area": "Marina",
      "Development": "Marina Square",
      "Location": "1.29115 103.85728",
      "AvailableLots": 1679,
      "LotType": "C",
      "Agency": "LTA"
    }
  ]
}
```

## Field Definitions

| Field | Type | Description |
|-------|------|-------------|
| `CarParkID` | String | Unique identifier for the carpark |
| `Area` | String | Zone name (Marina, Orchard, Harbfront, JurongLakeDistrict, Others, "") |
| `Development` | String | Mall / development name |
| `Location` | String | WGS84 as `"lat lon"` (space-separated) |
| `AvailableLots` | Integer | Currently available lots (no total — use HDB API for capacity) |
| `LotType` | String | C=Car, H=Heavy vehicle, Y=Motorcycle |
| `Agency` | String | Always `LTA` |

## Coverage

- **500 carparks** across **387 unique malls/developments**
- Areas: Marina, Orchard, Harbfront, JurongLakeDistrict, Others, "" (unclassified)
- Predominantly retail mall carparks (shopping centres, commercial buildings)

## Key Differences from HDB API

| | LTA | HDB |
|---|---|---|
| Total lots available | ❌ No | ✅ Yes |
| Location format | WGS84 `"lat lon"` string | SVY21 x/y |
| Coordinates in response | ✅ Yes | ❌ Need to join with CSV |
| Coverage | Retail/commercial malls | HDB estate carparks |

## Quick Stats (2026-05-03)

- Total carparks: 500
- Total available lots: ~67,827
- Lot types: C, H, Y (no S)
- Top areas by volume: Harbfront (5 carparks, ~6,700 lots), Orchard (19 carparks), Marina (5 carparks)

## Common Issues

1. **Missing `v2` suffix** — `CarParkAvailability` returns 404; must be `CarParkAvailabilityv2`
2. **Domain unreachable** — `datamall2.mytransport.sg` may be blocked in some network environments
3. **No total lots** — LTA only provides `AvailableLots`, not capacity. Cannot compute % fullness.
4. **Location parsing** — `Location` field is a string `"lat lon"` (space-separated), not separate fields. Split on space.
5. **Car lots only (C) — skip H/Y rows:** The API returns one row per (CarParkID, LotType) pair. A single CarParkID may appear with LotType=C (car), H (heavy vehicle), or Y (motorcycle). The `lta_live()` aggregation function filters to `LotType == "C"` only and sums `AvailableLots` per CarParkID — H/Y rows are discarded entirely. Verified fix applied May 2026.
