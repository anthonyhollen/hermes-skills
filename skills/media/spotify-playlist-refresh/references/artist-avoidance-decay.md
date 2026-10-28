# Artist Avoidance — Why Decay Is Too Slow

> **✅ Update (2026-06-07, v2.8.0):** Option 1 has been applied — the scoring window **and** half-life are now **7 days** (not 30/14), and both are read live from `config.yaml` → `scoring` (no longer hardcoded). The diagnosis below describes the original 30-day-window / 14-day-half-life state; treat its numbers as historical. **Options 2 (artist-level skip penalty) and 3 (dual-window decay) remain available** if avoided artists still linger.

> **⚠️ Correction (2026-09-23, v2.17.0):** the skip signal this doc relies on ("inferred from low `ms_played/duration_ms` ratios") was attributed to the wrong track before this date — `ms_played` was computed from the gap to the *next* play although `played_at` is the play *end*. Fixed and back-filled by a one-shot migration (see CHANGELOG 2.17.0); any Option 2 (artist-level skip penalty) work should only use `play_events` data as recomputed on/after 2026-09-23.

## The Problem

A user who actively avoids (stops playing) an artist expects their tracks to fall out of the playlist within days. Instead, the 30-day window + 14-day half-life means:

| Artist | Last Play | Age | Recency Multiplier | Effective Weight |
|--------|-----------|-----|-------------------|-------------------|
| IU (9 tracks) | 2 days ago | 2d | `0.5^(2/14)` = **0.906** | ~91% of original |
| CrocoBeat (1 track) | 11 days ago | 11d | `0.5^(11/14)` = **0.57** | ~57% of original |

IU's last play was May 26. By May 28 (today), it still carries ~91% affinity weight — barely decayed at all.

**Root cause**: `RECENCY_HALF_LIFE_DAYS = 14` means a play from 14 days ago still contributes 50% of its base weight. `WINDOW_DAYS = 30` means events from 30 days ago still exist in the scoring window.

## Why Skips Alone Don't Solve This

Spotify doesn't expose explicit skip events. Skips are inferred from low `ms_played/duration_ms` ratios, but Spotify only records events for tracks played ≥~30s. Instant skips are invisible — no event is recorded, affinity never decreases.

Even if skip detection worked perfectly: avoiding an artist by simply not playing them generates **zero negative signal** — only explicit skips do.

## Three Fix Options

### Option 1 — Shorter Scoring Window (simplest)
Change in `config.yaml`:
```yaml
scoring:
  window_days: 7    # was 30
```
**Effect**: IU plays from May 26 (2+ days old) score much lower. CrocoBeat May 17 (11 days old) falls completely outside window.

**Pros**: Single config line, no schema change, no logic rewrite.
**Cons**: Good tracks you haven't heard in 8+ days also disappear from core pool. Recency decay for negative signals still takes 14 days to halve.

### Option 2 — Artist-Level Skip/Avoidance Penalty
Add a `skipped_artist` table + logic:
```sql
CREATE TABLE artist_avoidance (
  artist_id TEXT PRIMARY KEY,
  skip_count INTEGER DEFAULT 0,
  last_skip_at INTEGER
);
```
Each time a track from an artist is skipped (partial play <10%), increment `skip_count`. Artist affinity gets `-0.5 * skip_count` penalty (capped at −5). Tracks from high-skip-count artists score lower even if their raw affinity is positive.

**Pros**: Directly models behavioral avoidance; skip signal is already available in `play_events`.
**Cons**: Requires new schema + code; per-artist not per-track — might over-penalize if you skip one track from an artist with many tracks in core.

### Option 3 — Dual-Window Decay for Negative Signals
Apply **7-day half-life** to events with negative weight (partial/skip), and 14-day only to positive weight events. Skip events from May 10–14 would have `0.5^(14/7)` = **0.25** weight now vs. **0.5** currently. Negative signals melt away twice as fast.

**Pros**: Preserves high recency for positive signals; negative signals decay faster; no new schema.
**Cons**: More complex scoring logic; skip signal quality is still noisy.

## Recommendation

Start with **Option 1** (`WINDOW_DAYS = 7`) as a one-line fix. Monitor for 1–2 weeks. If avoided artists still dominate, add **Option 2** (artist-level skip counter) as a follow-up.

## Diagnostic Query — Artist Play Frequency (last 14 days)

```python
import sys; sys.path.insert(0, '.')
import db, datetime as dt, json
db.DB_PATH='/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db'
db.init_db()

cutoff = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) - 14*86400*1000

with db.get_conn() as conn:
    conn.row_factory = db.sqlite3.Row
    rows = conn.execute('''
        SELECT a.track_id, a.played_at, a.ms_played, a.duration_ms, b.artist_ids
        FROM play_events a JOIN track_cache b ON a.track_id = b.track_id
        WHERE a.played_at >= ?
        ORDER BY a.played_at DESC
    ''', (cutoff,)).fetchall()

    artist_play_count = {}
    artist_last_play = {}
    for r in rows:
        try:
            aids = json.loads(r['artist_ids'])
            primary_artist = aids[0] if aids else 'unknown'
        except:
            primary_artist = 'unknown'
        artist_play_count[primary_artist] = artist_play_count.get(primary_artist, 0) + 1
        if primary_artist not in artist_last_play:
            artist_last_play[primary_artist] = r['played_at']
    
    artist_map = {}
    for aid in artist_play_count:
        row = conn.execute('SELECT name FROM artist_cache WHERE artist_id=?', (aid,)).fetchone()
        artist_map[aid] = row['name'] if row else aid[:16]

    for aid, cnt in sorted(artist_play_count.items(), key=lambda x: x[1], reverse=True):
        name = artist_map.get(aid, aid[:16])
        last = dt.datetime.fromtimestamp(artist_last_play[aid]/1000).strftime('%m-%d')
        print(f'{cnt:3}x  {name[:25]:25s}  last={last}')
```

Use this to confirm which artists the user has been actively playing vs. avoiding in the last 14 days. Cross-reference against the current top-20 composition to identify mismatch.
