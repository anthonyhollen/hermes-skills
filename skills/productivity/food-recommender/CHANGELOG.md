# Changelog

## 1.3.0 — 2026-06-07 (tune-up)

### Fixed
- **Recursive `_discord_post` with no retry limit** — added `_retries`/`_max_retries` params (max 5), prevents `RecursionError` on sustained 429s
- **Save-before-post ordering** — `save_recommendation_cache` now called only after successful Discord delivery, prevents silent eating of recommendations on failed posts
- **Hardcoded Google API key** — now loaded from `~/.hermes/.env` via `_load_google_api_key()`, with guard in `main()` that exits with Discord error notification if missing
- **Silent exception swallowing** — `get_recent_recommendations()` now logs cache read failures to stderr instead of returning `[]` silently
- **`AttributeError` on non-string sheet cells** — `load_order_history` wraps cell values in `str()` before calling `.strip()`, and filters empty merchant/date fields
- **"General" cuisine unfair scoring** — unclassifiable restaurants now get 0 pts (no variety/affinity/recency bonuses), preventing generic places from dominating recommendations
- **Name-only dedup dropped chain locations** — `fill_top_n` now deduplicates by `(name, lat, lon)` tuple, keeping different physical locations of the same chain
- **5km fallback not implemented** — script now automatically widens search radius to 5km when 3km returns 0 results; `format_results` accepts `radius_km` parameter
- **Order deduplication missing** — `load_order_history` now deduplicates by `(normalized_merchant, amount)` per SKILL.md requirement
- **File handle leak** — `_load_discord_token` now uses `with open()` context manager
- **Duplicate `KNOWN_CUISINES` keys** — removed redundant "thai" entries (was 3×, now 1×)
- **`format_results` hardcoded "3km"** — now accepts `radius_km` parameter to reflect actual search radius

### Changed
- Pitfalls section trimmed (957→~400 tok): Discord formatting gotchas moved to `references/discord-formatting.md`
- Scoring algorithm section updated: "General" cuisine exemption documented
- 5km fallback documented in Components and Pitfalls
- `--dry-run` flag reference removed (never implemented)

## 1.2.0 — earlier

- Converted to `no_agent` cron job — script self-posts to Discord
- Discord emoji-in-bold fix, Maps URL angle-bracket fix
- 7-day rolling cache for repeat avoidance
