# CLAUDE.md - working on hermes-skills

This repo is the editable mirror of Ham's Hermes skills, which run live on a Raspberry Pi. See README.md for the path mapping and sync mechanics. **Every push to `main` goes live on the Pi within ~10 minutes**, and cron jobs then run that code unattended. Treat a push as a deploy.

## Workflow (from the Mac / Apple Claude Code)

1. `git pull --rebase` first. The Pi pushes its own `pi: live edits` commits.
2. Read the skill's `SKILL.md`, plus its `CLAUDE.md` / `CHANGELOG.md` if present. Many skills keep a version and changelog there: bump them the way the existing entries do.
3. Make the change. Don't create `.bak-*` copies; git history is the backup.
4. Check before committing:
   - `python3 -m py_compile <changed .py files>`. The Pi checks with Python **3.11** and cron runs scripts on 3.11/3.14, so use only 3.11-compatible syntax.
   - `bash -n <changed .sh files>`
   - Run the skill's tests if it has any (e.g. `scripts/test_*.py`) and they don't need Pi-only resources (Hermes home, `.env`, Discord, live APIs).
5. Commit with a clear message and push to `main`.
6. Confirm the sync applied it. The Pi has no SSH access from outside, so check GitHub instead: the Pi's next push, or no alert in the Discord playground channel.

## Constraints

- **No secrets in the repo.** Tokens live in `~/.hermes/.env` on the Pi; scripts read them at runtime. Never hardcode one.
- **Don't trigger paid LLM calls or post to real Discord channels** while testing locally. Live test messages go only to the playground channel.
- Paths in code reference the Pi (`/home/amh/.hermes/...`, `~/.hermes/...`). Keep them; the code runs there, not on the Mac.
- `scripts/` wrappers are cron entry points (`~/.hermes/cron/jobs.json` names the script). Renaming one breaks its cron job unless jobs.json changes too, and jobs.json is not in this repo.
- Changing `tools/pi_sync.py` changes the deploy mechanism itself; a broken sync script can only be fixed over SSH. Change it with care, and `py_compile` it.

## If the sync is blocked or conflicted

- **Blocked** (a check failed): fix the file and push. The next run retries automatically.
- **Conflict**: `git fetch origin pi-conflict && git merge origin/pi-conflict`, resolve, commit, push `main`.
