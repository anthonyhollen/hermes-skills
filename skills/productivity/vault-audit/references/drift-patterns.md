# Vault Audit — drift-pattern taxonomy (the learning log)

The growing memory of **documentation-drift classes** this audit knows about — the
analog of hermes-skill-tuneup's bug-class notes. Each new kind of drift we discover
gets recorded here, then (where possible) mechanised so future runs catch it
automatically. **This is how the skill gets better over time.**

## The feedback loop
1. **Discover** — drift is found that no current check covers (during reconciliation,
   a manual vault edit, or a `📌 recurring` review finding the audit itself flags).
2. **Record** — add an entry to the table below (manifestation + ground truth + status).
3. **Mechanise** — turn it into a check so it's caught for free next time:
   - **Data rule** (preferred, no code): append to `learned_rules.json`, e.g.
     `python3 ~/.hermes/scripts/vault_audit.py --learn-rule '{...}'`
   - **Coded check** (for logic a rule can't express): add a `check_*()` in
     `vault_audit.py`, append it to `CHECKS`, and document it in `references/checks.md`.
4. **Mark status** here: `coded:<check_name>` / `rule:<rule_id>` / `pending`.

## Learned-rule kinds (`learned_rules.json`)
Each rule: `{ "id", "kind", "note", "sev": "auto"|"review", "added", "why", ... }`.

| kind | extra fields | fixable | use for |
|---|---|---|---|
| `value_equals` | `anchor` (regex, group 1 = claimed value), `truth_source` (`config`/`profile`/`env`), `truth_path` (e.g. `model.default`, `pro.model.default`) | ✅ when `sev:auto` | a labelled value in a note that must equal a ground-truth value |
| `require_in_note` | `must` (literal string) | ❌ review | a fact that must remain documented in a note |
| `forbid_in_note` | `pattern` (regex) | ❌ review | a stale/deprecated string that must not reappear |

`--learn-rule` refuses: a `truth_path` that isn't a plain dotted path or contains a credential-looking word (KEY,
TOKEN, SECRET, PASS, AUTH, CRED, WEBHOOK, COOKIE, BEARER, SESSION, PRIVATE); a regex with a nested quantifier
(catastrophic backtracking would hang the unattended run); `sev:auto` on a non-`value_equals` rule; multi-line
fields; and writing over a rules file it can't read. An auto `value_equals` edits only when its anchor matches once
and the result re-reads as the truth.

`value_equals` example (auto-fixes a stale provider name in Operations):
```
python3 ~/.hermes/scripts/vault_audit.py --learn-rule '{"id":"ops-provider","kind":"value_equals","note":"Hermes Agent/Operations.md","anchor":"provider `([\\w-]+)`","truth_source":"config","truth_path":"model.provider","sev":"auto","why":"default provider drifted"}'
```

## Known drift classes
| Class | How it showed up | Ground truth | Status |
|---|---|---|---|
| Stale model name | Operations said `deepseek-v4-pro` after switch to `-flash` | `config.yaml model.default/fallback`, `profiles/*/config.yaml` | coded:check_models |
| Curator state | note claimed enabled/disabled out of step with config | `config.yaml curator.enabled` | coded:check_curator |
| Cron schedule expr | Routines table expr ≠ jobs.json | `cron/jobs.json schedule.expr` | coded:check_crons |
| Schedule gloss vs expr | "08-21" gloss while expr ran every hour (the triage bug) | jobs.json expr (heuristic) | coded:check_crons (review) |
| Skill version | per-skill note version behind SKILL.md | `SKILL.md` frontmatter `version` | coded:check_skills |
| MOC grouping vs origin | a skill listed under the wrong Built-by-Ham/Bundled group | each skill note's origin marker | coded:check_moc_grouping |
| VIP roster | family VIP in skill missing from vault roster (or vice versa) | email-gmail-triage `VIP_EMAILS` | coded:check_vip (review) |
| Dangling wikilink | `[[X]]` with no matching note | the vault's note set | coded:check_wikilinks |
| Missing skill path | a documented skill's install dir gone (renamed/archived) | `skills/**` on disk | coded:check_skills (review) |
| Env-key drift | API Reference names a key not in `.env` | `.env` key names | coded:check_env_keys (review) |
| Archive bookkeeping | `.archive/` skill not noted in MOC Archived (or listed but not archived) | `skills/.archive/` listing | coded:check_archive (review) |
| Paused marker | Routines Name cell missing/keeping `**[PAUSED]**` vs the job's state | jobs.json `enabled`/`paused_at` | coded:check_crons (auto, 1.2.0) |
| Orphaned cron output | `cron/output/<id>/` left behind by a deleted/one-shot job | jobs.json ids | coded:check_orphaned_output (auto → quarantine, 1.2.1/1.3.0) |
| Auto-fix leaves the human copy stale | the expr fix rewrote `28 7` → `25 7` but left "07:28" beside it (live row `b617d89da15b`, found 2026-10-02) | jobs.json expr | coded:check_crons (time fixed with the expr, else review) |
| Script column drift | Routines says `baby_calendar_check.py`, job runs `calendar_check.py` | jobs.json `script` | coded:check_crons (review) |
| Header count drift | "22 active + 2 paused" vs 23 + 2 | jobs.json | coded:check_crons (review) |
| Retired job id in a note | Email Gmail Triage.md still names `edcb5a47beea` | jobs.json ids | coded:check_job_refs (review) |
| Stale schedule in a skill note | SG Commute.md `` `28 7 * * 2,3,4` `` / Morning Brief.md "07:28" next to the job id | jobs.json expr | coded:check_job_refs (review) |

_Append new classes below as they are discovered._
