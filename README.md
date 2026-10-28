# hermes-skills

Ham's own Hermes Agent skills (the "Built by Ham" section of the Obsidian *Skills MOC*) and the cron wrapper scripts that run them, version-controlled so they can be edited away from home.

The live copy runs on the Raspberry Pi (`amh@amhpi5.local`). This repo mirrors it:

| Repo path | Live path on the Pi |
|---|---|
| `skills/<category>/<skill>/` | `~/.hermes/skills/<category>/<skill>/` |
| `scripts/` | `~/.hermes/scripts/` (Ham's skill wrappers only) |
| `data/morning-brief/interests.json` | `~/.hermes/morning-brief/interests.json` |
| `tools/pi_sync.py` | the Pi sync job (cron runs a self-tested copy, `.sync/pi_sync_stable.py`) |
| `tools/mac_pull.sh` | keeps the Mac clone current (launchd runs an installed copy) |

## How sync works

On the Pi, cron runs the sync every 10 minutes from `~/hermes-skills-repo`. Each run:

1. **Capture**: edits made on the Pi (by Hermes or over SSH) are committed as `pi: live edits - ...`. The baseline is the last commit applied to live. A live file counts as an edit only if it differs from both that baseline and HEAD, so a file that is just waiting for an incoming change is never captured as a revert.
2. **Rebase** onto `origin/main` to pick up edits pushed from elsewhere, then **push**.
3. **Check** every file it is about to write: `py_compile` (3.11) on `.py`, `bash -n` on `.sh`, frontmatter on `SKILL.md`, parse on `.json`.
4. **Apply** the files changed in git since the last apply, keeping each live file's mode. Runtime state (DBs, caches, logs, `.bak-*`) is gitignored and never touched. Finally, tag `pi-applied` moves to the commit now live.
5. After a clean run, if `tools/pi_sync.py` has changed, the new version is promoted to the copy cron runs, but only if it passes `--self-test`.

Every run that moves commits posts one 📦 message to the Discord playground channel. It lists the Pi's own edits (**Pi → GitHub**) and the GitHub commits it just deployed (**GitHub → Pi**), with links.

When something goes wrong, the job alerts the same channel:

- **Check failed**: nothing is applied and the sync is *blocked* until a fixed commit lands. Capture keeps running.
- **Rebase conflict**: nothing is applied. The Pi's commits are pushed to branch `pi-conflict`. Merge that branch into `main` (see CLAUDE.md); the next run applies it and deletes the branch.
- **Refused capture**: a live file over 1 MB, a binary file, or a file containing a secret (a credential value from `.env` or a token pattern) is not committed. The live file is left alone.
- **Mass deletion**: if more than 5 tracked files, or any `SKILL.md`, vanish from live, the deletion isn't captured. A missing root dir pauses the sync. To really remove a skill, delete it in git.
- **Overwrite**: when a GitHub change lands on a live file holding an edit git hasn't seen, the live copy is kept as `<file>.bak-pisync-<timestamp>` first.
- **New sync script rejected**, or **sync looks hung** (lock held and no successful run for 2 h).

Deleting a file in git deletes it on the Pi. Adding a new skill means creating `skills/<category>/<name>/SKILL.md`, and the directory is mirrored from then on.

Logs and state on the Pi: `~/hermes-skills-repo/.sync/` (gitignored).

## Mac auto-pull

launchd job `com.anthonyhollen.hermes-skills-pull` runs `~/Library/Scripts/hermes-skills-pull.sh` every 10 minutes. That's an installed copy of `tools/mac_pull.sh`, so a push can't change what runs on the Mac. It fast-forwards `~/hermes-skills` to GitHub, and skips the run while there are uncommitted changes to tracked files or unpushed commits. Log: `~/Library/Logs/hermes-skills-pull.log`.

To install or update it (after reviewing the script):
```
cp tools/mac_pull.sh ~/Library/Scripts/hermes-skills-pull.sh
cp tools/com.anthonyhollen.hermes-skills-pull.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.anthonyhollen.hermes-skills-pull 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.anthonyhollen.hermes-skills-pull.plist
```
