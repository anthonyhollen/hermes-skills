# Content-based dedup analysis (2026-06-14)

> **Update (2026-10-05, 1.13.0): past the lede, and chained members.** Two gaps showed up
> in the 9 briefs from Sep 27 to Oct 5:
>
> | Duplicate | title J | lede J | shared distinctive | gap |
> |---|---|---|---|---|
> | StarCraft cheat (PC Gamer ↔ Verge), same day Oct 5 | 0.26 | 0.03 | 3 | commentary lede |
> | "Super Intelligence" (Slashdot ↔ PC Gamer), same day Oct 1 | 0.14 | 0.015 | 2 | commentary lede |
> | macOS Full Disk Access (MacRumors Oct 3 → 9to5Mac Oct 4) | 0.18 | 0.16 | 1 | chained member not recorded |
> | Gemini 4 Argon (Ars Oct 1 → Slashdot Oct 2) | 0.18 | 0.12 | 2 | chained via Techmeme (inferred) |
>
> **Commentary ledes.** PC Gamer (and some Ars and Verge pieces) open with an editorial
> paragraph, so the 300-char lede shares nothing with the wire copy. At 1,000 chars the
> StarCraft pair overlaps 0.19.
>
> **Pairs near the threshold** (2026-10-05 capture: 797 fresh usable articles, 47,733 pairs
> sharing a title word):
>
> | Pair | Overlap | Published apart | Verdict |
> |---|---|---|---|
> | Shield TV ×2 | 0.206 | 21.4 h | same story |
> | Capcom | 0.198 | 6.1 h | same story |
> | StarCraft (Verge) | 0.189 | 4.5 h | same story |
> | StarCraft (Slashdot) | 0.171 | 14.0 h | same story |
> | Nova Lake | 0.149 | 49.9 h | follow-up |
> | Deadlock | 0.147 | 17.5 h | follow-up |
> | Arc Raiders | 0.139 | 18.4 h | same story, missed |
> | "Xbox is not for sale" | 0.128 | 12.6 h | same story, missed |
> | MacRumors "Top Stories" round-up vs HomePad | 0.117 | — | different |
>
> An adversarial reviewer wrote realistic follow-ups (a new development plus a 2–3
> sentence recap); they scored 0.15–0.33. That is why the deep path needs a publication gap
> of ≤ 36 h and a cutoff of 0.16, midway between 0.147 and 0.171. It fixes **same-day**
> duplicates; next-day ones are the hold-back's job (weak, one run).
>
> Rejected:
> - **Rare shared title words** — matched on "trying", "anymore", "hack", "generated",
>   "IPO", "DGX".
> - **≥ 2 shared rare body names with 1 title word** — Apple Watch vs Garmin; a Richard Ho
>   Q&A vs a Tom's Premium round-up.
> - **One title word at any depth** — Halide vs Final Cut Camera 0.23; "local", "risk".
>
> Rare body names (≥ 3, with ≥ 2 title words) were as clean as the deep path but need
> per-fetch document frequencies, so they weren't adopted.
>
> **Chained members.** A cluster member that matches a direct twin but not the shown copy
> was hidden for the day but never recorded. The next day the hold-back matched it against
> the recorded twins, but graded it an echo: it was published before they were recorded,
> and the one-run tier only holds articles published after. On the 2026-10-05 capture, 7
> members sat one step out. The 2 that were different stories (MacBook OLED rumours in the
> Ternus cluster) both linked through a Techmeme summary that names several stories. The
> review fan-out then found more bad chains:
> - 4 of the 5 survivors came from the rep's own outlet;
> - links through the 8–12% lede band (GTA 5 zombies under a GTA 6 story);
> - a sequel of the rep chained through a launch story.
>
> So the identity-only `kind: chained` record now needs firm links (`firm_story_f`), with
> no aggregator, round-up or promo along the way, and is never made for a same-outlet,
> sequel, aggregator or round-up member. That leaves exactly the 9to5Mac case on the
> capture.

> **Correction (2026-09-26, 1.10.0):** the 12% content threshold below was validated on 15
> pairs from one run, and the "false positive count at 12%: ZERO" claim did not hold. On a
> full 175-article pool about half the content matches above 12% were different stories —
> same-outlet boilerplate (MacRumors' "…spotted by MacRumors contributor Aaron Perris" put
> "iPad mini 8" vs "HomePod mini 2" at 36%), undecoded HTML entities (`nbsp`, `ldquo` counted
> as shared words) and generic launch vocabulary. The dedup is now `article_id.same_story`
> (entity decoding, per-source boilerplate stripping, a shared-title-word gate, a strict
> same-outlet rule, a sequel veto) — see CLAUDE.md "Same-story detection & dedup" and
> CHANGELOG 1.10.0. The Fable-5 trio below still merges (13.6% / 12.8% / 17.1%).

## Problem

Title-only token overlap at 40% cannot cluster same-story articles when outlets use very different headline phrasings. On 2026-06-14, three articles about **Anthropic's Fable 5/Mythos 5 suspension** appeared in the morning brief:

| Section | Title | Source |
|---------|-------|--------|
| Must-read | `Statement on the US government directive to suspend access to Fable 5 and Mythos` | Anthropic blog (A-tier) |
| Maybe[0] | `US government forces Anthropic to disable Claude Fable 5 and Mythos 5 for all cu` | News outlet |
| Maybe[1] | `Anthropic shuts down Fable, Mythos models following Trump admin directive` | News outlet |

### Title token overlap (all below 40% threshold)

| Pair | Jaccard | Verdict |
|------|---------|---------|
| Must-read vs Maybe[0] | 35.0% | ❌ below 40% — NOT merged |
| Must-read vs Maybe[1] | 15.0% | ❌ below 40% — NOT merged |
| Maybe[0] vs Maybe[1] | 14.3% | ❌ below 40% — NOT merged |

**Pipeline failure chain:**
1. `fetch_rss.py::deduplicate` — 40% threshold, all pairs below → 3 separate clusters
2. `classify()` — Anthropic blog (A-tier, high kw count) → must-read; news reports (fewer kw/sources) → maybe-relevant
3. `send_brief.py::dedup_mr_vs_may(40%)` — same 40% threshold → no cross-section catch
4. `send_brief.py::dedup_within_may(40%)` — same 40% → no within-maybe catch

## Validation: content overlap as a fix

Content tokens (stopwords removed, `\w+` regex, first 300 chars). Stopwords filtered: the, a, an, to, in, on, of, for, and, with, that, this, is, are, was, were, has, have, had, been, will, would, could, its, their, his, her, it, they, from, by, as, at, after, over, than, more, also, said, but, not, all.

### Same-story content (different outlets, actual articles from 2026-06-14)

```
Anthropic statement (Simon Willison recap):
"Statement on the US government directive to suspend access to Fable 5 and Mythos 5
 Well this is nuts : The US government, citing national security authorities, has
 issued an export control directive to suspend all access to Fable 5 and Mythos 5
 by any foreign national, whether inside or outside the United States, including
 foreign national Anthropic employees..."

The Decoder news report:
"The US government has ordered Anthropic to shut down global access to Fable 5 and
 Mythos 5, citing alleged jailbreak risks. Anthropic is complying but pushing back
 publicly: the vulnerabilities are minor and exist in competing models like GPT-5.5,
 the company says..."

Ars Technica report:
"Anthropic completely shut off access to its Mythos 5 and Fable 5 models Friday
 night, just days after they were launched. The move comes after Anthropic's receipt
 of a US Commerce Department directive Friday evening..."
```

**Real content overlap (stopwords removed):** MR statement vs The Decoder = **13.0%**, MR vs Ars = **15.6%**, Decoder vs Ars = **13.7%**.

### Simulated same-story content (synthetic, for methodology validation)

```
Outlet A: "Anthropic has suspended all access to their Fable 5 and Mythos 5 models
           following a critical security vulnerability the company announced the
           suspension earlier today..."
Outlet B: "Anthropic Fable 5 and Mythos 5 models have been suspended due to a critical
           safety flaw found during an internal audit..."
```

**Simulated content overlap (stopwords removed): 27.3%** — the synthetic examples are longer and have more shared tokens. Real articles are shorter and more varied, hence the lower actual overlap of 13–16%.

### Unrelated stories (baseline, validated with 2026-06-14 real data)

All 15 MR×May pairs from the actual run:

| MR article | May article | Content overlap | Verdict |
|-----------|-------------|----------------|---------|
| Anthropic Fable 5 statement | OpenAI WebRTC Audio | 0.0% | Correctly preserved |
| Anthropic Fable 5 statement | Apple devices support | 0.0% | Correctly preserved |
| Anthropic Fable 5 statement | iOS 27 Siri app | 0.0% | Correctly preserved |
| OpenAI investigation | OpenAI WebRTC Audio | 1.7% | Correctly preserved |
| OpenAI investigation | Anthropic Fable 5 (Decoder) | 1.8% | Correctly preserved |
| OpenAI investigation | Apple devices support | 0.0% | Correctly preserved |
| OpenAI investigation | iOS 27 Siri app | 1.9% | Correctly preserved |
| OpenAI investigation | Anthropic Fable 5 (Ars) | 1.8% | Correctly preserved |
| Claude Fable 5 benchmark | OpenAI WebRTC Audio | 5.1% | Correctly preserved |
| Claude Fable 5 benchmark | Apple devices support | 0.0% | Correctly preserved |
| Claude Fable 5 benchmark | iOS 27 Siri app | 0.0% | Correctly preserved |
| Claude Fable 5 benchmark | Anthropic Fable 5 (Ars) | 3.4% | Correctly preserved |

**Worst-case unrelated pair:** 5.1% (Claude Fable 5 benchmark vs OpenAI WebRTC — both are AI-related, both mention Anthropic/OpenAI, still well below 12% threshold)

### Same-company, different story (additional validation)

Anthropic partnership announcement vs Anthropic Fable 5 suspension: **6.1%** — well below 12%.

The **12% threshold** has a 6–7% safety margin above the worst-case unrelated pair, so false positives are not a practical concern.

### Real headlines from the 2026-06-14 run (the actual bug)

| # | Title | Source | Section |
|---|-------|--------|---------|
| A | `Statement on the US government directive to suspend access to Fable 5 and Mythos` | Simon Willison | Must-read |
| B | `US government forces Anthropic to disable Claude Fable 5 and Mythos 5 for all cu` | The Decoder | Maybe-relevant |
| C | `Anthropic shuts down Fable, Mythos models following Trump admin directive` | Ars Technica | Maybe-relevant |

| Pair | Title overlap | Content overlap | Merged at 40%? | Caught by content? |
|------|-------------|----------------|----------------|-------------------|
| A vs B | 35.0% | **13.0%** | ❌ | ✅ at 12% threshold |
| A vs C | 15.0% | **15.6%** | ❌ | ✅ at 12% threshold |
| B vs C | 14.3% | **13.7%** | ❌ | ✅ at 12% threshold (within-may) |

All three pairs correctly flagged as duplicates by the content-based secondary signal.

### All predicted headline variants below 40% (synthetic simulation)

9 of 15 plausible same-story headline pairs fall below the 40% title threshold:

| Pair | Title overlap |
|------|-------------|
| [0] vs [5] (Anthropic Suspends... / Fable 5 and Mythos 5 Models Suspended...) | 40.0% |
| [1] vs [2] (Temporarily Suspends... / Halts Access to...) | 29.4% |
| [1] vs [3] (Temporarily Suspends... / Breaking: Suspended —) | 35.7% |
| [1] vs [5] (Temporarily Suspends... / Models Suspended by...) | 31.2% |
| [2] vs [3] (Halts Access to... / Breaking: Suspended —) | 31.2% |
| [2] vs [4] (Halts Access to... / Suspends Mythos 5 and Fable 5 Access...) | 37.5% |
| [2] vs [5] (Halts Access to... / Models Suspended by...) | 35.3% |
| [3] vs [4] (Breaking: Suspended — / Suspends Mythos 5 and Fable 5 Access...) | 35.7% |
| [4] vs [5] (Suspends Mythos 5 and Fable 5 Access... / Models Suspended by...) | 31.2% |

Headlines used:
- [0] `Anthropic Suspends All Access to Fable 5 and Mythos 5 Models`
- [1] `Anthropic Temporarily Suspends Fable 5 and Mythos 5 Over Safety Concerns`
- [2] `Anthropic Halts Access to Fable 5 and Mythos 5 After Critical Security Loophole`
- [3] `Breaking: Fable 5 and Mythos 5 Suspended — Anthropic Pulls Models`
- [4] `Anthropic Suspends Mythos 5 and Fable 5 Access Amid Safety Flaws`
- [5] `Fable 5 and Mythos 5 Models Suspended by Anthropic Following Security Issue`

## Actual threshold: 12% (not 15%)

When validated against real articles from the same run, some same-story content pairs showed lower overlap than the synthetic simulation predicted. The must-read (Anthropic official statement, very short content at ~130 chars) vs The Decoder news report had only **13%** content overlap — below a 15% threshold. The threshold was lowered to **12%** and re-validated:

| Pair | Content overlap | Caught at 12%? |
|------|----------------|----------------|
| Must-read vs Maybe[0] (same story — The Decoder) | 13.0% | ✅ YES |
| Must-read vs Maybe[1] (same story — Ars Technica) | 15.6% | ✅ YES |
| Maybe[0] vs Maybe[1] (same story, within-may) | 13.7% | ✅ YES |
| Worst-case unrelated pair | 5.1% (OpenAI WebRTC vs Claude Fable 5 benchmark) | ❌ NO — correctly preserved |
| Same-company diff story (Anthropic partnership vs suspension) | 6.1% | ❌ NO — correctly preserved |

**False positive count at 12%: ZERO** across all 15 MR×May pairs from the 2026-06-14 run.

## Actual implementation

Rather than the proposed `content_overlap` helper, the fix adds:

1. A `_STOP_WORDS` frozenset (40+ English stopwords) and `_CONTENT_SIM_THRESHOLD = 0.12` constant
2. A `content_similarity(art_a, art_b) -> float` function using `re.findall(r"\w+")` on `content[:300]`, removing stopwords and single-char tokens
3. Modified `dedup_mr_vs_may` condition: `if title_ov > threshold or content_similarity(mr_art, may_art) > _CONTENT_SIM_THRESHOLD:`
4. Modified `dedup_within_may` condition: same two-signal approach
5. DEDUP log shows which signal caught it: `via content@13%`

**Validated dry-run:** The two Fable 5 duplicate variants were dropped. All other articles preserved. Footer math balanced.

## Coverage gap: within-must-read dedup (fixed 2026-06-26)

The Pitfall 29 fix added `content_similarity()` to `dedup_mr_vs_may` and `dedup_within_may` — both operate on **maybe-relevant** articles only. When two same-story articles independently qualify for must-read (one via ≥2 sources, the other via ≥3 keywords) with title overlap below 40%, no dedup function catches them.

**Bug 30 (2026-06-26):** Ars Technica + Tom's Hardware articles about Anthropic's Alibaba accusation. Title overlap: 8.3%. Both landed in must-read. Tom's Hardware content was author bio (0% content similarity), so even the secondary signal failed.

**Fix:** Added `dedup_within_mr()` — same two-signal approach applied to must-read before formatting. See SKILL.md Pitfall 30 and `references/session-fixes.md` Bug 30.

**Lesson:** When adding a dedup signal, apply it to ALL dedup functions in the chain. Audit: `deduplicate()` (fetch-time) → `dedup_mr_vs_may` (cross-section) → `dedup_within_mr` (within MR) → `dedup_within_may` (within MR).
