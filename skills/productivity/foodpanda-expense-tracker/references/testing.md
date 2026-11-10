# Offline test suite (`scripts/test_foodpanda.py`)

No network, no live sheet/Discord: `HERMES_HOME` points at a temp dir, `run_gapi`, the Sheets
service and `urlopen` are stubbed. One check per fixed bug. Run it after **every** edit, against the
skill copy and the deployed copy:

```bash
python ~/.hermes/skills/productivity/foodpanda-expense-tracker/scripts/test_foodpanda.py
FP_SCRIPTS_DIR=~/.hermes/scripts python ~/.hermes/skills/productivity/foodpanda-expense-tracker/scripts/test_foodpanda.py
```

## Fake-Google rules

`run_sync()` builds a fake `Svc` that only understands the Sheets methods the sync calls at the
time it was written. Whenever the sync starts using a new one, extend the fake in the **same edit**:

- `get()` must return `sheets[].properties` including `sheetId` (a new `insertDimension` needs it),
  and its `sheetId → tab` mapping must match the `SHEET` dict order it enumerates.
- `batchUpdate()` must actually apply the request to the fake sheet — e.g. splice rows for
  `insertDimension` — or the suite validates the old write shape and the live run is the first to
  exercise the new path. A restore that fails this way degrades into a "restore failed" alert, not
  a test failure.
- Reset module-level caches (`sync._sheet_gid_cache = {}`) at the top of `run_sync`. A gid cached
  against one test's fake tab list leaks into the next and resolves `sheetId` to the wrong tab.

## What the checks pin down

- Parser: FoodPanda/Grab totals, ride receipts → `nonfood`, non-SGD → `foreign`, impossible dates and
  merchant-capture overruns → `fail`.
- Sync flow: append + summary, the migration batchGet count, ride-only runs, unreadable dates,
  empty/legacy tabs, deadline handling, fetch-failure exit codes.
- Reconcile: a wiped row is restored and **inserted in date order**, a surviving row is never
  duplicated, `manual` markers are never tracked as message ids, an intact tab is a no-op, and the
  3-strike guard both blocks a repeat deletion and says so once.
- Weekly: Sun–Sat window maths, boundary weeks, MTD through Saturday, read-failure exit code.
- Reminder: window/state idempotency, the sync-caveat, timeout and 502 handling.
