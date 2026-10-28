#!/usr/bin/env python3
"""Validates gmail_triage.py's state-write (the triaged_at stamping + the
orphan-free merge) with the Gmail/Discord I/O fully mocked — no network, no
inbox changes, no Discord posts. Writes state to a temp file and asserts the
resulting entries.

Run:  python3 test_triage_state.py
      GMAIL_TRIAGE_PATH=/tmp/gmail_audit/gmail_triage.py python3 test_triage_state.py
"""
import os, sys, json, tempfile, importlib.util
from pathlib import Path

TRIAGE_PATH = os.environ.get("GMAIL_TRIAGE_PATH",
                             "/home/amh/.hermes/scripts/gmail_triage.py")
spec = importlib.util.spec_from_file_location("gt", TRIAGE_PATH)
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)

EPOCH = "1970/01/01 00:00:00"
fail = 0
def check(name, cond, detail=""):
    global fail
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fail += 1

# ── Seed state: a pre-feature entry (no stamp), a stamped entry, and an entry
#    that will also appear in the fake inbox (idempotency skip must preserve it).
seed = {"last_run": "2026/05/03 09:00:00", "processed_ids": [
    {"id": "oldNoStamp", "category": "Auto-filed", "from": "a@b.com", "subject": "old"},
    {"id": "oldStamped", "category": "Action", "from": "c@d.com", "subject": "keep",
     "triaged_at": "2026/05/01 10:00:00"},
    {"id": "dupId", "category": "ReadLater", "from": "e@f.com", "subject": "dup",
     "triaged_at": "2026/05/02 10:00:00"},
]}
tmp = Path(tempfile.mkdtemp()) / "state.json"
tmp.write_text(json.dumps(seed))

# ── Fake inbox: a brand-new promo, an already-Hermes-labeled email, and the dup.
fake_inbox = [
    {"id": "newAuto", "from": "deals@shop.com", "subject": "Flash sale 50% off",
     "snippet": "unsubscribe", "labels": ["CATEGORY_PROMOTIONS"]},
    {"id": "alreadyLabeled", "from": "someone@corp.com", "subject": "prior",
     "labels": ["L1", "INBOX"]},          # Gmail returns label IDS, never names
    {"id": "dupId", "from": "e@f.com", "subject": "dup", "labels": ["INBOX"]},
]

# ── Mock every I/O boundary (no network, no Discord, no inbox mutation). ──────
gt.STATE_FILE = tmp
LABELS = {"Hermes/Action": "L1", "Hermes/Auto-filed": "L2", "Hermes/ReadLater": "L3", "Hermes/VIP": "L4"}
gt.ensure_hermes_labels_simple = lambda: dict(LABELS)
gt.load_correspondents = lambda: (set(), set())
gt.get_gmail_messages = lambda *a, **k: fake_inbox
gt.get_message_body = lambda *a, **k: ""
gt.apply_labels = lambda *a, **k: True
gt.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
gt.send_action_reminder = lambda *a, **k: True

gt.main()

state = json.loads(tmp.read_text())
by_id = {e["id"]: e for e in state["processed_ids"]}

check("all entries carry a triaged_at (no orphans)",
      all("triaged_at" in e for e in state["processed_ids"]),
      str([e["id"] for e in state["processed_ids"] if "triaged_at" not in e]))
check("pre-feature entry backfilled to EPOCH (won't re-report)",
      by_id["oldNoStamp"]["triaged_at"] == EPOCH, by_id["oldNoStamp"])
check("already-stamped entry preserves its stamp",
      by_id["oldStamped"]["triaged_at"] == "2026/05/01 10:00:00", by_id["oldStamped"])
check("idempotency-skipped dup preserves its stamp + category",
      by_id["dupId"]["triaged_at"] == "2026/05/02 10:00:00"
      and by_id["dupId"]["category"] == "ReadLater", by_id["dupId"])
check("new email categorized Auto-filed with a real (non-EPOCH) stamp",
      by_id["newAuto"]["category"] == "Auto-filed"
      and by_id["newAuto"]["triaged_at"] not in ("", EPOCH), by_id.get("newAuto"))
check("new email persists from + subject",
      by_id["newAuto"]["from"] and by_id["newAuto"]["subject"], by_id.get("newAuto"))
check("already-labeled email added with category from label + EPOCH stamp",
      by_id["alreadyLabeled"]["category"] == "Action"
      and by_id["alreadyLabeled"]["triaged_at"] == EPOCH, by_id.get("alreadyLabeled"))

# ── #4 regression (2026-06-06): a label-application FAILURE must NOT archive /
#    mark-read the email and must NOT record it in state, so the next run
#    re-discovers and retries it (instead of filing it away with no label).
#    Since 2026-09-28 label+archive+mark-read is ONE modify call, so "failure"
#    is that call raising — nothing else may be attempted afterwards. ─────────
spec2 = importlib.util.spec_from_file_location("gt2", TRIAGE_PATH)
g2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(g2)
tmp2 = Path(tempfile.mkdtemp()) / "state.json"
tmp2.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g2.STATE_FILE = tmp2
g2.ensure_hermes_labels_simple = lambda: dict(LABELS)
g2.load_correspondents = lambda: (set(), set())
g2.get_message_body = lambda *a, **k: ""
g2.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g2.send_action_reminder = lambda *a, **k: True
g2.get_gmail_messages = lambda *a, **k: [
    {"id": "failLabel", "from": "deals@shop.com", "subject": "Flash sale 50% off",
     "snippet": "unsubscribe", "labels": ["CATEGORY_PROMOTIONS", "INBOX", "UNREAD"]},
]
modify_calls = []
def failing_gapi(args, retries=3):
    modify_calls.append(args)
    raise RuntimeError("simulated modify failure")
g2.run_gapi = failing_gapi
g2.main()
ids2 = {e["id"] for e in json.loads(tmp2.read_text())["processed_ids"]}
check("label failure → email NOT recorded in state (retried next run)",
      "failLabel" not in ids2, str(ids2))
check("label failure → exactly one (failed) modify attempt, no separate archive/mark-read",
      len(modify_calls) == 1, str(modify_calls))
check("Auto-filed modify is atomic: add L2 + remove INBOX,UNREAD in the same call",
      modify_calls and "--add-labels" in modify_calls[0] and "L2" in modify_calls[0]
      and "INBOX,UNREAD" in ",".join(modify_calls[0]), str(modify_calls))

# ── 2026-09-28: Hermes-label pre-check compares label IDS (was dead: names). ──
#    Empty state + inbox mail already carrying Hermes label ids → recorded with
#    their existing category + EPOCH, and NO modify call (no re-label/re-archive).
spec3 = importlib.util.spec_from_file_location("gt3", TRIAGE_PATH)
g3 = importlib.util.module_from_spec(spec3); spec3.loader.exec_module(g3)
tmp3 = Path(tempfile.mkdtemp()) / "state.json"
tmp3.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g3.STATE_FILE = tmp3
g3.ensure_hermes_labels_simple = lambda: dict(LABELS)
g3.load_correspondents = lambda: (set(), set())
g3.get_message_body = lambda *a, **k: ""
g3.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g3.send_action_reminder = lambda *a, **k: True
g3.get_gmail_messages = lambda *a, **k: [
    {"id": "wasRL", "from": "x@y.com", "subject": "s", "labels": ["INBOX", "L3"]},
    {"id": "wasVipAction", "from": "akanestar@hotmail.com", "subject": "hi", "labels": ["INBOX", "L4", "L1"]},
]
calls3 = []
g3.run_gapi = lambda args, retries=3: calls3.append(args) or {}
g3.main()
by3 = {e["id"]: e for e in json.loads(tmp3.read_text())["processed_ids"]}
check("pre-check (by label ID) skips already-labeled mail: no modify calls",
      not [c for c in calls3 if c[:2] == ["gmail", "modify"]], str(calls3))
check("pre-check records ReadLater from L3 with EPOCH",
      by3.get("wasRL", {}).get("category") == "ReadLater" and by3["wasRL"]["triaged_at"] == EPOCH, str(by3))
check("pre-check prefers category over VIP flag (L4+L1 → Action)",
      by3.get("wasVipAction", {}).get("category") == "Action", str(by3))

# ── 2026-09-28: re-label removes OTHER Hermes label ids (was a no-op). ─────────
got = []
g3.run_gapi = lambda args, retries=3: got.append(args) or {}
ok = g3.apply_labels("m", ["Hermes/ReadLater"], dict(LABELS), ["INBOX", "L1", "L4"])
check("apply_labels removes stale Hermes ids and keeps non-Hermes labels",
      ok and got == [["gmail", "modify", "m", "--add-labels", "L3", "--remove-labels", "L1,L4"]], str(got))
got.clear()
ok = g3.apply_labels("m", ["Hermes/Nope"], dict(LABELS), [])
check("apply_labels refuses an unmapped label (no name-as-id fallback)", ok is False and got == [], str(got))

# ── 2026-09-28: state is persisted PER EMAIL — a crash mid-run keeps the ones
#    already filed in state (and so in the digest). ─────────────────────────────
spec4 = importlib.util.spec_from_file_location("gt4", TRIAGE_PATH)
g4 = importlib.util.module_from_spec(spec4); spec4.loader.exec_module(g4)
tmp4 = Path(tempfile.mkdtemp()) / "state.json"
tmp4.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g4.STATE_FILE = tmp4
g4.ensure_hermes_labels_simple = lambda: dict(LABELS)
g4.load_correspondents = lambda: (set(), set())
g4.get_gmail_messages = lambda *a, **k: [
    {"id": "a1", "from": "deals@shop.com", "subject": "Flash sale", "labels": ["CATEGORY_PROMOTIONS", "INBOX"]},
    {"id": "a2", "from": "deals@shop.com", "subject": "Flash sale 2", "labels": ["CATEGORY_PROMOTIONS", "INBOX"]},
]
n = {"i": 0}
def body_then_crash(mid):
    n["i"] += 1
    if n["i"] == 2:
        raise KeyboardInterrupt("simulated kill")
    return ""
g4.get_message_body = body_then_crash
g4.run_gapi = lambda args, retries=3: {}
try:
    g4.main()
except KeyboardInterrupt:
    pass
ids4 = {e["id"]: e for e in json.loads(tmp4.read_text())["processed_ids"]}
check("crash after 1st email: the filed email is already in state with a real stamp",
      "a1" in ids4 and ids4["a1"]["triaged_at"] != EPOCH and "a2" not in ids4, str(ids4))

# ── 2026-09-28: a failed Unsure question stays pending and is re-asked. ───────
spec5 = importlib.util.spec_from_file_location("gt5", TRIAGE_PATH)
g5 = importlib.util.module_from_spec(spec5); spec5.loader.exec_module(g5)
tmp5 = Path(tempfile.mkdtemp()) / "state.json"
tmp5.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g5.STATE_FILE = tmp5
g5.ensure_hermes_labels_simple = lambda: dict(LABELS)
g5.load_correspondents = lambda: (set(), set())
g5.get_message_body = lambda *a, **k: ""
g5.run_gapi = lambda args, retries=3: {}
g5.send_action_reminder = lambda *a, **k: True
g5.categorize = lambda m, b: "Unsure"
inbox5 = [{"id": "u1", "from": "x@y.com", "subject": "payment failed? 50% off", "labels": ["INBOX"]}]
g5.get_gmail_messages = lambda *a, **k: inbox5
asked = []
g5.send_unsure_query = lambda ms: asked.append([m["id"] for m in ms]) or []      # post fails
g5.main()
st5 = json.loads(tmp5.read_text())
check("failed Unsure post → id stays in unsure_pending", st5.get("unsure_pending") == ["u1"], str(st5.get("unsure_pending")))
g5.send_unsure_query = lambda ms: asked.append([m["id"] for m in ms]) or [m["id"] for m in ms]  # succeeds
g5.main()
st5 = json.loads(tmp5.read_text())
check("next run re-asks the pending Unsure, then clears it",
      asked == [["u1"], ["u1"]] and st5.get("unsure_pending") == [], f"{asked} {st5.get('unsure_pending')}")

# ── 2026-09-28: 9 PM reminder catch-up — fires on the first run ≥21:00 that
#    hasn't delivered it today, once. ────────────────────────────────────────────
from datetime import datetime as _dt
class FakeDT(_dt):
    now_value = None
    @classmethod
    def now(cls, tz=None):
        return cls.now_value
g5.categorize = gt.categorize
g5.get_gmail_messages = lambda q, **k: ([{"id": "act", "from": "p@q.com", "subject": "x", "labels": ["L1", "UNREAD"]}]
                                         if "is:unread" in q else [])
sent = []
g5.send_action_reminder = lambda ms: sent.append(len(ms)) or True
g5.datetime = FakeDT
for hh in (20, 22, 23):
    FakeDT.now_value = _dt(2030, 1, 2, hh, 0, 5, tzinfo=g5.SGT)   # a day no earlier run touched
    g5.main()
check("reminder: not at 20:00, fires at 22:00 (21:00 missed), not again at 23:00",
      sent == [1], str(sent))

# ── gate lap 1: a FAILED body fetch defers the email (was: filed on an empty body).
spec6 = importlib.util.spec_from_file_location("gt6", TRIAGE_PATH)
g6 = importlib.util.module_from_spec(spec6); spec6.loader.exec_module(g6)
tmp6 = Path(tempfile.mkdtemp()) / "state.json"; tmp6.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g6.STATE_FILE = tmp6
g6.ensure_hermes_labels_simple = lambda: dict(LABELS)
g6.load_correspondents = lambda: (set(), set())
g6.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g6.send_action_reminder = lambda *a, **k: True
g6.get_gmail_messages = lambda *a, **k: [{"id": "b1", "from": "noreply@notify.ocbc.com",
                                          "subject": "Card Transaction Alert", "labels": ["INBOX"]}]
mods6 = []
def gapi6(args, retries=3):
    if args[:2] == ["gmail", "get"]:
        raise RuntimeError("simulated get failure")
    mods6.append(args); return {}
g6.run_gapi = gapi6
g6.main()
ids6 = {e["id"] for e in json.loads(tmp6.read_text())["processed_ids"]}
check("body fetch failure → email NOT filed and NOT recorded", "b1" not in ids6 and not mods6, f"{ids6} {mods6}")

# ── gate lap 1b: an AMBIGUOUS modify failure (e.g. timeout after Gmail applied
#    it) is recorded in modify_unknown and resolved next run from real labels —
#    an archived Auto-filed email must still reach a digest. ───────────────────
spec7 = importlib.util.spec_from_file_location("gt7", TRIAGE_PATH)
g7 = importlib.util.module_from_spec(spec7); spec7.loader.exec_module(g7)
tmp7 = Path(tempfile.mkdtemp()) / "state.json"; tmp7.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g7.STATE_FILE = tmp7
g7.ensure_hermes_labels_simple = lambda: dict(LABELS)
g7.load_correspondents = lambda: (set(), set())
g7.get_message_body = lambda *a, **k: ""
g7.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g7.send_action_reminder = lambda *a, **k: True
server = {"z1": ["CATEGORY_PROMOTIONS", "INBOX", "UNREAD"]}
def gapi7(args, retries=3):
    if args[:2] == ["gmail", "modify"]:
        server[args[2]] = ["CATEGORY_PROMOTIONS", "L2"]            # applied server-side…
        raise RuntimeError("gapi error after 3 attempts: timed out after 90s")   # …but the call failed
    if args[:2] == ["gmail", "get"]:
        return {"id": args[2], "labels": server[args[2]]}
    return {}
g7.run_gapi = gapi7
g7.get_gmail_messages = lambda q, **k: ([{"id": "z1", "from": "deals@shop.com", "subject": "Flash sale",
                                          "labels": list(server["z1"])}] if "INBOX" in server["z1"] else [])
g7.main()
st7 = json.loads(tmp7.read_text())
check("ambiguous modify failure → id parked in modify_unknown, not in processed",
      "z1" in st7.get("modify_unknown", {}) and not st7["processed_ids"], str(st7))
g7.main()                                        # next run: archived, so not in the inbox query
st7 = json.loads(tmp7.read_text())
e7 = {e["id"]: e for e in st7["processed_ids"]}
check("next run: recovered from real labels as Auto-filed with a REAL stamp (reaches a digest)",
      e7.get("z1", {}).get("category") == "Auto-filed" and e7["z1"]["triaged_at"] != EPOCH
      and not st7.get("modify_unknown"), str(st7))

# ── gate lap 1b: Unsure clears only the ids SHOWN; a failing post is capped. ──
spec8 = importlib.util.spec_from_file_location("gt8", TRIAGE_PATH)
g8 = importlib.util.module_from_spec(spec8); spec8.loader.exec_module(g8)
posted = []
class R:
    status_code = 200; text = "{}"
    def json(self): return {}
import types
fake_requests = types.SimpleNamespace(post=lambda *a, **k: posted.append(k["json"]["content"]) or R(),
                                      exceptions=__import__("requests").exceptions)
sys.modules_backup = dict(sys.modules)
import requests as _rq
g8.load_env = lambda: {"DISCORD_BOT_TOKEN": "x"}
orig_post = _rq.post
_rq.post = fake_requests.post
many = [{"id": f"u{i}", "from": f"sender{i}@example.com", "subject": "S" * 80} for i in range(40)]
shown = g8.send_unsure_query(many)
_rq.post = orig_post
check("Unsure query shows only what fits, returns exactly those ids",
      0 < len(shown) < 40 and all(f"sender{i}@" in posted[-1] for i in range(len(shown)))
      and "…and" not in posted[-1] and g8.discord_len(posted[-1]) <= 2000, f"shown={len(shown)}")

print("never_sent:", g8._never_sent(_rq.exceptions.ConnectTimeout("x")),
      g8._never_sent(_rq.exceptions.ConnectionError("Connection aborted. RemoteDisconnected")))
check("retry only if never sent: ConnectTimeout yes, post-send disconnect no",
      g8._never_sent(_rq.exceptions.ConnectTimeout("x")) is True
      and g8._never_sent(_rq.exceptions.ConnectionError("('Connection aborted.', RemoteDisconnected())")) is False)

# gate lap 2: parked id seen by the inbox PRE-CHECK (get failed) → real stamp, reported once.
spec9 = importlib.util.spec_from_file_location("gt9", TRIAGE_PATH)
g9 = importlib.util.module_from_spec(spec9); spec9.loader.exec_module(g9)
tmp9 = Path(tempfile.mkdtemp()) / "state.json"
tmp9.write_text(json.dumps({"last_run": None, "processed_ids": [],
                            "modify_unknown": {"p1": {"from": "a@b.com", "subject": "s"}}}))
g9.STATE_FILE = tmp9
g9.ensure_hermes_labels_simple = lambda: dict(LABELS)
g9.load_correspondents = lambda: (set(), set())
g9.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g9.send_action_reminder = lambda *a, **k: True
def gapi9(args, retries=3):
    if args[:2] == ["gmail", "get"]:
        raise RuntimeError("get failed")
    return {}
g9.run_gapi = gapi9
g9.get_gmail_messages = lambda q, **k: [{"id": "p1", "from": "a@b.com", "subject": "s", "labels": ["INBOX", "L1"]}]
g9.main()
e9 = {e["id"]: e for e in json.loads(tmp9.read_text())["processed_ids"]}
check("parked + pre-check → recorded with a REAL stamp (reaches a digest)",
      e9.get("p1", {}).get("triaged_at") not in (None, EPOCH), str(e9))

# gate lap 2: a body that never loads is categorized on the 3rd run, not deferred forever.
spec10 = importlib.util.spec_from_file_location("gt10", TRIAGE_PATH)
g10 = importlib.util.module_from_spec(spec10); spec10.loader.exec_module(g10)
tmp10 = Path(tempfile.mkdtemp()) / "state.json"; tmp10.write_text(json.dumps({"last_run": None, "processed_ids": []}))
g10.STATE_FILE = tmp10
g10.ensure_hermes_labels_simple = lambda: dict(LABELS)
g10.load_correspondents = lambda: (set(), set())
g10.send_unsure_query = lambda ms, *a, **k: [m["id"] for m in ms]
g10.send_action_reminder = lambda *a, **k: True
g10.get_message_body = lambda mid: None
mods10 = []
g10.run_gapi = lambda args, retries=3: mods10.append(args) or {}
g10.get_gmail_messages = lambda q, **k: [{"id": "q1", "from": "deals@shop.com", "subject": "Flash sale",
                                          "labels": ["INBOX", "CATEGORY_PROMOTIONS"]}]
for _ in range(3):
    g10.main()
ids10 = {e["id"] for e in json.loads(tmp10.read_text())["processed_ids"]}
check("body unavailable: deferred twice, filed on the 3rd run",
      "q1" in ids10 and len([m for m in mods10 if m[:2] == ["gmail", "modify"]]) == 1, f"{ids10} {mods10}")

print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILURE(S)'}")
sys.exit(1 if fail else 0)
