# Wildcard Rate-Limit Diagnosis (May 2026)

> **Size note (2026-07-06):** the playlist target is now **40 tracks (24 core / 12 discovery / 4 wildcard)**. Numbers below reflect the earlier 20-track (12/6/2) config and are kept as a point-in-time record.

> **⚠️ Update (2026-09-23, v2.17.0) — fixes now implemented:** option 1 (album track lists cached in `artist_cache` — slim dicts for the first 5 albums + every album in the 30-day window, reused by wildcards) and option 2 (429 `Retry-After` honored when it fits the stage deadline, else fail fast + GET breaker; since 2.7.0/2.17.0) are **done**. Option 3 was deliberately *not* taken — instead all top-10 artists and every track of each new release are candidates, catalogue GETs are paced ≥1.2s apart and memoized per process, and wildcards are picked before discovery so they don't lose the combined per-artist cap. Offline (100 seeds): wildcard fill **0.06 → ~2.3 of 4**. Option 4's `limit=5` is stale: the album listing uses `include_groups=album,single&limit=10` (the endpoint 400s above 10). `_apply_guardrails` (below) was removed in v2.17.0.

## Symptom
`_pick_wildcards` consistently returns 0 tracks on most runs. Pool before guardrail shows `wild=0`.
`_fetch_album_tracks` returns empty list for all albums.

## Root Cause
`GET /v1/albums/{id}/tracks` is subject to Spotify's "Other" rate limit category (~50 requests/minute).
When `_pick_wildcards` iterates over recent albums (≤30 days) for top 5 artists and calls `_fetch_album_tracks`
for each, it triggers rate limiting after just 1–2 requests. The API returns `{"error": {"status": 0, "message": "Too many requests"}}`.

## Evidence
```python
album_id = '1PllcP5dYm6Zjfqzwmqp9U'  # Mario's Secret Birthday (Croco Beat, 2026-05-12)
resp = client.get(f'albums/{album_id}', token=token)
# → {'error': {'status': 0, 'message': 'parse_error: Too many requests'}}
```

Yet albums with `release_date` within 30 days are correctly identified:
- Croco Beat: 8 albums from May 2–12, all within window
- YUJU: `First Love Is a Curse` (2026-04-29, 16 days old)

So `artist_cache` albums data is valid; only the per-album track fetch hits rate limits.

## Impact on Composition
- Wildcard slot (2 tracks): almost always 0 after `_apply_guardrails` (removed 2026-09-23)
- Fallback fills: `core=12` + whatever survived guardrail + fallback fills remaining
- Typical run: `core=12, disc=1-4, wild=0, fall=4-7` → total 20, but wrong section weights

## Not a Blocking Issue
The pipeline still produces 20 tracks and delivers to Discord. Wildcard slot is advisory (new releases
from liked artists). Without it, the playlist is still good — just less "new release" discovery.

## Fix Options (as of May 2026 — see the 2026-09-23 update above for what shipped)
1. **Cache album tracks at discovery time**: When `_pick_discoveries` already fetches album data via
   `_fetch_artist_graph`, also fetch and cache the album track lists. `_pick_wildcards` reads from
   existing cache instead of calling the album API again. Requires restructuring `_fetch_artist_graph`
   to return full track dicts (which it now does for top_tracks, not for album tracks).

2. **Add retry with exponential backoff**: In `_fetch_album_tracks`, catch 429 and retry after
   `Retry-After` header with backoff. Small fix but rate-limit category has low quota regardless.

3. **Reduce wildcard candidates**: Instead of iterating top 5 artists × up to 10 albums each, limit to
   2 artists × 2 recent albums = 4 album calls max. Reduces rate-limit exposure significantly.

4. **Use `GET /v1/artists/{id}/albums?include_groups=single,album&limit=5`** (already used): The album
   listing is working fine; only the per-album track fetch is rate-limited. Could try `GET /v1/search`
   for new releases from liked artists instead.

## Diagnostic Commands
```python
# Check which albums are within 30-day window
import sys; sys.path.insert(0, '.')
from datetime import datetime, timezone
import db, composer, client, scorer
db.DB_PATH='/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db'; db.init_db()
token = client.get_token()
market = client.get_user_market(token)
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
cutoff_ms = now_ms - 30 * 86400 * 1000

artist_affinity = composer._aggregate_artist_affinity(scorer.get_liked_tracks(scorer.compute_affinity()[0]))
top_artists = sorted(artist_affinity.items(), key=lambda x: x[1], reverse=True)[:10]   # composer.TOP_ARTISTS_N
for aid, score in top_artists:
    cached = db.get_cached_artists([aid])
    artist_data = cached.get(aid, {})
    for album in artist_data.get('albums', [])[:3]:
        release_date = album.get('release_date', '')
        age_days = (now_ms - datetime.fromisoformat(release_date).timestamp() * 1000) / 86400000
        print(f'{album.get("name","?")[:40]} | {release_date} | {age_days:.0f}d within_30={age_days <= 30}')
```