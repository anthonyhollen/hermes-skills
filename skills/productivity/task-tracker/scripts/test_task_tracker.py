#!/usr/bin/env python3
"""Regression suite for task-tracker.py write subcommands + read path.

Runs entirely on a temp copy (TASK_TRACKER_DATA_DIR) — never touches live data,
never sends anything. Usage:
  python3 test_task_tracker.py [path/to/task-tracker.json]   # default: live data file (copied)
Exit 0 = all pass.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "task-tracker.py"
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".hermes" / "data" / "task-tracker.json"
LEGACY = Path(sys.argv[2]) if len(sys.argv) > 2 else SRC.parent / "task-tracker backups"  # pre-existing backups

tmp = Path(tempfile.mkdtemp(prefix="tt-test-"))
ENV = dict(os.environ, TASK_TRACKER_DATA_DIR=str(tmp))
DATA = tmp / "task-tracker.json"
BK = tmp / "task-tracker backups"
fails = []

def run(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], env=ENV, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr

def load():
    return {t["id"]: t for t in json.loads(DATA.read_text())["tasks"]}

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"\n     {detail}"))
    if not cond:
        fails.append(name)

def reset():
    for p in tmp.iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copy(SRC, DATA)
    BK.mkdir()
    # a snapshot = the table as it would be shown now
    out = subprocess.run([sys.executable, str(SCRIPT), "--json"], env=ENV, capture_output=True,
                         text=True, timeout=60).stdout
    secs = json.loads(out)
    rows = {r[0]: i for s in secs for r, i in zip(s["rows"], s["ids"])}
    snap = {r[0]: {"id": i, "name": r[1]} for s in secs for r, i in zip(s["rows"], s["ids"])}
    (tmp / "task-tracker.last-shown.json").write_text(json.dumps({"discord:1": {"shown_at": "test", "rows": snap}}))
    return rows

rows = reset()
orig = DATA.read_bytes()
t = load()
tid = lambda n: rows[str(n)]

# --- read path
rc, out = run("--json")
check("json runs", rc == 0, out[:300])

# --- done by numbers, against the snapshot
rc, out = run("done", "1,2")
d = load()
check("done 1,2 marks snapshot rows", rc == 0 and all(d[tid(n)]["last_done"] != t[tid(n)]["last_done"] for n in (1, 2)), out)
rc, out = run("done", "1")
check("numbers still resolve against the SHOWN table after a write (no re-sort)", "was already done" in out and tid(1) in out, out)

# --- H1: unquoted multi-word args are one item; '2 and 5' is not three matches
rows = reset(); before = load()
rc, out = run("done", "2", "and", "5")
check("'2 and 5' writes nothing", rc == 1 and load() == before, out)
name = next(x["name"] for x in before.values() if len(x["name"].split()) >= 3)
rc, out = run("done", *name.split())
check("unquoted multi-word name = one item", rc == 0 and "SKIPPED" not in out, out)

# --- H2: short / meaningless / stop-word keys don't fuzzy-match
rows = reset(); before = load()
for key in ("x", "&", "a", "and", "the"):
    rc, out = run("done", key)
    check(f"'{key}' does not match", rc == 1 and load() == before, out)

# --- H3: never move a date backwards via done
rows = reset()
run("done", tid(1))
rc, out = run("done", tid(1), "--date", "yesterday")
check("done yesterday after today does not move back", rc == 1 and "more recently" in out, out)

# --- M1: bare all needs --yes
rows = reset(); before = load()
rc, out = run("done", "all")
check("bare 'all' refused without --yes", rc == 1 and load() == before, out)
rc, out = run("done", "all weekly")
from datetime import date
after = load()
check("'all weekly' marks exactly the weekly tasks", rc == 0 and all(
    (v["last_done"] == date.today().isoformat()) == (v["category"] == "weekly" or before[k]["last_done"] == date.today().isoformat())
    for k, v in after.items()), out)

# --- M2: unreachable names rejected
rows = reset()
for bad in ("7", "all", "all weekly"):
    rc, out = run("add", bad, "weekly")
    check(f"add '{bad}' rejected", rc == 1, out)
rc, out = run("add", "All hands wash", "weekly")
rc2, out2 = run("done", "all hands wash")
check("name starting with 'all' is reachable by exact name", rc == 0 and rc2 == 0 and "all-hands-wash" in out2, out + out2)

# --- M4: no-op write makes no backup; undo refuses after a hand edit
rows = reset()
run("done", tid(1)); n = len(list(BK.iterdir()))
rc, out = run("done", tid(1))
check("no-op done makes no backup", len(list(BK.iterdir())) == n and "no change" in out, out)
run("done", tid(2))
d = json.loads(DATA.read_text()); d["tasks"][0]["name"] += " (hand edit)"
DATA.write_text(json.dumps(d, indent=2) + "\n")
rc, out = run("undo")
check("undo refuses after hand edit", rc == 1 and "changed outside" in out, out)

# --- undo chain round-trips byte-identically
rows = reset()
steps = [DATA.read_bytes()]
for args in (("done", "1,3"), ("add", "Test chore", "monthly"), ("rename", "Test chore", "Test chore two"),
             ("set", "test-chore-two", "2026-01-05"), ("move", "2,4", "quarterly"),
             ("remove", "test-chore-two", "--yes")):
    rc, out = run(*args)
    check(f"{args[0]} ok", rc == 0, out)
    steps.append(DATA.read_bytes())
for i in range(len(steps) - 1, 0, -1):
    rc, out = run("undo")
    check(f"undo step {i} byte-identical", rc == 0 and DATA.read_bytes() == steps[i - 1], out)
check("full round-trip == original", DATA.read_bytes() == orig)
rows = reset()
for i in range(12):
    run("add", f"Cap chore {i}", "monthly")
res = [run("undo") for _ in range(12)]; n_ok = sum(r[0] == 0 for r in res)
check("undo depth = 10 kept backups; _undo backups capped at 10",
      n_ok == 10 and len([p for p in BK.iterdir() if "_undo" in p.name]) <= 10, f"undos ok={n_ok} first={res[0][1][:300]}")

# --- corrupt newest backup: clear error, nothing written
rows = reset()
run("done", tid(1))
newest = sorted(BK.iterdir())[-1]
newest.write_text("garbage")
before = DATA.read_bytes()
rc, out = run("undo")
check("corrupt backup → clear error, nothing written", rc == 1 and "unreadable" in out and DATA.read_bytes() == before, out)

# --- remove / rename need exact names
rows = reset(); before = load()
partial = next(" ".join(x["name"].split()[:2]) for x in before.values()
               if len(x["name"].split()) >= 3 and sum(1 for y in before.values()
               if y["name"].lower().startswith(" ".join(x["name"].split()[:2]).lower())) == 1)
rc, out = run("done", partial, "--dry-run")
rc2, out2 = run("remove", partial, "--yes")
check(f"remove refuses a unique partial ('{partial}') that done would accept", rc == 0 and rc2 == 1 and load() == before, out + out2)
rc, out = run("remove", tid(1))
check("remove without --yes refused", rc == 1 and load() == before, out)

# --- bad dates
for bad in ("2026-9-1", "2026-02-30", "tomorrow", "2099-01-01"):
    rc, out = run("done", tid(1), "--date", bad)
    check(f"date {bad} rejected", rc == 1, out)

# --- trailing punctuation / '#' on numbers; exit 3 on partial
rows = reset()
rc, out = run("done", "#1, 2.", "--dry-run")
check("'#1, 2.' resolve as rows", rc == 0 and tid(1) in out and tid(2) in out, out)
rc, out = run("done", "1,999")
check("partial success exits 3 with SKIPPED", rc == 3 and "SKIPPED" in out and load()[tid(1)]["last_done"] != t[tid(1)]["last_done"], out)

# --- move: change frequency by row number, preserving everything else
rows = reset(); pre = DATA.read_bytes(); before = load()
rc, out = run("move", "1,2", "quarterly", "--target", "discord:1")
after = load()
check("move 1,2 → quarterly changes exactly those categories",
      rc == 0 and after[tid(1)]["category"] == "quarterly" and after[tid(2)]["category"] == "quarterly"
      and all(after[k] == before[k] for k in after if k not in (tid(1), tid(2))), out)
check("move preserves name / last_done / added",
      all(after[tid(n)][f] == before[tid(n)][f] for n in (1, 2) for f in ("name", "last_done", "added")), out)
rc, out = run("undo")
check("undo reverses a move byte-identically", rc == 0 and DATA.read_bytes() == pre, out)
rows = reset(); before = load()
rc, out = run("move", "1", "yearly")
check("move to an invalid category refused, nothing written", rc == 1 and "invalid category" in out and load() == before, out)
rc, out = run("move", "999", "weekly")
check("move with an unknown row refuses, nothing written", rc == 1 and "no such row" in out and load() == before, out)
rc, out = run("move", "1", "quarterly", "--target", "discord:1")
nb = len(list(BK.iterdir()))
rc2, out2 = run("move", "1", "quarterly", "--target", "discord:1")
check("second identical move is a no-op with no backup",
      rc == 0 and rc2 == 0 and "already" in out2 and "no change" in out2 and len(list(BK.iterdir())) == nb, out2)
rc, out = run("move", "1,999", "fortnightly", "--target", "discord:1")
check("partial move exits 3 with the good row written",
      rc == 3 and "SKIPPED" in out and load()[tid(1)]["category"] == "fortnightly", out)

# --- a row whose task was renamed since the table was shown is refused (name changed)
rows = reset()
old_name = load()[tid(3)]["name"]
run("rename", tid(3), old_name + " renamed")
before = load()
rc, out = run("done", "3")
check("row of a task renamed since shown is refused", rc == 1 and load() == before and "renamed or removed" in out, out)

# --- undo stack: after a no-op, after a hand edit (multi-step), and with legacy backups only
rows = reset()
run("done", tid(1)); run("done", tid(1))  # second is a no-op
rc, out = run("undo")
check("undo after a no-op command refuses", rc == 1 and "changed nothing" in out, out)
rc, out = run("undo", "--previous")
check("undo --previous reverts the real write", rc == 0 and load()[tid(1)]["last_done"] == t[tid(1)]["last_done"], out)
rows = reset()
run("done", tid(1))
d = json.loads(DATA.read_text()); d["tasks"][0]["name"] += " (hand edit)"
DATA.write_text(json.dumps(d, indent=2) + "\n")
run("done", tid(2))
rc1, out1 = run("undo")
rc2, out2 = run("undo")
check("second undo refuses to cross a hand edit", rc1 == 0 and rc2 == 1 and "(hand edit)" in DATA.read_text(), out1 + out2)
if LEGACY.is_dir() and any(LEGACY.iterdir()):
    rows = reset()
    for f in LEGACY.iterdir():
        if f.is_file():
            shutil.copy(f, BK / f.name)
    run("done", tid(1))
    rc1, out1 = run("undo")
    before = DATA.read_bytes()
    rc2, out2 = run("undo")
    rc3, out3 = run("undo", "--force")
    check("undo never walks into pre-existing (legacy) backups, not even with --force",
          rc1 == 0 and rc2 == 1 and rc3 == 1 and "nothing left" in out3
          and DATA.read_bytes() == before and DATA.read_bytes() == orig, out1 + out2 + out3)

# --- snapshot: a partial delivery only remaps the sections actually sent
rows = reset()
import importlib.util, types
os.environ["TASK_TRACKER_DATA_DIR"] = str(tmp)
spec = importlib.util.spec_from_file_location("tt", SCRIPT); tt = importlib.util.module_from_spec(spec); spec.loader.exec_module(tt)
secs = tt.build_sections(tt.load_data(), date.today())
for sec in secs:
    sec["ids"] = ["fake-id"] * len(sec["ids"])  # pretend every section changed
calls = []
def fake_post(token, cid, content, img=None):
    calls.append(content)
    if len(calls) >= 3:
        raise tt.FatalSend("boom")
    return True
tt.discord_post, tt.render_tables, tt.RATE_DELAY = fake_post, (lambda ss: {x["title"]: None for x in ss}), 0
try:
    tt.deliver("T", "1", secs, date.today(), "discord:1")
except tt.FatalSend:
    pass
snap = json.loads((tmp / "task-tracker.last-shown.json").read_text())["discord:1"]["rows"]
second = secs[1]["rows"][0][0]
check("partial delivery remaps only sent sections", snap["1"]["id"] == "fake-id" and snap[second]["id"] == rows[second], str(snap)[:300])

# --- per-thread snapshots: row numbers follow the thread the user is looking at
rows = reset()
snaps = json.loads((tmp / "task-tracker.last-shown.json").read_text())
other = {k: {"id": rows["1"], "name": load()[rows["1"]]["name"]} for k in snaps["discord:1"]["rows"]}
snaps["discord:2"] = {"shown_at": "zzz-later", "rows": other}  # a later table elsewhere: every row = task 1
(tmp / "task-tracker.last-shown.json").write_text(json.dumps(snaps))
rc, out = run("done", "5", "--target", "discord:1", "--dry-run")
check("--target resolves against that thread's table", rc == 0 and rows["5"] in out, out)
rc, out = run("done", "5", "--target", "discord:999")
check("--target with no table shown there refuses", rc == 1 and "this thread" in out, out)
rc, out = run("done", "5", "--target", "discord:7:1", "--dry-run")
rc2, out2 = run("done", "5", "--target", "1", "--dry-run")
check("--target discord:<chan>:<thread> and a bare id normalize to discord:<thread>",
      rc == 0 and rows["5"] in out and rc2 == 0 and rows["5"] in out2, out + out2)
rc, out = run("done", "5", "--dry-run")
check("no --target → the most recently shown table (discord:2)", rc == 0 and rows["1"] in out and "discord:2" in out, out)

# --- deep undo keeps every replaced state recoverable; no-op undo can't wedge; missing backup
rows = reset()
states = []
for i in range(5):
    run("add", f"Deep chore {i}", "monthly"); states.append(DATA.read_bytes())
for i in range(5):
    run("undo")
kept = {f.read_bytes() for f in BK.iterdir() if f.is_file()}
check("after 5 undos every undone state is still in a backup", all(st in kept for st in states))
rows = reset()
run("done", tid(1)); run("done", tid(2))
u = json.loads((tmp / "task-tracker.undo.json").read_text())
shutil.copy(BK / u["stack"][-1]["backup"], DATA)  # as if a crash hit mid-undo
rc0, out0 = run("undo", "--dry-run")
rc1, out1 = run("undo")
rc2, out2 = run("undo")
check("undo whose target is already in place consumes the entry without --force (no wedge)",
      rc0 == 0 and "would be consumed" in out0 and rc1 == 0 and "consumed" in out1
      and rc2 == 0 and DATA.read_bytes() == orig, out0 + out1 + out2)
rows = reset()
run("done", tid(1)); run("done", tid(2))
u = json.loads((tmp / "task-tracker.undo.json").read_text())
(BK / u["stack"][-1]["backup"]).unlink()
ubefore = (tmp / "task-tracker.undo.json").read_bytes()
rc0, out0 = run("undo", "--dry-run")
dry_same = (tmp / "task-tracker.undo.json").read_bytes() == ubefore
before = DATA.read_bytes()
rc1, out1 = run("undo")
rc2, out2 = run("undo")
check("missing backup: dry run leaves history; real run clears it, never suggests --force; nothing reverted",
      rc0 == 1 and dry_same and rc1 == 1 and "cleared" in out1 and "do NOT use undo --force" in out1
      and rc2 == 1 and "nothing left to undo" in out2 and DATA.read_bytes() == before, out0 + out1 + out2)
rows = reset()
run("done", tid(1))
ubefore = (tmp / "task-tracker.undo.json").read_bytes()
rc, out = run("undo", "--dry-run")
check("undo --dry-run changes nothing", rc == 0 and (tmp / "task-tracker.undo.json").read_bytes() == ubefore
      and load()[tid(1)]["last_done"] != t[tid(1)]["last_done"], out)

# --- dry run writes nothing
rows = reset(); before = DATA.read_bytes()
rc, out = run("done", "1,2", "--dry-run")
check("dry run writes nothing", rc == 0 and DATA.read_bytes() == before and not list(BK.iterdir())
      and not (tmp / "task-tracker.undo.json").exists(), out)

shutil.rmtree(tmp)
print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
