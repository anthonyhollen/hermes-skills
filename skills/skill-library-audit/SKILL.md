---
name: skill-library-audit
description: "Use for skill usage, last-updated, or vault skill-coverage checks."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [meta, skills, audit, usage, state-db]
    related_skills: [hermes-skill-tuneup, token-usage, discord-table-format, skill-usage-report, vault-audit]
---

# Skill Library Audit

Answers the recurring library-level questions: **which skills are most used, when was skill X
last used / last updated, what's stale or never used, what should be tuned or pruned next** —
from the two durable records: `state.db` (loads) and the skill dirs (content mtimes +
`CHANGELOG.md`). It also feeds tune-up / prune target selection.

## When to use
- "what are my most frequently used skills?" / skill-usage leaderboard requests
- "when was \<name\> last updated / last used?" / "what's stale or never used?"
- "check my skills against the vault" / "did you miss any skills" (vault coverage → §5)

Not for: a single skill's tune-up (`hermes-skill-tuneup`), authoring a skill
(`hermes-agent-skill-authoring`), or token/cost reporting (`token-usage`).

**Routing:** for the plain "post my skill usage" ask, `skill-usage-report` is the standard
path — its no_agent-style script self-posts the table image to the current Discord thread.
Reach for THIS skill when the ask needs analysis beyond the standard table:
staleness/prune/tune-up targets, custom cuts, or agent-presented output.

## Procedure

**1. Loads + last-used (state.db).** Query `~/.hermes/state.db`, table `messages`:

    SELECT timestamp, tool_calls FROM messages
    WHERE role='assistant' AND tool_calls LIKE '%skill_view%'

Parse each `tool_calls` JSON (list of call dicts); count where `function.name == 'skill_view'`
keyed by `json.loads(function.arguments)['name']`; `max(timestamp)` per skill = **last used**.
- Filter `role='assistant'` — tool-result rows match the same LIKE (via `tool_name`) but hold
  no calls; unfiltered they roughly double the row count.
- Python stdlib `sqlite3`, opened read-only (`sqlite3.connect('file:.../state.db?mode=ro',
  uri=True)`); don't assume a `sqlite3` CLI exists.
- Counts are a floor — compaction, pruning and any past store repair drop history.

**2. Last-updated (skill dirs).** For each skill dir take the newest mtime among CONTENT files
only — skip `__pycache__/`, `*.db*`, `*.log`, `*.csv`, caches (they change on every RUN and
make an idle skill look freshly edited). Report three things: SKILL.md mtime (last edit to the
body the agent reads), newest content file (last real content touch), and the `CHANGELOG.md`
head — entries are newest-first, so it gives version + date + what the update WAS (tune-up, bug
fix, feature). Where no changelog exists, fall back to `skill_manage`/`patch` calls in
`messages` plus mtimes.

`scripts/skill_usage_report.py` performs steps 1–2 in one run (text table or `--json`); run it
first and adapt by hand only if the store schema has drifted.

**2b. Stale mirrors (bundled skills).** Bundled skills are also mirrored into `~/.hermes/skills/`
— and the LOCAL copy is what every session loads, so a mirror that lagged the repo tree
**shadows** the newer version everywhere. Diff by name across the two trees (layouts differ, so
match on the skill directory name):

    for r in $(find ~/.hermes/hermes-agent/skills -name SKILL.md); do
      n=$(basename "$(dirname "$r")")
      l=$(find ~/.hermes/skills -maxdepth 4 -type d -name "$n" -not -path '*/.archive/*' | head -1)
      [ -n "$l" ] && ! cmp -s "$r" "$l/SKILL.md" && echo "DIFF: $n"
    done

Fix a genuinely stale copy by saving the old file as `<file>.bak-sync-<date>` and overwriting
from the repo; a local copy that is a superset of its repo twin (typically much larger) is a
deliberate customisation — diff the content first, never blind-sync those.

**3. State the caveats in the answer** — they change how the user reads the report:
- Counts cover **agent loads only**. Cron `no_agent` runs execute scripts without loading
  skills, and maintenance performed outside this box (e.g. over SSH from another machine) never
  enters this box's session history — an actively maintained skill can show a months-stale
  "last used". Say this instead of implying neglect.
- **"Last used" ≠ "last updated"** — reloaded daily / edited months ago is a normal pair; the
  pair is the point of the report.
- Automation frequency is a different metric: cron completion counts live in
  `~/.hermes/cron/jobs.json` (`skills[]` × `repeat.completed`) — keep it a separate column or
  footnote, never silently merged into "uses".

**4. Output (Discord).** One image table via the `discord-table-format` renderer:
`# | Skill | Uses | Last used | Last updated | Last change` — top ~14 rows; the next tier
(~8–5 uses) goes in the caption text together with the method + caveats line. Vision-check the
PNG before sending (every row present, nothing clipped at the right/bottom edge).

**5. Vault coverage (documented skills ↔ live library).** For "check my skills against the
vault" / "did you miss any skills": run the vault audit engine first
(`python3 ~/.hermes/scripts/vault_audit.py`; add `--fix` to apply auto-fixes), then reconcile
the vault's per-skill notes ↔ the on-disk library ↔ `state.db` loads. Report both gap
directions — documented skills with zero loads (usually normal: cron-only, SSH-run, or
reference notes) and heavily-used skills missing from the vault (it documents a curated
subset — propose candidates, don't bulk-document). Adding a skill to the vault is a manual
two-part write (note + MOC bullet — the engine only RE-GROUPS existing bullets, never adds):
recipe, format rules, and pitfalls in `references/vault-reconciliation.md`.

## See also
- `token-usage` → `references/state-db-skill-introspection.md`: cron-vs-interactive leaderboard
  + system-prompt token share (companion reference; predates this skill and lacks the
  last-used / last-updated halves).
- `hermes-skill-tuneup`: the per-skill four-pass tune-up method; its bug hunt mines state.db
  usage for a SINGLE skill — this skill is the library-wide counterpart.
