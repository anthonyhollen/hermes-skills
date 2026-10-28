# CLAUDE.md — morning-brief

Engineering notes for the **morning-brief** skill. `SKILL.md` is the operator's view (triage
recipes); this file covers how the code is wired and where it bites; the per-bug history is
`references/pitfalls-log.md` and `CHANGELOG.md`. When docs and code disagree, the code wins —
fix the doc.

## What it is

A 3-stage batch pipeline (no server, no daemon) run once daily by `hermes cron`
(`30 7 * * *` = 7:30am SGT). Each stage is a standalone Python script that reads/writes
JSON files in `~/.hermes/morning-brief/` (NOT in this skill dir). Stages are chained on
the command line and communicate only through those files.

```
fetch_rss.py  →  articles.json  →  summarize.py  →  articles_summarized.json  →  send_brief.py  →  Discord
```

## Layout

```
morning-brief/                         ← this skill dir (code + docs)
├── SKILL.md                           ← product spec (pitfalls log lives in references/)
├── CLAUDE.md                          ← this file
├── CHANGELOG.md                       ← per-tune-up provenance
├── references/                        ← feed-history.md, performance-notes.md, pitfalls-log.md, …
└── scripts/
    ├── feeds.py        — FEEDS list: (name, url, category, tier). Edit to add/remove sources.
    ├── article_id.py   — SHARED: identity hashes, outlet key, MIN_CONTENT_CHARS, and the
    │                     same-story test (same_story / story_features / index_keys / _slots)
    ├── bounded_http.py — SHARED: HTTP under a wall-clock deadline (its docstring is the reference)
    ├── fetch_rss.py    — Stage 1: fetch → match interests → score → dedupe → classify (+ writes feed_health.json)
    ├── summarize.py    — Stage 2: LLM summaries via Hermes’ configured model (must-read 3-5 sent, maybe 2 sent)
    ├── send_brief.py   — Stage 3: backstop dedup → format → POST to Discord → record seen (+ surfaces dead feeds)
    ├── feedback.py     — reader ratings: reaction sync, the preference model, briefs/ manifests, CLI
    ├── feed_doctor.py  — on-demand tool: find LLM-suggested + live-probed replacements for dead/zero-yield feeds
    └── test_morning_brief.py — offline regression suite (temp HOME; run with /usr/bin/python3)

~/.hermes/morning-brief/               ← RUNTIME DATA dir (separate from code; not in git)
├── interests.json                     ← topics[] + block_keywords[]  (hand-edited config)
├── articles.json                      ← Stage 1 output
├── articles_summarized.json           ← Stage 2 output
├── feed_health.json                   ← per-feed {consecutive_failures, last_ok, …} (auto-managed)
├── seen_articles.json                 ← 7-day dedup ledger (auto-managed)
├── feedback.json                      ← reader ratings keyed by Discord message id (feedback.py)
├── briefs/<YYYY-MM-DD>.json           ← per delivered article: message id + rating features (30 days)
└── .run.lock                          ← wrapper's single-run flock
```

Key gotcha: **code dir ≠ data dir.** Every script hardcodes
`BASE_DIR = ~/.hermes/morning-brief` for I/O. The scripts live under
`~/.hermes/skills/productivity/morning-brief/scripts`. Don't confuse them.

## Run / test

```bash
cd ~/.hermes/skills/productivity/morning-brief/scripts
python fetch_rss.py && python summarize.py && python send_brief.py   # full pipeline
python send_brief.py --dry-run                                       # format only, no Discord post
```

`fetch_rss.py` and `summarize.py` overwrite their output JSON, but they are NOT side-effect
free: `fetch_rss.py` updates `feed_health.json` (its counters count RUNS — pass `--dry-run`
to skip that) and `summarize.py` spends LLM calls. `send_brief.py` appends to
`seen_articles.json` after posting, so re-running it posts again AND marks those articles
seen. Use `--dry-run` when iterating; the wrapper's `--dry-run` passes it to both ends.

## Data flow & the article dict

Articles are plain dicts passed stage to stage via JSON. Fields accrete:

- Stage 1 (`fetch_feed`): `title, url, content` (tags stripped, then HTML entities decoded — feeds double-encode `&amp;nbsp;`; capped 2000 chars), `source, tier, published` (ISO str). `_extract_articles` returns `(articles, usable)` — `usable` is the count of entries ≥ `MIN_CONTENT_CHARS` measured BEFORE `MAX_ENTRIES_PER_FEED`, so feed health describes the feed and not our cap.
- Stage 1 (`annotate_story_tokens` → `match_articles` → `deduplicate` → `classify`): adds `_story` (boilerplate-stripped lede words), `_story_deep` (the same for the first 1,000 chars), `_score, _interest_kw_count, _topics` (the matched topic names), `_pref, _pref_why` (only when a rating changed the score), `_sources, _num_sources, _cluster_size, _twins, _chained, _category`. `articles.json` also carries `held_retellings` (re-tellings held back today, recorded by send_brief).
- Stage 2: adds `summary` (and passes `held_retellings` through).
- Underscore-prefixed keys are internal pipeline state; none reach Discord or the LLM prompt. `published` is an **ISO string**, not a struct_time.

## Scoring & classification (in code)

`score = min(families, 3) × tier_mult × recency_mult` (× 0.5 for a roundup / newsletter /
guide — `_ROUNDUP_RE` on the title, `_ROUNDUP_LEDE_RE` on the lede, `_GUIDE_RE`), computed inline in
`fetch_rss.match_articles` (`score_article` is a REPL-only copy — changing it changes nothing):
- **Filters, in order:** seen (any identity hash) → block keyword → freshness (older than
  `MAX_ARTICLE_AGE_DAYS = 7` or undated → dropped; recency is only a multiplier, Pitfall 18) →
  teaser (`content < MIN_CONTENT_CHARS` = 200) → **promo** (`_PROMO_RE` on the title —
  dropped, counted as skipped) → keyword pass → **hold-back** (below).
- **Topic families** (`TOPIC_FAMILIES`): synonyms count once — LLM / language model / frontier
  model…, OpenAI / ChatGPT / GPT-6…, AI agent / agentic / automation, … `_interest_kw_count`
  is the FAMILY count (used by the score cap and must-read eligibility); `_topics` keeps the
  raw topics for feedback.py. Areas for the diversity rule: `NON_AI_FAMILIES` (apple, camera,
  ev, GPU, gaming), everything else "ai".
- Keyword matching is word-boundary regex (`\bEV\b`), plural-tolerant for topics
  (`\bLLMs?\b`) and exact for `block_keywords` (`compile_keyword_patterns(..., allow_plural=False)`).
- **Broad-keyword scoping (A4):** `BROAD_KEYWORDS = {"EV", "Grok", "Sora"}` count only when a specific topic also matched.
- `TIER_MULT = {A:3, B:2, C:1}`; recency: `<24h → 3×, <48h → 2×, else 1×`.
- **Reader ratings** (`feedback.Model.preference`, passed in as `prefs`): `_score ×= mult`
  (×0.5–×1.8) right after scoring (so after the hold-back) and before clustering, so it
  also picks the cluster representative. No ratings →
  `prefs is None` → the pass is skipped entirely. `_pref < PREF_MUST_READ_FLOOR` (0.7)
  blocks must-read. The reactions are synced on a thread started before `fetch_all_feeds`
  (`SYNC_BUDGET_SECONDS` 20, joined before matching); late/failed → the saved ratings.
- **Ranking is deterministic** — `_rank_key` = (score, `_num_sources`, keywords, tier, published, url, source, title). Ties used to fall to `as_completed` order, i.e. network timing.
- **Outlet bonus** (in `deduplicate`, once `_num_sources` is known): `_score ×= 1 + min(0.5, 0.25 × (outlets − 1))`.
- **Must-read:** `(num_sources≥2 AND kw≥2) OR kw≥3`, published ≤ `MUST_READ_MAX_AGE_HOURS` (48),
  score before ratings ≥ `MUST_READ_MIN_SCORE` (10), not a roundup, and a **C-tier** article
  only with `num_sources≥2`. Up to 3 slots; fewer when fewer qualify (the rest compete for maybe).
- **Maybe:** the rest by rank, ≤ `MAX_PER_SOURCE_MAYBE` (2) per **outlet** (`outlet_key`, so The
  Verge + The Verge AI share; `MAX_PER_OUTLET_MAYBE` sets reddit.com to 1), and ≤
  `MAYBE_MAX_PER_AREA` (3) single-area stories of one area while another area's story scores ≥
  `MAYBE_OTHER_AREA_MIN` (7). 5 slots.
- **Selection walks the whole ranked pool** and skips a same-story twin of anything already picked, so dedup never leaves a slot empty (it used to cut to 3 + 5 first and let send_brief remove twins: 4 of 12 briefs went out short).
- `num_sources` = distinct **outlets** among cluster members that match the representative directly (not via a chain, not promotional — `_PROMO_RE`), clamped to the distinct feed names.

## Same-story detection & dedup

One predicate, `article_id.same_story` (fast form `same_story_f` on `story_features`), used by
fetch_rss clustering + selection, send_brief's backstop, and the hold-back. The rules
(constants and calibration notes in `article_id.py`):
- same normalized url;
- a **sequel veto**: equal-length headlines differing only in 1–2 identifiers (versions,
  model numbers, "beta 2", names) are different stories (`_slots`/`_slot_conflict_t`) — skipped
  across outlets only for a near-identical lede (≥ 0.90);
- across outlets: syndicated lede (content Jaccard ≥ 0.50), or title Jaccard > 0.40 with ≥ 1
  distinctive shared word, or content > 0.12 with ≥ 2 distinctive shared title words
  (> 0.08 with ≥ 3), or — the **deep path** (1.13), for outlets that open with commentary —
  ≥ 2 distinctive shared title words and ≥ 0.16 overlap of the first 1,000 chars, published
  ≤ 36 h apart when both dates are known (`_story_deep` / publication time = elements 8 / 9
  of `story_features`; swap the sequel flags with `with_slots`, never by slicing, or they
  are lost). Follow-ups recap the original exactly in this window — the margin is thin;
- within one outlet (aggregators Techmeme/Slashdot/Reddit excepted): title > 0.65 AND
  content > 0.25 with ≥ 8 shared words — no content-only path (same-outlet matches were
  wrong 11/11 at the gate);
- comparison text is entity-decoded, plural-folded, "U.N." → "UN", Techmeme attribution
  stripped, and per-source boilerplate removed (`annotate_story_tokens`: a 5-word run in ≥ 3
  of one source's ledes).

`fetch_rss.deduplicate` is blocked single-linkage (union-find over pairs sharing an
`index_keys` key — url, title word, or a story-word min-hash) with the representative chosen
by `_rank_key`. Its direct matches become `rep["_twins"]` (recorded by send_brief); members
that match a direct twin (not the rep) become `rep["_chained"]`, recorded by identity only
(`kind: chained`, 3 days) — only through a firm link (`firm_story_f`: no 8-12% band, no deep
path) that is not an aggregator / round-up / promo item, and never for an aggregator or
round-up member, a member from the rep's own outlet, a sequel of the rep, or under a
round-up rep. The
send-time passes (`dedup_mr_vs_may` → `dedup_within_mr` → `dedup_within_may`) are a backstop
and cannot drop anything fetch_rss clustered.

**Hold-back** (`fetch_rss._hold_grade`, against ledger entries from the last 7 days + 6 h):
a *re-telling* (url, title > 0.55, or content ≥ 0.35) is held while eligible and itself
recorded (`held_retellings`); a *weaker* match (incl. a deep match) is held one run, and only
against a story recorded before it was published — which is why an article already sitting
in the shown story's cluster needs its own `kind: chained` ledger entry. Held articles are removed before clustering. Ledger
entries store their title's sequel flags (`slots`, judged with their own body) so the
hold-back agrees with same-day clustering. Known limits (accepted at the gate): a follow-up
can be delayed a day; a repeat that isn't recognised can still out-score a development in
its cluster; different-length sequels and alias headline swaps (both bodies naming both
names) aren't distinguished.

## External dependencies

- **LLM is provider-agnostic — `summarize.py` follows whatever Hermes Agent is configured to use.**
  It reads `model.default` from `~/.hermes/config.yaml` and resolves base_url/api_key/api_mode via
  `hermes_cli.runtime_provider.resolve_runtime_provider()` (imported from `~/.hermes/hermes-agent`;
  works under `/usr/bin/python3` — no openai/anthropic SDK needed). Switching providers
  (`hermes model`) requires NO code change. Two wire formats, picked by `api_mode`:
  `anthropic_messages` → POST `{base_url}/v1/messages` (Bearer auth + `anthropic-version` header;
  reasoning arrives as separate `thinking` blocks, only `type=="text"` blocks are kept — and the payload
  sends `thinking:{"type":"disabled"}` so a reasoning model can't burn the budget and return empty text,
  Pitfall 22; auto-dropped on 400);
  `chat_completions` → POST `{base_url}/chat/completions` (inline `<think>…</think>` is stripped).
  `call_model` retries empty/errored AND malformed-but-200 responses (`_extract_chat` is `.get`-defensive
  so an error envelope / empty `choices` yields `""`; `call_model` catches `RequestException` plus
  `ValueError/KeyError/IndexError` — Pitfall 25); on final failure `summarize_article`
  emits a sentence-boundary **extractive** fallback flagged `summary_is_extractive` (shown as-is by
  `send_brief.py::get_summary`, which otherwise replaces content-echoes with "Summary unavailable").
  `send_brief.py::get_summary` also strips `<think>` defensively.
- **Discord** Bot API v10, posts to channel `1499972245437550602` as multiple messages. `post_message`
  retries transient errors (429 honoring `Retry-After`, 5xx, network) up to `POST_RETRIES=4` and caps
  content to the 2000-char limit (`_cap_summary` + a final truncate). `send_messages` posts every message
  with `fatal=False`: on the first failure it stops, records exactly the articles that landed (each built
  message carries its articles), then exits 1 — unless only the footer / health line was lost (exit 0 with a
  `[WARN]`). On an empty day a lost header or placeholder is a failure too. Twins are recorded only when every
  article-carrying message landed (cluster twins: those of each delivered article). Every POST carries a
  per-message Discord `nonce` + `enforce_nonce`, identical across its retries and unique per run, so a
  retried POST that Discord had already accepted can't double-post. `save_seen_articles` and `update_feed_health`
  write atomically (temp file + `os.fsync` + `os.replace`) so a watchdog SIGKILL mid-write can't truncate
  the seen ledger (which `load_seen_hashes` would then read as empty → a run of duplicate briefs).
- **Cron entry / wrapper:** the live no_agent job (4879) runs `~/.hermes/scripts/morning_brief_run.py`
  (NOT in this dir) under Hermes' dependency venv; it invokes fetch / send with `/usr/bin/python3` (the
  venvs lack feedparser) and summarize with the first interpreter that imports Hermes' config +
  runtime_provider (`_summarize_interp`: its own, the install's dependency venv, the gateway venv, then
  `/usr/bin/python3`) — Hermes v0.22.0's config code needs ruamel.yaml, which the system python lacks. It gives each step a per-step `subprocess.run(timeout=...)` (`STEP_TIMEOUT`, sum < the 600s
  cron watchdog) and routes any step failure/timeout/launch-error through `fail()`, which self-notifies
  Discord (urllib POST, `allowed_mentions:{"parse":[]}`, suppressed under `--dry-run`) then exits non-zero
  — because `deliver: local` means the scheduler pushes no failure alert itself. It holds a `flock` on
  `<data>/.run.lock` for the whole run (passed to the steps via `pass_fds`): a real run waits up to
  `LOCK_WAIT_SECONDS` (30) for an overlapping run and then fails via `fail()`; a `--dry-run` gives up at
  once; an unusable lock file just runs unlocked. Each step's timeout is also capped by what is left of the
  600 s watchdog minus `NOTIFY_RESERVE_SECONDS` (20), so `fail()` can always post its alert.
- **Environment variables:** `DISCORD_BOT_TOKEN` (env, else `~/.hermes/.env` — the cron scrubs secrets from
  the child env, so `.env` is the live source); `DISCORD_BRIEF_CHANNEL_ID` (optional; falls back to
  `DISCORD_HOME_CHANNEL`, then `1499972245437550602`, from env or `.env`); `HERMES_INFERENCE_MODEL`
  (optional model-name override); `HERMES_HOME` (wrapper's Hermes root, default `~/.hermes`).
- **Step budgets** (each step bounds itself below its wrapper timeout — a SIGKILL lands before the step
  writes, so an overrun destroys the brief or, in send, delivers it and loses the ledger): fetch
  `FETCH_BUDGET_SECONDS` 100 / 120, summarize `STEP_BUDGET_SECONDS` 150 / 200, send `SEND_BUDGET_SECONDS`
  195 / 240; wrapper 30 s lock wait + steps, capped to 600 − 20 s.
- **Model creds** come from Hermes’ resolved runtime (the active provider's token, credential pool, and fallbacks — deepseek-flash as of 2026-09) — no model API key lives in this skill. **Discord secrets** for `send_brief.py` resolve from env first, then `~/.hermes/.env`: `DISCORD_BOT_TOKEN`, `DISCORD_BRIEF_CHANNEL_ID` (or `DISCORD_HOME_CHANNEL`).
- Python libs: `feedparser`, `requests` (no requirements.txt — assumed present in the hermes env). `bounded_http.py` touches urllib3 internals (`resp.raw.stream(..., decode_content=False)`, the socket behind `resp.raw`) — if urllib3 is upgraded, re-run the drip/header-stall probes in `references/performance-notes.md`.

## Discord formatting rules (load-bearing — easy to regress)

The exact message sequence (relocated from SKILL.md 2026-09-14 — `build_messages()` is the
single source of truth, and `--dry-run` prints it verbatim):

```
1  ## 🤖 Morning brief  |  dd-MMM-yyyy
2  ### Must-read news                    (or "*No must-read stories today.*")
   • **[Headline](<URL>)**
     Full summary                        ← ONE MESSAGE PER must-read article
   ⠀                                     ← blank-row glyph, after the LAST must-read only
3  ### Maybe relevant                    (header on the FIRST message only; "*Nothing to show today.*" if empty)
   • **[Headline](<URL>)**
     2-sentence summary                  ← ONE MESSAGE PER maybe article (since 1.11 — a
                                           👍/👎 reaction must name exactly one article)
   ⠀                                     ← after the LAST maybe only
4  __X articles scanned · N must-read · M maybe · D matched & dropped · Y skipped · rate with 👍 👎__
5  🩺 _Feed health: …_                   ← only when a feed is down / yields nothing usable
```

- **Lengths are UTF-16 units, not code points** (`_dlen`): that is how Discord counts, and an
  emoji-dense 1,852-char message is 3,652 units. Every limit check and every summary budget
  goes through it, and a summary's budget is derived from the ACTUAL rendered headline length
  (a flat 1500-char cap produced a 2010-char message for a 217-char title + 280-char URL).
- **`build_messages()` is the only assembler.** `send_messages` and `dry_run` both consume it,
  and it returns `(label, content, articles)` — the article list is what lets a partial send
  record exactly what was delivered.

- Links MUST be `**[title](<url>)**` — the **angle brackets** are what actually suppress
  link-preview unfurling. The payload uses `embeds:[]` + `flags:4` (SUPPRESS_EMBEDS); the old
  `unfurl_links`/`disable_web_page_preview` keys were Slack/Telegram params Discord ignores.
- **Untrusted-content sanitization (security, don't regress):** titles/summaries are feed- and
  LLM-sourced, and some feeds (Reddit self-posts, HN) have user-submitted titles. So: every POST
  sends `allowed_mentions:{"parse":[]}` (blocks @everyone/@here/role pings server-side);
  `format_headline` runs `html.unescape` → whitespace-collapse → escapes `_TITLE_META`
  (`\ ` `` ` `` `* _ [ ] < >`) so a crafted title can't inject a masked link or forge lines (parens
  are intentionally NOT escaped — escaping `[ ]` already prevents a link label from forming, and it
  keeps legit parenthetical titles clean); `_defang_untrusted` breaks `](` / `://` / `@everyone`
  in summaries with a zero-width space. The `<url>` in the wrapper is added by the function, not
  from the title, so unfurl-suppression is preserved.
- Bullets are `•`, never `-`. Sections are separated by a **blank-row glyph** — a line holding
  only `⠀` (U+2800), via the `SECTION_SEP = "\n⠀"` constant. Plain blank lines collapse and `---`
  renders as a heavy `<hr>`; U+2800 survives trimming and reads as an empty row.
- `format_headline()` already bolds; do NOT re-wrap its output in `**` (causes `****`).
- Every article is its own message (maybe-relevant was chunked 2/message until 1.11; don't
  re-chunk — `send_messages` records each article message's id in `briefs/<date>.json` and a
  reaction on a 2-article message can't say which one it rates). The `### Maybe relevant`
  header appears **exactly once** (first message only), likewise `### Must-read news` —
  messages 2+ start with a bare `•` bullet (Pitfall 9).
- `cron` job MUST use `--deliver local` — `send_brief.py` posts to Discord itself; `deliver=discord:…` double-posts.
- **`_safe_url` sanitizes the LINK, not just the title.** Both come from the same untrusted
  feed item; a `>` in the URL used to close the angle bracket and turn the rest of the line
  into live Markdown. A rejected scheme renders the headline as plain `**title**` �� and then
  the bare-URL defang matters, because the `<>` unfurl suppressor is gone on that path.

## Cron config conventions

- Server TZ is SGT (+08), so `30 7 * * *` is literally 7:30am local.
- Setting interests: edit `~/.hermes/morning-brief/interests.json` directly — no restart, cron re-reads each run.
- The job has `repeat.times = 999` — check `hermes cron list` occasionally and raise it with `hermes cron edit` before it runs out.

## When editing

- Adding/removing feeds → `feeds.py` only; tiers drive scoring, keep them honest. `fetch_feed` sends a **browser User-Agent** (a bot UA gets 403'd by Cloudflare on several feeds) and **retries transient failures** (network/429/5xx, not 403/404). Per-feed outcomes are tracked in `feed_health.json`; a feed is "dead" after 3 straight failed runs, surfaced in the brief by `send_brief.py`. Run `feed_doctor.py` to get verified replacement candidates. Probe new feeds before adding (full content ≥200 chars or the paywall guard zeroes them); SG mainstream feeds are teaser-only.
- Changing scoring/classification → `fetch_rss.py` (`match_articles` computes the score; `classify`, `_rank_key`, `_must_read_eligible`). Tunables at the top: `BROAD_KEYWORDS` (A4), `MAX_PER_SOURCE_MAYBE` (B2), `_PROMO_RE`, the `HOLD_BACK_*` constants. Same-story thresholds live in `article_id.py` — change them only after re-running the pair scan over a full captured fetch, and add the case to `test_morning_brief.py`.
- Changing the model/provider → do it in Hermes (`hermes model` / `config.yaml`), NOT here; `summarize.py` follows automatically. Changing summary length/prompt → `summarize.py` (`MAX_TOKENS`: must-read 2000, maybe 1500 — reasoning tokens count against this, so don’t set it too low or the visible text gets squeezed out).
- Changing Discord output → `send_brief.py`; always verify with `--dry-run` first.
- Tuning throughput → `MAX_WORKERS` (`fetch_rss.py`; raised to `len(FEEDS)` at runtime so all feeds fetch in ONE wave — two waves doubled the worst case past the step timeout) and `CONCURRENT_LIMIT` (`summarize.py`, 8 = max articles/run so all summaries issue in one wave). See `references/performance-notes.md`.
- **Changing any timeout → check it against its step budget**, and the budget against the wrapper's `STEP_TIMEOUT`: fetch 100/120, summarize 150/200, send 195/240, sum 560 < the 600s watchdog. A step killed by the watchdog dies *before* writing, so an unbounded call doesn't slow the brief, it destroys it. All HTTP goes through `bounded_http.py`; do not add a bare `requests.get/post` on a pipeline path.
- **The ledger contract:** `send_brief` writes it, `fetch_rss` reads it, `article_id` defines identity, and each entry keeps the LEGACY `hash` so a rollback still reads it. Entries carry `hashes, ts, title, source, url, story, story_deep, slots` and optionally `ttl_days` (`DEDUPED_TWIN_TTL_DAYS` 3 for a send-time twin; cluster twins and held re-tellings get `MAX_AGE_DAYS`; a pre-1.10 twin — `ttl_days` but no `story` — is read as `MAX_AGE_DAYS`; a `"kind": "chained"`
entry has `ttl_days` (`CHAINED_TTL_DAYS` 3) and no `story`/`slots` by design, keeps its ttl, and
never joins the hold-back set). Clamp identically on both sides (`load_seen_hashes` / `_entry_ttl`) — if reader and writer disagree about what is expired, an article is delivered every run and never re-recorded.
- **After any script edit** run `/usr/bin/python3 test_morning_brief.py` (and `MB_WRAPPER=<path>` to test a staged wrapper).
- After any behavior change, update `SKILL.md` AND add a `CHANGELOG.md` entry. `SKILL.md` deliberately carries no feed counts or tier lists (`feeds.py` is the source of truth; those lists drifted before 1.9.0).
