# Spotify Playlist Refresh — Implementation Notes

> **⚠️ Update (2026-09-23, v2.17.0):** several May 2026 notes below are superseded — each is marked inline. Current behavior: `references/scoring-and-composition.md`.

## Composer pipeline fixes (May 2026)

### 1. `limit=20` → `limit=10` on `/artists/{id}/albums`

**Symptom**: `_fetch_artist_top_tracks` and `_fetch_artist_graph` returned 0 tracks for all artists → discovery=0, wildcard=0.

**Root cause**: Spotify API for `GET /v1/artists/{id}/albums` hard-rejects `limit > 10` with `400 Invalid limit`. The code used `limit=20`. The 400 error was caught by `if "error" in resp: return []` and silently swallowed.

**Fix**: Changed `limit=20` → `limit=10` in both functions.

Affected lines in `composer.py`:
- Line ~310: `_fetch_artist_top_tracks` — `limit=20` → `limit=10`
- Line ~398: `_fetch_artist_graph` — `limit=20` → `limit=10`
- Line ~297: docstring updated to reflect `limit=10`

### 2. Empty artist_cache entries not re-fetched

**Symptom**: 13 artists cached with `top_tracks=0, albums=0` — `_pick_discoveries` treated these as valid (truthy dict) and never re-fetched.

**Root cause**: Condition `if artist_data:` is True even for `{"name": "...", "top_tracks": [], "albums": [], "cached_at": ...}` — an empty-but-valid cache entry. `_fetch_artist_graph` was skipped entirely for these artists.

**Fix**: Changed to `if artist_data and (artist_data.get("top_tracks") or artist_data.get("albums")):` — forces re-fetch when both are empty. Same pattern applied in `_pick_wildcards`.

### 3. `top_track_ids` typo in `_pick_wildcards`

**Symptom**: `NameError: name 'top_track_ids' is not defined` in wildcard path.

**Root cause**: Pre-existing typo at line ~512 — called `db.upsert_artist_cache(artist_id, name, top_track_ids=top_track_ids, ...)` but `top_track_ids` was not in scope. Correct variable is `top_track_dicts`.

### 4. `heard_track_ids` removal from discovery filter

**Change**: In `_pick_discoveries`, removed the `heard_track_ids` exclusion from discovery candidate pool. Previously all 38 cached tracks were excluded; now only core favorites and blocklist are excluded.

**Effect**: Discovery candidate pool widened — necessary but not sufficient alone (the `limit=20` and empty-cache bugs were the primary failures).

> **Superseded:** the heard-track filter is back — current `_pick_discoveries` drops any candidate already in `play_events` (via `_get_heard_track_ids()`) **or** matching a heard track's same-song signature, so discovery surfaces only unheard songs. The 2.17.0 composer made this affordable by filtering at ingestion and picking with fallthrough (discovery 12/12 offline).

## Token path in `compose_playlist`

*(Rewritten 2026-09-23 — the May 2026 note said `client.get_token()` read SQLite first; that was true of the code until v2.17.0.)*

`client.get_token()` → `auth.get_access_token()` reads `~/.hermes/auth.json` → `providers.spotify.access_token` **first**, then falls back to the SQLite `oauth_token` row. The wrappers (`spotify_refresh_wrapper.py` / `spotify_poll_wrapper.py`) call `resolve_spotify_runtime_credentials(force_refresh=True)` and write the fresh token to `auth.json` atomically; nothing in the skill refreshes the auth.json token itself.

**For dry-run testing without a full wrapper run**: make sure `auth.json` holds a token issued < 1h ago (run a wrapper or `hermes auth spotify`) before calling `compose_playlist`.

## Artist concentration and guardrail behavior

With ~5 days of data (~38 unique tracks, 182 play_events), top artists BABYMONSTER (~30% of plays) and IU dominate. The per-artist guardrail (then `max 2 per artist`; now **3 per section / 4 across the playlist**) correctly removes over-represented tracks. This is expected behavior — not a bug.

**Self-correct path**: As more listening history accumulates over weeks, artist distribution widens and discovery naturally finds room.

## Verification commands

```python
# Composer dry-run (needs a fresh token in auth.json; writes nothing to the DB)
import sys; sys.path.insert(0, '.')
import db, poller, scorer, composer
db.DB_PATH = '/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db'
db.init_db()
scores, _ = scorer.compute_affinity()   # returns (scores, computed_at_ms)
playlist = composer.compose_playlist(scores, dry_run=True)
for t in playlist: print(t['source'], t['name'])
```

```bash
# Clear stale artist_cache entries
sqlite3 ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db \
  "DELETE FROM artist_cache WHERE top_tracks='[]' AND albums='[]'"
```