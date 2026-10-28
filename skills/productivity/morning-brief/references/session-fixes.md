# Session Fixes — morning-brief

> **Historical (noted 2026-09-26):** superseded by `pitfalls-log.md` (same bugs, fuller
> write-ups) and `CHANGELOG.md`. Function names here (`format_section`, `score_article`
> returning 0.0, the old send-time dedup thresholds) may no longer exist.

Log of bugs found and fixed, grouped by session. Each entry has: date, symptoms, root cause, fix, and which file(s) changed.

## 2026-06-26: Duplicate must-read + Future plc content extraction

**Symptom:** Today's morning brief had two articles about the same story (Anthropic accusing Alibaba of Claude cloning) both appearing in must-read — Ars Technica and Tom's Hardware versions.

**Root cause (two-part):**

1. **No within-must-read dedup.** Title overlap was only 8.3% (different headline phrasings), so `deduplicate()` placed them in separate clusters. One qualified via ≥2 sources corroboration, the other via ≥3 keywords — both independently met the must-read gate. The Pitfall 29 fix (content-similarity secondary signal) only applied to `dedup_mr_vs_may` and `dedup_within_may` — both operate on maybe-relevant only.

2. **Tom's Hardware content was author bio.** Future plc feeds (Tom's Hardware, PC Gamer, TechRadar, etc.) put the author bio in `content[0]` and the actual article in `content[1]`. The old code did `entry.content[0].value`, so the content field was biographical filler. This meant: (a) content-similarity dedup got 0% (comparing Ars article text with Tom's HW author bio), and (b) keyword matching was degraded (author bio lacks story-specific terms).

**Fix:**
- `send_brief.py`: Added `dedup_within_mr()` — same two-signal approach (title overlap >40% OR content similarity >12%) applied to must-read articles before formatting. Keeps highest-scoring article per cluster. Called after `dedup_mr_vs_may` and before `dedup_within_may`.
- `fetch_rss.py`: Changed `_extract_articles()` to pick the longest content entry from the list: `max(entry.content, key=lambda c: len(c.value)).value`. Backwards-compatible with single-entry feeds.

**Files changed:** `scripts/send_brief.py`, `scripts/fetch_rss.py`, `SKILL.md` (Pitfalls 15, 30, 31), `CLAUDE.md`

**Note:** After the content fix, the Tom's Hardware article only matched 2 keywords (Anthropic, Claude) — below the maybe-relevant cutoff — so the duplicate was resolved even without `dedup_within_mr`. The function remains as a safety net for future cases.

---

## 2026-06-14: Title-only dedup misses paraphrased coverage

**Symptom:** Three articles about Anthropic's Fable 5 / Mythos 5 suspension in one brief — 1 must-read + 2 maybe-relevant. All same story, different outlets.

**Root cause:** Title token overlap was 15–35% (below 40% threshold). Content-similarity secondary signal didn't exist yet.

**Fix:** Added `content_similarity()` (stopwords-removed Jaccard on first 300 chars, 12% threshold) to `dedup_mr_vs_may` and `dedup_within_may`.

**Files changed:** `scripts/send_brief.py`, `SKILL.md` (Pitfall 29), `references/content-dedup-analysis.md`

---

## 2026-06-06: Multiple send-time failures

**Symptoms:** Brief aborted mid-send (429 rate-limit), Discord 50035 (message too long), malformed LLM responses not retried, empty URL dedup fabrication, feed_doctor chunking errors, bare traceback on bad interests.json.

**Root causes & fixes:**
- `post_message` did `sys.exit(1)` on any 4xx → added retry for 429/5xx with backoff (Pitfall 23)
- No content length cap → added `_cap_summary()` + `DISCORD_CONTENT_LIMIT=2000` (Pitfall 24)
- `_extract_chat` hard-indexed response → made `.get`-defensive, `call_model` catches `ValueError/KeyError/IndexError` (Pitfall 25)
- `normalize_url("")` returned `""` → empty URLs never match on URL path (Pitfall 26)
- `_chunk` emitted empty chunks / didn't split over-long lines (Pitfall 27)
- `load_interests()` unguarded `json.load` → graceful exit on bad config (Pitfall 28)

**Files changed:** `scripts/send_brief.py`, `scripts/feed_doctor.py`, `scripts/fetch_rss.py`

---

## 2026-06-01: Empty model responses

**Symptom:** Two articles showed "Summary unavailable" — reasoning model returned empty text block.

**Fix:** Retry empty responses, disable reasoning for summaries, add extractive fallback (Pitfall 22).

**Files changed:** `scripts/summarize.py`, `scripts/send_brief.py`

---

## 2026-05-31: Feed churn + keyword/dead-code fixes

**Fixes:** 13 dead feeds removed, new feeds added (Vulcan Post, The Independent SG, etc.), keyword word-boundary matching (Pitfall 11), freshness cutoff (Pitfall 18), corroboration threshold split (Pitfall 19), broad keyword scoping (Pitfall 20), per-source diversity cap (Pitfall 21).
>
> **Correction (2026-09-15):** Vulcan Post, The Independent SG and Mothership were REMOVED in the 1.9.0 tune-up. They fetched fine, but `Singapore` was the only interest topic they could match and it was a broad keyword, so they contributed 0 matched articles every run. See `references/feed-history.md`.

---

## 2026-05-30: Discord formatting + dedup issues

**Fixes:** Link preview suppression via angle brackets (Pitfall 7), double-bolding fix (Pitfall 8), section header once-only rule (Pitfall 9), post-classification dedup threshold (Pitfall 15), paywall guard (Pitfall 16), excerpt detection (Pitfall 17).
