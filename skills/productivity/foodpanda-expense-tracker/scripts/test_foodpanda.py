#!/usr/bin/env python3
"""Offline regression suite for the food-expense scripts — no network, no live sheet/Discord.

    python scripts/test_foodpanda.py            # tests the copies next to this file
    FP_SCRIPTS_DIR=~/.hermes/scripts python ... # tests the deployed cron copies

HERMES_HOME is pointed at a temp dir, and Google (run_gapi / the Sheets service), Discord
(urlopen) and sleeps are stubbed, so nothing here can touch real data. One check per fixed bug.
"""
import importlib.util, io, json, os, socket, sys, tempfile, urllib.error
from datetime import date, datetime

sys.dont_write_bytecode = True   # don't leave __pycache__ in the skill / cron scripts dirs

HERE = os.environ.get("FP_SCRIPTS_DIR") or os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="fp_test_")
os.environ["HERMES_HOME"] = TMP
os.makedirs(os.path.join(TMP, "cron"), exist_ok=True)

def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.time.sleep = lambda s: None
    m.DISCORD_BOT_TOKEN = "test-token"
    return m

sync, weekly, rem = (load("foodpanda_sync"), load("foodpanda_weekly_report"),
                     load("foodpanda_payment_reminder"))
FAILS = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

# ── fake Discord ──────────────────────────────────────────────────────────────
class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

def fake_urlopen(script, posts):
    """script: list of outcomes per call — 'ok' | an exception instance."""
    def _open(req, timeout=None):
        posts.append(json.loads(req.data))
        outcome = script.pop(0) if script else "ok"
        if outcome != "ok":
            raise outcome
        return Resp(b'{"id": "m1"}')
    return _open

def http_err(code, body=b"{}"):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))

# ── parser ────────────────────────────────────────────────────────────────────
GRAB = sync.PROVIDERS[1]
FP = sync.PROVIDERS[0]
grab_food = ("Hope you enjoyed your food! TOTAL S$ 37.20 Pickup time: 20 Sep 26 16:46 +0800 "
             "Booking code A-9RVL29QWWENFAV Order from: Shang Hai Shi Xiang Ge Delivery location "
             "x TOTAL (INCL. TAX) S$ 37.20")
grab_ride = ("Hope you enjoyed your ride! Picked up on 19 August 2026 Booking ID: A-9NS73DGWW7FRAV "
             "Total Paid S$13.30 Breakdown Fare 11.20")
fp_ok = ("Order number: abcd-1234 Order date: 2026-09-21 Name: Giant (Tampines North) Partner "
         "address x Subtotal S$ 90.00 Order total S$ 94.45")
check("grab food parses", sync.extract_expenses(grab_food, GRAB) ==
      ("ok", ("2026-09-20", "Shang Hai Shi Xiang Ge", 37.2, "A-9RVL29QWWENFAV")))
check("grab ride → nonfood (no false 'unparseable' alert)",
      sync.extract_expenses(grab_ride, GRAB) == ("nonfood", None))
check("ride marker never skips a real food receipt",
      sync.extract_expenses(grab_food + " Hope you enjoyed your ride", GRAB)[0] == "ok")
check("foodpanda parses", sync.extract_expenses(fp_ok, FP) ==
      ("ok", ("2026-09-21", "Giant (Tampines North)", 94.45, "abcd-1234")))
check("impossible FoodPanda date → fail, not a crash loop",
      sync.extract_expenses(fp_ok.replace("2026-09-21", "2026-09-31"), FP) == ("fail", None))
check("merchant overrun (end label missing) → fail",
      sync.extract_expenses(fp_ok.replace("Partner address", "") + " x" * 200, FP) == ("fail", None))
check("no S$ → foreign", sync.extract_expenses("TOTAL Rp 50.000", GRAB) == ("foreign", None))
check("overseas ride (no S$) → nonfood, not a 'non-SGD' note",
      sync.extract_expenses("Hope you enjoyed your ride! Total Paid Rp 50.000", GRAB) == ("nonfood", None))
def _pd(d):
    try:
        return sync.parse_email_date(d).isoformat()
    except Exception as e:
        return repr(e)
check("Date header variants parse ('(UTC)', GMT, no weekday)",
      all(_pd(d) == "2026-05-02T12:25:07+08:00" for d in
          ["Sat, 2 May 2026 04:25:07 +0000", "Sat, 2 May 2026 04:25:07 +0000 (UTC)",
           "Sat, 02 May 2026 04:25:07 GMT", "2 May 2026 04:25:07 +0000"]))
try:
    sync.parse_email_date(""); empty_raises = False
except Exception:
    empty_raises = True
check("empty Date header still raises (counted as unreadable)", empty_raises)

# ── run_gapi: interpreter + non-JSON empty search ─────────────────────────────
seen = {}
class R:  # fake CompletedProcess
    def __init__(self, out): self.returncode, self.stdout, self.stderr = 0, out, ""
def fake_run(cmd, **kw):
    seen["cmd"] = cmd
    return R("No messages found.\n")
real_run = sync.subprocess.run
sync.subprocess.run = fake_run          # (one shared subprocess module for all three scripts)
check("'No messages found.' (non-gws empty search) → []", sync.run_gapi(["gmail", "search", "q"]) == [])
for m in (sync, weekly, rem):
    seen.clear()
    try:
        m.run_gapi(["sheets", "get"])
    except Exception:
        pass                             # the fake output isn't JSON — only the argv matters
    check(f"{m.__name__}: run_gapi spawns sys.executable", seen.get("cmd", [""])[0] == sys.executable,
          seen.get("cmd"))
sync.subprocess.run = real_run

# ── Discord POST: never re-send after a read timeout; retry 429/5xx/connect errors ─
for m in (sync, weekly, rem):
    posts = []
    m.urllib.request.urlopen = fake_urlopen([socket.timeout("The read operation timed out")], posts)
    try:
        m.discord_send("c", {"content": "x"})
        raised = False
    except Exception:
        raised = True
    check(f"{m.__name__}: read timeout on POST is not re-sent", raised and len(posts) == 1, len(posts))
    posts = []
    m.urllib.request.urlopen = fake_urlopen([http_err(429, b'{"retry_after": 1}'), http_err(503),
                                             urllib.error.URLError("refused")], posts)
    m.discord_send("c", {"content": "x"}, attempts=4)
    check(f"{m.__name__}: 429 / 503 / connect error are retried", len(posts) == 4, len(posts))
    for code in (500, 502, 504):   # the message may exist — don't risk a duplicate
        posts = []
        m.urllib.request.urlopen = fake_urlopen([http_err(code)], posts)
        try:
            m.discord_send("c", {"content": "x"}); raised = False
        except urllib.error.HTTPError:
            raised = True
        check(f"{m.__name__}: HTTP {code} not retried", raised and len(posts) == 1, len(posts))
    posts = []
    m._run_deadline = -1e9           # past the run deadline: posts go out, but single-shot
    m.urllib.request.urlopen = fake_urlopen([urllib.error.URLError("refused")], posts)
    try:
        m.discord_send("c", {"content": "x"}); raised = False
    except urllib.error.URLError:
        raised = True
    m._run_deadline = None
    check(f"{m.__name__}: past the deadline a post is single-shot", raised and len(posts) == 1, len(posts))
    posts = []
    m.urllib.request.urlopen = fake_urlopen([http_err(403)], posts)
    try:
        m.discord_send("c", {"content": "x"}); raised = False
    except urllib.error.HTTPError:
        raised = True
    check(f"{m.__name__}: 403 not retried", raised and len(posts) == 1)

# ── sync main(): migration cost, alerts past the deadline, watermark holds, exit codes ─
SHEET = {"Sep 2026": [["Date", "Merchant", "Amount", "Email ID", "Order ID", "Provider", "MTD Total"],
                      ["2026-09-02", "Giant", "63.9", "old1", "moi1", "FoodPanda", "63.9"]]}
for i in range(1, 9):  # 8 more legacy tabs, all already migrated
    SHEET[f"{sync.MONTHS[i-1]} 2026"] = [SHEET["Sep 2026"][0][:], ["2026-0%d-02" % i, "X", "1", "", "", "FoodPanda", "1"]]

def run_sync(msgs, bodies, state=None, deadline_at_read=False, alert_fail=False, on_call=None):
    """Drive sync.main() on fakes. Returns (exit_code, posts, gapi_calls, writes, state)."""
    calls, writes, posts = [], [], []
    st = os.path.join(TMP, "sync_state.json")
    sync.STATE_FILE = st
    json.dump(state or {"providers": {"FoodPanda": {"last_checked_email_ts": "2026-09-20T00:00:00+08:00"},
                                      "Grab": {"last_checked_email_ts": "2026-09-20T00:00:00+08:00"}}},
              open(st, "w"))
    def gapi(args):
        calls.append(args[:2])
        if on_call:
            on_call(args)
        if args[:2] == ["gmail", "search"]:
            return [m for m in msgs if m["_p"] in args[2]]
        if args[:2] == ["gmail", "get"]:
            b = bodies[args[2]]
            if isinstance(b, Exception):
                raise b
            return {"body": b}
        tab, rng = args[3].rsplit("!", 1)
        rows = SHEET.get(tab.strip("'"), [])
        if deadline_at_read and rng == "A:F":
            sync._run_deadline = -1e9   # the run deadline passes here, mid-run
            raise TimeoutError("run deadline exceeded — aborting to stay under the cron watchdog")
        if rng == "A:A":
            return [r[:1] for r in rows]
        if rng == "G2":
            return [["63.9"]]
        return rows
    class Req:
        def __init__(self, fn): self.fn = fn
        def execute(self): return self.fn()
    class Svc:
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, **k): return Req(lambda: {"sheets": [{"properties": {"title": t}} for t in SHEET]})
        def batchGet(self, **k):
            calls.append(["batchGet"])
            return Req(lambda: {"valueRanges": [{"values": SHEET[r.rsplit("!", 1)[0][1:-1].replace("''", "'")]}
                                                for r in k["ranges"]]})
        def update(self, **k):
            if "'s " in k["range"]:
                raise RuntimeError("HttpError 400 Unable to parse range")
            writes.append(k["range"])
            tab, rng = k["range"].rsplit("!", 1)
            if rng.startswith("A") and ":F" in rng:   # a data-row write — reflect it in the fake sheet
                start = int(rng.split(":")[0][1:])
                rows = SHEET.setdefault(tab.strip("'"), [])
                for i, v in enumerate(k["body"]["values"]):
                    while len(rows) < start + i:
                        rows.append([])
                    rows[start + i - 1] = [str(x) for x in v]
            return Req(lambda: {})
        def batchUpdate(self, **k):
            writes.append("batchUpdate"); return Req(lambda: {})
    sync.run_gapi = gapi
    sync.get_sheets_service = lambda: Svc()
    sync.urllib.request.urlopen = fake_urlopen([http_err(403)] * 50 if alert_fail else [], posts)
    try:
        code = sync.main()
    except Exception as e:
        code = f"RAISED {type(e).__name__}"
    return code, posts, calls, writes, json.load(open(st))

def msg(mid, p, day, hh="12"):
    return {"id": mid, "_p": "foodpanda" if p == "FP" else "grab",
            "date": f"Mon, {day} Sep 2026 {hh}:00:00 +0000"}

code, posts, calls, writes, st = run_sync([msg("new1", "FP", 22)], {"new1": fp_ok.replace("abcd-1234", "zz-1")})
spawns = sum(1 for c in calls if c != ["batchGet"])
check("sync: new receipt appended + summary posted + exit 0",
      code == 0 and any("Synced" in json.dumps(p) for p in posts) and any("A3:F3" in w for w in writes),
      (code, writes))
check("sync: migration reads all 9 tabs in ONE batchGet (not 18 spawns)",
      calls.count(["batchGet"]) == 1 and spawns <= 8, (calls.count(["batchGet"]), spawns))
check("sync: no migration writes on already-migrated tabs", not [w for w in writes if "F1:" in w or "G1:" in w or w.endswith("!E1")], writes)

code, posts, calls, writes, st = run_sync([msg("r1", "GR", 21)], {"r1": grab_ride})
check("sync: ride-only run → no alert, Grab watermark advances",
      posts == [] and st["providers"]["Grab"]["last_checked_email_ts"].startswith("2026-09-21"), (posts, st))

bad_date = {"id": "nd", "_p": "foodpanda", "date": ""}
code, posts, calls, writes, st = run_sync([bad_date, msg("new2", "FP", 23)], {"new2": fp_ok.replace("abcd-1234", "zz-2")})
check("sync: unreadable Date → alerted; other receipts still recorded and the watermark advances",
      any("Unreadable" in json.dumps(p) for p in posts)
      and st["providers"]["FoodPanda"]["last_checked_email_ts"].startswith("2026-09-23"), (posts, st))

SHEET["Oct 2026"] = []   # a brand-new EMPTY tab: E1 + F1 + the G MTD all in the SAME run
code, posts, calls, writes, st = run_sync([msg("new4", "FP", 25)], {"new4": fp_ok.replace("abcd-1234", "zz-4")})
check("sync: empty tab gets Order ID, Provider and MTD headers in one run (as the per-tab re-read did)",
      all(any(w == f"'Oct 2026'!{r}" for w in writes) for r in ("E1", "F1:F1", "G1:G2")), writes)
del SHEET["Oct 2026"]

SHEET["Anthony's notes"] = [["just", "notes"]]   # unmigrated non-month tab whose writes 400
code, posts, calls, writes, st = run_sync([msg("new5", "FP", 25)], {"new5": fp_ok.replace("abcd-1234", "zz-5")})
check("sync: a tab whose migration write fails is skipped — the sync still records receipts",
      code == 0 and any("Synced" in json.dumps(p) for p in posts), (code, posts))
del SHEET["Anthony's notes"]

class Clock:   # the run deadline passes while month 1 is being processed
    t = 0.0
real_mono = sync.time.monotonic
sync.time.monotonic = lambda: Clock.t
orig_gapi_holder = {}
def tick_after_read(args):
    if args[:2] == ["sheets", "get"] and args[3].endswith("!A:F"):
        Clock.t = 1e6
code, posts, calls, writes, st = run_sync(
    [msg("m1", "FP", 22), {"id": "m2", "_p": "foodpanda", "date": "Mon, 03 Aug 2026 12:00:00 +0000"},
     {"id": "m3", "_p": "foodpanda", "date": "Mon, 06 Jul 2026 12:00:00 +0000"}],
    {"m1": fp_ok.replace("abcd-1234", "zz-6"),
     "m2": fp_ok.replace("abcd-1234", "zz-7").replace("2026-09-21", "2026-08-03"),
     "m3": fp_ok.replace("abcd-1234", "zz-8").replace("2026-09-21", "2026-07-06")},
    state={"providers": {"FoodPanda": {"last_checked_email_ts": "2026-07-01T00:00:00+08:00"},
                         "Grab": {"last_checked_email_ts": "2026-09-20T00:00:00+08:00"}}},
    on_call=tick_after_read)
sync.time.monotonic = real_mono
out_of_time = [p for p in posts if "ran out of time" in json.dumps(p, ensure_ascii=False)]
check("sync: past the deadline → ONE combined alert, month loop stops, state not saved, exit 1",
      code == 1 and len(out_of_time) == 1
      and st["providers"]["FoodPanda"]["last_checked_email_ts"] == "2026-07-01T00:00:00+08:00", (code, posts))

code, posts, calls, writes, st = run_sync([msg("f1", "FP", 22)], {"f1": RuntimeError("gapi failed: 500")})
check("sync: persistent fetch failure → non-zero exit (job shows red)", code == 1, code)

code, posts, calls, writes, st = run_sync([msg("new3", "FP", 22)], {"new3": fp_ok.replace("abcd-1234", "zz-3")},
                                          deadline_at_read=True)
check("sync: past the run deadline the failure is still POSTED (alerts aren't deadline-gated)",
      len(posts) >= 1, (code, posts))

# ── weekly: previous complete week, Sunday included, exit codes ───────────────
check("weekly: Sunday 4 Oct → Sun 27 Sep – Sat 3 Oct", weekly.week_range(date(2026, 10, 4)) == (date(2026, 9, 27), date(2026, 10, 3)))
check("weekly: a Saturday run → the Sun–Sat week before (never the unfinished one)",
      weekly.week_range(date(2026, 10, 3)) == (date(2026, 9, 20), date(2026, 9, 26)))
check("weekly: a mid-week manual run → last complete Sun–Sat",
      weekly.week_range(date(2026, 9, 30)) == (date(2026, 9, 20), date(2026, 9, 26)))
check("weekly: Sun 3 Jan 2027 → 27 Dec 2026 – 2 Jan 2027 (spans a year)",
      weekly.week_range(date(2027, 1, 3)) == (date(2026, 12, 27), date(2027, 1, 2)))
check("weekly: every weekday maps to a Sun–Sat window ending before today",
      all(weekly.week_range(date(2026, 10, d))[0].weekday() == 6 and weekly.week_range(date(2026, 10, d))[1].weekday() == 5
          and weekly.week_range(date(2026, 10, d))[1] < date(2026, 10, d) for d in range(1, 15)))
wk_posts = []
weekly.urllib.request.urlopen = fake_urlopen([], wk_posts)
weekly.sgt_now = lambda: datetime(2026, 9, 27, 9, 5, tzinfo=weekly.SGT)   # Sunday → Sep 20–26
WK = {"Sep 2026": [["Date", "Merchant", "Amount", "Email ID", "Order ID", "Provider"],
                   ["2026-09-20", "Sunday order", "5", "", "", "Grab"],
                   ["2026-09-21", "A", "10", "", "", "FoodPanda"],
                   ["2026-09-27", "Today", "99", "", "", "FoodPanda"],
                   ["21/09/2026", "Hand-typed date", "2", "", "", "FoodPanda"]]}
def wk_gapi(args):
    tab, rng = args[3].rsplit("!", 1)
    if tab.strip("'") not in WK:
        raise RuntimeError("Unable to parse range")
    return [["999"]] if rng == "G2" else WK[tab.strip("'")]   # G2 (whole tab) must NOT be used
weekly.run_gapi = wk_gapi
code = weekly.main()
desc = wk_posts[-1]["embeds"][0]["description"] if wk_posts else ""
check("weekly: the window's Sunday order is in, today's (next window) isn't; MTD through Saturday",
      code == 0 and "Sunday order" in desc and "Today" not in desc and "MTD Total: S$17.00" in desc, desc)
weekly.sgt_now = lambda: datetime(2026, 10, 4, 9, 5, tzinfo=weekly.SGT)   # Sep 27–Oct 3, no Oct tab yet
WK["Sep 2026"] = [r for r in WK["Sep 2026"] if r[0].startswith("2026-09-2") and r[0] < "2026-09-27"]  # an empty week
wk_posts.clear()
check("weekly: empty week at a month start doesn't cite last month's rows against a S$0.00 MTD",
      weekly.main() == 0 and "recorded earlier" not in wk_posts[-1]["embeds"][0]["description"]
      and "MTD Total: S$0.00" in wk_posts[-1]["embeds"][0]["description"], wk_posts[-1:] if wk_posts else None)
weekly.run_gapi = lambda a: (_ for _ in ()).throw(RuntimeError("HttpError 403"))
check("weekly: read failure → alert + non-zero exit", weekly.main() == 1)

# ── reminder ──────────────────────────────────────────────────────────────────
rem.STATE_FILE = os.path.join(TMP, "rem_state.json")
rem.JOBS_FILE = os.path.join(TMP, "cron", "jobs.json")
AUG = {"Sep 2026": [["Date", "Merchant", "Amount"], ["2026-09-02", "Giant", "63.9"]]}
rem.run_gapi = lambda a: AUG[a[3].rsplit("!", 1)[0].strip("'")]
def rem_run(argv, jobs_last_run=None, send=None, status="ok"):
    posts = []
    rem.urllib.request.urlopen = fake_urlopen(list(send or []), posts)
    json.dump({"jobs": [{"script": "foodpanda_sync.py", "enabled": True, "last_status": status,
                         "last_run_at": jobs_last_run}]} if jobs_last_run else {"jobs": []},
              open(rem.JOBS_FILE, "w"))
    code = rem.main(argv)
    return code, posts, (json.load(open(rem.STATE_FILE)) if os.path.exists(rem.STATE_FILE) else {})

json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run(["--simulate-date", "2026-10-01"])
check("reminder: --simulate-date alone never posts or marks a cycle",
      code == 0 and posts == [] and st == {"last_fired_cycle": "Aug 2026"}, (posts, st))
rem.sgt_now = lambda: datetime(2026, 10, 1, 9, 0, tzinfo=rem.SGT)
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00")
content = posts[0]["content"] if posts else ""
check("reminder: real 1 Oct fire posts once, with nonce, no caveat, marks Sep",
      code == 0 and len(posts) == 1 and posts[0].get("enforce_nonce") is True
      and "Heads-up" not in content and st["last_fired_cycle"] == "Sep 2026", (posts, st))
code, posts, st = rem_run([])
check("reminder: already fired → silent", code == 0 and posts == [])
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00")
check("reminder: the month's last-night sync succeeded → no caveat (no catch-up needed)",
      len(posts) == 1 and "Heads-up" not in posts[0]["content"], posts)
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00", status="error")
check("reminder: last-night sync FAILED → caveat in the message",
      len(posts) == 1 and "Heads-up" in posts[0]["content"], posts)
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run([], jobs_last_run="2026-09-29T23:30:40+08:00")
check("reminder: no sync on the month's last night (last OK run the day before) → caveat",
      len(posts) == 1 and "Heads-up" in posts[0]["content"], posts)
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
rem.sgt_now = lambda: datetime(2026, 10, 2, 9, 0, tzinfo=rem.SGT)   # day-2 retry of a failed 1st
code, posts, st = rem_run([], jobs_last_run="2026-10-01T23:30:40+08:00")
check("reminder: day-2 retry after the 1st's syncs → NO false caveat",
      len(posts) == 1 and "Heads-up" not in posts[0]["content"], posts)
check("reminder: naive last_run_at is read as SGT",
      (json.dump({"jobs": [{"script": "/home/amh/.hermes/scripts/foodpanda_sync.py", "last_status": "ok",
                            "last_run_at": "2026-10-01T08:46:00"}]}, open(rem.JOBS_FILE, "w"))
       or rem.sync_covered_month_end(date(2026, 10, 1))) is True)
rem.sgt_now = lambda: datetime(2026, 10, 1, 9, 0, tzinfo=rem.SGT)
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00",
                          send=[socket.timeout("The read operation timed out")])
reminder_posts = [p for p in posts if "content" in p and "Reminder" in p.get("content", "")]
check("reminder: read timeout → reminder NOT re-sent, not marked, exit 1",
      code == 1 and len(reminder_posts) == 1 and st["last_fired_cycle"] == "Aug 2026", (code, posts, st))
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
real_save = rem.save_state
rem.save_state = lambda s: (_ for _ in ()).throw(OSError(28, "No space left on device"))
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00")
rem.save_state = real_save
check("reminder: posted but state write failed → says it WAS posted, exit 1",
      code == 1 and any("WAS posted" in json.dumps(p, ensure_ascii=False) for p in posts), posts)
json.dump({"last_fired_cycle": "Aug 2026"}, open(rem.STATE_FILE, "w"))
code, posts, st = rem_run([], jobs_last_run="2026-09-30T23:30:40+08:00", send=[http_err(502)])
check("reminder: a transient 502 is retried (nonce-protected) → posted, marked",
      code == 0 and len(posts) == 2 and posts[0].get("nonce") == posts[1].get("nonce")
      and st["last_fired_cycle"] == "Sep 2026", (code, posts, st))
check("reminder: sync_covered_month_end three-valued (unreadable jobs.json → None)",
      (os.remove(rem.JOBS_FILE) or rem.sync_covered_month_end(date(2026, 10, 1))) is None)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
