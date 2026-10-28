# Vault Audit — check registry

Each check compares one class of vault claim against a local, structured ground-truth
source. No network, no LLM. Add a check = add one `check_*()` function in
`vault_audit.py`, append it to `CHECKS`, document it here, and add a case to
`scripts/test_vault_audit.py`.

## Ground-truth sources
| Source | File | Used by |
|---|---|---|
| Model config | `~/.hermes/config.yaml` → `model.default` / `model.fallback` | models |
| Profile models | `~/.hermes/profiles/*/config.yaml` → `model.default` | models (`pro`) |
| Curator state | `config.yaml` → `curator.enabled` (must be a real boolean) | curator |
| Cron jobs | `~/.hermes/cron/jobs.json` → `id`, `name`, `schedule.expr`, `script`, `enabled`/`paused_at` | cron, cron-ref, cron-output-orphan |
| Installed skills | `~/.hermes/skills/**/SKILL.md` frontmatter `name` + the raw `version:` text, excl. `.archive/`,`backups/` | skill-version, skill-path |
| VIP roster | email-gmail-triage `SKILL.md` (any category folder) → the line starting `VIP senders:` | vip |
| Env keys | `~/.hermes/.env` → key NAMES only (values never read except `DISCORD_*` ids/token) | env, ids |
| Archive | `~/.hermes/skills/.archive/` directory listing | archive |
| Vault itself | all `*.md` under the vault | moc, links, and every "claim" side |

`jobs.json` is parsed as leniently as Hermes's own loader (raw control characters, a bare list, an id-keyed map) but
all-or-nothing: missing, unparseable, or ANY entry without a clean string id ⇒ one "missing, unreadable…" review
item and every cron check is skipped. It is never treated as "no jobs".

## Checks
| Check | Severity | Vault target | Auto-fix anchor / notes |
|---|---|---|---|
| `models` | auto | Operations → Models/profiles | backticked model token after the **line-anchored** `default`/`fallback`/`pro` label; a missing label line ⇒ review "not verified" |
| `curator` | review | Operations, Decisions | `` `curator.enabled: <bool>` `` vs config. Both notes pair the flag with the decision behind it ("disabled deliberately — do not re-enable"), so a flip is reported, never rewritten; a missing token ⇒ "not verified" |
| `cron` (table) | — | Routines | a job row belongs to its table's header — the line above a GFM delimiter row (`\|---\|`, `\|:-:\|`, `---\|---`…); a heading/prose line ends a table (blank lines don't); code fences follow CommonMark. Only rows under a header with ID + Name + Schedule columns are checked/edited (columns by header name, cells split on unescaped `\|`, a missing trailing pipe tolerated) — no positional fallback; no such rows ⇒ review |
| `cron` (expr + time) | auto | Routines schedule cell | the ONE cron-shaped backticked token in the cell (parenthesised or bare), auto-fixed only when the words around it stay true: the days unchanged AND either a fixed time of day before and after (its HH:MM is rewritten too) or only the minute changed — otherwise ("24/7", "08-21", "every 2h", "Sun", "daily" around a changed hour/day, or a time written as "8am"/"7.30"/":00"/"evening") it goes to review; comparisons are semantic (`2-4` == `2,3,4`, dow 7 == 0); several exprs, or a named one (`MON-FRI`, `@daily`, normalised for comparison), are compared and never edited; plus the cell's single 24h HH:MM when the job fires at one time (rewritten only when the cell's expr is right). am/pm times are compared, never rewritten. Anything it can't reconcile sends the WHOLE cell to review with nothing edited — it recurs until a human fixes the row |
| `cron` (paused marker) | auto | Routines Name cell | canonical `**[PAUSED]**` added (a loose `[PAUSED]`/`(PAUSED)`/`**PAUSED**` in the Name cell is normalised) / removed; a marker form elsewhere on an active job's row ⇒ review; the bare word "paused" is not a marker |
| `cron` (row) | auto | Routines | one combined finding per row (expr + time + marker), so two drifts can't invalidate each other's anchor; a row whose cell count ≠ the header's ⇒ review |
| `cron` (script / header / presence / stale / hour-gloss) | review | Routines | Script column vs the job's `script`; `**N active + M paused**` vs jobs.json; active job missing from the table; a row whose job is gone (names the job — ask before deleting); "08-21"-style gloss on an every-hour expr |
| `cron-ref` | review | every other note | a backticked 12-hex job id no longer in jobs.json; on a line naming exactly one known job, a cron expr (compared semantically) or single HH:MM that disagrees with it |
| `cron-output-orphan` | auto | system | `cron/output/<id>/` (12-hex real dir, not a symlink) whose job is gone ⇒ `--fix` MOVES it to `~/.hermes/scripts/.vault_audit_orphans/<ts>/` (pruned after 30 days). Refused (review) when jobs.json is unknown, lists no jobs, has non-12-hex ids, or > max(3, 25%) of dirs look orphaned |
| `skill-version` | auto | per-skill note | the `(vX.Y.Z)` next to the skill's install path (raw frontmatter text — `1.10` stays `1.10`) |
| `skill-path` | review | per-skill note | — (a documented skill whose install dir is gone) |
| `moc` | auto | Skills MOC | rebuilds "Built by Ham" / "Bundled" lists from each note's origin marker (preserves the real section header text) |
| `vip` | review | Anthony (Ham), Email Gmail Triage | — (relationships are subjective); a roster line that parses to nothing ⇒ review |
| `env` | review | API Reference | — (never auto-touch the keys area); unreadable `.env` ⇒ review, not false findings |
| `ids` | review | Operations, Anthony (Ham) | — (channel changes span many notes; report only) |
| `links` | review | all notes | — resolved like Obsidian: by basename, `.md`-less, case-insensitive; `![[embeds]]`, links inside code, `\|` aliases in tables handled |
| `archive` | review | Skills MOC → `## Archived` | exact backticked names in that section, both directions (archived-but-unlisted, listed-but-not-archived) |
| `learned` | auto / review | per-rule note | data-driven rules from `learned_rules.json` (the feedback loop) — see `drift-patterns.md`. Evaluated in a child process with a 30 s budget (Python's `re` can't be interrupted) that reports rule by rule — a timeout keeps the finished rules' findings and names the slow rule; a rule it can't verify (truth path / anchor not found, unknown kind, credential-looking path) is reported; every anchor match must agree with the truth |
| `vault` / `state` / `prune` | review | (audit) | a core note that can't be read, a malformed recurrence state (backed up + reset), an unlistable quarantine — the audit reports what it could NOT check |

## Auto-fix invariants
1. Back up the note to `<note>.bak-audit-<ts>` (fresh mtime) before the first edit in a run.
2. Replace only when the anchor occurs **exactly once** in the file; otherwise downgrade to `review`.
3. Write atomically (temp + fsync + `os.replace`, the note's file mode kept, no temp left on failure) and re-read to
   confirm the change landed. One failing fix downgrades to review; the others still run.
4. Subjective notes and decision records (preferences, family relationships, decision rationale, the curator flag)
   are never auto-fixed.
5. `*.bak-audit-<ts>` / `*.tmp-audit-<ts>` and quarantined output dirs are pruned on each `--fix` run when the
   timestamp **in the name** is ≥ 30 days old (never by mtime, never this run's; no symlinks followed).
6. Learned `value_equals` fixes only when the capture lies inside the match, the anchor matches once, and the edited
   text re-reads as the truth through the same anchor.

## Recurrence signal
Every run is logged to `.vault_audit_state.json`. A review finding seen on ≥ 3 of the last 6 distinct audit DAYS is
tagged `📌 recurring` — same-day manual runs count once. The finding id masks free-standing numbers only (job ids are
kept). (1.3.0 changed the id for findings that contain ids — those restart their count once.)

## Delivery (`--post` / the weekly cron)
Text-only urllib JSON POST (User-Agent set; `allowed_mentions: {parse: []}`, `flags: 4`), token from `~/.hermes/.env`.
Messages are split at 1880 UTF-16 units (+ a `(part i/n)` marker). Each message carries a deterministic `nonce` with
`enforce_nonce`, so retries (429 honouring a sane `retry_after`, 5xx, network/timeouts) can't duplicate it; the whole
post is bounded by a 240 s budget (per-attempt timeout ≤ what's left). A failed or partial post ⇒ exit 3, stderr says
how many messages landed, the full report is in stdout (= the saved cron output). Error lines print the exception type
only.

## Verification
- `python3 scripts/test_vault_audit.py` — 112 offline cases on throwaway HERMES_HOMEs, network stubbed (needs the
  sibling `vault_audit.py` + `vault_audit_cron.py`; `VAULT_AUDIT_ENGINE` / `VAULT_AUDIT_WRAPPER` override them).
- A run on an in-sync vault prints "✓ Vault in sync".
- After deliberately editing a note (e.g. a wrong model name), a `--fix` run reports the fix, restores accuracy, and
  leaves a `.bak-audit-*` backup.

## Known limitations (candidates for v2)
- The cron *human gloss* is only checked for its time(s) (a single 24h HH:MM is also fixed) and the every-hour
  heuristic; days of week and a single time next to a multi-time expr (`30 7,19 * * *`) are not checked. A row with
  no backticked expr is only time-checked.
- `ids` is report-only by design — a channel id change touches many notes.
- Skill-note ↔ install mapping relies on the note embedding the `~/.hermes/skills/...` path; notes without a
  `(vX.Y.Z)` skip the version check.
- A fix rewrites a CRLF note with LF line endings (Obsidian writes LF).
- Learned-rule regexes with nested quantifiers are refused at `--learn-rule` (conservatively — write `([\d.]+)`, not
  `(\d+(?:\.\d+)*)`); slower shapes the static check can't see (e.g. `\w+.*\w+.*`) are caught by the 30 s child-process
  budget, which skips that rule and the ones after it for that run (reported by name).
