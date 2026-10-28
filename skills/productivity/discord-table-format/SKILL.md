---
name: discord-table-format
description: "Discord does NOT render markdown tables. Load this before ANY reply that would contain a table (lists of rows/columns, comparisons, reports) — it posts each table as a crisp image in one command."
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [discord, tables, images, formatting, delivery]
    related_skills: [task-tracker, token-usage, skill-usage-report, hermes-skill-tuneup]
---

# Discord Table Format

Discord shows markdown pipe tables as raw `| --- |` text, and bullet-list stand-ins lose the columns. **Every table you show in Discord goes out as an image through this skill** — including small ones, summaries, comparisons and tables inside explanations. Only skip it when the user explicitly asks for plain/copyable text (see *Text fallback*).

## Show tables (interactive) — one command
Put ALL tables for the reply into one sections list and run (quoted heredoc — apostrophes in the data are safe):

```bash
python3 ~/.hermes/skills/productivity/discord-table-format/scripts/send_tables.py <<'JSON'
[{"title": "Overdue", "caption": "**Overdue tasks** (2)", "headers": ["Task", "Status"],
  "rows": [["Water plants", "19d overdue"], ["Descale kettle", "3d overdue"]]},
 {"title": "Upcoming", "headers": ["Task", "Due"], "rows": [["Car wash", "Oct 2"]]}]
JSON
```

- Each section is posted as ONE message — its `caption` (default `**<title>**`) + the table image — in order, to the **current conversation** (resolved from the session; never #alerts). Don't pass `--to` for "here". Only if the user asked for a different channel: `--to discord:<channel_id>[:<thread_id>] --force-target` (a channel/thread id, never a message id).
- stdout is one JSON line: `{"target", "posted", "message_ids", "empty", "failed", "unsent"}`.
  - exit 0 → done. Your final reply is a short line of commentary — **never restate the table** as text or bullets.
  - exit 1 → a post failed and it stopped there; re-run with **only** the `failed` + `unsent` sections. Re-sending an identical section within a few minutes is deduplicated (nonce), so a `failed` with `"may_have_posted": true` can be re-sent safely right away.
  - killed (e.g. terminal timeout; no JSON) → the `posted: <title> -> message <id>` lines on stderr are what landed; re-send the rest the same way.
  - exit 2 → nothing was posted (bad input, no/refused target, no token): fix what the error says. Never fall back to a markdown table or `send_message` — tell the user if delivery isn't possible.
- Sections with no rows are listed in `empty` and not posted; mention them in your reply if it matters.
- Do NOT use `MEDIA:` tags or `send_message` for tables (several `MEDIA:` tags in one reply get re-batched — all text first, then all images), and don't delete the images yourself (the script does).
- `--dry-run` renders and resolves the target without posting; `--repost` posts an identical table again on purpose.

## Input rules
- `headers`: list of strings. `rows`: list of lists, each exactly `len(headers)` long — a mismatch, a dict row, or a duplicate `title` is rejected (exit 2), never drawn wrong.
- Cells are shown verbatim (numbers are **not** reformatted: `0.50` stays `0.50`); numeric columns are right/decimal-aligned. JSON `null` → empty cell. Newlines/tabs become spaces; cells over 200 chars are truncated.
- Emoji: ✅→✓, ❌→✗, ⭐→★, 🔴🟢🟡🔵…→● are substituted automatically (the font has no emoji); other emoji draw as a box — prefer words, and use bold for emphasis.
- Bold rows: any data row containing a keyword in `bold_match` (case-insensitive substring; default `["overdue", "failed", "critical"]`; `[]` = none). The header row is never bold.
- Keep each image readable: ≲ 40 rows and ≲ ~110 characters wide — split bigger data into several sections.

## Render only (scripts and other skills)
`scripts/render_tables.py` renders without posting — `<sections.json>` or stdin → `{"<title>": "<png path>"|null}`; `--one '<headers_json>' '<rows_json>' [prefix]` → one path. In Python: `render_table(headers, rows, out_prefix=, bold_match=)` / `render_sections(sections)`. PNGs land in `/tmp/hermes_cache/<prefix>_<ms>_<n>.png`; delete after a confirmed send (the renderer prunes its own >24 h leftovers). Consumers, the full CLI/exit-code contract and internals: `references/implementation.md`. A `no_agent` script must deliver with REST multipart `curl`, never `hermes send`/`MEDIA:` — `references/delivery.md`.

## Text fallback (opt-in only)
When the user explicitly wants copyable text: `tabulate(rows, headers, tablefmt="fancy_grid")` in a ` ```prolog ` code block (replace any backtick in a cell with `'`), only for ≤ 5 rows and ≤ ~90 characters wide (Discord desktop clips wider code blocks; mobile shows ~39 chars). If the user says "don't use discord-table-format" / "show as text", respect it for that reply.

## References
| File | Read when |
|------|-----------|
| `references/delivery.md` | Delivering tables anywhere other than the one-command path above (single image via `send_message`, `no_agent` scripts, troubleshooting misrouted/misordered posts) |
| `references/implementation.md` | Editing the renderer, integrating it in another skill (caller contract, consumers), or debugging an image (fonts, widths, emoji, numbers, bold, sizes) |
| `references/development-log.md` | Historical design log (May 2026) — superseded; for background only |
| `CHANGELOG.md` | Version history and tune-up provenance |
