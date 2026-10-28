# Region Boundary Logic

## Non-overlapping boundaries

```
North:  lat >= 1.38
South:  lat <  1.28
East:   1.28 <= lat < 1.38 AND lon >= 103.88
West:   1.28 <= lat < 1.38 AND lon <  103.80
Center gap: 1.28-1.38, 103.80-103.88 → assigned to nearest region center
```

## Why non-overlapping matters

Original version used overlapping bounding boxes (e.g. North lat ≥ 1.38 AND East lat 1.28-1.40). This caused cameras at boundary latitudes to appear on multiple maps. Total camera count exceeded 90 across the 4 maps.

Fix: make East/West lat_max = 1.38 (same as North lat_min) so no overlap.

## Center gap assignment

Cameras in the center gap (lat 1.28-1.38, lon 103.80-103.88) aren't in any region's bounding box. The center-gap branch of the `assign_region()` function assigns each to the nearest region center:

| Region | Center |
|--------|--------|
| North | 1.43, 103.80 |
| South | 1.22, 103.84 |
| East | 1.33, 103.96 |
| West | 1.33, 103.70 |

This can put central Singapore cameras (Bishan, Toa Payoh, Thomson) into unexpected regions based on distance to region center. For example:
- Thomson Flyover (1.329, 103.841) → essentially equidistant from the North and South centers (both ≈0.1090) → resolves to **North** (North is tested first and the comparison is strict `<`, so an exact tie keeps the earlier region)
- Keng Lee Rd (1.314, 103.846) → closer to South center → assigned to South

## Pitfall: South map got 15 cameras when only 8 are strictly south

The South region (lat < 1.28) has only 8 cameras. The other 7 were center-gap cameras assigned to South by nearest-center logic. This is correct behavior but can be confusing — the user expects "South Singapore" to show only CBD/Marina/Sentosa cameras, not central ones.

**Mitigation:** The geocoded addresses make it obvious which cameras are truly south vs assigned from the center gap. If the user complains about specific cameras being in the wrong region, adjust the center-gap assignment logic or exclude gap cameras from all regions.
