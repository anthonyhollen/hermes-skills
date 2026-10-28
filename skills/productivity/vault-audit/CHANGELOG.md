# Changelog — vault-audit

## 1.3.1 — 2026-10-15 (Hermes v0.22.0: PyYAML for the cron wrapper)
- **Problem:** since the Hermes v0.22.0 update (10-14 20:39), the scheduler runs no_agent
  scripts on its Python 3.14 dependency venv, which has no PyYAML.
  - `vault_audit.py` imports `yaml` at the top, so Sunday's `--fix --post-home` run would have
    crashed in the wrapper (rc 4). This was confirmed by importing the engine under the
    scheduler's interpreter.
- **Fix:** `vault_audit_cron.py` (both byte-identical copies) appends
  `~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages` before importing the engine,
  as the Spotify wrappers do. PyYAML is pure python and loads on 3.14.
- **Verified:**
  - a report-only engine run (no `--fix`, no post) completes under the scheduler's interpreter;
  - the suite passes.

## 1.3.0 — 2026-10-02 (hermes-skill-tuneup: bug hunt + delivery + docs)
Four blind reviewers (auto-fix/state · parser/data-shape · delivery/cron · usage forensics over 41 state.db
sessions) + an adversarial refuter, then a 3-lap terminal gate (regression + adversarial lenses; lap 3 simplified
the parts laps 1–2 kept breaking; laps 4–6 closed the rest, lap 6 passed; its three LOWs were fixed and verified by a
self-probed lap 7 re-running every lap's probes and fuzzers). All work on an offline replica of the Pi (real vault copy, real jobs.json); the
real vault was never mutated. New offline suite `scripts/test_vault_audit.py` (112 cases, every fixture under one temp root removed at exit, each mutation-checked
against the code it fixes); staged on the Pi under the venv and system Python 3.11.

**Destructive / data-loss (the unattended weekly `--fix`):**
- `gt_crons()` returned `[]` for a missing/unparseable `jobs.json` — and for a bare-list or raw-control-character file
  Hermes's own loader accepts — so `check_orphaned_output` rmtree'd EVERY `cron/output/<id>/` dir (and non-job dirs
  like `.git`) and reported it as "✅ Auto-fixed". Now: ground truth is three-valued (unknown ⇒ one review item, cron
  checks skipped), parsed like Hermes (`strict=False`, bare list, id-keyed map) but all-or-nothing on any bad entry;
  only 12-hex real dirs; never with an empty job list or when > max(3, 25%) of dirs look orphaned; and orphans are
  MOVED to `scripts/.vault_audit_orphans/<ts>/` (pruned after 30 days) instead of deleted.
- `_backup` used `shutil.copy2`, keeping the note's old mtime, so `_prune_backups(30)` deleted the fresh backup of any
  note untouched ≥ 30 days **in the same run** (Pi baseline: 5 fixes, 3 backups; the 09-27 run lost two). Backups now
  get a fresh mtime and pruning goes by the timestamp in the name, never this run's.
- The cron-expr auto-fix rewrote only the backticked expr, leaving "Tue/Wed/Thu 07:28 (`25 7 * * 2,3,4`)" (live row
  `b617d89da15b`) which the audit then called clean. The single HH:MM is now fixed with the expr; anything it can't
  reconcile sends the whole cell to review (and stays there) instead of half-fixing it.
- `curator.enabled` was auto-rewritten in Decisions.md and Operations.md, whose lines pair the flag with the decision
  ("disabled deliberately — do not re-enable") — contradicting the skill's own "decision rationale is never
  auto-edited" rule and leaving the prose contradicting the token. Now report-only in both.

**Silent false-clean:**
- VAULT wasn't realpath'd, so with a symlink anywhere in its path every `read()` "escaped the vault": 7 of 12 checks
  silently returned nothing, every fix was refused, and 5 false VIP findings appeared. A missing core note, a missing
  model/curator anchor, an unreadable `.env`/VIP file/rules file now produce "not verified" review items.
- Routines columns were positional (`cells[2]/[3]`): an escaped `\|` or an extra column broke the checks, and "PAUSED"
  anywhere in the row counted as the marker; the first `(`…`)` span was taken as the expr even when it wasn't one
  (`(`after 999de4b21eec`)` would be overwritten). Now: each row is read with its nearest preceding jobs-table header
  (GFM delimiter rows, CommonMark fences, a heading ends a table, no positional fallback), unescaped-pipe split,
  row-width check, the expr is the ONE cron-shaped token in the cell (named ones compared, never edited), it is
  auto-fixed only when the words around it stay true (same days, and a fixed time whose HH:MM is rewritten too or a
  minute-only change — "24/7", "08-21", "Sun", "daily" around a changed hour/day ⇒ review), compared semantically, marker
  forms only in the Name cell, one combined fix per row.
- Coverage the docs claimed but the code lacked: the Script column (checks.md listed `script` as ground truth), header
  counts, and (new `check_job_refs`) retired job ids / stale schedules quoted in other notes. Live drift found:
  `0613525a80ee` script, 22 vs 23 active, `edcb5a47beea` in Email Gmail Triage, 07:28 in SG Commute / Morning Brief,
  11:00 vs 11:05 in Backup Cleaner.
- Parsers: unquoted `version: 1.10` was read as `1.1` (and `--fix` would write it) — raw text now; BOM; the first
  "VIP senders" line (not the roster) was used; Family section needed a following heading; curator `"false"` string
  counted as true; wikilinks `[[Folder/Note]]`, `[[Note.md]]`, `[[A\|b]]`, links in code were false dangling; archive
  was a substring match over the whole MOC (now exact names in `## Archived`, both directions).

**Delivery / cron contract:**
- Weekly runs with review items exited 1 (archived as "script failed" with no content: 06-14, 07-12, 07-19, 07-26) and
  weeks that edited notes were "silent". The wrapper now prints the report (deliver:local only saves it), maps 1→0,
  and keeps 3 (post failed) / 4 (internal error) so a real failure shows as one.
- `--post-home` with no valid DISCORD_HOME_CHANNEL silently posted and printed nothing ⇒ exit 3 + report.
- Retries of a POST after a timeout/5xx could duplicate a message ⇒ per-message nonce + `enforce_nonce`.
- A negative/NaN `retry_after` crashed mid-post; no overall time bound; code-point (not UTF-16) splitting; `--post`
  argument interpolated unvalidated into the URL; error lines could echo request headers; footer omitted the fixable
  count. All fixed (240 s budget, UTF-16 split + part markers, snowflake-only `--post`, `flags: 4`).

**State / feedback loop:**
- "📌 recurring" counted every run, so bursts of manual runs (5 in 6 min on 07-20) produced "recurring (7x)" — the
  number the agent cited when proposing the 07-28 auto-delete change. Now distinct audit days; `_fid` keeps digits inside ids.
- `{"runs": null}` state crashed every run forever ⇒ backed up, reset, reported. A failing fix/backup/write no longer
  aborts the run; a top-level guard prints traceback + report (exit 4).
- `--learn-rule`: the secret filter only covered `env` suffixes (a `config` `*.api_key` rule printed the key) — now a
  strict path syntax + credential words anywhere; lookahead captures could splice text in every week — the edit must
  re-read as the truth; Python's `re` can't be interrupted, so rules now run in a child process with a 30 s budget
  (nested-quantifier regexes are also refused at learn time); a rule that can't be verified is reported; only
  `value_equals` may be auto; an unreadable rules file is never overwritten.

**Docs:** SKILL.md re-synced (exit codes, orphan quarantine, review-item handling, the cron contract; "Verification"
moved to checks.md); checks.md rewritten to the shipped behaviour; drift-patterns.md gained the paused-marker,
orphan and five new classes; memory-cleanup.md dated correction (trigger phrases must stay in memory).

## 1.2.1 — 2026-07-28 (retroactive entry, written 2026-10-02)
An agent session (user: "these are output for a one-time reminder, clean them out"; agent: "Want me to add a learned
rule so --fix auto-cleans them in future runs?"; user: "yes") patched `check_orphaned_output` in place so `--fix`
rmtree'd orphaned cron output dirs. No version bump, changelog, or doc change was made — SKILL.md, checks.md and this
file kept saying the check was review-only / "always human-confirmed". Superseded and documented by 1.3.0.

## 1.2.0 — 2026-07-20 (paused-marker auto-fix + orphan check reconciled to live)
- **`check_crons` paused-state is now `auto`-fixable** (was report-only). The `**[PAUSED]**`
  marker in a Routines row's **Name cell** is added when the job is paused/disabled in
  `jobs.json` and removed when it is active. Only the Name cell (`cells[2]`) is ever
  rewritten; the whole row is the exactly-once anchor handed to `apply_fix` (so the same
  backup / atomic-write / write-back-validation invariants apply). If `PAUSED` appears in
  the row but not as the canonical marker in the Name cell, it safely **downgrades to
  review** rather than guess. This retires the recurring `📌 consider codifying` finding
  for paused-state drift (e.g. `68c25a77dabd` Warframe).
- **`check_orphaned_output` (`cron-output-orphan`) is now actually running on live.** It was
  added to the skill-dir copy on 2026-06-14 and documented in SKILL.md / `checks.md`, but was
  never synced to `~/.hermes/scripts/vault_audit.py`, so the documented check had silently
  never run in production. The two copies are back in sync (12 checks). It surfaces orphaned
  `~/.hermes/cron/output/<id>/` dirs from deleted jobs as **review** (deleting dirs is
  destructive → always human-confirmed).

## 1.1.2 — 2026-06-13 (tune-up: delivery reliability + auto-fix safety)
Full bug-hunt via 3 independent blind reviewers (auto-fix/state · parser/edge-case · delivery/cron/safety lenses) + an adversarial terminal gate over the cumulative diff; every fix validated against synthetic `HERMES_HOME` vaults (the real vault was never mutated). The detect-mode baseline ("✓ Vault in sync — 11 checks passed") is unchanged.

**Delivery reliability (the weekly `deliver: local` cron self-posts, and the scheduler pushes no alert of its own):**
- `post_discord` now **returns success/failure**; on a failed required post the run **prints the report to stdout even under `--quiet`** (so the saved cron output isn't empty) and **exits non-zero** — a delivery failure is no longer silently lost.
- **429-aware retries** (honor `retry_after`, capped) + bounded back-off on 5xx/network; a 4xx (e.g. 50035) is permanent (no pointless retry). One failed chunk no longer silently drops the rest.
- `_split` now **hard-splits any single line** over the limit (and never emits an empty or >limit chunk) — a long dangling-link/learned-rule line can't trip Discord's 2000-char rejection.

**Auto-fix safety (the cron runs `--fix` unattended on the long-term-memory vault):**
- `check_models` patterns are **anchored to the definition line** (`^[\t *-]*` + label, prefix captured) so a mid-sentence/example mention of `default`/`fallback`/`pro` can't be edited instead of the authoritative line.
- `check_moc_grouping` **preserves the real "Built by Ham" header text** (was hard-coded, dropping any trailing text).
- Learned `value_equals` auto-fix is **skipped (→ review) when the anchor's value group matched blank**, preventing a splice into adjacent text; `--learn-rule` now **refuses an `env` rule on a secret-looking key** (KEY/TOKEN/SECRET/…) so a credential value can't leak into the report.
- `_prune_backups()` removes `.bak-audit-*`/`.tmp-audit-*` older than 30 days (they accumulated forever); it matches only the exact `<name>.(bak|tmp)-audit-<timestamp>` suffix, does **not** descend directory symlinks, and never deletes a path resolving outside the vault.

**Check coverage:**
- `check_crons` now also reads a **bare backticked schedule expr** (not only the parenthesised form) — a previously silent false-negative — but only when exactly one cron-shaped token is present in the schedule cell (no decoy guessing).
- `check_wikilinks` no longer false-flags Obsidian **embeds** `![[file.png]]`, matches link targets **case-insensitively** (as Obsidian resolves them), and reads the vault once instead of twice.

## 1.1.1 — 2026-06-13 (focused safety review)
- **MOC regroup is now data-loss-safe**: if a `Built by Ham` / `Bundled` section contains
  any non-bullet content, the auto-regroup downgrades to a *review* finding instead of
  rewriting (the rebuild only re-emits bullets, so other lines could have been dropped).
- **No finding can be silently dropped**: the report's review bucket is now "everything
  unresolved that isn't auto-fixable"; exit code is non-zero if *anything* is unresolved
  (unfixed drift or review), not only `review`-tagged items.
- **Config blind-spot surfaced**: if `config.yaml` is present but unreadable/empty, the run
  emits a warning instead of silently skipping (and falsely "passing") the model/curator checks.
- **Hardening**: path-containment so a learned rule's `note` can never read/write outside the
  vault; atomic writes (tmp + `os.replace`) for note fixes, the rule file, and the state log;
  `--learn-rule` validates `truth_source` and `sev`; removed dead code; `replace(...,1)` in fixes.

## 1.1.0 — 2026-06-13
- **Feedback loop / learning new drift types:**
  - `learned_rules.json` — declarative, data-driven rule registry evaluated each run
    (`check_learned_rules`). New drift types can be added as data (no code) via three
    kinds: `value_equals` (auto-fixable), `require_in_note`, `forbid_in_note`.
  - `--learn-rule '<json>'` / `--list-rules` to manage rules from the CLI/chat (validated, backed up).
  - **Recurrence detector** — each run is logged to `.vault_audit_state.json`; a review
    finding that recurs in ≥3 of the last 6 runs is tagged `📌 consider codifying`.
  - `references/drift-patterns.md` — the drift-class taxonomy + the discover→record→mechanise protocol.

## 1.0.0 — 2026-06-13
- Initial deterministic audit (10 checks) with `--fix` auto-repair (backups + exactly-once
  anchor guard), Discord self-post, and the weekly `no_agent` cron (`b807fa57ef4b`, Sun 10:30).
