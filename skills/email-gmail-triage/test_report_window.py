#!/usr/bin/env python3
"""Regression suite for the digest selection WATERMARK (gmail_report.py).

Proves the property the old design violated: an email reported in one digest
NEVER reappears in a later digest — even when cumulative volume far exceeds the
old 500-entry report-log cap. Also covers the watermark boundary, exclusion of
unstamped/legacy entries, category routing, and footer-total reconciliation.

Pure: constructs state/report_log dicts in memory and calls build_report
directly. No Gmail API, no Discord, no file or network I/O (entries always
carry from/subject so the heal path never fires).

Run:  python3 test_report_window.py
      GMAIL_REPORT_PATH=/tmp/gmail_audit/gmail_report.py python3 test_report_window.py
"""
import os, sys, importlib.util
from datetime import datetime, timedelta

REPORT_PATH = os.environ.get("GMAIL_REPORT_PATH",
                             "/home/amh/.hermes/scripts/gmail_report.py")
spec = importlib.util.spec_from_file_location("gr", REPORT_PATH)
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)

fail = 0
def check(name, cond, detail=""):
    global fail
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fail += 1

BASE = datetime(2026, 1, 1, 0, 0, 0)
def ts(minutes):
    """Monotonic, fixed-width stamp matching triage's '%Y/%m/%d %H:%M:%S'."""
    return (BASE + timedelta(minutes=minutes)).strftime("%Y/%m/%d %H:%M:%S")

def entry(i, cat="Auto-filed", stamp=None):
    return {"id": f"id{i}", "category": cat,
            "from": f"sender{i}@example.com", "subject": f"Subject {i}",
            "triaged_at": stamp if stamp is not None else ts(i)}

def run_digest(state, report_log):
    """Mirror main()'s successful-send watermark advance."""
    report, new_entries, new_wm, total = gr.build_report("TEST SLOT", state, report_log)
    report_log = gr.advance_watermark(report_log, new_entries, new_wm)   # main()'s own update
    return report, new_entries, total, report_log


# ── 1) THE core invariant: reported once → never re-reported, well past 500. ──
# 100 triage runs × 10 emails = 1000 (> old 500 cap, < 5000 state cap).
# A digest fires after every 3 runs. Every email must be shown EXACTLY once.
state = {"processed_ids": [], "last_run": None}
report_log = {"last_report_ts": None, "last_reported_ids": []}
shown_counts = {}
counter = 0
run = 0
RUNS, PER_RUN, RUNS_PER_DIGEST = 100, 10, 3
for run in range(1, RUNS + 1):
    for _ in range(PER_RUN):
        counter += 1
        # All emails in one triage run share that run's timestamp (realistic).
        state["processed_ids"].append(entry(counter, stamp=ts(run)))
    state["processed_ids"] = state["processed_ids"][-5000:]
    if run % RUNS_PER_DIGEST == 0:
        _, new_entries, _, report_log = run_digest(state, report_log)
        for e in new_entries:
            shown_counts[e["id"]] = shown_counts.get(e["id"], 0) + 1
# final flush
_, new_entries, _, report_log = run_digest(state, report_log)
for e in new_entries:
    shown_counts[e["id"]] = shown_counts.get(e["id"], 0) + 1

duplicates = [i for i, c in shown_counts.items() if c > 1]
never_shown = [f"id{n}" for n in range(1, counter + 1) if f"id{n}" not in shown_counts]
check("1000 emails (>500): none re-reported", not duplicates,
      f"{len(duplicates)} re-reported, e.g. {duplicates[:3]}")
check("1000 emails (>500): every email shown exactly once", not never_shown,
      f"{len(never_shown)} never shown, e.g. {never_shown[:3]}")

# ── 2) Watermark boundary: strictly-after selection. ──────────────────────────
state = {"processed_ids": [entry(1, stamp=ts(10)), entry(2, stamp=ts(20)),
                           entry(3, stamp=ts(30))]}
rlog = {"last_report_ts": ts(20)}              # already reported up to minute 20
_, new_entries, total, _ = run_digest(state, rlog)
ids = {e["id"] for e in new_entries}
check("entry AT the watermark is excluded", "id2" not in ids, f"got {ids}")
check("entry BEFORE the watermark is excluded", "id1" not in ids, f"got {ids}")
check("entry AFTER the watermark is included", ids == {"id3"}, f"got {ids}")

# ── 3) Unstamped / legacy / EPOCH entries are never selected. ────────────────
state = {"processed_ids": [
    {"id": "legacyA", "category": "Auto-filed", "from": "a@x.com", "subject": "A"},  # no triaged_at
    {"id": "legacyB", "category": "", "from": "", "subject": "", "triaged_at": gr.EPOCH},
    entry(99, stamp=ts(500)),
]}
rlog = {"last_report_ts": ts(100)}
report, new_entries, total, _ = run_digest(state, rlog)
ids = {e["id"] for e in new_entries}
check("entry with no triaged_at excluded", "legacyA" not in ids, f"got {ids}")
check("EPOCH/empty-category orphan excluded", "legacyB" not in ids, f"got {ids}")
check("only the real new entry selected", ids == {"id99"}, f"got {ids}")
check("no blank '`: `' rows rendered", "`: `" not in report and "``:" not in report)

# ── 4) Footer total == sum of sections; Unsure counted separately. ───────────
state = {"processed_ids": [
    entry(1, "Action",     ts(10)),
    entry(2, "Action",     ts(11)),
    entry(3, "ReadLater",  ts(12)),
    entry(4, "Auto-filed", ts(13)),
    entry(5, "Unsure",     ts(14)),   # asked in Discord, not a digest row
    {"id": "id6", "category": "", "from": "x@y.com", "subject": "x", "triaged_at": ts(15)},  # empty cat
]}
rlog = {"last_report_ts": ts(0)}
report, new_entries, total, _ = run_digest(state, rlog)
check("footer total = Action+ReadLater+Auto-filed (excludes Unsure/empty)",
      total == 4, f"total={total}")
check("footer line shows total 4", "Total triaged since last digest: 4" in report, report)
check("footer reconciles: 2 Action / 1 ReadLater / 1 Auto-filed",
      "🔴 Action: 2" in report and "🟡 Read Later: 1" in report and "⚪ Auto-filed: 1" in report,
      report)
check("Unsure surfaced as its own count", "❓ Unsure: 1" in report, report)

# ── 5) Category routing into the right sections. ─────────────────────────────
check("Action subject rendered under Action", "Subject 1" in report and "Subject 2" in report)
check("ReadLater subject rendered", "Subject 3" in report)
check("Auto-filed subject rendered", "Subject 4" in report)
check("Unsure subject NOT rendered as a row", "Subject 5" not in report)
check("empty-category subject NOT rendered", "Subject 6" not in report)

# VIP category (re-encountered already-labeled VIP) surfaces under Action.
state = {"processed_ids": [entry(7, "VIP", ts(10))]}
report, new_entries, total, _ = run_digest(state, {"last_report_ts": ts(0)})
check("VIP category surfaces under Action", "🔴 Action: 1" in report and "Subject 7" in report, report)

# ── 6) Empty selection leaves the watermark unchanged. ───────────────────────
state = {"processed_ids": [entry(1, "Action", ts(10))]}
rlog = {"last_report_ts": ts(50)}              # nothing newer than minute 50
report, new_entries, total, new_rlog = run_digest(state, rlog)
check("no new emails → 'No new emails since last digest.'",
      "No new emails since last digest." in report and total == 0, report)
check("empty selection does not move the watermark", new_rlog["last_report_ts"] == ts(50),
      f"watermark={new_rlog['last_report_ts']}")

# ── 7) 2026-09-28: Discord bot limit is 2000 UTF-16 units (was checked vs 4000).
state = {"processed_ids": [dict(entry(i, "Auto-filed", ts(10 + i)),
                                subject="A fairly long promotional subject line number %d here" % i)
                           for i in range(30)]}
report, new_entries, total, new_rlog = run_digest(state, {"last_report_ts": ts(0)})
check("30-row digest (full form would be >2000) fits Discord's 2000-unit limit",
      gr.discord_len(report) <= 2000, str(gr.discord_len(report)))
check("…and it still reports all 30 in the footer total", "Total triaged since last digest: 30" in report, report[:200])
state = {"processed_ids": [dict(entry(i, "Action", ts(10 + i)), subject="X" * 900) for i in range(8)]}
report, *_ = run_digest(state, {"last_report_ts": ts(0)})
check("summary fallback with huge subjects still ≤2000", gr.discord_len(report) <= 2000, str(gr.discord_len(report)))

# ── 8) 2026-09-28: a failed send exits non-zero and does NOT move the watermark;
#    an unreadable report log falls back to its mtime, never EPOCH. ────────────
import tempfile, json as _json
from pathlib import Path as _P
d = _P(tempfile.mkdtemp())
gr.STATE_FILE = d / "state.json"; gr.REPORT_LOG_FILE = d / "log.json"
gr.STATE_FILE.write_text(_json.dumps({"processed_ids": [entry(1, "Action", ts(10))]}))
gr.REPORT_LOG_FILE.write_text(_json.dumps({"last_report_ts": ts(0)}))
gr.send_discord = lambda msg: False
try:
    gr.main(); code = 0
except SystemExit as e:
    code = e.code
check("send failure → exit code 1", code == 1, f"code={code}")
check("send failure → watermark unchanged",
      _json.loads(gr.REPORT_LOG_FILE.read_text())["last_report_ts"] == ts(0))
gr.REPORT_LOG_FILE.write_text('{"last_report_ts": "2026/09')       # torn file
log = gr.load_report_log()
check("torn report log → watermark from mtime (not EPOCH/None)",
      log["last_report_ts"] and log["last_report_ts"] > "2020", str(log))
gr.send_discord = lambda msg: True
gr.REPORT_LOG_FILE.write_text(_json.dumps({"last_report_ts": ts(0)}))
gr.main()
check("send success → watermark advanced + log still valid JSON",
      _json.loads(gr.REPORT_LOG_FILE.read_text())["last_report_ts"] == ts(10))

# ── 9) 2026-09-28 gate lap 1: a digest reading a HALF-WRITTEN triage run (per-
#    email persist) must not lose the emails filed later in the same second. ───
S = ts(100)
state = {"processed_ids": [entry(1, "Auto-filed", S)]}                 # m1 persisted
rlog = {"last_report_ts": ts(0)}
_, sel, _, rlog = run_digest(state, rlog)
check("race: digest#1 shows the 1 entry persisted so far", [e["id"] for e in sel] == ["id1"])
state["processed_ids"] += [entry(2, "Auto-filed", S), entry(3, "Auto-filed", S)]   # same second, later
_, sel, _, rlog = run_digest(state, rlog)
check("race: digest#2 picks up the same-second stragglers (tie-break)", sorted(e["id"] for e in sel) == ["id2", "id3"],
      str([e["id"] for e in sel]))
_, sel, _, rlog = run_digest(state, rlog)
check("race: digest#3 shows nothing (no re-report at the tie)", sel == [], str([e["id"] for e in sel]))
state["processed_ids"] += [entry(4, "Auto-filed", ts(101))]
_, sel, _, rlog = run_digest(state, rlog)
check("race: later stamp moves on normally", [e["id"] for e in sel] == ["id4"] and rlog["reported_at_watermark"] == ["id4"])

# Deploy-time compatibility: a LEGACY report log (no reported_at_watermark) must
# not re-report the entries the old code already showed at its watermark.
state = {"processed_ids": [entry(1, stamp=ts(20)), entry(2, stamp=ts(20)), entry(3, stamp=ts(30))]}
_, sel, _, _ = run_digest(state, {"last_report_ts": ts(20), "last_reported_ids": ["id1", "id2"]})
check("legacy log: ties already in last_reported_ids are NOT re-reported",
      [e["id"] for e in sel] == ["id3"], str([e["id"] for e in sel]))

# gate lap 2: legacy log + an EMPTY same-second digest must not wipe the tie set.
state = {"processed_ids": [entry(1, stamp=ts(20)), entry(2, stamp=ts(20))]}
rlog = {"last_report_ts": ts(20), "last_reported_ids": ["id1", "id2"]}
_, sel, _, rlog = run_digest(state, rlog)                      # empty digest, same second
_, sel2, _, rlog = run_digest(state, rlog)
check("legacy log: empty same-second digest keeps ties (no re-report next time)",
      sel == [] and sel2 == [], f"{[e['id'] for e in sel]} {[e['id'] for e in sel2]}")
rl = gr.advance_watermark({"last_report_ts": ts(20)}, [], ts(20))
check("no tie info at all → stays strict (key not invented)", "reported_at_watermark" not in rl, str(rl))

print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILURE(S)'}")
sys.exit(1 if fail else 0)
