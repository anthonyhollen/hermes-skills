# CLAUDE.md - working on hermes-skills

This repo is the editable mirror of Ham's Hermes skills, which run live on a Raspberry Pi. See README.md for the path mapping and sync mechanics. **Every push to `main` goes live on the Pi within ~10 minutes**, and cron jobs then run that code unattended. Treat a push as a deploy.

The Pi sync posts a 📦 message to the Discord playground channel for every commit that moves (Pi → GitHub and GitHub → Pi), and alerts there when something goes wrong.

## Workflow (from the Mac / Apple Claude Code)

1. `git pull --rebase` first. The Pi pushes its own `pi: live edits` commits. A launchd job also fast-forwards this clone every 10 min, and it pauses while you have uncommitted changes.
2. Read the skill's `SKILL.md`, plus its `CLAUDE.md` / `CHANGELOG.md` if present. Many skills keep a version and changelog there: bump them the way the existing entries do.
3. Make the change. Don't create `.bak-*` copies; git history is the backup.
4. Check before committing. The Pi runs these same checks on every file it is about to write, and refuses the whole commit if any fails:
   - **Python:** `/usr/bin/python3 -c 'import ast,sys; [ast.parse(open(f,"rb").read(), f) for f in sys.argv[1:]]' <changed .py files>`. That is Apple's Python 3.9, which is stricter than the Pi's 3.11 check, so passing it guarantees the Pi accepts the file. Don't rely on Homebrew `python3` (3.14): it accepts syntax 3.11 rejects, e.g. `except A, B:` and f-strings that reuse the outer quote. If 3.9 rejects a genuinely 3.10/3.11-only construct (`match`, `except*`), confirm with `python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3,11))' <file>`, and avoid the 3.12 f-string forms by hand.
   - **Shell:** `bash -n <changed .sh files>`
   - **JSON** must parse. **SKILL.md** must start with a `---` frontmatter block.
   - Run the skill's tests if it has any (e.g. `scripts/test_*.py`) and they don't need Pi-only resources (Hermes home, `.env`, Discord, live APIs).
5. Commit with a clear message and push to `main`. If the push is rejected, run `git pull --rebase` and push again (unless you have just made a merge commit; see *Conflicts*).
6. Confirm it went live:
   ```
   git fetch -q origin --tags --force && git merge-base --is-ancestor <your-sha> pi-applied && echo live
   ```
   Within ~10 min the Pi moves tag `pi-applied` to the commit it deployed. That may be a `pi: live edits` commit on top of yours, hence the ancestor check. If it never passes:
   - A Discord alert explains it (blocked check or conflict).
   - If Discord is silent and `pi-applied` stopped moving, the Pi is down or offline. Fixing that needs SSH.

## What a push does on the Pi

- Changed and new files are written into the live paths (README.md has the mapping). New files get mode 600, or 700 if git marks them executable. Existing files keep their mode.
- **Deleting a file in git deletes it on the Pi.**
- Files the Hermes agent edited in the last ≤10 minutes may not be in GitHub yet. If your change lands on top of one, the Pi saves the live version as `<file>.bak-pisync-<ts>` and alerts.
- Changes to `tools/pi_sync.py` take effect only after the Pi has run them through `--self-test`. Cron runs a promoted copy, so a broken version is rejected with an alert, never run. Even so, keep changes to it small.

## Constraints

- **No secrets in the repo.** Tokens live in `~/.hermes/.env` on the Pi; scripts read them at runtime. Never hardcode one. The Pi refuses to capture files containing secrets, but a Mac push isn't scanned.
- **Don't trigger paid LLM calls or post to real Discord channels** while testing locally. Live test messages go only to the playground channel.
- Paths in code reference the Pi (`/home/amh/.hermes/...`, `~/.hermes/...`). Keep them; the code runs there, not on the Mac.
- `scripts/` wrappers are cron entry points (`~/.hermes/cron/jobs.json` names the script). Renaming one breaks its cron job unless jobs.json changes too, and jobs.json is not in this repo.
- **Twin copies must stay byte-identical.** Several scripts exist both as `scripts/<x>` (the copy cron runs) and as `skills/<cat>/<skill>/scripts/<x>` (the skill's canonical copy). Examples: food_recommender, foodpanda_*, sg_econ_*, token_report_no_agent, vault_audit*, backup_cleaner.
  1. **Before editing,** `cmp` the pair. Hermes often edits only the cron copy. If they differ, reconcile first (`git log -p -- <both paths>`) so you don't copy over its change.
  2. After editing one copy, copy it over the other and `cmp` again.
  
  The Pi warns when an applied commit leaves a pair different.
- `tools/mac_pull.sh` is installed as `~/Library/Scripts/hermes-skills-pull.sh`, and the launchd job runs that copy. Editing the repo file changes nothing until someone reviews the change and re-copies it.

## Conflicts (alert says "rebase conflict", branch `pi-conflict` exists)

```
git pull --rebase                      # main up to date first
git fetch origin pi-conflict
git merge origin/pi-conflict           # MERGE, never rebase/cherry-pick: the merge keeps the Pi's commits as ancestors
#   resolve, git add, git commit
git push origin main
```
- If that push is rejected, run `git pull --no-rebase` (or `git pull --rebase=merges`), then push. A plain `git pull --rebase` would flatten the merge and the conflict comes back.
- The Pi deletes `pi-conflict` itself once it has applied the merge. To check whether a conflict is open, use `git ls-remote origin pi-conflict` (your local `origin/pi-conflict` may be stale).

## Blocked (alert says "NOT applied - checks failed")

Fix the file and push. The next run re-checks and applies. The Pi keeps capturing Hermes' live edits in the meantime.
