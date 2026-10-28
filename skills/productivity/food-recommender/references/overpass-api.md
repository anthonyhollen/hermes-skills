# Overpass API — SG-specific Notes

## Content-Type workaround

When calling Overpass API via direct HTTP (bypassing `maps_client.py`), you must use form-encoded POST, NOT JSON:

```python
# ✅ Correct
overpass_q_encoded = urllib.parse.urlencode({"data": overpass_query})
req = urllib.request.Request(url, data=overpass_q_encoded.encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "..."})

# ❌ Wrong — returns HTTP 400
data = json.dumps({"data": overpass_query}).encode()
req = urllib.request.Request(url, data=data,
    headers={"Content-Type": "application/json", "User-Agent": "..."})
```

This caused a 2-hour debug session when building `food_recommender.py` (May 2026). The form-encoded endpoint is `https://overpass-api.de/api/interpreter` and `https://overpass.kumi.systems/api/interpreter`.

## Radius for SG (Pasir Ris)

3km radius returns ~192 restaurants near Pasir Ris Grove (1.371743, 103.944818). For Innovis (1.298465, 103.788747) at Fusionopolis, 3km returns ~420 restaurants — a denser urban area. OSM data completeness in SG is moderate; popular chains may be missing. The fuzzy filter at 70% threshold catches most known-order matches.

## Fallback

If Overpass returns 0 results, fall back to 5km radius (documented in food_recommender.py as a known edge case).