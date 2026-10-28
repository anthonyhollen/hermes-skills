# Changelog — morning-brief

Per-tune-up provenance. Older per-bug history lives in `references/pitfalls-log.md`
(31 entries relocated from SKILL.md) and `references/session-fixes.md`.

## 1.13.1 — 2026-10-15 (Hermes v0.22.0 broke summarize.py's import of Hermes' config)

**The 10-15 07:30 brief failed at summarize.py:** `Could not import Hermes config from
~/.hermes/hermes-agent (No module named 'ruamel')`.
- **Cause:** summarize.py imports `hermes_cli.config` / `runtime_provider` to use Hermes'
  configured model (deepseek-flash). Since the Hermes v0.22.0 update (10-14 20:39), that code
  needs Hermes' own dependencies (ruamel.yaml, …). The wrapper ran every step under
  `/usr/bin/python3`, which lacks them. The fetch had finished, and 10-14's run predated the
  update.
- **Fix (wrapper only):** `summarize.py` now runs under the first interpreter that can import
  requests + Hermes' config + runtime_provider (`_summarize_interp`, a 15 s probe each).
  - **Order:** the wrapper's own interpreter (the scheduler runs no_agent scripts on Hermes'
    dependency venv), then the install's current dependency venv (`installs/*/facts.json`;
    its path changes when Hermes rebuilds it), then the gateway venv, then `/usr/bin/python3`.
  - **Fetch / send stay on `/usr/bin/python3`** (feedparser, bs4).
  - The step marker shows the interpreter when it differs.
- **Verified:** on the Pi, both Hermes interpreters import summarize and resolve the model
  with no LLM call made.
- **Tests:** the suite has 4 new wrapper checks:
  - the first interpreter that imports Hermes' config wins;
  - only summarize moves;
  - none importable → INTERP;
  - the candidate list.

## 1.13.0 — 2026-10-05 (duplicates past the lede, chained cluster members)

The Oct 5 brief ran PC Gamer's "An OpenAI model was caught trying to cheat at StarCraft…"
as must-read and The Verge's "An AI couldn't beat humans at StarCraft, so it decided to
cheat" as maybe. Replaying `same_story` on the pair: title 0.26 (< 0.40); 3 shared
distinctive title words, but lede content 0.03 (< 0.08). PC Gamer opens with commentary
("Commercial large-language models… have repeatedly been accused of stealing the work of
humans"), and the StarSkirmish / GPT-6 Astra / Stardust story starts after the 300-char
window. Oct 1 ("Super Intelligence": Slashdot + PC Gamer, both maybe) looks like the same
failure: the titles share 2 distinctive words and the PC Gamer lede is jokes, but the bodies
are gone, so the fix can't be checked on it. The last 9 briefs also had two next-day
repeats, a separate gap:

- **Deep same-story path** (`article_id`, `deep@NN%`). Across outlets, titles sharing
  ≥ 2 distinctive words also match when the first `DEEP_CONTENT_CHARS` (1,000) of both
  bodies overlap ≥ `DEEP_CONTENT_THRESHOLD` (0.16), after per-source boilerplate stripping,
  and both were published ≤ `DEEP_MAX_PUB_GAP_HOURS` (36) apart. The gap check applies
  only when both dates are known; ledger entries have none, and a time with no timezone
  reads as UTC.
  - The sequel veto still runs first, and nothing changes within one outlet.
  - New fields: `_story_deep` on articles and twins, `story_deep` in ledger entries,
    feature elements 8 (deep words) and 9 (publication time), and `with_slots()`.
    `with_slots()` replaces the `feat[:7] + (slots,)` slices in `_hold_grade` and
    `feedback.py`, which would have dropped the new elements.
  - In the hold-back a deep match grades like the weak content path: one run at most,
    or an echo when the titles overlap < 0.20.
  - Ratings manifests keep only the lede, so rating follow-ups are unchanged.
- **Chained cluster members are recorded** (`fetch_rss.deduplicate` `_chained` →
  `send_brief.save_seen_articles`). On Oct 3, 9to5Mac's "tightening macOS privacy controls"
  sat in the "Full Disk Access" cluster via TechCrunch's copy. It wasn't a direct twin, so
  it wasn't recorded. On Oct 4 the hold-back matched it against the recorded twins
  (title@41%, content@20%), but it was published before they were recorded, so it graded
  as an echo and ran. Now such members are recorded by identity only: `"kind": "chained"`,
  `CHAINED_TTL_DAYS` 3, no story words, never in the hold-back set. Each step has to be
  solid:
  - **The linking twin:** not an aggregator item, round-up or promo, and both hops are
    `firm_story_f` matches: no 8–12% lede band, no deep path.
  - **The member:** not an aggregator item or a round-up. It must not be one the
    predicate separates from the rep by rule, i.e. from the same outlet or a sequel of it.
  - **The rep:** nothing is chained under a round-up.
  - **Why:** the review fan-out found a wrong record behind each loophole. On the Oct 5
    fetch, Techmeme summaries chained MacBook OLED rumours into the Ternus cluster, and
    content@10% links chained "zombies in GTA 5" and a Satisfactory piece. 4 of 5 chained
    members were the rep's own outlet, and a "Sonnet 5.5 review" chained via an
    Opus+Sonnet launch story.
  - With the guards, the Oct 5 fetch chains exactly one member, the 9to5Mac case, both in
    the interest-matched pool and in a pool of every fresh article.
- **Calibration** (full fetch captured 2026-10-05 11:47, 797 fresh usable articles):
  - `_story` is byte-identical to 1.12, and no existing path changed on any of the 47,733
    pairs that share a title word.
  - The deep path adds 5 matches at 0.171–0.206, all the same story, published 4.5–21.4 h
    apart: StarCraft ×2, Capcom's RE Engine AI plans, and the Shield TV Pro price rise
    (Ars's 2 feeds × TechPowerUp).
  - **The margin is thin.** Just below sit follow-ups: Intel Nova Lake 0.149 (a second
    leak 50 h later, now also outside the gap) and Deadlock 0.147 (17.5 h). The threshold
    sits midway between them and the weakest true pair. Some true pairs also sit below
    (Arc Raiders 0.139, "Xbox is not for sale" 0.128), so this cutoff misses some
    duplicates rather than risk follow-ups. On the interest-matched pool, every cutoff
    from 0.10 to 0.16 gives the same picks; at 0.10 one wrong merge appears (MIT Tech
    Review's OpenAI interview with Ars's hack story, 0.102).
  - One shared title word is not enough at any depth (0.23 unrelated).
  - Replaying `fetch_rss.main` on the capture with the pre-run ledger and real ratings: 1.12
    → two StarCraft must-reads (three with an injected "kicked out of the tournament"
    follow-up); 1.13 → one (3 outlets), other picks unchanged.
- **Known limits:**
  - A follow-up that recaps the original can still merge into its cluster: directly within
    36 h, or further apart through a third article (single linkage; the gap is per pair).
    It's hidden that day. If it matches the shown copy directly, it is recorded as a twin
    for the week (adversarial fixture: a near-copy headline about the StarCraft ban).
    Otherwise it isn't recorded and shows the next day.
  - A member linked only through a deep twin is never chained (deep links aren't firm),
    so such a member can still repeat the next day.
  - A sequel of a different length ("…launches Claude Sonnet 5.5 for free users" against
    an "Opus 5.5" rep) can still be chained through a launch story that names both. The
    sequel veto only handles headlines of the same length.
  - MacRumors "Top Stories:" round-ups aren't caught by `_ROUNDUP_RE`.
  - The Gemini 4 Argon repeat (Oct 1 → 2, Slashdot via the Techmeme twin) is not covered,
    and its cause was inferred, not replayed.
- **Hardening:** `send_brief._dict_items` reads `_twins` / `_chained`. A malformed value in
  articles.json used to raise after delivery, before the ledger write, so the whole brief
  was re-sent the next run.
- **Rollback note:** the 1.12 reader treats a `kind: chained` entry (ttl but no `story`) as a
  pre-1.10 twin: 7-day identity suppression, and a title-only hold-back entry. The 1.12
  writer also rewrites its `ttl_days` to 7 on disk, so a later 1.13 keeps it 7 days. Both
  effects age out within a week. `story_deep` is ignored.
- The test suite now removes its temp HOME on exit (each run left a `/tmp/mb-test-*` dir on
  the Pi).

Suite 235 checks (42 new; the feed_doctor probe skips where a local server can't bind).
The new same-story checks fail against 1.12, and the run then crashes on the missing
`with_slots`. A mutation that uses `same_story_f` instead of `firm_story_f` for the chain
hops fails the suite.

Reviewed by three independent agents (ledger contract, edge cases and data shapes,
adversarial refuter), plus a second lap on the fixes. Their findings are the chain guards,
the gap limit, the corrected calibration claims, the hardening and the wiring tests
above.

## 1.12.0 — 2026-09-28 (brief quality)

The reader found the Sep 28 brief "noticeably worse": must-read #1 said "Summary
unavailable", must-read #2 was a 6-day-old CLI release note ("llm 0.36", score 6.7), maybe
held a $900-off MacBook deal, a Verge newsletter issue whose summary read "Hi, friends!
Welcome to Installer No.", and two keyword-heavy Reddit posts. Diagnosed article by article
(old 1.9.2 / 1.10 / live replays of the same fetch):

- **"Summary unavailable" on a good summary** — a May-era echo check discarded any summary
  whose first 100 chars matched the lede; on a short lede a correct summary opens that way.
  Now `send_brief._is_content_copy`: only a ≥90% verbatim copy is replaced.
- **Empty LLM summaries** — deepseek-flash began spending ~1,000-1,550 hidden reasoning
  tokens (~Sep 27); at `max_tokens` 1,500 it returned `finish_reason=length` with 0 visible
  chars (probed on the Pi). `MAX_TOKENS` 4,000 (unused budget isn't billed).
- **Fallback extract** used a naive splitter (cut at "No.") and kept greetings — now the
  abbreviation-safe `_sentences`, greeting openers skipped.
- **Must-read had no quality bar** — any ≥3-keyword article, always 3 slots; 1.10's C-tier
  rule freed a slot for the next eligible item, a 6-day-old release note. User's choices:
  must-read must be ≤48 h old with base score ≥10 (fewer than 3 is fine); promos excluded;
  newsletters/roundups/guides ×0.5; keyword stuffing capped (3); Reddit 1 slot.
- **Editorial review** (independent) showed the brief was 8/11 AI across two days while
  3-outlet Apple stories scored below Reddit posts: topic FAMILIES (synonyms count once), an
  outlet bonus (+25%/extra outlet, max +50%), and a maybe-section area-diversity rule (≤3
  stories of one area when another area's story scores ≥7).
- **Hold-back** no longer holds on the weakest match (content ≤12% AND title <20%) — it had
  hidden a distinct score-18 story ("OpenAI agents tried to 'bruteforce' a UN website").

Replayed on the Sep 28 and Sep 26 fetches (clock frozen at fetch time): Sep 28 → 1 must-read
(the security-probes story) + UN bruteforce, Muse quote, GPT-6 Astra robot, Apple $5.7B
verdict (3 outlets), iPad mini leak; Sep 26 → 3 corroborated must-reads; the 👎-rated item
drops out and the 👍-rated one rises. Terminal gate: 3 laps (lap 2: the area rule could
leave maybe short and four pieces were untested; lap 3: a second fill pass was still needed).
Suite 193 checks, the new pieces mutation-verified.

## 1.11.1 — 2026-09-26 (ratings: 👍 / 👎 only)

User's call: only 👍 (like) and 👎 (dislike) rate; no reaction means neutral. 😐 is no longer
a rating emoji (it is ignored like any other). An unreacted article is NOT stored as a 0 —
it adds no evidence — so a day the brief goes unread doesn't dilute what was learned.
👍 + 👎 on one message now cancel (unrated) instead of reading as 0. `rate` takes {-1,1};
footer hint is "rate with 👍 👎". No ratings existed yet (the first rateable brief is 09-27).

## 1.11.0 — 2026-09-26 (reader ratings)

The reader wanted to rate the recommended articles −1 / 0 / +1 so disliked ones show up less
and liked ones more. User's choices: reactions on the brief (not a Discord thread per rating),
learn from outlet + matched topics + title words + story follow-ups, moderate strength,
free-text reasons stored only.

- **New `scripts/feedback.py`.** 👍 / 😐 / 👎 on an article message → a rating. Each run's
  fetch step re-reads the reactions on the last 7 days of briefs (one `GET messages?after=`
  page per ~100 messages; runs on a thread beside the feed fetch, own 20 s budget) and
  reconciles `feedback.json` — change or remove a reaction and the rating follows. The
  bot's own reactions don't count; 👍 + 👎 together read as 0; a failed or partial read never
  deletes a rating. CLI: `show`, `list`, `sync`, `rate` (manual, with `--note`, wins over a
  reaction), `unrate`.
- **Ranking.** Affinity per outlet / topic / distinctive title word = Σ r·w / (2 + Σ w) with
  a 45-day half-life (a rating's title words share one unit of weight), scaled by overall
  evidence N/(N+3), plus ±0.4·w for a same-story follow-up of a rated article (≤ 14 days);
  `_score ×= clamp(exp(…), 0.5, 1.8)` in `match_articles`, before clustering. Below ×0.7 an
  article can't be must-read (it can still be a maybe). Calibrated so one rating is a nudge
  (one 👎 on a Verge/LLM story → ×0.89 on the next one; 3 → ×0.66; 5 → ×0.54) — the first
  cut let one rating's correlated features stack to the floor (independent review). No
  ratings → no change at all (the pass is skipped). `[PREF]` lines explain each adjusted pick.
- **Brief layout.** Maybe-relevant is now one article per message (was 2 per message) so a
  reaction names one article: ~10 POSTs instead of ~7, inside the send budget. Footer gains
  "rate with 👍 😐 👎".
- **Manifest.** `send_messages` writes `briefs/<date>.json` (message id, section, title, url,
  outlet, topics, title words, story words) for each delivered article message — after the
  ledger, best-effort: a failure there costs only that day's ratings. Kept 30 days.
- `match_articles` records `_topics` (matched topic names) on each candidate; `prefs=None`
  keeps the old signature's behaviour. Tests: 40 new checks (161 total).

## 1.10.0 — 2026-09-26 (hermes-skill-tuneup: second tune-up)

Live copy confirmed unchanged since 1.9.2 (no curator drift; the curator is blocked from
this skill). Green baseline: 12 on-time scheduled runs since 1.9.0 (all delivered), the
dry-run fingerprint, and a captured full fetch (2,427 articles) replayed offline. Found by
reading the 50 archived briefs against the user's complaint history (duplicates; "why
wasn't breaking news X in the brief").

### Selection & dedup (the user-visible fixes)
- **Short briefs.** send_brief's dedup removed twins from an already-capped 3 + 5 and
  nothing refilled them: 4 of 12 briefs went out short (Sep 23: 5 of 8). Selection now walks
  the whole ranked pool and skips twins, so slots stay filled; send_brief's pass is a backstop.
- **The 12% content dedup was ~50% wrong** on a full pool (same-outlet boilerplate,
  undecoded `&nbsp;`/`&ldquo;`, generic launch vocabulary). Replaced by one shared predicate,
  `article_id.same_story`: entity decoding, per-source boilerplate stripping, plural folding,
  a distinctive-shared-title-word gate, a strict same-outlet rule, aggregator handling, a
  syndicated-lede rule, and a sequel veto ("Opus 5.5" ≠ "Sonnet 5.5", "iOS 27.0.1" ≠ "27.0.2").
- **Multi-outlet stories now corroborate.** The fetch-side dedup was title-only, so
  `_num_sources ≥ 2` held for 1 of 175 candidates and the ≥2-outlets route to must-read was
  dead — the Sep 26 OpenAI/Australia breach sat as three separate items below the cut. It now
  clusters with the same predicate (blocked union-find; corroboration only from direct,
  non-promotional matches) and reaches must-read with 2 outlets.
- **C-tier (r/LocalLLaMA) needs corroboration for must-read** (it took 2 of 3 slots on Sep 26;
  feeds.py always said it couldn't). User's choice.
- **Deterministic ranking** — ties were broken by feed response order.
- **Hold-back of stories already shown** (user's choice: ~3 days → implemented as "while
  eligible" for re-tellings): a re-telling (same url, headline > 55% alike, or lede ≥ 35%)
  of a story in the ledger is held and itself recorded; a weaker match (often a real
  follow-up recapping its background) is held one run only. Meta's Muse agent had been in 8
  briefs in 9 days.
- Twins: cluster twins recorded for the full 7 days (a 2- or 3-day twin simply reappeared as
  "new" on day 3/4); pre-1.10 twin entries read as 7 days.
- Maybe-section cap is per outlet (The Verge + The Verge AI share 2 slots).

### Reliability
- Discord `nonce` + `enforce_nonce` per message: a retried POST that Discord had accepted
  can no longer double-post.
- Empty day: a failed header/placeholder now exits 1 (alert) instead of 0.
- Wrapper: single-run `flock` (a real run waits 30 s, then alerts; a dry run exits), lock fd
  passed to the steps; each step's timeout capped by what's left of the 600 s watchdog minus
  a 20 s reserve for the failure alert.
- `fetch_rss --dry-run` (and the wrapper's) no longer bumps `feed_health.json` counters.
- `Accept-Encoding: gzip, deflate` pinned (a br/zstd body would have parsed to 0 entries).
- feed_doctor: default mode covers zero-yield feeds too (the brief told the user to run it
  and it said "all healthy"); probes the way the pipeline fetches (longest content element,
  bounded, size-capped); guarded `load_health`.
- summarize: trims a clause where the model narrates its input ("…though the provided
  excerpt did not include a launch date", Sep 26 PetaPixel must-read) — narrow on purpose;
  no sleep after the last 429 attempt.
- Log line "N seen articles" counted identity hashes (~3 per article); now says so.

### Docs
- SKILL.md rewritten as an operator playbook (~1,590 tokens, from ~4,880): a "why wasn't X
  in the brief" checklist in pipeline order, duplicate / feed / topic / format / model
  recipes. Reference detail moved to CLAUDE.md (which gained the env vars, budgets, dedup,
  ledger contract).
- Corrected: "this Pi has no RTC" (it has `/dev/rtc0` + NTP; the ledger floor stays as
  cheap insurance); the content-dedup analysis's "zero false positives" (dated correction);
  the provider checklist's nonexistent `.minimax.bak` rollback; dead identifiers flagged in
  historical references.
- New `scripts/test_morning_brief.py`: 126 offline checks (temp HOME), many mutation-verified.

### Method notes
- The terminal gate ran **10 laps**. Laps 1–9 each found at least one MED+ problem, most of
  them introduced by the previous lap's fix to the multi-day hold-back (a held follow-up
  recorded as a 7-day twin; echo de-preference losing follow-ups; body-less sequel flags).
  A 10-day simulation harness with injected repeats/follow-ups (built by the refuters) was
  what found them; the fixed thresholds were calibrated on a captured real fetch.
- Known limits (accepted): a follow-up can be delayed a day; an unrecognised repeat can
  still out-score a development in its cluster; different-length sequels and alias headline
  swaps aren't distinguished.

## 1.9.2 — 2026-09-15 — drop 5 zero-yield feeds (38 → 33)

Removed **Hugging Face Blog, CNET, Engadget, BBC World and Hacker News** — every one fetched
successfully and contributed **zero** articles, because their RSS bodies fall under the
200-char paywall guard. Surfaced by the `consecutive_zero_yield` tracking added in 1.9.0 and
confirmed feed-by-feed with `feed_doctor.py --name`, which found no drop-in replacement for any
of them (per-feed probe results in `references/feed-history.md`).

Worst offender: Hugging Face Blog, A-tier, fetching 862 entries / ~254 KB every run — 26% of
everything fetched — with `content`, `summary` and `description` all empty at source. It looked
like a strong AI source while supplying nothing.

Net effect: ~950 fetched entries/run removed with no loss of brief content (they produced none),
and the 🩺 feed-health line should go quiet. Tier mix 13 A / 16 B / 4 C.

**Known coverage gap, by decision:** BBC World was the only `general` feed, so there is now no
world-news source. It was already contributing nothing, but the gap is real — closing it needs
a world-news feed that serves ≥200 chars of body.

## 1.9.1 — 2026-09-15 — fix: unarmed deadline broke feed_doctor's LLM suggestions

Found by actually running `feed_doctor.py` after the 1.9.0 tune-up shipped.

`feed_doctor.py` reuses `summarize.call_model` but never arms `summarize._DEADLINE`, so it
stayed at its `float("inf")` default. 1.9.0's new `bounded_http.request_bounded` passed that
straight into `Thread.join(timeout=inf)`, which raises
`OverflowError: timestamp too large to convert to C _PyTime_t`. Every LLM candidate-generation
call failed with `(LLM call failed: timestamp too large…)`, and because those failures are
caught and reported as a note, the tool still printed a normal-looking report — just with the
*LLM-suggested alternative sources* silently missing, i.e. its main value. Only the
same-domain alternate paths were ever probed, so "no working replacement found" was not a
trustworthy answer.

Fix: `bounded_http._wait_for()` guarantees a finite, positive wait — it falls back to the
request's own `timeout=` plus a margin (capped by `_JOIN_CEILING`) when the deadline is `inf`
or NaN, and the body-read watchdog `Timer` uses it too (`Timer(inf)` fails the same way, and
would have silently left the read unwatched). Verified: `call_model` with an unarmed deadline
now returns normally, and `feed_doctor --name "Hugging Face Blog"` probed 4 LLM-suggested
candidates where it previously probed none.

**Bug class worth remembering:** extracting shared code that depends on module-level state
breaks every caller that doesn't initialize that state — and an on-demand tool is exactly the
caller a tune-up focused on the cron path forgets to exercise. Run the skill's *other* entry
points before declaring a shared-helper refactor done.

Known, not fixed (pre-existing, unrelated to the refactor): the candidate-suggestion prompt
gets an empty response from deepseek-flash for some feeds (2 of 3 in one run). `call_model`
retries 3× and the tool degrades to same-domain probes only, reporting the failure — so it
fails visibly rather than silently, but the suggestions are missing for those feeds.

## 1.9.0 — 2026-09-15 (hermes-skill-tuneup: full tune-up)

Live copy confirmed: cron `4879434e50ed` (`no_agent`, `deliver: local`) runs
`~/.hermes/scripts/morning_brief_run.py`, which invokes the skill-dir `scripts/` under
`/usr/bin/python3`. Green baseline: `send_brief.py --dry-run` over the frozen
`articles_summarized.json` (fingerprint `3628cd15…`) plus 50 archived trigger-time cron
outputs; pre-change data snapshotted to `.tuneup-baseline-20260914/`. Validated by the real
07:30 scheduled run on 2026-09-15 (`status: ok`, on time, 8 messages delivered).

**Method note:** the bug hunt fanned out 4 blind reviewers (ingest / LLM / delivery /
wrapper+docs), and the terminal gate then ran **four** further rounds — each found real
regressions in the *previous* round's fixes, which is why the fix list below includes
several "fix to the fix" entries. Performance was measured and deliberately not optimized:
the whole pipeline runs ~15 s against a 560 s step budget (~3%); the work went into
*bounding* pathological cases instead. See `references/performance-notes.md`.

### Correctness — recurrence bugs (the ones a reader would actually notice)
- **Send-time-deduped twins were never recorded as seen** (HIGH). `save_seen_articles` got
  only the survivors, so the dropped duplicate stayed unseen while its shown sibling was
  recorded — the dedup pair could not re-form next run and the duplicate was delivered as a
  new story. Observed live: MacRumors' "Here's When iOS 27 Rolls Out in Every Time Zone" was
  dropped as a duplicate and only its twin reached the ledger; 22 of 50 archived runs had
  such drops. They are now recorded with a shorter `ttl_days` (2) so a false dedup pairing
  costs one day rather than a week.
- **Sibling-feed copies escaped the seen check** (HIGH). Identity was `sha256(title|source)`,
  so the same article arriving via a publisher's topic sub-feed had a different hash. Proven
  from the live ledger: an Ars Technica story recorded under both `Ars Technica` and
  `Ars Technica AI`, delivered 24.01 h apart. Identity now matches on normalized URL,
  canonical title **or** the legacy hash (new `scripts/article_id.py`); the legacy hash is
  still written as `hash` so a rollback still reads the ledger.
- **`_num_sources` counted feed names, fabricating corroboration.** The Verge / The Verge AI
  and Ars / Ars AI made one outlet look like two and promoted single-outlet stories via the
  "≥2 sources" path (reproduced on a real article with an identical URL under both feeds).
  Now the registrable domain of the article link, clamped by the distinct feed-name count —
  the gate showed the first attempt (full host) inflated it the *other* way, because Slashdot
  serves one feed across five subdomains.
- **Must-read overflow was discarded instead of demoted.** Ranks 4+ were dropped entirely
  rather than falling back to the maybe pool, producing a visible score inversion: an
  18.0-score / 6-keyword article dropped from the brief while a 12.0 / 2-keyword one showed.
- **Keyword matching was plural-blind.** `\bLLM\b` missed "LLMs", `AI agent` missed "AI
  agents": 115 articles/run scored 0 purely on word form, 16 would have crossed the must-read
  threshold. An optional trailing `s` was added **for topics only** — the gate caught the
  first version widening `block_keywords` too, which newly suppressed on-topic OpenAI posts.

### Delivery / security (send_brief.py)
- **An untrusted feed URL could inject a live masked link.** Only the title was escaped; a
  `>` in the link closed the angle bracket and everything after it became live Markdown
  (`…/a>)** CLICK [FREE BITCOIN](<https://evil>)`). Added `_safe_url` (scheme allow-list +
  percent-encoding + length cap); a title with no usable link now has its bare URLs defanged
  too, since the `<>` wrapper that suppresses unfurling is absent there.
- **A summary could forge a section.** Summaries were the one untrusted field never collapsed
  to one line, so newlines + `### Must-read news` + the U+2800 separator fabricated a whole
  fake must-read entry. `_defang_untrusted` now collapses whitespace and strips the separator;
  `format_headline` strips it too.
- **A 1500-char summary cap did not keep a message under the limit.** Today's longest real
  article has a 217-char title and a 280-char URL — a 507-char headline — so a capped summary
  produced a 2010-char must-read message, on the path with no split branch. The budget is now
  derived from the actual headline length, and measured in **UTF-16 units** (`_dlen`), which
  is how Discord counts (an emoji-dense message of 1,852 chars is 3,652 units).
- **`--dry-run` modelled neither the chunking nor the length limit** — the only safe way to
  validate the script was also the useless one. `build_messages()` is now the single builder
  for both the live send and the dry run.
- **A partial send lost the whole brief.** `post_message` was fatal for every message, so a
  permanent failure or an exhausted budget called `sys.exit(1)` *before* the ledger write:
  messages delivered, ledger unwritten, entire brief re-sent. Each message now carries the
  articles it displays; a failure records exactly what landed and exits non-zero, so an
  interrupted brief re-sends only what never arrived. A footer-only failure no longer fires a
  false "failed" alert or discards the twins.
- Unreachable content-excerpt fallback (the echo detector always replaced it with the
  placeholder); the repeat-collapse regex kept the *repeating junk* and threw away the
  substance; `resp.json()` sat outside the only `try` (a 200 with an HTML body escaped
  `post_message` after delivery); a 3xx was treated as delivered; `dead_feeds_line` could
  raise on a non-numeric counter *after* delivery, and could exceed the message limit.

### Observability (morning_brief_run.py)
- **The cron archive discarded every diagnostic.** The scheduler returns only stdout on a
  successful run, and every operational signal here goes to stderr — `[DOWN] <feed>`,
  "N feed(s) down", `[DEDUP]` drops, `[WARN]` summary failures. 0 of 50 archived runs
  contained a single stderr line, including a day when 4 feeds were down. Child stderr is now
  merged into stdout.
- **The wrapper's own stdout was block-buffered**, so all three `→ step` markers printed
  *after* all output (useless for attribution) and a watchdog kill discarded them entirely.
  Line-buffered now, and children run with `PYTHONUNBUFFERED`.

### Reliability — bounding the pathological cases (new `scripts/bounded_http.py`)
- **A total fetch failure posted an empty brief with no alert.** `fetch_rss` exited 0 with
  `total_scanned: 0`; it now exits non-zero when **no usable article** was fetched (gating on
  raw entries alone let a one-teaser outage through).
- **One bad feed could abort the whole run** — `future.result()` was unguarded and
  `fetch_feed` caught only `requests.RequestException`.
- **A per-socket `timeout=` is not a request budget.** Three separate holes, each found by a
  later gate round after the previous fix claimed to have closed it: (1) a dripping body
  re-arms the read timer forever; (2) checking a deadline *between* chunks never fires,
  because `iter_content(N)` blocks until N bytes arrive (measured 212 s against a 150 s
  budget — past the 200 s step timeout, i.e. SIGKILL before the write); (3) nothing bounded
  the connect/TLS/**header** phase, which sits inside `requests.post` before any watchdog can
  arm (measured 20 s against a 4 s budget). Now owned by one module: the request phase runs on
  a joined daemon thread, and a watchdog calls `socket.shutdown()` to interrupt a blocked
  read. Extracted precisely because the gate found the fix applied in `summarize.py` and
  **not** in `fetch_rss.py`, with the two copies of the helpers already diverging.
- Per-step wall-clock budgets below each wrapper timeout: fetch 100/120, summarize 150/200,
  send 195/240. `fetch_rss`'s 120 s cap was not supported by its own arithmetic (3 attempts ×
  a 30 s read timeout × two waves = a measured 192 s); it now fetches in one wave with a 15 s
  read timeout and a hard budget.
- `summarize.py`: retry set widened to any 5xx (Cloudflare 520–524, Anthropic's 529) after the
  first fix's fixed allow-list silently dropped them; fail fast on 401/403/404; honour
  `Retry-After`; a truncated gzip body no longer raises `EOFError` past every handler (which
  silently downgraded all 8 articles to excerpts); bodies are inflated incrementally under a
  cap (1 MB of wire inflates to ~980 MB); response cap lowered to 200 KB because the
  `<think>`-stripping regex is O(n²) (~95 s on 280 KB).
- **The shared `_FEAT` downgrade raced its own workers.** All 8 summaries issue in one wave,
  so when the winner flipped a flag the other 7 skipped their own re-post and returned `""` —
  **7 of 8 summaries lost** (measured 1/8 vs 8/8). Fixed by comparing against what was
  actually *sent*; a second round then fixed 0/8 when *both* optional params were rejected.
  The feature sniff now reads only the JSON error field, so a 400 that merely echoes the
  request can't disable `thinking` run-wide.
- Atomic writes for `articles.json` and `articles_summarized.json` (they were the outliers);
  the ledger prune keeps a `MIN_LEDGER_ENTRIES` floor because this Pi has no RTC and one
  forward-skewed run wiped the whole dedup window; NaN/future timestamps are normalized on
  both sides so reader and writer always agree about what is expired.

### Feed library / relevance (user decisions)
- **Singapore category removed** (Mothership, Vulcan Post, The Independent SG) and the
  `Singapore` topic dropped from `interests.json` and `BROAD_KEYWORDS`. Not a feed failure —
  structurally unreachable: `Singapore` was the only topic those feeds could match and it was
  a broad keyword, so **19 raw hits → 0 matched, every day**, and 0 SG articles appeared in
  any of 50 archived briefs. 41 → 38 feeds. See `references/feed-history.md`.
- **Zero-yield feed tracking.** A feed can fetch perfectly and still be useless: Hugging Face
  Blog (A-tier) returns 862 entries with *zero* content at source, every run, and was recorded
  as healthy forever. `feed_health.json` gained `consecutive_zero_yield`/`last_usable`, counted
  **before** the entry cap, and the brief reports the count. 19 of 41 feeds contributed 0
  matched articles on the measured day. Nothing is auto-removed — pruning is a coverage call.
- Per-feed entry cap (1500, above the largest real feed) to bound a pathological archive
  feed; it partitions on the freshness cutoff so it provably cannot drop a fresh article — the
  first version sorted by date and the gate showed a date-only `pubDate` (all entries
  00:00:00) let a stable sort evict the newest 100 entries of an oldest-first feed.

### Runtime prompt (hygiene, not a cost lever)
- Dropped the article URL from the summarization prompt (~144 chars/prompt, never used) and
  replaced a no-op `content[:3000]` slice (content is already capped at 2000 upstream) with
  real per-category caps. ~3.8 K input tokens/run before; this is hygiene on a daily 8-item
  cron, not a saving worth reporting as one.

### Docs
- `SKILL.md` 6,240 → ~4,840 tokens while *adding* the new invariants, with the archival feed
  churn log and the performance history relocated to `references/`.
- Corrected drift found by the reviewers: 41→38 feeds and the Singapore row; IGN documented
  B-tier but A-tier in code; PetaPixel and TechXplore missing from the tier lists; a footer
  glossary that stated the must-read rule *without* its `AND ≥2 keywords` conjunct;
  `BROAD_KEYWORDS` stale in `CLAUDE.md`; the seen-ledger description (no longer
  `sha256(title|source)`); the `feed_health.json` record shape; `deepseek-v4-flash` →
  `deepseek-flash`; a `references/` list missing two files that exist and are cited; and
  `feed_doctor.py` parsing `resp.text` in violation of a stated invariant.

## 1.8.0 — 2026-07-11 (hermes-skill-tuneup: full tune-up)

Live copy confirmed: cron job `4879434e50ed` (`no_agent`, `deliver: local`) runs
`~/.hermes/scripts/morning_brief_run.py`, which invokes the skill-dir `scripts/` under
`/usr/bin/python3`. Green baseline: `send_brief.py --dry-run` over the frozen
`articles_summarized.json` fingerprinted `6d9209a6…`; footer math balances
(3800 = 3 must-read + 4 maybe + 158 matched&dropped + 3635 skipped). Performance measured
~20s fetch + ~3.6s summarize + ~0.2s send ≈ 23s against the 600s cron watchdog (~26×
headroom) — nothing to optimize (the 2026-06-06 concurrency tuning already won). Bug hunt +
terminal gate each ran an independent blind-reviewer fan-out (distinct lenses + adversarial
refuter); dry-run output is byte-identical to baseline (behavior preserved on live data).

### Delivery / security (send_brief.py, feed_doctor.py)
- **allowed_mentions:{"parse":[]}** added to every POST (`base_payload` + feed_doctor). Root
  cause: a crafted feed title / LLM summary containing `@everyone`/`@here`/`<@&role>` would
  ping the channel. Reachability is narrow (HN/Reddit teasers are dropped by the <200-char
  paywall guard; surviving vectors are a Reddit self-post >200 chars or an LLM echo) — but the
  fix is free and correct.
- **format_headline hardened**: `html.unescape` → collapse whitespace → escape
  `\ ` `` ` `` `* _ [ ] < >`. Root cause: it escaped only `* _ \`` *after* unescape, so a title
  like `pwn](<evil>)` injected a masked link and `&#91;`-encoded payloads slipped through and a
  `\n` forged sections. Parens intentionally not escaped (escaping `[ ]` already blocks link
  formation; keeps legit parenthetical titles clean). The `[...](<url>)` wrapper + unfurl
  suppression are untouched. `_defang_untrusted` added for summary text (`](`/`://`/`@everyone`
  → zero-width-space).
- **Feed-health notice is best-effort** (`post_message(..., fatal=False)`): it POSTed after the
  footer but before `save_seen_articles`, so a hiccup on that non-essential trailing message
  re-sent the whole already-delivered brief next run. Now a failed notice logs + continues.
- **Atomic writes** for `seen_articles.json` (send_brief) and `feed_health.json` (fetch_rss):
  temp file + `os.fsync` + `os.replace`. A cron-watchdog SIGKILL mid-write could truncate the
  seen ledger, which `load_seen_hashes` reads as empty → a run of duplicate briefs.
- **Guarded input loads**: `send_brief.main` and `summarize.main` now catch a missing/corrupt
  intermediate JSON with a clear message + non-zero exit (was a bare traceback); `summarize`'s
  `out` uses `.get(...,0)` for `total_scanned`/`skipped`; `get_summary` reads
  `art.get("summary") or ""` (a present-but-null summary would have crashed `strip_thinking`).
- **Maybe-relevant oversized-split**: if a 2-article chunk exceeds 2000 chars (a runaway
  summary) it's split into per-article messages so truncation can't corrupt the 2nd link's
  markdown. Header still appears exactly once; SECTION_SEP on the true last message only.

### Reliability (fetch_rss.py, morning_brief_run.py)
- **parse_published forces aware-UTC** — `match_articles` subtracted a possibly-naive datetime
  from an aware `now` (unguarded, unlike recency_multiplier). Latent today (stored dates are
  always aware) but a free hardening.
- **Wrapper: per-step timeout + failure self-notify** (deliver:local silent-failure fix). The
  scheduler pushes no alert for a deliver:local job, so a crash/error/hang was invisible. Each
  step now runs under `STEP_TIMEOUT` (fetch 120 / summarize 200 / send 240s, sum < 600s
  watchdog) and any non-zero exit / timeout / launch-OSError routes through `fail()`, which
  posts a terse "⚠️ Morning brief failed at <step>" to the brief channel (urllib, User-Agent
  set for Cloudflare, `allowed_mentions:{"parse":[]}`) then exits non-zero. Suppressed under
  `--dry-run`. (send step gets the largest budget — a 429-throttled delivery can run long and a
  killed send costs a duplicate brief.)

### Docs
- SKILL.md slimmed ~9.6k → ~4.7k tokens: the 31-entry Pitfalls section relocated verbatim to
  `references/pitfalls-log.md`, replaced by a compact **Critical invariants** section + pointer.
- Doc↔code drift fixed: "44 feeds" → 43 (Reddit r/technology was removed 2026-07-04, Tech
  18); File Structure now shows code-dir vs data-dir (`~/.hermes/morning-brief/`) and the cron
  wrapper; dropped the dangling `summarize.py.minimax.bak` reference; reframed stale MiniMax /
  `minimax-oauth` language (the model is deepseek-v4-flash). CLAUDE.md updated for the new
  sanitization, atomic writes, best-effort health notice, and the wrapper.
- Cron config: cleared the stale `skill`/`skills`/`prompt` fields on the no_agent job (they
  described MiniMax and would reload the full skill as an agent if `no_agent` ever flipped off).
- Removed stray files: `scripts/__pycache__/`, `~/.hermes/morning-brief/articles.json.pre-audit`,
  `references/provider-switch-checklist.md.bak-audit`.

Backups from this tune-up: `*.bak-tuneup-20260711` (delete after the user confirms a live run).
