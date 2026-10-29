---
name: task-tracker
description: "Use when user asks to show, update, add, remove, move, or rename tracked recurring tasks by frequency."
version: 1.9.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [productivity, tasks, tracking, habits]
    related_skills: [discord-table-format]
---

# Task Tracker

Tracks when the user last did recurring chores, grouped by frequency (weekly / fortnightly / monthly / quarterly), and reports overdue status. Data: `~/.hermes/data/task-tracker.json`.

**One script does everything — show AND every write.** Never read, `patch`, `write_file`, or hand-code (heredoc / `execute_code`) changes to `task-tracker.json`; the write subcommands lock, back up, validate, replace atomically, and record undo history in one call.
```bash
TT=~/.hermes/skills/productivity/task-tracker/scripts/task-tracker.py
```
`<thread>` below = `discord:<id of THIS conversation's thread/channel>` — never the "Triggering message id" (a message id 404s unless it is the thread's starter message). Pass the same `--target` to show and to every row-number command.

## When to Use
- `"show tasks"` / `"task tracker"` / `"show latest"` → **Show**
- `"done 2,5"` / `"done today: car wash"` / `"done all weekly"` → **Mark done**
- `"add task: <name> (<frequency>)"` → **Add** · `"remove task: <name>"` → **Remove** · `"rename <old> to <new>"` → **Rename**
- `"make 14,16 quarterly"` / `"move 21 to fortnightly"` → **Move**
- `"undo it"` / `"that was wrong, I meant …"` → **Undo / Correct**

Not for: appointments, calendar events, or one-off reminders.

## Show — run the script, do not improvise
```bash
python3 $TT --target <thread>
```
- The script renders image tables and posts them itself (Discord REST multipart) as **one post**: the title, plus one card per category with the category header as the card title and its table inside (since 1.9.0). A `WARNING: single post rejected` means it fell back to one message per category; the tracker still arrived. A `timed out and may or may not have arrived` error means: check the thread before re-running, so nothing is duplicated. Do **NOT** build a Markdown table or use `send_message` / `MEDIA:`. Success prints `Delivered N sections…` to stderr; on exit ≠ 0 or any `ERROR:`/`WARNING:`, tell the user — don't fall back to a hand-made table.
- Delivering records the row numbers the user now sees in that thread; `done N --target <thread>` resolves against exactly that table.

## Mark done
```bash
python3 $TT done 2,5,"car wash","all weekly" --target <thread> [--date yesterday|YYYY-MM-DD]
```
- Items are comma-separated: row numbers, task names (case-insensitive; a unique run of whole words like `sayang bottle` works), ids, or `all <category>`. Turn "A & B" / "A and B" into separate items (`"A","B"`) — unless the whole phrase is itself a task name (e.g. "Toilet bowl & sink wash"). Bare `all` needs `--yes` after the user confirms.
- Default date is today; use `yesterday` or zero-padded `YYYY-MM-DD` ("May 15" → current year). The script never moves a date backwards (use `set` for that).
- **Never map numbers yourself** (no `--json`, no reading the JSON) — the script uses the table shown in `<thread>`. If it says no table was shown there yet, run Show first. If the user is replying to an *older* tracker message than the latest one in the thread, use task names instead of numbers.
- Output (stdout): `✓ Name (id): old → new (next due …)` per task, `=` already done that day, `SKIPPED: …` for items it couldn't use (ambiguous / no match / would move backwards). Exit 3 = the ✓ ones *were* written — ask about the skipped ones. Exit 1 = nothing written.
- Confirm from those lines, e.g. `✅ Marked 3 tasks done (Sep 24): Car wash, Mop house, Towel change`. Don't re-post the tracker unless asked.

## Correct / undo
- **"undo it"** → `python3 $TT undo` reverts the **most recent write** exactly; run again to step further back (up to 10). Its first line says `(undoing: <command> at <time>)` — check that's what the user meant (if that write wasn't made in this conversation, run `undo --dry-run` first and confirm). Report the `↺ / + / -` lines. There is no redo: to reverse an undo, re-run the command it names.
- Undo refuses, and you should tell the user rather than override, when: the last command changed nothing ("undo that" would hit an *older* write — use `--previous` only if the user confirms that older write), the file was edited outside the script (`--force` only after reviewing `undo --dry-run --force` with the user), its backup is gone (then fix tasks with `set`; never `--force`), or there is nothing left to undo (the script only undoes its own last 10 writes — use `set`; `--force` doesn't apply).
- **"22 was wrong, I meant 23"** → fix only that task (plain `undo` would revert every task in that write): `python3 $TT set <id> <old>` with the id and old value from the earlier `✓ Name (id): old → new` line (`never` if it was never done), then `python3 $TT done 23 --target <thread> --date <new>`, where `<new>` is the date that ✓ line set.
- `set <item> <YYYY-MM-DD|today|yesterday|never>` sets one task's last-done directly.

## Add / remove / rename / move
```bash
python3 $TT add "<name>" <weekly|fortnightly|monthly|quarterly> [--last-done today]
python3 $TT move <items> <category> --target <thread>   # change frequency in place (row numbers / names / ids)
python3 $TT remove "<exact name or id>" --yes      # ONLY after the user explicitly says yes
python3 $TT rename "<exact name or id>" "<new name>"
```
- Add: missing category → **ask**. The script rejects invalid categories, duplicates, and names that are numbers, start with "all <category>", or contain commas; sets `added` = today and last-done = never unless `--last-done` is given.
- Move: items are comma-separated row numbers (as last shown — pass `--target <thread>`), names, or ids; name / id / last_done / added are preserved — one atomic write, one undo entry. Output per task: `↔ Name (id): monthly → quarterly`; `= …` if already in that category; `SKIPPED: …` + exit 3 for unusable items (the `↔` ones were written).
- Remove: ask `Remove **<name>** from <category>? (yes/no)` first; run with `--yes` only on an explicit yes. Remove/rename need the exact name, id, or row number.
- Ambiguous name (the script lists candidates) → ask the user, don't guess.
- After an add/remove/rename/move, update `references/canonical-tasks.md` (the recovery list).

## Key Pitfalls
- **Never write the JSON yourself.** Every past write bug (in-place edits, a corrupted task name, fake backup timestamps, bad prunes, gateway-"Blocked" heredocs) came from hand-written writes.
- **Always pass `--target <thread>`** to show and to `done`/`set`/`remove`/`rename` when using row numbers — and it's the thread id, not a message id.
- **Ambiguity → ask.** Bulk `all` → confirm. Remove → confirm. Refused undo → explain, don't force.

## References
| File | Read when |
|------|-----------|
| `references/data-and-writes.md` | Schema, backup/undo mechanics, state files, or recovering by hand when the script can't run |
| `references/discord-delivery.md` | Delivering tables **by hand** (the script already does it) — display format + multipart pattern |
| `references/canonical-tasks.md` | `task-tracker.json` is lost/corrupt and must be rebuilt |
| `scripts/test_task_tracker.py` | After any script edit: `python3 scripts/test_task_tracker.py` (runs on a temp copy) |
| `CHANGELOG.md` | Change history |
