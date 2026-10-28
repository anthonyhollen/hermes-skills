# Xiaomi MiMo Token Plan — Pricing & Analysis (May 2026)

> **Status update May 27, 2026** — MiMo Token Plan is usable for general API/agent workloads (including Hermes Agent), confirmed by ham via direct MiMo support inquiry. The "coding-tools-only" restriction in published docs does NOT apply to all account tiers — **verify with MiMo support for your own account.**

---

## Packages

| Package | Monthly | Annual (first purchase -12%) | Monthly Credits |
|---|---|---|---|
| **Lite** | $6 | ~$5.28 | 4.1B |
| **Standard** | $16 | ~$14.08 | 11B |
| **Pro** | $50 | ~$44 | 38B |
| **Max** | $100 | ~$88 | 82B |

## Credit Consumption Rates (per Million Tokens)

| Model | Cache Hit Input | Cache Miss Input | Output |
|---|---|---|---|
| **MiMo-V2.5** | 2/M | 100/M | 200/M |
| **MiMo-V2.5-Pro** | 2.5/M | 300/M | 600/M |
| MiMo-V2-Pro | 140/M | 700/M | 2100/M |
| MiMo-V2-Omni | 56/M | 280/M | 1400/M |

**TTS series models are FREE** and do not consume package credits.

---

## Credit Calculation — ham's usage (May 16-27 baseline × 30)

**Usage:** new_input=28.5M/mo, cache_read=215M/mo, output=2.4M/mo | Hit rate: 74.7%

```python
new_input = 28.52e6      # brand-new input tokens/month
cr_total  = 214.98e6     # cache reads (accumulated context from prior turns)
out_tokens = 2.36e6      # output tokens/month
hit_rate = 0.747         # 74.7% (ham's MiMo dashboard rate)

# ALL cache_read = existing context hits (bill at hit rate)
# new input split by hit/miss rate
hit_portion  = cr_total + (new_input * hit_rate)    # 236.3M total hit tokens at 74.7%
miss_portion = new_input * (1 - hit_rate)            # 7.22M miss tokens at 74.7%

# MiMo-V2.5: hit=2, miss=100, output=200
v25_credits = (hit_portion * 2) + (miss_portion * 100) + (out_tokens * 200)
# = (236.3M × 2) + (7.22M × 100) + (2.36M × 200)
# = 472.7M + 722M + 472M = ~1,515M credits/month

# MiMo-V2.5-Pro: hit=2.5, miss=300, output=600
v25p_credits = (hit_portion * 2.5) + (miss_portion * 300) + (out_tokens * 600)
# = (236.3M × 2.5) + (7.22M × 300) + (2.36M × 600)
# = 590.8M + 2,166M + 1,416M = ~3,582M credits/month
```

---

## Plan Viability (ham's usage)

| Model | Hit Rate | Credits/Month | Lite (4.1B) | Standard (11B) | Pro (38B) |
|---|---|---|---|---|---|
| MiMo-V2.5 | 87% | ~1,200M | ~3.4 months ⚠️ | ~9.2 months ✅ | overkill |
| MiMo-V2.5 | 74.7% | **~1,515M** | ~2.7 months ❌ | ~7.3 months ✅ | overkill |
| MiMo-V2.5-Pro | 87% | ~2,870M | ~1.4 months ❌ | ~3.8 months ⚠️ | overkill |
| MiMo-V2.5-Pro | 74.7% | **~3,582M** | ~1.1 months ❌ | ~3.1 months ⚠️ | ~10.6 months ✅ |

## Annual Cost (ham's usage — May 16-27 × 30)

### MiMo-V2.5 (74.7% hit → 1,515M credits/mo)
- **Lite fails** — 4.1B / Model | Plan Needed | Annual Cost1,515M = 2.7 months (expires before use-up)
- Standard ($16/11B): 11B / 1,515M = 7.3 months → buy ~1.7×/yr → **~$27/yr**
- Cheapest viable: **~$27/yr via Standard annual** (~$16 every ~7 months)

### MiMo-V2.5-Pro (74.7% hit → 3,582M credits/mo)
- Lite fails (1.1 months); Standard barely sufficient (3.1 months — tight)
- Standard ($16/11B): 11B / 3,582M = 3.1 months → buy ~3.9×/yr → **~$62/yr**
- Pro ($50/38B): 38B / 3,582M = 10.6 months → overkill
- Cheapest viable: **~$62/yr via Standard annual** (~$16 every ~3 months)

### At 87% hit rate

| Model | Hit Rate | Plan Needed | Annual Cost | vs MiniMax ($120/yr) |
|---|---|---|---|---|
| V2.5 | 87% | Lite ($6) | **~$21/yr** ⭐ | Save ~$99/yr |
| V2.5 | 74.7% | Standard ($16) | **~$27/yr** | Save ~$93/yr |
| V2.5-Pro | 87% | Standard ($16) | **~$49/yr** | Save ~$71/yr |
| V2.5-Pro | 74.7% | Standard ($16) | ~$62/yr | Save ~$58/yr |
| V2.5-Pro | 74.7% | Pro ($50) | ~$550/yr | ❌ 4.6× more than MiniMax |
| MiniMax M2.7 | — | $10/mo flat | **$120/yr** | baseline |

**Recommendation: MiMo-V2.5 with Standard plan** — Pro's 3× higher cache_miss and output rates make it far more expensive. At 74.7% hit rate, V2.5-Pro costs 2.3× more than V2.5 for the same workload.

---

## Discount Stacking Rules

1. **First purchase** (12% off) — once per account, cannot combine with auto-renewal discount
2. **First auto-renewal** (23% new / 30% existing) — mutually exclusive with first purchase discount
3. **Consecutive annual** (88% cheaper than 12× monthly) — does not combine with first purchase/auto-renewal
4. **Nighttime** (0.8×) — stacks with all other discounts (Beijing Time 0:00–8:00)

---

## Supported Base URLs

**OpenAI-compatible endpoints:**
- China: `https://token-plan-cn.xiaomimimo.com/v1`
- Singapore: `https://token-plan-sgp.xiaomimimo.com/v1`
- Europe: `https://token-plan-ams.xiaomimimo.com/v1`

**Anthropic-compatible endpoints:**
- China: `https://token-plan-cn.xiaomimimo.com/anthropic`
- Singapore: `https://token-plan-sgp.xiaomimimo.com/anthropic`
- Europe: `https://token-plan-ams.xiaomimimo.com/anthropic`

---

*Source: https://platform.xiaomimimo.com/docs/en-US/price/tokenplan/subscription (verified May 27, 2026)*
