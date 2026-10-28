# Token Report — Double Fire & Multiple Message Analysis

## Summary of Findings (May 25, 2026)

This reference documents the root causes and correct architecture for duplicate/multi-message token report delivery, based on live debugging on the production system.

---

## Finding 1: Multiple Messages = Separate Scheduler Ticks

**What happened:** After the fix was applied (filelock + [SILENT]), Discord still showed multiple message batches at 08:16, 08:22, 08:26 — three separate batches within ~30 minutes.

**Root cause:** The scheduler's gateway thread fires `tick()` every 60 seconds via `_cron_tick()` in `gateway/run.py`. The `cronjob(action='run')` call triggers `trigger_job()` which sets `next_run_at = now`, making the job immediately due. On a loaded system, `advance_next_run()` may not save the new schedule before the next 60s tick fires, causing the job to run again.

**Each message batch is a LEGITIMATE separate scheduler tick**, not a bug in the script logic. The filelock prevents concurrent execution, and [SILENT] suppresses the per-tick output.

**Architecture conclusion:** Multiple message batches from a single `cronjob(action='run')` call are expected behavior. The script is correctly deduplicated — no further fix needed for the multi-tick scenario.

---

## Finding 2: os._exit(0) Releases the Filelock Immediately

**OS-level behavior:**
- `FileLock.acquire()` acquires an actual file lock (flock/F_SETLK) on the lock file descriptor
- `os._exit(0)` closes ALL file descriptors — releasing the OS lock immediately
- The lock file inode stays on disk (0 bytes) until the next acquire() creates a new file

**Current pattern is correct:**
```python
lock = FileLock(LOCK_FILE, timeout=0)
_acquired = lock.acquire(blocking=False)
if not _acquired:
    os._exit(0)
# ... do work ...
os._exit(0)
```

---

## Finding 3: filelock.FileLock — Non-Atomic Advisory Lock

`acquire(blocking=False)` returning `False` means "another instance running — exit silently". `Timeout` exception also means "already running — exit". Any other exception during acquisition also triggers exit. The lock file `/tmp/hermes_cache/token_report.lock` persists across invocations.

---

## Finding 4: [SILENT] Must Be First Line of Stdout

Scheduler captures ALL stdout from script execution. Any print before `[SILENT]` — even `print("", flush=True)` — causes the scheduler to deliver the full response.

Flow: script prints `[SILENT]` first → scheduler captures → `os._exit(0)` → `_process_job()` checks `if trimmed.startswith(SILENT_MARKER): should_deliver = False` → delivery skipped.

---

## Finding 5: strftime 'unixepoch' Modifier Required

`started_at` is a Unix epoch FLOAT (e.g., `1777593600.0`). Without `'unixepoch'`, SQLite interprets the float as a Julian day number → NULL for all rows → single-date grouping.

**Correct:** `strftime('%Y-%m-%d', started_at, 'unixepoch')`

**Double-percent bug:** Inside `"""`, `%%` is a literal percent — not an escape. So `strftime('%%Y-%%m-%%d', ...)` sends literal `%%Y` to SQLite instead of format code `%Y`. Use single `%` inside triple-quoted Python strings.

---

## Finding 6: fmt() Auto-Scaling

```python
def fmt(v, suffix='M'):
    if suffix == 'M':
        return f"{v/1e6:.1f}M" if v >= 1_000_000 else f"{v/1e3:.1f}K"
    return f"{v/1e3:.1f}K"
```

`fmt(15350, 'M')` → `15.4K` not `0.0M`.

---

## Scripts

- `/home/amh/.hermes/scripts/token_report_no_agent.py` — production script; `sys.path.insert(0, "/usr/lib/python3/dist-packages")` at lines 12-14 for PIL; bot token via `dotenv.values().BOT_TOKEN` from `~/.hermes/hermes-agent/.env`; lock `/tmp/hermes_cache/token_report.lock`; always `os._exit(0)`
---

> **Correction (2026-07-12 tune-up):** the line above states `fmt(15350, 'M') → 15.4K`; the shipped code actually produces **`15.3K`** (`15350/1e3 = 15.35`, and `f"{15.35:.1f}"` rounds to `15.3`). Left as a dated record — the `fmt` behavior itself is unchanged; only this example's stated output was wrong. SKILL.md/CLAUDE.md corrected to `15.3K` in the same tune-up (see `2026-07-delivery-hardening.md`).
