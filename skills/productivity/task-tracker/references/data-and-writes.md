# Data & Writes

**All writes go through `scripts/task-tracker.py` subcommands** (`done` / `set` / `add` / `move` / `remove` / `rename` / `undo` — see SKILL.md). This file documents what they do, for debugging and for the rare by-hand recovery when the script itself can't run. Do not hand-edit the JSON otherwise.

## Files (all in `~/.hermes/data/`; `TASK_TRACKER_DATA_DIR` overrides the dir, e.g. for tests)
| File | What |
|------|------|
| `task-tracker.json` | The data. Written as `json.dumps(indent=2, ensure_ascii=False)` + newline. |
| `task-tracker backups/` | `task-tracker.YYYY-MM-DD_HHMMSS[_N].json` = exact pre-change bytes of a write (newest 10 kept); `…_undo.json` = the state an undo replaced (newest 10 kept, separately). `_archive/` holds retired stray files (ignored by the script). |
| `task-tracker.undo.json` | Undo stack `[{backup, pre, post, cmd, at}]` (sha256 of the file before/after each scripted write) + `last` = outcome of the most recent write command (`wrote: true/false`). |
| `task-tracker.last-shown.json` | Per thread: `{"discord:<id>": {shown_at, rows: {"<row#>": {id, name}}}}` — the table last delivered there. Only sections that actually arrived are recorded. |
| `task-tracker.lock` | `flock` lock serializing writes. |

**Schema:** top-level `tasks` (array) and `categories` (map).
- Each task: `id`, `name`, `category`, `last_done` (zero-padded ISO `YYYY-MM-DD` or `null`), `added` (ISO).
- Each category: `label`, `interval_days` (weekly 7, fortnightly 14, monthly 30, quarterly 90). A missing/invalid interval falls back to those standard values (30 for an unknown category) with a stderr warning; section titles show the interval actually used.
- **Task id** = slug of the name: lowercase, `&` → `and`, apostrophes dropped, other non-alphanumeric runs → `-` (`"Lock'n'lock water jug wash"` → `locknlock-water-jug-wash`, `"Toilet bowl & sink wash"` → `toilet-bowl-and-sink-wash`). Rename regenerates it.

## What every write does (`atomic_write`, all under the lock)
1. Read the current bytes; apply the change in memory.
2. If nothing changed → `(no change — nothing written)`: no backup, no undo entry.
3. **Validate** the tasks this write created/changed (all five fields, ISO dates, unique ids; untouched legacy rows aren't re-judged). Failure → `ERROR: … (nothing written)`, exit 1. `--dry-run` stops here.
4. **Backup first** (exact pre-change bytes, fsynced; `_N` suffix orders writes within one second — compare N numerically, `_10` is after `_9`), then write `task-tracker.json.tmp<pid>`, fsync, `os.replace`. If that fails, the backup is removed again.
5. Push `{backup, pre, post}` onto the undo stack and save it, then prune backups by **filename** timestamp (not mtime). Stack entries whose backup was pruned are dropped, so undo depth = 10.

`done` / `move` exit codes: 0 all written · 3 partial (the ✓ / `↔` items written; `SKIPPED:` lines list the rest) · 1 nothing written.

## Undo
`undo` pops the top stack entry and restores that entry's backup **byte-for-byte**; repeating it steps one scripted write further back. It refuses:
- when `last.wrote` is false — the last command was a no-op/failed, so "undo that" would revert an older write (`--previous` overrides);
- when the file's sha ≠ the entry's `post` — it was changed outside the script since (`--force` overrides; review with `undo --dry-run --force` first)
- when the backup's sha ≠ the entry's `pre` — the backup was modified after it was taken (`--force` overrides);
- when the entry's backup file is gone — the stack is then cleared (older entries can't be restored without also reverting the lost write); fix tasks with `set`;
- when the stack is empty (all scripted writes undone, or only backups from before the write subcommands existed) — always, even with `--force`: restoring "the newest backup" blindly could revert or re-apply the wrong writes. Use `set`, or the by-hand recovery below.

If the file already equals the entry's `pre` state (a crash after the restore), undo just consumes the entry. Undo keeps the backup it restored and saves the replaced state as an `_undo` backup, so nothing is lost by undoing. There is no redo — re-run the undone command.

Undo reverts a whole write. To fix one task inside a multi-task write, use `set <id> <old value>`.

## By-hand recovery (script can't run)
Copy the right backup over the live file via a temp file + `mv` (never edit in place). Pick by the timestamp in the **filename** (`ls -1 "…/task-tracker backups" | sort`), not `ls -t` — copies can carry old mtimes. Check the backup's contents first, and compare it with the live file for tasks legitimately updated since (re-apply those afterwards). Afterwards the undo stack no longer matches the file, so the script's `undo` refuses ("changed outside the script") for the writes before it — by design.

## Date / clock notes
- The script accepts `today`, `yesterday`, or zero-padded `YYYY-MM-DD`; it rejects non-padded (`2026-7-6`) or impossible dates, and dates more than 1 day in the future. The agent converts "May 15" → current year.
- "today" = the Pi's `date.today()` (Asia/Singapore, NTP-synced, RTC present). The report clamps days-ago/overdue to `max(0, …)`.
