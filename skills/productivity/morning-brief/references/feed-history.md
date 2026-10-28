# Feed library history (relocated from SKILL.md 2026-09-14)

Archival provenance for `scripts/feeds.py`. Read when adding/removing a feed or wondering
why a source is absent — the "we already tried that" record. The live list is `feeds.py`;
this file is history only.

## Feed churn 2026-05-31
Probed all feeds, pruned dead ones, then sourced working replacements:
- **Removed, unrecoverable:** The Batch (no RSS), Last Week in AI / DPReview / The Next Web
  (hard Cloudflare 403), AI Insider (timeout). InsideEVs and the mainstream SG outlets
  (Straits Times, CNA, Today Online) work but serve only <200-char teasers → zeroed by the
  paywall guard, so not re-added.
- **Recovered with corrected URLs / browser UA:** VentureBeat AI (`/category/ai/feed/`),
  MarkTechPost (needed the browser UA).
- **New, full-content:** The Decoder + AI News (smol.ai) for AI; Vulcan Post + The
  Independent SG for Singapore (both full article text, unlike the teaser-only mainstream
  SG feeds).

## Browser User-Agent (2026-05-31)
`fetch_feed` sends a Chrome desktop UA, not a custom bot UA — several feeds (e.g.
MarkTechPost) 403 a bot UA via Cloudflare. Validated against a full fetch with no
regressions. **Still load-bearing — do not change it to a bot UA.**

## Feed churn 2026-09-05
- **ZDNet removed** — its feed 404s (4 consecutive failed runs) and all six live
  same-domain alternates (`/rss/news`, `/feed`, `/rss`, `/rss/`, `/?feed=rss2`) serve
  teaser-only content (~118–123 chars), which the paywall guard zeroes. Same disposition as
  InsideEVs and the mainstream SG outlets: working URL, unusable content.
- **AI News (smol.ai) removed** — HTTP 402 Payment Required (went paid); not recoverable
  without a subscription.
- **Added for LLM-release breaking news:** *The Verge AI* (A, AI-only cut of The Verge,
  686-char avg) and *Qwen Blog* (B, first-party open-weights lab, 606-char avg).
- **r/LocalLLaMA** probes fine solo but Reddit rate-limits per IP, so it non-deterministically
  429s against a second subreddit. It was **swapped in for r/programming** rather than added —
  Reddit serves only one subreddit feed per run from this IP. Do not re-add a second
  subreddit without re-testing.
- `BROAD_KEYWORDS` gained `Grok`/`Sora` — both collide with non-AI usage ("grok the
  codebase", Kingdom Hearts) so they only count alongside a specific topic.
- **VentureBeat AI (HTTP 429) and Digital Trends (HTTP 405) removed** — both still failed
  when probed solo, 3 days after their last success, so neither was a transient blip.

## Feed churn 2026-09-14 — Singapore category removed
**Mothership, Vulcan Post and The Independent SG were removed, and the `Singapore` topic was
dropped from `interests.json` and from `BROAD_KEYWORDS`.** Not a feed failure — all three
fetched fine and served full content. The category was *structurally unreachable*:
`Singapore` was the only interests topic those feeds could match, and it sat in
`BROAD_KEYWORDS`, which only counts a broad keyword when a *specific* topic also matches.
Measured over a live run: **19 raw `Singapore` keyword hits across the three feeds → 0 after
broad-keyword scoping → 0 matched**, and 0 SG-feed articles appeared in any of the 50
archived briefs.

Collateral, measured: 3 non-SG articles lost one keyword each when the topic went (a
MacRumors iOS-27 timezone list 12→6, a TechRadar Ted Lasso release-time piece 12→6, a
MarkTechPost item 6→5 kw) — all noise of exactly the kind the scoping rule exists to remove.

**To restore SG coverage you must do BOTH:** re-add the feeds *and* give them a non-broad
topic to match on (or remove `Singapore` from `BROAD_KEYWORDS` and accept the
human-interest noise A4 was introduced to kill).

## Zero-yield removals 2026-09-15 — Hugging Face Blog, CNET, Engadget, BBC World, Hacker News

A feed that fetches perfectly can still be useless: if its RSS body is under the 200-char
paywall guard, every entry scores 0 and it contributes **nothing**. Fetch-only health tracking
called these healthy forever; `consecutive_zero_yield` (added 1.9.0) surfaced them, and
`feed_doctor.py --name <feed>` confirmed there is no drop-in replacement for any of them.
38 → 33 feeds; ~950 fetched entries/run removed for zero loss of content.

| Feed | Tier | Per run | Body | feed_doctor verdict |
|------|------|---------|------|---------------------|
| Hugging Face Blog | A | 862 entries, ~254 KB | **0 chars** — `content`/`summary`/`description` all empty at source | 11 probed; only FULL candidate was `simonwillison.net`, already subscribed. OpenAI news 147 chars, Google AI 98, DeepMind 21 — all teasers |
| CNET | A | 25 entries | ~84 chars | 5 same-domain alternates, all 84 chars |
| Engadget | A | 20 entries | ~116 chars | 4 same-domain alternates, all 116 chars |
| BBC World | A | 33 entries | 0 usable | 11 probed, none working |
| Hacker News | B | 30 entries | none — link aggregator, no body by design | 9 probed, none working; a full-content HN feed would need a third-party scraper |

Notes for whoever revisits this:
- **These are teaser-only by publisher policy, not misconfiguration.** Re-adding the same URL
  will reproduce the same zero contribution. Only re-add with a probe showing ≥200-char bodies.
- **`general` is now empty** — there is no world-news source. Deliberate (BBC World was
  already contributing nothing), but it is a real coverage gap: add a full-content world-news
  feed as `general` to close it.
- **Techmeme covers Hacker News's slot** ("what is the industry talking about") and does serve
  content, which is why HN's removal isn't a signal loss.
- Wired survives but is marginal — it served 2 usable of 50 entries on the measured day. Watch
  its `consecutive_zero_yield`.
- `feed_health.json` prunes records for feeds no longer in `feeds.py` automatically on the next
  run, so no manual cleanup is needed.
