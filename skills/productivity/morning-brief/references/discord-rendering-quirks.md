# Discord Rendering Quirks

## Trailing newlines are silently dropped

Discord ignores a `\n` at the end of a message if nothing follows it. For example:
```
**Header**
\n          ← Discord renders this as no newline at all
```

**Fix:** Always follow a trailing newline with visible content, or end the section with a **blank-row glyph** — a line containing only `⠀` (U+2800, Braille Pattern Blank):
```
**Header**
⠀          ← U+2800; survives trimming, renders as an empty row
```

U+2800 is classified as a printable character (not whitespace), so Discord does NOT trim it and gives it a full line height. This yields the visual gap of an empty row without the heavy `<hr>` that `---` produces. `send_brief.py` uses the constant `SECTION_SEP = "\n⠀"`.

> Earlier versions used `---` as the separator. It works but renders as a bold full-width horizontal rule, which is visually heavier than desired — replaced by the U+2800 blank row (2026-05-30). Zero-width space (`​`) also survives trimming but has zero width, so the blank line is inconsistent; U+2800 is preferred for a real gap.

## Consecutive blank lines collapse

Discord collapses multiple consecutive `\n\n` into a single newline. Use a `⠀` (U+2800) line to force visual separation between sections when you need distinct blocks — a plain blank line will be collapsed.

## Link previews

Discord auto-scrapes URLs in message content to generate link previews. The supported payload suppressor is:
```json
{"embeds": [], "flags": 4}
```
`flags: 4` is SUPPRESS_EMBEDS. The old `unfurl_media`/`unfurl_links`/`disable_web_page_preview` keys are Slack/Telegram params that Discord silently ignores.

Note: payload flags alone are NOT sufficient — Discord still unfurls certain URLs (e.g. simonwillison.net). **The load-bearing fix is the markdown:** wrap the URL in angle brackets — `[title](<url>)` — which forces Discord to treat it as a raw link and skip unfurling.
