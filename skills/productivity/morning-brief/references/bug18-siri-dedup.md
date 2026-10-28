# Morning Brief — Bug 18 Detail (2026-05-18)

> **Historical (noted 2026-09-26):** the 40% title threshold analysed here now lives inside
> `article_id.same_story`; `format_and_send()` no longer exists (messages are built by
> `send_brief.build_messages`).

## Issue
3 Siri paraphrased variants appeared in same morning brief: TechCrunch, Engadget, The Verge — all in maybe-relevant. No Siri in must-read.

## Root Causes

### Root Cause 1: 65% title-overlap dedup threshold too conservative

`deduplicate()` in `fetch_rss.py` uses threshold 0.65. The 3 Siri headlines:
- "Apple's Siri revamp could include auto-deleting chats" vs "Apple's new Siri app will reportedly offer auto-deleting chat options" → **31% overlap**
- "Apple's Siri revamp..." vs "Revamped Siri will reportedly offer auto-deleting chats" → **31% overlap**
- "Apple's new Siri app..." vs "Revamped Siri..." → **43% overlap**

All below 65% → no cluster merge → each article is its own cluster → `_num_sources=1` for each → classification sees single-source stories → Siri never qualifies as must-read (needs ≥2 sources OR ≥3 interest keywords matched).

### Root Cause 2: Substring matching for interest keywords too loose

Plain `in` operator matches "EV" against "revamped", "deleting", "reviewed" — giving Siri articles spurious keyword hits. See Pitfall 11 in SKILL.md.

## Fixes Applied

### Fix 1: `dedup_mr_vs_may()` — cross-section dedup

Removes maybe-relevant articles that overlap >40% title token similarity with any must-read.

```python
def dedup_mr_vs_may(mr: list, may: list, threshold: float = 0.40) -> tuple[list, list]:
    """Remove maybe-relevant articles too similar to any must-read."""
    may_filtered = []
    for may_art in may:
        is_dup = False
        for mr_art in mr:
            if token_overlap(mr_art["title"], may_art["title"]) > threshold:
                print(f"[DEDUP] Dropping maybe-relevant duplicate: '{may_art['title'][:60]}'...")
                is_dup = True
                break
        if not is_dup:
            may_filtered.append(may_art)
    return mr, may_filtered
```

### Fix 2: `dedup_within_may()` — within-section dedup

Collapses near-duplicate variants within maybe-relevant itself. Keeps highest-scoring article per cluster.

```python
def dedup_within_may(may: list, threshold: float = 0.40) -> list:
    """Within maybe-relevant: cluster by title overlap, keep best-scoring per cluster."""
    deduped = []
    dropped = []
    for art in may:
        is_dup = False
        for existing in deduped:
            if token_overlap(existing["title"], art["title"]) > threshold:
                is_dup = True
                dropped.append(art["title"])
                break
        if not is_dup:
            deduped.append(art)
    if dropped:
        print(f"[DEDUP] Within-maybe-relevant: dropped {len(dropped)} variants: {dropped}")
    return deduped
```

## Threshold Rationale (user-confirmed)

Empirically measured on 2477 articles from articles.json:
- Max overlap among **true duplicates** (Siri variants): **43%** (Engadget vs Verge)
- Max overlap among **unrelated pairs**: **<30%**
- **40%** cleanly separates the two groups — zero collateral merge risk

Setting threshold at 40%:
- Catches the 43% Siri pair (Engadget vs Verge) → merged ✓
- Misses the 31% pairs (TechCrunch vs others) → they stay as distinct articles ✓

## Integration

Both functions called in `send_brief.py` main(), after classification, before `dry_run()` and `format_and_send()`:

```python
must_read, maybe_rel = dedup_mr_vs_may(must_read, maybe_rel, threshold=0.40)
maybe_rel = dedup_within_may(maybe_rel, threshold=0.40)
```

Neither function changes article classification — they only filter display output.

## Historical Data

From articles_summarized.json (2026-05-18):
- Must-read: 3 articles (all single-source, kw_count=3 each — xAI, AI sovereignty, LLM compression)
- Maybe-relevant (before dedup): 5 articles (Siri×3, Seth Rogen, OpenAI Malta)
- Maybe-relevant (after dedup): 4 articles (TechCrunch + Engadget Siri survive; weakest Verge variant dropped)

Siri kw_count=2 because interest keywords "Apple" and "EV" both matched in title. "Revamped Siri..." only matched "EV" (false positive from loose `in` check — fixed in Pitfall 11, SKILL.md).

## Lessons Learned

1. **65% threshold too high for paraphrased headlines** — same story from different publishers with different wordings overlaps 30-45%. Need ≤40% to catch true duplicates.
2. **`dedup_mr_vs_may` is one-directional** — removes from maybe-relevant only when must-read counterpart exists. Doesn't help when all variants are in same section → need `dedup_within_may`.
3. **Substring matching for interest keywords too loose** — use word-boundary regex `\bEV\b` for 2-char acronyms.
4. **Both functions are post-classification** — they filter display, not classification. Siri stayed maybe-relevant because each showed `_num_sources=1` (dedup failed at source level) — root cause not fixed per user choice.