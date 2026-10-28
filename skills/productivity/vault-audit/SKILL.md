---
name: vault-audit
category: productivity
version: 1.3.1
description: Run the vault audit — check the Obsidian vault for drift against live ground truth (config, cron, installed skills, VIP roster); auto-fixes machine-verifiable facts, flags the rest. Also: clean up MEMORY.md against the vault.
author: ham
triggers:
  - "audit the vault"
  - "run vault audit"
  - "audit my obsidian vault"
  - "check my obsidian vault"
  - "check the vault is accurate"
  - "vault drift check"
  - "is the vault out of date"
metadata:
  hermes:
    tags: [maintenance, obsidian, memory, accuracy]
    related_skills: [obsidian, hermes-skill-tuneup]
---

# Vault Audit

## Overview
Keeps Ham's Obsidian vault (`~/.hermes/memories/ObsidianHam`, Hermes's long-term memory) accurate by comparing what
the notes claim against live ground truth on the Pi. Pure Python, zero LLM tokens.

## When to use
- Ham says "run vault audit", "audit the vault", "check my obsidian vault", "is the vault out of date".
- The weekly cron (Sun 10:30 SGT) runs it automatically and only pings Discord when there is something to report.
- After a config / model / cron / skill change, to confirm the vault caught up.

## How to run
The engine is `~/.hermes/scripts/vault_audit.py` (pure Python, **safe by default**):

| Command | Effect |
|---|---|
| `python3 ~/.hermes/scripts/vault_audit.py` | Detect + print report. **No vault edits, no posting** (it does log the run to the recurrence state). |
| `python3 ~/.hermes/scripts/vault_audit.py --fix` | Also apply the auto-fixable drift (backs up every edited note; orphaned cron output dirs are quarantined, not deleted). |
| `python3 ~/.hermes/scripts/vault_audit.py --post <channel_id>` | Self-post the report to a Discord channel/thread id (17–20 digits; `<#id>` / `discord:id` accepted), only if there are findings. Test runs go to the **playground** channel. The script posts it — **don't also relay its stdout**. |

Exit codes: `0` clean / everything fixed · `1` something left to review (normal, not an error) · `2` bad CLI input ·
`3` a requested post was not (fully) delivered · `4` internal error (traceback + report printed).

## What it checks
Two severities — `references/checks.md` has the full registry and how to add a check.
- **auto** (fixed in place with `--fix`): model `default`/`fallback`/`pro` in Operations; the Routines jobs table's
  schedule expr when the words around it stay true (same days; a fixed time of day whose HH:MM is rewritten with it,
  or only the minute changed), that HH:MM, and the `**[PAUSED]**` marker;
  installed skill versions vs the per-skill notes; Skills-MOC grouping vs each note's origin marker; orphaned
  `~/.hermes/cron/output/<id>/` dirs of deleted jobs (moved to a quarantine).
- **review** (reported, never auto-edited): VIP roster; dangling `[[wikilinks]]`; a documented skill whose install path
  is gone; `.env` key-name drift; channel ids; cron rows/jobs missing on either side, Script-column and header-count
  drift, a schedule cell it can't fix safely; retired job ids or wrong schedules quoted in other notes; archive
  bookkeeping; `curator.enabled` (a deliberate decision — reported, never rewritten); and **anything it could not verify** (a missing note or
  anchor line, an unreadable `jobs.json`/`.env`) — an unverifiable check is reported, never passed.

## Safety rules (do not weaken)
- **Subjective content is never auto-edited** — preferences, working style, family relationships, decision rationale
  (incl. `Decisions.md`) have no machine ground truth, so they are only ever flagged for Ham/LLM confirmation.
- **Every note fix backs up first** to `<note>.bak-audit-<ts>` and only edits when the stale token is found **exactly
  once** (else it downgrades to review). The write is re-validated after each edit. Backups are pruned after 30 days
  (by the timestamp in the name).
- **Orphaned cron output dirs** are moved to `~/.hermes/scripts/.vault_audit_orphans/<ts>/` (kept 30 days), and only
  when `jobs.json` was read cleanly and few dirs look orphaned; otherwise it reports. Never `rm` cron output by hand.
- **Never print secret values** — `.env` is checked by key *name* only; a learned rule can't target a credential path.
- Source of truth for skill categorisation is each skill's **own note** (its origin marker); the MOC mirrors the notes.

## Handling review items
- "Routines row … has no job in jobs.json" — ask Ham whether the job was deleted on purpose before touching the row or
  any cron output; it can mean a job vanished.
- "could not read / not verified" — the check did not run; fix the note or file, don't treat the run as clean.
- `📌 recurring (N of the last M audit days)` — seen on N distinct days; fix it in the vault, or codify the check
  (see *Feedback loop*).

## Cron job
- `b807fa57ef4b`, `no_agent`, `30 10 * * 0` (Sun 10:30 SGT), `deliver: local`, script `vault_audit_cron.py` →
  `vault_audit.main(["--fix", "--post-home"])`. Posts to the home channel `1499972245437550602` only when there are findings.
- `deliver: local`: the scheduler only **saves** stdout (the full report, every run) to
  `~/.hermes/cron/output/b807fa57ef4b/`. The wrapper exits 0 on a normal week and non-zero only for a failed post (3)
  or an internal error (4). Delivery mechanics: `references/checks.md`.

## Feedback loop — learning new drift types
When a new kind of vault inaccuracy turns up (during reconciliation, a manual edit, or a `📌 recurring` finding):
1. **Record** it in `references/drift-patterns.md` (the drift-class taxonomy).
2. **Mechanise** it — prefer a **data rule** (no code):
   `python3 ~/.hermes/scripts/vault_audit.py --learn-rule '{"id":...,"kind":...}'` (`--list-rules` to inspect).
   Kinds: `value_equals` (auto-fixable), `require_in_note`, `forbid_in_note` (report-only). Rules with nested
   quantifiers or credential-looking paths are refused; all rules run in a time-boxed child process (30 s). For
   logic a rule can't express, add a coded `check_*()`.
   Rules and the recurrence log live beside the engine (`~/.hermes/scripts/learned_rules.json`, `.vault_audit_state.json`).

## Keeping in sync (Ham's rule)
After changing the logic, keep `scripts/vault_audit.py` in this skill dir byte-identical to the live
`~/.hermes/scripts/vault_audit.py`, run the offline suite `python3 scripts/test_vault_audit.py` (never touches real
data), bump the version, and document the change in `references/checks.md` + `CHANGELOG.md`.

## References
| File | Read when |
|------|-----------|
| `references/checks.md` | Adding/changing a check; what each check anchors on; verification steps; known limits |
| `references/drift-patterns.md` | A new kind of drift was found — the taxonomy + the record → mechanise protocol |
| `references/memory-cleanup.md` | Ham asks to "clean up memory" / audit MEMORY.md against the vault |
| `CHANGELOG.md` | History and the reasoning behind each safety rule |
