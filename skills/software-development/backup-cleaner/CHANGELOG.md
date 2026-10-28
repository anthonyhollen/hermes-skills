# Changelog — backup-cleaner

## 1.3.0 — 2026-07-12

**Weekly cron now auto-deletes the validated set** (per user request — no more asking
permission every run). The DELETABLE class was always "the update was validated by a
successful post-update cron run", so auto-removing exactly that set is the criterion the
user asked for; the manual engine stays report-only by default (`--delete` to act).

- **Cron wrapper** `backup_cleaner_cron.py` now invokes the engine with `--delete` (in
  addition to `--post --quiet`): it auto-removes the validated backups and posts the list of
  what it removed. `NOT_YET` / `NO_CRON` / `ORPHAN` / `ANOMALY` are still report-only and
  never touched; every removal is logged to the manifest with its validating job id + run time.
- **Report/caption wording is now delete-aware.** `render_full`, `render_summary` and
  `post_report_image` take a `deleted` argument; when the run removed the set they say
  "🗑️ Auto-removed N validated backup(s), freed X. Logged to the manifest." (with an honest
  "M of N (… could not be removed — see logs)" on a partial failure) instead of prompting the
  reader to run a `--delete` that already happened. Headers/section labels switch to "removed".
- **`main()` now deletes before posting/printing** so the report states what was removed and a
  failed Discord post can never leave the validated set un-removed or its removal unlogged
  (deletion + manifest are the source of truth; the post is only notification). A failed post
  still dumps the report to stdout and exits non-zero (red in `hermes cron list`), unchanged.
- **Tests:** added report-wording regression checks (report mode prompts `--delete`; delete
  mode says "Auto-removed", drops the prompt, and lists a **Removed** section). Classification,
  resolution, delete-safety and delivery-contract checks unchanged and green.

## 1.2.0 — 2026-07-06 (hermes-skill-tuneup: full tune-up)

Full tune-up (bug-hunt fan-out → perf → delivery → terminal gate → docs). 3 blind reviewers
(control-flow · data-shape · delivery/security) + an adversarial refuter; every finding
reproduced on synthetic fixtures; a shipped regression suite added as the baseline. The
terminal gate **caught a regression in one of my own fixes** (see below).

**Delete-safety fixes (a backup could otherwise be validated against the WRONG unit):**
- **Live-file over-strip.** `resolve_live` could resolve a backup to an unrelated file: the
  `\.[\w]+\.bak$` marker over-stripped (`run.sh.bak`→`run`, `foo.py.minimax.bak`→`foo.py`).
  Removed that ambiguous marker (a `.<word>.bak` whose live file differs is now ORPHAN, not
  mis-resolved), and kept **longest-existing-candidate-first** resolution.
  - **Gate-caught regression:** an intermediate fix switched to *marker-order* resolution,
    which then mis-resolved a **backup-of-a-backup** (`X.pre-tuneup.bak` with both `X` and
    `X.pre-tuneup` present → the `.pre-*.bak` marker over-strips to `X`, which has a cron).
    Reverted to longest-first (prefers the intermediate `X.pre-tuneup`). Added a regression
    case reproducing it.
- **Script-match by basename → cross-tree collision.** A file merely sharing a cron script's
  basename could be validated by that cron. Now script-match requires the live file be
  exactly `~/.hermes/scripts/<script>` (where `no_agent` crons run).
- **Disabled jobs no longer validate** — a stale `ok` from an `enabled:false` job isn't evidence.
- **Naive `last_run_at` now interpreted as system-local** (`astimezone()`), not UTC — assuming
  UTC read a run up to the local offset later and could flip NOT_YET→DELETABLE. (Latent: live
  jobs are all tz-aware.)

**Robustness / delivery:**
- `do_delete`/`append_manifest`: `mkdir` the manifest dir before deleting; a corrupt/non-list
  manifest is preserved as `.corrupt-<ts>` (not silently discarded); a manifest-write failure
  dumps provenance to stderr so a deletion is never unrecorded.
- **Failed `--post` under the `deliver: local` cron is no longer silent** (vault-audit 1.1.2
  pattern): `main()` now prints the full report to stdout even under `--quiet` and exits
  non-zero on delivery failure; the cron wrapper passes `--quiet` and propagates the exit
  code (red in `hermes cron list`).
- `post_discord` now checks the real HTTP status (`-w %{http_code}`) instead of trusting
  curl's exit code (Discord 4xx with curl exit 0 was reported as success).
- Both posts send `allowed_mentions: {"parse": []}`; the text-fallback sanitizes interpolated
  file paths (backticks/mentions/link-syntax) — defense-in-depth for a crafted filename.

**Added:** `scripts/test_backup_cleaner.py` regression suite (classification, resolution,
delete-safety, backup-of-a-backup, disabled-job, delivery contract) — baseline-green and a
case per fix. **Perf:** full scan+classify of the whole tree is ~65 ms; nothing to optimize.
No change to live-tree classification (verified old-vs-new: 0 paths changed).

## 1.1.0 — 2026-07-06

Report format overhaul (per user feedback):
- **Paths are now shown relative to `~/.hermes`** (the scan scope) — the `~/.hermes/` prefix
  is dropped in every view (`_short` → `os.path.relpath` from `~/.hermes`).
- **Each item now shows its last-modified date** (`mod YYYY-MM-DD`) so you can see how old a
  backup is, in both the terminal report and the image table.
- **`--post` now sends the FULL deletable list as a Discord table image** (columns:
  File / Size / Modified / Validated ok-run), rendered via the `discord-table-format` skill
  and uploaded in one REST multipart call (content + file together — no `MEDIA:` tag, which
  the scheduler reorders). PNG magic-bytes are verified before upload; on any render/upload
  failure it falls back to the text summary. **No more `…and N more` truncation** — the image
  holds the whole list.
- **Weekly cron** now runs `--post <#alerts>` with `deliver: local` (engine self-posts the
  image) instead of delivering a truncated text summary via stdout. Still report-only, still
  silent when nothing is deletable.
- Added `discord-table-format` to `related_skills`.

## 1.0.1 — 2026-07-06

- **The report (and weekly Discord summary) now LISTS the deletable items**, not just
  counts. `render_summary` prints each deletable backup (`path` + size); if the list
  would overflow Discord's 2000-char limit it shows as many as fit and appends
  "…and N more — run `backup_cleaner.py` for the full list." The full terminal report
  (no args) already listed every item and still does.

## 1.0.0 — 2026-07-06 (initial)

New skill. Finds stale edit-backup files under `~/.hermes/skills/` + `~/.hermes/scripts/`
and deletes only those whose preceding update has been validated by a successful cron run.

- **Engine** `~/.hermes/scripts/backup_cleaner.py` (pure stdlib, zero tokens): scans for
  `.bak`/`.bak-<tag>`/`.<ts>.bak`/`.pre-*.bak`/`.orig`/`~` (skips `__pycache__`); resolves
  each backup's live file by existence-checking stripped-name candidates; finds the owning
  skill (nearest `SKILL.md` ancestor) and/or the live script name; classifies:
  - **DELETABLE** — a cron job matched by `skill` or `script` ran `last_status=ok` with
    `last_run_at` after the backup mtime.
  - **NOT_YET / NO_CRON / ORPHAN / ANOMALY** — report-only, never auto-deleted.
- **Safe by default:** reports and changes nothing; `--delete` removes only the DELETABLE
  set and appends each removal (path, size, mtime, validating job id + run time) to
  `~/.hermes/scripts/.backup_cleaner_log.json`. Also `--json`, `--quiet`, `--post <channel>`.
- **Weekly cron** (`no_agent`, Sun 11:00 SGT) runs `backup_cleaner_cron.py` → engine
  `--quiet` (report only) → stdout delivered to Discord; silent unless there are deletable
  backups. Deletion stays manual by design.
- Scope excludes the full-system `~/.hermes/backups/*.zip` snapshots (different retention
  rule) and `.pyc` bytecode.

Validated on the live Pi: report classified 39 deletable / 80 scanned, each tied to a
named successful run; `--delete` proven on a synthetic fixture to remove only the validated
backup (NOT_YET/ORPHAN/ANOMALY and the live file all preserved) and log it.

### Known limitations / future work
- A backup stored in a dedicated `backups/`/`.backups/` subdir whose live file lives
  elsewhere (e.g. `~/.hermes/scripts/backups/x.py.<ts>.bak` vs live `~/.hermes/scripts/x.py`)
  resolves as ORPHAN → report-only. Safe, but could be mapped in a later version.
- Skills whose cron `skill` field is null and whose code is only reached via a wrapper
  import (e.g. spotify-playlist-refresh) aren't linked to their cron → NO_CRON. Also safe
  (report-only); a wrapper-import scan could tighten this later.
