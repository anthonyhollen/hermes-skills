# Google Places API — Rating Lookup Notes

## API Choice: Places API v1 (New), not legacy

The project has a Google API key registered on a GCP project where the **legacy Maps Places API is disabled** (returns `REQUEST_DENIED: You're calling a legacy API...`). Use the **Places API (New)** endpoints instead. The key is loaded from `~/.hermes/.env` (`GOOGLE_API_KEY`).

## Endpoints

### Text Search (recommended for rating lookup)
```
POST https://places.googleapis.com/v1/places:searchText?key=YOUR_KEY
Headers: X-Goog-FieldMask: places.displayName,places.rating,places.id
Body: {
  "textQuery": "<RestaurantName> Singapore",
  "locationBias": {"circle": {"center": {"latitude": LAT, "longitude": LON}, "radius": 300}},
  "includedType": "restaurant",
  "maxResultCount": 3
}
```

### Fields needed for rating only
```
X-Goog-FieldMask: places.displayName,places.rating,places.id
```
Avoid `*` or overly broad field masks — they increase response size and may cause quota issues.

## Fuzzy matching to OSM names

Places API returns a list of candidates (max 3 per search). Match the best one back to the OSM restaurant name using `difflib.SequenceMatcher` ratio — pick the candidate with the highest ratio. This handles minor spelling differences (e.g. "Fong Sheng Hao" vs "Fong Sheng Hao 豐盛號 Paya Lebar Quarter").

## Key constraints discovered (May 2026)

- **`maxResultCount: 3`** — per request, not unlimited. Batch accordingly.
- **0.2s sleep between calls** — prevents quota errors; Places API is rate-limited.
- **No rating found** — some SG restaurants aren't in Google's Places database. Treat as "below threshold" (don't include in recommendations).
- **Rating is a float** — e.g. 4.3. Compare with `>= MIN_GOOGLE_RATING (4.0)`.
- **Quota** — the two-pass approach (score all → check top 10 → fill from next 10) uses ~20 calls vs 192 for the naive approach. Monitor GCP console if quota warnings appear.
- **Content-Type** — use `application/json` with the v1 endpoint; form-encoded is only for Overpass API.

## Rating display format

```python
stars = "★" * int(rating) + "☆" * (5 - int(rating))
# e.g. 4.3 → "★★★★☆"
```

## Quick verification

```bash
curl -s "https://places.googleapis.com/v1/places:searchText?key=YOUR_KEY" \
  -H "X-Goog-FieldMask: places.displayName,places.rating" \
  -H "Content-Type: application/json" \
  -d '{"textQuery": "Fong Sheng Hao Singapore", "locationBias": {"circle": {"center": {"latitude": 1.3732651, "longitude": 103.9483979}, "radius": 300}}, "includedType": "restaurant"}' | python3 -m json.tool
```