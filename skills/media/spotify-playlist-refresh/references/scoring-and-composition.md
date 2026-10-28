# Scoring, composition & run budgets — detail

Relocated from SKILL.md (v2.17.0, 2026-09-23) and rewritten for the v2.17.0 code.
SKILL.md keeps a one-paragraph summary of each; this file is the full reference.

## 1. ms_played inference (poller.py)

Spotify exposes no skip events and no listen duration. `recently-played` gives a
`played_at` per play, and **`played_at` marks when the play ENDED** — verified on
1,276 production gaps (2026-09-23): the gap to the *previous* event matched the
*current* track's duration 911× vs the *next* track's 11×. So each event's listen
time is the gap back to the previous event:

```
events sorted ascending by (played_at, track_id)      # ties: same order as the migration
ms_played_i = min(duration_ms_i, played_at_i − played_at_{i−1})
predecessor of the oldest event in a poll = db.get_latest_played_at_before(played_at_0)
    (normally the previous poll's newest event → overlapping polls compute identical values)
no predecessor at all (first-ever poll)   → ms_played = None (unscored)
gap < 0 (clock skew)                      → full play (duration_ms)
gap > 6h (listening resumed after break)  → full play (the gap says nothing about this play)
gap == 0 (two plays stamped the same ms)  → None (unscored)
```

The newest event of a poll is **not** pending any more — its value is final as
soon as it is fetched (its predecessor is already known). *(History: before
v2.17.0 the poller assumed `played_at` = play START and computed
`min(dur_i, played_at_{i+1} − played_at_i)`, i.e. charged each track with the
NEXT track's listen time, and stored the newest event as NULL "pending". That
mis-scored skips onto the wrong track; see CHANGELOG 2.17.0.)*

**Upsert (`db.upsert_play_events`)** — `ON CONFLICT(track_id, played_at) DO UPDATE
SET ms_played = excluded.ms_played WHERE excluded.ms_played IS NOT NULL`: any
non-NULL value overwrites; NULL never does. Returns the number of rows that were
actually NEW.

**run_log detail** — `{"events_fetched": n, "events_stored": new_rows}`.
`events_stored` is now the real count of new rows (before 2026-09-23 it was
`len(inferred)`, i.e. always the fetch size — don't analyse old rows with it). If
every fetched event is new, the fetch was full (50) and a predecessor existed,
the poll adds `"possible_truncation": true` and prints a stderr warning: the
50-event window may not reach back to the previous poll (plays lost). Any poll
failure writes an `'error'` poll row (`{"detail": "<Exception>: ..."}`) before
re-raising; a failed recently-played fetch raises `poller.PollError(status, msg)`
(`status` 0 = network).

**One-shot migration** — `spotify-refresh/scripts/migrate_2026_09_played_at_end.py`
(run 2026-09-23 with the gateway venv; `--db PATH`, `--dry-run`). Backed the DB up
to `spotify.db.bak-playedat-20260923` (chmod 600), recomputed ms_played for all
events with `poller._infer_ms_played` (1,547 of 3,522 values changed), lifted the
6 blocklist bans that all traced to mis-scored plays (BILLIONAIRE, To. My First
Love, Can't Help Myself, Swipe It Out, Boom Boom Boom, Busy Boy — user-approved),
and wrote a marker row to a new `migrations` table in the same transaction.
Idempotent: a re-run recomputes the same values but will NOT lift bans again
(the marker is checked). `scorer.update_blocklist` re-blocks anything genuinely
disliked on the next refresh.

## 2. Scoring model (scorer.py, spec §5)

### Completion brackets → weight
| Completion (`ms_played / duration_ms`) | Weight |
|---|---|
| ≥ 90% | +1.0 (full listen) |
| 50–90% | +0.5 (mostly listened) |
| 30–50% | +0.1 (partial listen) |
| 10–30% | −1.0 (skip) |
| < 10% | −1.5 (hard skip) |

Events with `ms_played` NULL, `duration_ms` 0, or `duration_ms` > 10 min
(podcasts/mixes) are skipped; so are blocklisted and category-excluded tracks.

### Recency decay
```
age_ms    = now_ms − played_at                          # milliseconds since play
age_days  = age_ms / (86400 * 1000)                     # convert to days
recency   = 0.5 ^ (age_days / 7)                        # 7-day half-life (config.yaml: scoring.half_life_days)
event_score = weight * recency
affinity(track) = Σ event_score for events in last 7 days   # config.yaml: scoring.window_days
```

### Buckets
| Affinity | Bucket | Action |
|---|---|---|
| ≥ +1.0 | **Like** | Eligible for playlist |
| ≤ −1.0 | **Dislike** | Permanently blocklisted |
| otherwise | Neutral | Ignored for selection (fallback only) |

**Blocklist is permanent** — once a track enters it, `update_blocklist()` never
removes it, even if affinity later rises. (The only removals ever made were the
manual ones above and `troubleshooting.md`'s manual unblock.)

`scorer.compute_affinity()` returns a **tuple** `(scores_dict, computed_at_ms)` —
always unpack it: `scores, _ = scorer.compute_affinity()`.

**Not comparable across 2026-09-23:** `affinity_snapshot` rows written before the
v2.17.0 migration were computed from mis-attributed ms_played values (and rows
before 2026-06-07 / v2.8.0 from optimistic full plays) — don't trend across
either boundary.

## 3. Playlist composition (composer.py, spec §6)

> **v2.19.0 (2026-10-06):** with `llm_discovery.enabled` (live) the new-music
> slots are **12 DeepSeek picks (`llm`) + 4 wildcards = similar artists'
> ≤30-day releases**, read from `llm_pick` by `composer._llm_candidates` (no API
> calls; one song per new artist; shortfall → fallback liked → neutral; banner
> note when the batch failed / is stale), then reordered by `new_music_layout:
> mix` (positions 1–3 core, the 16 new songs spread through the rest). v2.20:
> up to 3 of the 12 llm slots go to `encore`s — finished, never-skipped DeepSeek
> songs — placed first in the new-music block.
> `_pick_discoveries` / `_pick_wildcards` / the artist graph below run only when
> the flag is off (then the llm slots go to discovery: exact v2.18). See
> `references/llm-discovery.md`.

40 tracks: 24 core + 12 discovery + 4 wildcard (doubled from 20 = 12+6+2 on
2026-07-06, ratio 6:3:1; all counts, both caps and the 30-day window read live
from `config.yaml` → `playlist`).

| Slot | Count | Source |
|---|---|---|
| **Core favorites** | 24 | All liked tracks in an **exact weighted-random order** (Efraimidis–Spirakis, key `log(u)/w`, weight = affinity; `_weighted_order`), metadata from `track_cache`. Re-drawn each run so the playlist actually changes day-to-day. |
| **Similar discovery** | 12 | Artist graph (§6.2): liked-track affinity aggregated by artist (split equally across a track's artists) → top 10 artists. Per artist: (a) up to 10 tracks from `_fetch_artist_top_tracks()` (10 albums → newest 5 → artist-filtered tracks, deduped, ≥60s), (b) tracks of the first 5 cached albums. Filtered **at ingestion**: already heard (track ID in `play_events` **or** same-song signature of a heard track), plus every output filter. Stops at 12×5 = 60 candidates; weighted-random order by artist affinity. |
| **Wildcard** | 4 | Every track of every release in the last 30 days by **all** top-10 artists (`include_groups=album,single`); unplayable dropped; **unheard first** (shuffled), then heard (shuffled). |

### Picking — guardrails applied WHILE picking (`_Picker`)
`_Picker.try_add(t)` accepts a track only if it (in this order):
1. has a track ID not already picked;
2. is not a same-song/same-artist duplicate of a picked track (`_track_sig`:
   normalized name + artist-ID set, or artist-name set when no IDs) — catches the
   same song under different release IDs (reported: Baby DONT Cry "Shapeshifter" —
   standalone single `0kkqkpgbkLFpopZqPAH1CO`, the liked/heard one with 83 play
   events, vs the "The 1st Mini Album [AFTER CRY]" copy `6ONjO8j2uE9Y9wPgbYR3fw`,
   0 plays, which entered via discovery). First picked wins — core is picked
   first, so the liked/heard release is kept. Different-version titles ("Song" vs "Song (Sped
   Up)", "Ice Princess (Kor Ver.)") and same-name-different-artist tracks are NOT
   merged; tracks with no name/artist info are never sig-deduped;
3. keeps its primary artist within **4 across the whole playlist** and **3 within
   the section** being filled (fallback stages use only the combined cap). Tracks
   with no artist ID (`artist_ids=[]`, local-file/regional entries) are never
   bucketed under either cap;
4. passes `_passes_output_filters` — blocklist, live, instrumental, remix,
   unplayable, category + album exclusions (see `references/filters.md`).

A rejected candidate is skipped and the next one tried, so a section is short
only when its candidate supply is exhausted. Order of filling:

1. core (24, from the weighted order) →
2. **wildcards (4) before discovery** — they are scarce; appended last they
   always lost the combined per-artist cap →
3. discovery (12) →
4. **fallback** to 40, combined cap only: next **liked** tracks (by affinity) →
   **neutral** tracks → **leftover discovery/wildcard candidates**. Never padded
   with junk.

Short of 40 after all fallbacks: `⚠️ Only N tracks composed (target: 40)` goes to
**stderr** (the refresh wrapper's stdout is the Discord message), what exists is
PUT, and the wrapper banner appends `⚠️ N/40 tracks — not enough eligible tracks
to fill the playlist`.

Removed in v2.17.0 (don't look for them): `_apply_section_guardrails`,
`_apply_guardrails`, `_fill_from_fallback` / `_try_add`, `_weighted_sample` —
replaced by `_passes_output_filters` + `_Picker` + `_weighted_order`.

### Artist data & caching
`_get_artist_data`: per-run memo → fresh `artist_cache` row (younger than
**36h**, `db.ARTIST_CACHE_TTL_MS`; a 24h TTL expired seconds before every 03:00
run) → live fetch via `_fetch_artist_graph` (albums list `limit=10`; album track
lists prefetched for the **first 5 albums + every album in the 30-day window**,
stored as slim dicts in `albums[].tracks`; then `_fetch_artist_top_tracks`,
whose GETs hit the per-process memo). A live fetch is written to the cache only
if it was **complete and non-empty and not a dry run**; a failed/partial fetch
never overwrites a good row — the last good row is reused even if stale, else the
partial data is used for this run only. Rows cached before v2.17.0 lack
`albums[].tracks`, so `_album_tracks` live-fetches them until they expire.

**`dry_run=True`** makes the same read-only API calls but writes nothing to the DB
(no `artist_cache` upserts). `compose_playlist` never touches the playlist either
way; only `refresher.refresh_playlist()` writes it. (Before v2.17.0 the flag was
accepted but ignored — dry runs did write `artist_cache`.)

### Offline results (v2.17.0 harness, 100 seeds, production DB copy)
| Metric | Before | After |
|---|---|---|
| Discovery | 7.9 / 12 | 12 / 12 |
| Wildcard | 0.06 / 4 | ~2.3 / 4 |
| Fallback | 8 | ~1.7 |
| Total | — | 40/40 every run |
| API calls, cold cache | 92 | 52 |
| API calls, warm cache | 49 | 1 (transitional — until pre-2.17 cache rows expire) |

## 4. Run budgets & HTTP client (refresher.py, client.py)

The gateway SIGKILLs a `no_agent` script at **600s** (`cron.script_timeout_seconds`).
`refresh_playlist()` sets a per-stage deadline with `client.set_deadline(s)`:

| Stage | Budget | Notes |
|---|---|---|
| Poll | 60s | **best-effort**: a `PollError` (5xx/timeout) prints a stderr warning and scoring uses existing history; 401/403 or any non-`PollError` aborts |
| Score + compose | 290s | |
| Snapshot GET | 40s | `GET playlists/{id}?fields=snapshot_id`, logging only (None on failure) |
| Playlist write | 150s | one atomic `PUT /v1/playlists/{id}/items` (≤100 uris; overflow POSTed) |

Sum 540s leaves ~60s for the wrapper's token refresh, DB work and weekly vacuum.
The deadline is cleared (`set_deadline(None)`) on exit. Every failure **before**
the write records an `'error'` refresh row in `run_log` (incl. `composer_empty`
and `no_uris` — the latter guards against PUTting `{"uris": []}`); a failed PUT
leaves the previous playlist intact and logs an error row. **After** a
successful PUT nothing may report failure: a failed run_log write, snapshot
write, prune or vacuum only prints a stderr warning.

`client.api()` (retry policy):
- `MAX_RETRIES = 6`; never sleeps after the final attempt; never starts an attempt
  (36s budget each) or a sleep that would overrun the stage deadline — returns
  `{"error": {"message": "run deadline reached"}}` instead.
- **429 with Retry-After**: honored in full if it fits the deadline (≤30s when no
  deadline is set, e.g. the standalone poll cron); otherwise fail fast and open a
  **GET breaker** until the ban lifts (GETs short-circuit; non-GETs such as the
  PUT always try). 429 without Retry-After: backoff `min(3·2ⁿ, 30)`s.
- 5xx / status 0 (timeout, connection error, non-JSON body): backoff `min(3·2ⁿ, 20)`s.
- Other errors (400/401/403/404): returned immediately.
- **Catalogue pacing**: `artists/…` and `albums/…` GETs are spaced ≥1.2s apart
  (a cold graph fired in a burst exceeded the ~50 req/min "Other" limit).
- **Per-process memo** of successful `artists/…` + `albums/…` GETs (discovery,
  top-tracks and wildcards ask for the same URLs). `_fetch_album_tracks` has no
  retry loop of its own (it used to multiply a timeout into 18 attempts).
- `get_user_market()` (`GET /v1/me` → `country`) is cached per process; a failure
  or no remaining budget caches `"SG"`.
