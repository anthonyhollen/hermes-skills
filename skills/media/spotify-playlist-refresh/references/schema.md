# SQLite schema & config.yaml reference

Relocated from SKILL.md (v2.17.0, 2026-09-23). Source of truth: `src/db.py`
(`SCHEMA` + `_migrate`) and `spotify-refresh/config.yaml`.

## Tables

```sql
play_events    (track_id TEXT, played_at INTEGER, ms_played INTEGER, duration_ms INTEGER, PRIMARY KEY (track_id, played_at))
track_cache    (track_id TEXT PRIMARY KEY, name, artist_ids TEXT, duration_ms, cached_at, album_name)  -- album_name added 2026-08-28 (album-name exclusion rule)
artist_cache   (artist_id TEXT PRIMARY KEY, name, top_tracks TEXT, albums TEXT, cached_at)  -- 36h TTL (was 24h before v2.17.0)
blocklist      (track_id TEXT PRIMARY KEY, reason TEXT, banned_at INTEGER)
oauth_token    (id INTEGER PRIMARY KEY CHECK (id=1), access_token TEXT, refresh_token TEXT, expires_at INTEGER)
run_log        (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_type TEXT, started_at INTEGER, finished_at INTEGER, status TEXT, detail TEXT)
affinity_snapshot (run_id INTEGER, track_id TEXT, affinity REAL, computed_at INTEGER, PRIMARY KEY (run_id, track_id))
migrations     (name TEXT PRIMARY KEY, applied_at INTEGER)  -- created by scripts/migrate_2026_09_played_at_end.py, not by db.py
-- v2.18.0 (2026-09-30) skip signals — see references/skip-signals.md
play_events.context_uri TEXT                  -- recently-played context (spotify:playlist:…); NULL before v2.18 (the first
                                               -- post-deploy poll backfilled the last 50 plays via the COALESCE upsert)
playlist_version       (version_id INTEGER PK AUTOINCREMENT, applied_at, snapshot_id, run_id, origin 'refresh'|'bootstrap')
playlist_version_track (version_id, position 1..N, track_id, source, name, PRIMARY KEY (version_id, position))
player_sample  (t_ms, status, track_id, progress_ms, duration_ms, context_uri, shuffle, smart_shuffle, repeat_state,
                device_type, spotify_ts, rtt_ms)          -- raw non-idle /me/player samples, 14 days
play_segment   (segment_id PK, track_id, data JSON, first_seen, exit_hi, end_reason)  -- one row per observed exposure, 180 days
sampler_coverage (from_ms PK, to_ms)                    -- when the sampler was watching (fallback only runs outside)
sampler_state  (key PK, value JSON, updated_at)         -- 'segmenter' (open segment, resumable) + 'heartbeat'
skip_signal    (track_id, at_ms, kind, source, tier, exposure_ms, exposure_hi_ms, duration_ms, end_reason, version_id,
                position, context_uri, segment_id, played_at, score_ms, PRIMARY KEY (track_id, at_ms, kind, source))
                                                        -- DERIVED: fully rebuilt by skip_signals.rebuild_signals(); never hand-edit
skip_label     (version_id, position, label skip|not|maybe|ok, track_id, labeled_at, PRIMARY KEY (version_id, position))
-- v2.19.0 (2026-10-06) DeepSeek discovery — see references/llm-discovery.md
llm_batch      (batch_id PK AUTOINCREMENT, created_at, finished_at, model, thinking, angle,
                status running|ok|partial|error|killed, detail JSON: attempts/usage/cost_usd/timings/funnel/llm_error/
                stage_errors/lookup_errors)   -- rows older than 90 days are pruned after each run
llm_pick       (batch_id, kind pick|similar|new_release, rank, artist, title, fit, tier, why, norm_key,
                status resolved|unresolved|filtered, reason, track_id, artist_id, track JSON (composer candidate
                + release_date/album_type; similar: {spotify_name}), created_at, PRIMARY KEY (batch_id, kind, rank))
artist_name    (artist_id PK, name, source segment|api, updated_at)   -- names for the taste profile (artist_cache names are not trusted)
release_scan   (artist_id PK, scanned_at, albums JSON)   -- v2.20.1: album list re-fetched ≤ every release_rescan_hours (72); pruned after 30 days
album_tracks_cache (album_id PK, tracks JSON, cached_at) -- v2.20.1: album track lists never change; pruned after 90 days
api_call_log   (t_ms, endpoint, status)   -- v2.20.2 catalogue guard: one row per catalogue GET (any process), kept 3 days
api_guard      (key PK, value, updated_at) -- 'catalogue_blocked_until' (unix ms): a remembered Spotify 429 ban
llm_usage      (id PK AUTOINCREMENT, t_ms, caller, reserved, used, status reserved|done|failed|timeout|refused)
                -- v2.23 DeepSeek spend guard: every chat call (any process) reserves max_tokens + ~prompt first;
                -- a budget day's (from 01:00 SGT) SUM(COALESCE(used, reserved)) over budget_tokens_day is refused; kept 30 days
```
`playlist_version_track.source` gains `llm` (DeepSeek picks) and, in v2.20, `encore`
(a finished DeepSeek song played again). `wildcard` rows are similar artists' new
releases while `llm_discovery.enabled`. `llm_batch.detail` (v2.20) also holds
`close_share`, `close_share_note` and `feedback_summary`.

`_migrate` adds `play_events.context_uri` and `playlist_version_track.name` with a
duplicate-column-tolerant `ALTER` (the sampler service and a cron wrapper can race
on `init_db()`). run_log detail additions: poll rows carry `signals` (rows
rebuilt) or `signals_error`; refresh rows carry `snapshot_id_after`.

All times are unix **ms**. Indexes: `idx_events_time`, `idx_events_track` on
`play_events`; `idx_snapshot_time`, `idx_snapshot_track` on `affinity_snapshot`.
WAL mode enabled on every connection. `auto_vacuum = INCREMENTAL` set at init time
(must precede any table creation). `db.init_db()` runs `SCHEMA` then `_migrate`
(idempotent column adds — currently `track_cache.album_name`); the cron wrappers
call it every run.

- **play_events** — one row per play. `played_at` = play END; `ms_played` is
  inferred (see `scoring-and-composition.md` §1) and NULL only for an event with
  no predecessor or a zero gap. (The `db.py` column comment "nullable until next
  poll" predates v2.17.0.)
- **track_cache** — track_id, name, artist_ids (JSON), duration_ms, cached_at +
  album_name (caches the album title from recently-played and powers the
  output-only album-name exclusion rule — self-healed for pre-existing rows on
  their next play). artist_names / popularity / audio_features are not stored
  (endpoints deprecated Nov 2024). No playability column.
- **artist_cache** — `top_tracks` = slim track dicts from `_fetch_artist_top_tracks`;
  `albums` = `[{album_id, name, release_date, tracks?}]` where `tracks` (slim) is
  stored for the first 5 albums + in-window releases (v2.17.0+). Stale after 36h
  (checked on read via `cached_at`); a stale row is still reused when a refetch
  fails.
- **blocklist** — permanent bans (`update_blocklist`: affinity ≤ −1.0) and manual
  bans (`troubleshooting.md`).
- **oauth_token** — legacy bootstrap path only (`auth.bootstrap_interactive`); since
  v2.17.0 `auth.get_access_token()` reads `~/.hermes/auth.json` first and only
  falls back to this row.
- **run_log** — `run_type` `'poll' | 'refresh'`, `status` `'ok' | 'error'`, `detail`
  JSON. Poll ok: `{events_fetched, events_stored[, possible_truncation]}`; refresh
  ok: `{snapshot_id_before, tracks_added, scores_computed, newly_blocked}`;
  errors: `{detail: "..."}`. `db.log_run` returns the new `run_id`.
- **affinity_snapshot** — write-only observability, never read by scorer or
  composer. Written at the end of each successful refresh with all non-zero
  affinity tracks, keyed by the run_id returned from `log_run`. Pruned to a 90-day
  TTL (`DELETE WHERE computed_at < now − 90·86400·1000`). Weekly
  `PRAGMA incremental_vacuum` on Sunday **SGT** (`vacuum_weekday`). Not comparable
  across 2026-09-23 (ms_played migration).

## config.yaml — what is actually read

`skip_signals` (v2.18) is read live by `skip_signals.load_config()` (every call) and
by the resident sampler (on file mtime change); defaults in `skipsig.DEFAULTS`.
`enabled: false` = shadow mode. Keys and meaning: `references/skip-signals.md`.

| Block | Read by | Notes |
|---|---|---|
| `playlist` | `composer.py` at import | target_size 40; composition core 24 / llm 12 / discovery 0 / wildcard 4 (v2.19; disabled → discovery 12); `new_music_layout` mix\|block; `max_tracks_per_artist` 3 (per section), `max_tracks_per_artist_combined` 4, `wildcard_release_window_days` 30 |
| `llm_discovery` | `llm_discovery.load_config()` every call; `enabled` also by `composer.py` at import | DeepSeek job + composer switch — keys and meaning: `references/llm-discovery.md` |
| `exclude` | `exclusions.py` at import | `artist_ids` (pre-scoring + output), `albums[].track_ids` (output-only), `album_name_contains` (output-only) |
| `scoring` | `scorer.py` (+ `snapshot` by `refresher.py`) | window 7d, half-life 7d, thresholds ±1.0, bracket weights, `snapshot.retention_days` 90, `snapshot.vacuum_weekday` 6 |
| `spotify` | **nothing** | `client_id` / `redirect_uri` are hardcoded in `auth.py` (client_id overridable via `SPOTIFY_CLIENT_ID`; secret via `SPOTIFY_CLIENT_SECRET`); `playlist_id` is hardcoded as `refresher.PLAYLIST_ID` (and in the refresh wrapper's `PLAYLIST_URL`) |
| `schedule` | **nothing** | mirror of the Hermes cron config, for reference only |
| `paths` | **nothing** | the DB path is `db.DB_PATH` (hardcoded default; wrappers set it explicitly); `logs/` is unused |

Every read is "live per run": cron spawns a fresh process, and each module reads
its block at import. Each level is `or {}`-guarded so a present-but-null block
can't crash the import.

```yaml
spotify:                       # NOT read by code (see table)
  client_id: "c3616d872c07443980727a2beef2e936"
  redirect_uri: "http://127.0.0.1:8888/callback"
  playlist_id: "4y4Fd7Pg52Ubi0zqJBNJnm"

playlist:                      # read live by composer.py
  target_size: 40
  composition: {core: 24, discovery: 12, wildcard: 4}
  constraints:
    max_tracks_per_artist: 3            # per-section cap
    max_tracks_per_artist_combined: 4   # combined-pool cap (looser than per-section)
    wildcard_release_window_days: 30

exclude:                       # read at import by src/exclusions.py (scorer + composer)
  artist_ids:                  # every track from these artists is dropped pre-scoring + in composition
    - "5PFhEtUuits5kch6cVL31o" # baby-sleep-music artists ("Relaxing Baby Sleep Music")
    - "3fPVH9rILsWGNptYYQUnh0"
    - "2U8YfLNkdcmIyWPLU9Lzx8"
  albums:                      # OUTPUT-ONLY: dropped from the playlist, still scored
    - name: "XDM Unidentified Waves"   # XG × JAKOPS — XDM alternate versions of XG tracks
      album_id: "1z3VCBUJBHkpDHtzOZP205"
      track_ids: ["1zoaw0B1QheyRnA7pZEqCR", "5W5yP5z0QkTsk06GSX2GNX", "1a5Dhd82KaUqmz3sQEDNbs",
                  "5yRRQlRb1b8J3DiDHMsB1P", "3JyviRYTc6zWPuQtZtgFw2", "0L7zZ3Wl9KVCaGpl2vzXnH",
                  "0ytRjAjsycFlu3ZA1bVpiJ", "28bgnX4IxlSiNgTyh3iAU9"]
  album_name_contains:         # OUTPUT-ONLY general rule: album name CONTAINS substring (case-insensitive)
    - "(Soundtrack from the Netflix Series)"   # subsumes the former CTLBT ID entry (merged 2026-08-28)

scoring:                       # read live by scorer.py
  window_days: 7
  half_life_days: 7
  like_threshold: 1.0
  dislike_threshold: -1.0
  weights: {full_listen: 1.0, mostly_listen: 0.5, partial_listen: 0.1, skip: -1.0, hard_skip: -1.5}
  snapshot:                    # read by refresher.py
    retention_days: 90         # affinity_snapshot TTL
    vacuum_weekday: 6          # 0=Mon … 6=Sun (SGT); incremental_vacuum runs this day

schedule:                      # NOT read — source of truth is the Hermes cron config
  poll_weekday_times: ["02:00","04:00","07:00","10:00","13:00","16:00","17:00","19:00","22:00","23:00"]
  poll_weekend_times: ["02:00","04:00","07:00","10:00","13:00","16:00","17:00","19:00","22:00","23:00"]
  refresh_time: "03:00"
  timezone: "Asia/Singapore"

paths:                         # NOT read
  db: "~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"
  logs: "~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/logs"
```
