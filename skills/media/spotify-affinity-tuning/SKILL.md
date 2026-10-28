---
name: spotify-affinity-tuning
description: Tuning Spotify playlist refresh affinity scoring — window days, half-life, artist avoidance
---
# Spotify Affinity Tuning — Lessons Learned

## Context
User (ham) noticed that despite consciously avoiding CrocoBeat and IU for 2 weeks, their tracks still appeared in the Spotify playlist refresh top 20. Session date: May 28, 2026.

---

## Key System Parameters

| Parameter | Old Value | New Value | Purpose |
|-----------|-----------|-----------|---------|
| `RECENCY_HALF_LIFE_DAYS` | 14 | **7** | Controls how fast events decay: `0.5^(age_days/7)` |
| `WINDOW_DAYS` | 30 | **7** | Only count events within last N days |
| `LIKE_THRESHOLD` | 1.0 | 1.0 | Min affinity to enter liked pool |
| `DISLIKE_THRESHOLD` | -1.0 | -1.0 | Min negative affinity to block |

**File**: `~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src/scorer.py`

---

## Root Cause Analysis

### Why avoided artists persisted

| Artist | Last Play | Age at May 28 | Old Window (30d) | Old HL (14d) | Affinity Weight |
|--------|-----------|---------------|-------------------|--------------|-----------------|
| CrocoBeat | May 17 | 11 days | Inside (✅) | `0.5^(11/14)` = **0.58** | Still significant |
| IU | May 26 | 2 days | Inside (✅) | `0.5^(2/14)` = **0.91** | Near full weight |

Neither artist was outside the 30-day window. CrocoBeat's last play May 17 → 11 days old → still 58% weight with 14-day half-life.

### Why shorter window alone doesn't solve avoidance

Setting `WINDOW_DAYS=7` alone (without changing half-life) would exclude CrocoBeat (11d > 7d window) but IU's 2-day-old plays would still carry **0.91 multiplier**. The half-life is what controls *decay speed* — window just gates what's included.

### Why skip signals don't help much

Spotify does **not** expose explicit skip events to third-party apps. Skip signals are inferred from `ms_played / duration_ms` ratio — `<10%` = hard skip, `10-30%` = skip. This is noisy (Spotify sometimes records sub-4s playback artifacts during track transitions that look like hard skips). The avoidance signal is behavioral (not listening), not punitive (pressing skip).

---

## Combination Evaluation Matrix

| Window | HL | CrocoBeat | IU | Track@7d | Track@14d | CrocoExcl? |
|--------|----|-----------|----|----------|-----------|------------|
| 3 | 7 | 0.00 | 0.82 | 0.00 | 0.00 | ✅ | Too aggressive — kills discovery |
| **7** | **7** | **0.00** | **0.82** | **0.50** | **0.00** | **✅ BEST** |
| 7 | 14 | 0.00 | 0.91 | 0.71 | 0.00 | ✅ | IU decays too slowly |
| 14 | 7 | 0.34 | 0.82 | 0.50 | 0.25 | ❌ | CrocoBeat still included |
| 14 | 14 | 0.58 | 0.91 | 0.71 | 0.50 | ❌ | No meaningful change |
| 30 | 14 | 0.58 | 0.91 | 0.71 | 0.50 | ❌ | **OLD SETTINGS** |

**Winner: Window=7 + HL=7**

---

## Recommendation Logic

1. **To fully exclude an avoided artist** → their last play must be *outside* `WINDOW_DAYS`. Set window small enough.
2. **To fade an artist over days, not weeks** → reduce `RECENCY_HALF_LIFE_DAYS`. Doubling the half-life doubles the decay rate.
3. **Both parameters must change together** — window alone gates, half-life decays. Alone, neither fully solves behavioral avoidance.
4. **7 days is the sweet spot** for ham's use case — aggressive enough to exclude 11-day-old CrocoBeat plays, moderate enough to preserve 7-day-old good tracks at 0.50 weight.

---

## Future Tuning Tips

- **Before changing parameters**, query `play_events` to find the exact last-play date of the avoided artist, then calculate if they're outside the candidate window: `age_days = (today - last_play_ms) / 86400`
- **Verify the fix**: after the next 3am refresh, query `affinity_snapshot` to confirm the avoided artist's tracks now show 0.00 affinity
- **If user wants even faster fading**: HL=5 gives `0.5^(2/5)` = 0.76 for 2-day-old IU plays (vs 0.82 with HL=7) — marginal improvement, costs more discovery
- **If user complains good tracks are disappearing**: increase `WINDOW_DAYS` to 10 while keeping HL=7; 10-day-old tracks score 0.42
