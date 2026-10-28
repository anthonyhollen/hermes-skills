# DeepSeek discovery (v2.19.0, 2026-10-06)

DeepSeek replaced artist-graph discovery and top-artist wildcards. Code:
`src/llm_discovery.py` (the job) plus `composer._llm_candidates` (the
read side). Config: `config.yaml` → `llm_discovery` and `playlist.composition.llm`.

## Why

- **Discovery and wildcards could never find a new artist.**
  - Both drew only from the top-10 already-liked artists:
    - discovery = deep cuts from their 5 newest albums;
    - wildcard = their releases from the last 30 days.
  - Spotify removed every API that could find new artists: recommendations,
    related-artists, top-tracks, genres, popularity (Nov 2024 / Feb 2026).
- **Sampler outcomes, 2026-09-30 → 10-06:**

  | Source | Finished | Skipped / skimmed |
  |---|---|---|
  | core | 97 | 9 |
  | discovery | 7 (29%) | 17 |

  Only 7 of 28 wildcard slots were ever filled.

## Probe, 2026-10-06

One call to `deepseek-flash` for 25 songs by artists the user hasn't played,
each verified with Spotify search:

| Mode | Time | Cost | Exact title + artist matches |
|---|---|---|---|
| Thinking | 64 s | ~$0.004 | 24/25 (the miss was a matcher false negative: "CHUNG HA" vs "Chungha") |
| Non-thinking | 6 s | ~$0.0005 | 21/25 (about 1 made up) |

No pick was by a library artist, and none was already heard.

Gotchas found by the probe:
- `deepseek-flash` **thinks by default**. With `max_tokens` 8000 it spent all of
  it reasoning and returned empty content. `"thinking": {"type": "disabled"}`
  turns thinking off (verified). The job uses thinking first and retries once
  without it.
- **Weaknesses:**
  - Mainstream bias (NewJeans "Super Shy", aespa "Supernova").
  - Runs repeat each other heavily.
  - Knowledge stops around 2024.
  - The prompt counters these with the "major" tier, `major_bsides_only`,
    `DO_NOT_SUGGEST` and daily angles.
  - Freshness comes from Spotify release dates: the wildcards.
- **Search results no longer include `popularity`** (Feb 2026), so mainstream bias
  can't be filtered numerically.

## Flow

```
01:20 SGT (02:20 retry)  spotify_llm_discover_wrapper.py → llm_discovery.run(token)
  build_profile → build_messages → ask_llm (1 DeepSeek call)
  → resolve_picks (Spotify search) → resolve_similar → fetch_new_releases
  → llm_batch + llm_pick rows; artist_name backfill with any time left
03:00 SGT  refresh → composer._llm_candidates (DB only) → picker → mix layout → PUT
```

- **The refresh makes no DeepSeek or catalogue calls.**
- **A DeepSeek outage costs the new-music slots, never the refresh.**
  - Unheard picks stay offerable for 7 days (`max_pick_age_days`).
  - When DeepSeek fails, the job still scans earlier similar artists for releases
    (the batch is then `partial`).
  - When there's no supply, the new slots fall back to favourites (liked →
    neutral). The 03:00 banner gets one line, e.g. `ℹ️ DeepSeek picks 5/12, new
    releases 0/4 today (the last DeepSeek run failed); topped up with favourites`.
  - That line appears only when the slots are short **and** one of these is true:
    - there is no batch;
    - the latest one failed (DeepSeek or Spotify lookups, worded accordingly),
      crashed or was cut off;
    - it is older than 30 h;
    - the store couldn't be read.

## Rules, and where they live

- **New artist means by Spotify artist ID** (`known_artist_ids`): every artist
  of every heard track (track_cache, sampler segments), plus the artist of any
  heard DeepSeek pick.
  - The `LIBRARY_ARTISTS` names in the prompt are only a hint. On 2026-10-06 only
    33 of 90 library artists had a name; the `artist_name` table and backfill
    close that gap.
  - **Exception:** artists whose DeepSeek songs were finished at least as often
    as skipped (`positive_artist_ids`, 30 days) may get another song.
  - Skipped DeepSeek artists become known and drop out.
- **No J-pop (user rule):**
  - The prompt says so. Picks *and* similar artists must carry a Korean `origin`
    ("KR", "Korea", "ROK"; a missing origin is trusted).
  - The taste profile drops kana-named or blocked artists, so tuki. no longer
    pulls in YOASOBI, Ado and similar.
  - `is_japanese()` checks Spotify's own data:
    - kana, but not kanji or ・ ー;
    - Japanese markers in the title (Ver./Version/Edition/JP/JPN, or "(Japanese)", "- Japanese");
    - "Japan" in the album name;
    - an artist name in CJK ideographs with no Hangul (乃木坂46);
    - `blocked_artist_names`, which includes J-idol acts that turn up next to K-pop
      (ME:I, &TEAM, JO1 and others). "LiSA" is left out because it would also block
      BLACKPINK's LISA.
  - **Residual gap:** Japanese-original songs by K-pop acts often carry no marker
    at all (e.g. TWICE "DIVE"). Add such an artist to `blocked_artist_names` if one
    slips through.
  - **Core is untouched**, so tuki. stays in rotation.
- **Versions** (`bad_version`):
  - sped-up, slowed, nightcore;
  - intro, outro, interlude, skit;
  - a bracketed or dash-suffixed English / Chinese / acoustic / piano / band /
    orchestral / remastered / re-recorded tag;
  - anything under 90 s.
  - Another version of a heard song (same base title, same artists:
    `base_sig`) counts as heard. Live, instrumental, remix,
  unplayable, blocklist and exclusions come from `composer._passes_output_filters`.
- **Matching** (`pick_best`): the artist must match on `compact` keys (NFKC +
  casefold + letters and digits only; the model's "A & B" is split), and the titles
  must match main-to-main or main-to-bracket (`_name_match`).
  - "Gift" matches "선물 (Gift)".
  - Never bracket-to-bracket: "A (feat. X)" doesn't match "B (feat. X)".
  - Results the output filters would drop (live, instrumental, remix, unplayable)
    are skipped too.
  - Version and J-pop variants are skipped, so a clean release can win.
  - Preference: exact title → no parenthetical → year ±1 → not a compilation.
  - A near miss (right artist, other title) is rejected. The model's song is
    never swapped for a different one.
- **Similar artists** are resolved through the song the model named for them.
  - The song's title must match too, so a same-name artist (Crush, DEAN, LUCY…)
    is only accepted with that artist's own song.
  - The exact compact-name artist search is used only when no song was given.
  - A search failure is recorded as `search_error`, never as "no match".
- **One song per new artist** across the llm and wildcard sections
  (`section_cap=1` with shared counts). New releases are interleaved by artist,
  title track first.
- **Composer re-checks at 03:00:** heard (ID and same-song signature, including
  the sampler), sampler-skipped, newly known artists, J-pop and version guards,
  and the 30-day window. The picker adds cooldown, blocklist, output filters and
  caps.
- **Pick order:** newest batch first, so the new-music block changes daily.
  Within a batch, "close" and "adjacent" picks are interleaved and the "major"
  tier goes last.
- **Prompt lists:**
  - `WORKED` / `DIDNT_WORK`: taste feedback from recent new-music placements,
    including the old discovery ones.
  - `MORE_FROM`: only DeepSeek artists whose songs were finished; the only
    artists allowed another song.
  - `DO_NOT_SUGGEST`: pick artists of the last 7 batches, newest first, capped
    at 150.
- **New-release sources, in order:** today's similar artists → earlier batches'
  similar artists (60 days) → today's pick artists, capped at
  `new_release_artists_max` (60).

## Learning loop (v2.20)

Every sampler verdict on a DeepSeek placement feeds the next night's batch and the
next refresh. `_verdicts()` reads, joined to the placements:
- sampler `completed`;
- `early_exit` at ≥ 90%, a listen under the user's rule;
- `skip` / `skim`;
- for encores only, the fallback's `inferred_skip`.

**Replay skips are dropped:** a skip within `replay_skip_hours` (12) of finishing
the same song means the in-order playlist wrapped round, not a dislike. The
skip cooldown still applies to it through the picker.

- **Encores** (`encore_candidates`, composer section `encore`):
  - **The problem:** one full listen scores 1.0 only at the moment of play, and
    decays below the 1.0 "liked" threshold by the 03:00 refresh. So a finished
    DeepSeek song was retired as heard and never became a favourite.
  - **The rule:** a song now returns if it was first finished within
    `encore_days` (7), was never skipped (replays aside; an inferred skip also
    withholds), has had fewer than `encore_max` (2) heard encores, was placed
    as an encore on fewer than `encore_max_days` (4) listening days, and isn't
    already liked.
  - **Placement:** up to `encore_slots` (3) of the 12 llm slots, more when picks
    run short, first in the new-music block. The artist may also get a new pick.
  - **Outcome:** a second full listen makes it liked, so core takes over.
  - **Known limit (scorer, not changed):** "liked" lasts about a week without
    further plays, because the 7-day window and 7-day half-life apply to every
    favourite.
- **Labelled feedback:**
  - Each WORKED / DIDNT_WORK item says where it came from: DeepSeek pick (with
    fit / tier / angle), new release, artist-graph deep cut, or a liked artist's
    new release (v2.18).
  - Skips carry `left_at`, how far in the listener left.
  - `FEEDBACK_SUMMARY` has finished/judged counts by kind, fit, tier and angle
    over `adaptive.feedback_days` (14). It uses sampler verdicts only and
    DeepSeek-proposed placements only.
  - The kind comes from the placement's section: llm → pick, wildcard →
    new_release, encore.
  - There is one verdict per placement, and a skip there beats a finish.
  - The prompt tells the model to lean toward what gets finished.
- **Prompt lists are cut by recency, not alphabet** (Python sorts capitals, then
  lowercase, then Hangul):
  - LIBRARY_ARTISTS: heard in the last `input_max_days`, most recent first, `library_list_max` 400;
  - MORE_FROM: most recent finish first, 30;
  - DO_NOT_SUGGEST: newest batch first.
- **Adaptive mix** (`adaptive_close_share`): once both fits have ≥
  `adaptive.min_judged` (8) verdicts, the base 70/30 is reweighted by each fit's
  smoothed finish rate, (finished+1)/(judged+2), and clamped to 0.5–0.85.
  Equal rates keep 0.7.
- **Adaptive angle** (`pick_angle`): the weekday rotation becomes Thompson
  sampling only when every angle has ≥ `adaptive.min_per_angle` (5) verdicts
  (and ≥ 8 overall).
  - Each angle gets a Beta(finished+1, misses+1) draw.
  - It is seeded by the SGT date, so the 01:20 and 02:20 runs agree.
  - An angle applies to about half a batch, so its attribution is noisy; the
    review simulation found only a small gain.
- **Positive artists** (`positive_artists_ranked`): `positive_artist_days` is 90 (the prompt's
  MORE_FROM list is capped at `input_max_days`, 14, since v2.21).
  They are ordered by most recent finish.
  - They may get another song (MORE_FROM). The prompt allows at most
    `more_from_max_slots` + 2 such picks, and the composer places at most
    **`more_from_max_slots` (3) of the 12** (user choice 2026-10-08),
    interleaved with new-artist picks. On 10-08, before the cap, 7 of 9 fresh
    picks had been more-from songs.
  - The newest `positive_release_max` (8) are scanned first for new releases.
    Without the cap they crowd out the similar artists, who are the ones with
    releases (10-08: 30 artists scanned → 0 new releases).
  - The scan considers up to `new_release_artists_max` (60) artists but
    fetches at most `release_fetch_max` (30) fresh per run. Cached ones (72 h)
    are free; the rest are deferred to later nights.
  - A refusal from our own guard budget stops the scan quietly
    (`guard_limited`). Only a real or remembered Spotify ban counts as
    `rate_limited` and makes the batch `partial`.
- **Cool-off** (`release_blocked_artist`): an artist the listener heard and
  didn't take to stays out of picks. Their **new releases** may be tried again
  once they've gone `known_release_cooloff_days` (60) without being heard.
  One early skip shouldn't shut out every future comeback.
- **Lane-exhaustion alert:** a batch records `known_artist_share`, the share of
  picks rejected because the artist is already known. Above 40%, the job's
  summary line warns that the lane may be running dry.
- **Not reached ≠ disliked:** a placement with no verdict teaches nothing.
- **Where to see it:** `stats --days 7` prints the learning view: finish rates by
  kind / fit / tier / angle, the next batch's close share and why, the latest
  batch's angle, positive artists, and encore candidates. Each batch's `detail`
  also records the `close_share` it used, the summary it saw, and
  `known_artist_share`.

## Input window cap (v2.21, user 2026-10-12)

The user asked whether the DeepSeek input keeps growing and capped it at 14
days. It was growing: prompt tokens per batch went 2.5k → 4.2k → 4.9k → 5.5k →
6.1k → 6.3k (10-06 → 10-12), and reasoning on 10-12 used 35.3k tokens (37.6k
of the then 40k `max_tokens` with the reply).

`input_max_days` (14) is a ceiling on every window of data in the prompt
(`input_days(cfg, window)`):

| Prompt section | Before | Now |
|---|---|---|
| FAVOURITE_ARTISTS (top 25 × 3 loved songs) | `profile_window_days` 60 | 14 |
| WORKED / DIDNT_WORK (30 each, newest first) | `feedback_days` 21 | 14 |
| FEEDBACK_SUMMARY (also drives the adaptive mix + angle) | `adaptive.feedback_days` 30 | 14 |
| MORE_FROM (positive artists, 30) | `positive_artist_days` 90 | 14 (the list only — the composer's "more from" slots and the release scan keep 90) |
| LIBRARY_ARTISTS (≤ 400) | every artist ever heard | heard in the last 14 days |
| DO_NOT_SUGGEST (≤ 150) | pick artists of the last 7 batches | … of the last 7 batches inside 14 days |

- **Only the prompt is capped.**
  - The known-artist filter after the call still uses every artist ever heard
    (`known_artist_ids`), so a pick by an artist last heard 3 weeks ago is still
    rejected. It is a wasted pick, not a playlist change; watch
    `known_artist_share` on the first batches.
  - Positive artists keep 90 days for the composer and the release scan. A
    first draft capped them too, and the independent review caught that: a
    stored "more from" pick would have expired mid-week, and a 20-day-old
    positive artist's new releases would have been cooled off.
- **Angle bandit:** with 14 days of verdicts, an angle the bandit stops choosing
  can drop under `min_per_angle` (5), and the weekday rotation briefly returns.
  That acts as forced exploration.
- **The encore history (90 days) and the release-scan caches are not prompt
  input** and are unchanged.
- A longer window set in config is clamped. Raising `input_max_days` widens
  every section (tests cover both).
- **Batch detail:** `detail.profile` records `input_days`, the section counts
  and `prompt_chars`.
- **Headroom:** `max_tokens` is 48000 and `request_timeout_s` 250 (10-12). The
  API accepts these values. At ~220 tokens/s a full 48k takes about 220 s, and
  `job_budget_s` 520 still bounds the job.

## Bounded token use (v2.22)

**Rule: `max_tokens` (48000) is a fixed ceiling.** If reasoning creeps up, shrink
the request (`picks_requested` first); never raise the limit.
- **The API has no reasoning cap.** `thinking.budget_tokens` is ignored and
  `reasoning_effort: low` is only a hint.
- **What drives reasoning** (probes 2026-10-12, CHANGELOG 2.22.0): the number of
  picks asked for, and the size of the artist lists the model checks every
  candidate against.
- **Bounds:**
  - 20 picks + 10 similar artists;
  - compact feedback items;
  - DO_NOT_SUGGEST de-duplicated against LIBRARY_ARTISTS;
  - compact JSON;
  - no per-pick `why`;
  - `prompt_chars_max` 14000 (oldest feedback first, floor 10 each; then the
    oldest names of the longer list, floor 20);
  - inputs ≤ `input_max_days`.
- **Cut-off / timeout:** one smaller thinking retry (`retry_picks` 12,
  `retry_max_tokens` 24000, ≤ 150 s, skipped with under 60 s left), then the
  non-thinking fallback.
- **Monitoring:** `detail.reasoning` {tokens, max_tokens, share, cut_off}. The
  batch summary and the report's footer warn above `reasoning_warn_share` (0.7).
- **Known artists by name:** `ctx.known_names` (all history minus positive
  artists) drops a pick with no Spotify search. The ID check after a search
  still catches the rest.

## Listener taste rules (v2.24)

**User, 2026-10-15:** "I generally don't like low bpm songs. Slow songs + driving =
sleepy, which is dangerous" and "too many boy band / male artist songs".
- **Evidence:** 77 DeepSeek songs were judged over the 14 days to 10-15
  (classified by hand, approximately). About 80% of slow songs / ballads were
  skipped (most within 3–15%), 60% of male acts, and 49% of female acts. The
  old adjacent scene (ballads / MeloMance, K-R&B, Korean bands) and two angles
  steered into both: "K-R&B and Korean bands" 1/8 finished, "MeloMance
  vocalists" 1/4.
- **Rules:**
  - `vocal_filter: female_led` (girl groups, female soloists, female-fronted
    bands);
  - `min_bpm: 95`;
  - a new upbeat, female-led `core_scene` / `adjacent_scenes`;
  - the two weak angles replaced.
- **How they apply:**
  - `_system_prompt` adds them as hard rules 6–7.
  - Every pick carries the model's labels: `act` (girl_group | female_solo |
    female_band | mixed | male) and `bpm`.
  - `_taste_reject` drops a pick labelled male (unless the artist is positive,
    i.e. the listener finished their songs) or below `min_bpm`, BEFORE any
    Spotify search.
  - Similar artists labelled male are dropped too, as are unlabelled ones
    (`unlabelled_act`, positives excepted), so there are no male wildcards
    from them.
  - A pick whose labelled act is only featured on someone else's track is
    dropped (`featured_only`).
- **Only gated rows play:** passing picks / releases are stored with
  `taste_gate` (set only while the rules are on), and `load_candidates` skips
  rows without it, re-checking the stored `bpm` against today's `min_bpm`.
  - So the unlabelled pre-v2.24 pool ages out instead of playing, and the day
    after deploy draws new songs from that night's batch only.
  - Older similar artists feed the release scan only once labelled female-led.
- **Limits:**
  - Spotify removed `audio-features` (Feb 2026), so tempo is the model's
    estimate.
  - Releases by gated artists aren't tempo-checked; a girl group's ballad
    single can still be a wildcard.
  - Encores (songs the listener finished) are unaffected.
- **Rollback:** `vocal_filter: any`, `min_bpm: 0`.

## Spend guard (v2.23)

**Cause, 2026-10-12:** an operator session made 23 test calls in one evening
(14 + 6 probes, 3 checks) to measure reasoning: 657K tokens, 564K of them
output, billed $0.77 per the user's console. That is many times the usual
daily spend.
- **The guard:** every chat call through `_post_json`, from any process (cron,
  manual `run --force`, probes, agents), first reserves its worst case
  (`max_tokens` + prompt chars ÷ 3) in `llm_usage`.
  - A budget day's total over `budget_tokens_day` (120k) is refused with
    "local DeepSeek budget: …". The refusal is logged.
  - The day opens at `budget_day_start_hour` (01:00 SGT), just before the
    nightly batch, so every night starts with the whole allowance. A night
    needs about 27k; its worst case (cut off → small retry cut off → fallback)
    is about 89k; a 02:20 retry after a timed-out 01:20 still fits.
  - A rolling 24 h window was tried first and dropped after review: yesterday's
    batch could sit inside it, so a bad night or a daytime manual run starved
    the next night.
  - The guard reserves the call's `max_tokens` (64k if unset) plus prompt
    chars ÷ 3, and fails closed if the DB can't be read.
- **How outcomes count:** an HTTP 4xx, or a connection that was refused / never
  resolved, releases the reservation. Timeouts, dropped connections, 5xx
  errors and odd usage fields keep it, since those calls may still be billed.
  A success records prompt + completion tokens.
- **Where it shows:** `stats` prints the budget day's total (re-priced at
  today's prices), and the nightly report warns when a call was refused.
- **Rule for operators and agents:** ask the user before making DeepSeek test
  calls. The test suite blocks the network (a tripwire check).
- **Prices** (`PRICE_*`): input and cache hit come from Hermes' snapshot
  (0.15 / 0.003 $/M). Output, 1.34 $/M, is fitted to that console bill; the old
  0.28 under-reported spend about 4–5×.

## DeepSeek skip report (v2.21)

The nightly report: `src/llm_report.py`, run by the no_agent
`spotify_deepseek_report_wrapper.py` at 23:30 and posted to the playground. It
replaced the skip-check digest, which is paused.
- **What it shows:**
  - **Today:** new DeepSeek songs (picks + new releases) skipped of those
    judged, plus encores ("repeats") apart.
  - **Since DeepSeek started (6 Oct):** the skip share, then picks and new
    releases separately. After 90 days it becomes the last 89 days, because
    skip signals are rebuilt over 90 days.
  - **Last 7 days**, once the history is longer than a week.
  - **Before DeepSeek:** the benchmark.
  - **Verdict:** ✅ better / ❌ worse only when the 95% range of the
    **difference** between the two skip shares excludes zero (Newcombe's hybrid
    score interval from both Wilson ranges). The benchmark is only 23 songs,
    and its own range is 54–87%, so DeepSeek at 65 of 100 is still "no clear
    difference". Fewer than 10 judged → "too few".
  - **Skipped per day** for the last 7 listening days.
- **Counting:** the same as `stats()` and the old digest. One verdict per
  playlist position (`skip_signals.day_outcomes` / `_outcome`): skip or skim =
  skipped, past 90% (incl. an early exit at ≥ 90%) = finished. Stops,
  pass-throughs, "left early" and unknown lengths are not counted.
  - **Today and the per-day line** count by listening day (04:00→04:00 SGT):
    every playlist version live that day, judged over its part of it. A manual
    refresh, or a failed 03:00 refresh that leaves yesterday's playlist live,
    doesn't break them.
  - **The running totals** judge each version over its lifetime, as `stats()`
    does.
- **Benchmark** (`llm_report.BENCHMARK`): artist-graph discovery in the
  sampler-judged playlists of 10-01 → 10-06 was finished 6 of 23 and skipped 17
  (recomputed 10-12 with exactly these rules).
  - The user's "26%" is that **finish** rate, so the skip benchmark is 74%.
  - It is fixed in code because its signals age out of the 90-day rebuild.
- **When it posts:** nothing when no DeepSeek song was finished or skipped in
  today's listening day.
- **Calls:** DB only, apart from the wrapper's signal rebuild. No Spotify or
  DeepSeek calls.
- **By hand, from `src/`:** `python3 llm_report.py` prints the report from the
  signals as they are.
- **Layout (v2.22):** short blocks separated by blank lines (Today / Since
  DeepSeek started / Before DeepSeek / verdict with its range as `-#`
  subtext). Skipped per day is an aligned code-block table with bars, and the
  footer shows last night's token use against the fixed limit.

## Run safety

- **Lease:** `_start_batch` uses `BEGIN IMMEDIATE`. A `running` row older than
  `lease_s` (600 s) becomes `killed`, and a live one makes the run skip.
- **Once per 6 h:** an `ok` batch within `min_interval_h` means the 02:20 retry
  does nothing.
  - `partial` means DeepSeek failed, more than 25% of Spotify lookups failed, or
    a later stage crashed. A `partial` batch still retries, and its rows are
    still used.
  - One odd item costs one pick or artist, never the batch.
- **No overlap with 02:50–03:25 SGT** without `--force`: a run is refused if its
  start time plus `job_budget_s` would reach 02:50. That window belongs to the
  refresh and the sampler's quiet time.
- **Retention:** batches older than 90 days, and their rows, are pruned after
  each run.
- **Budget:** `job_budget_s` 520 (the watchdog is 600). Stages run in order of
  value (LLM → picks → similar artists → releases → names), and each stage's
  rows are saved as it finishes.
- **Calls:**
  - Spotify calls go through `client.api` (retries, 429 breaker, stage deadline)
    via `_paced_get`, which spaces search ≥1.2 s apart (client.py paces only
    `artists/` and `albums/`).
  - DeepSeek uses stdlib urllib with a **wall-clock** timeout (a thread with
    `join`), since the socket timeout is per read.
  - Redirects are never followed, so the key can't be forwarded.
  - The key is read from `~/.hermes/.env` (handles `export` prefixes, quotes and
    inline comments) and is never logged or put on a command line.
- **The job does not rebuild skip signals.** The 02:00 poll and the 23:30 digest
  already do.
- **Model text (`why`, titles) is never printed to Discord.** It's visible only
  through `show`.

## Operations (run from `src/`, venv python)

```bash
python3 llm_discovery.py show            # latest batch: funnel, cost, every row with status/reason
python3 llm_discovery.py show 12
python3 llm_discovery.py stats --days 7  # finished-share per playlist source (digest verdicts) + batches + cost
python3 llm_report.py                    # the nightly DeepSeek skip report (DB only)
python3 llm_discovery.py run --dry-run   # DeepSeek + read-only Spotify, writes nothing, prints rows
python3 llm_discovery.py run --force     # a real batch now (ignores the 6h / window guards and enabled)
# The CLI force-refreshes the Spotify token through Hermes like the wrapper (auth.json only as a fallback).
# Exactly the cron path: ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/spotify_llm_discover_wrapper.py --force
python3 llm_discovery.py backfill-names --max 80
```

**Rollback:** `llm_discovery.enabled: false`. The composer then goes back to
artist-graph discovery (the 12 llm slots), top-artist wildcards and block order,
exactly as in v2.18. Then pause the cron job. The first refresh after rollback
rebuilds the artist cache from cold (about 75 s, within the 290 s budget).
- Keep the sampler's 02:55–03:20 quiet window: rollback brings the 03:00
  artist-graph calls back.
- **Rolling back the code means restoring `config.yaml` from the same snapshot.**
  The v2.18 composer reading the v2.19 config would see `discovery: 0`.
- The deploy keeps a tarball in `~/backups-manual/`. The `.bak-*` copies are
  deleted by the Sunday 11:05 backup cleaner.

**Judging it:** `stats --days 7`. Compare `llm`'s finished share and 95% interval
with the discovery baseline (6 of 23 judged = 26%, `llm_report.BENCHMARK`), or
read the nightly skip report. The mix layout puts new songs
at positions 4–40, so they're reached far more often than discovery's old 25–36.

## Spotify rate limits (2026-10-07) and the catalogue guard (v2.20.2)

**What happened.** The catalogue calls (search / artists / albums / tracks)
came from this skill only. There were no Hermes Spotify-tool calls, and the
sampler only polls `me/player`.
- **10-06 23:14–23:22:** ~230 catalogue calls in ~8 min, at about 50/min. That
  was an 80-artist name backfill plus batch 1's ~150 calls. No 429.
- **Then nothing until 10-07 17:44.** The v2.20.0 deploy ran a dry-run batch:
  ~85 searches, then album lists at ~50/min.
- **The first album-list call after that** got `429 Retry-After 17160` (4.8 h)
  on the catalogue endpoints. `me/player` and the playlist write kept working.
- **Then a fresh diagnostic process called the banned endpoint twice more.**
  Nothing remembered the ban across processes.

**Why the 4.8 hours:** Spotify documents only a rolling 30-second window. The
multi-hour penalty tier is undocumented. The same pace was accepted at night and
penalised 18 h later, which points at cumulative load as the escalator.
**Inferred, not provable.**

**The guard.** Every catalogue GET from any process goes through
`client._guard_acquire` (SQLite: `api_call_log`, `api_guard`; config
`api_guard`):
- **Remembered bans:** a catalogue 429's Retry-After plus `ban_margin_s` (600)
  is stored, and every process refuses catalogue calls until then. Calling into
  a ban, which risks extending it, can no longer happen.
- **Spacing:** ≥ `min_gap_s` (2.5 s) between catalogue calls, across processes
  (≤ 24/min, against the ~50/min that preceded the ban).
- **Caps:** `per_hour` 120, `per_day` 200 (a nightly batch needs ~80–110; the ban followed ~315 calls in 18 h).
- **Refusals** come back as a 429-shaped error that every caller already treats
  as no data. The batch goes `partial` and the 02:20 retry runs, or the next
  night does. The playlist falls back to the 7-day pick pool. An unusable guard
  DB fails closed.
- **Night window:** `llm_discovery.run()` refuses outside 00:00–02:45 SGT,
  forced and dry runs included, unless `--daytime`. It also skips while a ban
  is remembered.
- **Smaller nightly job:** 30 picks (was 40), 15 similar artists (was 25),
  30 release artists (was 60), 10 name lookups (was 30), 2.5 s job spacing. The
  release-scan caches (72 h album lists, permanent album track lists) cut repeat
  calls.
- **Not guarded:** player / playlist / `/me` (the sampler, the polls, the
  playlist write).
- **Visibility:** `stats` shows catalogue calls for the last hour and 24 h, the
  caps, and any remembered ban. `detail.release_scan` records each batch's scan.

**This can't be a guarantee:** Spotify can change its limits. The guard keeps us
far below the observed trigger, stops any repeat at the first 429, and keeps a
ban from touching the playlist or skip detection.

## Cost

About 1.5K input + 13–20K output tokens per thinking call. That's ~$0.004–0.006
a day, under $0.25 a month. Each batch's `detail.cost_usd` records it.
