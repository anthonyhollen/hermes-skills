# Timeout Crash — July 17, 2026 (Incident Record)

> **Correction (2026-09-28):** the no_agent script watchdog on this host is the global `cron.script_timeout_seconds` = **600 s** (config.yaml), not ~120 s. The "~120 s" budget math below was sized for a limit that didn't apply; v1.7.0 uses `RUN_DEADLINE_SECS = 300` — see `tuneup-2026-09-28.md`.


On 2026-07-17 the FoodPanda daily sync (job `b3b0042bd9cf`) failed with a transient network timeout. The script found a receipt from Gmail but crashed before writing it to the sheet. This file records the failure, root causes, diagnostic path, and fix.

## Failure output

From `~/.hermes/cron/output/b3b0042bd9cf/2026-07-17_23-32-30.md`:

```
Script exited with code 1
stderr:
...
  File "foodpanda_sync.py", line 411, in main
    all_sheets = get_all_sheets()
  File "foodpanda_sync.py", line 157, in get_all_sheets
    ).execute()
      ^^^^^^^^^
...
TimeoutError: The read operation timed out
stdout:
Found: 2026-07-17 | Giant (Tampines North) | 103.91 | order: moi1-2629-va79
```

The script found the receipt but crashed at `get_all_sheets()` — a Google Sheets API call that timed out.

## Root causes

Two compounding bugs:

1. **`get_all_sheets()` called `.execute()` directly without `retry()`** — unlike `update_sheet_row()`, `ensure_order_id_column()`, and `create_month_sheet()` which all wrap their direct Python-client calls in `retry()`, `get_all_sheets()` was the only direct API call that ran naked. A transient network timeout took the whole script down.

2. **The `retry()` pattern check was case-sensitive** — the error string `"The read operation timed out"` doesn't contain `"TIMEOUT"` (uppercase). The `retry()` function matched against `"429"`, `"503"`, `"rateLimit"`, `"TIMEOUT"`, `"Transient"` — none of which appear in a `TimeoutError` message. So instead of retrying, the function re-raised the exception on the first attempt.

3. **No timeout on the google client HTTP connection** — the default httplib2 timeout is effectively infinite. When the TCP handshake or SSL handshake hangs, the socket blocks until the OS-level TCP timeout (often 120+ seconds). The cron scheduler may have its own shorter timeout, or the gateway interrupted the script.

## Why state wasn't lost

The crash happened *after* the Gmail fetch but *before* the sheet write. Because the script only saves state (`last_checked_email_ts`) when `any_failure` is `False`, the watermark was not advanced. On the next run the same email was refetched, successfully parsed, and written.

## Fix applied

Two changes in all four copies (cron + canonical):

1. **`get_all_sheets()` wrapped with `retry()`**:
   ```python
   result = retry(lambda: service.spreadsheets().get(
       spreadsheetId=SHEET_ID,
       fields="sheets.properties.title"
   ).execute())
   ```

2. **Retry patterns made case-insensitive + `"timed out"` added**:
   ```python
   err_str = str(e).lower()
   if any(k in err_str for k in ["429", "503", "ratelimit", "timeout", "transient", "timed out"]):
   ```

3. **Same pattern fix applied to `foodpanda_weekly_report.py`** — it had the same case-sensitive `retry()` (though it only uses `run_gapi()` for sheets, so it wasn't affected here).

## Recovery

After the fix, re-run to backfill:
```bash
python ~/.hermes/scripts/foodpanda_sync.py
```
The script re-fetched the email and successfully wrote the row.

## Prevention

- Any new direct Sheets API `.execute()` call must be wrapped in `retry(lambda: ...)`.
- When adding error patterns to `retry()`, use `.lower()` on the error string and keep patterns lowercase.
- If adding a new `no_agent` cron script, the top-level try/except crash handler (from the float-expiry incident) ensures noisy failures even when the error occurs mid-function.

## Correction (2026-07-18 tune-up) — this fix was CORRECT but INCOMPLETE

A same-day tune-up found this fix only bounded the *one* call that crashed. It left `run_gapi`'s `subprocess.run` and all other direct `.execute()` calls **unbounded**, so a hang elsewhere still ran to the ~120s cron SIGKILL (uncatchable → no alert); and `retry()` around an unbounded per-attempt call can itself *exceed* the watchdog. The completed fix (v1.5.0): `socket.setdefaulttimeout(20)` + a `subprocess.run(timeout=…)` on `run_gapi` bound every call, a run-global `_run_deadline` gates `retry()` pre-start/pre-sleep, and the crash alert became a single-shot `quick_alert` (a retry-storm alert could be SIGKILLed too). See `references/tuneup-2026-07-18.md`. **Lesson: when fixing a timeout/hang, bound EVERY sibling blocking call, not just the one that crashed — and keep the retry envelope + crash alert under the watchdog.**
