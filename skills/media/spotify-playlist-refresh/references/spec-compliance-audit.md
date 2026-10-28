# Spec Compliance Audit — v1.2 (2026-05-10)

> **Size note (2026-07-06):** the playlist target is now **40 tracks (24 core / 12 discovery / 4 wildcard)**. Numbers below reflect the earlier 20-track (12/6/2) config and are kept as a point-in-time record.

> **⚠️ Correction (2026-09-23, v2.17.0):** the §5.1 check below ("`gap_ms = events[i+1]["played_at"] - ev["played_at"]`") passed an inference that was later proven backwards: `played_at` is the play END, so the gap must be taken to the *previous* event. The spec pseudocode shared the error. The poller now computes `min(dur_i, played_at_i − played_at_{i−1})` (see `references/scoring-and-composition.md` §1). Also historical: `_weighted_sample` (§6.2) no longer exists (v2.17.0 uses `_weighted_order` + `_Picker`), and `MAX_PER_ARTIST=2` / the §6.1 guardrail order reflect v1.2.

Full audit of `spotify-playlist-refresh` implementation against `spotify-playlist-refresh-design_v1.2.md`.

## Result: 23/23 checks passed

### §7 Schema — ✓
- All 7 tables present: play_events, track_cache, artist_cache, blocklist, affinity_snapshot, oauth_token, run_log
- track_cache columns: track_id, name, artist_ids, duration_ms, cached_at ✓
- artist_cache columns: artist_id, name, top_tracks, albums, cached_at ✓
- Indexes: idx_events_time, idx_snapshot_time, idx_snapshot_track ✓
- WAL mode on open ✓
- `PRAGMA auto_vacuum = INCREMENTAL` in `_open()` before any tables ✓

### §5 Scorer constants — ✓
All 9 constants match spec:
- WEIGHT_FULL_LISTEN=1.0, WEIGHT_MOSTLY_LISTEN=0.5, WEIGHT_PARTIAL_LISTEN=0.1
- WEIGHT_SKIP=-1.0, WEIGHT_HARD_SKIP=-1.5
- RECENCY_HALF_LIFE_DAYS=14, WINDOW_DAYS=30
- LIKE_THRESHOLD=1.0, DISLIKE_THRESHOLD=-1.0

### §5.1 ms_played inference — ✓
- `gap_ms = events[i+1]["played_at"] - ev["played_at"]` (exact variable name differs from spec pseudocode, logic identical) ✓
- `min(duration_ms, gap_ms)` cap ✓
- 6h cap: `gap_ms > 6 * 3600 * 1000` → full play ✓

### §6 Composition — ✓
CORE_COUNT=12, DISCOVERY_COUNT=6, WILDCARD_COUNT=2, TARGET_SIZE=20, MAX_PER_ARTIST=2, TOP_ARTISTS_N=10

### §6.1 Guardrails — ✓
blocklist → dedupe (seen_ids) → MAX_PER_ARTIST → fallback fill

### §6.2 Discovery algorithm — ✓
_aggregate_artist_affinity, _get_heard_track_ids, _fetch_artist_graph, _fetch_album_tracks, _weighted_sample all present. *(2026-09-23: `_weighted_sample` removed in v2.17.0.)*
Fallback to slot-1 overflow ✓.

### §7a Snapshot write + prune + vacuum — ✓
write_snapshot, prune_snapshot, incremental_vacuum, vacuum_weekday config all in refresher.py ✓

### §8.2 Poller — no deprecated endpoints — ✓
No audio-features, audio_analysis, recommendations, or related-artists in poller.py ✓

### §8.3 Refresher artist-graph market param — ✓
`get_user_market()` cached one-time. `?market=` passed in _fetch_artist_graph and _fetch_album_tracks ✓.

**Note:** `market=` lives in `composer.py` (called by refresher) not directly in refresher.py — audit check for "market=" in refresher.py was a false positive.

### §11 Config — ✓
target_size=20, core=12, discovery=6, wildcard=2, retention_days=90, vacuum_weekday=6 ✓

### §10 File layout — ✓
All 7 source files in skill-folder `src/` ✓

### §12 Init — ✓
`PRAGMA auto_vacuum = INCREMENTAL` in `_open()` before any tables ✓

## DB Migration Completed

Live `spotify.db` had `auto_vacuum=0` (NONE) instead of 2 (INCREMENTAL). Migrated:
- Backed up to `spotify.db.pre-incremental-vacuum`
- Recreated with `PRAGMA auto_vacuum = INCREMENTAL`
- All data preserved (20 play_events, 17 track_cache, 2 artist_cache, 4 run_log rows)
- Verified: auto_vacuum=2, WAL=wal, all tables + indexes intact

## False Positives in Audit

1. **`gap_ms` exact string** — poller.py uses `events[i + 1]["played_at"] - ev["played_at"]` vs spec's `played_at[i+1] - played_at[i]`. Same logic, different variable reference style in pseudocode vs implementation.
2. **`?market=` in refresher.py** — market param is correctly used in `composer.py` `_fetch_artist_graph()` and `_fetch_album_tracks()` which refresher calls via `composer.compose_playlist()`.