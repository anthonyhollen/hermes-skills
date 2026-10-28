# spotify-playlist-refresh — Troubleshooting

Relocated from SKILL.md (v2.10.0, 2026-07-04) to keep the hot-path SKILL.md lean.
Symptom → root cause → fix for the common failure modes.

### "Only N tracks composed (target: 40)"
Printed to **stderr** by the composer (the refresh wrapper's Discord banner then appends `⚠️ N/40 tracks — not enough eligible tracks to fill the playlist`). Since v2.17.0 guardrails are applied while picking, and the fallback fills liked → neutral → leftover discovery/wildcard candidates, so a short playlist means the eligible supply is genuinely exhausted (e.g. a nearly empty scoring window after a long listening break, or a rate-limited cold artist graph). After 2–3 days of polling, more events accumulate and composition improves. Offline on the 2026-09-23 DB: 40/40 on every one of 100 seeds.

**Blocklist tracks never re-enter playlist**
Correct behavior — permanent blocklist per spec §5.5. (Before blaming the user's taste, note that bans issued before 2026-09-23 came from the backwards ms_played inference — all 6 then present were lifted by the v2.17.0 migration.) To manually unblock:
```bash
sqlite3 ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db "DELETE FROM blocklist WHERE track_id='...'"
```

**⚠️ Skips not detected by Spotify API — manual blocklist for persistent tracks**
`GET /me/player/recently-played` only records tracks that played ≥ ~30s. Instant skips are invisible — no event recorded, affinity never goes negative. Tracks with high past affinity keep reappearing despite user skips. Fix: manually blocklist. See `references/skip-detection-limitation.md`.

### Tracks show null in `/playlists/{id}/items` GET response
Two distinct causes — check both when the GET looks empty:

1. **Field rename (Feb 2026):** each element of `items[]` now carries the track object under the key **`item`**, NOT `track`. The old `item["track"]` key is now a **boolean type flag** (`true` for tracks), so `it.get("track")` returns `True`/`False` and `it["track"]["name"]` blows up or reads empty. Use `it["item"]` → `{id, uri, name, artists, album, ...}`. See `references/feb-2026-api-audit.md`.
2. **Market blanking:** passing `?market=SG` blanks region-unavailable tracks in the GET (fields come back empty though the track IS in the playlist — confirmed by snapshot-ID change on add/clear). To read names/IDs back for verification, **omit `market`**. Some tracks are region-locked and appear greyed out in-app — a Spotify catalog limitation, not a bug.

> ⚠️ **These two causes look identical and are easy to confuse** (observed 2026-07-28). `(i.get("track") or {}).get("name")` returns `None` for *every* row — 40/40 nulls — which reads exactly like total market blanking. It isn't: cause 1 fires on all rows regardless of market, cause 2 only on genuinely region-locked ones. **Disambiguate by count**: all rows null → key bug (cause 1); a handful null → market blanking (cause 2). Use `i.get("item") or i.get("track") or {}` when reading playlist contents back, and don't reach for the `market` explanation until the key is confirmed right.
>
> Since region-locked tracks are the *real* cause-2 rows, note they are now filtered at composition time — see "Unplayable / greyed-out tracks" below.

### Unplayable / greyed-out tracks reaching the playlist
**Symptom:** a track appears in the playlist greyed out; playing it gives *"this content is not available"*. Reported examples: izna **"Animal Fries"** and **"PROUD CITIZEN"** (2026-07-28).

**Root cause:** Spotify marks these `is_playable: false` with `restrictions: {"reason": "market"}` — the track exists in the catalogue but is not licensed in the user's market (`available_markets` is empty). Discovery and wildcard pull raw track objects straight from `/v1/albums/{id}/tracks`, which *does* carry these fields, but nothing checked them before the Feb 2026 tune-up, so unplayable tracks flowed into the pool like any other candidate.

**Fix (v2.15.0):** `composer._is_unplayable()` drops them at every live-fetch ingestion point (discovery sources a + b, wildcards) and again at pick time (since v2.17.0 via `_passes_output_filters` in `_Picker.try_add`; originally at three separate guardrail sites). Diagnose a suspect track with:
```bash
GET /v1/tracks/{track_id}?market=SG    # → is_playable, restrictions.reason
```

**Gotchas when working on this:**
- `is_playable` is **only returned when `market` is passed**. Without `market` it is absent (`None`) — but `restrictions` is returned either way, which is why the check tests both fields rather than `is_playable` alone.
- **Do not** verify playability by batch-fetching composed tracks: batch `GET /v1/tracks?ids=...` returns **403** (removed Feb 2026), and 40 individual `/v1/tracks/{id}` calls per refresh would collide with the same ~50 req/min "Other" rate limit that already starves wildcards. Filter where the data is already in hand instead.
- `track_cache` stores **no** playability column, and `/me/player/recently-played` does not return `is_playable`/`restrictions` — so there is nothing to filter on for core/fallback tracks. That is acceptable: those tracks are sourced from the user's own play history, so they were playable when played. A track that goes region-locked *after* being played can still slip through; if that is ever observed, the fix is a playability column on `track_cache` backfilled by a periodic job, not a per-refresh verification pass.


(Note: the refresher only *writes* the playlist — `PUT /v1/playlists/{id}/items` — so these read-side quirks affect manual verification, not the refresh itself.)

### Token expiry
Tokens do **not** auto-refresh via `auth.py` (its `_do_refresh()` can't refresh PKCE tokens — `400 invalid_client`). The cron wrappers refresh the token at the start of every run via `resolve_spotify_runtime_credentials(force_refresh=True)` and write it to `~/.hermes/auth.json`, which `auth.get_access_token()` reads first (since v2.17.0; before that a stale SQLite `oauth_token` row could shadow it). A 401 in cron output means the job isn't using a wrapper or the refresh token is dead → `hermes auth spotify`. See `references/cron-wrappers.md`.

### `/me/top/tracks` returns 0 items
Your `time_range` may need adjustment. Try `?time_range=medium_term` or `long_term` to get more historical data:
```
GET /v1/me/top/tracks?time_range=medium_term&limit=20
```

### Source must live inside the skills folder
System source code (Python, configs, DB, scripts) must be placed under `~/.hermes/skills/<category>/<name>/` so it is included in `Hermes Backup` (runs Sundays 03:00 → Google Drive). Standalone `$HOME`-level directories are NOT backed up automatically.

### Discovery underfill with high artist concentration (normal, not a bug)
> **Update (2026-09-23, v2.17.0):** largely resolved — discovery candidates are now filtered at ingestion and the picker falls through to the next candidate when one is capped/filtered, so discovery fills 12/12 offline. The note below is kept as the May 2026 record; the combined cap is 4 (per-section 3).

When top artists dominate listening history (e.g. BABYMONSTER + IU = ~30% of plays), the max-3-per-artist
guardrail cuts discovery candidates aggressively. With ~5 days of data and ~38 unique tracks, artist
diversity is too low for the 24-core + 12-discovery + 4-wildcard split to fill naturally. This is expected
behavior — discovery pool generates correctly but guardrail removes concentrated artists. Self-corrects
as more listening history accumulates over weeks.

### Guardrail over-removal: root cause and symptoms
> **Fixed (2026-09-23, v2.17.0):** `_apply_guardrails` (and the rest of the sample-then-filter pipeline) is gone. `_Picker` applies the per-section cap (3) and the **combined cap (4, not 3)** while picking, with wildcards picked before discovery, so a capped track is replaced by the next candidate instead of leaving a hole for fallback. The analysis below is the historical record.
>
> Target is now 40 tracks (24 core / 12 discovery / 4 wildcard) as of 2026-07-06; the worked example below is from the 20-track (12/6/2) era but the mechanism is identical at any size.

**Symptom**: `compose_playlist` returns fewer than the target, then fallback fills to a skewed composition
(e.g., all-core + little discovery + no wildcard instead of the configured split).

**Root cause** (pre-2.17.0): `_apply_guardrails` (removed 2026-09-23) removed tracks exceeding `max_tracks_per_artist` (then: 3; the combined cap has since been `max_tracks_per_artist_combined` = 4) from the
_combined_ pool (core + discovery + wildcard). When a single artist dominates across all 3 categories,
all their excess tracks are cut at once. Example with BABYMONSTER (affinity 32.0):
- BABYMONSTER has 6 tracks in the raw 20-track pool (3 core + 2 discovery + 1 wildcard)
- Guardrail keeps only 3 → removes 3 tracks
- IU (affinity 14.9) similarly loses 1-2, Croco Beat loses 1
- Total removed: 5-7 → pool drops from 20 to 13-15 → fallback fills back to 20 but with wrong section weights

**Fix at the time** (May 2026): `MAX_PER_ARTIST=3` (raised from 2). Test results: `core=12 disc=3-4 wild=0 fall=4-5`
consistently. Not yet hitting the discovery / wildcard targets — discovery is limited by artist concentration
in the listening history, wildcards are limited by Spotify album API rate limits.

**527/528/529 errors**: These are Spotify server-side (upstream overload or maintenance). Not a client
or token issue. `client.api()` already retries 5xx with bounded backoff inside the stage deadline; otherwise retry on next poll cycle. If persistent for >1 hour, check Spotify status page.

**Wildcards always 0 → rate limiting** *(largely fixed 2026-09-23, v2.17.0: album track lists are cached in `artist_cache` for the first 5 + in-window albums, catalogue GETs are paced 1.2s apart and memoized per run, all top-10 artists and every track of each new release are candidates, and wildcards are picked before discovery — offline wildcard fill 0.06 → ~2.3 of 4. A wildcard is still empty when no liked artist released anything in the last 30 days.)* Historical diagnosis: `_pick_wildcards` calls `GET /v1/albums/{id}/tracks` for each
recent album. This endpoint is subject to Spotify's "Other" rate limit (~50 req/min). After 1–2 album
calls, API returns `"Too many requests"` and `_fetch_album_tracks` returns 0 tracks. Wildcard slot is
never filled; fallback covers it. Not a blocking issue. See `references/wildcard-rate-limit.md` for
diagnosis, fix options, and diagnostic commands.

### `events_stored` in run_log.detail — accurate only from 2026-09-23
Since v2.17.0 `events_stored` is the number of rows actually new this poll (returned by `db.upsert_play_events`), and a poll whose 50 fetched events are all new adds `possible_truncation: true` (+ a stderr warning) — plays may have been lost between polls. Rows logged **before** 2026-09-23 recorded `len(inferred)` (always the fetch size, up to 50), so for historical analysis query the `play_events` table directly. See `references/polling-data-loss.md`.

### Poll / refresh failures in run_log
Every poll failure writes a `run_type='poll', status='error'` row with the exception text before re-raising. `refresh_playlist()` writes a `status='error'` refresh row for every failure before the playlist write (poll auth failure, scoring/compose exception, `composer_empty`, `no_uris`) and for a failed PUT. A transient poll failure (5xx/timeout) inside a refresh is best-effort: a stderr warning, then scoring proceeds on existing history. Check with:
```bash
sqlite3 ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db \
  "SELECT run_id, run_type, status, datetime(started_at/1000,'unixepoch','+8 hours'), detail FROM run_log ORDER BY run_id DESC LIMIT 10"
```

### Skip detection (v2.18) — sampler / digest

- **Is the sampler running?** Run
  `export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user status spotify-sampler`,
  then `journalctl --user -u spotify-sampler -n 50`. The heartbeat is in
  `sampler_state` (`db.get_sampler_state('heartbeat')`) and advances about
  every 30s.
- **Digest says "Sampler saw 0/N recorded plays"** while the service is up:
  - Check the journal for 401s (`hermes auth spotify` if a forced refresh
    didn't help) or 429 backoffs.
  - Check that the phone was online.
  - Offline listening only reaches the fallback (`⏭?` rows).
- **`coverage_miss` rows** are recorded plays that happened while the sampler was
  watching but match no segment. A few are normal: sub-interval passes, or plays
  on another device that isn't the account's active one. Many of them mean the
  match tolerance or the clock is off (`timedatectl`).
- **A track the user skipped keeps coming back.** First confirm
  `skip_signals.enabled` is true (shadow mode changes nothing). Then check its
  rows:
  `SELECT kind, source, at_ms FROM skip_signal WHERE track_id='…' ORDER BY at_ms`.
  A `skim` or `skip` row puts it on a 3-refresh cooldown. Two sampler rows
  within 14 days demote it.
- **Never edit `skip_signal` by hand.** It is rebuilt from scratch on every
  poll. Fix the inputs instead: `play_segment`, `playlist_version`, or the
  thresholds in config.
