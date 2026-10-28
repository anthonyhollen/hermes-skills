# Polling Data Loss Analysis (May 2026)

> **⚠️ Update (2026-06-07, v2.6.0+):** The poller now requests **`limit=50`** per poll (was the default 20), so the "20-event cap" framing throughout this doc is historical. Also, `db.upsert_play_events` is now a **self-correcting upsert** (was `INSERT OR IGNORE`): a later poll's real gap value fills a pending `NULL` or lowers a stale optimistic `ms_played`. The dedup-on-`(track_id, played_at)` analysis below still holds.

> **⚠️ Update (2026-09-23, v2.17.0):** (1) The "pending NULL / only-lower" upsert described above is superseded: `played_at` is the play END, each event's `ms_played` is fixed by its predecessor (the oldest event in a poll takes it from the DB), so the newest event is final — and the upsert now lets **any non-NULL value overwrite; NULL never does**. (2) `run_log.detail.events_stored` is **now accurate** (rows actually new this poll, returned by `db.upsert_play_events`); rows logged before 2026-09-23 still carry the inflated `len(inferred)` value, so the "query `play_events` directly" advice below remains right for historical analysis. (3) A poll that finds all 50 fetched events new (with a known predecessor) sets `possible_truncation: true` and warns on stderr — the direct signal for the cap-binding case discussed below.

## ⚠️ CORRECTED ANALYSIS — Do not use raw API return counts

**Previous error:** Concluding "100% hit cap rate" by counting `events_fetched = 20` from every poll. This metric only measures how many events the Spotify API returned — not how many were actually new data.

**Correct framing:** `INSERT OR IGNORE` deduplicates on `(track_id, played_at)`. The DB has 285 rows despite 1860 total API events across 93 polls — ~1575 events were already stored and silently dropped. `events_stored` in `run_log.detail` is also broken (reports `len(inferred)` not actual INSERT count). The only reliable metric is `play_events` table row counts.

## Correct methodology

Query actual `play_events` stored, grouped by `played_at` hour:

```bash
cd ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh && python3 -c "
import sqlite3, datetime
conn = sqlite3.connect('spotify.db')
cur = conn.cursor()
cur.execute('''
SELECT strftime(\"%H\", played_at/1000, \"unixepoch\", \"localtime\") as hour,
       COUNT(*) as new_events
FROM play_events
WHERE played_at > (strftime(\"%s\", \"now\", \"localtime\") - 7*86400)*1000
GROUP BY hour ORDER BY hour
''')
for r in cur.fetchall():
    print(f'{r[0]}: {r[1]} new events')
"
```

## Last 7 days (May 15–21, holiday period)

| Polling Window | New Events | % of Total |
|---------------|-----------|------------|
| 00:00–04:00 | 3 | 2.9% |
| 04:00–07:00 | 1 | 1.0% |
| 07:00–10:00 | 19 | 18.4% |
| 10:00–13:00 | 15 | 14.6% |
| 13:00–16:00 | 4 | 3.9% |
| **16:00–19:00** | **49** | **47.6%** |
| 19:00–22:00 | 1 | 1.0% |
| 22:00–24:00 | 11 | 10.7% |

**Total: 103 new events in 7 days** across 64 polls = ~1.6 new events/poll average.

## Key finding: listening is burst-based, not continuous

- **16:00–19:00** = ~50% of all new events (post-work evening peak)
- **07:00–10:00** = ~18% (morning commute / weekend leisure)
- **10:00–13:00** = ~15% (weekend late-morning)
- Rest of day: near-zero (vacation = no music during work hours)

## When is the 20-event cap actually a problem?

The cap only binds when your listening rate generates 20+ events between two consecutive polls. On May 16, the 17:00 window had 16 events + 18:00 had 8 = 24 total across 2 hours. The 18:00 poll captured all 20 in that window — only 8 were "new" relative to prior poll. During vacation/quiet periods, most polls capture 0–2 new events (stale data already in DB).

## Current schedule (updated May 21 2026)

| Time | Poll | Purpose |
|------|------|---------|
| 04:00 | ✅ | Pre-morning |
| 07:00 | ✅ | Morning commute |
| 10:00 | ✅ | Mid-morning |
| 13:00 | ✅ | Afternoon |
| 16:00 | ✅ | Pre-evening |
| 19:00 | ✅ | Evening peak |
| 22:00 | ✅ | Evening |
| **23:00** | 🆕 | Late-night cluster (added May 21) |
| **02:00** | 🔄 | Shifted from 1am (added May 21) |

## Why more polls don't help much

With burst listening and 3hr gaps, most clusters are fully captured by the next scheduled poll. Adding 17:00 would only capture events the 19:00 poll would get anyway — the cap only matters if 20+ events accumulate between polls, which requires continuous listening for hours.

## Monitoring (corrected)

```bash
# Actual new events per day
python3 -c "
import sqlite3, datetime
conn = sqlite3.connect('spotify.db')
cur = conn.cursor()
cur.execute('''
SELECT DATE(played_at/1000, 'unixepoch', 'localtime') as day, COUNT(*) as n
FROM play_events
WHERE played_at > (strftime('%s', 'now', 'localtime') - 7*86400)*1000
GROUP BY day ORDER BY day
''')
for r in cur.fetchall(): print(f'{r[0]}: {r[1]} new events')

# By hour
cur.execute('''
SELECT strftime(\"%H\", played_at/1000, 'unixepoch', 'localtime') as hour, COUNT(*) as n
FROM play_events
WHERE played_at > (strftime('%s', 'now', 'localtime') - 7*86400)*1000
GROUP BY hour ORDER BY hour
''')
for r in cur.fetchall(): print(f'{r[0]}:00 — {r[1]} events')
"
```

If a window that previously had many events drops to 0 (vacation, travel), that's expected — not a bug. Deduplication is working correctly.