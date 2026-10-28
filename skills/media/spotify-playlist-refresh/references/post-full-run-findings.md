# Post Full Pipeline Run Findings — May 2026

> **Size note (2026-07-06):** the playlist target is now **40 tracks (24 core / 12 discovery / 4 wildcard)**. Numbers below reflect the earlier 20-track (12/6/2) config and are kept as a point-in-time record.

> **Correction (2026-09-23, v2.17.0):** two claims below are stale. (1) `GET /v1/playlists/{id}` does not return "snapshot_id only" — unfiltered it returns the whole playlist object; `_get_snapshot_id()` now calls `playlists/{id}?fields=snapshot_id`. (2) The refresher no longer clears (`PUT {"uris": []}`) then adds — it replaces the contents in one atomic `PUT /v1/playlists/{id}/items` with the new uris. Also, the "Poll: 20 events" / per-event `GET /v1/tracks/{id}` description is historical: polls fetch `limit=50` and cache metadata straight from the recently-played response (no extra calls).

## What worked

- **Token**: Fresh auth via `hermes auth spotify` (PKCE, browser callback to `http://127.0.0.1:8888/callback`). Token stored in `~/.hermes/auth.json` → `providers.spotify.access_token`.
- **Poll**: 20 events fetched → 20 stored (all from `recently-played` → `track_cache` via `GET /v1/tracks/{id}` per-event — no batch endpoint needed).
- **Score compute**: 19 tracks scored (1 liked: PSYCHO, 18 neutral).
- **Composition**: 20/20 tracks hit (target), via fallback chain.
- **Discovery**: 1 track (DRIP Live Remix) from BABYMONSTER album tracks.
- **Refresher end-to-end**: `refresh_playlist()` returned `{'status': 'ok', 'tracks': 20, 'scores': 28}`.
- **Snapshot write**: `affinity_snapshot` table written with `run_id` from `run_log`.
- **Vacuum logic**: Weekly `incremental_vacuum` conditional on `datetime.utcnow().weekday() == vacuum_weekday` (Sunday=6).

## Critical API limitations discovered

### 1. `/v1/artists/{id}/top-tracks` — Removed in Feb 2026
This endpoint is **no longer available** for any app type — officially removed per Spotify Web API Feb 2026 changelog.

**Fix implemented**: `_fetch_artist_top_tracks()` in `composer.py` replaces it using the Feb 2026 algorithm:
- Fetch 20 albums → sort by release_date desc → take top 5 newest
- For each: fetch album tracks → filter to tracks where `artist_id` appears in `track.artists[]`
- Dedupe by track_id + filter tracks < 60s → return up to 10 representative tracks

> **Correction (2026-07-04):** "Fetch 20 albums" is stale — `/v1/artists/{id}/albums` 400s on `limit>10`, so the code fetches `&limit=10` then takes the newest 5. See CHANGELOG 2.10.0.

**Impact after fix**: Discovery source (a) is now fully functional. Discovery quality depends only on how many albums the artist has released recently.

### 2. `GET /v1/playlists/{id}/tracks` → 403 Forbidden
Playlist contents cannot be read by this app. Cannot verify track list post-refresh via GET.

**Verification method**: Use snapshot ID diff (`_get_snapshot_id()` calls `GET /v1/playlists/{id}` which returns snapshot_id without needing track read). The `snapshot_id` changes on any playlist mutation, confirming the write succeeded.

### 3. `GET /v1/me/player/currently-playing` → 200 with `currently_playing: null`
Non-Premium users return null. Poller does not depend on this endpoint — it uses `recently-played` only. No impact.

### 4. `GET /v1/audio-features?ids=...` → 403
Deprecated for third-party apps. Scoring uses completion ratio only (no audio features) — spec §5 design accounts for this.

## Discovery underfill — root cause chain (pre-fix, for reference)

```
Only 1 liked artist (BABYMONSTER, score 3.43)
  ↓ /top-tracks 403 (endpoint removed Feb 2026) → 0 top-tracks from source (a)
  ↓ /albums works → 5 albums found
  ↓ most albums are old Live Version / compilation albums
  ↓ only 1 album (DRIP, Nov 2024) has recent-enough tracks
  ↓ 17 candidate album tracks → 6 selected by weighted_sample
  ↓ discovery = 1 track (DRIP Live Remix)
  ↓ wildcard = 0 (all BABYMONSTER albums either too old or compilation)
  ↓ core = 1 track (PSYCHO — only liked track)
  ↓ Total: 1 + 1 + 0 + 18 fallback = 20 ✓
```

**Post-fix status (May 2026)**: `_fetch_artist_top_tracks()` now uses the album-track algorithm, so source (a) is available. Discovery quality improves as liked artists accumulate over polling days.

**Recovery path**: As polling continues daily, more liked artists accumulate → more artists in `top_artists` → more album discovery candidates. After ~1 week of polling: expect 5-8 discovery tracks and 2 wildcards.

## Token expiry — confirmed failure mode

`hermes auth spotify` stores **no client_secret** in `auth.json`. When token expires, auto-refresh fails with `400 invalid_client`. The `spotify` skill documents this as does `spotify-playlist-refresh` SKILL.md.

**Symptom in this session**: `401 Invalid access token` on all API calls, then `refresh_playlist()` returned `{'status': 'ok', 'tracks': 20}` despite no actual playlist change happening.

**Recovery**: `hermes auth spotify` (browser PKCE flow, paste callback URL back into terminal).

## Verified working endpoints (May 2026)

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `/v1/me` | GET | 200 | Returns `country: SG` — used for market param |
| `/v1/me/top/artists` | GET | 200 | Works, used in artist-graph |
| `/v1/me/player/recently-played` | GET | 200 | 20 events per poll, primary data source |
| `/v1/artists/{id}` | GET | 200 | Artist metadata |
| `/v1/artists/{id}/albums?market=SG&limit=5` | GET | 200 | Works for all tested artists |
| `/v1/albums/{id}/tracks?market=SG` | GET | 200 | Works |
| `/v1/playlists/{id}` | GET | 200 | Returns `snapshot_id` for verification |
| `/v1/playlists/{id}/items` (clear) | PUT | 200 | `{"uris": []}` body |
| `/v1/playlists/{id}/items` (add) | POST | 201 | `{"uris": [...]}` body |
| `/v1/playlists/{id}/tracks` | GET | **403** | App type limitation |
| `/v1/artists/{id}/top-tracks?market=SG` | GET | **403** | App type limitation |
| `/v1/artists/{id}/related-artists` | GET | **403** | Deprecated for new apps |
| `/v1/recommendations` | GET | **404** | Fully removed |
| `/v1/audio-features?ids=...` | GET | **403** | Deprecated for new apps |
| `/v1/tracks?ids=...` | GET | **403** | Deprecated for new apps |
| `/v1/me/player/currently-playing` | GET | 200 | `currently_playing: null` for non-Premium |