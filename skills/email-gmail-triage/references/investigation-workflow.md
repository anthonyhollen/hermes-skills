# Investigating Gmail Triage Runs

## Cron Output Files

All cron job output is logged to:
```
~/.hermes/cron/output/{job_id}/{YYYY-MM-DD_HH-MM-SS}.md
```

### Gmail Triage Job IDs
| Job | Job ID | Mode |
|-----|--------|------|
| Gmail Hourly Triage | `16d17820b1b1` | script (`no_agent`) |
| Gmail Morning Digest | `ada7196cfb74` | script (`no_agent`) |
| Gmail Afternoon Digest | `20c1d2012109` | script (`no_agent`) |
| Gmail Evening Digest | `d1708ee1990b` | script (`no_agent`) |

> All four jobs run `gmail_triage.py` or `gmail_report.py` as `no_agent` scripts (since 2026-05-31). Previous agent-mode runs loaded the full skill content into output — no longer the case.

### Finding Recent Runs
```bash
# List most recent output files for hourly triage
ls -t ~/.hermes/cron/output/16d17820b1b1/ | head -10
```

### Output Format (all jobs now `no_agent`)

All four jobs produce short script logs (~15-20 lines):

**Hourly triage** (`gmail_triage.py`):
```
[2026-05-31 11:00:34 SGT] Gmail triage starting...
  Ensuring Hermes labels...
  Label cache: {...}
  Correspondents: 16 addrs / 17 threads | known humans: 3
  Query: in:inbox newer_than:1d
  Found 2 inbox email(s)
  Processing: [19e7bf963093a612]  —
  Done: 0 Action, 0 ReadLater, 0 VIP, 1 Auto-filed, 0 Unsure
[2026-05-31 11:00:42 SGT] Triage complete (8s).
```
Since 2026-09-28 the "complete" line carries the real end time + duration (it used to repeat the start
time), the count says "inbox email(s)" (the query includes read mail), and these lines may appear:
`[skip] Already triaged` (Hermes label found by ID), `[defer]` (body fetch failed / budget reached),
`[recover]` (ambiguous modify resolved), `[unsure] … will re-ask`, `[reminder] …` (always logged at ≥21:00
until done). Warnings go to stderr — the scheduler keeps stderr only for failed runs.

**Digest reports** (`gmail_report.py`):
```
[2026-05-31 13:10:44 SGT] Generating 1:10 PM SGT report...
  Report preview:
## 📬 Gmail Triage Digest — 1:10 PM SGT
  ...
  Total triaged: 2 emails
  [discord] Report sent successfully (msg_id=...).
[2026-05-31 13:10:44 SGT] Report complete.
```

**Failed runs** show `**Status:** script failed` and the error (e.g. timeout):
```
# Cron Job: Gmail Evening Digest
**Job ID:** d1708ee1990b
**Run Time:** 2026-05-31 19:12:55
**Mode:** no_agent (script)
**Status:** script failed

Script timed out after 120s: /home/amh/.hermes/scripts/gmail_report.py
```
(Historical example — the global watchdog on this Pi is now `cron.script_timeout_seconds: 600`.) A digest
whose Discord send fails now exits 1 on purpose: the output shows `DELIVERY FAILED — full digest follows`
and `failure_deliver` posts a notice.

## Looking Up Email Details

Once you have a message/thread ID from the output (e.g. `19e7bf963093a612`):

```bash
python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail get {message_id}
```

Returns JSON with: `id`, `threadId`, `from`, `to`, `subject`, `date`, `labels`, `body`.

## Checking if an Email Was Processed

```bash
# Check if a message ID is in the processed list (new format)
python3 -c "
import json
state = json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json'))
ids = state['processed_ids']
if ids and isinstance(ids[0], dict):
    cats = {e['id']: e.get('category','') for e in ids}
    print('19e7bf963093a612' in cats, cats.get('19e7bf963093a612', ''))
else:
    print('19e7bf963093a612' in ids)
"
```

## Checking State Format

```bash
# Check if state is new format ({id, category} dicts) or legacy (bare strings)
python3 -c "
import json
ids = json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json'))['processed_ids']
fmt = 'new ({id, category})' if ids and isinstance(ids[0], dict) else 'legacy (bare strings)'
print(f'Format: {fmt}, count: {len(ids)}')
"
```

## Detecting Orphan State Entries (data loss) — ⚠️ DEPRECATED (2026-06-03)

> Orphans are no longer possible under the `triaged_at` watermark design (a category-less entry
> carries an EPOCH stamp and is simply never selected for a digest). The detector below is kept for
> historical investigation of *old* state backups only. To sanity-check current state, confirm every
> entry has a `triaged_at` instead:
>
> ```bash
> python3 -c "
> import json
> e = json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json'))['processed_ids']
> print('entries:', len(e), '| all stamped:', all('triaged_at' in x for x in e if isinstance(x, dict)))
> "
> ```

<details><summary>Historical orphan detector (pre-watermark)</summary>

Entries with `category=""` AND empty `from`/`subject` were **orphans** — the old report silently skipped them (`if not cat: continue`) while they stayed in `last_reported_ids`, so they never reappeared. Symptom: blank `` `: `` lines in a digest body.

```bash
python3 -c "
import json
state = json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json'))
entries = state['processed_ids']
orphans = sum(1 for e in entries if isinstance(e, dict) and not e.get('category') and not e.get('from'))
populated = sum(1 for e in entries if isinstance(e, dict) and e.get('category') and e.get('from'))
print(f'Orphans (cat+meta empty): {orphans}')
print(f'Fully populated: {populated}')
print(f'Total: {len(entries)}')
"
```

</details>

## Full Investigation Example

**User asks**: "What email was auto-filed at 11am?"

1. Find the output file: `ls ~/.hermes/cron/output/16d17820b1b1/2026-05-31_11-*`
2. Read it: shows `Processing: [19e7bf963093a612]` + `1 Auto-filed`
3. Look up the email: `google_api.py gmail get 19e7bf963093a612`
4. Result: "Domino's Pizza" promotional email → correctly auto-filed

## Checking for Failures in a Time Window

1. `cronjob(action='list')` → get all jobs with `last_status` and `last_run_at`
2. Filter by `last_run_at` within the time window
3. Check `last_status` — anything not `"ok"` is a failure
4. For details on failed runs, check the output file for error messages

## Diagnosing Script Timeouts

> **Note (2026-06-03):** the watermark redesign removed the report's Gmail-API "legacy fallback" and
> the empty-`last_reported_ids` re-fetch path — the two things that used to cause digest timeouts.
> The report now selects purely by `triaged_at > last_report_ts` with no per-email API calls, so a
> digest timeout is no longer expected. If one happens, suspect Discord/network or a genuinely huge
> render (Discord 2000-unit path), not state format.

When a digest job shows `**Status:** script failed` with "Script timed out after 120s":

1. **Check the watermark + state:** `python3 -c "import json; s=json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json')); r=json.load(open('$HOME/.hermes/scripts/gmail_triage_report_log.json')); print('entries:',len(s['processed_ids']),'| watermark:',r.get('last_report_ts'))"`
2. **Confirm entries are stamped** (a bare-string/legacy state yields no selection until triage normalizes it — not a timeout, just an empty digest): `all('triaged_at' in e for e in s['processed_ids'])`.
3. **If the render is huge** → the report auto-truncates to a (capped) summary at Discord's 2000-UTF-16-unit bot limit (it checked 4000 until 2026-09-28, so a 2001–4000 digest was rejected). Check output for `HTTP 400: BASE_TYPE_MAX_LENGTH`. The summary shows top 5 Action, top 3 ReadLater, top 5 Auto-filed senders.
4. **Otherwise** → check Discord API reachability / network. The heal path only fires for the rare entry missing from/subject and is bounded by that count.

## Performance Baseline (v3 — triaged_at watermark)

| Scenario | Expected Time | Notes |
|----------|--------------|-------|
| 0 new emails (watermark catches up) | <0.5s | No API calls |
| any batch, all entries fully populated | <1s | Pure in-memory selection from state |
| N selected entries missing from/subject | +~0.27s per such entry | Read-only heal fetch (should be ~0) |

If times are significantly slower than these baselines, check for:
- An unexpected number of entries hitting the read-only heal (triage should persist from/subject —
  a `[heal]` warning in the output names them; the real fix is upstream in triage)
- Discord API rate limiting or network issues
