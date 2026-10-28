#!/usr/bin/env python3
"""
⚠️ DEPRECATED (2026-06-03) — DO NOT RUN against current state.

This tool repaired "orphan" state entries under the OLD digest design, where the
report selected emails via `state_ids − last_reported_ids` and silently dropped
category-less entries (leaving them marked-reported = data loss). That design is
gone. The digest now selects by a `triaged_at` watermark: a category-less entry
carries an EPOCH stamp and is simply never selected — orphans cannot form and
cannot render blank. This script also un-marks IDs from `last_reported_ids`,
which NO LONGER drives selection, so running it does nothing useful and only
muddies the report log. Kept for historical reference only. See SKILL.md
"Digest redesign" bug-table row and references/orphan-state-recovery.md.

────────────────────────────────────────────────────────────────────────────
One-shot Gmail state heal: back-fills category/from/subject for entries in
gmail_triage_state.json that are missing those fields, then un-marks the
previously-reported-but-blank IDs so the next digest re-emits them with
real data. See references/orphan-state-recovery.md for full context.

Usage:
    python3 gmail_state_heal_once.py --dry-run   # report only, no mutation
    python3 gmail_state_heal_once.py             # apply (mutates state + report log)

Run this once when:
- The 7:10 AM / 1:10 PM / 7:10 PM digest shows blank `` `: `` lines in any section
- `processed_ids` has many entries with `category=""` and empty `from`/`subject`
- The orphans-prevention stderr warnings have accumulated

Safety:
- Always run --dry-run first to see the plan
- Auto-backs up `gmail_triage_state.json` and `gmail_triage_report_log.json`
  to `.heal-bak` files before mutating
- Uses Gmail API (not gws binary CLI) with a service-rebuild retry to
  defeat the OAuth token-refresh race that caused the 2026-06-02 incident
- Idempotent — re-running is safe; healed entries are no-ops, and a
  partial run can be resumed (only unhealable entries get pruned)

What it does (in order):
  1. Load state + report log + auto-backup
  2. Find orphan entries (empty category + empty from/subject) in state
  3. For each orphan, fetch from Gmail via gmail_report.get_messages_metadata
     — which returns {id, labels, from, subject}. If from or subject is
     empty on the first pass, force-rebuild the singleton _service and
     retry (works around the OAuth token-refresh race that strips
     payload.headers on large batches).
  4. Categorize from labels (Hermes/* → category name)
  5. Prune entries that are unhealable (no Gmail metadata available)
  6. For the *morning's* 10 blank IDs (the trailing slice of state that
     rendered as blank in the 7:10 AM digest), un-mark them from
     last_reported_ids so the next digest re-emits them with real data
  7. Save state + report log
"""
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Ensure we can import gmail_report
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# Try common install locations
for candidate in [
    SCRIPT_DIR.parent.parent.parent / "scripts",  # ~/.hermes/scripts
    Path.home() / ".hermes" / "scripts",
]:
    if (candidate / "gmail_report.py").exists():
        sys.path.insert(0, str(candidate))
        break

import gmail_report as gr  # noqa: E402

SGT = timezone(timedelta(hours=8))
STATE_FILE = Path(gr.STATE_FILE if hasattr(gr, 'STATE_FILE')
                  else Path.home() / ".hermes" / "scripts" / "gmail_triage_state.json")
RLOG_FILE  = Path(gr.RLOG_FILE if hasattr(gr, 'RLOG_FILE')
                  else Path.home() / ".hermes" / "scripts" / "gmail_triage_report_log.json")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    """Atomic save with temp file + rename."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(path)


def fetch_enrichment(msg_ids):
    """For each msg_id, return dict {category, from, subject}.
    Bypasses orphan-detection in build_report: enriches from labels
    directly. With service-rebuild retry to handle OAuth token-refresh
    races on large batches (2026-06-02 regression)."""
    if not msg_ids:
        return {}
    try:
        messages = gr.get_messages_metadata(msg_ids)
    except Exception as e:
        print(f"  [heal] Metadata fetch failed: {e}", file=sys.stderr)
        return {}

    id_to_name = gr.get_labels_map()
    result = {}
    for m in messages:
        labels = [id_to_name.get(lid, lid) for lid in m.get("labels", [])]
        hermes_labels = [n for n in labels if n.startswith("Hermes/")]
        cat = hermes_labels[0].replace("Hermes/", "") if hermes_labels else ""
        result[m["id"]] = {
            "category": cat,
            "from": m.get("from", ""),
            "subject": m.get("subject", ""),
        }
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="One-shot heal of empty entries in gmail_triage_state.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report plan only; do not mutate state files")
    args = parser.parse_args()

    now_str = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
    print(f"[{now_str}] gmail_state_heal_once starting "
          f"({'DRY RUN' if args.dry_run else 'APPLY'})...")

    state = load_json(STATE_FILE, {"processed_ids": [], "last_run": None})
    rlog = load_json(RLOG_FILE, {"last_report_ts": None, "last_reported_ids": []})

    # Step 1: identify orphans
    entries = state.get("processed_ids", [])
    orphans = [e for e in entries
               if isinstance(e, dict) and not e.get("category")
               and not e.get("from")]
    morning_blanks = [e for e in entries
                      if isinstance(e, dict) and not e.get("from")]

    print(f"  Total state entries: {len(entries)}")
    print(f"  Orphan entries (empty category + empty from): {len(orphans)}")
    print(f"  Blank-from entries (morning window, all 7:10 AM blanks): {len(morning_blanks)}")

    if not orphans and not morning_blanks:
        print("  Nothing to heal — state is clean.")
        return

    # Step 2: enrich from Gmail
    all_healing_ids = [e["id"] for e in orphans]
    if not all_healing_ids:
        print("  No Gmail enrichment needed (no orphans).")
        enriched = {}
    else:
        print(f"  Enriching {len(all_healing_ids)} orphan IDs from Gmail API...")
        enriched = fetch_enrichment(all_healing_ids)
        print(f"  Got category/from/subject for {len(enriched)}/{len(all_healing_ids)}")

    # Step 3: heal entries in place
    healed_count = 0
    for e in entries:
        if not isinstance(e, dict) or e.get("category"):
            continue
        eid = e["id"]
        if eid in enriched and enriched[eid].get("category"):
            e["category"] = enriched[eid]["category"]
            e["from"] = enriched[eid].get("from", "")
            e["subject"] = enriched[eid].get("subject", "")
            healed_count += 1
        elif eid in enriched and enriched[eid].get("from"):
            # From/subject recovered but no category — keep the entry
            e["from"] = enriched[eid].get("from", "")
            e["subject"] = enriched[eid].get("subject", "")
            e["category"] = enriched[eid].get("category", "Uncategorized")
            healed_count += 1

    print(f"  Healed {healed_count} orphan entries (enriched with category + from + subject)")

    # Step 4: prune unrecoverable entries
    prune_ids = set()
    for e in entries:
        if isinstance(e, dict) and not e.get("category"):
            prune_ids.add(e["id"])
    if prune_ids:
        print(f"  Pruning {len(prune_ids)} unhealable entries (no Gmail metadata available)")

    new_processed = [e for e in entries
                     if isinstance(e, dict) and e.get("category")]
    print(f"  State: {len(entries)} → {len(new_processed)} entries")

    # Step 5: un-mark morning-blank IDs from report log so the next digest
    # re-emits them. These are the IDs in the trailing 10 of state (the
    # 7:10 AM digest's "new" window). Without this, even after healing
    # they'd be considered already-reported and never appear.
    reported_ids = list(rlog.get("last_reported_ids", []))
    morning_ids = set(e["id"] for e in morning_blanks
                      if e.get("from"))  # only healed ones
    new_reported = [rid for rid in reported_ids if rid not in morning_ids]
    print(f"  Report log: {len(reported_ids)} → {len(new_reported)} IDs "
          f"(un-marked {len(reported_ids) - len(new_reported)} morning IDs for re-emit)")

    if args.dry_run:
        print("\n  DRY RUN — no changes written. Re-run without --dry-run to apply.")
        return

    # Step 6: back up + save
    backup_state = STATE_FILE.with_suffix(".json.heal-bak")
    backup_rlog = RLOG_FILE.with_suffix(".json.heal-bak")
    shutil.copy(STATE_FILE, backup_state)
    shutil.copy(RLOG_FILE, backup_rlog)
    print(f"  Backed up state → {backup_state.name}")
    print(f"  Backed up report log → {backup_rlog.name}")

    state["processed_ids"] = new_processed
    rlog["last_reported_ids"] = new_reported
    save_json(STATE_FILE, state)
    save_json(RLOG_FILE, rlog)

    print(f"\n  Done. Next digest (1:10 PM / 7:10 PM) will re-emit the morning's")
    print(f"  healed IDs with real senders and subjects.")


if __name__ == "__main__":
    main()
