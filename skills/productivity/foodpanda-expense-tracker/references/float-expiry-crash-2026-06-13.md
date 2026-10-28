# Float Expiry Crash — June 13, 2026 (Incident Record)

On 2026-06-13 the FoodPanda daily sync (job `b3b0042bd9cf`) failed silently — no Discord notification, no receipt data added. This file records the failure, diagnostic path, and fix for future reference.

## Failure output

From `~/.hermes/cron/output/b3b0042bd9cf/2026-06-13_23-31-04.md`:

```
Script exited with code 1
stderr:
Traceback (most recent call last):
  File "/home/amh/.hermes/scripts/foodpanda_sync.py", line 459, in <module>
    main()
  File "/home/amh/.hermes/scripts/foodpanda_sync.py", line 338, in main
    all_sheets = get_all_sheets()
  File "/home/amh/.hermes/scripts/foodpanda_sync.py", line 139, in get_all_sheets
    service = get_sheets_service()
  File "/home/amh/.hermes/scripts/foodpanda_sync.py", line 131, in get_sheets_service
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
  File ".../google/oauth2/credentials.py", line 476, in from_authorized_user_info
    expiry.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S"
    ^^^^^^^^^^^^^
AttributeError: 'float' object has no attribute 'rstrip'
stdout:
Found: 2026-06-13 | 明记煮炒 Meng Kee Zi Char (Pasir Ris Drive 6) | 15.79
```

Note: the script found a receipt but crashed *before* posting it to Discord or the sheet. No data was lost because state wasn't saved.

## Root cause

Google's `google-auth` library, when refreshing an expired access token, writes `expiry` back to `google_token.json` as a **Unix timestamp float** (e.g. `1781178751.1786096`) instead of an ISO 8601 string (e.g. `"2026-06-11T11:52:31Z"`). The `from_authorized_user_info()` method calls `.rstrip("Z")` on the expiry value — which crashes on a float.

The token had expired on June 11, 2026 (`2026-06-11T11:52:31Z`). Sometime between June 12's clean run and June 13's run, another Google API consumer (likely Gmail triage or a digest script) triggered a token refresh that wrote back a float expiry.

## Why it was silent (pre-v1.3.0)

The script had `discord_alert()` calls for *specific* failure modes (e.g., write verification failures), but the crash happened in `get_sheets_service()` — before any of those handlers ran. There was no top-level exception handler, so the script exited with code 1 and the cron scheduler just recorded "script failed" in the run log with no Discord notification.

## Diagnostic steps

1. Checked `cronjob list` → saw `last_status: "error"` for job `b3b0042bd9cf`
2. Read the run log at `~/.hermes/cron/output/b3b0042bd9cf/2026-06-13_23-31-04.md`
3. Observed `AttributeError: 'float' object has no attribute 'rstrip'` in the traceback
4. Checked `google_token.json` expiry field type:

   ```python
   import json
   tok = json.load(open('/home/amh/.hermes/google_token.json'))
   print(type(tok.get('expiry')), repr(tok.get('expiry')))
   # At time of incident: <class 'float'> 1781178751.1786096
   # After fix:           <class 'str'>  '2026-06-11T11:52:31Z'
   ```

5. Checked file modification time vs cron run time:

   ```bash
   stat /home/amh/.hermes/scripts/foodpanda_sync.py | grep Modify
   # Modify: 2026-06-14 08:23:21 — fix was added AFTER the 23:31 run
   ```

6. Verified no other recent runs had the error → only June 13 failed

## Fix applied

Two changes went into v1.3.0:

1. **`_fix_token_expiry()`** (already in the script from the June 14 backup fix session) — converts float `expiry` to ISO string before it reaches `from_authorized_user_info()`. Also attempts an in-place file write so other consumers benefit.

2. **Top-level crash handler** — wraps `main()` in try/except that posts the full traceback to Discord via `discord_alert()` before re-raising:

   ```python
   if __name__ == "__main__":
       try:
           main()
       except Exception:
           import traceback
           tb = traceback.format_exc()
           print(tb)
           discord_alert("❌ FoodPanda Sync Failed", f"```\n{tb}```")
           raise
   ```

## Recovery

After the fix, re-run the script to backfill missed receipts:

```bash
python ~/.hermes/scripts/foodpanda_sync.py
# Output: "Found: ... Added: ... Done. Last checked: ..."
```

State wasn't saved on the failed run (the `any_failure` guard prevents it), so the missed receipt is picked up by a fresh run.

## Prevention

- Any new no_agent script that self-posts to Discord MUST have a top-level try/except crash handler (the "noisy failure" pattern).
- Any new Google API consumer MUST call `_fix_token_expiry(token_data)` before `from_authorized_user_info()`.
- Refreshed tokens reset the 60-minute clock; the float-vs-string issue recurs each time a token refresh happens mid-lifecycle between two Google consumers.

> **Note (2026-07-18):** the crash-handler snippet above shows the v1.3.0 form (`discord_alert("❌ FoodPanda Sync Failed", …)`). Since v1.5.0 the handler posts via a single-shot `quick_alert()` with a **truncated** traceback (a retry-based alert could be SIGKILLed by the cron watchdog, and a long traceback exceeds Discord's 4096-char embed limit). The "always have a top-level crash handler" lesson stands; only its posting mechanism changed. See `references/tuneup-2026-07-18.md`.
