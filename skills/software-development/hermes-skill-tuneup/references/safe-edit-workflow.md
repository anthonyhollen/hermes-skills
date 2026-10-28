# Safe Edit Workflow (editing skills on the Pi)

Skills live under `~/.hermes/skills/` outside any git repo, so there's no `git checkout` to undo a bad edit. Follow this for **every** change.

## 1. Back up before overwriting
```bash
ssh amh@amhpi5.local "cp '<file>' '<file>.bak-<tag>-<date>'"   # e.g. .bak-precurl-20260712 — descriptive tag + date
```
Use the **`<file>.bak-<tag>-<date>`** form (the convention actually in use — a descriptive tag plus a date, so multiple backups of one file sort chronologically and don't collide). Note it does **not** end in `.bak`; the auditor's stray-file check matches both `*.bak` and `*.bak-*` for exactly this reason (§6). Keep backups only until the user confirms a live run; then delete them (clutter otherwise) — the auditor will keep listing them under `[STRAY FILES]` until you do.

## 2. Edit locally, push over SSH
**First confirm your local copy matches the live file** (`shasum -a 256` both ends) — otherwise you may clobber drift you never saw (a sibling cron-deployed copy edited since you pulled, or a change from another session). **A background skill-curator edits skills *between your passes*:** Hermes runs an "update the skill library" agent after Discord sessions (visible in `agent.log` as a `bg-review` "Be ACTIVE" turn) that rewrites `SKILL.md`/scripts with **no version bump or changelog**, and can **misdiagnose** — in one session it "fixed" a red-herring (a carpark-code note) and renamed map markers while the real delivery bug went untouched. So re-pull + `diff` the live file **before every push**, not just once at tune-up start; reconcile its undocumented edits *into* yours instead of clobbering them; and verify its changes like any other diff (don't trust them as correct). Then edit the local copy (clean diffs), push, and restore permissions to match siblings (mode `600`):
```bash
ssh amh@amhpi5.local "cat > '<remote path>'" < /tmp/<localfile>
ssh amh@amhpi5.local "chmod 600 '<remote path>'"
```
Editing in place over SSH with `sed`/heredocs is fragile (quoting, backticks) — push whole files instead. Beware: backticks inside an `ssh '… <<PY … PY'` heredoc break the remote shell; run such validators from a local file via `ssh host 'python3 -' < local.py`.

**Batch remote work into ONE ssh connection — a rapid loop of many `ssh host '…'` calls can trip a local outbound-connection block and lock *you* (the orchestrator) out.** This is the tuner-side cousin of the reviewer/fail2ban lockout in `bug-hunt.md`, but the trigger is *your own* burst, not the reviewers': a `for f in …; do ssh host "check $f"; done` (e.g. hashing 5 files one-ssh-each) fired ~5 connections in a second and every subsequent connect returned `ssh: connect … Operation not permitted` (**EPERM**, a *local* policy/rate block — distinct from fail2ban's `Permission denied (publickey)` on the *remote*). A single `ssh` after a short pause worked immediately, confirming it's a rate/burst block, not a ban. Do all remote steps in one connection: **pull once** (`tar czf - <files> | …` on the host → one stream), **push once** (`tar czf - <files> | ssh host 'tar xzf - -C ~ && chmod … && echo ok'`), and **validate in one scripted call** (`ssh host 'bash -s' < validate.sh`). If you do get EPERM, **stop looping and wait one quiet interval** (retrying in a tight loop just re-arms the block) — then resume with a single batched call.

**Deploy + validate in ONE connection, every time** (task-tracker 2026-09-24: ~3 ssh calls per deploy × 6 gate-lap redeploys tripped the Pi's ban mid-tune-up even though every reviewer was offline): `tar -C <local_skill_dir> -cf - scripts SKILL.md references | ssh amh@amhpi5.local 'tar -C ~/.hermes/skills/<cat>/<skill> -xf - && chmod 600 … && <venv> -m py_compile … && <suite>'`. On a lockout, stop — one probe per ~30 min at most.

**After pushing, `diff` the live file against your edited local copy and confirm the diff is *exactly* your intended hunks — nothing more.** The pre-edit `shasum` catches drift *before* you start; this catches the other half — a stray edit you didn't mean to make, or drift that landed between pull and push. (In the email-gmail-triage tune-up, a `ssh host 'cat <file>' | diff - <local>` after each of two fix rounds confirmed the diff showed only the four/two intended changes and the prior round was untouched.)

## 3. Compile & dry-run — with the interpreter that actually runs it
A `no_agent` cron script does **not** run under `/usr/bin/python3`; the scheduler invokes it with the **gateway venv** interpreter (`~/.hermes/hermes-agent/venv/bin/python`). Import availability differs there — user-site is disabled, so `~/.local` packages are absent; `PIL` is missing unless the script inserts `/usr/lib/python3/dist-packages` on `sys.path`; `filelock`/`tabulate` live in the venv. Compiling under system `python3` gives false confidence. Use the real interpreter:
```bash
VENV=/home/amh/.hermes/hermes-agent/venv/bin/python
ssh amh@amhpi5.local "$VENV -m py_compile '<script>'"            # syntax, under the real interp
ssh amh@amhpi5.local "$VENV '<script>' --json"                   # compute path unaffected?
# Dry-run a delivery script without sending: stub curl on PATH, confirm exit 0 + first stdout line == [SILENT]
ssh amh@amhpi5.local "mkdir -p /tmp/stubbin && printf '#!/bin/sh\nexit 0\n' >/tmp/stubbin/curl && chmod +x /tmp/stubbin/curl && PATH=/tmp/stubbin:\$PATH $VENV '<script>'"
```
Re-run `scripts/skill_audit.py` to confirm the token budget / links / anti-patterns improved.

**If the target skill ships its own regression suites, they ARE your validation harness — run all of them against the *deployed* copy as the final gate, and add a case per fix.** A skill that already has committed tests (e.g. email-gmail-triage's four `test_*.py`) gives you a far stronger gate than ad-hoc probing: run them before your change (baseline green), after (still green), and extend them so each bug you fixed has a regression that would catch it coming back. Caveat: a suite that **hardcodes the live script path** (`spec_from_file_location("gt", "/home/amh/.hermes/scripts/gmail_triage.py")`) can't validate an edited copy in isolation — either run it after deploying to that path, or `sed`-swap the path into a temp copy to validate locally first (some suites already accept a `GMAIL_*_PATH`-style env override — prefer that). Mirror any new test mock to the changed signature (a helper that started returning a bool will break a `lambda: None` mock).

**The `patch` tool can mangle when replacing entire function bodies.** If `old_string` captures a partial function (e.g. the first few lines), the tool's fuzzy matching may splice the replacement inside the existing function, creating a nested `def` or duplicated code. For multi-line replacements that span an entire function, either: (a) use `execute_code` with the `patch` helper for precise control, or (b) include enough unique context around the function (the `def` line + a distinctive comment or decorator above it) so the tool can identify the full boundary. Always `py_compile` after any `patch` call that replaces ≥5 lines — a mangling produces valid-looking but broken Python.

**A multi-block `patch` replace can silently drop lines between the old and new spans.** When `old_string` covers lines A..D and `new_string` covers A'..D', any line between A and D that is *not* reproduced in A'..D' vanishes — including initializations, guard clauses, and comments the editor assumed were "outside" the replacement. (food-recommender: replacing a block from `BATCH_SIZE = 10` through the loop dropped `scored_idx = 0` because it sat between the captured old_string boundary and the loop — the replacement re-initialized `result` and `result_keys` but forgot the index variable; `py_compile` passed but a live run hit `NameError`.) Tell: when a patch spans ≥2 logical blocks, read back the *resulting* file around the edit boundary, not just the diff. Or prefer surgical per-block patches over one mega-replace.

## 4. Validate outward actions WITHOUT side effects
Never fire a real send/post/write to verify. Instead:
- Read-only probe (e.g. `GET /channels/{id}` for Discord) confirms auth + reachability, posts nothing.
- Then **ask the user** before any single live test (a thread post / message is visible to others; a write mutates real data).

## 5. Record the change
- Bump `version:` in the frontmatter.
- Add a `CHANGELOG.md` entry: root cause → fix → how it was validated. Put it in `CHANGELOG.md`, **not** SKILL.md (hot-path tokens).
- **Sync the docs to the code in the same change — required.** Update the target skill's `SKILL.md`, `CLAUDE.md`, and `references/` for anything your edit changed (constants, function names, flow, behavior, removed helpers) and **grep the *entire skill dir* for the old values/names** — `grep -rn '<old-name>' <skill_dir>`, **every file, not just `SKILL.md`** (the same recursive sweep the secret-scrub rule below uses). This is the only thing that catches a stale copy an earlier token/relocation pass moved into `references/`: that moved-out copy is the most likely to rot, because the value it named was just deleted from `SKILL.md`, so a SKILL.md-only grep finds nothing — and a *prose* description (naming no greppable constant) can slip a value-grep entirely, so re-read relocated behavior docs semantically too. Shipping a code change without updating its docs is exactly how doc↔code drift (a Pass-1 bug-class) gets created — don't leave new drift behind. For dated historical/diagnosis docs, add a dated correction note rather than rewriting the record. (See *Always finish a tune-up with* in SKILL.md — this is the per-change half of that closing discipline.)
- **If the fix removed a hardcoded secret, `grep` every file in the skill to confirm it's gone** (`grep -rn '<key-prefix>' <skill_dir>`). It's easy to scrub the script and miss a copy pasted into `SKILL.md` or a reference — and the secret should still be **rotated** at the provider regardless, since it was committed.
- **When migrating a hardcoded key to `~/.hermes/.env`, verify the key is *uncommented and functional* in the env file — not just present as a comment.** A `# GOOGLE_API_KEY=your_key_here` placeholder in `.env` looks like "the key is there" on a grep but returns `None` at runtime. After adding or uncommenting the key, *load and print it* from the script to confirm it reads correctly. (food-recommender: `.env` had `# GOOGLE_API_KEY=your_google_ai_studio_key_here` — a template comment, not a live value. Discovered only when a runtime probe returned `None`.)
- Keep any deployed copy in sync. Some skills deploy a script to a second path (e.g. `~/.hermes/scripts/<x>.py` for cron) that must stay **byte-identical** to the skill's `scripts/` copy — check the skill's `CLAUDE.md`, and deploy your fix to **both** (see SKILL.md *Phase 0* / bug-hunt.md Step 0).

## 5b. Changing a cron job's config — use the CLI, never hand-edit `jobs.json`
The gateway rewrites `~/.hermes/cron/jobs.json` when it persists run state, so a hand-edit races the scheduler and can be clobbered. Use `hermes cron edit <job_id> --deliver … / --schedule … / --script … / --no-agent` (race-free, the gateway reloads). Two cron-config bugs worth checking during a tune-up: a **`deliver` target pointed at the wrong channel/thread** (e.g. a test/playground id left in production), and a **stray `profile`** — for `no_agent` jobs `profile` must stay unset or `script` resolves under `~/.hermes/profiles/<profile>/scripts/` and fails `Script not found`.

## 6. Clean up
**Order matters — "done" comes last.** The independent-reviewer fan-out **on your own applied edits** (now the **terminal validation gate** in `SKILL.md`, run over the *cumulative* diff of **all** passes — not just the bug hunt) and the doc-sync (§5) are part of finishing, not afterthoughts: run them *before* you delete backups or declare the tune-up complete. (Learned the hard way: shipping fixes and deleting backups first, then reviewing, meant a regression had already shipped and the user had to prompt the review — do it inside the change, while the backups still exist.)
**Remove your Phase 0 kit and sandboxes from the host's `/tmp` in the deploy connection, not "later"** (2026-10-04): agents roam `/tmp`. On cc-bill-review's first live run, an agent investigating its own output grepped through a 42 MB kit (including a mode-644 export of state.db sessions) and a stale copy of the old skill. **After the first live run, check `~/.hermes/skills/.curator_ledger.jsonl`** for skills the post-session curator created or patched in reaction (that run produced a global `script-relay` skill). A skill it **created** (`created_by: agent` in `.usage.json`): ask the user, then `hermes curator archive <name>` (recoverable). Never archive a skill it only **patched** — that disables a real skill. Diff the patch (the ledger keeps before/after sha256) and reconcile or revert it as in §1. `hermes curator purge` deletes **every** expired archive, not just one. **A scheduled cleaner can delete your backups before the user confirms:** the Pi's weekly "Backup Cleaner" cron (Sun 11:05) removed cc-bill-review's `.bak-*` files 2 days after the deploy. Check they still exist before promising a rollback.

After the user confirms the live run works, delete the backup files (and scan for strays: `find <skill_dir> \( -name '*.bak' -o -name '*.bak-*' \)` — the bare `-name '*.bak'` misses the `.bak-<tag>-<date>` convention, exactly the hole that let ~20+ backups pile up unseen in this skill's own dir; the auditor's `[STRAY FILES]` now catches both).

## Reversibility & honesty
- Look before you overwrite: if a file's contents contradict how it was described, surface that instead of blindly replacing it.
- Report outcomes faithfully — if you validated by read-only probe and not a live send, say exactly that.

## Don't push into an imminent scheduled run — stage on the host instead (morning-brief 2026-09-15)

When the target's next scheduled run is minutes away and your latest round is compile-clean but
not yet verified *on the host*, do **not** push to the live path to find out. Copy the changed
files to a scratch dir on the host (`/tmp/<skill>-rN/`), import them from there with every
output path redirected to `/tmp`, and run the probe suite against those copies. You get real
verification at the host's real dependency versions — which matters whenever the change touches
library internals (urllib3's `raw.stream`/socket attributes behave differently across major
versions, and the Pi is often several behind your laptop) — with zero risk to the run. Push
after the run completes, or once the scratch suite is green and you still have margin to revert
from the `.bak` copies.

Corollary: **a scheduled run is itself the best live validation you have.** Letting it fire on
the last fully-verified state, then reading its archived output, is both the confirmation the
cleanup step waits for and a free end-to-end test at the real trigger time.

## Reviewer fan-out can lock YOU out of the host — and a locked-out reviewer reports "clean"

`bug-hunt.md` already warns that parallel reviewers trip the remote's rate limiting. Two
additions learned the hard way:
- The lockout hits the **orchestrator** too: a burst of reviewer connections got the ban, and
  every subsequent `ssh` returned `Permission denied (publickey,password)` for ~10 minutes.
  Separately, a passphrase-protected key can silently drop out of `ssh-agent` mid-tune-up, and
  only the user can re-add it (`ssh-add ~/.ssh/<key>` prompts on a tty you don't have). Ask
  rather than retrying.
- **A reviewer that loses host access will still return a report.** One came back with zero
  findings and a page of "unexecuted leads from reading the code" — correctly labelled, but a
  less careful one would have read as a clean pass. Require reviewers to state explicitly that
  they *ran* their probes and to paste the output, and treat "no findings" from an agent whose
  tool calls failed as **no result**, not a pass. (Those static leads were still valuable: two
  of them were real and are fixed — but only after being executed.)
- Fan-out has a **budget** cost as well as a rate-limit one: one confirmation-gate reviewer
  died on a daily spend cap mid-run. For late gate rounds, prefer running the probe suite
  yourself over spawning another reviewer.

## Sandboxed runs on the host: override HOME, keep the user site (morning-brief 2026-09-26)

To run a skill whose scripts hardcode `~/.hermes/<data>` against a COPY of its data, run it
with `HOME=/tmp/<sandbox>` and a sandbox `.hermes/` that holds a copy of the data dir plus
symlinks to everything else (`config.yaml`, `.env`, `hermes-agent/`, …) so model/credential
resolution still works. Two gotchas: overriding `HOME` also hides `~/.local`, so user-installed
packages vanish (`ModuleNotFoundError: feedparser`) — add `PYTHONUSERBASE=/home/<user>/.local`;
and the gateway venv may lack the skill's deps entirely, so run the steps and the suite with the
interpreter the wrapper uses (`/usr/bin/python3` here). Check the live data files' mtimes before
and after to prove the sandbox run touched nothing.

## Check the HOST clock before any time-relative cron change (foodpanda 2026-09-28)

A one-shot `hermes cron create "0 10 28 9 *" … --repeat 1` meant for "10:00 today" was scheduled for **next year** — the tuner had assumed from the local session date that it was early morning on the Pi, but it was already 10:33 SGT. Run `ssh host date` (one call, or inside the batched deploy) before deciding whether a schedule change fires "today", and always print `next_run_at` after `hermes cron create/edit`. Knowing the real host time also removed the need for the one-shot entirely (the Monday slot had already passed, so the schedule could be switched directly with no duplicate run).

## One tar per skill — never mix two skills' files in one archive (foodpanda 2026-09-28)

A batched deploy packed the target skill's docs AND this skill's writeback into ONE tarball (`tar -C skillA SKILL.md … -C skillB SKILL.md …`). Both have a `SKILL.md` at the same relative path, so on extraction the second overwrote the first in staging, and the target skill's `SKILL.md` went live as *this* skill's file for about a minute, until a post-deploy hash check caught it. The drift guard only checks the files *before* the write, so it can't catch this. **Stage each skill in its own subdirectory (`skillA/…`, `skillB/…`) or send separate archives, and always re-hash every pushed file against its local source after the push, as the final step of the same connection.**


## Selftest sandboxes must isolate EVERY side channel; slice-edits need an undefined-name check (cc-bill-review 2026-10-02)

- A new regression suite pointed `DB_PATH` at a temp DB but not the skill's `db_snapshots/` dir, which `init_db()` writes to and trims to the last 5 — so five suite runs evicted the one-shot migration's rollback snapshot (two gate reviewers caught it). Its import tests also logged `fake.pdf` imports into the real `skill.log` (the July tune-up's stray entries came from the same cause). Patch every path a code path can write (DB, snapshot dir, log handlers, caches) inside the sandbox context manager, and assert the real dirs are unchanged after a run.
- Removing a "dead" function by slicing the file between two anchors deleted a LIVE function that sat between them (`display_ym`); only the new crash guard's message revealed it. After any removal run an AST pass that every loaded name is defined (imports, defs, assignments, args) — the *removing a dead function orphans its imports* class, inverted.
- A mutation runner must pass `stdin=subprocess.DEVNULL` (and per-test fake stdins): a mutant that reads stdin hangs the whole run instead of failing.
