---
name: backup-cleaner
category: software-development
version: 1.3.0
description: Find stale edit-backup (.bak/.pre-*/.orig) files under ~/.hermes and delete those whose preceding update was validated by a successful cron run. Manual runs are report-only by default (--delete to act); the weekly cron auto-deletes the validated set. Reports as a Discord table image.
author: ham
triggers:
  - "clean up old backups"
  - "delete stale backup files"
  - "which backups can be deleted"
  - "backup cleanup"
  - "remove .bak files"
metadata:
  hermes:
    tags: [maintenance, housekeeping, backups, skills]
    related_skills: [hermes-skill-tuneup, vault-audit, discord-table-format]
---

# Backup Cleaner

## Overview
Skill/script edits (especially `hermes-skill-tuneup` runs) leave `.bak` files
scattered across `~/.hermes/skills/` and `~/.hermes/scripts/`. This skill finds
them and deletes the ones that are provably safe to remove. **Pure Python, zero
LLM tokens.**

**Deletion criterion (strict):** a backup is deletable only when the update it
preceded has been *validated by a successful run* — i.e. a cron job that exercises
that file has `last_status == "ok"` **and** `last_run_at` is **after** the backup's
mtime. Anything that can't be tied to such a run is **reported for manual review,
never auto-deleted.**

## When to use
- Ham says "clean up old backups", "which backups can I delete", "remove .bak files".
- The weekly cron (Sun 11:00 SGT) **auto-deletes the validated set** and pings Discord (with
  the list of what it removed) only when there was something to remove — no permission prompt.
- After a tune-up, to clear the backups it left once the skill's cron has run green (or just
  wait for the weekly cron to remove them automatically).

For a **manual** run, the engine is still report-only by default — add `--delete` to act.

## How to run
Engine: `~/.hermes/scripts/backup_cleaner.py` (**safe by default** — reports, deletes nothing).

| Command | Effect |
|---|---|
| `python3 ~/.hermes/scripts/backup_cleaner.py` | Full report: deletable set + report-only groups, listed by path (relative to `~/.hermes`) + size + modified date. **No deletion.** |
| `python3 ~/.hermes/scripts/backup_cleaner.py --delete` | Delete **only** the validated-deletable set; log each removal to the manifest. |
| `python3 ~/.hermes/scripts/backup_cleaner.py --json` | Machine-readable classification of every backup found. |
| `python3 ~/.hermes/scripts/backup_cleaner.py --quiet` | Print the text summary only if there are deletable backups. |
| `python3 ~/.hermes/scripts/backup_cleaner.py --post <channel_id>` | Post the report to Discord as a **table image** (the full deletable list — File / Size / Modified / Validated — rendered via the `discord-table-format` skill, uploaded in one REST multipart call). Posts only if there are deletable backups; falls back to a text summary if rendering fails. |

Deletions are appended to `~/.hermes/scripts/.backup_cleaner_log.json` (path, size,
mtime, and the job id + run time that validated each one). Paths in every view are shown
relative to `~/.hermes` (the scan scope), so the prefix is dropped.

## How a backup is matched to a validating run
- **Owning skill** (nearest ancestor dir with a `SKILL.md`) → matched to **enabled** cron
  jobs whose `skill` field equals that skill name. (This validates that the *skill* ran
  successfully since the backup — your stated criterion; for a skill with several cron
  scripts it means "a cron of this skill ran ok", not necessarily that exact file.)
- **Script** → matched to enabled cron jobs whose `script` equals the live file's name,
  **but only when the live file is exactly `~/.hermes/scripts/<script>`** (where `no_agent`
  crons actually run) — a same-basename file elsewhere in the tree is a different unit and
  does NOT match.
- If any matched job ran **ok** after the backup's mtime → **DELETABLE**. Disabled jobs
  (`enabled: false`) never validate — a stale `ok` from a paused job isn't evidence.

## What it will NOT delete (report-only)
- **NO_CRON** — no enabled cron references the file's skill/script (interactive skills like
  `hermes-skill-tuneup`, doc-only backups with no runtime). Can't prove a run → kept.
- **NOT_YET** — a matching cron exists but hasn't run ok since the backup was made.
- **ORPHAN** — the live file the backup shadowed no longer exists. This also covers a
  `.<word>.bak` backup (e.g. `foo.py.minimax.bak`) whose live file has a *different* name:
  such names are intentionally left ORPHAN rather than risk resolving to the wrong file.
- **ANOMALY** — the backup is newer than its live file (not a stale backup).
- **Out of scope entirely:** the full-system zips in `~/.hermes/backups/*.zip`
  (rolling Drive snapshots — different retention rule) and `__pycache__` bytecode.

## Backup patterns detected
`*.bak`, `*.bak-<tag>`, `*.<timestamp>.bak`, `*.pre-*.bak`, `*.orig`, `*~`
(including files inside `backups/` / `.backups/` subdirs). `__pycache__` is skipped.

## Cron
Weekly `no_agent` job (Sun 11:00 SGT, `deliver: local`) runs `~/.hermes/scripts/backup_cleaner_cron.py`
→ engine `--post <alerts channel> --delete` → **auto-removes the validated `DELETABLE` set**,
logs each removal to the manifest, and posts the **full removed list as a table image**
(via `discord-table-format`), captioned with what it removed. Silent when there was nothing
to remove. Only the strictly-validated set is ever deleted — `NOT_YET` / `NO_CRON` / `ORPHAN` /
`ANOMALY` backups stay report-only and are never touched. To review before anything is removed,
run the engine yourself with no flags (report-only) between cron runs.

## Safety
- Manual runs are report-only by default; deletion requires the explicit `--delete` flag. The
  weekly cron passes `--delete` on purpose so it auto-deletes without prompting.
- Only files classified `DELETABLE` — validated by a successful post-update run — are ever
  removed, whether by a manual `--delete` or the cron; every other class is kept.
- Never touches a file outside the backup patterns, the full-system zips, or `.pyc`.
- Every deletion (manual or cron) is logged to the manifest with the validating job id + run
  time for an audit trail.
- The mapping is deliberately conservative: any uncertainty resolves to *keep*.
- The weekly cron is `deliver: local` and self-posts the image; if that post fails it prints
  the full report to stdout (preserved in the job output) and exits non-zero, so a delivery
  failure shows red in `hermes cron list` instead of vanishing silently. Deletion happens
  before the post, so a failed post never leaves the validated set un-removed or unlogged.

## Files
- `~/.hermes/scripts/backup_cleaner.py` — engine (this dir keeps a synced copy in `scripts/`).
- `~/.hermes/scripts/backup_cleaner_cron.py` — weekly cron entry (`--post --delete`: auto-removes the validated set + posts the removed list).
- `~/.hermes/scripts/.backup_cleaner_log.json` — deletion manifest (a corrupt one is preserved as `.corrupt-<ts>`, never silently discarded).
- `scripts/test_backup_cleaner.py` — regression suite (classification, resolution, delete-safety, delivery contract). Run: `python3 scripts/test_backup_cleaner.py`; exit 0 = green. Run it after any engine edit.
