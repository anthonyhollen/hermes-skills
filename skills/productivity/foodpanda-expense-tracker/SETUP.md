# Food Expense Tracker — Setup

One-time setup; not needed once the cron jobs exist. Overview + I/O: `SKILL.md` · editing: `CLAUDE.md`.

## Prerequisites

- **Gmail + Sheets API** via the sibling `google-workspace` skill. Verify auth:
  ```bash
  cd ~/.hermes/skills/productivity/google-workspace/scripts && python google_api.py gmail list
  ```
  `~/.hermes/google_token.json` must include the `spreadsheets` scope (the sync builds its own Sheets service from it).
- **Discord bot token** in `~/.hermes/.env` (read directly, not via `os.environ`):
  ```
  DISCORD_BOT_TOKEN=your_bot_token_here
  ```
- **State files** `~/.hermes/foodpanda_sync_state.json` (per-provider watermarks) and `~/.hermes/foodpanda_reminder_state.json` (last reminded month) — created automatically on first run.

## Providers & lookback

Providers are configured in the `PROVIDERS` list at the top of `foodpanda_sync.py` (FoodPanda + Grab out of the box). Each has its own Gmail `after:` cutoff (`lookback`) — the earliest receipt to capture for that provider:
```python
"lookback": "2026/05/01",
```
Both default to `2026/05/01` so the combined tracker covers the same period. Grab receipts predate that (back to 2025); raise/lower a provider's `lookback` to change how far back its first run reaches. To add a provider, append a config entry (fields documented in `CLAUDE.md`) and confirm a real receipt parses before deploying.

## Create cron jobs

> Hermes cron is **SGT (UTC+8)**, not UTC — the expression is the SGT wall-clock time directly. Verify `next_run_at` after any change. The scripts handle all providers, so there are **three** jobs regardless of provider count.

```bash
HB=~/.hermes/hermes-agent/venv/bin/hermes   # `hermes` isn't on a non-login PATH
$HB cron create "30 23 * * *" --name "FoodPanda Daily Sync"               --script foodpanda_sync.py             --no-agent --deliver local
$HB cron create "5 9 * * 0"   --name "FoodPanda Weekly Report"            --script foodpanda_weekly_report.py    --no-agent --deliver local
$HB cron create "0 9 1-5 * *" --name "Food Expense Payment Reminder (monthly)" --script foodpanda_payment_reminder.py --no-agent --deliver local
```

These run `no_agent` (no LLM) and self-post to Discord, so `deliver` is `local`. The weekly job runs Sunday 09:05 and reports the previous complete Sun–Sat week (the window doesn't depend on the run day, so a manual re-run is safe). Change schedules with `hermes cron edit <id> --schedule …`, never by hand-editing `jobs.json`.

## Verify

```bash
V=~/.hermes/hermes-agent/venv/bin/python     # the interpreter cron uses
$V ~/.hermes/skills/productivity/foodpanda-expense-tracker/scripts/test_foodpanda.py          # offline suite
$V ~/.hermes/scripts/foodpanda_payment_reminder.py --dry-run --simulate-date 2026-10-01      # safe preview
# NOTE: not dry runs — migrate tabs, append to the sheet + post to Discord
$V ~/.hermes/scripts/foodpanda_sync.py
$V ~/.hermes/scripts/foodpanda_weekly_report.py
```
