# JTC Carpark Information

**Source:** User-provided CSV (`JTCCarparkInformation.csv`)

**Coverage:** 28 JTC industrial estate carparks across Singapore.

**Note:** No public live availability API for JTC carparks. All are coupon-parking surface lots.

## Carpark Fields

| Field | Description |
|-------|-------------|
| `car_park_no` | Unique ID (prefixed by zone: A=Ang Mo Kio, D=Defu, E=Eunos, G=Geylang, J=Jurong, M=Marsiling, S=Sin Ming, T=Toa Payoh, U=Ubi, W=Woodlands, Y=Yishun) |
| `address` | Industrial estate address |
| `x_coord` | SVY21 easting |
| `y_coord` | SVY21 northing |
| `car_park_type` | Always `SURFACE CAR PARK` |
| `type_of_parking_system` | Always `COUPON PARKING` |
| `short_term_parking` | `WHOLE DAY`, `7AM-10.30PM`, or `NO` |
| `free_parking` | `SUN & PH FR 7AM-10.30PM` or `NO` |
| `night_parking` | `YES` or `NO` |
| `car_park_decks` | Always `0` |
| `gantry_height` | Always `0` |
| `car_park_basement` | Always `N` |

## Coverage by Zone

| Zone | Carparks |
|------|----------|
| Ang Mo Kio (A) | A1001–A1004 |
| Defu (D) | D1000–D1012 (partial) |
| Eunos (E) | E1000 |
| Geylang Bahru (G) | G1000 |
| Jurong (J) | J1000 |
| Marsiling (M) | M1000 |
| Sin Ming (S) | S1000 |
| Toa Payoh (T) | T1000–T1002 |
| Ubi (U) | U1001 |
| Woodlands (W) | W1000 |
| Yishun (Y) | Y1000 |

## Usage

```python
import csv

with open('/home/amh/.hermes/skills/productivity/sg-commute/data/jtc_carpark_info.csv') as f:
    carparks = list(csv.DictReader(f))

# All Ang Mo Kio carparks
for cp in carparks:
    if cp['car_park_no'].startswith('A'):
        print(cp['car_park_no'], cp['address'])
```

## Distance Query (SVY21 Euclidean)

```python
import csv, math

TARGET_X, TARGET_Y = 29466.6, 41365.8  # AMK SVY21

with open('/home/amh/.hermes/skills/productivity/sg-commute/data/jtc_carpark_info.csv') as f:
    carparks = list(csv.DictReader(f))

results = sorted(
    ((math.sqrt((TARGET_X - float(cp['x_coord']))**2 + (TARGET_Y - float(cp['y_coord']))**2), cp)
     for cp in carparks)
)
for dist, cp in results[:5]:
    print(f'{dist:.0f}m | {cp["car_park_no"]} | {cp["address"][:40]}')
```
