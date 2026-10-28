#!/usr/bin/env python3
"""
Regression suite for backup_cleaner.py — drives the engine's classifier + delete path
against a synthetic fixture (never touches real backups) and unit-tests path resolution.

Run:  python3 scripts/test_backup_cleaner.py         (from the skill dir, or anywhere)
Exit: 0 all pass, 1 on any failure.
"""
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

FIXTURE = "/tmp/bc_selftest"
os.environ["HERMES_BACKUP_CLEANER_LOG"] = f"{FIXTURE}/log.json"  # isolate manifest BEFORE import
os.environ["HERMES_SCRIPTS_DIR"] = f"{FIXTURE}/scripts"          # fixture's scripts dir for script-match

# ── locate + import the engine ──────────────────────────────────────────────
ENGINE = os.environ.get("BACKUP_CLEANER_PATH", "/home/amh/.hermes/scripts/backup_cleaner.py")
spec = importlib.util.spec_from_file_location("backup_cleaner", ENGINE)
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)

fails = []
def check(name, cond):
    print(f"  [{'✓' if cond else '✗ FAIL'}] {name}")
    if not cond:
        fails.append(name)

# ── build fixture ───────────────────────────────────────────────────────────
shutil.rmtree(FIXTURE, ignore_errors=True)
SK = f"{FIXTURE}/skills/cat/myskill"
os.makedirs(f"{SK}/references", exist_ok=True)
os.makedirs(f"{FIXTURE}/scripts", exist_ok=True)

def w(p, c, mtime=None):
    with open(p, "w") as f: f.write(c)
    if mtime is not None: os.utime(p, (mtime, mtime))

DAY = 86400
t_bak_old = time.time() - 10 * DAY
t_live    = time.time() - 9 * DAY
t_run     = time.time() - 8 * DAY     # ok run AFTER old backup -> VALIDATES
t_bak_new = time.time() - 2 * DAY     # backup AFTER the run -> NOT_YET

w(f"{SK}/SKILL.md", "---\nname: myskill\n---\n", t_live)
# validated: skill-field match
w(f"{SK}/foo.py", "live\n", t_live)
w(f"{SK}/foo.py.bak-tuneup-20260101", "old\n", t_bak_old)
# validated: doc backup (SKILL.md.bak must resolve to SKILL.md, not SKILL)
w(f"{SK}/SKILL.md.bak", "old skill\n", t_bak_old)
# not-yet: backup newer than last ok run
w(f"{SK}/bar.py", "live\n", time.time() - DAY)
w(f"{SK}/bar.py.bak-recent", "old\n", t_bak_new)
# no-cron: skill has no job AND script name not a cron script
os.makedirs(f"{FIXTURE}/skills/cat/nocron", exist_ok=True)
w(f"{FIXTURE}/skills/cat/nocron/SKILL.md", "---\nname: nocron\n---\n", t_live)
w(f"{FIXTURE}/skills/cat/nocron/x.py", "live\n", t_live)
w(f"{FIXTURE}/skills/cat/nocron/x.py.pre-tuneup.bak", "old\n", t_bak_old)
# orphan: no live file
w(f"{SK}/gone.py.bak-x", "orphan\n", t_bak_old)
# anomaly: backup newer than live
w(f"{SK}/baz.py", "live\n", time.time() - 5 * DAY)
w(f"{SK}/baz.py.bak-stale", "backup\n", time.time() - DAY)
# scripts/ backup validated by script-field match
w(f"{FIXTURE}/scripts/tool.py", "live\n", t_live)
w(f"{FIXTURE}/scripts/tool.py.20260101_010101.bak", "old\n", t_bak_old)
# gate regression: a backup-of-a-backup must resolve to the INTERMEDIATE (zz.py.pre-tuneup),
# NOT over-strip to zz.py (which HAS a validating cron). Both live files exist.
w(f"{FIXTURE}/scripts/zz.py", "live\n", t_live)                 # has cron script=zz.py (validated)
w(f"{FIXTURE}/scripts/zz.py.pre-tuneup", "intermediate\n", t_live)  # a distinct, unvalidated file
w(f"{FIXTURE}/scripts/zz.py.pre-tuneup.bak", "old\n", t_bak_old)    # backup OF the intermediate
# F1: over-strip guard — run.sh.bak whose run.sh is GONE but unrelated 'run' exists must be ORPHAN
w(f"{SK}/run", "unrelated\n", t_live)
w(f"{SK}/run.sh.bak", "old\n", t_bak_old)
# F1b: a .word.bak (e.g. .minimax.bak) is NOT stripped to summ.py -> ORPHAN (safe, not auto-cleaned)
w(f"{SK}/summ.py", "live\n", t_live)
w(f"{SK}/summ.py.minimax.bak", "old\n", t_bak_old)
# F3: a file sharing the 'tool.py' basename but NOT under the scripts dir must NOT script-match
w(f"{FIXTURE}/skills/cat/nocron/tool.py", "live\n", t_live)
w(f"{FIXTURE}/skills/cat/nocron/tool.py.bak-collide", "old\n", t_bak_old)
# enabled guard: a disabled job's stale ok must NOT validate
os.makedirs(f"{FIXTURE}/skills/cat/disabledskill", exist_ok=True)
w(f"{FIXTURE}/skills/cat/disabledskill/SKILL.md", "---\nname: disabledskill\n---\n", t_live)
w(f"{FIXTURE}/skills/cat/disabledskill/d.py", "live\n", t_live)
w(f"{FIXTURE}/skills/cat/disabledskill/d.py.bak-x", "old\n", t_bak_old)
# __pycache__ must be skipped entirely
os.makedirs(f"{SK}/__pycache__", exist_ok=True)
w(f"{SK}/__pycache__/foo.pre-x.cpython-311.pyc", "bytecode\n", t_bak_old)

run_iso = datetime.fromtimestamp(t_run, tz=timezone.utc).isoformat()
jobs = [{"id": "job_skill", "skill": "myskill", "script": "myskill_run.py",
         "no_agent": True, "last_status": "ok", "last_run_at": run_iso},
        {"id": "job_script", "skill": None, "script": "tool.py",
         "no_agent": True, "last_status": "ok", "last_run_at": run_iso},
        {"id": "job_zz", "skill": None, "script": "zz.py",
         "no_agent": True, "last_status": "ok", "last_run_at": run_iso},
        {"id": "job_disabled", "skill": "disabledskill", "script": "d_run.py",
         "no_agent": True, "enabled": False, "last_status": "ok", "last_run_at": run_iso}]
w(f"{FIXTURE}/jobs.json", json.dumps(jobs))

# ── 1. classification ────────────────────────────────────────────────────────
results = bc.scan([f"{FIXTURE}/skills", f"{FIXTURE}/scripts"], f"{FIXTURE}/jobs.json")
by_path = {os.path.basename(r["path"]): r for r in results}
print("=== classification ===")
check("foo.py.bak (skill match, run after) -> DELETABLE", by_path["foo.py.bak-tuneup-20260101"]["status"] == "DELETABLE")
check("SKILL.md.bak resolves to SKILL.md (not SKILL) -> DELETABLE",
      by_path["SKILL.md.bak"]["status"] == "DELETABLE" and by_path["SKILL.md.bak"]["live"].endswith("/SKILL.md"))
check("tool.py.<ts>.bak (script match) -> DELETABLE", by_path["tool.py.20260101_010101.bak"]["status"] == "DELETABLE")
check("bar.py.bak-recent (run before backup) -> NOT_YET", by_path["bar.py.bak-recent"]["status"] == "NOT_YET")
check("x.py.pre-tuneup.bak (no cron) -> NO_CRON", by_path["x.py.pre-tuneup.bak"]["status"] == "NO_CRON")
check("gone.py.bak-x (no live file) -> ORPHAN", by_path["gone.py.bak-x"]["status"] == "ORPHAN")
check("baz.py.bak-stale (newer than live) -> ANOMALY", by_path["baz.py.bak-stale"]["status"] == "ANOMALY")
check("run.sh.bak (run.sh gone, unrelated 'run' present) -> ORPHAN [over-strip guard]",
      by_path["run.sh.bak"]["status"] == "ORPHAN")
check("summ.py.minimax.bak (.word.bak not stripped to summ.py) -> ORPHAN",
      by_path["summ.py.minimax.bak"]["status"] == "ORPHAN")
check("tool.py.bak-collide (basename collision, not in scripts dir) -> NO_CRON",
      by_path["tool.py.bak-collide"]["status"] == "NO_CRON")
check("d.py.bak-x (validated only by a DISABLED job) -> NO_CRON",
      by_path["d.py.bak-x"]["status"] == "NO_CRON")
check("zz.py.pre-tuneup.bak resolves to the INTERMEDIATE (not zz.py) -> NO_CRON [backup-of-backup]",
      by_path["zz.py.pre-tuneup.bak"]["status"] == "NO_CRON"
      and by_path["zz.py.pre-tuneup.bak"]["live"].endswith("/zz.py.pre-tuneup"))
check("__pycache__ .pyc skipped entirely", not any("__pycache__" in r["path"] for r in results))
check("scan found exactly 12 backups", len(results) == 12)

# ── 2. path resolution unit ──────────────────────────────────────────────────
print("=== resolve_live ===")
check("live_candidates('SKILL.md.bak') includes SKILL.md",
      any(c.endswith("SKILL.md") for c in bc.live_candidates(f"{SK}/SKILL.md.bak")))

# ── 3. delete path ───────────────────────────────────────────────────────────
print("=== --delete (only DELETABLE removed, others kept, manifest written) ===")
deletable = [r for r in results if r["status"] == "DELETABLE"]
bc.do_delete(deletable)
check("foo.py.bak deleted", not os.path.exists(f"{SK}/foo.py.bak-tuneup-20260101"))
check("SKILL.md.bak deleted", not os.path.exists(f"{SK}/SKILL.md.bak"))
check("tool.py.<ts>.bak deleted", not os.path.exists(f"{FIXTURE}/scripts/tool.py.20260101_010101.bak"))
check("bar.py.bak-recent (NOT_YET) KEPT", os.path.exists(f"{SK}/bar.py.bak-recent"))
check("x.py.pre-tuneup.bak (NO_CRON) KEPT", os.path.exists(f"{FIXTURE}/skills/cat/nocron/x.py.pre-tuneup.bak"))
check("gone.py.bak-x (ORPHAN) KEPT", os.path.exists(f"{SK}/gone.py.bak-x"))
check("baz.py.bak-stale (ANOMALY) KEPT", os.path.exists(f"{SK}/baz.py.bak-stale"))
check("live files intact (foo.py, SKILL.md, tool.py)",
      os.path.exists(f"{SK}/foo.py") and os.path.exists(f"{SK}/SKILL.md") and os.path.exists(f"{FIXTURE}/scripts/tool.py"))
with open(f"{FIXTURE}/log.json") as _mf:
    manifest = json.load(_mf)
check("manifest logged 3 deletions with provenance",
      len(manifest) == 3 and all(m.get("validated_by") and m.get("validated_run_at") for m in manifest))

# ── 4. report format (relative paths, mod date) ──────────────────────────────
print("=== report format ===")
check("_short strips the ~/.hermes/ prefix",
      bc._short(os.path.expanduser("~/.hermes/scripts/x.py.bak")) == "scripts/x.py.bak")
check("render_full includes a 'mod ' date", "mod " in bc.render_full(results))
# delete-aware wording (the auto-delete cron path): when `deleted` is passed the report must
# say what was REMOVED, not prompt to run --delete again.
_full_report = bc.render_full(results)
check("render_full (report mode) prompts to run --delete",
      "--delete" in _full_report and "deletable" in _full_report and "Auto-removed" not in _full_report)
_full_deleted = bc.render_full(results, deleted=deletable)
check("render_full (delete mode) says 'Auto-removed', drops the --delete prompt, header 'removed'",
      "Auto-removed" in _full_deleted
      and "backup_cleaner.py --delete` to remove" not in _full_deleted
      and f"{len(deletable)} removed" in _full_deleted)
_sum_deleted = bc.render_summary(results, deleted=deletable)
check("render_summary (delete mode) says 'Auto-removed' and lists a **Removed** section",
      "Auto-removed" in _sum_deleted and "**Removed**" in _sum_deleted)

shutil.rmtree(FIXTURE, ignore_errors=True)
print()
if fails:
    print(f"✗ FAIL: {len(fails)} check(s) failed: {fails}")
    sys.exit(1)
print("✓ PASS: all backup_cleaner regression checks passed")
