# Performance notes (relocated from SKILL.md 2026-09-14)

## Measured profile (2026-09-14, warm, 38 feeds / ~3,300 articles)
| Stage | Cost | Note |
|-------|------|------|
| `import fetch_rss` | 0.14 s | — |
| fetch all feeds (network) | ~5 s | dominates stage 1 |
| `match_articles` | ~1.5 s | 3,300 articles × ~40 keyword patterns |
| `deduplicate` | 0.03 s | O(n²) but only over ~190 matched |
| `classify` | <1 ms | — |
| LLM summarize (8 concurrent) | ~5 s | one wave |
| Discord send | ~2 s | 6–9 POSTs |
| **whole pipeline** | **~15 s** | vs a 560 s step budget / 600 s watchdog |

**Conclusion: there is no meaningful bottleneck to cut.** The run uses ~3% of its budget.
The honest finding is "nothing to optimize" — the two concurrency caps below were already
right, and the CPU stages are noise next to the network. Effort went into *bounding* the
pathological cases instead (see the step budgets in SKILL.md), which is where the real
risk was.

## Tunables and why they are what they are
- **`MAX_WORKERS` (fetch)** — raised to `len(FEEDS)` at runtime (ceiling 48) so all feeds
  fetch in ONE wave. This is a *correctness* choice as much as a speed one: two waves
  doubled the worst-case wall clock to a measured 192 s against a 120 s step timeout.
- **`CONCURRENT_LIMIT = 8` (summaries)** — equals the max articles per run (3 must-read +
  5 maybe), so all summaries issue in a single concurrent wave; 5 forced two waves and an
  extra full LLM round-trip.
- **`match_articles` single-pass** — it used to compute the keyword match and parse the
  date, then call `score_article`, which recomputed *both*. It now does one keyword pass +
  one date parse and inlines the score. ~28% faster over a 3,843-article corpus
  (3.24 s → 2.35 s). `score_article` is kept as the readable definition of the formula —
  **if you change one, change both.**
- **`deduplicate` token cache** — each cluster caches its representative's normalized URL
  and title-token set instead of re-tokenizing on every O(n²) comparison (~60% faster on a
  large matched set; sub-ms at normal volume).
- **`MAX_ENTRIES_PER_FEED = 1500`** — set deliberately ABOVE the largest real feed (OpenAI
  Blog, 1,193 entries) so it never bites in practice. It exists only to bound a
  pathological feed: 20k entries parsed in 3.5 s but pushed `deduplicate` to ~28 s at 8k
  matched and past the step timeout beyond that. An earlier value of 400 bounded the same
  risk but distorted the feed-health `usable` count (228 → 10 for OpenAI Blog), which is
  why the cap is high and `usable` is counted *pre-cap*.

## Prompt tokens (runtime LLM cost)
~3.8 K input tokens per run before the 2026-09-14 trim; the trim removed the article URL
(~144 chars/prompt, never used by the model) and replaced a no-op `content[:3000]` slice
(content is already capped at 2000 upstream) with real per-category caps
(`PROMPT_CONTENT_CHARS`: must-read 2000, maybe 1200). **Be honest about the scale: this is
hygiene, not a cost lever** — a daily 8-item cron is a few thousand tokens either way.
`max_tokens` does NOT bill, so it was left generous.

## 1.10.0 same-story clustering (2026-09-26)
`fetch_rss.deduplicate` compares only pairs that share a blocking key (`article_id.index_keys`:
url, non-stop title word, 4 story-word min-hashes) on features computed once per article
(`story_features`). Measured on a Mac: 1,200 candidates 0.5–0.7 s (the first 1.10 draft
re-tokenised every pair and took 18.8 s; the old title-only dedup 0.71 s); a real day has
~180–220 candidates. The hold-back compares ~200 candidates × ~64 ledger entries.

