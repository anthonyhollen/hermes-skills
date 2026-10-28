# Orphan State Recovery

> ## ⚠️ DEPRECATED (2026-06-03) — kept for historical context only
>
> Orphans can no longer form. The digest redesign replaced the `state_ids − last_reported_ids`
> selection (which silently dropped category-less entries and left them marked-reported) with a
> `triaged_at` **watermark**: an entry with no category carries an EPOCH stamp and is simply never
> selected for a digest — it cannot render blank and cannot "use up" a report slot. The triage merge
> branch that created `{id, category:""}` orphans was also removed. `scripts/gmail_state_heal_once.py`
> is likewise deprecated. **Do not run the recovery below against current state** — there are no
> orphans to recover, and it manipulates `last_reported_ids`, which no longer drives selection.
> If a future digest ever shows blank rows again, the bug is elsewhere (e.g. triage writing empty
> from/subject at triage time) — investigate that, don't resurrect this tool.

---

## What is an "orphan"?

A `processed_ids` entry in `~/.hermes/scripts/gmail_triage_state.json` is an **orphan** when
`category` is empty AND `from`/`subject` are empty. The report script
(`gmail_report.py:194`) does `if not cat: continue` — silent drop — and the ID is already
in `gmail_triage_report_log.json:last_reported_ids`, so the email never reappears in any
future digest. This is **silent data loss** from ham's perspective.

### How orphans get created (2026-06-02 incident)

The 335 orphans in state today (95% of the file) come from an older code path
(pre-2026-05-31 dict format) where `gapi gmail search` returned messages with empty
headers (see `gws` binary warning in `google_api.py:219-222` — under certain conditions
the gws binary strips `payload.headers` when `metadataHeaders` is set). Triage then
took the `else` branch at `gmail_triage.py:873` (line numbers as of 2026-06-02):

```python
elif pid not in existing_entries:
    # Legacy entry or already-triaged with no category recorded
    existing_entries[pid] = {"id": pid, "category": ""}
```

That wrote `{id, category: ""}` with no from/subject, and the report was happy to call
it "reported" and never look at it again.

## Detecting orphans

```bash
python3 -c "
import json
state = json.load(open('$HOME/.hermes/scripts/gmail_triage_state.json'))
entries = state['processed_ids']
orphans = [e['id'] for e in entries if isinstance(e, dict) and not e.get('category') and not e.get('from')]
print(f'Orphans: {len(orphans)}')
print('First 5:', orphans[:5])
"
```

## Recovery: option A (back-fill from Gmail, recommended)

This is a one-shot. It:
1. Identifies all orphan IDs.
2. Fetches each from Gmail via the existing `google_api.py` CLI to recover from/subject.
3. Marks them as **unreported** in `gmail_triage_report_log.json` so the next digest
   picks them up.
4. Does NOT touch the well-formed entries (preserves all of ham's history).

**Safety:**
- Always dry-run first.
- The recovery mutates two files (`gmail_triage_state.json` and
  `gmail_triage_report_log.json`). Back them up with the Hermes backup cron
  (`999de4b21eec`, Sundays 3am) or copy them manually before running.
- The next digest will include the recovered entries. That's the point — ham sees them
  for the first time since they were filed.

### Dry-run

```bash
python3 -c "
import json
from pathlib import Path
import subprocess, sys

state = json.load(open('/home/amh/.hermes/scripts/gmail_triage_state.json'))
orphans = [e['id'] for e in state['processed_ids']
           if isinstance(e, dict) and not e.get('category') and not e.get('from')]
print(f'Found {len(orphans)} orphan IDs')
print('Sample (first 5):', orphans[:5])
print()
print('To back-fill via Gmail API, run the recovery script.')
"
```

### Recovery script (save as `~/orphan_recover.py` and run)

```python
#!/usr/bin/env python3
"""
One-shot orphan recovery. Fetches metadata from Gmail for each orphan ID,
fills in from/subject, and marks the IDs as unreported so the next digest
includes them.

Run as: python3 ~/orphan_recover.py [--apply]
Without --apply, prints the plan and exits.
"""
import json, sys, subprocess
from pathlib import Path

STATE = Path('/home/amh/.hermes/scripts/gmail_triage_state.json')
RLOG  = Path('/home/amh/.hermes/scripts/gmail_triage_report_log.json')
GAPI  = '/home/amh/.hermes/skills/productivity/google-workspace/scripts/google_api.py'

state = json.loads(STATE.read_text())
rlog  = json.loads(RLOG.read_text())

orphans = [e for e in state['processed_ids']
           if isinstance(e, dict) and not e.get('category') and not e.get('from')]
print(f'Found {len(orphans)} orphan entries to recover.')

if '--apply' not in sys.argv:
    print('DRY RUN — pass --apply to mutate state files.')
    print('Sample orphans:')
    for e in orphans[:5]:
        print(f'  {e[\"id\"]}')
    sys.exit(0)

# Back up first
import shutil
shutil.copy(STATE, STATE.with_suffix('.json.bak'))
shutil.copy(RLOG,  RLOG.with_suffix('.json.bak'))

# Fetch metadata from Gmail for each orphan
recovered = 0
failed = []
for e in orphans:
    msg_id = e['id']
    try:
        out = subprocess.run(
            ['python3', GAPI, 'gmail', 'get', msg_id],
            capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0 and out.stdout.strip():
            msg = json.loads(out.stdout)
            e['from']    = msg.get('from', '')
            e['subject'] = msg.get('subject', '')
            if e['from']:
                recovered += 1
            else:
                failed.append((msg_id, 'empty from'))
        else:
            failed.append((msg_id, f'gmail get rc={out.returncode}'))
    except Exception as exc:
        failed.append((msg_id, str(exc)))

# Re-mark these IDs as unreported so the next digest includes them
reported_set = set(rlog.get('last_reported_ids', []))
for e in orphans:
    reported_set.discard(e['id'])
rlog['last_reported_ids'] = [i for i in rlog.get('last_reported_ids', []) if i in reported_set]

# Save
STATE.write_text(json.dumps(state, indent=2, default=str))
RLOG.write_text(json.dumps(rlog, indent=2, default=str))

print(f'Recovered {recovered} / {len(orphans)} orphans from Gmail.')
print(f'Failed: {len(failed)}')
if failed:
    for msg_id, reason in failed[:10]:
        print(f'  {msg_id}: {reason}')
print()
print(f'State + report log updated. Next digest will include the recovered entries.')
print(f'Backups: {STATE.with_suffix(\".json.bak\")}, {RLOG.with_suffix(\".json.bak\")}')
```

### Running

```bash
# 1. Dry-run (no changes):
python3 ~/orphan_recover.py

# 2. Apply (mutates state + report log):
python3 ~/orphan_recover.py --apply

# 3. Wait for the next 7:10 AM / 1:10 PM / 7:10 PM digest cron and check Discord.
#    The recovered entries appear in the Auto-filed section with real senders + subjects.
```

## Recovery: option B (defensive patch, future-only)

Patch `gmail_report.py` so that when it hits `if not cat: continue`, it falls back to
fetching metadata via the Gmail API for that one ID. Pros: self-healing for any future
orphan. Cons: re-introduces API calls (defeats the May 31 zero-API fast path), only
fixes the *appearance* not the data loss (the email is still considered "already
reported" and won't re-render at the moment of the actual triage).

Not recommended unless orphans keep appearing, in which case the real fix is to find
the upstream cause (probably another `gws` binary header-stripping regression).

## Prevention

**Defensive rule:** if `gmail_report.py` ever silently drops an entry via `continue`,
log a stderr warning with the ID. That way orphans are detectable on the NEXT run,
not after ham notices blank lines weeks later.

```python
# In gmail_report.py, replace:
if not cat:
    continue
# with:
if not cat:
    print(f"  [warn] Orphan entry [{msg_id}] — empty category, skipping.", file=sys.stderr)
    continue
```

## Related bugs

- **2026-06-01 1:10 PM** — same symptom (blank `` `: `` lines) but different cause
  (category set, from/subject empty). That bug is fixed; today's is the same symptom
  with category also empty.
- **2026-05-31 7:12 PM timeout** — original "store categories in state" fix that
  removed the API-call fast path. The dict-format state file is what made orphans
  possible (legacy bare-string state would have triggered the API fallback instead).
