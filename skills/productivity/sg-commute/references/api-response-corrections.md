# HDB CarPark Availability — API Corrections

Verified against live API responses — the official OpenAPI spec has errors.

## Response Shape (CORRECTED)

The spec shows `carpark_data[]` items as flat objects. Reality: `carpark_info` is nested inside.

```json
// SPEC (WRONG):
{ "carpark_number": "HE12", "total_lots": "500", "lot_type": "C", "lots_available": "123" }

// ACTUAL (2026-05-03 verified):
{
  "carpark_number": "HE12",
  "update_datetime": "2026-05-03T23:51:21",
  "carpark_info": [
    { "lots_available": "101", "total_lots": "105", "lot_type": "C" },
    { "lots_available": "12",  "total_lots": "100", "lot_type": "H" }
  ]
}
```

## Key corrections

| Field | Spec says | Actual |
|-------|-----------|--------|
| `carpark_info` | Does not exist (flat structure) | **Nested array** — must access via `item.carpark_info[].lots_available` |
| `update_datetime` | Not documented | Present at carpark level, not per-lot |
| Lot counts | String or integer | **String** — cast to int before arithmetic |

## Field types

All of these are **strings** in the live response:
- `lots_available` — string, e.g. `"101"`
- `total_lots` — string, e.g. `"105"`
- `lot_type` — string, e.g. `"C"`

Always `int(lot["lots_available"])` not `lot["lots_available"] + 1`.

## Verified 2026-05-03

- Endpoint: `https://api.data.gov.sg/v1/transport/carpark-availability`
- ~1,988 of 2,265 carparks report availability
- Response wrapped in `{"items": [{"timestamp": "...", "carpark_data": [...]}]}`
