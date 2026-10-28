# Food Recommender — Run Diagnostics

## Checking if multiple runs occurred

The cron output for job `2731e556079c` is at:
```
/home/amh/.hermes/cron/output/2731e556079c/
```

Each run produces one `.md` file timestamped at execution time. If multiple runs occurred, there will be multiple files from the same day — e.g. one at `18-00-09` and one at `18-05-22`. Run count = number of `.md` files with today's date.

```bash
ls -la /home/amh/.hermes/cron/output/2731e556079c/
```

To see the full list of runs across all days:
```bash
ls /home/amh/.hermes/cron/output/2731e556079c/ | sort
```

## What to look for in each run file

The run file contains the script's stderr/stdout capture. Since the job runs as `no_agent`, there is no LLM-generated content — the script handles everything (pipeline + Discord delivery).

Check the output for:
- **Missing space before emoji**: `**Name**:round_pushpin:` — the emoji is inside the bold and disappears
- **Missing angle brackets**: `[Maps](https://...)` instead of `[Maps](<https://...>)` — produces Discord embed preview
- **Popcorn emoji** (`:popcorn:`): not defined in standard Discord, so it renders as literal text `::popcorn:` or just disappears

## If duplicates appear in Discord but only one cron output file exists

**Root cause:** Discord's own infrastructure can retry HTTP deliveries, causing the same message to appear twice in the channel. The script delivered once, but Discord retried and delivered again. This is NOT a double-fire of the cron job.

**How to confirm it was a Discord retry (not a second cron run):**
1. Check `cron/output/<job_id>/` — if only one `.md` file exists for that day, exactly one cron run fired
2. Check the `last_delivery_error` field on the cron job object — will be populated if Discord reported a failure

**Distinguishing a true double-fire from a Discord retry:**
```bash
# True double-fire: two .md files with different timestamps on the same day
ls /home/amh/.hermes/cron/output/2731e556079c/*2026-06-07*
# Discord retry: one .md file, but two identical messages in Discord channel
```

**No fix possible in the cron layer** — Discord's retry behavior is platform-level. The script's `split_and_post()` retries on 429 but doesn't retry the whole run, so duplicates indicate a Discord-side retry.

## Quick verification of today's run

The script's formatted output is delivered directly to Discord (channel `1499972245437550602`). The run file captures stderr/stdout from the script. The formatted shortlist should look like:

```
**1. Restaurant Name** :round_pushpin: 0.57km · cuisine · ★★★★☆ 4.5
[Maps](<https://www.google.com/maps/search/?api=1&query=...>) | [Directions](<...>) | :takeout_box: FoodPanda
```

If the `:` characters around emoji names are missing (e.g. `round_pushpin:` instead of ` :round_pushpin:`), the format is broken in `food_recommender.py` — patch the script directly.

## Related fixes applied

- `food_recommender.py`: space before `:round_pushpin:` (was `**Name**:round_pushpin:`, now `**Name** :round_pushpin:`)
- SKILL.md canonical example updated to match
- Pitfalls section updated with Discord emoji-in-bold and bare-URL-embed rules
- v1.2.0: Converted to `no_agent` — script self-posts to Discord, handles splitting and error notifications
- v1.3.0: Tune-up — 12 bug fixes (recursion limit, save-before-post, API key from .env, General scoring exemption, chain dedup, 5km fallback, order dedup, type safety); SKILL.md 3,073→2,838 tok; Discord gotchas moved to references/discord-formatting.md
