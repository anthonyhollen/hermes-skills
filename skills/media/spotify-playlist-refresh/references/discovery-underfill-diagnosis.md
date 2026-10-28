# Discovery Underfill — Post-v1.2 Dry Run Diagnosis (2026-05-10)

> **Size note (2026-07-06):** the playlist target is now **40 tracks (24 core / 12 discovery / 4 wildcard)**. Numbers below reflect the earlier 20-track (12/6/2) config and are kept as a point-in-time record.

After migrating to v1.2 and running the first dry-run, `_pick_discoveries` returned 0 candidates, causing `⚠️ Only 17 tracks composed (target: 20)` with all 3 missing tracks being discovery/wildcard slots.

## Root Cause Chain

1. `track_cache` contained 2 rows with **non-standard artist IDs** (~24 chars instead of Spotify's standard 22-char base62)
2. `_aggregate_artist_affinity()` failed to look up real Spotify artist IDs for these tracks (calls `db.get_cached_tracks` which returned the non-standard IDs)
3. Top-artists list was truncated to ~2 artists with non-standard IDs
4. `_fetch_artist_graph()` called `GET /v1/artists/{non-standard-id}` → `400 Bad Request` ("Invalid base62 id")
5. `artist_cache` never populated → discovery pool stayed empty

## Symptom in Logs
```
⚠️ Only 17 tracks composed (target: 20)
  1-17: core + fallback (all scored liked tracks used)
  18-20: missing (discovery 0/6, wildcard 0/2)
```

## Self-Heal Mechanism
The next `poll_listening_events()` run fetches fresh track metadata from the `recently-played` API response, which contains properly-formed 22-char Spotify artist IDs. These get cached into `track_cache` and replace the non-standard IDs. The next `refresh_playlist()` run will then:
1. Resolve real artist IDs via `_aggregate_artist_affinity()`
2. Call `GET /v1/artists/{id}/top-tracks?market=SG` successfully
3. Populate `artist_cache`
4. Discovery and wildcard slots fill normally

**No manual intervention required.** The system self-corrects on the next polling cycle.

## Diagnostic Query
```sql
-- Check which artist_ids in track_cache look malformed
SELECT track_id, artist_ids, length(artist_ids) as id_len
FROM track_cache
WHERE length(artist_ids) > 50;  -- JSON array with long IDs

-- Expected: after next poll, this should return 0 rows
```

## Lesson
The `artist_cache` warming depends on `artist_ids` being real Spotify artist IDs. Non-standard IDs (ISRC-resolved, nested episode references, or from legacy metadata) silently fail the artist-graph API calls. The system recovers automatically via the next poll cycle — no rollback or manual cleanup needed.