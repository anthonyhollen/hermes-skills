# Spotify Playlist Refresh — Implementation Reference

> **⚠️ Update (2026-06-07, v2.8.0):** Two sections below are out of date — see `SKILL.md` / `CLAUDE.md` for current behavior:
> - **"Scoring self-correction logic":** the last event is no longer "assumed full play then recomputed." It is now stored as `ms_played = NULL` (pending) and corrected when a later poll reveals its gap, via the self-correcting upsert in `db.upsert_play_events`. *(Superseded 2026-09-23 — see the next update.)*
> - **"Cron jobs (current active set)":** that table is stale. The live jobs are `f5e75496918a` (refresh, `0 3 * * *`), `afdb0b06e11a` (poll, `0 4,7,10,13,16,17,19,22,23 * * *`), and `dfadaf5e1833` (poll, `0 2 * * *`) — last confirmed live 2026-07-14; see `references/cron-wrappers.md`.
>
> **⚠️ Update (2026-09-23, v2.17.0):**
> - **ms_played inference direction was wrong.** `played_at` is the play END, so each event's listen time is the gap back to the *previous* event (`min(dur_i, played_at_i − played_at_{i−1})`); the newest event is final, not pending. Both the "assumed full play" and the "NULL pending" stories are historical. See `references/scoring-and-composition.md` §1.
> - **API-call counts:** "~80 calls cold / ~1 warm" below is superseded. Measured offline on v2.17.0 (100 seeds): cold **92 → 52**, warm **49 → 1** (the warm figure is transitional until pre-2.17 `artist_cache` rows, which lack album track lists, expire at the 36h TTL).
> - **Discovery step 3** (`/artists/{id}/top-tracks`) — that endpoint was removed Feb 2026; replaced by `_fetch_artist_top_tracks()`. **Step 5** is now an exact weighted order (`_weighted_order`) taken by `_Picker`, 12 tracks, not "sample 6".
> - **"Correct Modern Endpoints":** the refresher replaces the playlist in **one** `PUT /v1/playlists/{id}/items` with the new uris — it does not clear first.
> - **"Token storage":** `auth.py` reads `~/.hermes/auth.json` directly (JSON), not via `hermes auth token read`; since v2.17.0 it prefers auth.json over the SQLite `oauth_token` row.

## API Constraints (all confirmed live)

### `GET /v1/tracks?ids=...` → 403 Forbidden
**Discovered**: 2026-05-09. Even with all 10 custom scopes granted (user-top-read, user-read-recently-played, playlist-modify-public, etc.), batch track metadata fetch returns 403.

**Root cause**: App type in Spotify Developer Dashboard is registered as "App" (not "Web App" or "User"). Write operations are blocked at the app level regardless of scopes.

**Workaround**: Extract metadata (name, artist_ids, duration_ms) from the `recently-played` response directly. No extra API call needed since v1.2 (metadata is already in the response).

### `GET /v1/audio-features?ids=...` → 403 Forbidden
Same app-level restriction as above. Audio features are unavailable. Scoring uses completion ratio (ms_played/duration_ms) only.

### `GET /v1/recommendations` → 404 Not Found
**Confirmed deprecated** via official Spotify Web API docs (Nov 2024).

**Discovery replacement (v1.2+)**: Artist-graph algorithm (§6.2 spec). See `references/impl-notes.md` for the full call sequence. Briefly:
1. Aggregate affinity by artist from liked tracks (via `artist_cache` metadata)
2. Take top 10 artists by aggregated affinity
3. For each: `GET /artists/{id}/top-tracks?market=SG` + `GET /artists/{id}/albums?...&market=SG` → album tracks
4. Filter heard tracks, blocklist, core favorites, wildcards
5. Weighted-random sample 6, weight = artist affinity
~80 API calls cold (10 artists × 8 calls), ~1 call warm (artist_cache populated). *(Superseded 2026-09-23 — see header: measured cold 92 → 52, warm 49 → 1.)*

### `GET /v1/artists/{id}/related-artists` → 403 Forbidden
Same app-level restriction. Artist-graph discovery (§6.2) uses `/artists/{id}/top-tracks` as the primary artist signal instead — no need for related-artists.

### Artist-graph requires `market` param
`GET /v1/artists/{id}/top-tracks` and `/v1/artists/{id}/albums` return `400 Bad Request` without `?market=XX`. Get the user's market once via `GET /v1/me` (field: `country`), cache it in `client._USER_MARKET`, pass it to all artist-graph calls. Fallback: `"SG"`.

### Non-standard Artist IDs in track_cache
Some artist IDs from `recently-played` are non-standard-length (~24 chars instead of Spotify's 22-char base62). These return `400 Invalid base62 id` on `GET /v1/artists/{id}`. When this happens the track is skipped for artist-graph purposes — it won't warm `artist_cache`. The next poll run (which fetches fresh track data from the `recently-played` response) should contain properly-formed IDs and correct this. See `references/impl-notes.md` §"Non-standard Artist IDs in track_cache" for details.

### Search `limit` hard cap = 10
Any `GET /v1/search?...&limit=N` where N > 10 returns `400 Invalid limit`. Must batch across artists/genres.

## Correct Modern Endpoints

All `/items` endpoints (not `/tracks`):

```
PUT    /v1/playlists/{id}/items    body: {"uris": []}              → clear playlist (200)  # not used: the refresher PUTs the new uris directly (2026-09-23)
POST   /v1/playlists/{id}/items    body: {"uris": ["spotify:track:..."]} → add (201)
DELETE /v1/playlists/{id}/items    body: {"positions": [...], "snapshot_id": "..."} → remove by position
```

The `spotify_playlists remove_items` Hermes tool uses the old `/tracks` endpoint (403). Use `subprocess.run(["curl", ...])` directly.

## Recently-played response fields

The `track` object in `GET /me/player/recently-played` includes:
- `id`, `name`, `duration_ms`, `artists[{id, name}]`
- **`popularity` is NOT included** — always `null` in recently-played response

Since v1.2, metadata is cached directly from this response — no extra API calls needed.

## Token storage

Hermes Agent stores Spotify token at:
```
~/.hermes/auth.json → providers.spotify.access_token  (Bearer token)
~/.hermes/auth.json → providers.spotify.refresh_token
~/.hermes/auth.json → providers.spotify.expires_at    (unix seconds)
```

Auth module (`auth.py`) reads this via `hermes auth token read --provider spotify`. *(Incorrect — corrected 2026-09-23, see header: it reads the JSON file directly.)*

## DB schema (WAL mode, auto_vacuum=INCREMENTAL)

```python
PRAGMA journal_mode=WAL;
PRAGMA auto_vacuum = INCREMENTAL;  # must be set before any CREATE TABLE

# Tables (v1.2):
#   play_events      — track_id, played_at, ms_played, duration_ms
#   track_cache     — track_id, name, artist_ids, duration_ms, cached_at  (slimmed v1.2)
#   artist_cache    — artist_id, name, top_tracks, albums, cached_at       (NEW v1.2)
#   blocklist        — permanent ban once affinity ≤ -1.0
#   run_log          — every poll/refresh logged with status + detail JSON
#   affinity_snapshot — write-only, 90-day TTL, weekly vacuum

# Key indexes:
#   idx_events_time, idx_events_track on play_events
#   idx_snapshot_time, idx_snapshot_track on affinity_snapshot
```

## Scoring self-correction logic

On every poll, the previously-last event (which was assumed to be a full play) gets its ms_played recomputed using the new first event's timestamp as the gap boundary. This means scores improve over time as more data accumulates — no permanently stuck full-play events.

## Cron jobs (May 2026 set — historical, superseded; see header)

| Job ID | Schedule | Purpose | Deliver |
|---|---|---|---|
| `67574ba0eea2` | `0 10 * * 1-5` | Morning poll (weekday) | local |
| `be9a74dd4665` | `0 20 * * 1-5` | Evening poll (weekday) | local |
| `f1bfe614e3ca` | `0 20 * * 0,6` | Evening poll (weekend) | local |
| `f5e75496918a` | `0 3 * * *` | Daily refresh | Discord 1499972245437550602 |
| `02979b83960d` | `15 9 * * 6` | **PAUSED** (old weekly schedule) | — |

Old weekly cron (`02979b83960d`) is paused. It should be deleted once the daily system is confirmed stable for a full week.