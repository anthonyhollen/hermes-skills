# Morning Brief — Pitfalls Log (historical bug-fix provenance)

> **Note (2026-09-26):** some identifiers quoted in older entries no longer exist —
> `format_section` (#5, #8), `lines = ["### Maybe relevant"] if i == 0` (#9, now `head = …`),
> `resp.raise_for_status()` (#12), `score_article()` returning 0.0 (#16, now a drop in
> `match_articles`). Dedup pitfalls (#15, #29, #30) are superseded by 1.10.0's
> `article_id.same_story`.

Relocated from SKILL.md (2026-07-11) to keep the hot-path SKILL.md small — this is the
per-bug war-story log. The still-load-bearing operational rules these established are
summarized in SKILL.md ("Critical invariants") and detailed in CLAUDE.md; open this file
when you need the full root-cause/fix history behind a rule.

> Provider note: pitfalls tagged *MiniMax* are historical — `summarize.py` is
> provider-agnostic and currently runs whatever `hermes model` is set to (deepseek-flash
> as of 2026-07). The reasoning-handling (#1/#22) and max_tokens (#4) lessons still apply to
> any reasoning model; the HTML-entity (#5), repeat (#14), and excerpt (#17) guards are
> model-independent and live in send_brief.py.

## Pitfalls

> **Note (provider switch):** pitfalls marked *MiniMax* below are historical — `summarize.py` is now provider-agnostic (see **Model Provider**). The reasoning handling (#1) and `max_tokens` budgeting (#4) still apply in spirit to any reasoning model; the HTML-entity (#5), repeat (#14), and excerpt (#17) guards live in `send_brief.py` and are model-independent.

1. **MiniMax think blocks silently empty summaries** — Always set `"thinking": {"type": "off"}` in the API payload.
>
> **Correction (2026-09-14):** `{"type": "off"}` is silently IGNORED by MiniMax-M3 — the value that actually disables reasoning is `{"type": "disabled"}`, which is what `summarize.py` sends. It is also only emitted on the `anthropic_messages` wire; under `chat_completions` (the live mode) there is no such switch, so the bound on a reasoning model burning its budget is the extractive fallback, not this flag. Without it, the LLM places the actual summary inside a `<think>` block and stripping it leaves an empty string. See `references/minimax-api-quirks.md`.

2. **Double Discord delivery** — Cron `deliver=discord:` + script's Discord API call = two messages. Must use `deliver=local`. Same bug class hit Evening Weather Forecast (c967008ca121) in May 2026 — both crons were calling `send_message` AND getting cron auto-delivery simultaneously. Always check: if the cron prompt contains an explicit Discord delivery step, `deliver` MUST be `local`.

3. **Feed URL rot** — RSS URLs change frequently. Re-probe periodically and prune dead feeds; a feed that times out (rather than 404ing fast) adds latency every run. 13 dead feeds removed 2026-05-31 — see the RSS Feed Library note.

4. **Reasoning models truncate must-read summaries at low `max_tokens`** — Current budget is `must_read: 2000`, `maybe_relevant: 1500` (in `summarize.py`). Must-read (3-5 sentence summaries) needs a generous cap because reasoning tokens are spent before visible text; at a low cap (the old 300/400) complete summaries get hard-truncated mid-word. Never drop must-read below ~1000. *(Historical: the original hardcoded-MiniMax script used 1000/2000; the provider-agnostic rewrite settled on 2000/1500.)* Always verify after changing model or prompt length.

5. **MiniMax reproduces HTML entities in output** — When RSS content contains `&#8217;`, `&#8212;`, etc., MiniMax echoes them in its summary output. Fix: call `html.unescape()` on the summary in `send_brief.py` (both in `format_headline` for the title and in `format_section` for the summary body) before rendering.

6. **Server timezone** — Server is SGT (+08). `30 7 * * *` = 7:30am SGT.

7. **Discord link previews not suppressed by API flags alone** — Discord auto-scrapes URLs in message content to generate link previews. The `embeds: []` + `flags: 4` payload is necessary but NOT sufficient on its own — Discord still unfurls certain URLs (e.g. simonwillison.net). **The load-bearing fix is in the markdown format**: use angle brackets around the URL in inline links: `[title](<url>)` instead of `[title](url)`. This forces Discord to treat it as a raw link and skip unfurling entirely. (Historical note: the old payload used Slack/Telegram keys `unfurl_media`/`unfurl_links`/`disable_web_page_preview`, which Discord ignores — replaced by `flags: 4`.)

8. **`format_section` double-bolds headlines** — `format_headline()` already returns `**[title](url)**`. If `format_section` wraps it in `**` again → `****[title](url)****`. Fix: pass `format_headline()` output directly without re-wrapping in `**`.

9. **Section headers must appear exactly once** — Both `### Must-read news` and `### Maybe relevant` must appear once and only once across the whole brief, never repeated per chunk. Must-read uses `header = "### Must-read news\n" if idx == 0 else ""`; maybe-relevant uses `lines = ["### Maybe relevant"] if i == 0 else []`. Do NOT add the header to every chunk — chunks 2+ correctly start with bare `•` bullets.

10. **`score_article` uses wrong date field** — `article.get("utc")` returns `None` because articles store the publish date under key `"published"` (ISO string), not `"utc"`. This silently zeroes out the recency multiplier (always returns `1.0×`). Fix: parse `"published"` via `datetime.fromisoformat()`. See `references/session-fixes.md` Bug 7.

11. **Interest keyword matching uses word boundaries (FIXED)** — Plain substring matching was too loose: `"EV"` matched "revamped"/"deleting"/"reviewed" and `"AWS"` matched "laws"/"flaws", inflating `_interest_kw_count` and causing spurious blocks (the Apple/Siri triple-variant root cause). `fetch_rss.py` now compiles every topic and block keyword to a word-boundary regex (`\bEV\b`, case-insensitive) via `compile_keyword_patterns()` — applied to both scoring and block filtering.

12. **Seen tracking saves too early** — `save_seen_articles()` must be called AFTER confirmed Discord delivery, not in `fetch_rss.py`. Call it in `send_brief.py` after `resp.raise_for_status()`.

13. **Maybe-relevant format must match must-read** — Summary goes on the next line (indented 2 spaces), not on the same line with a `·` separator. Discord ignores trailing `\n` with no content after it; no workaround with extra characters needed since the format change removed the need for separation.

14. **MiniMax triple-repeat summaries** — Added two deduplication patterns (blank-line split + 50/50 half-match) but MiniMax still produced triple repeats where the same 30+ char phrase appears 3+ times separated by punctuation. This bypassed both checks. Fix: add a third regex pattern `(.{30,}?)(?:\s*(?:\n\n?|\.)\s*)(?:\1\s*(?:\n\n?|\.)\s*){2,}` to catch repeating non-overlapping substrings. Test with the Varda Space Industries article (MIT Tech Review "The Download" must-read).

**15. Post-classification deduplication in `send_brief.py`** — Three functions handle paraphrased variants that survive the dedup in `fetch_rss.py` (now 40% cross-source / 65% same-source — see Pitfall 19; these send-time guards remain as a belt-and-suspenders safety net):
    - `dedup_mr_vs_may(threshold=0.40)` — removes maybe-relevant articles that overlap >40% title token similarity with any must-read (cross-section dedup)
    - `dedup_within_mr(threshold=0.40)` — removes near-duplicate variants within must-read itself; keeps highest-scoring article per cluster (see Pitfall 30)
    - `dedup_within_may(threshold=0.40)` — removes near-duplicate variants within maybe-relevant itself; keeps highest-scoring article per cluster
    - Threshold rationale: max overlap among true duplicates is 43% (Siri variants); unrelated pairs are all <30%. 40% cleanly separates the two groups. A **secondary content-similarity check** at 12% threshold (see Pitfalls 29–30) catches cases where headlines differ too much for title overlap alone.
    - None of these functions change classification — they only filter which articles are displayed.

16. **Paywall detection at scoring time** — RSS teasers (paywalled articles) are typically <200 chars. Without a content-length check, these teasers can score high and beat real articles. Fix: `score_article()` returns 0.0 for content <200 chars. This is checked BEFORE keyword matching, so paywalled articles are never scored.

17. **Truncated/excerpt summary detection** — MiniMax can fail to synthesize a proper summary and instead echo the article's opening paragraph (especially with sparse content or HTML entities). If `get_summary()` finds the summary starts with the same text as content (first 100 chars match), treat it as a failed synthesis and replace with placeholder "Summary unavailable — preview limited. Click to read full article."

18. **Stale articles featured as must-read** — Recency was only a score *multiplier* (today 3× / yesterday 2× / older 1×), never a filter, and the must-read gate is purely source/keyword based. So a high-keyword months-old post could be featured: on 2026-05-30 "Introducing GPT-5.2" (published 2025-12-11, ~5.5 months old, OpenAI Blog, 3 kw → score 6.0) outranked the only fresh must-read (score 4.0). Big-archive feeds (OpenAI Blog serves ≈978 entries) re-surface old posts every run, and 7-day seen-tracking can't suppress something last featured months ago. Fix: a hard freshness cutoff in `match_articles` — `MAX_ARTICLE_AGE_DAYS = 7`; articles older than that (or undated/unparseable) are dropped *before* scoring, so stale content can't reach must-read OR maybe-relevant. The `RECENCY_CUTOFF_DAYS` constant that hinted at this was dead code, never wired up.

19. **Multi-source corroboration was dead; revived with split dedup threshold (B1, 2026-05-31)** — The must-read "≥2 sources" path depends on `_num_sources`, which comes from `deduplicate()`'s title-overlap clustering. The old flat **65%** threshold almost never merged across outlets (real paraphrased coverage measures 31–43% — see `references/bug18-siri-dedup.md`), so every story looked single-source and the path was effectively dead code; must-read came only from the ≥3-keyword branch. Fix: `deduplicate()` now merges at **40% across different sources** (revives corroboration) but keeps **65% within the same source** (two similar headlines from one outlet are usually distinct stories — don't drop one). Constants: `CROSS_SOURCE_DEDUP_THRESHOLD = 0.40`, `SAME_SOURCE_DEDUP_THRESHOLD = 0.65`. Caveat: 40% cross-source can occasionally merge two same-entity-different-product stories (e.g. "Apple unveils iPhone" vs "Apple unveils Mac" ≈60%) — acceptable; the higher-scoring one survives and the corroboration win outweighs it.

20. **Broad keywords matched noise (A4, 2026-05-31)** — All 27 topics counted equally, so broad terms (`Singapore`, `EV`) scored human-interest/car stories as if they were core hits ("Woman marries inmate at Tanah Merah Prison" matched `Singapore`). Fix: `BROAD_KEYWORDS = {"Singapore", "EV"}` in `fetch_rss.py`; `matched_keywords()` drops broad keywords unless a *specific* topic also matched, and returns empty if only broad keywords hit. `compile_keyword_patterns()` now returns `(keyword, regex)` pairs so callers know *which* keywords matched (block-keyword call sites must unpack the tuple: `for _kw, pat in block_patterns`). Add a topic here if you find it pulls noise; remove if it over-filters.

21. **One source dominating maybe-relevant (B2, 2026-05-31)** — Maybe-relevant was top-5-by-score with no diversity guard, so a prolific source could fill it (Simon Willison once took 3 of 5 slots). Fix: `classify()` applies a per-source cap of `MAX_PER_SOURCE_MAYBE = 2` after ranking by score and before the top-5 cut. Footer math still balances — capped extras fold into `matched & dropped`.

22. **"Summary unavailable" from intermittent empty model responses (2026-06-01)** — Two articles in the 06-01 brief rendered "Summary unavailable — preview limited". Root cause was NOT content or the echo-detector (Pitfall 17), which worked correctly: the reasoning model (MiniMax-M3, `anthropic_messages`) intermittently returned a response with *only* a `thinking` block and an **empty** `text` block — so `_extract_anthropic()` got `""`, `summarize.py` fell back to `content[:200]+"..."`, and `get_summary()` correctly flagged that echo. Both summarized fine on retry (transient). Three-part fix in `summarize.py` + `send_brief.py`: (1) **retry** empty/errored summary calls (`SUMMARY_RETRIES=2`, backoff); (2) **disable reasoning** for summaries — anthropic payload sends `thinking:{"type":"disabled"}` (verified: MiniMax-M3 then returns text-only; `{"type":"off"}` is silently ignored — still reasons; the disable is dropped automatically on a 400 for portability); (3) **extractive fallback** — when the model still can't summarize, `_extractive_fallback()` returns the first 2–3 clean sentences and sets `article["summary_is_extractive"]`, which makes `get_summary()` show it as-is instead of suppressing it (the echo-detector still fires for *unflagged* content echoes). Note: the unrelated `fetch_rss.py` content-floor/aggregator draft (#4) only would have dropped the Techmeme item; the Slashdot must-read would still have failed — so this, not the floors, is the actual fix.

23. **Discord send aborted the whole brief on transient errors (2026-06-06)** — `post_message` did `sys.exit(1)` on ANY `status_code >= 400`. The brief POSTs ~8–10 messages in a burst, so a routine **429 rate-limit** (or a 5xx) killed the run mid-send; since `save_seen_articles()` runs only after all sends, the already-posted articles were never recorded and got re-sent (duplicated) the next day. Fix: `post_message` retries transient failures — 429 (honoring `Retry-After`, `RETRY_AFTER_CAP=10s`), 5xx, and network errors — up to `POST_RETRIES=4` with backoff; a non-transient 4xx or exhausted retries is still fatal and exits *without* recording seen, so the brief re-sends cleanly next run rather than being silently marked delivered.

24. **Unbounded message length → Discord 50035 (2026-06-06)** — Nothing capped outgoing content to Discord's 2000-char limit; a model that ignored the "3–5 sentence" instruction (`must_read` budget is `MAX_TOKENS=2000`) could produce a message >2000 chars → HTTP 400 → (via Pitfall 23) an aborted brief. Fix: `get_summary` caps each summary via `_cap_summary()` (`MAX_SUMMARY_CHARS=1500`, word boundary) and `post_message` truncates total content to `DISCORD_CONTENT_LIMIT=2000` as a last-resort guard.

25. **Malformed-but-200 LLM response escaped the summary retry (2026-06-06)** — In `chat_completions` mode, `_extract_chat` hard-indexed `data["choices"][0]["message"]`; an error envelope or empty `choices` returned with HTTP 200 raised `KeyError`/`IndexError`, which is NOT a `requests.RequestException`, so it skipped `call_model`'s retry loop and dropped straight to the extractive fallback with no retry. Fix: `_extract_chat` is defensive (missing/empty `choices` → `""`, treated as an empty response → retried) and `call_model` also catches `(ValueError, KeyError, IndexError, TypeError)` (covers `json.JSONDecodeError`). The current `anthropic_messages`/MiniMax path was already safe — `_extract_anthropic` uses `.get`.

26. **`deduplicate` merged unrelated articles with empty URLs (2026-06-06)** — `normalize_url("")` returns `""`, and the merge test `normalized_url == rep_url` made `"" == ""` true, so two different-source articles that both lacked a `link`/`id` merged into one cluster and **fabricated cross-source corroboration** (`_num_sources ≥ 2` → could reach must-read). Fix: `url_match = bool(normalized_url) and normalized_url == rep_url` — empty URLs never match on the URL path (title-overlap still applies normally).

27. **`feed_doctor._chunk` could emit an empty / over-limit Discord chunk (2026-06-06)** — When a single report line exceeded the limit, `_chunk` appended an empty `""` chunk (Discord rejects empty content, 50006) and never split the over-long line (HTTP 400). Fix: `_chunk` no longer emits empty chunks and hard-splits any line longer than the limit. On-demand `--post` path only.

28. **`interests.json` failures crashed with a bare traceback (2026-06-06)** — `load_interests()` did an unguarded `json.load`, and `main()` used `interests['topics']` (hard index), so a missing/malformed config (it's hand-edited) failed the whole brief with an opaque error. Fix: `load_interests()` exits with a clear message on a missing / invalid-JSON / non-dict file, and `main()` uses `interests.get('topics', [])`.

29. **Title-only dedup misses same-story coverage when outlets use very different headlines (FIXED 2026-06-14)** — Three articles about Anthropic's Fable 5 / Mythos 5 suspension appeared in the same brief: the official Anthropic statement in must-read, and two news-report variants in maybe-relevant. Pairwise title token overlap was 15–35%, all well below the **40%** cross-source dedup threshold, so `deduplicate()` split them into 3 clusters. The send-time safety net (`dedup_mr_vs_may` / `dedup_within_may`) also uses the same 40% threshold and failed to catch them.

    **Root cause:** Title-only Jaccard overlap cannot distinguish same-story-from-different-outlets from different-story-when-headlines-diverge. An official statement ("Statement on the US government directive to suspend access to Fable 5 and Mythos") shares only 15% title tokens with a news report ("Anthropic shuts down Fable, Mythos models following Trump admin directive") despite being the same story.

    **Fix:** Added `content_similarity()` as a secondary signal in `send_brief.py::dedup_mr_vs_may` and `dedup_within_may`. Content overlap (stopwords-removed Jaccard on first 300 chars) at a **12%** threshold catches same-story variants while avoiding false positives:

    - **`_STOP_WORDS` frozenset** — 40+ common English stopwords filtered before computing Jaccard
    - **`_CONTENT_SIM_THRESHOLD = 0.12`** — empirically validated boundary; same-story overlap measures 15–27%, unrelated pairs measure 0%, same-company-different-story measures ~6%
    - **`content_similarity(art_a, art_b)`** — extracts `\w+` tokens from `content[:300]`, removes stopwords + single-char tokens, returns Jaccard

    Both dedup functions now use: `if title_ov > 0.40 or content_similarity(...) > 0.12:`. The DEDUP log prints which signal caught it (`via title@XX%` vs `via content@XX%`).

    **Validated against the 2026-06-14 real run data:** Both Fable 5 duplicate variants were caught by content similarity at 13% and 16%. Zero false positives across all MR×May pairs. Full reproduction in `references/content-dedup-analysis.md`.

30. **Same-story articles independently qualifying for must-read (FIXED 2026-06-26)** — Two articles about Anthropic's accusation against Alibaba (Ars Technica + Tom's Hardware) both appeared in must-read. Title overlap was only 8.3% (very different headline phrasings), so `deduplicate()` placed them in separate clusters. One qualified via ≥2 sources corroboration, the other via ≥3 keywords — both independently met the must-read gate. The Pitfall 29 fix only added content-similarity to `dedup_mr_vs_may` and `dedup_within_may` (both operate on maybe-relevant only), so must-read-vs-must-read duplicates were never checked.

    **Fix:** Added `dedup_within_mr()` — same two-signal approach (title overlap OR content similarity > 12%) applied to must-read articles before formatting. Keeps the highest-scoring article per cluster. Called after `dedup_mr_vs_may` and before `dedup_within_may`.

    **Compounding factor:** The Tom's Hardware article's RSS content was the author bio (HTML not fully stripped), so content similarity also failed in the real run. The fix works when both articles have proper content extraction; the Tom's Hardware extraction issue is a separate feed-quality problem.

31. **Future plc feeds return author bio as content[0] (FIXED 2026-06-26)** — Tom's Hardware (and other Future plc feeds like PC Gamer, TechRadar, etc.) structure their RSS `content` field as a list: `content[0]` = author bio (~200–500 chars), `content[1]` = actual article (1000+ chars, wrapped in `<article>` tag). The old code did `entry.content[0].value`, always grabbing the author bio. This caused: (a) articles scoring 0 on the paywall guard if bio <200 chars, or (b) articles matching fewer keywords (author bio lacks story-specific terms), or (c) content-similarity dedup failing because the compared text is biographical filler, not article content.

    **Fix:** `_extract_articles()` now picks the **longest** content entry: `max(entry.content, key=lambda c: len(c.value)).value`. Backwards-compatible — feeds with a single content entry are unaffected. Multi-entry feeds where content[0] is the real article (the common case) also work because the longest entry is still picked.

    **Diagnostic:** To check if a feed has this issue, run `python3 -c "import feedparser; f=feedparser.parse('FEED_URL'); e=f.entries[0]; print([(len(c.value), c.value[:60]) for c in e.content])"` — if the first entry is short and biographical, the fix applies.

