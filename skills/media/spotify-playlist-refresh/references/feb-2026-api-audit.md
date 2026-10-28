# Spotify Web API — February 2026 Official Changes

**Source**: [Web API Changelog — February 2026](https://developer.spotify.com/documentation/web-api/references/changes/february-2026)  
**Tested against**: Our app (client_id `c3616d872c07443980727a2beef2e936`, type "App", PKCE auth)

> **Correction (2026-07-04, tune-up) — two claims below are stale vs the live API/code:**
> 1. The `_fetch_artist_top_tracks` algorithm block shows `&limit=20` on `/v1/artists/{id}/albums`, but that endpoint hard-rejects `limit > 10` with `400 Invalid limit`. The **code correctly uses `&limit=10`** (then sorts newest-first and takes the top 5). Read "10 albums", not 20.
> 2. The "User Object field removals" table lists `country`/`email`/`product` as removed, but a live `GET /v1/me` (PKCE, our scopes) on 2026-07-04 still returned all three — `client.get_user_market()` reads `country` and works. Treat those as **NOT removed** for our app.
>
> **Correction (2026-09-23, v2.17.0):**
> 3. "`GET /v1/playlists/{id}` — Returns `snapshot_id` only" is wrong: without a `fields` filter it returns the whole playlist object. `refresher._get_snapshot_id()` now requests `playlists/{id}?fields=snapshot_id` so only that field comes back.
> 4. The refresher does **not** clear-then-add: it replaces the playlist in **one** `PUT /v1/playlists/{id}/items` with the new `uris` (≤100; overflow POSTed), so a failed write leaves the old playlist intact. `PUT {"uris": []}` (clear) still works but is not used.

---

## Endpoint Changes

### ❌ REMOVED (no longer available)

| Old endpoint | Replacement | Notes |
|---|---|---|
| `GET /v1/artists/{id}/top-tracks` | — | **Fully removed.** Replaced by `_fetch_artist_top_tracks()` in `composer.py`. |
| `POST /v1/users/{user_id}/playlists` | `POST /v1/me/playlists` | |
| `GET /v1/markets` | — | Not used by our pipeline. |
| `GET /v1/browse/new-releases` | — | Not used. |
| `GET /v1/artists` (batch) | — | Use single `/v1/artists/{id}` per artist. |
| `GET /v1/albums` (batch) | — | Use single `/v1/albums/{id}` per album. |
| `GET /v1/tracks` (batch) | — | Use single `/v1/tracks/{id}` per track. |
| `GET /v1/browse/categories` | — | Not used. |
| `GET /v1/browse/categories/{id}` | — | Not used. |
| `GET /v1/audiobooks` (batch) | — | Not used. |
| `GET /v1/chapters` (batch) | — | Not used. |
| `GET /v1/episodes` (batch) | — | Not used. |
| `GET /v1/shows` (batch) | — | Not used. |
| `GET /v1/users/{id}` | — | Not used. |
| `GET /v1/users/{id}/playlists` | — | Not used. |
| `PUT /v1/me/albums` | `PUT /v1/me/library` | |
| `PUT /v1/me/tracks` | `PUT /v1/me/library` | |
| `DELETE /v1/me/albums` | `DELETE /v1/me/library` | |
| `DELETE /v1/me/tracks` | `DELETE /v1/me/library` | |
| `DELETE /v1/me/audiobooks` | `DELETE /v1/me/library` | |
| `DELETE /v1/me/episodes` | `DELETE /v1/me/library` | |
| `DELETE /v1/me/shows` | `DELETE /v1/me/library` | |
| `GET /v1/me/albums/contains` | `GET /v1/me/library/contains` | |
| `GET /v1/me/tracks/contains` | `GET /v1/me/library/contains` | |
| `PUT /v1/me/following` | `PUT /v1/me/library` | |
| `DELETE /v1/me/following` | `DELETE /v1/me/library` | |
| `GET /v1/me/following/contains` | `GET /v1/me/library/contains` | |
| `POST /v1/playlists/{id}/tracks` | `POST /v1/playlists/{id}/items` | Old still works (May 2026) but deprecated. |
| `GET /v1/playlists/{id}/tracks` | `GET /v1/playlists/{id}/items` | Old returns 403 on our app type. New works. |
| `DELETE /v1/playlists/{id}/tracks` | `DELETE /v1/playlists/{id}/items` | Old still works but deprecated. |
| `PUT /v1/playlists/{id}/tracks` | `PUT /v1/playlists/{id}/items` | Old still works but deprecated. |
| `GET /v1/playlists/{id}/followers/contains` | `GET /v1/me/library/contains` | |
| `PUT /v1/playlists/{id}/followers` | `PUT /v1/me/library` | |

### ✅ ADDED (new endpoints)

| Endpoint | Notes |
|---|---|
| `DELETE /v1/me/library` | Remove items (tracks, albums, audiobooks, episodes, shows) from library. |
| `PUT /v1/me/library` | Save items to library. |
| `GET /v1/me/library/contains` | Check if items are saved in library. |
| `POST /v1/playlists/{id}/items` | Add tracks/episodes to playlist (replaces `/tracks` variant). |
| `GET /v1/playlists/{id}/items` | Get full playlist contents (replaces `/tracks` variant). |
| `DELETE /v1/playlists/{id}/items` | Remove items from playlist (replaces `/tracks` variant). |
| `PUT /v1/playlists/{id}/items` | Replace/reorder playlist items (replaces `/tracks` variant). |

### ⚠️ CHANGED

| Endpoint | Change |
|---|---|
| `GET /v1/search` | `limit` max reduced from 50 → 10. Default changed from 20 → 5. Our code always uses ≤10, so no change needed. |
| `GET /v1/playlists/{id}/items` | **Response shape changed** — each `items[]` element now nests the track object under the key **`item`** (was `track`). The old `track` key is now a **boolean type flag** (`true` for a track), so `item["track"]["name"]` no longer works — read `item["item"]` → `{id, uri, name, artists, album, ...}`. Verified live 2026-07-09. Also: `?market=SG` blanks region-unavailable tracks in the response (track IS in the playlist regardless) — omit `market` to read names/IDs back. |

---

## Field Removals (affects responses across all endpoints)

| Content type | Removed fields |
|---|---|
| **Album** | `album_group`, `available_markets`, `external_ids`, `label`, `popularity` |
| **Artist** | `followers`, `popularity` |
| **Track** | `available_markets`, `external_ids`, `linked_from`, `popularity` |
| **User** | `country`, `email`, `explicit_content`, `followers`, `product` |
| **Audiobook** | `available_markets`, `publisher` |
| **Chapter** | `available_markets` |
| **Show** | `available_markets`, `publisher` |

---

## Impact on Our Pipeline

### Endpoints we call — status

| Endpoint | Status | Implication |
|---|---|---|
| `GET /v1/me` | ✅ Works | No `product` field — we don't use it |
| `GET /v1/me/top/tracks` | ✅ Works | `time_range=medium_term` useful if short returns 0 |
| `GET /v1/me/player/recently-played` | ✅ Works | |
| `GET /v1/artists/{id}` | ✅ Works | Only `id` + `name` returned — no `genres`/`popularity` |
| `GET /v1/artists/{id}/albums` | ✅ Works | Requires `market` param |
| `GET /v1/albums/{id}/tracks` | ✅ Works | |
| `GET /v1/search` | ✅ Works | `limit` capped at 10 (our usage is fine) |
| `POST /v1/playlists/{id}/items` | ✅ Works | 201 — add tracks |
| `PUT /v1/playlists/{id}/items` | ✅ Works | 200 — replace/clear (the refresher's single atomic replace) |
| `GET /v1/playlists/{id}` | ✅ Works | Returns `snapshot_id` only *(stale — see correction 3 above; use `?fields=snapshot_id`)* |
| `GET /v1/playlists/{id}/items` | ✅ Works | User's own playlist — full contents |
| `DELETE /v1/playlists/{id}/items` | ✅ Works | Remove by position |

### Code changes needed

**All implemented as of May 2026:**

1. **`/v1/artists/{id}/top-tracks`** — replaced by `_fetch_artist_top_tracks()` in `composer.py`. Full algorithm below.

2. **Playlist write endpoints** — `refresher.py` already uses `POST/PUT /playlists/{id}/items` (Feb 2026 canonical names). No further migration needed.

3. **Search limits** — our code uses `limit=1` (artist lookup) and `limit=5` (track search), both safely under the cap of 10.

4. **Artist metadata** — we only used `id` and `name` from artist responses, both still present. `_aggregate_artist_affinity()` in `composer.py` relies on `artist_ids` from track cache (not artist endpoint), so no impact.

---

## Full `_fetch_artist_top_tracks()` Algorithm

```
Input: artist_id, market, token
Output: list[track_dict] (up to 10 representative tracks)

1. GET /v1/artists/{id}/albums
       ?include_groups=album,single
       &market={market}
       &limit=20
   → items[]

2. Sort items by release_date desc (newest first)
   → pick top 5 albums

3. For each album (in order):
     GET /v1/albums/{album_id}/tracks
         ?market={market}
         &limit=50
     → for each track:
         - artist must appear in track.artists[].id
           (skips features on collab albums where artist isn't primary)
         - track_id not already seen (dedup across albums)
         - duration_ms == 0 OR duration_ms >= 60000
           (skip interludes/intros < 60s)
         - append to result

4. Return first 10 from result[]
   (or all if fewer than 10 found)
```

Used by `_fetch_artist_graph()` to replace the removed endpoint. Also called by `_pick_wildcards()` via cached artist data.

Key differences vs. old `/v1/artists/{id}/top-tracks`:
- No artist-filter: old endpoint returned top tracks from Spotify's popularity algorithm
- New approach: newest albums → most likely to contain tracks the artist is still actively promoting
- Duration filter removes interludes (a real problem with album-track mining)
- Artist filter removes collab features that would pollute the candidate pool

---

## App-level write restrictions & ID quirks (relocated from SKILL.md, 2026-09-23)

Other write restrictions (app-level, not per-token):

| Operation | Status | Notes |
|---|---|---|
| `POST /v1/playlists/{id}/items` (add) | **201** ✓ | Working |
| `PUT /v1/playlists/{id}/items` (replace / clear) | **200** ✓ | Working — the refresher's one-shot replace; `{"uris": []}` clears |
| `DELETE /v1/playlists/{id}/items` (remove by position) | **200** ✓ | Working |
| `PUT /v1/playlists/{id}/details` | **403** | Cannot update playlist metadata |
| `POST /v1/playlists` | **403** | Cannot create playlists |
| `PUT /v1/me/tracks` | **403** | Cannot save to library |

**Non-standard artist IDs from nested track data.** IDs from `track.artists[].id`
inside `/me/player/recently-played` are sometimes non-standard-length and return
`400 Invalid base62 id` on `GET /v1/artists/{id}`. Resolve the full 22-char ID via:
```
GET /v1/search?q=artist:{name}&type=artist&limit=1
```
