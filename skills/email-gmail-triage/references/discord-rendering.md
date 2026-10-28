# Discord Rendering — Email Gmail Triage

See [morning-brief: references/discord-rendering-quirks.md](../../productivity/morning-brief/references/discord-rendering-quirks.md) for the canonical reference.

Key rules already captured there:
- Trailing `\n` at end of message is silently dropped by Discord
- `---` on its own line renders as a horizontal rule separator — use it between sections
- Consecutive blank lines collapse to one; explicit `---` forces visual separation
- Link preview suppression: `embeds: []` + `unfurl_media: false` + `unfurl_links: false` + `disable_web_page_preview: true`

## gmail_report.py Specific Issue (2026-05-07)

**Bug**: `"\\n".join([action_block, readlater_block, autofiled_block])` produced inconsistent spacing — empty sections ended with a single `\n`, non-empty sections ended with trailing content, so the join's `\n` appeared mid-line rather than at line start, causing Discord to render spacing erratically.

**Fix**: Single `---\n` between header and Action section. Blank line between each of the three content sections. Blank line before footer. Format:

```
**📬 Gmail Triage Digest — 7:10 AM SGT**
---
**🔴 Action — Needs Attention Today (0)**
No urgent emails.

**🟡 Read Later (0)**
No important emails.

**⚪ Auto-filed (4)**
1. `sender@email.com`: Subject line
...

Total triaged since last digest: 4 | 🔴 Action: 0 | 🟡 Read Later: 0 | ⚪ Auto-filed: 4
```
