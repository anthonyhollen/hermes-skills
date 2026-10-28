---
name: food-recommender
description: Recommend restaurants near a location based on FoodPanda order history — ranked by variety, distance, and delivery availability.
version: 1.3.0
author: ham
license: MIT
metadata:
  hermes:
    tags: [food, restaurant, recommendation, FoodPanda, personalisation]
    category: productivity
    requires_toolsets: [terminal]
    triggers:
      - "recommend food"
      - "what to eat"
      - "dinner ideas"
      - "new restaurant"
      - "food near"
---

# Food Recommender

Recommend restaurants near any Singapore address/postal code based on your personal FoodPanda order history — surfacing places you haven't ordered from recently, ranked for variety and convenience.

## Quick Start

```
Recommend food near [address/postal code]
```

The recommender reads your order history, finds nearby restaurants on OpenStreetMap, filters out places you've already ordered from, scores the rest by variety + distance, and returns a formatted top-10 shortlist.

## Components

### Core Script: `food_recommender.py`

Locations:
- **Live (cron):** `~/.hermes/scripts/food_recommender.py`
- **Skill dir:** `scripts/food_recommender.py` (canonical reference copy — keep in sync)

Usage:
```bash
python3 ~/.hermes/scripts/food_recommender.py "Pasir Ris Grove, Singapore"
python3 ~/.hermes/scripts/food_recommender.py "518141"
python3 ~/.hermes/scripts/food_recommender.py "Tampines" --dry-run  # print only, don't post to Discord
```

What it does:
1. Reads all past FoodPanda orders from the Google Sheet (`1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4`) across all monthly tabs
2. Deduplicates orders by (normalized merchant name, amount) — prevents duplicate rows inflating frequency profile
3. Builds a preference profile: order frequency per merchant, cuisine signals from merchant names
4. Geocodes the input address via Nominatim → gets lat/lon
5. Searches for restaurants within **3km** via Overpass API (OSM data); **falls back to 5km** if 0 results
6. Filters out merchants already in order history (treating as "already bored of these")
7. Scores remaining candidates: "General" (unclassifiable) cuisine excluded from bonuses; prefers diverse cuisines not in recent orders, then by distance
8. Validates top candidates via Google Places API (≥4.0★ rating required)
9. Posts to Discord; **saves repeat-avoidance cache only on successful delivery**

### Skill File: `SKILL.md` (this file)

Documents the system, conventions, and output format.

---

## Output Format

Both Maps and Directions links use `[title](<url>)` syntax — **angle brackets suppress Discord link previews/embeds**. This is the correct Discord-safe form. Deliver as:

```
**🍽️ Food Recommendations near [Location]**
_Based on your order history · ≥4.0★ Google rating · top 10 shown_

**1. Restaurant Name** :round_pushpin: 0.4km · Chinese · ★★★★☆ 4.3
[Maps](<https://www.google.com/maps/search/?api=1&query=lat,lon>) | [Directions](<https://www.google.com/maps/dir/?api=1&destination=lat,lon>) | :takeout_box: GrabFood

Rules:
- Rating stars: `★` × full stars + `☆` × remaining (e.g. 4.3 → `★★★★☆`)
- Platform emoji: `:takeout_box:` followed by platform name(s), with `(n/n)` suffix when multiple (e.g. `:takeout_box: FoodPanda · GrabFood (2/2)`)
- Location pin: `:round_pushpin:` immediately after the restaurant name on the same line
- **Both Maps and Directions URLs MUST use `[title](<url>)` form with angle brackets** — this is the only syntax that reliably suppresses Discord embed previews for Google Maps `search/` and `dir/` URLs
- No price indicators (not reliably available from OSM)

**The example output above IS the canonical format.** The script at `~/.hermes/scripts/food_recommender.py` produces this format — if the script and this example ever disagree, patch the script to match.

**User preference — variety over familiarity:**
Do NOT repeat the same restaurant within any 7-day window. The `food_recommender.py` script
handles this via the 7-day rolling cache (see Repeat-avoidance section below). If the
script is not available and you must generate recommendations manually, exclude all restaurants
from the last 7 days' lists.

---

## Scoring Algorithm

**Step 1 — Build order history profile:**
- Extract all unique merchants from Sheet (all tabs)
- Count order frequency per merchant
- Extract cuisine hints from merchant names using `KNOWN_CUISINES` dict in the script
- Track cuisines ordered in last 30 days (recency signal)

**Step 2 — Restaurant search:**
- Geocode input via Nominatim → lat/lon
- Overpass API: `amenity=restaurant` + `amenity=fast_food` within 3km radius (form-encoded POST, NOT JSON)
- Returns: name, lat/lon, cuisine tags, distance

**Step 3 — Filter already-ordered:**
- Any restaurant fuzzy-matching a merchant in order history (≥70% Levenshtein similarity)
- These are skipped — user is "bored of these"

**Step 4 — Score all candidates (no API calls yet):**
- **"General" cuisine exempted**: unclassifiable restaurants get 0 pts (no variety, affinity, or recency adjustment) — prevents generic/unnamed places from dominating the list
- **Cuisine variety bonus**: +10 pts if cuisine not in top-5 most-ordered cuisines
- **Cuisine affinity bonus**: +5 pts if cuisine IS in top-10 (you like this type)
- **Recency penalty**: -5 pts if cuisine was ordered in last 30 days
- **Distance tiebreaker**: (3km - distance_km) × 0.67, capped at 2 pts — only used to break ties within the same score band

Sort all scored candidates descending by (score, -distance).

**Step 5 — Two-pass Google Places validation:**
1. Take top 10 scored candidates → call `places:searchText` API → check Google rating
2. Any below 4.0★ → replaced by next-best from scored list (11th, 12th, ...)
3. Deduplication by (name, lat, lon) — different locations of the same chain are kept as distinct entries
4. Repeat until 10 valid recommendations or list exhausted

This minimises API quota: typically ~20 calls for 10 recommendations vs 192 for a naive all-at-once check.

**Step 6 — Select top 10** by descending validated score.

---

## Delivery Platforms

| Platform | Channel | Notes |
|----------|---------|-------|
| Discord | `1499972245437550602` (#alerts) | Primary |
| Origin | Thread where cron fired | Only if triggered by user |

---

## Cron Job

Daily dinner recommendation at **6pm SGT**, defaulting to home address. Runs as a **`no_agent`** script — the script self-posts to Discord (no LLM agent needed per tick).

```bash
hermes cron edit 2731e556079c \
  --no-agent \
  --script food_recommender.py \
  --deliver local \
  --enable
```

Job ID: `2731e556079c`.

The script handles:
- Running the full pipeline (Sheets → geocode → Overpass → score → Places API)
- Splitting output into ≤2000-char Discord messages
- Posting results to channel `1499972245437550602`
- Posting error notifications on failure

### ⚠️ Cron schedule timezone pitfall

The Hermes cron scheduler interprets hour fields in **SGT (Asia/Singapore, UTC+8)**, NOT UTC.
- To fire at **6pm SGT** → use `0 18 * * *` (18:00 SGT = 10:00 UTC)
- `0 10 * * *` fires at **10am SGT**, NOT 10am UTC / 6pm SGT — it will be 8 hours early
- When in doubt: `hour_in_SGT = desired_SGT_hour`, no UTC conversion needed in the schedule field

This applies to ALL cron jobs in this system — always treat the hour field as SGT.

### Repeat-avoidance (7-day rolling cache)

The script maintains a rolling cache at `~/.hermes/scripts/food_cache/recent_recommendations.json` that accumulates restaurant names across the last 7 days. Any restaurant in the cache is filtered out of scoring via `score_all(recent_recos=[...])`, so the same restaurant will not appear in recommendations within any 7-day window.

- New recommendations are appended to the cache **only after successful Discord delivery** — a failed post means recommendations will reappear tomorrow (prevents silent "eating" of recommendations the user never saw)
- Up to 50 entries retained; if the cache is older than 7 days, it is treated as stale and **reset entirely**
- On first run (cold cache), no repeat-avoidance is applied — this is expected behaviour

### One-shot usage (ad-hoc):

```
You: recommend food near Tampines
Bot: [runs recommender with "Tampines, Singapore"] → formatted top-10]
```

Note: one-shot usage posts directly to Discord (no agent intermediary). Use `--dry-run` to capture output without posting — the agent can then send it to any channel/thread.

---

## Pitfalls

- **OSM restaurant data in SG is incomplete** — popular chains like "Ichiban" may not appear in OSM. Use merchant name fuzzy matching as a secondary filter, not a hard block.
- **Cuisine extraction from merchant names is imperfect** — "Sushi Express" → Japanese is reliable; "Food Junction" → unknown. "General" cuisine is excluded from scoring bonuses to prevent generic places from dominating.
- **3km radius may return 0 results** in sparse areas — the script automatically falls back to 5km.
- **FoodPanda/Grab availability** cannot be queried programmatically. Heuristic: if the restaurant name matches a known chain or has a well-known SG presence, note it as "likely on FoodPanda/GrabFood". Do not claim certainty.
- **Nominatim rate limit** — 1 req/s max. The script already sleeps between calls; do not run concurrent instances.
- **Duplicate merchants in Sheet** — script deduplicates by (normalized_merchant_name, normalized_amount) before building frequency profile.
- **Google Places API quota** — the two-pass approach limits calls to ~20 per run (not 192). Do not switch back to a naive all-at-once check. If the script is slow, the two-pass is working correctly — be patient.
- **Overpass form-encoded requirement** — when calling Overpass API directly, use `application/x-www-form-urlencoded` POST, NOT JSON. JSON returns HTTP 400.
- **datetime tz-naive vs tz-aware** — `datetime.strptime()` returns naive datetimes; compare with `.replace(tzinfo=None)` on aware datetimes to avoid `TypeError`. Occurs in `build_profile()` when checking recency.
- **`food_recommender.py` is the live script** — cron jobs run this path. The skill directory at `skills/productivity/food-recommender/` holds the canonical version for reference. If you edit the live script, mirror changes to the skill directory version.
- **Google API key loaded from `~/.hermes/.env`** — `GOOGLE_API_KEY` must be set in the env file. The script exits with a Discord error notification if the key is missing.
- **Double delivery when running from agent** — if you run the script without `--dry-run`, it posts to the hardcoded Discord channel. If you ALSO `send_message` the output to a user's requested thread, they see it twice. Always use `--dry-run` when the user asks to "run and send to this thread/channel."
- **Discord formatting gotchas** — emoji in bold, Maps URL embeds, non-standard emoji, retry duplicates: see `references/discord-formatting.md`.

---

## Modifying

### Change radius
Edit `SEARCH_RADIUS_M` in `food_recommender.py`:
```python
SEARCH_RADIUS_M = 3000  # metres
```

### Change top-N
Edit `TOP_N`:
```python
TOP_N = 10
```

### Change minimum match score for "already ordered" filter
Edit `FUZZY_THRESHOLD`:
```python
FUZZY_THRESHOLD = 0.70  # Levenshtein ratio
```

---

## References

- `references/overpass-api.md` — Overpass API content-type gotcha, SG radius benchmarks, fallback strategy
- `references/google-places-api.md` — Places API v1 endpoints, field mask, fuzzy matching, quota notes
- `references/cron-schedule-tz.md` — Cron hour fields are SGT, not UTC. Includes correct schedule table.
- `references/diagnostics.md` — How to check if multiple runs occurred, what to look for in run files, related fixes applied.
- `references/discord-formatting.md` — Discord emoji, Maps URLs, embed suppression, retry duplicates.