# hermes-skills

Ham's own Hermes Agent skills (the "Built by Ham" section of the Obsidian *Skills MOC*) and the cron wrapper scripts that run them, version-controlled so they can be edited away from home.

The live copy runs on the Raspberry Pi (`amh@amhpi5.local`). This repo mirrors it:

| Repo path | Live path on the Pi |
|---|---|
| `skills/<category>/<skill>/` | `~/.hermes/skills/<category>/<skill>/` |
| `scripts/` | `~/.hermes/scripts/` (Ham's skill wrappers only) |
| `data/morning-brief/interests.json` | `~/.hermes/morning-brief/interests.json` |
| `tools/pi_sync.py` | the sync job itself (runs from `~/hermes-skills-repo` on the Pi) |
| `tools/mac_pull.sh` | keeps the Mac clone current (launchd, Mac only) |

## How sync works

`tools/pi_sync.py` runs on the Pi every 10 minutes from the system crontab:

1. **Capture**: edits made on the Pi (by Hermes or over SSH) are committed as `pi: live edits - ...`.
2. **Rebase** onto `origin/main` to pick up edits pushed from elsewhere, then **push**.
3. **Check** each incoming file: `py_compile` on `.py`, `bash -n` on `.sh`, frontmatter on `SKILL.md`, parse on `.json`.
4. **Apply** only the files that changed in git since the last apply. Runtime state (DBs, caches, logs, `.bak-*`) is gitignored and never touched. Afterwards, tag `pi-applied` is moved to the commit now live.

When something goes wrong, the job alerts the Discord playground channel:

- **Check failed**: nothing is applied and the sync is *blocked* until a fixed commit lands. While blocked, capture is paused so the live code doesn't revert your commit.
- **Rebase conflict**: nothing is applied. The Pi's commits are pushed to branch `pi-conflict`. Merge that branch into `main` (use `git merge`, not rebase) and push; the next run fast-forwards.
- **A live file was edited since the last sync and GitHub also changed it**: the live copy is kept as `<file>.bak-pisync-<timestamp>` before it is overwritten.

Every run that moves commits posts one 📦 message to the Discord playground channel. It lists the Pi's own pushes (**Pi → GitHub**, e.g. Hermes' live edits) and the GitHub commits it just deployed (**GitHub → Pi**), with links.

On the Mac, launchd job `com.anthonyhollen.hermes-skills-pull` runs `tools/mac_pull.sh` every 10 minutes. It fast-forwards `~/hermes-skills` to GitHub, and skips the run while there are local edits or unpushed commits. Log: `~/Library/Logs/hermes-skills-pull.log`. To install it: copy `tools/com.anthonyhollen.hermes-skills-pull.plist` to `~/Library/LaunchAgents/`, then run `launchctl bootstrap gui/$(id -u) <that path>`.

Deleting a file in git deletes it on the Pi. Adding a new skill means creating `skills/<category>/<name>/SKILL.md`, and the directory is mirrored from then on.

Logs and state live in `~/hermes-skills-repo/.sync/` on the Pi (gitignored).
