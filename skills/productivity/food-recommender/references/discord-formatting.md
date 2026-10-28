# Discord Formatting Gotchas

## Emoji inside bold = emoji disappears
`**Name**:round_pushpin:` renders the emoji INSIDE the bold span and Discord swallows it. Always put a space before the emoji: `**Name** :round_pushpin:`. This applies to ALL Discord emoji names (`:takeout_box:`, `:popcorn:`, etc.) — none render inside bold.

## Non-standard Discord emojis render as literal text
`:popcorn:` and other non-standard emoji names will display as `::popcorn:` or bare text in Discord. Only use emojis from Discord's standard emoji set (`:round_pushpin:`, `:takeout_box:`, `:star:`, etc.). When in doubt, test in playground channel `1501742338639401071` before running in production.

## Discord Maps URL bare-link form still produces an embed
`[Maps](https://maps.google.com/...)` (no angle brackets) renders as a rich link preview, not a plain link. Only `[Maps](<https://maps.google.com/...>)` with angle brackets suppresses the embed. Also: bare `https://maps.google.com/...` on its own line always produces an embed. The `[label](<url>)` form with angle brackets is the ONLY Discord-safe syntax for Google Maps URLs.

## Discord retry duplicates
If the same recommendation list appears twice in Discord with identical content, this is Discord's platform-level retry — not a double-fire. The script's `split_and_post()` retries on 429 but doesn't retry the whole run, so duplicates indicate a Discord-side retry. Check `cron/output/<job_id>/` for the number of output files on that date to confirm.

## Output format is script-controlled
The cron job runs as `no_agent`, so the script's `format_results()` is the sole source of truth for output formatting. If the example in SKILL.md differs from what the script produces, patch the SKILL.md example to match (the script always wins).
