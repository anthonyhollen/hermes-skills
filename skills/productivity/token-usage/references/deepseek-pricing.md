# DeepSeek API Pricing (authoritative snapshot)

> Source: https://api-docs.deepseek.com/quick_start/pricing
> Verified: 2026-05-27

## Current Models (V4 Family)

### DeepSeek V4 Flash
| Token Type | Price per Million |
|---|---|
| Input (cache hit) | $0.0028 |
| Input (cache miss) | $0.14 |
| Output | $0.28 |

- **1M context**, 2500 concurrency
- Supports non-thinking + thinking modes, tool calls, JSON output, Chat Prefix Completion
- No promo/discount — flat rate from Apr 2026 announcement

### DeepSeek V4 Pro
| Token Type | Price per Million | Full Price (after May 31, 2026) |
|---|---|---|
| Input (cache hit) | **$0.003625** (75% promo) | $0.0145 |
| Input (cache miss) | **$0.435** (75% promo) | $1.74 |
| Output | **$0.87** (75% promo) | $3.48 |

- **⚠️ 75% PROMO discount ends May 31, 2026** — price reverts to full column on June 1.
  During promo: $183/yr. After promo: **$731/yr** (on ham's projected usage).
- **1M context**, 500 concurrency, max output 384K
- Supports non-thinking + thinking modes, tool calls, JSON output, Chat Prefix Completion

### V4 Flash vs V4 Pro — Cost Comparison at 87% Cache Hit (ham's rate)

On ham's projected monthly usage (May 16-27 baseline × 30):
- New input: 28.5M/mo | Cache read: 215M/mo | Output: 2.4M/mo

| Model | Cache Hit Cost | Cache Miss Cost | Output Cost | Monthly | Annual (promo / full) |
|---|---|---|---|---|---|
| V4 Flash | $0.60 | $3.99 | $0.66 | $5.26 | **$63/yr** |
| V4 Pro | $0.78 | $12.41 | $2.05 | $15.24 | **$183/yr** / **$731/yr** |

**V4 Pro is 2.9× more expensive during promo. After May 31 it is 11.6× more expensive.** Unless the user needs Pro's thinking mode or 384K max output, V4 Flash is the clear winner.

## Legacy Models (still available — deprecated July 24, 2026)

### DeepSeek R1 / Reasoner (deepseek-reasoner)
- Input: $0.55/M | Output: $2.19/M

### DeepSeek V3 / Chat (deepseek-chat)
- Input: $0.14/M | Output: $0.28/M

> ⚠️ Both deprecated July 24, 2026 — migrate to V4 Flash/Pro before then.

## Hermes Integration

The file `agent/usage_pricing.py` in the hermes-agent codebase has `_OFFICIAL_DOCS_PRICING` entries for:
- `deepseek-chat` → input $0.14, output $0.28
- `deepseek-reasoner` → input $0.55, output $2.19
- `deepseek-v4-pro` → input $1.74, output $3.48, cache_read $0.0145
- `deepseek-v4-flash` → input $0.14, output $0.28, cache_hit $0.0028

> `estimated_cost_usd` in `state.db` may be $0 for DeepSeek Flash/Pro models if pricing isn't in the table — always recalculate from actual usage + current rates when doing cost comparisons.

## Cache Hit Rate Guidance

When the user provides a cache hit rate (e.g., "87%"), use it directly for cost projections:
```python
hit_rate = 0.87   # ham's actual DeepSeek rate
miss_rate = 1 - hit_rate

cache_hit_cost  = cache_hit_tokens  * price_per_million / 1e6
cache_miss_cost = cache_miss_tokens * price_per_million / 1e6
output_cost     = output_tokens     * price_per_million / 1e6
```
Do NOT derive cache hit rate from `cache_read_tokens / input_tokens` — on Hermes it produces >100% ratios due to multi-turn context accumulation.
