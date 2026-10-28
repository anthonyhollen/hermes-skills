---
name: skill-usage-report
description: "Use when asked which skills are used most or last updated."
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [skills, usage, statistics, reports, discord, state-db]
    related_skills: [token-usage, discord-table-format, no-agent-cron-scripts, hermes-skill-tuneup]
---

# Skill Usage Report

## Overview

Answers "what are my most-used skills?" and "when was <skill> last updated?" from real data — skill load counts and last-used dates from `state.db`, last-updated dates / versions / changelog notes from the skills tree — and posts the standard Discord table image to the current thread.

The whole job is done by one self-posting no_agent-style script (`scripts/skill_usage_report.py`, same delivery conventions as `token-usage` / `task-tracker` / `traffic-camera`). **Never hand-build this table inline** — the script output is the canonical format, and the script self-posts (byte-safe delivery).

**Ad-hoc only** — deliberately NO cron job (user decision 2026-09-29). If a scheduled digest is ever wanted: deploy a byte-identical copy under `~/.hermes/scripts/` (the scheduler resolves `script:` there when no profile is set) and create the job like token-usage does.

## When to Use

- "What are my most used skills?" / "which skills do I use most?"
- "When was <skill> last updated?" / "skill usage" / "skill stats"
- Any request for skill load counts or skill activity data.

Not for: auditing/tuning one skill (`hermes-skill-tuneup`), slimming the skill list (`hermes-skill-disable`), authoring new skills (`hermes-agent-skill-authoring`).

## Workflow

Run once; the script self-posts the table to the target:

```bash
# Preferred — pass the CURRENT thread id from session context (session env can be stale):
python3 ~/.hermes/skills/productivity/skill-usage-report/scripts/skill_usage_report.py --thread-id <current_thread_id>

# If you don't know the thread id — target resolves from HERMES_SESSION_KEY:
python3 ~/.hermes/skills/productivity/skill-usage-report/scripts/skill_usage_report.py
```

Then relay a one-line confirmation (e.g. "Posted — 356 loads across 38 skills"). Do not re-post, summarize, or duplicate the table.

Flags:
- `--top N` — top rows to list (default 15)
- `--recent-days N` — also list lower-ranked skills loaded in the last N days (default 7; `0` = off)
- `--thread-id <id>` — explicit target thread
- `--to discord:<channel>[:<thread>]` — test override (playground: `--to discord:1501742338639401071`)
- `--dry-run` — render only; prints the PNG path; posts nothing

Exit codes: `0` ok, `1` failure (stderr carries the diagnostic; unexpected crashes also post a traceback to the home channel).

## Output & interpretation

Table columns: `# | Skill | Uses | Last used | Last updated | Last change`.
Rows: the top N by uses (ties: most recent use first), then — below a `— used in last 7 days —` divider row — every lower-ranked skill loaded in the last 7 days, numbered with its true rank (so a just-used 1-load skill, e.g. this one, still shows).
Caption: `N loads across M skills since YYYY-MM-DD (of K installed)` + ` · plus R more used in the last 7 days` when the divider block is present.

- **Uses** — successful agent loads of the skill (`skill_view` of its SKILL.md) recorded in `state.db`, all time. NOT counted: supporting-file reads (`skill_view` with `file_path`), failed or skipped loads (anything but `success: true`), and same-session dedup stubs (`dedup: true`). Rows are keyed by the skill's directory name: frontmatter names and category-qualified names (`productivity:task-tracker`, `cat/skill`) count under it; a non-category prefix (`plugin:skill`) is kept. Cron runs of skill scripts, tune-ups executed over SSH, and the post-session curator's (bg-review) reads do NOT register — so a heavily *used* skill can still show an old "last used".
- **Last used** — most recent recorded load.
- **Last updated** — newest content-file edit in the skill directory (docs/code; caches, data files, and backups excluded).
- **Last change** — newest changelog heading when the changelog shipped with that update, else the frontmatter `version`.

## Common Pitfalls

1. **Do not pass a message id as `--thread-id`** — it must be a channel/thread id; a message id yields a 400/404 delivery error.
2. **Don't hand-build the table.** The script keeps counts, exclusions, and rendering consistent (and delivery byte-safe).
3. **Keep the `state.db` prefilter exact** — `WHERE role='assistant' AND tool_calls LIKE '%"skill_view"%'`, then parse the JSON. Dropping the role filter pulls in tool-result rows; don't "simplify" it to a bare scan. Results are looked up **by `tool_call_id`**, never by `tool_name` (older result rows have `tool_name` NULL).
4. **Don't switch the source to `~/.hermes/skills/.usage.json`.** Hermes' own counter is inflated by the curator's read-before-write `skill_view`s (its forks run with no session DB) and counts file reads — it is not "what the user used".
5. **Numbers are cumulative** — they only go up (unless a skill is renamed/moved/deleted: rows key on the current directory name); a skill's "last updated" is normally NEWER than its "last used" (tune-ups update, they don't load).
6. **New skills are invisible to the current session's loader** — after any skill change, verify in a fresh session or via `skill_view` by exact path.
7. **Run parity** — validate edits under the gateway venv (`~/.hermes/hermes-agent/venv/bin/python`) as well as system `python3`; the script inserts `/usr/lib/python3/dist-packages` itself for PIL/tabulate.

## Validation (after any edit)

1. `python3 -m py_compile scripts/skill_usage_report.py` + gateway-venv parity.
2. Stub-curl dry runs — recipe in `no-agent-cron-scripts/references/stub-binary-dry-run.md`: happy path (exit 0, payload JSON parses, PNG deleted after the stub's 2xx) and failure path (`STUB_CODE=500` → exit 1, PNG kept).
3. Regression: the `--dry-run` PNG's top counts must match the known-good set in `references/report-internals.md` (small upward drift is normal), and the divider block must list every skill loaded in the last 7 days below the top N.
4. Manual end-to-end: `--to discord:1501742338639401071` (playground channel).

## References

- `references/report-internals.md` — exact data rules (SQL, mtime exclusions, changelog-note logic), caveats, stub-run recipe, known-good values, runner commands.
