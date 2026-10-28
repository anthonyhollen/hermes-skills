# MiniMax M2.7 Pricing — Verified from Hermes Codebase

> Source: `agent/usage_pricing.py` lines 489–507 — `_OFFICIAL_DOCS_PRICING` entry for minimax / minimax-cn + minimax-m2.7
> Verified: 2026-05-24

## Pricing Structure

| Token Type | Price per Million |
|---|---|
| Input | $0.30 |
| Output | $1.20 |
| Cache read | **Included in input price** — no separate line item |
| Cache write | **Included in input price** — no separate line item |

**Key implication:** On MiniMax M2.7, cache hits are free — the full input cost covers them. At 87% cache hit rate, only 13% of input tokens are billed at $0.30/M. Output is billed separately at $1.20/M.

## Why `cache_read_tokens` Can Exceed `input_tokens` by 5–10×

MiniMax M2.7 uses a prompt caching mechanism where the accumulated context (system prompt + conversation history) is reused across turns within a session. Each turn's `cache_read_tokens` reflects the tokens served from the prompt cache — this can be 5–10× the `input_tokens` for that turn because the cache includes the entire conversation history, not just the current turn's prompt.

This is expected behavior, not a data error. Do NOT compute cache hit rate as `cache_read_tokens / input_tokens`.

## At 87% Cache Hit Rate — Monthly Projections

For a month with ~67.4M projected input tokens:
- Cache reads: 67.4M × 0.87 = **58.7M** → FREE
- Non-cached input: 67.4M × 0.13 = **8.8M** → $0.30/M = **$2.63**
- Output: ~4.4M → $1.20/M = **$5.22**
- **MiniMax total: ~$7.85/mo**

DeepSeek V4 Flash at same 87%: ~$2.61/mo (cache at $0.0028/M dominates)

## Comparison vs Subscription

MiniMax subscription: **$10/month flat** (any usage)
- At 87% hit: $7.85 actual cost → **saves $2.15/mo**
- At 100% hit: ~$5.22 actual cost → **saves $4.78/mo**

DeepSeek V4 Flash at 87%: **~$2.61/mo** → saves $7.39/mo vs subscription

---

> _Relocated from token-usage SKILL.md (June 2026)._

## Artificial Analysis Performance Benchmarks (MiniMax-M2.7)

Source: https://artificialanalysis.ai/models/minimax-m2-7 (verified May 28, 2026)

MiniMax-M2.7 scores on the Artificial Analysis Intelligence Index across 87 open-weight large models:

| Metric | Value | Rank | Notes |
|---|---|---|---|
| **Intelligence Index** | 50 | **#7 / 87** | Well above class avg (30). Reasoning model. |
| **Speed** | 63 tok/sec | #21 / 87 | Faster than avg (60 tok/sec) |
| **Input Price** | $0.30/1M | #33 / 87 | Below avg ($0.40/1M) — good value |
| — Cache hit | $0.06/1M (-80%) | | Included in input price |
| **Output Price** | $1.20/1M | #40 / 87 | Near avg ($1.25/1M) |
| **Verbosity** | 87M output tokens | #54 / 87 | 2× class avg (42M) — very verbose |
| **Context Window** | 205k tokens | | ~307 A4 pages |
| **Total Parameters** | 230B | | |
| **Active Parameters** | 10B | | Per-token MoE activation |
| **License** | Non-commercial | | |

**AA Summary:** *"Amongst the leading models in intelligence and reasonably priced for its class. Faster than average, however very verbose."*

Eval cost (Intelligence Index): **$175.51** for 87M output tokens.

For AI-native performance comparisons (intelligence, speed, price, verbosity), cite these AA figures. For Hermes cost projections, use the Provider Cost Comparison table above.

