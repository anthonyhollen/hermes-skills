# Spotify API: Skip Detection Limitation

> **⚠️ Update (2026-09-30, v2.18.0) — skips are now detected directly.** Two
> corrections to this page:
>
> - **It understated the problem.** On 2026-09-30 the user played the playlist
>   in order and skipped 15 tracks. `recently-played` recorded exactly **one**
>   skip, for a track that wasn't even in the playlist. Over the 7 days before,
>   there were 195 plays and 1 recorded skip.
> - **The "~30s threshold" is not a rule.** A 24s play was recorded, while
>   several passes of 35s or more were not.
>
> **"Fundamental API constraint, not a code bug" no longer holds.** A resident
> `GET /me/player` sampler now measures each track's real exposure. Stored
> playlist versions let the fallback name the tracks passed over between
> recorded plays. Skips drive a 3-refresh cooldown and a demotion, without
> waiting for affinity to drop.
>
> See **`references/skip-signals.md`**. The manual blocklist below still works
> and is still permanent.


> **⚠️ Correction (2026-09-23, v2.17.0):** until this date the poller inferred `ms_played` in the wrong direction (it treated `played_at` as play START and credited each track with the gap to the NEXT event). A skip therefore penalized the track played *before* the skipped one, and the skipped track itself scored as a full listen. Fixed (gap back to the previous event — `played_at` is the play END), all historical `ms_played` values were recomputed, and the 6 blocklist bans of the time (all traced to mis-scored plays) were lifted — see CHANGELOG 2.17.0 and `references/scoring-and-composition.md` §1. Ratios queried below from before the migration are not trustworthy. The core limitation documented here — instant skips below Spotify's threshold produce **no event at all** — is unchanged.

## The Problem

When a user skips a track in Spotify before the track's "played" threshold is reached, **the track does not appear in `GET /me/player/recently-played` at all**. The playlist refresh system has no direct signal for that skip.

This means:
- Tracks the user hates and skips immediately → **no play event recorded** → affinity stays at 0 → track can still appear in core favorites if other listens pushed it over `LIKE_THRESHOLD = 1.0`
- Only partial plays (where Spotify *did* record the event but with low `ms_played / duration_ms` ratio) contribute negative affinity

## Spotify Completion Brackets and What They Capture

| Bracket | `ms_played / duration_ms` | Weight | Appears in API? |
|---|---|---|---|
| Full listen | ≥ 0.90 | +1.0 | ✅ Yes |
| Mostly | 0.50–0.90 | +0.5 | ✅ Yes |
| Partial | 0.30–0.50 | +0.1 | ✅ Yes |
| Skip | 0.10–0.30 | −1.0 | ✅ Yes (low ratio) |
| Hard skip | < 0.10 | −1.5 | ✅ Yes (very low ratio) |
| Instant skip (before threshold) | N/A | N/A | ❌ **No event recorded** |

## Diagnosis Query (used May 2026 session)

```python
# Tracks with skip-like completion ratios
# (these are the ONLY negative-affinity signals the system can act on)
python3 -c "
import sys; sys.path.insert(0, '.')
import db, datetime
db.DB_PATH='/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db'
db.init_db()
with db.get_conn() as conn:
    conn.row_factory = db.sqlite3.Row
    rows = conn.execute('''
        SELECT a.track_id, b.name, a.ms_played, a.duration_ms,
               CAST(a.ms_played AS FLOAT)/CAST(a.duration_ms AS FLOAT) as ratio,
               a.played_at
        FROM play_events a
        JOIN track_cache b ON a.track_id = b.track_id
        WHERE CAST(a.ms_played AS FLOAT)/CAST(a.duration_ms AS FLOAT) < 0.3
        ORDER BY a.played_at DESC
    ''').fetchall()
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r['played_at']/1000).strftime('%Y-%m-%d %H:%M')
        print(f'{r[\"name\"]} [{r[\"track_id\"]}] | ratio={r[\"ratio\"]:.2f} | {ts}')
"
```

## Workaround: Manual Blocklist

When a track keeps appearing despite user skips (and has no negative-ratio events in `play_events`), the only reliable remedy is to manually add it to the blocklist:

```bash
sqlite3 ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db \
  "INSERT OR IGNORE INTO blocklist (track_id, reason, banned_at) VALUES (
    '<track_id>',
    'user requested block — skips not detected by Spotify API',
    \$(date +%s)000
  )"
```

Blocklist is **permanent** — the track will never re-enter the playlist.

## Finding the track_id to block

```python
# Search track_cache by partial name
python3 -c "
import sys; sys.path.insert(0, '.')
import db
db.DB_PATH='/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db'
db.init_db()
with db.get_conn() as conn:
    conn.row_factory = db.sqlite3.Row
    rows = conn.execute(\"SELECT track_id, name FROM track_cache WHERE name LIKE '%SEARCH_TERM%'\").fetchall()
    for r in rows:
        print(f'{r[\"track_id\"]} | {r[\"name\"]}')
"
```

## Why Core Tracks Still Appear Despite User Skips

Tracks in the `core` section are sourced from **liked tracks** (affinity ≥ +1.0) weighted by affinity score. A track like CHOOM has affinity 22.089 — multiple full-ratio listens accumulated over time. A single skip (that wasn't recorded) cannot overcome that. The user would need to:
1. Accumulate enough partial-play skip events (ratio < 0.30) to drag affinity below −1.0 → auto-blocklist
2. Or manually blocklist the track

This is a **fundamental Spotify API constraint**, not a code bug.
