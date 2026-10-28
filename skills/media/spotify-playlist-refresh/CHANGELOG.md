# Changelog — spotify-playlist-refresh

## 2.24.0 — 2026-10-15 (taste rules: upbeat, female-led — "slow songs + driving = sleepy")

The user explained the rising skip rate: low-BPM songs ("slow songs + driving =
sleepy, which is dangerous") and too many boy bands / male artists.
- **Evidence:** 77 DeepSeek songs were judged in 14 days. About 80% of slow
  songs / ballads were skipped, 60% of male acts, and 49% of female acts. The
  adjacent scene (ballads / MeloMance, K-R&B, Korean bands) and two angles
  (1/8 and 1/4 finished) steered into both.
- **Config:**
  - `vocal_filter: female_led` and `min_bpm: 95`;
  - `core_scene` → "girl groups and female soloists, upbeat tracks";
  - `adjacent_scenes` → upbeat female-led Korean pop (female hip-hop / dance
    soloists, energetic female R&B, sub-units, female-fronted pop-rock);
  - angles "MeloMance / Whee In vocalists" → "sub-units and female soloists'
    upbeat singles";
  - angle "K-R&B and Korean bands" → "bright, high-tempo underrated girl-group
    songs (Billlie / LIMELIGHT / HOT ISSUE lane)".
- **`_system_prompt`:** hard rules 6 (female-led only, except MORE_FROM) and 7
  (≈95+ BPM, no ballads / slow R&B / lo-fi / acoustic — "the listener plays
  this while driving"). The reply schema adds `act` and `bpm` for picks, and
  `act` for similar artists.
- **The gate:** `_taste_reject` drops a pick labelled male (unless the artist is
  positive) or below `min_bpm`, and a similar artist labelled male, before any
  Spotify search.
  - Passing rows carry `taste_gate`. `load_candidates` serves only those while
    the rules are on, so the unlabelled 7-day pool ages out.
  - `_older_similar_artists(labelled_only=…)` keeps unlabelled / male similar
    artists out of the release scan.
  - New releases from gated sources carry `taste_gate`.
- **Independent review fixes:**
  - `taste_gate` is set only while the rules are on, so a rollback-then-switch-on
    can't trust rows that were never gated.
  - `load_candidates` re-checks the stored `bpm` against today's `min_bpm`.
  - A similar artist with no `act` label isn't scanned (`unlabelled_act`;
    positives excepted). `labelled_only` treats `act: null` as unlabelled.
  - `_bpm` accepts decimals and numeric strings; under 40 counts as slow.
  - `_act` maps mixed text ("male & female duet") to mixed, and male-led text
    to male.
  - A pick whose labelled female act is only **featured** on another artist's
    track is dropped (`featured_only`).
- **Expected for about a week:** fewer wildcards. Every older similar artist is
  unlabelled, so the release scan restarts from tonight's labelled ones. New
  songs on the day after deploy come only from that night's batch; shortfalls
  fall back to favourites.
- **Limits:** tempo is the model's estimate (Spotify removed audio-features).
  Wildcard releases aren't tempo-checked. Encores are unchanged.
- **No live DeepSeek test calls were made** (rule since 10-12). The 01:20 batch
  shows the effect on reasoning tokens, and the spend guard caps it.
- **Tests:** `test_llm_discovery.py` has 362 checks (52 new; the review's
  mutations — empty `positive_names`, `release_sources` without
  `labelled_only`, no featured check, `taste_gate` always set — each fail a
  check):
  - act labels;
  - the prompt rules on and off;
  - parsing;
  - the gate (male / positive male / slow / none / off);
  - no search for dropped picks or similar artists;
  - stored labels;
  - the gated pool;
  - labelled older similar artists.

## 2.23.1 — 2026-10-15 (Hermes v0.22.0: PyYAML for the skip report wrapper)

- **What broke:** the 23:30 DeepSeek skip report (`ebe12186f5fd`) failed on
  10-14 with `ModuleNotFoundError: No module named 'yaml'` (`scorer.py`
  imports yaml). The Hermes update to v0.22.0 (10-14 20:39) moved cron `.py`
  scripts off the old gateway venv onto Hermes' dependency venv
  (`~/.hermes/installs/<id>/environments/<id>/venv`, Python 3.14.7), which has
  no PyYAML.
- **Already handled on 10-14 (21:17–21:20) by a Hermes agent Discord session:**
  a scripts venv `~/.hermes/scripts/.venv-scripts` (system Python 3.11 +
  pyyaml/pillow/tabulate/wcwidth). The poll, refresh, discovery and digest
  wrappers and the sampler append its site-packages for PyYAML. The
  poll/refresh/discovery wrappers also fall back to the new `spotify` plugin's
  `spotify_auth` (Spotify auth moved out of core in v0.22.0). The sampler unit
  now starts through `spotify_sampler_run.sh` on the dependency venv. The
  skip report wrapper was missed.
- **Fix:** `spotify_deepseek_report_wrapper.py` gets the same PyYAML block
  (pure-python PyYAML loads on 3.14; libyaml off, which is fine). Verified
  under the scheduler's exact interpreter and bootstrap.

## 2.23.0 — 2026-10-13 (DeepSeek spend guard after a usage spike)

The user reported a DeepSeek usage spike on 10-12.
- **Their console:** 17:00–18:00 SGT, 23 requests, 657K tokens (564K output),
  $0.77, way above the daily average.
- **Cause:** my own test calls from the v2.22 token review. There were 3 tiny
  max_tokens checks plus 20 full-size probes of the real prompt (14 + 6, up to 7
  in parallel). Together that is 93K input + 564K output tokens, an exact match.
  - Three more confirmation calls ran about 17:59–18:02 (81K tokens).
  - The same day, the Hermes agent used $0.005 (one Discord session) and the
    nightly batch about 27K tokens. Every cron job is a plain script, and no
    other code calls DeepSeek.
- **Fix: a hard spend guard.** Every chat call through `_post_json`, from any
  process, reserves `max_tokens` (64k if unset) + about the prompt in the new
  `llm_usage` table first. A budget day's total over `budget_tokens_day`
  (120k, about $0.15) is refused.
  - **The budget day opens at 01:00 SGT,** just before the batch.
    - My first draft used a rolling 24 h window with 100k. The independent
      review replayed it: yesterday's worst-case night or a daytime manual run
      could refuse tonight's thinking calls (the 9 Oct fallback-only mode).
    - Now every night gets the whole allowance, and a 02:20 retry after a
      timed-out 01:20 still fits.
  - **It fails closed.** A 4xx or a never-sent request releases its
    reservation. Timeouts, dropped connections, 5xx errors and odd usage fields
    keep it, since those may be billed.
  - **Visibility:** `stats` shows the day's total (re-priced at today's prices),
    and the report warns on a refusal.
  - **Replayed against the 10-12 burst,** it stops after 2 calls.
- **Cost estimates were 4–5× too low.** `PRICE_OUT` 0.28 → 1.34 $/M, fitted to
  the console bill; input and cache now 0.15 / 0.003 (Hermes' snapshot).
  `detail.cost_usd` and `stats` were under-reporting.
- **Tests:** `test_llm_discovery.py` has 310 checks.
  - The guard:
    - the burst replay;
    - a worst-case night then a manual run then the next night;
    - the 01:20-timeout → 02:20 retry;
    - the exact cap ± 1, in-flight rows, the 01:00 day boundary;
    - 4xx / 5xx / refused / dropped / odd usage;
    - no max_tokens;
    - a 6-way race (exactly 2 granted);
    - fail-closed, non-chat URLs.
  - All 5 of the review's guard mutations now fail a check (in-flight ignored,
    `>=`, no `BEGIN IMMEDIATE`, rolling window, dropped connections
    released).
  - The cost fit to the console.
  - A network tripwire. The old network-error test patched `urlopen`, while
    `_post_json` uses its own opener, so it reached DeepSeek with a fake key
    on every run (401, unbilled). Now no test can reach the network.
- **Rule** (SKILL.md, CLAUDE.md): ask the user before any DeepSeek test / probe
  calls.

## 2.22.0 — 2026-10-12 (bounded DeepSeek token use; readable skip report; slim agent docs)

The user said: "I don't want to perpetually keep raising the token limit. Review the
current design and check for anything that has an uncapped growth or causes
increased token usage." The report was also hard to read.
- **Probes on the real prompt** (14 + 6 DeepSeek calls, no Spotify calls; reasoning
  tokens, mean of 3):

  | Prompt variant | Reasoning tokens | Picks by already-known artists |
  |---|---|---|
  | As deployed | 31.3k (30.3–32.2k) | 3–7 of 30 |
  | Compact feedback (prompt −31%) | 29.1k | — |
  | Lists cut to 60 names | 17.2k | 21–29 of 30 |
  | 20 picks | 22.7k | 3–5 of 20 |
  | Compact + de-duplicated lists | 26.3k | — |
  | `thinking.budget_tokens` 8000 | 31–39k | — |

  - The `budget_tokens` cap is **ignored by the API**, so there is no server-side
    reasoning cap.
  - **Reasoning scales with the number of picks and with the size of the artist
    lists** the model checks each candidate against. Shortening the lists alone
    backfires: known-artist picks pile up.
  - Single calls vary ±5k, and one baseline call reached 38.8k.
- **Fixed ceiling, bounded request.** `max_tokens` stays 48000 and is documented as
  never to be raised.
  - **Picks:** `picks_requested` 30 → 20 and `similar_artists_requested` 15 → 10.
    About 13 usable a night against about 9 placed a day; the 7-day pool held 70.
  - **Prompt** (about 17k → about 11k chars):
    - compact WORKED / DIDNT_WORK items: a DeepSeek pick carries no
      `from` / `fit` / `tier` / `angle`, since FEEDBACK_SUMMARY has the totals
      and the angle text alone was about 20% of the prompt;
    - DO_NOT_SUGGEST never repeats a LIBRARY_ARTISTS name (27 duplicates
      today);
    - compact JSON separators;
    - no per-pick `"why"` in the reply.
  - **Hard `prompt_chars_max` 14000:** past it, the oldest feedback goes first
    (floor 10 each), then the oldest names of the longer list (floor 20).
    `detail.profile.trimmed` records any trimming.
  - **Cut-off / unusable reply:** ONE smaller thinking retry (`retry_picks` 12,
    `retry_similar` 5, `retry_max_tokens` 24000, ≥ 8 valid picks), then the
    non-thinking fallback. The fallback alone had given the worst day (9 Oct,
    10 of 12 skipped).
    - The retry's timeout keeps back the fallback's 60 s plus about 2 min for
      the Spotify checks.
    - After a **timeout** it goes straight to the fallback: the review showed a
      slow DeepSeek could otherwise leave the Spotify checks about 35 s.
    - Attempts record their `stage`.
  - **Headroom:** `detail.reasoning` = first-attempt reasoning ÷ `max_tokens`. The
    batch summary and the report warn above `reasoning_warn_share` (0.7) or on a
    cut-off ("trim its request, don't raise the limit").
- **Known artists, by name, before searching.** As heard artists pile up (about
  10 a day, all history), their picks were each searched on Spotify and only
  then rejected by ID.
  - `ctx.known_names` now drops them with no catalogue call. It covers all
    history, minus positive / MORE_FROM artists, and uses authoritative names
    only (sampler segments + `artist_name`, not `artist_cache`).
  - **Known cost:** a new artist sharing a known artist's name loses one pick.
- **`db.get_cached_tracks` / `get_cached_artists`** chunk their `IN` lists (500).
  "Every heard track" grows forever. This is defensive: this Pi's SQLite allows
  250k variables, but stock builds stop at 32,766 and old ones at 999.
- DO_NOT_SUGGEST is de-duplicated against the **capped** library list.
- **The skip report is restructured into short blocks with blank lines:**
  - **Blocks:** Today; Since DeepSeek started (picks / new releases / repeats);
    Before DeepSeek; the verdict, with its 95% range as subtext.
  - **Skipped per day:** an aligned table with bars, in a code block.
  - **Footer:** last night's token use against the fixed limit.
- **Always-loaded agent docs.**
  - `CLAUDE.md` (attached to every agent command in the skill dir) went from
    15.6k to 2.8k chars, about 3.9k → 0.9k tokens per command. Every fact it
    dropped is in `SKILL.md` or `references/` (checked).
  - `SKILL.md` went from 17.1k to 13.7k: the how-it-works bullets now point to
    the references.
  - The tests fail if `CLAUDE.md` > 4,000 or `SKILL.md` > 15,000 chars.
  - **Rule:** these files hold the current state only; release stories go here.
- **Checked and bounded:**
  - every prompt section (14 days + count caps);
  - the reply size;
  - table pruning (`llm_batch` / `llm_pick` 90 d, caches, `api_call_log` 3 d,
    samples 14 d, segments 180 d, snapshots 90 d, signals rebuilt over 90 d);
  - the job wall clock (520 s);
  - Spotify calls (guard caps);
  - the report (7 days shown, versions ≤ 89 days).
  - Never-pruned tables (`play_events`, `track_cache`, `run_log`,
    `playlist_version*`, …) grow under 20k rows a year.
- **Not changed (noted):** the release scan and composer keep their windows. A
  rising `known_artist_share` (the lane running dry) now costs usable picks, not
  tokens or searches.
- **Tests:** `test_llm_discovery.py` has 286 checks (31 new; the review's
  mutations — positive exemption, `run()` retry wiring, the retry after a
  timeout — now each fail a check):
  - the smaller thinking retry (after a cut-off / unusable reply only), its
    held-back time, 9-of-12 acceptance, no bigger budget, stages, the wiring
    in `run()`;
  - the reasoning share;
  - the name prefilter (no search) and the positive exemption;
  - de-duplicated lists, compact JSON, no `why`;
  - the prompt budget (feedback first, floors, newest kept, no loop);
  - 40,000-id lookups;
  - the usage footer (quiet / ⚠️ / cut off / no run);
  - the report layout;
  - doc sizes.

## 2.21.0 — 2026-10-12 (14-day DeepSeek input cap; DeepSeek skip report replaces the skip digest)

User requests:
- **Disable the daily skip check** — the user is confident in the skip logic.
  Job `bdbb183faf82` (Spotify skip digest) is **paused**, not removed;
  `hermes cron resume bdbb183faf82` brings it back. Its wrapper and the
  `skip_signals` digest / label / accuracy code are unchanged.
- **Does the DeepSeek input grow every day? Cap it at 14 days.** It did grow:
  - **Prompt tokens per batch:** 2.5k → 4.2k → 4.9k → 5.5k → 6.1k → 6.3k
    (batches 1–6).
  - **Windows before this release:**
    - favourites: 60 days;
    - WORKED / DIDNT_WORK: 21 days;
    - FEEDBACK_SUMMARY: 30 days;
    - MORE_FROM: 90 days;
    - LIBRARY_ARTISTS: all history (166 artists, up to 400 names);
    - DO_NOT_SUGGEST: the last 7 batches, however old.
  - **Now:** `input_max_days: 14` caps every one of them (`input_days()`).
    - The library list is the artists heard in the last 14 days (82 today), and
      DO_NOT_SUGGEST is the last 7 batches inside 14 days.
    - The known-artist filter after the call still uses every artist ever heard,
      so no known artist can reach the playlist.
    - Config: `profile_window_days`, `feedback_days` and
      `adaptive.feedback_days` are 14. MORE_FROM asks
      `positive_artists_ranked` for 14 days.
    - `positive_artist_days` stays 90 for the composer's "more from" slots and
      the release-scan order, because only the prompt is capped. The first
      draft capped those too; the independent review caught it (a stored more-from pick
      would have expired mid-week, and a 20-day-old positive artist's new
      releases would have been cooled off).
  - **Batch detail:** `detail.profile` also records `more_from`,
    `do_not_suggest`, `input_days` and `prompt_chars`.
- **Reasoning headroom:** batches 5 and 6 reasoned for 32.5k and 35.3k tokens
  (37.6k of the 40k `max_tokens` with the reply), close to the 10-09 cut-off.
  - `max_tokens` is now 48000 and `request_timeout_s` 250. A probe confirmed the
    API accepts these values.
  - `job_budget_s` 520 still bounds the whole job, and the non-thinking fallback
    is unchanged.
- **New nightly report: the share of DeepSeek songs skipped vs the benchmark.**
  - **Files:** `src/llm_report.py` and the no_agent wrapper
    `~/.hermes/scripts/spotify_deepseek_report_wrapper.py`.
  - **Schedule:** cron "Spotify DeepSeek skip report", `30 23 * * *`, posting
    to the Discord playground.
  - **What the wrapper does:** it rebuilds the skip signals, as the digest did,
    so the 01:20 batch still learns from the evening.
  - **What it prints:**
    - today's, per-day and since-7-Oct skip share of new DeepSeek songs (picks
      + new releases), with picks and new releases also shown separately;
    - the benchmark;
    - a better / worse / no-clear-difference verdict on the 95% range of the
      **difference** between the two skip shares (Newcombe, from both Wilson
      ranges). The benchmark is only 23 songs; the first draft compared against
      it as if it were exact, and the review caught that;
    - encores, shown apart.
  - **When it posts:** nothing when no DeepSeek song was finished or skipped
    in today's listening day. DB only.
  - **Today and the per-day line** count by listening day (04:00→04:00, every
    version live that day), so a manual refresh or a failed 03:00 refresh
    doesn't break them (review finding). The running totals judge each version
    over its lifetime, as `stats()` does.
  - **The digest wrapper's `auto_accept`** no longer runs. Accuracy isn't
    reported any more, and accepting playlists the user never saw would be
    wrong.
  - **Counting:** the same rules as `stats()` (one verdict per position: skip
    or skim = skipped, past 90% = finished; stops and pass-throughs not
    counted).
  - **Benchmark recomputed with those rules:** artist-graph discovery in the
    sampler-judged playlists of 10-01 → 10-06 was finished 6 of 23 and skipped
    17. The user's "26%" was that *finish* rate, so the skip benchmark is 74%.
  - The benchmark is fixed in `llm_report.BENCHMARK`, because its signals age
    out of the 90-day rebuild. The `stats()` baseline line now uses it (it said
    7 of 24 = 29%, from an earlier signal rebuild).
- **Tests:** `test_llm_discovery.py` has 244 checks.
  - New: the 14-day window for every prompt section, the ceiling over longer
    windows, raising the cap widens all of them, and the known-artist filter
    still covers all history.
  - Report:
    - today / since launch / per kind / per day / benchmark;
    - an early exit at 95% counts as finished;
    - a manual refresh (Today sums both versions, and the per-day line agrees);
    - a failed refresh with listening;
    - pre-launch versions are ignored;
    - the "Last 7 days" line;
    - verdicts, incl. 65 of 100 and 10 of 20 = no clear difference;
    - nothing on a day without verdicts;
    - under 1,900 characters.
  - The MORE_FROM cap is asserted on the default, longer-window and raised-cap
    paths. The composer keeps the 90-day positive window.

## 2.20.5 — 2026-10-09 (DeepSeek thinking cut off → reasoning_effort low)

Check of last night's run (user request):
- **Everything ran:**
  - the 23:30 digest posted the new accuracy line and silently accepted 10-07;
  - batch 3 was ok (430 s), and its release scan covered 53 artists (27
    fetched, 25 cached) and found 4 releases;
  - the guard logged 103 calls, all 200;
  - the 03:00 refresh had 4/4 wildcards, 3 encores, 9 picks (3 more-from),
    and the live playlist matched.
- **Issue:** batch 3's thinking call spent all 32,000 output tokens reasoning
  and was cut off (the prompt has grown with feedback to 4.9k tokens; batch 2
  had used 24.6k). The non-thinking fallback took over: 12/30 usable, 10 picks
  by already-known artists (rejected by the ID check, so the playlist wasn't
  affected).
- **Probe on the real prompt:** `reasoning_effort: medium` was still cut off at
  32k; `low` finished in 110 s with 24.7k tokens and 30 picks + 15 similar.
- **Fix:**
  - the thinking call sends `reasoning_effort` (config, default `low`);
  - `max_tokens` 32000 → 40000;
  - `request_timeout_s` 200 → 220;
  - the non-thinking fallback is unchanged.
- **Not issues:**
  - the nightly "delivery target lost thread_id" warning for the refresh job
    has happened every day since at least 09-30 (the job was created from a
    Discord thread in May); the banner still posts to the channel;
  - the Yoon Jong Shin wildcard is legitimate: "First Love Is a Curse" is
    YUJU's, and the old artist_cache name was wrong.
- **Tests:** 202 checks.

## 2.20.4 — 2026-10-08 (skip check: no reply = correct; honest accuracy line)

The user asked why the digest always showed "precision 97% · recall 97%". It
wasn't a live number.
- **Only 3 days were labelled:** 09-30 with 3 real corrections, and 10-01 and
  10-02 "ok". Both "ok" days score 100% by construction, because every
  unlabelled position is stored as the digest's own call.
- **The digest re-showed that same figure every night:** tp 31 / fp 1 / fn 1,
  both errors on 09-30. Precision and recall were equal only because fp = fn.

User rule: if I don't reply, the skips were correct.
- **`skip_signals.auto_accept()`**, run by the digest wrapper before each
  digest:
  - every playlist version that ended ≥ 20 h ago with listening and no labels
    is stored with the digest's calls (`origin='silent'`), judged up to its end;
  - a later `label` reply replaces it;
  - CLI: `auto-accept`.
- **`accuracy()`** adds `corrections` / `silent_days` / `last_correction`;
  `db.get_labels()` adds `silent`.
- **The digest line** now reads `Accuracy (N days · no reply = correct · K
  corrections from you, last DATE): precision … · recall …`, and the reply
  prompt `Reply only if something is wrong …`.
- **Tests:** `test_skip_signals.py` 98 checks (7 new: silent days accepted,
  empty / recent / live versions skipped, idempotent, a late correction
  overrides and lowers the score, digest wording). Two checks updated for the
  new reply line.

## 2.20.3 — 2026-10-08 (first night under the guard: wildcard regression + more-from cap)

The audit of the 10-08 runs (user request) found everything ran:
- **Batch 2** at 01:20 was `ok` (thinking, 349 s, $0.0074, 30 picks → 27 usable).
- **The guard** logged 99 catalogue calls, all 200, minimum gap 2.52 s, at most
  24 a minute, no ban.
- **The 03:00 refresh** was ok in 2.9 s and the live playlist matched.
- **Encores** were placed at positions 4 / 6 / 8. All 3 were finished again on
  the morning drive (affinity 0.96 → 1.99), so they become favourites as
  designed.

Two problems:
- **0 new releases → 2/4 wildcards.** v2.20.2 halved the scan to 30 artists and
  scanned up to 15 positive artists first, so the earlier similar artists with
  actual releases were never scanned (X:IN, mimiirose, PRIMROSE, Lapillus…).
- **7 of 9 fresh picks were "more from" songs.** DeepSeek ranked them first.
  User choice: cap at 3 of 12.

Fixes:
- **More-from cap:**
  - the composer fills at most `more_from_max_slots` (3) llm slots with picks
    by positive artists, interleaved with new-artist picks;
  - `LAST_INFO.more_from` reports the count;
  - the prompt allows at most 5 such picks.
- **Release scan:**
  - `positive_release_max` 15 → 8;
  - `new_release_artists_max` 30 → 60;
  - new `release_fetch_max` 30 fresh fetches per run (cached artists are free,
    the rest deferred), so calls stay inside the guard's hourly cap;
  - a guard budget refusal stops the scan quietly (`guard_limited`); only a
    real or remembered ban is `rate_limited`.
- **Tests:** 200 checks.

## 2.20.2 — 2026-10-07 (Spotify catalogue guard: root cause of the 4.8 h ban)

The user asked what caused the 4.8 h catalogue ban and to make sure it never
happens again.

**Evidence (Pi logs and DB):**
- Only this skill calls the catalogue, with 0 Hermes Spotify-tool calls in the
  window.
- **10-06 23:14–23:22:** ~230 catalogue calls in ~8 min at ~50/min (80-artist
  name backfill + batch 1). No 429.
- **No catalogue calls until 10-07 17:44.** Then the v2.20.0 deploy's dry-run
  batch made ~85 calls at ~50/min.
- **The next album-list call** got `429 Retry-After 17160`.
- **A diagnostic in a fresh process then called the banned endpoint again,**
  because nothing remembered bans across processes.
- `me/player` and the playlist write were unaffected.

**Cause:** a second burst within 18 h, triggered by a daytime dry run during
deploy. Spotify does not document the multi-hour escalation, so cumulative load
is inferred, not proven.

**Fix:**
- **`client.py` catalogue guard** for search / artists / albums / tracks GETs,
  in any process:
  - cross-process spacing ≥ 2.5 s;
  - rolling caps of 120 per hour and 200 per day;
  - a catalogue 429's Retry-After plus 10 min is remembered in the DB
    (`api_guard`), and every process refuses until then;
  - refusals are 429-shaped errors that callers already treat as no data;
  - an unusable guard DB fails closed;
  - player, playlist and `/me` calls are not guarded.
- **New `db.py` tables:** `api_call_log` (3 days) and `api_guard`.
- **`llm_discovery.run()`** refuses outside 00:00–02:45 SGT (forced and dry
  runs included) unless `--daytime`, and skips while a ban is remembered.
  `stats` shows catalogue usage and bans.
- **Smaller nightly job:** 30 picks (was 40), 15 similar artists (was 25), 30
  release artists (was 60), 10 name lookups (was 30), 2.5 s spacing.
- **Deploy:** seeds today's known ban (until 22:46 SGT) into `api_guard`. Deploy
  scripts make no catalogue calls, and nobody runs dry-run batches in the
  daytime.
- **An independent review before deploy; fixed:**
  - a shorter 429 could overwrite a remembered long ban (SQLite `MAX(real, text)`
    returns the text; both sides are now CAST);
  - a guarded 429 now returns at once instead of sleeping the Retry-After first;
  - guard config values are coerced per key (a typo keeps that key's default
    instead of silently disabling ban memory);
  - the ban write gets a longer, retried lock wait;
  - less refusal when processes contend;
  - far-future log rows (the clock stepping back) are ignored;
  - batch and legacy catalogue endpoints are guarded too;
  - caps lowered to 120 per hour and 200 per day.
- **Tests:** 193 checks (spacing, logging, the ban remembered across processes
  without a network call, a shorter 429 keeping the longer ban, an immediate
  return on a guarded 429, clock skew, config coercion, player calls unguarded,
  hourly cap, fail-closed, the night window, a skip while banned).

## 2.20.1 — 2026-10-07 (release scan: rate-limit handling + caching)

The v2.20.0 deploy's dry-run batch (17:44 SGT) returned **0 new-release
tracks**. A diagnostic showed every album call was refused: Spotify had put a
**4.8 h `Retry-After` (17160 s)** on the catalogue endpoints after ~110 paced
calls. `me/player` and the sampler were unaffected (heartbeat 0 errors), and the
03:00 refresh needs no catalogue calls. The scan had swallowed those errors
silently.

- **`fetch_new_releases` / `_releases_of`:**
  - counts fetched / cached / errors / rate_limited in `ctx['scan']`, recorded
    in `detail.release_scan`;
  - stops at the first 429;
  - a rate-limited scan, or one with more than 25% errors, makes the batch
    `partial` (the 02:20 retry runs) and the summary says why.
- **New caches** (`db.py`):
  - `release_scan`: each artist's album list, re-fetched at most every
    `release_rescan_hours` (72);
  - `album_tracks_cache`: album track lists, which never change;
  - dry runs write neither, and both are pruned.
- **Pacing:** `search_gap_s` 1.2 → 2.0 s.
- **Cleanup:** the release-scan blocked-artist set is built once per scan, not
  once per track.
- **Tests:** 178 checks (cache hit = 0 calls, 429 stops the scan, rate-limited
  scan → partial, dry run writes no cache).

## 2.20.0 — 2026-10-07 (learning loop on DeepSeek tracks)

The user asked whether the daily refresh learns from skips on DeepSeek tracks.
It already did as a filter: heard songs are retired, skipped artists become known
and drop out, and raw WORKED / DIDNT_WORK lists go into the prompt. But a song
the listener liked was never reinforced. One full listen decays below the 1.0
"liked" threshold by refresh time, so a finished pick just vanished. The model
also never learned which kind of pick works.

- **Encores** (`llm_discovery.encore_candidates`, composer source `encore`):
  - finished, never-skipped DeepSeek songs (picks or new releases) return;
  - first finished within 7 days, at most 2 heard encores, not already liked;
  - up to 3 of the 12 llm slots, placed first in the new-music block;
  - a second full listen makes them favourites the normal way.
- **Labelled feedback:**
  - WORKED / DIDNT_WORK carry where each song came from, plus fit / tier / angle
    for DeepSeek picks and `left_at` for skips;
  - a new `FEEDBACK_SUMMARY` (finished/judged by kind, fit, tier, angle; 30
    days; DeepSeek-proposed placements only; one verdict per placement);
  - the prompt now tells the model to lean toward what gets finished.
- **Adaptive:**
  - the close/adjacent share is reweighted by smoothed finish rates once each
    fit has 8 verdicts (clamped 0.5–0.85);
  - the daily angle uses Thompson sampling once the angles have 8 verdicts
    (seeded by the SGT date);
  - both are recorded in `llm_batch.detail`.
- **Positive artists:** window 30 → 90 days (`positive_artist_days`); their new
  releases are scanned first (`release_sources`).
- **`_placement_outcomes`** now returns `heard_pct`; encore placements count
  everywhere llm / wildcard do.
- **`stats`** gains an `encore` row and a learning view.
- **Tests:** a new learning-loop section on a fresh DB. A smoke run on the 10-06 DB copy caught v2.18 graph
  wildcards being counted as DeepSeek feedback, now fixed.
- **Two independent reviews before deploy:**
  - **Correctness:** the disabled path was checked byte-identical over 5 seeds,
    and the enabled path without encores matched 2.19.1.
  - **Learning-loop design:** simulations of encore, liked-pool, bandit and
    adaptive-share behaviour.
  - Fixed:
    - positive artists would fill the 60-artist release scan within about a
      month; they're now ordered by recency and capped at
      `positive_release_max` 20;
    - library and MORE_FROM prompt lists were cut alphabetically (lowercase and
      Hangul names dropped first); now by recency, library 400;
    - a once-heard, non-positive artist's new releases are allowed after a
      60-day cool-off, plus a `known_artist_share` alert above 40%;
    - FEEDBACK_SUMMARY is sampler-only, and its kind comes from the placement's
      section;
    - replay skips within 12 h of a finish are dropped everywhere;
    - `early_exit` at ≥ 90% counts as finished;
    - the encore placement-day cap (4), and an encore top-up when picks run short;
    - the angle bandit waits until every angle has 5 verdicts;
    - `adaptive` config is parsed strictly, and a failing learning step falls
      back to the weekday angle and base share without costing the batch;
    - the stats learning view is guarded.
  - **Not changed (user decision):** "liked" lasts about a week without replays,
    because of the scorer's 7-day window.
- **Tests:** `test_llm_discovery.py` 173 checks.
- Built offline while the Pi was unreachable; deployed after a drift check
  against the 2.19.1 checksums.
## 2.19.2 — 2026-10-06 (skip digest survives a late refresh; suite rot fixed)

A second refresh in the same SGT day silenced the skip check: a user-triggered
23:27 refresh replaced the playlist 3 minutes before the 23:30 digest,
`version_for_day()` picked the fresh version, and the digest reviewed its
3-minute lifetime — empty output, nothing posted. No data was lost (segments,
signals and plays were all intact); only the review window was wrong.

- **`version_for_day()`** (`skip_signals.py`) now ranks a day's versions by the
  listening observed inside each version's span of the day (sampler signals +
  recorded plays), then wall-clock coverage, then recency — a late refresh no
  longer takes the day over. Normal days (one 03:00 refresh) are unchanged.
- **Digest header** adds `(playlist refreshed again at hh:mm — this check covers
  up to then)` when the reviewed version was replaced mid-day.
- **Test rot fixed:** the "2 days may ban" check pinned `scorer.datetime` to the
  test's `now` — `update_blocklist()` windows on the real wall clock and the
  check had started failing today when its fixed dates slid out of 7 days
  (pre-existing, unrelated to the digest bug).
- **Tests:** D1 regression — a late refresh keeps the day's version and the
  digest still renders, noting the refresh time. Suite: 91 checks, all pass.

## 2.19.1 — 2026-10-06 (first live batch: wildcard fixes)

Batch 1 (23:16 SGT, thinking) gave 40 picks → 30 verified, 21 similar artists
and 19 new-release tracks, with 0 J-pop and 0 known artists among the usable
ones. Two of the dry-run's 4 wildcards weren't really by the similar artist:
- "Click — Моднюша, X:IN";
- "Live My Life — HEARTSTEEL, League of Legends" (BAEKHYUN only featured).

"POSE! - Kawaii ver." also got through as an alternate version.

- **New releases:** the similar artist must be the **primary** artist (`_releases_of`).
  The composer also re-checks this for stored rows via the new
  `load_candidates()` → `_artist_id` field.
- **`bad_version`:** any "<word> ver." / "<word> version" tag other than Korean or
  original is an alternate version ("Kawaii ver.", "(2024 ver.)", "- Holiday
  Version").
- **Tests:** 128 checks.

## 2.19.0 — 2026-10-06 (DeepSeek picks replace the artist graph)

The user felt discovery and wildcards were weak. The data agreed:
- **Discovery** (2026-09-30 → 10-06): 7 finished of 24 judged (29%), against
  ~86% for core.
- **Wildcards:** 7 of 28 slots filled.
- **Why:** both only ever drew from the top-10 liked artists, because Spotify
  removed every new-artist API.

A read-only probe asked `deepseek-flash` for 25 songs by unheard artists:
- **Thinking:** 24/25 matched an exact title and artist in Spotify search (the
  1 miss was "CHUNG HA" vs "Chungha").
- **Non-thinking:** 21/25.
- **Gotcha:** the model thinks by default, and at 8000 max_tokens it returned
  empty content.

User decisions:
- 24 core / **12 DeepSeek picks** / **4 wildcards** = new releases by similar
  artists DeepSeek names.
- Lesser-known 4th/5th-gen girl-group lane plus Korean adjacent scenes, **no
  J-pop**.
- New songs mixed in from position 4.
- Live from day 1.

- **New `src/llm_discovery.py`** + `~/.hermes/scripts/spotify_llm_discover_wrapper.py`
  (cron 01:20 SGT + 02:20 retry, deliver local):
  - **Profile:** 60-day decayed full listens with exclusions, J-pop artists
    removed; recent new-music outcomes; library and recently suggested artists.
  - **DeepSeek:** one call (thinking, then one non-thinking retry; wall-clock
    timeout; key from `~/.hermes/.env`, never logged).
  - **Verification:** every pick and similar artist is checked with Spotify search.
  - **Releases:** ≤30-day releases of the similar artists.
  - **Storage:** `llm_batch` / `llm_pick`.
  - **Run safety:** lease, once per 6 h, refuses 02:50–03:25, 520 s budget, rows
    saved per stage.
- **Rules:**
  - New artist means by artist ID (`known_artist_ids`). Artists with finished
    DeepSeek songs may get another.
  - J-pop guard: kana, Japanese versions, Japan albums, blocked names, and
    `origin` must be KR.
  - Version guard: sped-up/slowed, intro/outro/interlude/skit, under 90 s.
  - Title/artist matching on compact keys ("선물 (Gift)" matches "Gift"). Near
    misses are rejected, never substituted.
- **composer.py:**
  - `composition.llm`, `new_music_layout` (mix|block), `LAST_INFO`.
  - `_llm_candidates` re-checks heard / skipped / known / guards / window from
    the DB, with **no API calls at 03:00**, and never raises.
  - One song per new artist across llm + wildcard (`_Picker.fill(counts=)`).
  - Shortfall → liked → neutral.
  - `_spread` / `_round_robin`.
  - `llm_discovery.enabled: false` = exact v2.18 (the llm slots go back to graph
    discovery, top-artist wildcards, block order).
- **refresher / refresh wrapper:** run_log `detail.llm`. The banner gets one
  `ℹ️` line only when picks were short because the batch failed or is stale.
  Model text is never printed.
- **db.py:** `llm_batch`, `llm_pick`, `artist_name`, plus
  `upsert_artist_names` / `get_artist_names`.
  - On 2026-10-06 only 33 of 90 library artists had a name.
  - `artist_cache` names turned out unreliable: the graph stored the first
    album's primary artist, so "Chapter You" was cached as MeloMance but is
    WENDY's. Sampler and API names win.
- **Tests:** new `scripts/test_llm_discovery.py` (121 checks: matching, guards,
  replies, retry, batches/lease, composer incl. rollback).
  `test_track_name_filters.py` 120/120.
  - `test_skip_signals.py` 88/89. The failing "sampler skips on 2 days may ban"
    **fails identically on the untouched 2.18.2 code**. It passed 89/89 on 10-01,
    so it depends on the date; not touched here.
- **Three independent reviews (correctness, matching/prompt/security, ops) before deploy.** Fixed:
  - null items in Spotify lists;
  - stage isolation, so a late-stage crash or more than 25% failed lookups gives
    `partial` (retries; saved picks stay usable);
  - a similar artist must match together with its own song (same-name artists);
  - Korean `origin` is required for similar artists too;
  - artist names in kanji with no Hangul are blocked, and J-idol names added to
    the block list;
  - "LiSA" removed from the block list (it also blocked BLACKPINK's LISA);
  - more Japanese markers;
  - English / Chinese / acoustic / piano / remastered tags rejected, and other
    versions of heard songs count as heard;
  - titles matched main-to-main or main-to-bracket only (no more "A (feat. X)"
    matching "B (feat. X)");
  - `pick_best` skips results the filters would drop;
  - `MORE_FROM` instead of the WORKED exception;
  - `DO_NOT_SUGGEST` newest first;
  - no redirects with the key;
  - `.env` `export` prefixes and comments;
  - lone surrogates stripped;
  - the start guard accounts for the job budget;
  - 90-day retention;
  - the CLI force-refreshes the token;
  - the mix layout starts new songs at position 4;
  - `max_tokens` 32000.
- **Docs:**
  - new `references/llm-discovery.md`;
  - updated SKILL.md, schema.md, scoring-and-composition.md, cron-wrappers.md,
    CLAUDE.md.

## 2.18.2 — 2026-10-01 (user rule: < 90% heard = skip)

The user enabled skip signals on 2026-10-01 through the Hermes agent, after
verifying two live sessions (config note and `.bak-enable-20261001` by that
session). They also stated the rule: *if I didn't listen to at least 90% of the
song, it counts as a skip* — even a first listen that turned out not to be liked.

- **Thresholds:** `judge.skip_completion_max` 0.30 → **0.90**, and
  `cooldown_completion_max` 0.60 → 0.90 (config.yaml and `skipsig.DEFAULTS`).
  An exit before 90% via next/tap is now `skip` (≥5s) or `skim` (1.5–5s); an
  exit at ≥90% is a listen.
- **Scoring** (`skip_signals._segment_row`):
  - Skips **and skims** are scored as skips. `score_ms` is capped at 29% of the
    duration, giving −1.0, or −1.5 under 10%, so a 70% skip can no longer count
    as a +0.5 listen. Skims were previously unscored.
  - `negative_days` counts skims too.
  - Unchanged: passes <1.5s, `stopped`, and the neutral exits.
- **Digest:**
  - The "Skipped" header now says "before 90% of the song", and each skip shows
    the % reached.
  - Exits at ≥90% join "Played through".
  - Off-playlist skips are a full section, one line each. Before, they were
    capped at 4 on one line, and today's 5th (Love Is Lonely) was dropped.
- **Tests:**
  - `test_skip_signals.py` 89/89: 33%, 70% and 93% exits; the 70% skip score
    cap; resumed tracks left at 85% vs 95%.
  - The flag-off checks no longer depend on the live config's `enabled`.
  - Golden flag-off is identical to 2.18.1.

## 2.18.1 — 2026-09-30 (digest readability — user feedback)

User feedback on the first digest: nothing explained what "inferred T1-dense" or
T1 vs T3 meant, and song names alone didn't help recall.

- **skip_signals.build_digest** is rewritten around plain-language sections.
  - The section headers act as the legend: ⏭ Skipped, 👀 Skimmed, ⏩ Left
    early, ⏭? Probably skipped, ? Length unknown, · No verdict.
  - Each flagged song is shown as `#pos Song — Artist`.
  - The fallback's evidence is one `↳` note per gap ("~35s unaccounted for",
    "4 passed in a quick run (13s)", "4 missing over 4:31; could also be a
    pause", "right before autoplay took over"). No tier codes are shown.
  - The summary lines (Played through / Not reached, accuracy, reply) are never
    trimmed.
- **Artists:**
  - new `playlist_version_track.artists` column (race-tolerant `_migrate`);
  - `refresher` saves the artists it has (discovery/wildcard);
  - `skip_signals.fill_version_meta` fills the rest from ONE live-playlist read
    in the digest wrapper, best-effort;
  - fallbacks: sampler segments, then `artist_cache` names.
- **db** — `set_version_track_meta`; `get_playlist_versions` returns `artists`.
- **Tests:** `test_skip_signals.py` 86/86. Flag-off golden is identical to the
  deployed 2.18.0.

## 2.18.0 — 2026-09-30 (skip detection: playback sampler + stored playlist versions + cooldown, shadow mode)

### Root cause

Skipped tracks lingered because the pipeline almost never saw skips.
`recently-played` records only a play's END time and omits most skips.

On 2026-09-30 the user played the playlist in order and confirmed 15 skips
(positions 10, 15, 16, 18, 25–28, 32–35, 37, 38 and 40). The pipeline recorded
**1**, and that one was an off-playlist track. Over the 7 days before, it saw
195 plays and 1 recorded skip.

Consequences:
- Skipped core tracks kept their affinity (+2.2 to +3.6) and left only when
  plays aged out of the 7-day window.
- Skipped discovery/wildcard tracks stayed "unheard", so they were re-picked
  daily.
- A play after a pause or a skip burst scored as a full listen. #37 was paused
  34 min and then skipped, but scored +1.0.

Recording is not governed by a ~30s threshold: a 24s play was recorded, and
several passes of 35s or more were not.

### Added

- **`src/skipsig.py`** (pure, stdlib):
  - `parse_player`, `Segmenter` (per-track exposure from `/me/player` samples;
    pause/204/device-switch continuity; unseen-resume bound; resumable state),
    `classify_segment` (completed / skip / skim / pass / early_exit / stopped /
    neutral), `match_segments_to_events`, `listened_with_recorded_end`.
  - `infer_uncovered` — positional fallback T1/T2/T3 plus `unknown_play`.
- **`src/skip_signals.py`:**
  - `rebuild_signals` rebuilds the derived `skip_signal` table from scratch on
    every poll.
  - `scoring_inputs`, `cooldown_state` (cooldown / demotion / recent), heard
    and skipped id sets, and the `ban_evidence_ok` 2-day gate.
  - The nightly digest, a free-text `label` CLI and `accuracy`.
- **`src/sampler.py`:**
  - Adaptive GET `/me/player` loop: 3s playing, 1s bursts, 10s paused, 5s for
    45 min after a stop, 15s/60s idle, and at most 2 per minute around 03:00.
  - Token bucket (20 per 30s), 2× Retry-After on 429, 401 handling with backoff.
  - Buffered writes with a 2s busy timeout, coverage runs, persisted
    segmenter state.
- **`~/.hermes/scripts/spotify_sampler.py`** plus
  **`~/.config/systemd/user/spotify-sampler.service`** (user unit,
  `Restart=always`, `MemoryMax=150M`).
- **`~/.hermes/scripts/spotify_skip_digest_wrapper.py`** plus the no_agent cron
  job at 23:30, delivering to the playground channel.
- **DB** — see `references/schema.md`.
  - Tables `playlist_version(_track)`, `player_sample`, `play_segment`,
    `sampler_coverage`, `sampler_state`, `skip_signal`, `skip_label`.
  - Column `play_events.context_uri` via a race-tolerant `_migrate`.
  - The upsert now COALESCEs `ms_played` (same semantics as before) and
    `context_uri`.
  - `get_conn(timeout)`.
- **config.yaml** — new `skip_signals:` block, `enabled: false` (shadow mode).
- **`scripts/test_skip_signals.py`** — 82 checks:
  - a playback simulator covering bursts, pause-then-skip (paused and 204
    variants), session-start taps, next/jump/back/restart, repeat-one,
    crossfade, device switch, 204 windows, ads, private sessions, context
    switch, a stop at 88%, exits hidden by a 42s gap, and restart from
    persisted state;
  - a replay of the 2026-09-30 real data (`scripts/fixtures/skip_2026_09_30.json`)
    against the user's labels;
  - temp-DB policy integration.

### Changed

- **poller.py** — stores `context.uri` and calls
  `skip_signals.rebuild_signals()` best-effort. The poll detail gets `signals`
  or `signals_error`. The `_infer_ms_played` math is unchanged; only
  `context_uri` is passed through.
- **refresher.py** — `_replace_tracks` returns the PUT's `snapshot_id`. After
  the write, inside the no-fail block, it saves the composition to
  `playlist_version` with names and sources. The refresh detail gets
  `snapshot_id_after`.
- **scorer.py** — `compute_affinity(window_days, now_ms=None, use_signals=None)`
  and `update_blocklist(scores, use_signals=None)`. With signals on: sampler
  overrides, derived events, and `unknown_play` unscored; a sampler-driven ban
  needs negative evidence on 2 days.
- **composer.py** — `_passes_output_filters(t, blocked, cooldown=None)` and
  `_Picker(blocked, cooldown=None)` (track id + same-song signature). The heard
  set is extended; skipped wildcards are never re-offered; a relax pass runs on
  underfill. All of this is behind the flag.
- **client.py** — `_do_request(..., max_time=30)`. The sampler uses 8.
- **Poll and refresh wrappers** — the `auth.json` read-modify-write now runs
  under Hermes' `_auth_store_lock` (race with the sampler's token refresh).

### Independent review (3 reviewers, 2026-09-30) — fixed before deploy

**Sampler / Segmenter false-skip paths:**
- An overnight "paused" state continued one segment, so a morning "start
  fresh" tap became a skip. Continuity is now measured from when the track last
  played, and longer pauses start a new session.
- A tunnel/offline 204 followed by the next track read as an early skip. Exits
  bridged by a 204 are now `lost`.
- Server-extrapolated progress faked completions. Segments are now truncated at
  an earlier recorded end.
- Two "previous" presses mislabeled the middle track. The history is now 5
  tracks, and taps to an earlier position are neutral.
- `flush(lost)` was ignored.

**Coverage:**
- 204/stale samples and 429/exception gaps counted as "watched". Only observed
  device states now extend it, with ≤20s gaps.

**Fallback:**
- Shuffled sessions produced dozens of inferences, as did taps over many
  positions and queued tracks through T3. Now: sequential sessions only, at most
  6 positions, and T3 only near the end and followed by autoplay.
- The `unknown_play` slack went from 5s to 20s.

**Policy:**
- 30–60% exits now feed the cooldown (#25–28 pattern).
- Demotion is capped at 30 days.
- The relax pass is oldest-first.
- Fallback signals are kept out of heard, `skipped_ids` and `recent24h`.
- `coverage_miss` no longer counts as matched, or as claimed.
- Two-pass matching (+60s) prevents double counting.
- Relinked ids are handled (`alt_track_id`).
- Ban days run 04:00→04:00.
- A quoted `"false"` no longer enables the flag.
- A null config block keeps its defaults.

**Digest / labels:**
- The coverage line counted misses.
- `17, 30 not`, ranges and lists are parsed.
- The full truth set is stored at label time.
- Accuracy is scoped to the reviewed window, per source and per day, plus
  exit-time error.
- The digest fits one Discord message (UTF-16), with a sampler-health line.
- The version end no longer falls back to the wall clock.

**Operational:**
- The config is validated on (re)load, and the loop steps are guarded, so the
  unit can't crash-loop. It restarts indefinitely.
- Monotonic clocks.
- A real token re-read on the first 401.
- One SQLite transaction per flush.
- `INSERT OR IGNORE` segments.
- Wrappers skip the redundant `auth.json` rewrite if Hermes' lock times out,
  instead of failing the job.
- The refresher rebuilds signals before scoring.
- Docs: the test channel in `cron-wrappers.md` is corrected to the playground,
  and the scope list now includes the playback scopes.

### Validation (local)

- `test_skip_signals.py` 82/82 (54 + 28 review regressions).
- `test_track_name_filters.py` 120/120.
- **Golden flag-off test.** The old and new code on a sanitized production DB
  copy, with a frozen clock and a seeded composer, gave bit-identical scores,
  liked set and 40-track playlist.
- **Fallback replay of 2026-09-30:** precision 14/15, recall 14/15. The false
  positive is #17, a pass-through inside a 13s burst. The false negative is #37,
  which is now `unknown` instead of +1.0; the sampler resolves both.

### Docs

- New `references/skip-signals.md`.
- `SKILL.md`: architecture, skip-signals summary, service, digest and label
  operations. The test channel is corrected to the playground channel.
- `schema.md`, `cron-wrappers.md` and `troubleshooting.md` gain v2.18
  sections, and `skip-detection-limitation.md` gets a dated correction.

## 2.17.0 — 2026-09-23 (tune-up: played_at = play END, composer rewrite, run budgets)

### Root cause
**`played_at` marks when a play ENDED, not when it started.** Proved on 1,276 production inter-event gaps: the gap to the previous event matched the *current* track's duration 911× vs the *next* track's 11×. Since v1.x the poller computed `ms_played_i = min(dur_i, played_at_{i+1} − played_at_i)`, i.e. credited every track with the NEXT track's listen time: a skip penalized the track played *before* it and the skipped track scored as a full listen. All 6 blocklist bans in the DB traced to such mis-scored plays (they score +0.70..+1.00 under the corrected math). Separately, the composer sampled first and filtered afterwards, so filtered/capped picks were never refilled (discovery averaged 7.9/12, wildcards 0.06/4, ~8 fallback tracks per run), and the refresh had no overall time budget against the gateway's 600s kill.

### Fixes
- **poller.py** — `ms_played_i = min(dur_i, played_at_i − played_at_{i−1})`; the oldest event's predecessor comes from the DB (`db.get_latest_played_at_before`), so overlapping polls compute identical values. Gap < 0 or > 6h → full play; gap == 0 → None; ties sorted `(played_at, track_id)`. The newest event is no longer "pending". `events_stored` = actually-new rows; a full window of all-new events sets `possible_truncation` + stderr warning. Any poll failure writes an `'error'` run_log row; a failed fetch raises `PollError(status)`.
- **db.py** — upsert: any non-NULL `ms_played` overwrites, NULL never does (the old "only ever lower" rule assumed start-time semantics); returns the new-row count. `artist_cache` TTL 24h → **36h** (24h expired seconds before every 03:00 run → always cold).
- **scripts/migrate_2026_09_played_at_end.py** (one-shot, run 2026-09-23) — backed up to `spotify.db.bak-playedat-20260923`; recomputed **1,547 of 3,522** `ms_played` values with the new rules; lifted all **6** bans (BILLIONAIRE, To. My First Love, Can't Help Myself, Swipe It Out, Boom Boom Boom, Busy Boy — user-approved); marker row in a new `migrations` table (re-runs won't lift bans again). `affinity_snapshot` rows before 2026-09-23 are not comparable with later ones.
- **composer.py (rewrite)** — guardrails applied **while picking** via `_Picker` (output filters, track-ID + signature dedupe, per-section cap 3, combined cap 4). `_apply_section_guardrails`, `_apply_guardrails`, `_fill_from_fallback`/`_try_add` and `_weighted_sample` are gone, replaced by `_passes_output_filters` + `_Picker` + `_weighted_order`. Core = exact weighted order (Efraimidis–Spirakis, log key — fixes the old cumulative-weight loop that over-picked the track after a heavy one). Discovery candidates filtered at ingestion incl. heard-by-signature. Wildcards: all top-10 artists, every track of releases in the 30-day window, unheard first, picked **before** discovery for cap accounting. Fallback: liked → neutral → leftover discovery/wildcard candidates. Slim track dicts in `artist_cache`; album track lists cached (first 5 albums + in-window); a failed/partial fetch never overwrites a good row (stale row reused). `dry_run=True` now writes nothing (it used to be ignored). Underfill warning → stderr; the refresh wrapper banner appends `⚠️ n/40 tracks` when short.
- **client.py** — per-stage deadline (`set_deadline`); no sleep after the last attempt; `Retry-After` honored if it fits the deadline, else fail fast + GET breaker; unbounded mode caps sleeps at 30s; catalogue GET pacing 1.2s; per-process memo of `artists/` + `albums/` GETs; `_fetch_album_tracks` lost its second retry loop (a timeout used to become 18 attempts per album).
- **refresher.py** — stage budgets POLL 60 / COMPOSE 290 / SNAPSHOT 40 / WRITE 150 s (< 600s kill); inner poll best-effort except 401/403; every pre-write failure → `'error'` run_log row; a run_log failure after the PUT can't report failure; snapshot GET uses `?fields=snapshot_id`.
- **auth.py** — `~/.hermes/auth.json` token preferred over the DB `oauth_token` row (was DB-first: a stale row shadowed the fresh wrapper token and triggered a doomed PKCE refresh per call); error message now says `hermes auth spotify` (was a non-existent `scripts/bootstrap_oauth.py`).
- **Cron wrappers** (`~/.hermes/scripts/`) — refresh banner appends `⚠️ n/target tracks` when the playlist is short.

### Validation
- **Offline harness** (100 seeds, production DB copy): discovery **7.9 → 12/12**, wildcard **0.06 → ~2.3**, fallback **8 → ~1.7**, **40/40** every run; API calls cold **92 → 52**, warm **49 → 1** (transitional until pre-2.17 cache rows expire).
- **22 deterministic probes** — poller: end-time inference, re-poll idempotence, overlap (predecessor from DB), new-row count, `PollError` + error row, gap 0 → None; client: no sleep after the last attempt, long Retry-After fail-fast vs in-budget wait, deadline respected, breaker open/close, GET memo, 1.2s catalogue pacing, `get_user_market` under deadline; refresher: 5xx poll continues, 401 aborts before PUT + error row, run_log failure after PUT still ok, stalled snapshot GET can't starve the PUT, deadline reset.
- **3 terminal-gate laps** with independent reviewers over the cumulative diff.
- **Live Pi staging** + live dry-run compose: **40/40 with 12 discovery**.
- **Deployed 2026-09-23 ~22:25 SGT**; pre-deploy backups `*.bak-tuneup-20260923`. First live refresh pending **2026-09-24 03:00 SGT**.

### Docs (docs pass after code freeze)
SKILL.md slimmed to triggers + operations + a References table (~8.7k → ~2.4k tokens); reference detail relocated to new `references/scoring-and-composition.md`, `references/filters.md`, `references/schema.md`, and `references/cron-wrappers.md` (rewritten against the live wrappers) / `feb-2026-api-audit.md` (write-restriction table, non-standard artist IDs). Fixed stale claims: token order, dry_run, `scores, _ = compute_affinity()` tuple, clear-then-add write snippet, "10am/8pm" polls, combined cap, "auth.py auto-refresh", `events_stored`, "snapshot_id only", wildcard fixes "not yet implemented", API-call counts, removed function names, the "pending last event" story. Dated correction notes added to historical docs (`spec-impl-findings`, `spec-compliance-audit`, `polling-data-loss`, `skip-detection-limitation`, `artist-avoidance-decay`, `impl-notes`, `post-full-run-findings`, `wildcard-rate-limit`, `troubleshooting`). `config.yaml`: comments only (unread `spotify`/`schedule`/`paths` blocks labelled; "every 3h" corrected) — parsed data identical. `exclusions.py`: docstring only (track_cache has had `album_name` since 2.16.0).

## 2.16.0 — 2026-08-28 (general album-name exclusion rule)

*Reconstructed 2026-09-23 from the SKILL.md / config.yaml / code notes of the time — no contemporaneous entry was written.*

- **New output-only rule `exclude.album_name_contains`** — any track whose album name **contains** one of the substrings (case-insensitive) is dropped from the playlist but still earns affinity. For whole *categories* of albums (first use: `(Soundtrack from the Netflix Series)`); no per-album ID lookup.
- **Album names now ride through the pipeline:** `track_cache` gained an `album_name` column (idempotent `ALTER TABLE` in `db._migrate`, run by `db.init_db()` from the cron wrappers; appended last so positional reads of old rows keep working). The poller caches `album_name` from `/me/player/recently-played` and re-upserts rows cached before the column existed on their next play (self-healing, no backfill job). The composer stamps `album_name` at every live-fetch site (`_fetch_track_metadata`, `_fetch_artist_top_tracks` incl. artist_cache copies, discovery source b, `_pick_wildcards`).
- **exclusions.py** — `_load_album_name_filters()` / `ALBUM_NAME_FILTERS`, `is_album_name_excluded()`; `is_output_excluded_track(name, artist_ids, track_id, album_name)` unions it with the other output exclusions. `scorer.compute_affinity()` still calls the narrower `is_excluded_track()`.
- **Merge:** the 2.15.1 per-ID `exclude.albums` entry for "Can This Love Be Translated? (Soundtrack from the Netflix Series)" (album `5Qg2iNuV6zTCGlIYunvvTd`, 31 tracks) was removed — its name contains the substring, so the general rule subsumes it exactly. XDM Unidentified Waves stays ID-based.
- **scripts/test_track_name_filters.py** — `ALBUM_NAME_MUST_BLOCK` / `ALBUM_NAME_MUST_KEEP` batteries against the live config; the 31 CTLBT ID cases were dropped from `ALBUM_MUST_BLOCK`. Suite: 120/120 (as re-run 2026-09-23).
- Also present in the 2.16.0 code but never changelogged: same-song/same-artist **signature dedupe** (`_normalize_track_name` / `_track_sig` / `_dedupe_tracks`; reported case Baby DONT Cry "Shapeshifter" single vs mini-album copy) — its date is unrecorded.

## 2.15.1 — 2026-08-09 (album exclusion: Can This Love Be Translated? OST)

Excluded the Netflix series soundtrack **"Can This Love Be Translated? (Soundtrack from the Netflix Series)"** (Kim Min Seok (MeloMance), WENDY; album `5Qg2iNuV6zTCGlIYunvvTd`, 2026-01-16, 31 tracks) from playlist output.

- **`config.yaml`** — second entry under `exclude.albums`, all 31 track IDs pinned (from `GET /v1/albums/5Qg2iNuV6zTCGlIYunvvTd/tracks?market=SG&limit=50`, two pages). Output-only, per the 2.14.0 mechanism: the featured artists stay liked and their plays still earn affinity, only this album's tracks are kept out of the playlist.
- No code change — `exclusions.is_album_excluded_track()` reads the flattened ID set from config at import, so the existing three guardrail sites pick it up automatically.
- **`scripts/test_track_name_filters.py`** — 31 new `ALBUM_MUST_BLOCK` cases. Suite **102 → 133**.
- **Verified on the Pi:** YAML parses (2 albums, 8 + 31 IDs); 133/133 tests pass; live playlist read shows 0 of the 31 currently present; full dry-run composes 40/40 with 0 leaks. None of the 31 appear in `play_events`, so scoring is unaffected either way.
- Note: `GET /v1/search` now hard-rejects `limit > 10` with `400 Bad Request` — the Feb 2026 cap documented in `references/feb-2026-api-audit.md` is enforced, not advisory.
- Docs: SKILL.md excluded-album list, config snippet, version bump.

## 2.15.0 — 2026-07-28 (new output filter: unplayable / region-locked tracks)

Excludes tracks Spotify keeps in the catalogue but does not license in the user's market — greyed out in-app, error *"this content is not available"* on play. Reported: izna **"Animal Fries"** (`4RVUZyL3dhQCDwJur8urbj`) and **"PROUD CITIZEN"** (`139EfvtayUOb6a3cui9nks`), both live in the playlist at the time of the report.

- **Diagnosis:** both return `is_playable: false` + `restrictions: {"reason": "market"}` with an empty `available_markets`. Neither was in `play_events` or `track_cache`, so both entered via the **discovery** path — `/v1/albums/{id}/tracks` does carry the playability fields, but nothing was reading them.
- **`composer.py`** — new `_is_unplayable(track)`. A **data** check, not a regex: `is_playable is False`, or a truthy `restrictions.reason` (any reason, not just `market`). **Fail-open** — a dict with neither field is treated as playable, since most tracks carry neither and failing closed would empty the pool.
- **Filtered at two levels.** Primary: each live-fetch ingestion point (discovery source a `_fetch_artist_top_tracks`, source b album tracks, and `_pick_wildcards`), where the raw API object still has the fields. Defense in depth: the three guardrail sites. Discovery/wildcard candidate dicts now also carry `is_playable`/`restrictions` through so the later guardrails have something to test.
- **Why not verify after composing:** batch `GET /v1/tracks?ids=…` returns **403** (removed Feb 2026 — confirmed live), and 40 individual `/v1/tracks/{id}` calls per refresh would hit the same ~50 req/min "Other" rate limit that already starves wildcards. Filtering where the data is already in hand costs zero extra calls.
- **Known coverage limit (documented, not fixed):** core + fallback tracks are rebuilt from `track_cache`, which has no playability column, and `/me/player/recently-played` returns neither field — so those paths have nothing to filter on. Acceptable: they come from the user's own play history and were playable when played. A track that goes region-locked *after* being played can still slip through; the fix would be a `track_cache` playability column + backfill job.
- **`scripts/test_track_name_filters.py`** — new `UNPLAYABLE_MUST_BLOCK` / `UNPLAYABLE_MUST_KEEP` sections (own battery: the filter takes a track dict, not a name) plus `_load_unplayable_filter()`. Covers both reported tracks, `is_playable` alone, `restrictions` alone (the no-`market=` case), a non-market reason, and four fail-open negatives (missing fields, empty dict, no `reason` key). Suite **93 → 102**.
- **Verified on the Pi:** `py_compile` clean; 102/102 tests pass; live `GET /v1/tracks/{id}?market=SG` for both reported tracks → `_is_unplayable=True`, control track METRONOME → `False`; both dropped by all three guardrail paths while the control survives.
- Docs: `references/troubleshooting.md` gains an "Unplayable / greyed-out tracks" section (symptom → root cause → fix, plus the `market=` and rate-limit gotchas). SKILL.md new filter section + renumbered guardrails 5–10 + version bump; CLAUDE.md guardrail order + new section.

**Also in this release — troubleshooting note on null playlist reads.** The existing "Tracks show null" entry gained a disambiguation warning: the Feb 2026 `item`-vs-`track` key rename and SG market blanking produce *identical-looking* null reads. Distinguish by count — all 40 rows null means the key bug, a handful means market blanking. (Hit during the 2.14.0 validation: `(i.get("track") or {}).get("name")` returned 40/40 nulls and read exactly like total market blanking.)

## 2.14.0 — 2026-07-28 (new exclusion mechanism: output-only album exclusions)

Added an **album exclusion rule** so whole albums can be kept out of the playlist. First use: **"XDM Unidentified Waves"** (XG × JAKOPS, album `1z3VCBUJBHkpDHtzOZP205`, 2025-01-31) — 8 XDM alternate versions of XG tracks.

- **Keyed by track ID, not album name.** No pipeline surface carries an album name: `track_cache` has no album column (album_name was dropped when the Nov 2024 endpoints were deprecated), and `/albums/{id}/tracks` returns *simplified* track objects with no album field. Track IDs are stable so this is exact; a re-release under a new album ID needs its own entry.
- **Output-only, deliberately NOT pre-scoring.** Unlike `exclude.artist_ids`, album exclusions do not run in `scorer.compute_affinity()`. XG is a genuinely liked artist — dropping these plays pre-scoring would erase real taste signal and distort the artist-graph. The tracks still earn affinity; they just never reach the playlist. (An artist-level exclusion was rejected for the same reason, and a name filter on `x XDM` was rejected as too fragile/over-broad.)
- **`config.yaml`** — new `exclude.albums` list: `{name, album_id, track_ids[]}` per album, with the lookup command documented inline.
- **`exclusions.py`** — new `_load_excluded_album_track_ids()` + `EXCLUDED_ALBUM_TRACK_IDS` (flattened set, read at import like the artist list), `is_album_excluded_track(track_id)`, and `is_output_excluded_track(name, artist_ids, track_id)` — the union of the scoring-level exclusions and the album list. `is_excluded_track()` is unchanged and still what the scorer calls.
- **`composer.py`** — the three guardrail sites (`_apply_section_guardrails`, `_apply_guardrails`, `_fill_from_fallback._try_add`) switched from `is_excluded_track` to `is_output_excluded_track`, passing `t.get("track_id", "")`.
- **`scripts/test_track_name_filters.py`** — album exclusions are ID-based, so they get their own sections rather than joining the name battery: `ALBUM_MUST_BLOCK` (all 8 XDM IDs), `ALBUM_MUST_KEEP` (unrelated ID + empty string), 3 `is_output_excluded_track` union cases, and an explicit assertion that the album list does **not** leak into the pre-scoring `is_excluded_track()` path. The script now imports `exclusions.py` for real (it only needs os/re/yaml) so the IDs are read from the live config instead of being duplicated into the test. Suite **79 → 93**.
- **Verified on the Pi:** `py_compile` clean; 93/93 tests pass; all 8 XDM tracks forced through each of the three guardrail paths are dropped while a normal XG track survives; full dry-run composes 40/40 with 0 XDM tracks. Token was expired at session start — refreshed via `resolve_spotify_runtime_credentials(force_refresh=True)`, same call the cron wrappers use.
- Docs synced: SKILL.md (new "Album exclusion" section, renumbered guardrail 5 + 8, config block, output-vs-category note, version bump), CLAUDE.md (exclusions paragraph + architecture line).

## 2.13.1 — 2026-07-14 (hermes-skill-tuneup: bug hunt → gate → docs)

Full four-pass tune-up after the recent filter edits (Inst. abbreviation, live trailing/quote/tour markers, new remix filter). Phase-0 live copy confirmed: cron jobs `f5e75496918a` (refresh, deliver=discord) + `afdb0b06e11a`/`dfadaf5e1833` (poll, deliver=local) are `no_agent`+`profile:None`, so `~/.hermes/scripts/spotify_{refresh,poll}_wrapper.py` import the skill-dir `spotify-refresh/src/` (single copy — no drift). Green baseline: regression suite 70/70, scorer n_scores=52. Independent blind reviewers (control-flow + data-shape + adversarial refuter) fanned out during the bug hunt **and** again as the terminal gate over the cumulative diff.

**Code fixes (composer.py, refresher.py):**
- **Live filter over-matched `Live in/on …` studio titles** (HIGH, self-introduced last session; data-shape reviewer confirmed, refuter confirmed 0 live rows hit today). `_LIVE_VERSION_RE`'s delimiter-free branch was `\blive\s+(?:at|from|in|on|@)\b`; the ambiguous `in`/`on` collide with real studio titles ("Live in the Moment", "Live in Color", "We Live On"). Dropped `in`/`on` from the *delimiter-free* branch → now `(?:at|from|@)`; `(Live in X)` / `- Live in X` are still caught by the unchanged paren/bracket/dash branches, so no real live recording slips through. Verified: 0 decision flips across all 156 real `track_cache` names; the only effect is un-blocking the studio titles.
- **Successful refresh could be reported as a FAILURE** (MED, reproduced via monkeypatch). In `refresher.refresh_playlist` the post-replace bookkeeping (`write_snapshot`/`prune_snapshot`/weekly `incremental_vacuum`) sat *inside* the try/except wrapping the playlist replace, so a `sqlite3` "database is locked" (or the Sunday vacuum) after a successful replace raised → the wrapper delivered "⚠️ playlist refresh failed" + exit 1 **and** wrote a spurious `status=error` run_log, even though the playlist was already updated. Moved the observability writes into their own guard that logs a warning to stderr and never re-raises; the refresh now returns `status=ok` once the replace + `log_run("ok")` succeed.
- **em-dash (U+2014) separator** was missed by all three dash branches (LOW, robustness). Normalized every dash class to `[\-–—]` (hyphen / en-dash / em-dash). Verified the class matches exactly those three code points (not U+2212 minus, U+2015 bar, etc.) — no over-match; no ReDoS (linear scaling to 1M chars).
- **Removed dead code** (`_top_artists_from_tracks` + `_resolve_artist_id`, no live caller — the live discovery path uses `_aggregate_artist_affinity`). The terminal gate then caught that this orphaned the `urllib.parse` import (and `math` was already unused) → removed both from composer's import line; re-verified compile + real import + suite.
- **Passes 2 (perf) & 3 (delivery): no change, by design.** `compute_affinity` ≈31 ms (indexed `SEARCH … idx_events_time`, no full scan); wall-clock is the once-daily, rate-limited Spotify album/artist fan-out with a 24h `artist_cache` + backoff — raising concurrency would risk the documented album-endpoint limits. Delivery already follows the scheduler exit-code/stdout contract correctly (no `hermes send`/`MEDIA:`/self-`curl`; fixed banner, no untrusted interpolation).

**Validation:** regression suite **70 → 79** (added 9 cases: `Live in/on` MUST_KEEP, delimited `Live in` MUST_BLOCK, em-dash MUST_BLOCK for all three filters). Deployed hashes match local edits; `py_compile` + real import clean under the gateway venv; scorer.py byte-identical to baseline (scoring behavior preserved — the fingerprint "drift" is only the time-dependent recency decay). Docs synced: SKILL.md live/instrumental/remix filter descriptions + version bump.

## 2.13.0 — 2026-07-14 (new output filter: remix versions)

Added a **remix-version filter** so remix editions no longer enter the playlist (reported: izna **"SIGN (Remix)"**). Third output-only track-name filter alongside live + instrumental, wired into the same three guardrail call sites.

- **`composer.py`** — new `_REMIX_RE` + `_is_remix_version(name)`. Matches `\bremix\b` (and `remixes`/`remixed`) inside a parens/bracket group (`(Remix)`, `[Remix]`, `(Club Remix)`, `(Tiësto Remix)`, `(The Remixes)`), after a dash separator (`- Remix`, `– Alesso Remix`), or as a trailing word (`Blinding Lights Remix`). Case-insensitive; `remix` is word-bounded so it does NOT fire mid-word ("Remixing", "Premix" kept) and does NOT catch `Remastered`. Threaded into `_apply_section_guardrails`, `_apply_guardrails`, and `_fill_from_fallback._try_add`.
- **Accepted tradeoff** (mirrors the baby-sleep filter): a song titled exactly "Remix" would also be caught — remix titles are reliably version markers, so this errs toward excluding them.
- **`scripts/test_track_name_filters.py`** — now also loads `_is_remix_version` from `composer.py`; added 9 remix MUST_BLOCK cases + 2 word-boundary MUST_KEEP negatives (`Remixing Hearts`, `Premix`).
- Docs synced: SKILL.md (new "Remix version filter" section, renumbered guardrails list, "Adding a filter" note); version bump to 2.13.0.
- Verified on the Pi: `py_compile` clean, `test_track_name_filters.py` passes, `_is_remix_version("SIGN (Remix)")` → True and non-remix tracks kept.

## 2.12.1 — 2026-07-14 (filter gaps: instrumental "Inst." abbreviation + trailing "Live" markers)

Fixed two output-filter gaps in `composer.py` that let unwanted versions into the playlist. Both were the same class of bug: the regexes only recognised markers in a narrow anchored position, so common real-world naming variants slipped past all three guardrail call sites (`_apply_section_guardrails`, `_apply_guardrails`, `_fill_from_fallback`).

**Instrumental — the `Inst.` abbreviation** (reported: **"Psycho - Inst." (izna)** kept appearing despite the instrumental exclusion). `_INSTRUMENTAL_RE` only matched the full word `instrumental` (plus `Instrumental Version` / `Karaoke Version`), so the common Korean-catalogue abbreviation `- Inst.` was never caught.
- The parens/bracket and dash branches now match `(?:instrumental\b|inst\.)`, so `- Inst.`, `– Inst.`, `(Inst.)`, `[Inst.]` are all filtered. The abbreviation branch requires the trailing period (`inst\.`), so it does NOT fire on words like "Instant"/"Instinct"/"Install" ("Instant Crush", "Some Song - Instant Classic" are kept).

**Live — trailing / quote / tour markers** (reported: **XG `NEW DANCE - XG 1st WORLD TOUR "The first HOWL" Live`** in the playlist). `_LIVE_VERSION_RE` only matched `Live` immediately after a dash or paren/bracket, so a trailing "Live" (the dash here is followed by "XG", not "Live") was missed.
- Added branches for `Live at/from/in/on ...` (no dash needed), a trailing `Live` right after a closing quote (`"..." Live`), and `... (World) Tour ... Live` titles. High-precision by design: a bare trailing "Live" with no quote/tour/at-from-in-on context is still kept, so studio songs like **"Long Live"** / **"Live Forever"** are NOT dropped (documented tradeoff — mirrors the instrumental branch's period requirement).

- **`scripts/test_track_name_filters.py`** — added 5 instrumental-abbreviation + 3 trailing-live MUST_BLOCK cases and 4 instrumental + 3 live MUST_KEEP negatives (`Instant Crush`, `Some Song - Instant Classic`, `Long Live`, `Live Forever`, …).
- Docs synced: SKILL.md live + instrumental filter sections; version bump to 2.12.1.
- Verified on the Pi: `py_compile` clean, `test_track_name_filters.py` passes, `_is_instrumental_version("Psycho - Inst.")` → True and `_is_live_version('...HOWL" Live')` → True.

## 2.12.0 — 2026-07-09 (baby-sleep / category exclusions)

Added a category-exclusion layer so tracks played for non-taste reasons (baby-sleep / white-noise music played on repeat to settle a baby) no longer pollute the playlist. Prompted by "Relaxing Baby Sleep Music" (`2xCoYKt0sKxUDnutaEJCFs`), which had scored a full listen (completion 1.0) 4.3 days ago — inside the 7-day window — so it was an active Like and would have become a core favorite and made its artists discovery/wildcard "top artists" (pulling in *more* sleep music).

- **New `src/exclusions.py`** — `is_excluded_track(name, artist_ids)`: a track is excluded if EITHER (a) any of its `artist_ids` is in `config.yaml` → `exclude.artist_ids`, OR (b) its name matches a baby-sleep regex (`baby sleep`, `sleep music`, `sleep/rain/nature/womb sounds`, `white/pink/brown noise`, `lullaby/lullabies`, `nursery rhyme`, `calming/soothing music/sounds`). Anchored to sleep-*music* phrasing so bare titles ("Sleep", "Ocean", "Rain", "Purple Rain") are NOT caught; standalone "Lullaby"/"Nursery Rhyme" pop titles *are* (accepted tradeoff — the artist-id list is the precise lever).
- **Applied BEFORE scoring** in `scorer.compute_affinity()` (excluded events are dropped, so they earn no affinity and never contaminate artist-graph discovery/wildcard) **and** as a composer guardrail in `_apply_section_guardrails`, `_apply_guardrails`, and `_fill_from_fallback._try_add` (defense in depth). Unlike the live/instrumental filters, which are output-only.
- **`config.yaml`** — new top-level `exclude.artist_ids` block seeded with the 3 artists on the seed track. Read at import by `exclusions.py` (fresh process per cron run = live).
- **`scripts/test_track_name_filters.py`** — now also loads `is_baby_sleep_name` from `exclusions.py`; added 13 baby-sleep MUST_BLOCK cases + 7 specificity MUST_KEEP cases. **44/44 pass.**
- Verified on the Pi: compiles clean, config loads all 3 IDs, the seed track drops out of `compute_affinity()` (55 other tracks unaffected), name/artist/normal-track cases all correct. **Live refresh run 2026-07-09** (via `spotify_refresh_wrapper.py`, PKCE token force-refreshed): status ok, 40 tracks written, 0 baby-sleep/excluded tracks in the resulting playlist (K-pop group "Baby DONT Cry" correctly *kept* — name filter requires `baby sleep`, not bare `baby`).
- Docs synced: SKILL.md (architecture tree + new "Category exclusions" section + guardrails list + config block + "Adding a filter" note), CLAUDE.md (architecture tree + exclusions section). Also updated `references/feb-2026-api-audit.md` + `references/troubleshooting.md` with the `GET /v1/playlists/{id}/items` response-shape change (track under `item`, not `track`) hit while verifying the live playlist.
- To exclude more sleep artists later: add their `open.spotify.com/artist/<ID>` to `config.yaml` → `exclude.artist_ids`.

## 2.11.0 — 2026-07-06 (playlist size doubled)

Doubled the playlist from 20 → **40 tracks**, keeping the core/discovery/wildcard ratio (12:6:2 → **24:12:4**, both 6:3:1). Config-only change (`composer.py` reads `playlist.target_size` + `composition.*` live): `config.yaml` → `target_size: 40`, `core: 24`, `discovery: 12`, `wildcard: 4`. Per-artist caps unchanged (3/section, 4/combined). Verified: 5 dry-runs all compose exactly 40, max 4/real-artist, discovery excludes heard, no crash — section fill leans more on fallback (discovery/wildcard still underfill their now-larger targets due to artist concentration + album rate-limits, same documented behavior as at 20). Docs synced (SKILL.md description/composition/config/guardrails/quick-start, CLAUDE.md, `references/troubleshooting.md`, composer/refresher docstrings made count-agnostic where possible). Dated reference docs (spec-compliance-audit, discovery-underfill, post-full-run, wildcard-rate-limit) retain their 20-era numbers as point-in-time records.

## 2.10.0 — 2026-07-04 (hermes-skill-tuneup: bug hunt → gate → docs)

Full tune-up (bug-hunt → perf → delivery → terminal gate → docs). Every finding was validated on live data / a cloned DB; the terminal fan-out re-reviewed the cumulative diff and caught a regression in one of these own fixes (see the `_fill_from_fallback` note).

**Code fixes:**
- **`composer._is_live_version`/`_is_instrumental_version` crashed on a None track name** (HIGH, latent): `re.search(None)` → `TypeError` aborts the whole compose. Spotify `"name": null` survives `.get("name","")` as `None`, and a NULL `track_cache.name` reaches the predicates via `_fetch_track_metadata`. Fixed with `search(name or "")`.
- **Discovery didn't filter already-heard tracks** although SKILL.md + the docstring said it did, and `_get_heard_track_ids()` was dead code. Wired it in (`seen_ids |= _get_heard_track_ids()`) so discovery surfaces only tracks not in `play_events` (user decision: keep discovery genuinely new; fallback covers any underfill). Verified 0 discovery/heard overlap on live data.
- **Fallback fill bypassed the combined per-artist cap** → one artist could reach 5–6 of 20 (violating the documented "max 4 combined"). `_fill_from_fallback` now enforces `MAX_PER_ARTIST_COMBINED` via a `_try_add` helper seeded from the pool.
  - **Gate-caught regression in that fix:** keying the cap on `artist_ids[0]` collapsed every empty-`artist_ids` track into one `""` bucket, capping distinct unknown-artist tracks at 4 → undershoot. Fixed so empty-artist tracks bypass the cap (never bucketed) at all three sites (`_apply_section_guardrails`, `_apply_guardrails`, `_fill_from_fallback`). Latent (0 empty-artist tracks in cache today) but reachable via local-file/regional entries.
- **`client._do_request` never honored Retry-After**: the curl write-out used the invalid `%{header{retry-after}}` (double-brace); the Pi's curl 7.88.1 errors `unknown --write-out variable` and emits a literal `}`, so `retry_after` was always None (blind exponential backoff only). Fixed to `%header{retry-after}` (verified on the Pi's curl). **Supersedes the 2.7.0 note that claimed the double-brace form worked on curl 7.88** — it never did.
- **`test_track_name_filters.py` couldn't find `composer.py`**: path was `parents[1]/src/composer.py` but the code moved under `spotify-refresh/src/`, so the mandated regex regression suite crashed with FileNotFoundError. Fixed the path; suite passes 24/24.
- **`refresher.py` config parse** used `.get().get().get()` which `AttributeError`s if a `scoring:`/`snapshot:` block is present-but-null in config.yaml. Mirrored the safe `(x or {})` idiom used by scorer/composer.
- **`db.log_run` now returns the inserted `run_id`** (`cur.lastrowid`); `refresher` uses it directly instead of re-reading via `get_last_runs` (which ordered by `started_at` and could attach the affinity snapshot to the wrong run on a tie / out-of-order insert).
- **`auth._curl_post`** had no request timeout and `_do_refresh` did `resp["access_token"]` (KeyError on a malformed 200). Added `--max-time/--connect-timeout` + `subprocess timeout=35` + `TimeoutExpired` handling and `.get("access_token")` with a guard. (Dead path today — no `oauth_token` row — but hardened.)
- Removed the dead `poller.new_ids` local.

**Considered and NOT changed (with reason):**
- **NULL `ms_played` "stranding" under burst listening** — refuted: 50 events span a median 36.8h vs the 1–3h poll cadence (peak burst 34 events/3h), so an event can't roll out of the window before re-poll; and the proposed back-fill would *inflate* affinity (promote unknown-gap events to full-listen via the >6h clamp). Left as-is.
- `artist_names`/`popularity` are always empty for core/fallback tracks (track_cache dropped those columns Nov 2024) — cosmetic only (no production consumer; the Discord message is just a link). Left as-is.

**Perf:** refresh avg 9s / max 54s (well under the 120s scheduler watchdog); poll <1s. No bottleneck to optimize; the Retry-After fix is the meaningful improvement for the rate-limited path.

**Delivery:** already reliable — `no_agent` stdout→Discord, honest success/failure lines, traceback→stderr, no `hermes send`/`MEDIA:`. Channel reachability confirmed read-only. No change.

**Docs synced to shipped code:** "top 20 albums"→"top 10" (SKILL.md + composer docstring + dated notes in `references/feb-2026-api-audit.md` & `post-full-run-findings.md`); discovery-filters-heard now accurate; guardrail list notes the fallback cap + unknown-artist handling; "Adding a filter" step points at `_try_add`. Token: relocated the Troubleshooting section (~1160 tok) to `references/troubleshooting.md`. `references/feb-2026-api-audit.md` User field-removal table corrected (country/email/product are still returned by `/v1/me`).

## 2.8.1 — 2026-06-07 (documentation reconciliation)

Swept the whole doc set for doc↔code drift after the 2.6–2.8 changes and brought the docs in line with current behavior:

- **CLAUDE.md** (architecture): `recently-played?limit=50` (was "20 events/call"); last event is `ms_played=NULL`/pending corrected via the self-correcting upsert (was "assumed full play, self-corrected next poll"); refresh does one atomic `PUT` (was "clears and replaces via PUT/POST"); scoring is a **7-day window × 7-day half-life** read live from config (was "30-day × 14-day"); noted the 12/6/2 counts + caps + wildcard window are read live from `config.yaml`.
- **SKILL.md**: updated the `events_stored` troubleshooting note (`INSERT OR IGNORE`→self-correcting upsert; reports up to 50, not 20) and the polling-data-loss pointer blurb (`limit=50` now).
- **references/** (historical session docs — added dated correction notes at top, body preserved): `artist-avoidance-decay.md` (Option 1 applied; window/half-life now 7/7, config-driven); `polling-data-loss.md` (`limit=50` now; upsert is self-correcting); `spec-impl-findings.md` (its "self-correction logic" and "current cron set" sections are stale → point to SKILL.md/CLAUDE.md).
- Left as-is (verified still accurate): `wildcard-rate-limit.md` (30-day window is current), `skip-detection-limitation.md` (manual blocklist still uses `INSERT OR IGNORE`), `discovery-underfill-diagnosis.md` ("self-corrects" = underfill improving with more data).

## 2.8.0 — 2026-06-07 (scoring fix: ms_played accuracy — A2 + A3)

Fixes the optimistic-full-play bias the reviewer fan-out surfaced (the old `_update_last_event_self_correct` was a no-op, so every poll's last event kept its assumed full-play score forever; ~56% of in-window rows sat at `ms_played == duration_ms`).

- **A2 — pending last event (`poller._infer_ms_played`):** the last event of each poll is now stored with `ms_played = NULL` instead of `duration_ms`. The scorer already skips `NULL` rows, so the most-recent track is simply left unscored until the next poll establishes its real gap — no optimistic inflation. Removed the dead `_update_last_event_self_correct`.
- **A3 — self-correcting upsert (`db.upsert_play_events`):** `INSERT OR IGNORE` → `INSERT … ON CONFLICT(track_id, played_at) DO UPDATE SET ms_played = excluded.ms_played WHERE excluded.ms_played IS NOT NULL AND (play_events.ms_played IS NULL OR excluded.ms_played < play_events.ms_played)`. A later poll's real gap value now fills a pending NULL or lowers a stale optimistic value — never inflates. (SQLite 3.40 on the Pi; UPSERT-with-WHERE supported.)
- **Note:** affinity numbers shift after this change, so `affinity_snapshot` rows aren't directly comparable across it. The fix is forward-looking — pre-existing optimistic rows age out of the 7-day window within a week (and any that reappear in a poll get corrected by A3).

## 2.7.0 — 2026-06-07 (acting on the fan-out's surfaced items)

- **Combined per-artist cap documented as 4.** `MAX_PER_ARTIST_COMBINED=4` is intended (looser than the per-section cap of 3). Aligned SKILL.md guardrails, CLAUDE.md, and config.yaml to say "3 per section, 4 across the combined pool" (code unchanged).
- **`composer` now reads the `playlist:` config block live** (was entirely hardcoded/dead). Wired: `composition.{core,discovery,wildcard}` → section counts, `target_size`, `constraints.max_tracks_per_artist` (per-section), new `constraints.max_tracks_per_artist_combined` (combined), and `constraints.wildcard_release_window_days` (the two hardcoded `30 * 86400 * 1000` wildcard windows). Defaults preserve current behavior (12/6/2, 3/4, 30d). Only the `scoring:` block was live before.
- **`client.py` retry policy hardened** (the fan-out reproduced ~189s of blocking backoff and 5xx-as-HTML never being retried):
  - `_do_request` now captures the real HTTP status code and the `Retry-After` header via curl `-w "%{http_code}"`/`%{header{retry-after}}` (curl 7.88 on the Pi). Returns `(body, http_status, retry_after)`.
  - 429 now honors the `Retry-After` header (capped at 30s) instead of looking for it in the JSON body (where it never was).
  - 5xx responses that return a non-JSON body (HTML error pages → previously status 0 → "fail immediately") and transient timeouts/connection failures are now retried with bounded exponential backoff (capped at 20s/attempt).
  - The success path is unchanged (a body with no `error` envelope is still treated as success).

## 2.6.1 — 2026-06-07 (round-2: independent-reviewer fan-out)

Spawned 3 independent reviewers (control-flow lens, edge-case/data-shape lens, adversarial refuter) against the post-2.6.0 code. They corroborated all eight 2.6.0 fixes as real and correctly applied, and surfaced the following — fixed here:

- **Regression introduced in 2.6.0:** the wrappers' "guarded read" of `~/.hermes/auth.json` fell back to `{}` on any read error, then wrote atomically — so a transiently-unreadable auth.json would be rewritten with *only* the spotify provider, wiping `minimax-oauth` / `credential_pool` / other keys. Now **aborts** the token write on an unreadable auth.json instead of clobbering it.
- **Empty-`uris` could still wipe the playlist:** `refresher` guarded `if not tracks` but then built `uris = [t["uri"] for t in tracks if t.get("uri")]` and PUT unconditionally — an all-empty-uri composition would `PUT {"uris": []}` and clear the playlist (the exact outcome the 2.6.0 atomic-PUT was meant to prevent). Now guards `if not uris`.
- **No request timeout on curl:** `client._do_request`/`_do_request_raw` ran curl with no `--max-time`/`--connect-timeout` and `subprocess.run` with no `timeout=`, so a hung connection wedged the cron indefinitely. Added `--max-time 30 --connect-timeout 10` + `subprocess` `timeout=35` with a `TimeoutExpired` guard.
- **Weekly `incremental_vacuum` fired on the wrong day:** it compared `datetime.now(timezone.utc).weekday()` to `VACUUM_WEEKDAY=6`, but the refresh runs 03:00 SGT = 19:00 UTC the previous day, so Sunday-SGT is Saturday-UTC and the Sunday vacuum never fired. Now computes the weekday in SGT (UTC+8, no DST). (Currently moot — `freelist_count=0` — but a latent correctness/doc bug.)

Surfaced for a decision, not changed here (see the tune-up notes): `MAX_PER_ARTIST_COMBINED=4` vs the docs/config "3"; the entire `playlist:` config block is dead (composer hardcodes the counts/caps/30-day window — only `scoring:` is read live); `_update_last_event_self_correct` is effectively a no-op so the documented cross-poll ms_played correction largely never happens (upward affinity bias); and `client.py`'s retry policy still reads `Retry-After` from the JSON body (it's an HTTP header) and doesn't retry 5xx responses that return non-JSON bodies.

## 2.6.0 — 2026-06-07 (tune-up: bug hunt + reliability + token slim)

Four-pass tune-up (validated against the live `spotify.db`; latent crashes reproduced deterministically).

**Bug fixes (`src/`)**
- **`composer._pick_discoveries` crashed on a non-empty blocklist.** It built `seen_ids` with `{t["track_id"] for t in blocked}`, but `blocked` is a `set` of track-id *strings* → `TypeError: string indices must be integers`. Never fired in production only because the blocklist has always been empty (0/39 refreshes ever blocked a track) — but `skip-detection-limitation.md` instructs the user to manually `INSERT INTO blocklist`, which would have crashed the very next discovery step. Fixed to `seen_ids |= set(blocked)` (matching the already-correct `_pick_wildcards`).
- **`scorer.compute_affinity` returned a bare `{}` on an empty scoring window** instead of the `(scores, now_ms)` tuple every caller unpacks → `ValueError: not enough values to unpack`. Returns `{}, now_ms` now. (Latent: triggers after ≥`window_days` with no events.)
- **`db.get_latest_snapshot` selected `track_cache.artist_names`, a column that does not exist** → `OperationalError`. The query now selects only stored columns; `artist_names` is reported empty (artist endpoints were deprecated Nov 2024 and the names were never persisted). No live caller, so previously dormant.
- **`refresher` clear-then-add was non-atomic** — a failed add after a successful clear left the playlist empty, with no rollback (despite the docstring claiming "snapshot rollback"). Replaced with a single atomic `PUT /playlists/{id}/items` (`_replace_tracks`); a failure now leaves the previous playlist intact. Removed the now-dead `_clear_playlist`/`_add_tracks` helpers and the false docstring claim.
- **`poller` never requested `?limit=50`** so `recently-played` returned the default 20, despite the unused `MAX_EVENTS_PER_POLL = 50`. Now sends `limit=50` — directly relieves the 16:00–19:00 burst overflow that `polling-data-loss.md` worked around with extra poll times.

**Doc/config↔code drift**
- **scorer hardcoded `WINDOW_DAYS=7` / `HALF_LIFE=7` and ignored `config.yaml`** while `config.yaml`, the SKILL.md scoring model, and the artist-avoidance section all said 30/14. scorer now reads `config.yaml` → `scoring` (defaults preserve the live 7/7); `config.yaml` and SKILL.md reconciled to 7/7 and marked as the live source. Removed the brittle dated `# CrocoBeat (last May17) excluded` comment.
- Fixed two SKILL.md dry-run examples that called `scores = scorer.compute_affinity()` without unpacking the tuple (would `AttributeError`).
- `config.yaml`: `max_tracks_per_artist` 2→3 (matched code), dropped deprecated `min_popularity`, schedule aligned to the real cron.

**Delivery reliability (cron wrappers in `~/.hermes/scripts/`)**
- **`spotify_refresh_wrapper.py` printed the "Hot Hits Updated!" success banner unconditionally** (the `result` was discarded) — a `{"status":"error"}` return still posted a false success. Now checks `result["status"]`, posts an honest success/failure line, and a top-level guard logs any traceback to stderr and posts a clean failure line instead of letting a raw traceback become the message body.
- Both wrappers now write the shared `~/.hermes/auth.json` **atomically** (temp + `fsync` + `os.replace`) with a guarded read, so a crash mid-write can't corrupt the auth file every provider depends on.

**Token efficiency**
- Moved this changelog out of the SKILL.md frontmatter into this file (hot-path tokens).
- Pointer-replaced the duplicated "Feb 2026 Spotify API Changes" deprecation tables (they live in full in `references/feb-2026-api-audit.md`).

---

## 2.5.5 — Polling schedule updated (May 2026 session)
- Added 17:00 SGT poll to `afdb0b06e11a` (was: 4,7,10,13,16,19,22,23 → now: 4,7,10,13,16,17,19,22,23)
- Shifted overnight poll `dfadaf5e1833` from 1am → 2am SGT
- Corrected polling analysis: "100% hit cap" was measuring API responses, not actual new events. `events_fetched` = 20 on every poll (API always returns up to 20). `events_stored` = len(inferred) (pre-dedup count, not actual INSERT rows). True signal: query `play_events` table directly. 103 new events across 64 polls in 7 days (~1.6/poll avg). 16:00–19:00 window = 47.6% of all holiday listening (49/103 events) — overflowing the 20-event cap. 17:00 poll added to address. See `references/polling-data-loss.md` for full corrected analysis.

## 2.5.4
`MAX_PER_ARTIST=2` → `MAX_PER_ARTIST=3` in `composer.py`. 12-core unique artists and high-affinity artists (BABYMONSTER, IU) appearing across all 3 sections, the old cap of 2 was cutting 7 tracks from the combined 20-track pool (→ 13 after guardrail). MAX_PER_ARTIST=3 now keeps 16–20 tracks before fallback. Also added retry-with-backoff to `_fetch_album_tracks` (3 retries: 0.5s, 1s, 2s) for rate-limit resilience. Fixed artist name extraction in `_fetch_artist_graph` to derive name from album artist list instead of calling `/v1/artists/{id}`. Added `references/wildcard-rate-limit.md` documenting album API rate limits (~50 req/min). Not a blocking issue.

## 2.5.3
Added guardrail over-removal diagnosis. Added 527/528/529 as Spotify server-side errors.

## 2.5.2
Fixed `limit=20` → `limit=10` in `_fetch_artist_top_tracks` and `_fetch_artist_graph` (Spotify API max is 10; >10 returns 400). Fixed stale cache entries not re-fetched (`if artist_data and (top_tracks or albums)`). Fixed `top_track_ids` typo in wildcard path (`NameError`). Added full bug diagnosis to `references/impl-notes.md`.

## 2.5.0
Implemented `_fetch_artist_top_tracks()` (Feb 2026 algorithm) replacing the removed `/v1/artists/{id}/top-tracks`. Top 20 albums → newest 5 → artist-filtered tracks per album → dedup + 60s duration filter → up to 10 tracks. Updated `_fetch_artist_graph()` return type (now full track dicts, not string IDs). Cache layer in `_pick_discoveries()` updated to handle full dict storage. SKILL.md composition table updated.

## 2.3.0
Fixed cron job failures: "Script not found" caused by inline bash in cron `script` field. PKCE token refresh via auth.py `_do_refresh()` fails with `400 invalid_client`. Added `references/cron-wrappers.md` and wrapper scripts `spotify_refresh_wrapper.py` / `spotify_poll_wrapper.py` that use `resolve_spotify_runtime_credentials(force_refresh=True)`. Updated all 4 Spotify cron jobs to use the correct wrapper scripts.

## 2.1.0
Added affinity_snapshot table (§7a): write at end of each refresh, 90-day TTL prune, weekly incremental_vacuum.

## 2.0.2
Moved source into skill folder (now included in Hermes backups). Added `age_days` variable to recency formula docs.

## 2.0.1
Fixed `_fill_from_fallback`: now fills from liked tracks first, then neutral (spec §6.1 guardrail 4).

## 2.0.0
Full spec-compliant rewrite: SQLite, 2 entrypoints, daily 03:00 refresh, commute-aware polling.
