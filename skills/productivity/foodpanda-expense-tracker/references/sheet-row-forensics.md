# Missing / vanishing rows in the expense sheet

Use when the user reports an expense that is not in the month tab ("why is X not recorded?").
Work outside-in: prove what the sync did, then what the tab held over time, then restore.

## 1. List every receipt for the month and diff against the tab

```bash
GAPI='~/.hermes/hermes-agent/venv/bin/python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py'
$GAPI gmail search 'from:no-reply@foodpanda.sg after:2026/11/01 before:2026/11/30' --max 60
$GAPI gmail search 'from:no-reply@grab.com after:2026/11/01 before:2026/11/30' --max 60
```

Compare each message id against column D of the tab. Gaps split into three cases:

- **Receipt dated today** → not missing; the daily 23:30 SGT run has not fired yet. Safe to add by hand
  (dedup is by email id, so the run will skip it later).
- **No row in any revision and no `Added:` line in the cron log** → parser/skip problem, see `TROUBLESHOOTING.md`.
- **Row existed earlier but is gone now** → a manual tab rewrite dropped it (step 3).

## 2. Prove whether the sync added it

Per-run output: `~/.hermes/cron/output/b3b0042bd9cf/<YYYY-MM-DD>_<HH-MM-SS>.md`.
A successful insert logs `Added: <date> | <merchant> | S$<amount> (Provider)`; a re-issued receipt
logs two `Found:` lines and `Within-batch dedup: 2 → 1` — the amount to trust is the one on the
`Added:` line, not the first `Found:` line.

## 3. Reconstruct the tab from Drive revisions

Google Sheets revisions are listable and exportable — the whole workbook at that point in time.

```python
import sys, requests
sys.path.insert(0, "/home/amh/.hermes/skills/productivity/google-workspace/scripts")
import google_api as g
from google.auth.transport.requests import Request
import openpyxl

SHEET = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"
svc = g.build_service("drive", "v3")

# page through: results come oldest-first, capped per page
revs, tok = [], None
while True:
    kw = dict(fileId=SHEET, pageSize=1000,
              fields="nextPageToken,revisions(id,modifiedTime)")
    if tok: kw["pageToken"] = tok
    res = svc.revisions().list(**kw).execute()
    revs += res.get("revisions", [])
    tok = res.get("nextPageToken")
    if not tok: break

creds = g.get_credentials()
if not creds.valid: creds.refresh(Request())
hdrs = {"Authorization": f"Bearer {creds.token}"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

for r in revs:
    link = svc.revisions().get(fileId=SHEET, revisionId=r["id"], fields="exportLinks").execute()
    blob = requests.get(link["exportLinks"][XLSX], headers=hdrs, timeout=60).content
    open(f"/tmp/rev_{r['id']}.xlsx", "wb").write(blob)
    wb = openpyxl.load_workbook(f"/tmp/rev_{r['id']}.xlsx", data_only=True)
    print(r["id"], r["modifiedTime"], wb["Nov 2026"]["A1:F20"])
```

Facts that keep this honest:

- Every revision shows the **same** `lastModifyingUser` (the account owning the OAuth token), so
  provenance comes from **timing** — a revision timestamp just after a manual edit, plus the row
  set changing shape — never from the display name.
- The cron job names its revisions at the run time (23:30 SGT = 15:30 UTC); anything in between is a
  human/assistant edit.
- `openpyxl` is not always in the gateway venv:
  `~/.hermes/hermes-agent/venv/bin/python -m pip install openpyxl`.

## 4. Restore without breaking dedup

- Insert at the **date-sorted** position, not at the end — this tab is chronological.
- `insertDimension` needs the numeric `sheetId` (gid), not the tab title:
  `spreadsheets().get(fields="sheets.properties")` → matching `properties.sheetId`.
- Insert **below row 2** so the `G2 = =SUM(C2:C)` MTD formula is untouched, then
  `values().update(range="'Nov 2026'!A5:F6", valueInputOption="USER_ENTERED")`.
- Column D must be the **real Gmail message id**, never `manual` — a `manual` key makes a later sync
  run append the same order again.
- After writing, re-read the tab, sum column C in code, and confirm it equals `G2`.

## 5. The automatic guard (sync v1.9.0+)

Every sync run reconciles the tab, so a current- or previous-month loss usually self-heals within a
day — **re-read the sheet before doing a manual restore**.

- `tab_snapshot` in `~/.hermes/foodpanda_sync_state.json` records the Gmail message ids the current
  month's tab held (plus the previous month around a boundary). An id that disappeared is re-fetched
  by message id (no search), re-parsed, re-inserted in date order, and announced with
  `♻️ Food Receipts Restored`.
- Only real hex message ids are tracked — `manual` / `manual (wife account)` markers never are, so
  hand-entered rows are not "restored".
- The same id restored 3× and deleted again is left out (`reconcile_blocked_ids`) with a one-time
  alert, so a deliberate deletion doesn't loop.
- It never searches Gmail and never touches the watermark; a failure only means the next run retries.

A hand-added row is still safe if column D holds the **real** Gmail message id (the sync dedups on it);
a `manual` marker is not — a later run will treat the order as new.
Invariants for editing it (insert bottom-up, never above row 3, fake-Svc test support) → `CLAUDE.md`
step 12 + the reconcile gotcha.
