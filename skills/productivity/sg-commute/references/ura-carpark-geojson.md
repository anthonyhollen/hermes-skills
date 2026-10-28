# URA Carpark GeoJSON — data.gov.sg Dataset

**Collection URL:** `https://data.gov.sg/datasets?query=parking&resultId=d_9bf8620ecfdc8a5f8f77e3f02160af5c`
**Resource ID:** `d_9bf8620ecfdc8a5f8f77e3f02160af5c`
**Format:** GeoJSON (FeatureCollection)
**Features:** 777 Point features

## Download

The GeoJSON must be downloaded manually from the data.gov.sg UI — the **DOWNLOAD GEOJSON** button is rendered via JavaScript and has no static URL. The CKAN API (`https://data.gov.sg/api/3/action/resource_show?id=...`) returns empty `{}` for this resource ID.

Workaround: browse to the collection URL in a browser, click DOWNLOAD GEOJSON, then upload or zip the file to the agent.

## Geometry

All features are `Point` geometry in **WGS84** (longitude, latitude) — not SVY21. Use haversine for distance calculations.

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [103.8442347, 1.2790586] },
      "properties": {
        "OBJECTID_1": 37162,
        "PP_CODE": "BL021",
        "PARKING_PL": "Backlane Off Cook Street",
        "NO_CAR": 0,
        "NO_H_VEHIC": 0,
        "NO_MCYCLE": 10,
        "INC_CRC": "F5BB073ECD537F1E",
        "FMEL_UPD_D": "20220210124746"
      }
    }
  ]
}
```

## Fields

| Field | Type | Description |
|-------|------|-------------|
| `PP_CODE` | string | Carpark code (e.g. `BL021`, `A0043`, `U0014`) |
| `PARKING_PL` | string | Carpark name / location description |
| `NO_CAR` | integer | Number of car lots |
| `NO_H_VEHIC` | integer | Number of heavy vehicle lots |
| `NO_MCYCLE` | integer | Number of motorcycle lots |
| `INC_CRC` | string | Internal CRC reference |
| `FMEL_UPD_D` | string | Last update timestamp (YYYYMMDDHHMMSS) |

## Code prefix zones

| Prefix | Zone |
|--------|------|
| BL | Backlanes (Chinatown, Geylang, etc.) |
| A | Various (Ang Mo Kio, Ayer Rajah) |
| T | Tanglin / Queenstown |
| M | Margaret / others |
| U | Ubi |
| S | Sin Ming |
| (others) | Various URA-managed locations |

## Key stats

- 777 total carparks
- ~21,497 car lots, ~9,879 heavy vehicle lots, ~7,435 motorcycle lots
- 113 carparks have 0 car lots (motorcycle/heavy vehicle only)
- Lot counts are **static** — no live availability API for URA carparks

## Distance examples (WGS84 haversine)

```python
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# Innovis (1.29843, 103.78870) → nearest URA
# A0043 Ayer Rajah Crescent: 592m

# AMK (1.39037, 103.84650) → nearest URA
# A0037 Ang Mo Kio St 64: 408m
```

## Conversion to CSV

> ⚠️ **Important:** The CSV uses **lowercase** field names (`pp_code`, `parking_pl`, `no_car`, `no_h_vehicle`, `no_mcycle`, `longitude`, `latitude`) — NOT the uppercase GeoJSON names. When reading the CSV in code, always use the lowercase names. The uppercase names above are only for reading raw GeoJSON.

```python
import json, csv

with open('URAparking.geojson') as f:
    d = json.load(f)

with open('ura_carpark_info.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['pp_code','parking_pl','no_car','no_h_vehicle','no_mcycle','longitude','latitude'])
    w.writeheader()
    for feat in d['features']:
        p = feat['properties']
        coords = feat['geometry']['coordinates']
        w.writerow({
            'pp_code': p['PP_CODE'],
            'parking_pl': p['PARKING_PL'],
            'no_car': p['NO_CAR'],
            'no_h_vehicle': p['NO_H_VEHIC'],
            'no_mcycle': p['NO_MCYCLE'],
            'longitude': coords[0],
            'latitude': coords[1],
        })
```
