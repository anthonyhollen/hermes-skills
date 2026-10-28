# sg-commute — editing notes (short; the long form is `references/developer-guide.md`)

This file is attached to the agent's context whenever it runs a script in this dir (e.g. every parking query), so it stays short.

- **Parking queries:** follow `SKILL.md` → run `scripts/nearest_carpark.py "<postal or address>" --send` once; it posts the report itself. Don't hand-post, don't read `DISCORD_BOT_TOKEN`, don't re-run after `[SILENT] Sent N/N`.
- **What runs where:** `scripts/nearest_carpark.py` (interactive, `/usr/bin/python3`; monthly `--refresh` via the system crontab). The two cron reports are `no_agent` scripts in `~/.hermes/scripts/` — `morning_commute.py` (job `b617d89da15b`) and `evening_weather.py` (job `c967008ca121`) — run by the gateway venv Python. `~/.hermes/scripts/verify_ecp_route.py` re-derives the Innovis waypoints.
- **Changing a report's text/format → edit its script** (the job `prompt` is never read). Schedule/target → `hermes cron edit <id> …`; never hand-edit `~/.hermes/cron/jobs.json`.
- **Editing safely:** this skill and its cron scripts are in git (github.com/anthonyhollen/hermes-skills; see hermes-skill-tuneup `safe-edit-workflow.md` §0), so git is the rollback and a `<file>.bak-<tag>-<date>` is optional. Compile + dry-run with the interpreter that runs it (venv for the cron scripts), dry-run the reports without posting (`morning_commute.py innovis|amk`, `evening_weather.py` just print), keep the parking table column order (user-specified), bump `version` in `SKILL.md` and add a dated entry to `references/changelog.md`.
- **Keys** come from `~/.hermes/.env` — never hardcode or print one.
