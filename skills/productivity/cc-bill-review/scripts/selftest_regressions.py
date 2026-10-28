"""Offline regression cases for the v3.5 fixes — imported by selftest.py.

Every case runs against a TEMP COPY of the DB (parse_ocbc.DB_PATH is pointed at
it and restored afterwards), with Discord's urlopen and the hermes cron state
stubbed: no network, no live writes. Each case is named after the bug it pins.
"""
from __future__ import annotations

import io
import json
import re
import os
import shutil
import tempfile
import urllib.error
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts import parse_ocbc as P

SYNTH_HEADER = """OCBC Bank STATEMENT OF ACCOUNT
Card No.: XXXX-XXXX-XXXX-{card}
STATEMENT DATE
{stmt}
PAYMENT DUE DATE
{due}
TOTAL MINIMUM DUE
S$50.00
TRANSACTION DATE DESCRIPTION AMOUNT (SGD)
"""


def synth_statement(card, stmt, due, txns, total):
    """OCBC-layout text: date / amount / description lines / country, and the
    statement total right after the last transaction (the real-PDF quirk)."""
    out = [SYNTH_HEADER.format(card=card, stmt=stmt, due=due)]
    for d, amt, desc, cc in txns:
        out.append(f"  {d}")
        out.append(f"{amt}")
        out.extend(desc.split("\n"))
        if cc:
            out.append(cc)
    out.append(f"{total:,.2f}")
    out.append("TOTAL AMOUNT DUE")
    out.append(f"{total:,.2f}")
    return "\n".join(out) + "\n" + "x" * 120


@contextmanager
def sandbox(seed_db: bool = True):
    """Temp DB (copy of the live one, or empty) + temp HERMES_HOME with a FAKE token."""
    tmp = Path(tempfile.mkdtemp(prefix="ccbr_selftest_"))
    old_db, old_env, old_snap = P.DB_PATH, dict(os.environ), P.SNAPSHOT_DIR
    old_handlers, old_prop = list(P.log.handlers), P.log.propagate
    from scripts import discord_post as D
    old_home = D.HERMES_HOME
    try:
        # Never touch the real db_snapshots/ (trimmed to 5 — a test snapshot would
        # evict a real rollback copy) or the real skill.log audit trail.
        P.SNAPSHOT_DIR = tmp / "db_snapshots"
        import logging
        P.log.handlers = [logging.FileHandler(tmp / "skill.log")]
        P.log.propagate = False
        db = tmp / "cc_review.db"
        if seed_db and Path(old_db).exists():
            shutil.copy(old_db, db)
        P.DB_PATH = db
        (tmp / "cron").mkdir()
        (tmp / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": P.CRON_JOB_ID, "name": P.CRON_JOB_NAME, "enabled": True,
            "state": "scheduled", "schedule": {"expr": "0 9 * * *"},
            "last_run_at": datetime.now().astimezone().isoformat(),
            "next_run_at": (datetime.now().astimezone() + timedelta(days=1)).isoformat(),
            "last_status": "ok", "failure_streak": 0}]}))
        (tmp / ".env").write_text("DISCORD_BOT_TOKEN=fake.token.value\n")
        D.HERMES_HOME = str(tmp)
        os.environ["HERMES_HOME"] = str(tmp)
        P.init_db().close()
        yield tmp
    finally:
        P.DB_PATH = old_db
        P.SNAPSHOT_DIR = old_snap
        for h in P.log.handlers:
            h.close()
        P.log.handlers, P.log.propagate = old_handlers, old_prop
        D.HERMES_HOME = old_home
        os.environ.clear()
        os.environ.update(old_env)
        shutil.rmtree(tmp, ignore_errors=True)


class FakeDiscord:
    """Replaces urllib.request.urlopen inside discord_post; scripted responses."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.posts = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.posts.append({"url": req.full_url, "body": body})
        step = self.script.pop(0) if self.script else 200
        if step == "timeout":
            raise TimeoutError("timed out")
        if step == "nonjson":
            return _Resp(b"<html>ok</html>")
        if isinstance(step, int) and step >= 400:
            raise urllib.error.HTTPError(req.full_url, step, "err", {},
                                         io.BytesIO(b'{"code": 0, "retry_after": 0.01}'))
        return _Resp(json.dumps({"id": str(_snowflake_now() + len(self.posts))}).encode())


def _snowflake_now() -> int:
    import time as _t
    return (int(_t.time() * 1000) - 1420070400000) << 22


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_all(t) -> None:
    """t = the SelfTest instance (assert_eq/_ok/_fail)."""
    print("--- v3.5 regressions (temp DB, stubbed Discord) ---")
    _grammar(t)
    _splitter_and_target(t)
    _send_policy(t)
    _reminder_windows(t)
    _cron_health(t)
    _parser_identity(t)
    _import_flow(t)
    _coverage(t)
    _lap2(t)
    _wrapper(t)
    _v351(t)


def _grammar(t):
    from scripts.approve import Refused, parse_command
    ok = {
        "approve #18 #53": ("approve", "ids", [18, 53]),
        "Approve all": ("approve", "all", []),
        "approve all new merchants": ("approve", "all", []),
        "approve all backlog": ("approve", "backlog", []),
        "approve 18, 19 and 20": ("approve", "ids", [18, 19, 20]),
        "approve #9-#12": ("approve", "ids", [9, 10, 11, 12]),
        "ok approve `#95` `#103`": ("approve", "ids", [95, 103]),
        "reject #12": ("reject", "ids", [12]),
        "unapprove all": ("unapprove", "all", []),
    }
    for text, (verb, mode, ids) in ok.items():
        try:
            c = parse_command(text)
            t.assert_eq(f"grammar {text!r}", (c["verb"], c["mode"], c["ids"]), (verb, mode, ids))
        except Refused as why:
            t._fail(f"grammar {text!r} refused: {why}")
    # the user's 7 real replies (May–Sep 2026) must keep working exactly as meant
    for text, mode, only_new in (("approve all", "all", False), ("Approve all", "all", False),
                                 ("approve all backlog", "backlog", False),
                                 ("approve all new merchants", "all", True),
                                 ("approve all merchants", "all", False)):
        c = parse_command(text)
        t.assert_eq(f"real reply {text!r}", (c["verb"], c["mode"], c["only_new"]),
                    ("approve", mode, only_new))
    c = parse_command("approve all except #18 #20")
    t.assert_eq("grammar: all except", (c["mode"], c["exclude"]), ("all", [18, 20]))
    for text in ("don't approve #18", "not #18, approve the rest", "approve #18 and hide #99",
                 "approve #96, that's all", "what does approve all do?",
                 "approve the LTA charge on 08-27", "approve DAPUR PENYET S3.50",
                 "hide all", "approve all except the grab one", "#18 looks wrong"):
        try:
            parse_command(text)
            t._fail(f"grammar must refuse {text!r}")
        except Refused:
            t._ok(f"grammar refuses {text!r}")
    from scripts.approve import _TARGET
    t.assert_eq("reject keeps flagged (0), never hides", _TARGET["reject"], 0)


def _splitter_and_target(t):
    saved = dict(os.environ)
    try:
        _splitter_and_target_inner(t)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _splitter_and_target_inner(t):
    from scripts import discord_post as D
    body = "\n\n".join(
        "### 💳 5999 - Sep 2026\n" + "\n".join(f"  `#{i}` 🆕 SHOP {i} SINGAPORE | 1x | S$1.00"
                                             for i in range(n)) for n in (40, 30, 5))
    chunks = D.split_message(body)
    t.assert_eq("split: every chunk <= 2000 UTF-16 units",
                all(D.utf16_len(c) <= 2000 for c in chunks), True)
    t.assert_eq("split: lossless", "".join("".join(chunks).split()), "".join(body.split()))
    t.assert_eq("split: short body is one message", D.split_message("hi"), ["hi"])
    t.assert_eq("split: empty -> nothing", D.split_message("  \n "), [])
    os.environ["HERMES_SESSION_KEY"] = "agent:main:discord:thread:1545595890545467392:1545595890545467392"
    t.assert_eq("target: session thread", D.resolve_target(None)[0], "1545595890545467392")
    t.assert_eq("target: session wins over a message id",
                D.resolve_target("1545596543107600454")[0], "1545595890545467392")
    os.environ["HERMES_SESSION_KEY"] = "agent:main:discord:group:1499951469950926999:470611394698412052"
    t.assert_eq("target: group key -> channel, never the user id",
                D.resolve_target(None)[0], "1499951469950926999")
    os.environ.pop("HERMES_SESSION_KEY")
    os.environ.pop("HERMES_SESSION_THREAD_ID", None)
    os.environ.pop("HERMES_SESSION_CHAT_ID", None)
    t.assert_eq("target: no session + no id -> refuse", D.resolve_target(None)[0], None)
    t.assert_eq("target: explicit id without session", D.resolve_target("1499972245437550602")[0],
                "1499972245437550602")


def _send_policy(t):
    from scripts import discord_post as D
    real_urlopen = D.urllib.request.urlopen
    with sandbox(seed_db=False):
        real, D.time.sleep = D.time.sleep, (lambda s: None)
        try:
            for script, nonce, expect_ok, expect_posts in (
                    ([200], "n1", True, 1), (["nonjson"], "n1", True, 1),
                    ([500, 200], "n1", True, 2), (["timeout", 200], "n1", True, 2),
                    (["timeout"], None, False, 1), ([404], "n1", False, 1),
                    ([429, 200], None, True, 2)):
                fake = FakeDiscord(script)
                D.urllib.request.urlopen = fake
                ok, detail, _mid, _may = D.send_discord("1499972245437550602", "x", nonce=nonce)
                t.assert_eq(f"send {script} nonce={bool(nonce)} -> (ok, posts)",
                            (ok, len(fake.posts)), (expect_ok, expect_posts))
            fake = FakeDiscord([200])
            D.urllib.request.urlopen = fake
            D.send_discord("1", "x", nonce="abc")
            b = fake.posts[0]["body"]
            t.assert_eq("send: nonce + enforce_nonce + no pings",
                        (b.get("nonce"), b.get("enforce_nonce"), b.get("allowed_mentions")),
                        ("abc", True, {"parse": []}))
            (Path(D.HERMES_HOME) / ".env").write_text("# no token\n")
            ok, detail, _m, _p = D.send_discord("1", "x")
            t.assert_eq("send: missing token returns, never raises", ok, False)
        finally:
            D.time.sleep = real
            D.urllib.request.urlopen = real_urlopen


def _reminder_windows(t):
    from scripts import reminder as R
    with sandbox(seed_db=False):
        conn = P.get_db()
        with conn:
            conn.execute("INSERT INTO statements (card_suffix, year_month, statement_date, "
                         "due_date, total_due) VALUES ('5999','2026-10','01-10-2026','24-10-2026',500)")
            conn.execute("INSERT INTO statements (card_suffix, year_month, statement_date, "
                         "due_date, total_due) VALUES ('7008','2026-10','01-10-2026','24-10-2026',0)")
        conn.close()
        due = date(2026, 10, 24)
        got = {}
        for off in range(-4, 5):
            got[off] = [(i["card"], i["kind"]) for i in R.due_messages(due - timedelta(days=off))]
        t.assert_eq("reminder: 3-day window = due-3..due-1, due window = due..due+2",
                    {k: v for k, v in got.items() if v},
                    {3: [("5999", "3day")], 2: [("5999", "3day")], 1: [("5999", "3day")],
                     0: [("5999", "due")], -1: [("5999", "due")], -2: [("5999", "due")]})
        R.mark_fired(R.due_messages(due - timedelta(days=3)))
        t.assert_eq("reminder: 3-day fires once per cycle",
                    R.due_messages(due - timedelta(days=2)), [])
        over = R.due_messages(due + timedelta(days=1))
        t.assert_eq("reminder: missed due date -> OVERDUE text",
                    "OVERDUE" in (over[0]["text"] if over else ""), True)
        t.assert_eq("reminder: S$0.00 statement never pings",
                    [i for i in R.due_messages(due) if i["card"] == "7008"], [])
        soon = date.today() + timedelta(days=3)
        conn = P.get_db()
        with conn:
            conn.execute("INSERT INTO statements (card_suffix, year_month, statement_date, "
                         "due_date, total_due) VALUES ('1111','2099-01','01-01-2099',?,10)",
                         (soon.strftime("%d-%m-%Y"),))
        conn.close()
        t.assert_eq("reminder CLI premise: something is due today",
                    any(i["card"] == "1111" for i in R.due_messages()), True)
        R.main(["reminder.py"])
        t.assert_eq("reminder CLI default does not mark fired",
                    P.get_config("reminded_3day_1111"), None)
        R.main(["reminder.py", "--mark-fired"])
        t.assert_eq("reminder CLI --mark-fired marks", P.get_config("reminded_3day_1111"), "2099-01")


def _cron_health(t):
    with sandbox(seed_db=False) as tmp:
        jobs = tmp / "cron" / "jobs.json"
        base = {"id": "7b68c1ef7afe", "name": P.CRON_JOB_NAME, "enabled": True,
                "state": "scheduled", "schedule": {"expr": "0 9 * * *"},
                "last_run_at": datetime.now().astimezone().isoformat(), "last_status": "ok",
                "next_run_at": (datetime.now().astimezone() + timedelta(days=1)).isoformat(),
                "failure_streak": 0}
        P.set_config("cron_job_id", "7b68c1ef7afe")
        for label, patch, healthy in (
                ("healthy job", {}, True),
                ("last run failed", {"last_status": "error", "failure_streak": 30,
                                     "last_error": "Script exited with code 1"}, False),
                ("stalled (no run for 3 days)",
                 {"last_run_at": (datetime.now().astimezone() - timedelta(days=3)).isoformat()},
                 False),
                ("paused", {"enabled": False, "state": "paused"}, False),
                ("state error, no next run", {"state": "error", "next_run_at": None}, False),
                ("failure_streak as a string", {"failure_streak": "0"}, True),
                ("tz-naive last run 10 days ago",
                 {"last_run_at": (datetime.now() - timedelta(days=10)).isoformat()}, False)):
            jobs.write_text(json.dumps({"jobs": [{**base, **patch}]}))
            h = P.verify_cron_health()
            t.assert_eq(f"cron health: {label}", bool(h["healthy"]), healthy)
        jobs.write_text(json.dumps({"jobs": []}))
        h = P.verify_cron_health()
        t.assert_eq("cron health: readable but empty jobs.json = job absent (not 'unknown')",
                    (h["determinable"], h["found"]), (True, False))


def _parser_identity(t):
    t.assert_eq("FX amount not part of the identity",
                P.normalize_merchant("-2030 NANONOBLE PTE. LTD. FOREIGN CURRENCY USD 10.90 SINGAPORE",
                                     "5999"),
                P.normalize_merchant("-2030 NANONOBLE PTE. LTD. SINGAPORE", "5999"))
    t.assert_eq("holiday canonical unchanged by FX strip",
                P.normalize_merchant("-9113 TPER SPA FOREIGN CURRENCY EUR 2.30 BOLOGNA", "5999"),
                "TPER SPA")
    t.assert_eq("MiMo canonical unchanged by FX strip",
                P.normalize_merchant("-6232 W  *XIAOMI MIMO FOREIGN CURRENCY USD 4.62 PLATFORM.XIAO",
                                     "5999"), "XIAOMI MIMO API")
    txt = synth_statement("7008", "01-10-2026", "24-10-2026", [
        ("02/09", "29.76", "SHOPEE SG MP             SINGAPORE    SG", "")], 29.76)
    d = P.parse_statement(txt, "7008", "2026-10")
    t.assert_eq("glued statement total stripped from the last txn",
                d["transactions"][-1]["merchant_raw"], "SHOPEE SG MP             SINGAPORE    SG")
    t.assert_eq("column-layout country captured", d["transactions"][-1]["country_code"], "SG")
    txt = synth_statement("7008", "01-10-2026", "24-10-2026", [
        ("03/09", "120.00", "AMAZON MKTPLACE      SEATTLE      US", "")], 120.0)
    t.assert_eq("column-layout foreign country captured",
                P.parse_statement(txt, "7008", "2026-10")["transactions"][0]["country_code"], "US")


def _import_flow(t):
    """process_pdf + review + approve end to end on a temp DB, PDF text stubbed."""
    from scripts import approve, import_and_review, reviewlib
    from scripts import discord_post as D
    import subprocess
    real_urlopen = D.urllib.request.urlopen
    with sandbox(seed_db=True) as tmp:
        texts = {}
        real_read, real_run = P.read_pdf_text, subprocess.run
        real_avail = import_and_review.pdf_reader_available
        import_and_review.pdf_reader_available = lambda: True
        P.read_pdf_text = lambda p: texts[p]
        subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess"))
        real_sleep, D.time.sleep = D.time.sleep, (lambda s: None)
        fake = FakeDiscord()
        D.urllib.request.urlopen = fake
        os.environ["HERMES_SESSION_KEY"] = "agent:main:discord:thread:1600000000000000001:1600000000000000001"
        try:
            prev = P.get_db().execute("SELECT total_due FROM statements WHERE card_suffix='5999' "
                                      "AND year_month='2026-09'").fetchone()[0]
            tx = [("05/09", f"({prev:.2f})", "PAYMENT BY INTERNET", ""),
                  ("06/09", "450.00", "BEST DENKI\nSINGAPORE", "SG"),
                  ("06/09", "450.00", "BEST DENKI\nSINGAPORE", "SG"),
                  ("07/09", "12.00", "NEW SHOP ONE\nSINGAPORE", "SG")]
            total = round(prev - prev + 450 + 450 + 12, 2)
            texts["/fake/a.pdf"] = synth_statement("5999", "01-10-2026", "24-10-2026", tx, total)
            r = P.process_pdf("/fake/a.pdf")
            t.assert_eq("import: 4 txns, no reconciliation warning",
                        (len(r["transactions"]),
                         any("don't add up" in w for w in r.get("warnings") or [])), (4, False))
            bad = synth_statement("5999", "01-10-2026", "24-10-2026", tx[:3], total)
            texts["/fake/b.pdf"] = bad
            r2 = P.process_pdf("/fake/b.pdf")
            t.assert_eq("import: a missing transaction trips the reconciliation warning",
                        any("don't add up" in w for w in r2.get("warnings") or []), True)
            P.process_pdf("/fake/a.pdf")
            cur = P.get_db().cursor()
            hv = reviewlib.high_value(cur, "5999", "2026-10")
            t.assert_eq("review: a same-day double charge shows twice", len(hv), 2)
            fake.posts.clear()
            rc = import_and_review.main(["x", "--post", "1545596543107600454", "/fake/a.pdf"])
            t.assert_eq("import_and_review --post -> rc 0, posted to the SESSION thread",
                        (rc, {p["url"].split("/")[-2] for p in fake.posts}),
                        (0, {"1600000000000000001"}))
            shown = reviewlib.last_shown(P.get_db())
            t.assert_eq("review records what it showed", bool(shown and shown["merchants"]), True)
            new_uids = sorted(m["uid"] for m in shown["merchants"])
            conn = P.get_db()
            # premise: an unapproved merchant on the OTHER card's latest statement,
            # which this review did NOT show (only 5999 was uploaded)
            other = conn.execute(
                "SELECT m.merchant_uid FROM merchants m JOIN transactions t ON "
                "t.card_suffix=m.card_suffix AND t.merchant_normalized=m.merchant_normalized "
                "WHERE m.card_suffix='7008' AND t.year_month='2026-09' AND m.approved=1 "
                "AND t.amount > 0 AND m.merchant_normalized NOT LIKE '%PAYMENT%' "
                "LIMIT 1").fetchone()[0]
            with conn:
                conn.execute("UPDATE merchants SET approved=0 WHERE merchant_uid=?", (other,))
            res = approve.apply(conn, approve.parse_command(f"approve all except #{new_uids[0]}"))
            t.assert_eq("approve all except: excluded id untouched",
                        conn.execute("SELECT approved FROM merchants WHERE merchant_uid=?",
                                     (new_uids[0],)).fetchone()[0], 0)
            t.assert_eq("approve all: only shown ids changed",
                        sorted(u for u, _c, _n in res["changed"]) == new_uids[1:], True)
            t.assert_eq("approve all: a merchant the user wasn't shown stays unapproved",
                        conn.execute("SELECT approved FROM merchants WHERE merchant_uid=?",
                                     (other,)).fetchone()[0], 0)
            hol = conn.execute("SELECT merchant_uid FROM merchants WHERE merchant_normalized="
                               "'TPER SPA'").fetchone()
            if hol:
                res = approve.apply(conn, approve.parse_command(f"approve #{hol[0]}"))
                t.assert_eq("holiday merchant not approvable", res["changed"], [])
            top = conn.execute("SELECT MAX(merchant_uid) FROM merchants").fetchone()[0]
            with conn:
                conn.execute("DELETE FROM merchants WHERE merchant_uid=?", (top,))
            texts["/fake/c.pdf"] = synth_statement("7008", "01-10-2026", "24-10-2026", [
                ("08/09", "9.00", "BRAND NEW MERCHANT\nSINGAPORE", "SG")], 9.0)
            P.process_pdf("/fake/c.pdf")
            newest = conn.execute("SELECT MAX(merchant_uid) FROM merchants").fetchone()[0]
            t.assert_eq("merchant ids are never reused after a delete", newest > top, True)
            conn.close()
        finally:
            P.read_pdf_text, subprocess.run = real_read, real_run
            import_and_review.pdf_reader_available = real_avail
            D.time.sleep = real_sleep
            D.urllib.request.urlopen = real_urlopen


def _ins_stmt(conn, card, ym, stmt, due, total):
    conn.execute("INSERT INTO statements (card_suffix, year_month, statement_date, due_date, "
                 "total_due) VALUES (?,?,?,?,?)", (card, ym, stmt, due, total))


def _ins_txn(conn, card, ym, d, raw, norm, amt, cc="SG", rn=0, approved=0):
    conn.execute("INSERT INTO transactions (card_suffix, year_month, txn_date, merchant_raw, "
                 "merchant_normalized, country_code, amount, row_num) VALUES (?,?,?,?,?,?,?,?)",
                 (card, ym, d, raw, norm, cc, amt, rn))
    conn.execute("INSERT INTO merchants (card_suffix, merchant_normalized, approved, txn_count, "
                 "total_spent) VALUES (?,?,?,1,?) ON CONFLICT DO NOTHING",
                 (card, norm, approved, amt))


def _coverage(t):
    """One check per fix that the lap-1 mutation run found unprotected."""
    from scripts import approve, import_and_review, reminder, review, reviewlib
    from scripts import discord_post as D
    from scripts import find_duplicate_merchants as F
    from scripts.approve import Refused, parse_command
    with sandbox(seed_db=False) as tmp:
        conn = P.get_db()
        with conn:
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "24-10-2026", 500)
            _ins_txn(conn, "5999", "2026-10", "2026-09-03",
                     "-2030 NANONOBLE PTE. LTD. FOREIGN CURRENCY USD 10.00 SINGAPORE",
                     "NANONOBLE PTE. LTD. SINGAPORE", 13.15)
            _ins_txn(conn, "5999", "2026-10", "2026-09-04", "SPORTPERFORMANCE LAB MILANO",
                     "SPORTPERFORMANCE LAB MILANO", 40.0, cc="IT")
            _ins_txn(conn, "5999", "2026-10", "2026-09-05", "TPER SPA BOLOGNA", "TPER SPA",
                     3.58, cc="IT", approved=-1)
            _ins_txn(conn, "5999", "2026-10", "2026-09-06", "ORPHAN SHOP", "ORPHAN SHOP", 5.0)
            conn.execute("DELETE FROM merchants WHERE merchant_normalized='ORPHAN SHOP'")
            conn.execute("INSERT INTO merchants (card_suffix, merchant_normalized, approved, "
                         "txn_count, total_spent) VALUES ('5999','GONE SHOP',0,0,0)")
        P._assign_merchant_uids(conn)
        conn.commit()
        cur = conn.cursor()
        fg = {r[1] for r in reviewlib.foreign(cur, "5999", "2026-10")}
        t.assert_eq("foreign: SG-tagged USD charge flagged via merchant_raw",
                    "NANONOBLE PTE. LTD. SINGAPORE" in fg, True)
        t.assert_eq("holiday exclusion is exact-name (SPORTPERFORMANCE stays foreign)",
                    ("SPORTPERFORMANCE LAB MILANO" in fg, "TPER SPA" in fg), (True, False))
        lines = "\n".join(reviewlib.render_statement(cur, "5999", "2026-10", "01-10-2026",
                                                     "24-10-2026", 500))
        t.assert_eq("foreign line shows the FX detail", "SG · USD 10.00" in lines, True)
        t.assert_eq("LEFT JOIN: a txn with no merchants row still listed (#?)",
                    "`#?` ORPHAN SHOP" in lines, True)
        t.assert_eq("backlog skips merchants with no rows left",
                    any(r["merchant_normalized"] == "GONE SHOP"
                        for r in reviewlib.all_unapproved(cur)), False)
        t.assert_eq("esc_md defangs masked links / autolinks / mentions / spoilers",
                    reviewlib.esc_md("[X](HTTPS://A) <@1> ||S||"),
                    "[X]\u200b(HTTPS:\u200b//A) <\u200b@1> |\u200b|S|\u200b|")
        t.assert_eq("dup sweep STOP words apply (PTE/LTD/SINGAPORE ignored)",
                    F.tokens("TIONG BAHRU BAKERY PTE LTD SINGAPORE"), ["tiong", "bahru", "bakery"])

        # --- "last shown" state machine
        shown = [{"uid": 1, "new": True}, {"uid": 2, "new": False}]
        reviewlib.record_shown(conn, shown, "latest review", "111111111111111111")
        c = parse_command("approve all new")
        ids, _ = approve._resolve_all(cur, conn, c, "111111111111111111")
        t.assert_eq("approve all new = only the new ones of a review list", ids, [1])
        reviewlib.record_shown(conn, [{"uid": 1, "new": None}, {"uid": 2, "new": None}],
                               "merchant", "111111111111111111")
        ids, _ = approve._resolve_all(cur, conn, c, "111111111111111111")
        t.assert_eq("approve all new after the merchant list = the whole list", ids, [1, 2])
        for label, setup, target in (
                ("blocked (post failed)", lambda: reviewlib.record_shown(
                    conn, [], "review", "111111111111111111", blocked="post failed"),
                 "111111111111111111"),
                ("other thread", lambda: reviewlib.record_shown(
                    conn, shown, "review", "222222222222222222"), "111111111111111111"),
                ("stale (20 days)", lambda: conn.execute(
                    "UPDATE config SET value=? WHERE key=?",
                    (json.dumps({"source": "review", "target": "111111111111111111",
                                 "at": (datetime.now() - timedelta(days=20)).isoformat(),
                                 "merchants": shown}), reviewlib.LAST_SHOWN_KEY)),
                 "111111111111111111"),
                ("malformed record", lambda: conn.execute(
                    "UPDATE config SET value='{\"merchants\": [101]}' WHERE key=?",
                    (reviewlib.LAST_SHOWN_KEY,)), "111111111111111111")):
            setup()
            conn.commit()
            try:
                approve._resolve_all(cur, conn, parse_command("approve all"), target)
                t._fail(f"approve all must refuse: {label}")
            except Refused:
                t._ok(f"approve all refuses: {label}")
        reviewlib.record_shown(conn, shown, "review", "111111111111111111")
        for text in ("approve all backlog new", "approve all except 7008", "approve all except #999"):
            try:
                c = parse_command(text)
                approve._resolve_all(cur, conn, c, "111111111111111111")
                t._fail(f"must refuse {text!r}")
            except Refused:
                t._ok(f"refuses {text!r}")
        first = reviewlib.all_unapproved(cur)[0]["merchant_uid"]
        c = parse_command(f"approve all backlog except #{first}")
        ids, _ = approve._resolve_all(cur, conn, c, None)
        t.assert_eq("backlog except: excluded id left out (premise: it was in the backlog)",
                    (first in ids, bool(ids) or True), (False, True))
        for text in ("[ham] Approve all", "ok, approve all", "approve #18 thanks",
                     "approve them all", "approve #18 to #20", "approve #18—#20"):
            try:
                parse_command(text)
                t._ok(f"accepts {text!r}")
            except Refused as why:
                t._fail(f"should accept {text!r}: {why}")
        with conn:
            conn.execute("UPDATE merchants SET approved=-1 WHERE merchant_uid=2")
        res = approve.apply(conn, parse_command("approve all"), "111111111111111111")
        t.assert_eq("approve all never un-hides a hidden merchant",
                    conn.execute("SELECT approved FROM merchants WHERE merchant_uid=2"
                                 ).fetchone()[0], -1)
        hol = conn.execute("SELECT merchant_uid FROM merchants WHERE merchant_normalized="
                           "'TPER SPA'").fetchone()[0]
        res = approve.apply(conn, parse_command(f"unapprove #{hol}"))
        t.assert_eq("holiday merchants can't be changed at all", res["changed"], [])

        # --- print-only review.py doesn't record; failed post blocks
        reviewlib.record_shown(conn, shown, "review", "111111111111111111")
        import sys as _s, contextlib as _cl
        real_argv = _s.argv
        try:
            _s.argv = ["review.py", "--merchants-only"]
            with _cl.redirect_stdout(io.StringIO()):
                review.main()
            t.assert_eq("print-only review.py does not change the shown record",
                        reviewlib.last_shown(conn)["merchants"], shown)
            os.environ["HERMES_SESSION_KEY"] = ("agent:main:discord:thread:"
                                                "111111111111111111:111111111111111111")
            real_open = D.urllib.request.urlopen
            D.urllib.request.urlopen = FakeDiscord([403])
            _s.argv = ["review.py", "--merchants-only", "--post"]
            with _cl.redirect_stdout(io.StringIO()):
                rc = review.main()
            D.urllib.request.urlopen = real_open
            t.assert_eq("failed post -> rc 3 and the record is blocked",
                        (rc, bool(reviewlib.last_shown(conn).get("blocked"))), (3, True))
            os.environ.pop("HERMES_SESSION_KEY")
            pending = conn.execute("SELECT merchant_uid FROM merchants WHERE approved=0 "
                                   "AND merchant_uid IS NOT NULL LIMIT 1").fetchone()[0]
            out = io.StringIO()
            with _cl.redirect_stdout(out):
                rc = approve.main(["approve.py", "--post", f"approve #{pending}"])
            t.assert_eq("approve --post with nowhere to post: rc 2 and nothing changed",
                        (rc, conn.execute("SELECT approved FROM merchants WHERE merchant_uid=?",
                                          (pending,)).fetchone()[0]), (2, 0))
            real_avail = import_and_review.pdf_reader_available
            import_and_review.pdf_reader_available = lambda: False
            with _cl.redirect_stdout(io.StringIO()):
                rc = import_and_review.main(["x", "/fake.pdf"])
            import_and_review.pdf_reader_available = real_avail
            t.assert_eq("no pymupdf -> exit 4 before importing anything", rc, 4)
        finally:
            _s.argv = real_argv

        # --- nonces: interactive posts salted; dedupe detection
        fake = FakeDiscord()
        real_open, D.urllib.request.urlopen = D.urllib.request.urlopen, fake
        real_sleep, D.time.sleep = D.time.sleep, (lambda s: None)
        try:
            os.environ["HERMES_SESSION_KEY"] = ("agent:main:discord:thread:"
                                                "111111111111111111:111111111111111111")
            with _cl.redirect_stdout(io.StringIO()):
                approve.main(["approve.py", "--post", "approve #999999"])
                approve.main(["approve.py", "--post", "approve #999999"])
            bodies = [p["body"]["content"] for p in fake.posts]
            n = [p["body"]["nonce"] for p in fake.posts]
            t.assert_eq("a repeated approve reply posts again (premise: identical bodies)",
                        (len(bodies) == 2 and bodies[0] == bodies[1], len(set(n))), (True, 2))
            fake.posts.clear()
            real_argv2 = _s.argv
            _s.argv = ["review.py", "--merchants-only", "--post"]
            with _cl.redirect_stdout(io.StringIO()):
                review.main()
            _s.argv = real_argv2
            rec = reviewlib.last_shown(conn)
            t.assert_eq("a posted merchant list is recorded without new/seen flags",
                        (bool(rec["merchants"]), {m["new"] for m in rec["merchants"]}),
                        (True, {None}))

            class OldId(FakeDiscord):
                def __call__(self, req, timeout=None):
                    self.posts.append(1)
                    return _Resp(b'{"id": "1000000000000000000"}')   # a 2016 snowflake
            D.urllib.request.urlopen = OldId()
            r = D.post_messages("111111111111111111", "hello", seed="x")
            t.assert_eq("an existing (deduplicated) message is reported", r["already_there"], 1)
            D.urllib.request.urlopen = FakeDiscord([429])

            class Slow(FakeDiscord):
                def __call__(self, req, timeout=None):
                    self.posts.append(1)
                    raise urllib.error.HTTPError(req.full_url, 429, "x", {},
                                                 io.BytesIO(b'{"retry_after": 60}'))
            slow = Slow()
            D.urllib.request.urlopen = slow
            ok, *_ = D.send_discord("1", "x", nonce="n")
            t.assert_eq("a long 429 fails fast (1 attempt, no 60 s sleep)",
                        (ok, len(slow.posts)), (False, 1))
        finally:
            D.urllib.request.urlopen, D.time.sleep = real_open, real_sleep
            os.environ.pop("HERMES_SESSION_KEY", None)

        # --- reminder confirmation mirrors the cron (S$0 skip, fired skip, late catch-up)
        with conn:
            conn.execute("DELETE FROM statements")
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "24-10-2026", 500)
            _ins_stmt(conn, "7008", "2026-10", "01-10-2026", "24-10-2026", 0)
        up = reminder.upcoming(datetime(2026, 10, 22, 10, 0))
        t.assert_eq("confirmation: S$0 card absent; late 3-day + due listed",
                    [(e["card"], e["kind"], str(e["fire_date"]), e["late"]) for e in up],
                    [("5999", "3day", "2026-10-23", True), ("5999", "due", "2026-10-24", False)])
        P.set_config("reminded_3day_5999", "2026-10")
        t.assert_eq("confirmation: an already-sent reminder isn't listed",
                    [e["kind"] for e in reminder.upcoming(datetime(2026, 10, 22, 10, 0))], ["due"])
        conn.close()

    # --- import paths: aggregates refresh, reminders_resumed, interrupt, date window
    with sandbox(seed_db=False):
        texts = {}
        real_read = P.read_pdf_text
        P.read_pdf_text = lambda p: (_ for _ in ()).throw(KeyboardInterrupt()) \
            if p == "/kb.pdf" else texts[p]
        try:
            texts["/a.pdf"] = synth_statement("5999", "01-10-2026", "24-10-2026",
                                              [("02/09", "10.00", "OLD NAME SHOP", "SG")], 10.0)
            texts["/b.pdf"] = synth_statement("5999", "01-10-2026", "24-10-2026",
                                              [("02/09", "10.00", "NEW NAME SHOP", "SG")], 10.0)
            P.process_pdf("/a.pdf")
            P.process_pdf("/b.pdf")
            row = P.get_db().execute("SELECT txn_count FROM merchants WHERE "
                                     "merchant_normalized='OLD NAME SHOP'").fetchone()
            t.assert_eq("re-import refreshes a merchant that lost its rows", row[0], 0)
            P.set_config("reminders_disabled", "1")
            texts["/c.pdf"] = synth_statement("5999", "01-11-2026", "24-11-2026",
                                              [("02/10", "10.00", "NEW NAME SHOP", "SG")], 20.0)
            r = P.process_pdf("/c.pdf")
            from scripts.import_response import format_import_summary
            t.assert_eq("a new cycle that undoes a manual pause says so",
                        (r["reminders_resumed"], "back on" in format_import_summary([r])),
                        (True, True))
            texts["/d.pdf"] = synth_statement("5999", "01-12-2026", "24-12-2026",
                                              [("02/11", "10.00", "NEW NAME SHOP", "SG"),
                                               ("03/01/19", "5.00", "ANCIENT SHOP", "SG")], 35.0)
            r = P.process_pdf("/d.pdf")
            t.assert_eq("date-window check runs (a 2019 date warns)",
                        any("outside the expected window" in w for w in r["warnings"]), True)
            try:
                P.process_pdf("/kb.pdf")
            except KeyboardInterrupt:
                pass
            st = P.get_db().execute("SELECT status FROM imports ORDER BY id DESC LIMIT 1"
                                    ).fetchone()[0]
            t.assert_eq("an interrupted import never stays 'in_progress'", st, "parse_error")
        finally:
            P.read_pdf_text = real_read

    # --- migration keeps an approval when folding
    with sandbox(seed_db=False):
        from scripts import migrate_identities as M
        conn = P.get_db()
        with conn:
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "24-10-2026", 99.0)
            _ins_txn(conn, "5999", "2026-10", "2026-09-02",
                     "-2030 NANO PTE. LTD. FOREIGN CURRENCY USD 1.00 SINGAPORE",
                     "NANO PTE. LTD. FOREIGN CURRENCY USD 1.00", 1.3, approved=1)
            _ins_txn(conn, "5999", "2026-10", "2026-09-03", "-2030 NANO PTE. LTD. SINGAPORE",
                     "NANO PTE. LTD. SINGAPORE", 2.0, approved=0)
        P._assign_merchant_uids(conn)
        conn.commit()
        conn.close()
        import contextlib as _cl
        with _cl.redirect_stdout(io.StringIO()):
            rc = M.main(["m", "--apply"])
        t.assert_eq("migration: folding keeps the approval (0 + 1 -> 1)",
                    (rc, P.get_db().execute("SELECT approved FROM merchants WHERE "
                                            "merchant_normalized='NANO PTE. LTD. SINGAPORE'"
                                            ).fetchone()[0]), (0, 1))
    txt = synth_statement("7008", "01-10-2026", "24-10-2026", [
        ("02/09", "5.00", "HUGGS COFFEE PTE  LT", "")], 5.0)
    t.assert_eq("a 2-space truncated name is not a country column",
                P.parse_statement(txt, "7008", "2026-10")["transactions"][0]["country_code"], "")


def _wrapper(t):
    """The cron wrapper (lives in ~/.hermes/scripts) on a sandbox skill copy."""
    import subprocess
    import sys as _s
    w = os.environ.get("CC_REVIEW_WRAPPER",
                       os.path.expanduser("~/.hermes/scripts/cc_payment_reminder.py"))
    if not os.path.exists(w):
        print(f"  (cron wrapper not found at {w} — skipped)")
        return
    skill = Path(__file__).resolve().parent.parent
    for case, day, want_rc, want_posts in (("ok", "2026-10-21", 0, 2),
                                           ("500x1", "2026-10-24", 0, 3),
                                           ("500x9", "2026-10-24", 1, 7)):
        tmp = Path(tempfile.mkdtemp(prefix="ccbr_wrap_"))
        try:
            sk = tmp / "skill"
            shutil.copytree(skill, sk, ignore=shutil.ignore_patterns("db_snapshots", "*.log*",
                                                                      "__pycache__", "*.bak*"))
            c = P.sqlite3.connect(sk / "scripts" / "cc_review.db")
            with c:
                c.execute("DELETE FROM statements")
                c.execute("DELETE FROM config WHERE key LIKE 'remind%'")
                for card in ("5999", "7008"):
                    c.execute("INSERT INTO statements (card_suffix, year_month, statement_date, "
                              "due_date, total_due) VALUES (?, '2099-10', '01-10-2026', "
                              "'24-10-2026', 100)", (card,))
            c.close()
            home = tmp / "hh"
            home.mkdir()
            (home / ".env").write_text("DISCORD_BOT_TOKEN=fake.token.value\n")
            wp = tmp / "wrapper.py"
            import re as _re
            src, n = _re.subn(r"^SKILL_SCRIPTS = .*$", f"SKILL_SCRIPTS = {str(sk / 'scripts')!r}",
                              open(w).read(), count=1, flags=_re.M)
            if n != 1:
                t._fail(f"cron wrapper test: no SKILL_SCRIPTS line in {w}")
                return
            wp.write_text(src)
            driver = f"""
import io, json, sys, urllib.request, urllib.error, datetime as _dt, time
posts = []
script = {{"ok": [], "500x1": [500], "500x9": [500] * 9}}[{case!r}]
class R:
    def __init__(s, d): s.d = d
    def read(s): return s.d
    def close(s): pass
    def __enter__(s): return s
    def __exit__(s, *a): return False
def fake(req, timeout=None):
    posts.append(json.loads(req.data))
    step = script.pop(0) if script else 200
    if step == 500:
        raise urllib.error.HTTPError(req.full_url, 500, "x", {{}}, io.BytesIO(b"{{}}"))
    return R(json.dumps({{"id": str(len(posts))}}).encode())
urllib.request.urlopen = fake
time.sleep = lambda s: None
class D(_dt.date):
    @classmethod
    def today(cls): return _dt.date.fromisoformat({day!r})
_dt.date = D
sys.argv = ["wrapper.py"]
try:
    exec(compile(open({str(wp)!r}).read(), "wrapper.py", "exec"), {{"__name__": "__main__"}})
except SystemExit as e:
    print("RESULT", e.code, len(posts))
    print("ALERT_HAS_TEXT", any("could not be sent" in p.get("content", "") and
                                "Payment due today" in p.get("content", "") for p in posts))
"""
            r = subprocess.run([_s.executable, "-c", driver], capture_output=True, text=True,
                               env=dict(os.environ, HOME=str(tmp), HERMES_HOME=str(home)),
                               timeout=60)
            got = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")]
            t.assert_eq(f"cron wrapper {case} on {day}: (rc, POSTs)",
                        tuple(map(int, got[0].split()[1:])) if got else r.stderr[-200:],
                        (want_rc, want_posts))
            c = P.sqlite3.connect(sk / "scripts" / "cc_review.db")
            marks = dict(c.execute("SELECT key, value FROM config WHERE key LIKE 'remind%'"))
            c.close()
            want_marks = {} if want_rc else {
                f"{'reminded_3day' if day.endswith('21') else 'reminders_fired'}_{cd}": "2099-10"
                for cd in ("5999", "7008")}
            t.assert_eq(f"cron wrapper {case}: sent reminders recorded, failed ones not",
                        marks, want_marks)
            if case == "500x9":
                t.assert_eq("cron wrapper failure alert carries the unsent reminder text",
                            "ALERT_HAS_TEXT True" in r.stdout, True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _lap2(t):
    """Checks for the holes gate lap 2 found in the lap-1 fixes."""
    import contextlib as _cl
    import sys as _s
    from scripts import approve, import_and_review, reminder, review, reviewlib
    from scripts import discord_post as D
    from scripts import migrate_identities as M
    from scripts.approve import Refused, parse_command
    A = "111111111111111111"
    B = "222222222222222222"
    key = lambda th: f"agent:main:discord:thread:{th}:{th}"
    for text in ("[System note: gateway restarted] approve all", "[ham] Approve all",
                 "[Triggering message id: `1` — use as message_id] [ham] approve all"):
        try:
            parse_command(text)
            t._ok(f"gateway prefix accepted: {text[:30]!r}")
        except Refused as why:
            t._fail(f"gateway prefix should be accepted {text!r}: {why}")
    try:
        parse_command("approve 105,106")
        t._fail("'approve 105,106' must be refused (amount-shaped)")
    except Refused as why:
        t.assert_eq("amount refusal tells the user how to write ids", "#105 #106" in str(why), True)
    for text in ("[don't] approve all", "approve 1,000", "reject all", "approve DAPUR S$13.50",
                 "[cancel] approve all", "[jk] approve #18", "[ham] [undo] approve all"):
        try:
            parse_command(text)
            t._fail(f"lap2 grammar must refuse {text!r}")
        except Refused:
            t._ok(f"lap2 grammar refuses {text!r}")
    t.assert_eq("hidden wins a merge (-1 + 1 -> -1)", M._merge_approval(-1, 1), -1)
    with sandbox(seed_db=True) as tmp:
        conn = P.get_db()
        cur = conn.cursor()
        uids = [r[0] for r in cur.execute("SELECT merchant_uid FROM merchants WHERE "
                                          "approved=1 LIMIT 3")]
        with conn:
            conn.executemany("UPDATE merchants SET approved=0 WHERE merchant_uid=?",
                             [(u,) for u in uids])
        shown = [{"uid": u, "new": True} for u in uids]
        # thread check also without --post
        reviewlib.record_shown(conn, shown, "latest review", A)
        os.environ["HERMES_SESSION_KEY"] = key(B)
        out = io.StringIO()
        with _cl.redirect_stdout(out):
            rc = approve.main(["approve.py", "approve all"])
        t.assert_eq("approve all from another thread is refused even without --post",
                    (rc, cur.execute(f"SELECT COUNT(*) FROM merchants WHERE approved=1 AND "
                                     f"merchant_uid IN ({','.join(map(str, uids))})"
                                     ).fetchone()[0]), (2, 0))
        # 14-day boundary (exact: 14 d + 1 h is too old)
        for days, ok in ((13, True), (14.05, False), (15, False)):
            with conn:
                conn.execute("UPDATE config SET value=? WHERE key=?", (json.dumps(
                    {"source": "review", "target": A, "merchants": shown,
                     "at": (datetime.now() - timedelta(days=days)).isoformat()}),
                    reviewlib.LAST_SHOWN_KEY))
            try:
                approve._resolve_all(cur, conn, parse_command("approve all"), A)
                t.assert_eq(f"a {days}-day-old list is accepted", ok, True)
            except Refused:
                t.assert_eq(f"a {days}-day-old list is refused", ok, False)
        # stdin marker only on its own; an unquoted range dash is a range
        old_in = _s.stdin
        _s.stdin = io.StringIO("approve #%d" % uids[0])
        with _cl.redirect_stdout(io.StringIO()):
            approve.main(["approve.py", "-"])
        _s.stdin = old_in
        t.assert_eq("approve.py - reads the reply from stdin",
                    cur.execute("SELECT approved FROM merchants WHERE merchant_uid=?",
                                (uids[0],)).fetchone()[0], 1)
        u0, u1, u2 = uids[0], uids[1], uids[2]
        with conn:
            conn.executemany("UPDATE merchants SET approved=0 WHERE merchant_uid=?",
                             [(u,) for u in uids])
        os.environ["HERMES_SESSION_KEY"] = key(A)
        lo, hi = min(uids), max(uids)
        old_in, _s.stdin = _s.stdin, io.StringIO("")   # a stdin read must not block
        try:
            with _cl.redirect_stdout(io.StringIO()):
                approve.main(["approve.py", "approve", str(lo), "-", str(hi)])
        finally:
            _s.stdin = old_in
        got = {r[0] for r in cur.execute(
            f"SELECT merchant_uid FROM merchants WHERE approved=1 AND merchant_uid BETWEEN "
            f"{lo} AND {hi}")}
        t.assert_eq("an unquoted 'approve 105 - 107' (argv) approves the whole range",
                    set(uids) <= got, True)
        with conn:
            conn.execute("UPDATE config SET value=? WHERE key=?", (json.dumps(
                {"source": "review", "target": None, "merchants": shown,
                 "at": datetime.now().isoformat()}), reviewlib.LAST_SHOWN_KEY))
        try:
            approve._resolve_all(cur, conn, parse_command("approve all"), A)
            t._fail("a list record with no thread must not be used from a thread")
        except Refused:
            t._ok("a list record with no thread is refused from a thread")
        conn.close()

    # import_and_review: pending record, kill, deterministic nonce, crash guard, print-only
    with sandbox(seed_db=True) as tmp:
        texts = {"/a.pdf": synth_statement("5999", "01-10-2026", "24-10-2026",
                                           [("02/09", "60.00", "FRESH SHOP ONE", "SG"),
                                            ("03/09", "70.00", "FRESH SHOP TWO", "SG")], 130.0)}
        real_read, real_avail = P.read_pdf_text, import_and_review.pdf_reader_available
        real_open, real_sleep = D.urllib.request.urlopen, D.time.sleep
        P.read_pdf_text = lambda p: texts[p]
        import_and_review.pdf_reader_available = lambda: True
        D.time.sleep = lambda s: None
        try:
            os.environ["HERMES_SESSION_KEY"] = key(A)
            fake = FakeDiscord()
            D.urllib.request.urlopen = fake
            with _cl.redirect_stdout(io.StringIO()):
                rc = import_and_review.main(["x", "--post", "/a.pdf"])
            rec = reviewlib.last_shown(P.get_db())
            t.assert_eq("posted review recorded with its thread", (rc, rec.get("target")), (0, A))
            t.assert_eq("fresh posts are not counted as 'already there'",
                        D.post_messages(A, "hello", seed="fresh")["already_there"], 0)
            n1 = [p["body"]["nonce"] for p in fake.posts]
            fake.posts.clear()
            with _cl.redirect_stdout(io.StringIO()):
                import_and_review.main(["x", "--post", "/a.pdf"])
            n2 = [p["body"]["nonce"] for p in fake.posts]
            t.assert_eq("re-running the same import re-uses its nonces (Discord dedupes)",
                        n2 == n1[:len(n2)] and bool(n2), True)
            # killed mid-post -> the record says "in progress", never the previous list
            def killed(*a, **k):
                raise KeyboardInterrupt()
            real_pm = D.post_messages
            D.post_messages = killed
            try:
                with _cl.redirect_stdout(io.StringIO()):
                    import_and_review.main(["x", "--post", "/a.pdf"])
            except KeyboardInterrupt:
                pass
            D.post_messages = real_pm
            t.assert_eq("a post killed midway leaves a blocked record",
                        bool(reviewlib.last_shown(P.get_db()).get("blocked")), True)
            # failed post -> blocked
            D.urllib.request.urlopen = FakeDiscord([403] * 5)
            with _cl.redirect_stdout(io.StringIO()):
                rc = import_and_review.main(["x", "--post", "/a.pdf"])
            t.assert_eq("failed post -> rc 3 + blocked record",
                        (rc, bool(reviewlib.last_shown(P.get_db()).get("blocked"))), (3, True))
            # print-only inside a session -> blocked
            D.urllib.request.urlopen = FakeDiscord()
            with _cl.redirect_stdout(io.StringIO()):
                import_and_review.main(["x", "--post", "/a.pdf"])
            with _cl.redirect_stdout(io.StringIO()):
                import_and_review.main(["x", "/a.pdf"])
            t.assert_eq("a print-only run inside a session blocks 'approve all'",
                        bool(reviewlib.last_shown(P.get_db()).get("blocked")), True)
            # crash guard
            real_build = import_and_review.build_review
            import_and_review.build_review = lambda *a: 1 / 0
            out = io.StringIO()
            with _cl.redirect_stdout(out):
                rc = import_and_review.main(["x", "--post", "/a.pdf"])
            import_and_review.build_review = real_build
            t.assert_eq("a review-build crash -> rc 3, says nothing was posted",
                        (rc, "Nothing was posted" in out.getvalue()), (3, True))
            # two uploads in a row in one thread: "approve all" covers ONLY the latest
            # review, and never re-approves what the user just un-approved (lap 3)
            texts["/b.pdf"] = synth_statement("7008", "01-10-2026", "24-10-2026",
                                              [("04/09", "80.00", "FRESH SHOP THREE", "SG")], 80.0)
            with _cl.redirect_stdout(io.StringIO()):
                import_and_review.main(["x", "--post", "/a.pdf"])
                approve.main(["approve.py", "--post", "approve all"])
            one = P.get_db().execute("SELECT merchant_uid FROM merchants WHERE "
                                     "merchant_normalized='FRESH SHOP ONE'").fetchone()[0]
            with _cl.redirect_stdout(io.StringIO()):
                approve.main(["approve.py", "--post", f"unapprove #{one}"])
                import_and_review.main(["x", "--post", "/b.pdf"])
                approve.main(["approve.py", "--post", "approve all"])
            db = P.get_db()
            t.assert_eq("after a 2nd upload, approve all covers only the latest review "
                        "(an explicit un-approval of the 1st stays)",
                        (db.execute("SELECT approved FROM merchants WHERE merchant_uid=?",
                                    (one,)).fetchone()[0],
                         db.execute("SELECT approved FROM merchants WHERE merchant_normalized="
                                    "'FRESH SHOP THREE'").fetchone()[0]), (0, 1))
            # review.py: pending record before posting, print-only in a session blocks
            real_pm2 = D.post_messages
            D.post_messages = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
            real_argv = _s.argv
            _s.argv = ["review.py", "--merchants-only", "--post"]
            try:
                with _cl.redirect_stdout(io.StringIO()):
                    review.main()
            except KeyboardInterrupt:
                pass
            finally:
                D.post_messages = real_pm2
            t.assert_eq("review.py: a post killed midway leaves a blocked record",
                        bool(reviewlib.last_shown(P.get_db()).get("blocked")), True)
            with _cl.redirect_stdout(io.StringIO()):
                review.main()
            _s.argv = ["review.py", "--merchants-only"]
            with _cl.redirect_stdout(io.StringIO()):
                review.main()
            _s.argv = real_argv
            t.assert_eq("review.py: a print-only run inside a session blocks approve all",
                        bool(reviewlib.last_shown(P.get_db()).get("blocked")), True)
            # busy DB on the record write -> exit 3, never a crash / silent stale list
            import sqlite3 as _sq
            real_rs = reviewlib.record_shown
            reviewlib.record_shown = lambda *a, **k: (_ for _ in ()).throw(
                _sq.OperationalError("database is locked"))
            try:
                with _cl.redirect_stdout(io.StringIO()):
                    rc1 = import_and_review.main(["x", "--post", "/a.pdf"])
                    rc2 = import_and_review.main(["x", "/a.pdf"])
                _s.argv = ["review.py", "--merchants-only", "--post"]
                with _cl.redirect_stdout(io.StringIO()):
                    rc3 = review.main()
                _s.argv = ["review.py", "--merchants-only"]
                with _cl.redirect_stdout(io.StringIO()):
                    rc4 = review.main()
            finally:
                reviewlib.record_shown = real_rs
                _s.argv = real_argv
            t.assert_eq("busy DB on the shown-record write -> exit 3 on every path",
                        (rc1, rc2, rc3, rc4), (3, 3, 3, 3))
            real_apply = approve.apply
            approve.apply = lambda *a, **k: (_ for _ in ()).throw(
                _sq.OperationalError("database is locked"))
            out = io.StringIO()
            with _cl.redirect_stdout(out):
                rc = approve.main(["approve.py", "--post", "approve #1"])
            approve.apply = real_apply
            t.assert_eq("approve.py on a locked DB: exit 3 + 'Nothing changed'",
                        (rc, "Nothing changed" in out.getvalue()), (3, True))
            # posting deadline
            class Flaky(FakeDiscord):
                """each message: two timeouts (each costs the full timeout), then OK"""
                def __call__(self, req, timeout=None):
                    clock[0] += timeout
                    self.posts.append(1)
                    if len(self.posts) % 3:
                        raise TimeoutError("slow")
                    return _Resp(json.dumps({"id": str(_snowflake_now())}).encode())
            clock = [0.0]
            real_mono = D.time.monotonic
            D.time.monotonic = lambda: clock[0]
            D.time.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
            D.urllib.request.urlopen = Flaky()
            body = "\n\n".join(("line %d " % i) * 150 for i in range(8))
            r = D.post_messages(A, body, seed="slow", deadline_s=90)
            D.time.monotonic = real_mono
            t.assert_eq("an interactive post gives up within its deadline (premise: >2 messages)",
                        (r["ok"], clock[0] <= 95, r["total"] > 2), (False, True, True))
            class SlowOk(FakeDiscord):
                """answers after 19 s — like urllib, gives up at the timeout it's handed"""
                def __call__(self, req, timeout=None):
                    if timeout < 19:
                        clock[0] += timeout
                        raise TimeoutError("timed out")
                    clock[0] += 19
                    return _Resp(json.dumps({"id": str(_snowflake_now())}).encode())
            clock[0] = 0.0
            D.time.monotonic = lambda: clock[0]
            D.urllib.request.urlopen = SlowOk()
            r = D.post_messages(A, body, seed="slowok", deadline_s=90)
            D.time.monotonic = real_mono
            t.assert_eq("slow-but-OK messages also stop at the deadline (no 8 x 19 s)",
                        (r["ok"], clock[0] <= 95), (False, True))
        finally:
            P.read_pdf_text, import_and_review.pdf_reader_available = real_read, real_avail
            D.urllib.request.urlopen, D.time.sleep = real_open, real_sleep

    # the deadline bounds WALL time (urllib's timeout is per address / per socket op)
    import time as _time
    with sandbox(seed_db=False):
        real_open = D.urllib.request.urlopen
        D.urllib.request.urlopen = lambda req, timeout=None: _time.sleep(4) or _Resp(b"{}")
        t0 = _time.time()
        try:
            r = D.post_messages("111111111111111111", "x", seed="hang", deadline_s=1.5)
        finally:
            D.urllib.request.urlopen = real_open
        t.assert_eq("a hung Discord call is abandoned at the deadline (real clock)",
                    (r["ok"], _time.time() - t0 < 3), (False, True))

    # pre-check asks for the module the parser imports
    import importlib.util as _iu
    asked = []
    real_fs = _iu.find_spec
    _iu.find_spec = lambda name, *a: asked.append(name) or None
    try:
        import_and_review.pdf_reader_available()
    finally:
        _iu.find_spec = real_fs
    t.assert_eq("the pdf pre-check looks for pymupdf", asked, ["pymupdf"])

    # migration verify() catches a changed approval
    with sandbox(seed_db=False):
        conn = P.get_db()
        with conn:
            _ins_txn(conn, "5999", "2026-10", "2026-09-02", "SHOP X", "SHOP X", 1.0, approved=1)
        P._assign_merchant_uids(conn)
        conn.commit()
        before = {("5999", "SHOP X"): 1}
        with conn:
            conn.execute("UPDATE merchants SET approved=0")
        t.assert_eq("migration verify() flags an approval the run changed",
                    any("approval of untouched" in b for b in M.verify(conn, {}, before)), True)
        t.assert_eq("migration verify() flags a folded row with the wrong approval",
                    any("expected 1" in b for b in M.verify(conn, {("5999", "SHOP X"): 1}, {})),
                    True)
        conn.close()

    # cron health: streak alone, no next run, overdue next run; 09:00 grace
    with sandbox(seed_db=False) as tmp:
        jobs = tmp / "cron" / "jobs.json"
        now = datetime.now().astimezone()
        base = {"id": P.CRON_JOB_ID, "name": P.CRON_JOB_NAME, "enabled": True,
                "state": "scheduled", "last_run_at": now.isoformat(), "last_status": "ok",
                "next_run_at": (now + timedelta(days=1)).isoformat(), "failure_streak": 0}
        for label, patch in (("failure streak 2 (status ok)", {"failure_streak": 2}),
                             ("scheduled but no next run", {"next_run_at": None}),
                             ("next run 4 days overdue",
                              {"next_run_at": (now - timedelta(days=4)).isoformat()})):
            jobs.write_text(json.dumps({"jobs": [{**base, **patch}]}))
            t.assert_eq(f"cron health flags: {label}", bool(P.verify_cron_health()["healthy"]),
                        False)
        conn = P.get_db()
        with conn:
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "24-10-2026", 500)
        conn.close()
        t.assert_eq("09:00:30 import: today's 09:00 run may still be ahead",
                    [str(e["fire_date"]) for e in reminder.upcoming(datetime(2026, 10, 21, 9, 0, 30))
                     if e["kind"] == "3day"], ["2026-10-21"])
        P.set_config("reminders_disabled", "1")
        texts = {"/n.pdf": synth_statement("5999", "01-11-2026", "24-11-2026",
                                           [("02/10", "10.00", "SHOP", "SG")], 510.0)}
        real_read = P.read_pdf_text
        P.read_pdf_text = lambda p: texts[p]
        try:
            r1 = P.process_pdf("/n.pdf")["reminders_resumed"]
            r2 = P.process_pdf("/n.pdf")["reminders_resumed"]
        finally:
            P.read_pdf_text = real_read
        t.assert_eq("the 'reminders resumed' notice is stable across a re-run", (r1, r2),
                    (True, True))
        P.set_config("reminders_disabled", "1")
        P.read_pdf_text = lambda p: texts[p]
        try:
            r3 = P.process_pdf("/n.pdf")["reminders_resumed"]
        finally:
            P.read_pdf_text = real_read
        t.assert_eq("no 'resumed' notice while reminders are paused again", r3, False)
        jobs.write_text(json.dumps({"jobs": [{**base, "schedule": {"expr": "0 9 20-22 * *"}}]}))
        t.assert_eq("cron health flags a schedule that isn't daily 09:00",
                    bool(P.verify_cron_health()["healthy"]), False)


_FOOTER = (" OCBC Bank 65 Chulia Street, OCBC Centre Singapore 049513 Contact Us 1800 363 3333 "
           "(65) 6363 3333 when overseas Oversea-Chinese Banking Corporation Limited Co. Reg. "
           "No.: 193200032W 147446 - PAGE 2 OF 4 HCBS261001(566565) TRANSACTION DATE "
           "DESCRIPTION AMOUNT (SGD)")
# text in a reply line that talks to the AGENT, not the user (2026-10-03: "Relay this; add
# nothing." -> a 6-minute investigation of that phrase, live in the user's thread)
_AGENT_TALK = re.compile(
    r"(?i)\brelay\b|add nothing|\b(tell|ask|let|inform) (the user|them)\b|\byourself\b|"
    r"\bre-?run\b|\brun (this|it|the same)\b|\bthe same command\b|wait for the user|"
    r"\b(your|own) words\b|\bexplain\b|from the (user's )?(discord )?thread|"
    r"\bdo(n't| not) paste\b|HERMES_SESSION|execute_code|(?<![/\w])skill\.log|"
    r"\{(target|sess|explicit|note)\}|verbatim|nothing else|\bthe user\b|\bthis line\b|"
    r"\bretry\b|again with")
_STDERR_WAYS = ("sys.stderr", "print_exc", "print_exception", "traceback.print", "warnings.warn")
_SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")


def _literal(node) -> str:
    """The text a say()/print() argument evaluates to, with {expr} for the holes —
    adjacent literals are already one node, so split phrases are seen whole."""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else
                       "{" + ast.unparse(v.value) + "}" for v in node.values)
    if isinstance(node, ast.IfExp):
        return _literal(node.body) + " | " + _literal(node.orelse)
    if isinstance(node, ast.BinOp):
        return _literal(node.left) + _literal(node.right)
    return "{" + ast.unparse(node) + "}"


def _v351(t):
    """v3.5.1: stdout is the user's reply; footer bleed keeps the city; the
    Coffee Hive merge (#18 + #108 into #109) through the v5 migration."""
    import ast
    import contextlib as _cl
    import sqlite3 as _sq
    import sys as _s
    from scripts import approve, import_and_review, review, reviewlib
    from scripts import discord_post as D
    from scripts import migrate_identities as M
    print("--- v3.5.1 (stdout contract, footer bleed, Coffee Hive merge) ---")
    here = Path(__file__).resolve().parent
    for name in ("import_and_review.py", "review.py", "approve.py"):
        src = (here / name).read_text()
        tree = ast.parse(src)
        docs, in_fstr = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body \
                    and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                docs.add(id(n.body[0].value))
            if isinstance(n, ast.JoinedStr):
                in_fstr.update(id(v) for v in n.values)
        # EVERY string literal (not just say() arguments: lines are also built in helpers
        # and variables), f-strings with their holes as {expr}, docstrings excepted
        texts = [_literal(n) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
        texts += [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and id(n) not in docs and id(n) not in in_fstr]
        t.assert_eq(f"{name}: no string in the script talks to the agent",
                    [x[:60] for x in texts if _AGENT_TALK.search(x)], [])
        prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and getattr(n.func, "id", "") == "print"]
        t.assert_eq(f"{name}: every stdout reply goes through say() (bare print = the body, or "
                    f"the failed-to-load line)",
                    [ast.unparse(n)[:50] for n in prints
                     if not ([ast.unparse(a) for a in n.args] == ["body"] and not n.keywords)
                     and not _literal(n.args[0]).startswith("🚨 The card-review scripts failed")], [])
        t.assert_eq(f"{name}: nothing written to stderr (the agent reads stderr too)",
                    [w for w in _STDERR_WAYS if w in src], [])

    buf = io.StringIO()
    with _cl.redirect_stdout(buf):
        reviewlib.say("🚨 a\n<html>\n  b\tc ")
    t.assert_eq("say() writes exactly one line, whitespace collapsed", buf.getvalue(),
                "🚨 a <html> b c\n")
    t.assert_eq("brief() drops HTML tags and caps the length",
                (reviewlib.brief("HTTP 503: <html>\n<b>busy</b></html>"),
                 len(reviewlib.brief("x" * 500))), ("HTTP 503: busy", 160))
    t.assert_eq("brief() keeps a network error's reason",
                reviewlib.brief("message 1/1: URLError: <urlopen error [Errno -3] Temporary "
                                "failure in name resolution>"),
                "message 1/1: URLError: [Errno -3] Temporary failure in name resolution")
    buf = io.StringIO()
    with _cl.redirect_stdout(buf):
        reviewlib.say("boom 1555933537457274922 id_1555933537457274922x")
    t.assert_eq("say() masks a Discord id (also next to letters)", buf.getvalue(),
                "boom [id] id_[id]x\n")
    t.assert_eq("reminder status in the reply: first clause only (the rest may be advice)",
                [import_and_review._reminder_status(x) for x in (
                    "🚨 Payment reminders may NOT fire — run `hermes cron resume 7b68c1ef7afe`.",
                    "✅ Payment reminders are SET — automatic daily check, nothing for you to do.",
                    "⏸️ Payment reminders are PAUSED (manual override is on).", "")],
                ["🚨 Payment reminders may NOT fire", "✅ Payment reminders are SET",
                 "⏸️ Payment reminders are PAUSED", ""])
    sl = approve.summary_line
    t.assert_eq("summary_line: >5 skips are capped, no tick for a no-op",
                sl({"verb": "approve"}, {"changed": [], "skipped": [(i, "no such id")
                                                                   for i in range(1, 8)]}, {}),
                "⚠️ Nothing changed — skipped #1 (no such id), #2 (no such id), #3 (no such id), "
                "#4 (no such id), #5 (no such id) and 2 more; nothing left to approve.")
    t.assert_eq("summary_line: nothing to do in the list",
                sl({"verb": "approve"}, {"changed": [], "skipped": []}, {"5999": 2}),
                "ℹ️ Nothing changed — nothing in the list needed it; still unapproved — 5999: 2.")
    t.assert_eq("summary_line: another verb",
                sl({"verb": "unapprove"}, {"changed": [(3, "5999", "X")], "skipped": []}, {"5999": 1}),
                "✅ Un-approved 1 merchant(s) (#3); still unapproved — 5999: 1.")

    thread, other = "1600000000000000002", "1545596543107600454"
    key = f"agent:main:discord:thread:{thread}:{thread}"

    class OldIds(FakeDiscord):
        """Discord answering with ALREADY-POSTED messages (nonce de-duplication on a re-run)"""
        def __call__(self, req, timeout=None):
            import time as _t
            self.posts.append(1)
            old = (int(_t.time() * 1000) - 120_000 - 1420070400000) << 22
            return _Resp(json.dumps({"id": str(old + len(self.posts))}).encode())

    class HtmlFail(FakeDiscord):
        """Cloudflare-style multi-line HTML error body on every request"""
        def __call__(self, req, timeout=None):
            self.posts.append(1)
            raise urllib.error.HTTPError(req.full_url, 503, "err", {}, io.BytesIO(
                b"<html>\n<head><title>503</title></head>\n<body>\nbusy\n</body>\n</html>"))

    def busy(*a, **k):
        raise _sq.OperationalError("database is locked")

    real = (D.urllib.request.urlopen, D.time.sleep, P.read_pdf_text,
            import_and_review.pdf_reader_available, _s.argv, reviewlib.record_shown,
            approve.apply, import_and_review.build_review)
    lines = {}
    with sandbox(seed_db=True):
        conn = P.get_db()
        prev = conn.execute("SELECT year_month, total_due FROM statements WHERE card_suffix="
                            "'7008' ORDER BY year_month DESC LIMIT 1").fetchone()
        approved = conn.execute("SELECT merchant_uid FROM merchants WHERE approved=1 AND "
                                "merchant_normalized NOT LIKE '%ANNUAL FEE%' LIMIT 1").fetchone()[0]
        with conn:
            conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO "
                         "UPDATE SET value=excluded.value",
                         (reviewlib.MERGED_IDS_KEY, json.dumps({"99998": approved})))
        conn.close()
        # the month after the latest real 7008 statement — a hard-coded month goes stale
        # (once the real one is imported, the fake is a re-import and the status line moves)
        y, m = map(int, (prev[0] if prev else "2026-10").split("-"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        pm = 12 if m == 1 else m - 1
        fake_ym = f"{y}-{m:02d}"
        prev_total = prev[1] if prev else 0.0
        D.time.sleep = lambda s: None
        import_and_review.pdf_reader_available = lambda: True
        P.read_pdf_text = lambda p: synth_statement("7008", f"01-{m:02d}-{y}", f"24-{m:02d}-{y}", [
            (f"05/{pm:02d}", f"({prev_total:.2f})", "PAYMENT BY INTERNET", ""),
            (f"06/{pm:02d}", "15.00", "V351 FRESH SHOP\nSINGAPORE", "SG")], 15.0)

        def imp(*extra):
            return lambda: import_and_review.main(["x", "--post", *extra, "/v351.pdf"])

        def rev(*args):
            def run():
                _s.argv = ["review.py", *args]
                return review.main()
            return run

        def app(text):
            return lambda: approve.main(["approve.py", "--post", text])

        def patch(**kw):
            def go():
                for k, v in kw.items():
                    {"rs": lambda v: setattr(reviewlib, "record_shown", v),
                     "apply": lambda v: setattr(approve, "apply", v),
                     "build": lambda v: setattr(import_and_review, "build_review", v)}[k](v)
            return go

        new_uid = None
        scenarios = [   # label, session?, discord, setup, command, exit code
            ("import", True, FakeDiscord, None, imp(), 0),
            ("import, explicit message id", True, FakeDiscord, None, imp(other), 0),
            ("import, re-run dedupe", True, OldIds, None, imp(), 0),
            ("import, post fails", True, lambda: FakeDiscord([403] * 9), None, imp(), 3),
            ("import, HTML 503 body", True, HtmlFail, None, imp(), 3),
            ("import, DB busy", True, FakeDiscord, patch(rs=busy), imp(), 3),
            ("import, build crash", True, FakeDiscord, patch(build=lambda *a: 1 / 0), imp(), 3),
            ("import, no session", False, FakeDiscord, None, imp(), 2),
            ("import, no PDFs", True, FakeDiscord, None,
             lambda: import_and_review.main(["x", "--post"]), 2),
            ("refused", True, FakeDiscord, None, app("approve all?"), 2),
            ("refused, post fails", True, lambda: FakeDiscord([403] * 9), None,
             app("approve all?"), 2),
            ("import again", True, FakeDiscord, None, imp(), 0),
            ("approve", True, FakeDiscord, None, app("approve all"), 0),
            ("approve, typo id", True, FakeDiscord, None, app("approve #99999"), 0),
            ("approve, merged id", True, FakeDiscord, None, app("approve #99998"), 0),
            ("approve, already approved", True, FakeDiscord, None, app(f"approve #{approved}"), 0),
            ("approve, no-op post fails", True, lambda: FakeDiscord([403] * 9), None,
             app("approve #99999"), 3),
            ("approve, skip clause", True, FakeDiscord, None, None, 0),
            ("approve, post fails", True, lambda: FakeDiscord([403] * 9), None, None, 3),
            ("approve, DB busy", True, FakeDiscord, patch(apply=busy), app("approve #1"), 3),
            ("approve, no session", False, FakeDiscord, None, app("approve #1"), 2),
            ("approve, empty reply", True, FakeDiscord, None, app(""), 2),
            ("list", True, FakeDiscord, None, rev("--merchants-only", "--post"), 0),
            ("review", True, FakeDiscord, None, rev("--post"), 0),
            ("review, post fails", True, lambda: FakeDiscord([403] * 9), None, rev("--post"), 3),
            ("review, DB busy", True, FakeDiscord, patch(rs=busy), rev("--post"), 3),
            ("review, no session", False, FakeDiscord, None, rev("--post"), 2),
        ]
        try:
            for label, sess, disc, setup, cmd, want in scenarios:
                if label in ("approve, skip clause", "approve, post fails"):
                    c = P.get_db()
                    new_uid = c.execute("SELECT merchant_uid FROM merchants WHERE "
                                        "merchant_normalized='V351 FRESH SHOP SINGAPORE'"
                                        ).fetchone()[0]
                    with c:
                        c.execute("UPDATE merchants SET approved=0 WHERE merchant_uid=?",
                                  (new_uid,))
                    c.close()
                    cmd = app(f"approve #{approved} #{new_uid}")
                if sess:
                    os.environ["HERMES_SESSION_KEY"] = key
                else:
                    os.environ.pop("HERMES_SESSION_KEY", None)
                D.urllib.request.urlopen = disc()
                if setup:
                    setup()
                out, err = io.StringIO(), io.StringIO()
                try:
                    with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                        rc = cmd()
                finally:
                    (reviewlib.record_shown, approve.apply, import_and_review.build_review) = real[5:]
                lines[label] = out.getvalue().strip()
                line = lines[label]
                t.assert_eq(f"{label}: exit {want}, ONE line for the user, no ids/agent talk/"
                            f"stderr ({line[:40]!r}…)",
                            (rc, line.count("\n"), bool(line), bool(_SNOWFLAKE.search(line)),
                             bool(_AGENT_TALK.search(line)), err.getvalue()),
                            (want, 0, True, False, False, ""))
        finally:
            (D.urllib.request.urlopen, D.time.sleep, P.read_pdf_text,
             import_and_review.pdf_reader_available, _s.argv, reviewlib.record_shown,
             approve.apply, import_and_review.build_review) = real
    fym = re.escape(fake_ym)
    status = (r"(; (✅ Payment reminders are SET|⏸️ Payment reminders are PAUSED|"
              r"🚨 Payment reminders may NOT fire))?")
    exact = {
        "import": rf"✅ Review posted above — imported 7008 {fym} \(2 txns\); "
                  rf"(\d+ parser warning\(s\) — see the review; )?1 merchant\(s\) awaiting "
                  rf"approval{status}\.",
        "import, no PDFs": r"🚨 Nothing imported — I didn't get the statement PDF\(s\); please "
                           r"send them again\.",
        "approve, empty reply": r"⚠️ Nothing changed — I didn't receive your reply text; please "
                                r"send it again\.",
        "refused": r"⚠️ Nothing changed: that reads like a question\. The replies I accept are "
                   r"posted above\.",
        "approve": r"✅ Approved 1 merchant\(s\) \(#\d+\); (nothing left to approve|"
                   r"still unapproved — .+)\.",
        "approve, typo id": r"⚠️ Nothing changed — skipped #99999 \(no such id\); .+\.",
        "approve, merged id": r"⚠️ Nothing changed — skipped #99998 \(merged into #\d+ — use "
                              r"that id\); .+\.",
        "approve, already approved": r"ℹ️ Nothing changed — skipped #\d+ \(no change needed\); .+\.",
        "approve, no-op post fails": r"⚠️ Nothing changed — skipped #99999 \(no such id\); [^S]+ "
                                     r"\(posting the details to Discord failed: .+\)\.",
        "import, re-run dedupe": rf"✅ Review posted above \(1 part\(s\) were already there and "
                                 rf"weren't repeated\) — imported 7008 {fym} \(2 txns\); .+\.",
        "approve, skip clause": r"✅ Approved 1 merchant\(s\) \(#\d+\); skipped #\d+ \(no change "
                                r"needed\); .+\.",
        "approve, post fails": r"🚨 Saved — Approved 1 merchant\(s\) \(#\d+\); .+ — but posting "
                               r"the confirmation to Discord failed: .+\.",
        "import, HTML 503 body": rf"🚨 Imported 7008 {fym} \(2 txns\), but the review was not "
                                 rf"fully posted \(0/1 message\(s\)\): [^<>]*HTTP 503[^<>]*\.",
        "list": r"✅ Merchant list posted above — \d+ merchant\(s\) awaiting approval\.",
        "review": r"✅ Review posted above — \d+ merchant\(s\) awaiting approval\.",
    }
    for label, pat in exact.items():
        t.assert_eq(f"{label}: the exact reply ({lines.get(label, '')[:44]!r}…)",
                    bool(re.fullmatch(pat, lines.get(label, ""))), True)

    for raw in ("-9113 NEW KOPI PLACE SINGAPORE", "-9113 COFFEE HIVE  INNOVI SINGAPORE",
                "-9113 DAPUR PENYET SINGAPORE", "-9330 THE WOK PEOPLE SINGAPORE",
                "-1534 SHOPEE SINGAPORE MP SINGAPORE", "CCY CONVERSION FEE FOR: 20.98 SGD",
                "-9330 OLD TEA HUT  GALAXI SG", "-9113 A PLACE LIKE HOME P N/A"):
        t.assert_eq(f"footer bleed keeps the identity: {raw[:30]!r}",
                    P.normalize_merchant(raw + _FOOTER, "5999"), P.normalize_merchant(raw, "5999"))
    t.assert_eq("user merge: #18's old descriptor is #109's merchant",
                P.normalize_merchant("-9113 COFFEE HIVE - INNOV SINGAPORE", "5999"),
                P.normalize_merchant("-9113 COFFEE HIVE  INNOVI SINGAPORE", "5999"))

    with sandbox(seed_db=False):
        conn = P.get_db()
        with conn:
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "23-10-2026", 99.0)
            for d, raw, norm, amt, ap, uid in (
                    ("2026-02-03", "-9113 COFFEE HIVE - INNOV SINGAPORE", "COFFEE HIVE - INNOV",
                     2.6, 1, 18),
                    ("2026-08-03", "-9113 DAPUR PENYET SINGAPORE" + _FOOTER, "DAPUR PENYET",
                     13.5, 1, 92),
                    ("2026-09-17", "-9113 COFFEE HIVE  INNOVI SINGAPORE" + _FOOTER,
                     "COFFEE HIVE INNOVI", 2.6, 0, 108),
                    ("2026-09-01", "-9113 COFFEE HIVE  INNOVI SINGAPORE",
                     "COFFEE HIVE INNOVI SINGAPORE", 2.3, 0, 109)):
                _ins_txn(conn, "5999", "2026-10", d, raw, norm, amt, approved=ap)
                conn.execute("UPDATE merchants SET merchant_uid=? WHERE merchant_normalized=?",
                             (uid, norm))
            for k, v in (("next_merchant_uid", "118"), ("identity_migration_v4", "2026-10-02"),
                         (reviewlib.LAST_SHOWN_KEY, json.dumps({
                             "source": "latest review", "target": "1",
                             "at": datetime.now().isoformat(timespec="seconds"),
                             "merchants": [{"uid": 108, "new": True}, {"uid": 109, "new": True},
                                           {"uid": 92, "new": True}]}))):
                conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) "
                             "DO UPDATE SET value=excluded.value", (k, v))
        conn.close()
        out2 = io.StringIO()
        with _cl.redirect_stdout(io.StringIO()):
            rc = M.main(["m", "--apply"])
        with _cl.redirect_stdout(out2):
            rc2 = M.main(["m", "--apply"])
        db = P.get_db()
        got = sorted(tuple(r) for r in db.execute(
            "SELECT merchant_uid, merchant_normalized, approved, txn_count FROM merchants"))
        t.assert_eq("v5 migration: #18 + #108 fold into #109 (approval kept), #92 renamed",
                    (rc, got), (0, [(92, "DAPUR PENYET SINGAPORE", 1, 1),
                                    (109, "COFFEE HIVE INNOVI SINGAPORE", 1, 3)]))
        t.assert_eq("v5 migration runs despite the v4 marker; a second run is 'Already applied'",
                    (rc2, "Already applied" in out2.getvalue()), (0, True))
        t.assert_eq("v5 migration: the id high-water mark is kept",
                    P.get_config("next_merchant_uid"), "118")
        t.assert_eq("v5 migration: merged ids are remembered",
                    json.loads(P.get_config(reviewlib.MERGED_IDS_KEY)), {"18": 109, "108": 109})
        t.assert_eq("v5 migration: the last-shown list points at the surviving ids",
                    [m["uid"] for m in json.loads(P.get_config(reviewlib.LAST_SHOWN_KEY))["merchants"]],
                    [109, 92])
        res = approve.apply(db, approve.parse_command("approve #18"))
        t.assert_eq("'approve #18' after the merge says where it went",
                    (res["changed"], res["skipped"]), ([], [(18, "merged into #109 — use that id")]))
        try:
            approve.apply(db, approve.parse_command("approve all except #108"), "1")
            t._fail("'all except #108' after the merge must be refused")
        except approve.Refused as why:
            t.assert_eq("'all except #108' after the merge: refused, says where it went",
                        "#108 was merged into #109" in str(why), True)
        cur = db.cursor()
        with db:
            db.execute("UPDATE config SET value=? WHERE key=?",
                       (json.dumps({"5": 18, "bad": "x", "7": None}), reviewlib.MERGED_IDS_KEY))
            db.execute("UPDATE config SET value=? WHERE key=?", (json.dumps({
                "source": "x", "target": "1", "at": "2026-10-04T00:00:00", "merchants": [
                    {"uid": 109, "new": False}, {"uid": 108, "new": True},
                    {"uid": 5, "new": False}]}), reviewlib.LAST_SHOWN_KEY))
            M._record_folds(cur, {18: 109, 108: 109})
        t.assert_eq("merged-id map: chains resolved, a bad entry doesn't wipe the rest",
                    json.loads(P.get_config(reviewlib.MERGED_IDS_KEY)),
                    {"5": 109, "18": 109, "108": 109})
        t.assert_eq("last-shown remap: folded ids collapse, 'new' kept if either was new",
                    json.loads(P.get_config(reviewlib.LAST_SHOWN_KEY))["merchants"],
                    [{"uid": 109, "new": True}])
        db.close()

    with sandbox(seed_db=False):   # the folded id is the HIGHEST and no id mark exists yet
        conn = P.get_db()
        with conn:
            _ins_stmt(conn, "5999", "2026-10", "01-10-2026", "23-10-2026", 9.0)
            for raw, norm, uid in (("-9113 ZED SHOP SINGAPORE" + _FOOTER, "ZED SHOP", 140),
                                   ("-9113 ZED SHOP SINGAPORE", "ZED SHOP SINGAPORE", 50)):
                _ins_txn(conn, "5999", "2026-10", "2026-09-02", raw, norm, 4.5, approved=1)
                conn.execute("UPDATE merchants SET merchant_uid=? WHERE merchant_normalized=?",
                             (uid, norm))
            conn.execute("DELETE FROM config WHERE key='next_merchant_uid'")
        conn.close()
        with _cl.redirect_stdout(io.StringIO()):
            M.main(["m", "--apply"])
        t.assert_eq("a folded top id (#140) is never handed out again",
                    int(P.get_config("next_merchant_uid") or 0) > 140, True)

    _guards(t, here)


def _guards(t, here):
    """The __main__ crash guards and the import guard, run as real processes: one line,
    the right exit code, NOTHING on stderr (the agent reads it). Temp DB/log only."""
    import subprocess
    import sys as _s
    import tempfile
    root = here.parent
    raise_rt = "(_ for _ in ()).throw(RuntimeError('boom 1555933537457274922'))"
    raise_ki = "(_ for _ in ()).throw(KeyboardInterrupt())"
    pdf_ok = ("import importlib.util as _iu; _f = _iu.find_spec; "
              "_iu.find_spec = lambda n, *a, **k: object() if n == 'pymupdf' else _f(n, *a, **k)")
    cases = [
        ("approve.py", ["--post", "approve #1"], f"P.get_db = lambda: {raise_rt}", 1),
        ("approve.py", ["--post", "approve #1"], f"P.get_db = lambda: {raise_ki}", 1),
        ("review.py", ["--post"], f"P.get_db = lambda: {raise_rt}", 1),
        ("review.py", ["--post"], f"P.get_db = lambda: {raise_ki}", 1),
        ("import_and_review.py", ["--post", "/x.pdf"],
         f"{pdf_ok}\nD.resolve_target = lambda *a, **k: {raise_rt}", 3),
        ("import_and_review.py", ["--post", "/x.pdf"],
         f"{pdf_ok}\nD.resolve_target = lambda *a, **k: {raise_ki}", 3),
    ]
    tmp = Path(tempfile.mkdtemp(prefix="ccbr_selftest_"))
    env = dict(os.environ, HERMES_SESSION_KEY="agent:main:discord:thread:1600000000000000003:"
                                              "1600000000000000003", HERMES_HOME=str(tmp))
    try:
        for script, argv, patch, want in cases:
            prelude = "\n".join([
                "import sys, logging, runpy",
                f"sys.path.insert(0, {str(root)!r})",
                "from scripts import parse_ocbc as P, discord_post as D",
                f"P.DB_PATH = {str(tmp / 'none.db')!r}",
                f"P.SNAPSHOT_DIR = {str(tmp)!r}",
                f"P.log.handlers = [logging.FileHandler({str(tmp / 'skill.log')!r})]",
                f"D.HERMES_HOME = {str(tmp)!r}",
                patch,
                f"sys.argv = {[script] + argv!r}",
                f"runpy.run_path({str(here / script)!r}, run_name='__main__')"])
            r = subprocess.run([_s.executable, "-c", prelude], capture_output=True, text=True,
                               timeout=120, env=env, cwd=tmp, stdin=subprocess.DEVNULL)
            line = r.stdout.strip()
            kind = "Ctrl-C" if "Keyboard" in patch else "crash"
            t.assert_eq(f"{script} {kind} guard: exit {want}, one line, empty stderr "
                        f"({line[:40]!r}…)",
                        (r.returncode, line.count("\n"), bool(line),
                         bool(_SNOWFLAKE.search(line)), r.stderr[-300:]),
                        (want, 0, True, False, ""))
        # an unwritable skill.log and a broken import: still nothing on stderr
        sk = tmp / "skill"
        # .py only: the deploy's cc_review.db.bak-* backup must never be copied to /tmp
        shutil.copytree(here, sk / "scripts", ignore=lambda d, names: [
            n for n in names if not n.endswith(".py")])
        log_file = sk / "skill.log"
        log_file.write_text("")
        os.chmod(log_file, 0)
        if os.access(log_file, os.W_OK):
            t._ok("unwritable-log check skipped (running as root)")
        else:
            code = "\n".join(["import sys", f"sys.path.insert(0, {str(sk)!r})",
                              "from scripts import parse_ocbc as P",
                              "P.log.info('post target: note 1555933537457274922')",
                              "P.log.exception('crash')"])
            r = subprocess.run([_s.executable, "-c", code], capture_output=True, text=True,
                               timeout=60, cwd=tmp, stdin=subprocess.DEVNULL, env=env)
            t.assert_eq("an unwritable skill.log in an agent turn: nothing on stderr",
                        (r.returncode, r.stderr), (0, ""))
            cron_env = {k: v for k, v in env.items() if k != "HERMES_SESSION_KEY"}
            r = subprocess.run([_s.executable, "-c", code], capture_output=True, text=True,
                               timeout=60, cwd=tmp, stdin=subprocess.DEVNULL, env=cron_env)
            t.assert_eq("an unwritable skill.log under cron: the warning goes to the job output",
                        (r.returncode, "could not open" in r.stderr), (0, True))
        (sk / "scripts" / "reviewlib.py").write_text("def broken(:\n")
        r = subprocess.run([_s.executable, str(sk / "scripts" / "approve.py"), "--post",
                            "approve #1"], capture_output=True, text=True, timeout=60, env=env,
                           cwd=tmp, stdin=subprocess.DEVNULL)
        t.assert_eq("a broken import: exit 1, one line for the user, empty stderr",
                    (r.returncode, r.stdout.strip().startswith("🚨 The card-review scripts failed "
                                                               "to load (SyntaxError)"),
                     r.stdout.strip().count("\n"), r.stderr[-300:]), (1, True, 0, ""))
    finally:
        for f in tmp.rglob("skill.log"):
            os.chmod(f, 0o600)
        shutil.rmtree(tmp, ignore_errors=True)
