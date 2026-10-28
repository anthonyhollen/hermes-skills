#!/usr/bin/env python3
"""test_vault_audit.py — offline regression suite for vault_audit.py.

Every case builds a throwaway HERMES_HOME under a temp dir (never ~/.hermes), loads a
FRESH copy of the engine against it, and stubs the network — nothing touches the real
vault, cron output or Discord. One case per bug fixed in a tune-up.

    python3 test_vault_audit.py            # engine = the vault_audit.py next to this file
    VAULT_AUDIT_ENGINE=/path/to/vault_audit.py python3 test_vault_audit.py
"""
import atexit, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, time, urllib.error
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("VAULT_AUDIT_ENGINE") or os.path.join(HERE, "vault_audit.py")
WRAPPER = os.environ.get("VAULT_AUDIT_WRAPPER") or os.path.join(HERE, "vault_audit_cron.py")
REAL = os.path.realpath(os.path.expanduser("~/.hermes"))
RESULTS = []
ROOT = os.path.realpath(tempfile.mkdtemp(prefix="va-suite-"))   # every throwaway home lives here; removed at exit


def _cleanup():
    for d, _subs, _files in os.walk(ROOT):          # a test may have left a dir unreadable
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
    shutil.rmtree(ROOT, ignore_errors=True)


atexit.register(_cleanup)

JOBS = [
    {"id": "aaaaaaaaaa01", "name": "Morning Thing", "schedule": {"kind": "cron", "expr": "25 7 * * 2,3,4"},
     "script": "morning.py", "enabled": True, "paused_at": None},
    {"id": "bbbbbbbbbb02", "name": "Paused Thing", "schedule": {"kind": "cron", "expr": "0 18 * * *"},
     "script": "paused.py", "enabled": False, "paused_at": "2026-06-07T23:51:25+08:00"},
    {"id": "cccccccccc03", "name": "Hourly", "schedule": {"kind": "cron", "expr": "0 * * * *"},
     "script": "hourly.py", "enabled": True, "paused_at": None},
]
ROUTINES = """# Routines

Scheduled jobs - **2 active + 1 paused**.

| ID | Name | Schedule (SGT) | Script | Skill / deliver |
|----|------|----------------|--------|-----------------|
| `aaaaaaaaaa01` | Morning Thing | Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`) | morning.py | [[Ops]] |
| `bbbbbbbbbb02` | Paused Thing **[PAUSED]** | 18:00 (`0 18 * * *`) | paused.py | [[Ops]] |
| `cccccccccc03` | Hourly | every hour, 24/7 (`0 * * * *`) | hourly.py | [[Ops]] |
"""
OPS = """# Ops
## Models / profiles
- `default` (active): **`model-a`**
- `fallback`: **`model-b`**
- `pro` (alias "pro"): `model-p`
- `curator.enabled: false`
Home channel 1000000000000000001.
"""


def make_home(jobs=JOBS, routines=ROUTINES, extra_notes=None, out_dirs=None):
    root = os.path.realpath(tempfile.mkdtemp(prefix="va-test-", dir=ROOT))
    assert not root.startswith(REAL)
    h = os.path.join(root, "hh")
    v = os.path.join(h, "memories", "ObsidianHam")
    for d in ["scripts", "cron/output", "profiles/pro", "skills/.archive/old-skill",
              "skills/email-gmail-triage", "skills/cat/tool", "memories/ObsidianHam/Hermes Agent/Skills",
              "memories/ObsidianHam/People"]:
        os.makedirs(os.path.join(h, d), exist_ok=True)
    w = lambda p, t: open(os.path.join(h, p), "w", encoding="utf-8").write(t)
    w("config.yaml", "model:\n  default: model-a\n  fallback: model-b\ncurator:\n  enabled: false\n")
    w("profiles/pro/config.yaml", "model:\n  default: model-p\n")
    w(".env", "DISCORD_BOT_TOKEN=FAKE.TOKEN.X\nDISCORD_HOME_CHANNEL=1000000000000000001\nSOME_API_KEY=x\n")
    if jobs is not None:
        w("cron/jobs.json", json.dumps({"jobs": jobs}, indent=1))
    for d in (out_dirs if out_dirs is not None else [j["id"] for j in jobs or []]):
        os.makedirs(os.path.join(h, "cron/output", d), exist_ok=True)
    w("skills/cat/tool/SKILL.md", "---\nname: tool\nversion: 1.2.0\n---\n")
    w("skills/email-gmail-triage/SKILL.md", "---\nname: email-gmail-triage\nversion: 2.1.0\n---\n"
      "**VIP senders:** `mom@example.com` (Mom)\n")
    notes = {
        "Hermes Agent/Operations.md": OPS,
        "Hermes Agent/Decisions.md": "# Decisions\n- `curator.enabled: false`\n",
        "Hermes Agent/Routines.md": routines,
        "Hermes Agent/API Reference.md": "# API\n- `SOME_API_KEY`\n",
        "Hermes Agent/Skills/Skills MOC.md": "# MOC\n## Built by Ham\n- [[Tool]]\n\n## Bundled / Hermes Agent\n\n"
                                            "## Archived (in skills/.archive/)\n- `old-skill`\n",
        "Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.2.0).\n",
        "People/Anthony (Ham).md": "# Ham\nVIP `mom@example.com`. Channel 1000000000000000001.\n## Family\n- Mom mom@example.com\n",
        "Home.md": "[[Ops]] [[Routines]] [[Tool]] [[Ham]]\n",
        "Ops.md": "x\n", "Ham.md": "x\n",
    }
    notes.update(extra_notes or {})
    for rel, t in notes.items():
        if t is not None:
            os.makedirs(os.path.dirname(os.path.join(v, rel)), exist_ok=True)
            w(os.path.join("memories/ObsidianHam", rel), t)
    w("scripts/learned_rules.json", '{"rules": []}')
    return h


def load(home, engine=ENGINE):
    os.environ["HERMES_HOME"] = home
    spec = importlib.util.spec_from_file_location("va_%d" % time.monotonic_ns(), engine)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(home, argv, post=None):
    """main(argv) on a fresh engine; returns (rc, stdout, stderr, module). `post` stubs _post_chunk."""
    m = load(home)
    calls = []
    if post is not None:
        def fake(url, token, chunk, *a, **k):
            calls.append(chunk)
            return post(len(calls))
        m._post_chunk = fake
    m.post_calls = calls
    o, e = io.StringIO(), io.StringIO()
    saved = m.urllib.request.urlopen
    m.urllib.request.urlopen = _no_network
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = m.main(argv)
    finally:
        m.urllib.request.urlopen = saved
    return rc, o.getvalue(), e.getvalue(), m


def _no_network(*a, **k):
    raise AssertionError("test tried to reach the network")


def case(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True, ""))
    except Exception as ex:
        RESULTS.append((fn.__name__, False, "%s: %s" % (type(ex).__name__, ex)))
    return fn


def vpath(h, rel):
    return os.path.join(h, "memories", "ObsidianHam", rel)


# ---------- H1: orphan cleanup must never act on unknown ground truth ----------
@case
def unreadable_jobs_json_deletes_nothing():
    for variant in ("bare_list", "control_char", "missing", "empty_obj", "truncated"):
        h = make_home(out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03", "dddddddddd04"])
        p = os.path.join(h, "cron/jobs.json")
        orig = open(p).read()
        if variant == "bare_list":
            json.dump(JOBS, open(p, "w"))
        elif variant == "control_char":
            open(p, "w").write(orig.replace('"Hourly"', '"Hour\tly"'))
        elif variant == "missing":
            os.remove(p)
        elif variant == "empty_obj":
            open(p, "w").write("{}")
        else:
            open(p, "w").write(orig[:40])
        rc, out, err, m = run(h, ["--fix"])
        left = sorted(os.listdir(os.path.join(h, "cron/output")))
        if variant in ("bare_list", "control_char"):   # Hermes reads these fine, so must we
            assert left == ["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03"], (variant, left)
        else:
            assert len(left) == 4, (variant, left)
            assert "missing, unreadable" in out or "lists no jobs" in out, (variant, out)


@case
def orphan_quarantined_not_deleted_and_non_job_dirs_ignored():
    h = make_home(out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03", "dddddddddd04", ".git", "archive-2026"])
    os.symlink(os.path.join(h, "cron/output/aaaaaaaaaa01"), os.path.join(h, "cron/output/eeeeeeeeee05"))
    rc, out, err, m = run(h, ["--fix"])
    left = sorted(os.listdir(os.path.join(h, "cron/output")))
    assert "dddddddddd04" not in left and ".git" in left and "archive-2026" in left and "eeeeeeeeee05" in left, left
    trash = os.path.join(h, "scripts/.vault_audit_orphans", m.TS, "dddddddddd04")
    assert os.path.isdir(trash), "orphan not quarantined"


@case
def bulk_orphaning_refused():
    many = ["%012x" % (0xd00000000000 + i) for i in range(5)]
    h = make_home(out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03"] + many)
    rc, out, err, m = run(h, ["--fix"])
    assert len(os.listdir(os.path.join(h, "cron/output"))) == 8 and "too many" in out, out


# ---------- H2: the backup of a stale note must survive its own run ----------
@case
def backup_of_old_note_survives_prune():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    p = vpath(h, "Hermes Agent/Skills/Tool.md")
    old = time.time() - 90 * 86400
    os.utime(p, (old, old))
    rc, out, err, m = run(h, ["--fix"])
    assert os.path.exists(p + ".bak-audit-" + m.TS), "fresh backup was pruned"
    assert "(v1.2.0)" in open(p).read()


@case
def prune_by_name_timestamp():
    h = make_home()
    d = vpath(h, "Hermes Agent")
    old_named = os.path.join(d, "Ops.md.bak-audit-20200101_000000")
    new_named = os.path.join(d, "Ops.md.bak-audit-%s" % time.strftime("%Y%m%d_%H%M%S", time.localtime(time.time() - 86400)))
    for p in (old_named, new_named):
        open(p, "w").write("x")
        os.utime(p, (0, 0))                  # ancient mtime on BOTH — only the name decides
    run(h, ["--fix"])
    assert not os.path.exists(old_named) and os.path.exists(new_named)


# ---------- M1: no silent false-clean ----------
@case
def symlinked_home_still_audits():
    h = make_home(jobs=JOBS + [{"id": "ffffffffff06", "name": "New", "schedule": {"expr": "0 9 * * *"},
                                "script": "n.py", "enabled": True}])
    link = h + "-link"
    os.symlink(h, link)
    rc, out, err, m = run(link, [])
    assert "ffffffffff06" in out and "escape" not in out, out


@case
def missing_core_note_is_reported():
    h = make_home(extra_notes={"Hermes Agent/Routines.md": None})
    os.remove(vpath(h, "Hermes Agent/Routines.md")) if os.path.exists(vpath(h, "Hermes Agent/Routines.md")) else None
    rc, out, err, m = run(h, [])
    assert "Routines.md" in out and "not verified" in out and "in sync" not in out, out


@case
def missing_model_anchor_is_reported():
    h = make_home(extra_notes={"Hermes Agent/Operations.md": OPS.replace("**`model-a`**", "`model-a`")})
    rc, out, err, m = run(h, [])
    assert "could not find the `default` model line" in out, out


# ---------- M2/M3/M4: Routines table ----------
@case
def stale_gloss_time_fixed_with_expr():
    r = ROUTINES.replace("07:25 (`25 7 * * 2,3,4`)", "07:28 (`28 7 * * 2,3,4`)")
    h = make_home(routines=r)
    rc, out, err, m = run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Routines.md")).read()
    assert "07:25 (`25 7 * * 2,3,4`)" in t, t


@case
def stale_gloss_time_alone_fixed():
    h = make_home(routines=ROUTINES.replace("07:25 (`25", "07:28 (`25"))
    rc, out, err, m = run(h, ["--fix"])
    assert "07:25 (`25 7" in open(vpath(h, "Hermes Agent/Routines.md")).read()


@case
def am_pm_gloss_left_alone():
    h = make_home(routines=ROUTINES.replace("07:25 (`25", "7:28am (`25"))
    run(h, ["--fix"])
    assert "7:28am (`25 7" in open(vpath(h, "Hermes Agent/Routines.md")).read()


@case
def two_drifts_one_row_both_applied():
    jobs = [dict(JOBS[0], enabled=False, paused_at="2026-09-01T00:00:00+08:00", schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs)
    run(h, ["--fix"])
    row = [l for l in open(vpath(h, "Hermes Agent/Routines.md")).read().splitlines() if "aaaaaaaaaa01" in l][0]
    assert "**[PAUSED]**" in row and "07:35 (`35 7 * * 2,3,4`)" in row, row


@case
def script_column_and_header_count():
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| old_morning.py |").replace("2 active + 1", "3 active + 1"))
    rc, out, err, m = run(h, [])
    assert "Script column says `old_morning.py`" in out and "header says 3 active + 1 paused" in out, out


@case
def escaped_pipe_in_name_cell():
    jobs = [dict(JOBS[0], schedule={"expr": "0 8 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=ROUTINES.replace("| Morning Thing |", "| Morning \\| Thing |"))
    rc, out, err, m = run(h, [])
    assert "'25 7 * * 2,3,4' -> '0 8 * * 2,3,4'" in out, out


@case
def paused_prose_is_not_the_marker():
    h = make_home(routines=ROUTINES.replace("Paused Thing **[PAUSED]** | 18:00", "Paused Thing | 18:00 (auto-paused off-season)"))
    run(h, ["--fix"])
    row = [l for l in open(vpath(h, "Hermes Agent/Routines.md")).read().splitlines() if "bbbbbbbbbb02" in l][0]
    assert "**[PAUSED]**" in row.split("|")[2], row


@case
def job_refs_in_other_notes():
    extra = {"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.2.0).\n"
             "- old cron `abcdef123456` hourly\n- Morning `aaaaaaaaaa01` - `28 7 * * 2,3,4`\n- Hourly `cccccccccc03` - `0 * * * *`\n"
             "- Paused `bbbbbbbbbb02` at 19:00\n"}
    h = make_home(extra_notes=extra)
    rc, out, err, m = run(h, [])
    assert "abcdef123456" in out and "runs `28 7 * * 2,3,4`" in out and "runs at 19:00" in out, out
    assert "cccccccccc03" not in out, out


# ---------- M5/M6: exit codes & the cron contract ----------
def _wrapper_rc(h, post_ok=True, env_extra=None):
    """Run the real wrapper in a subprocess with _post_chunk stubbed (no network)."""
    code = ("import sys,runpy,urllib.request; urllib.request.urlopen=None; sys.path.insert(0,%r); import vault_audit as v; "
            "v._post_chunk=lambda *a,**k: %r; sys.argv=[%r]; runpy.run_path(%r, run_name='__main__')"
            % (os.path.dirname(ENGINE), post_ok, WRAPPER, WRAPPER))
    env = dict(os.environ, HERMES_HOME=h, **(env_extra or {}))
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


@case
def wrapper_exit_and_saved_report():
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| old.py |"))     # a review item
    rc, out, err = _wrapper_rc(h, True)
    assert rc == 0 and "Script column" in out, (rc, out, err)                     # routine week: ok + report saved
    rc, out, err = _wrapper_rc(h, False)
    assert rc == 3 and "Script column" in out, (rc, out, err)                     # failed post: error


@case
def missing_home_channel_is_a_failure():
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| old.py |"))
    open(os.path.join(h, ".env"), "w").write("DISCORD_BOT_TOKEN=FAKE\n")
    rc, out, err, m = run(h, ["--post-home", "--quiet"], post=lambda n: True)
    assert rc == 3 and "Script column" in out and not m.post_calls, (rc, out, err)


@case
def post_arg_validated():
    h = make_home()
    rc, out, err, m = run(h, ["--post", "1501742338639401071/../../users/@me"], post=lambda n: True)
    assert rc == 2 and not m.post_calls
    m = load(h)
    assert m._channel_arg("<#1501742338639401071>") == "1501742338639401071"
    assert m._channel_arg("discord:1501742338639401071") == "1501742338639401071"


# ---------- M7/M8 + delivery ----------
class _Clock:
    def __init__(self):
        self.t = 0.0
    def mono(self):
        return self.t
    def sleep(self, s):
        assert s >= 0
        self.t += s


def _delivery(m, script, content="line\n" * 5):
    """Drive the REAL post path with a scripted urlopen + virtual clock; returns (ok, bodies, clock)."""
    bodies, clock = [], _Clock()
    seq = list(script)
    saved = (m.urllib.request.urlopen, m.time.monotonic, m.time.sleep)
    def urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        step = seq.pop(0)
        clock.t += 1
        if step == "ok":
            return io.BytesIO(b"{}")
        if step == "timeout":
            raise TimeoutError("read timed out")
        if step == "valueerror":
            raise ValueError("Invalid header value b'Bot SECRETPART'")
        code, body = step
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(json.dumps(body).encode()))
    m.urllib.request.urlopen = urlopen
    m.time.monotonic, m.time.sleep = clock.mono, clock.sleep
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            ok = m.post_discord("1501742338639401071", content)
    finally:   # these are the shared stdlib modules — always restore
        m.urllib.request.urlopen, m.time.monotonic, m.time.sleep = saved
    clock.err = err.getvalue()
    return ok, bodies, clock


@case
def retries_reuse_nonce_and_survive_bad_retry_after():
    m = load(make_home())
    ok, bodies, _ = _delivery(m, ["timeout", "ok"])
    assert ok and len(bodies) == 2 and bodies[0]["nonce"] == bodies[1]["nonce"] and bodies[0]["enforce_nonce"]
    for ra in (-1, float("nan"), "junk"):
        m = load(make_home())
        ok, bodies, _ = _delivery(m, [(429, {"retry_after": ra}), "ok"])
        assert ok, ra


@case
def delivery_respects_budget():
    m = load(make_home())
    content = "\n".join("- finding %d %s" % (i, "x" * 80) for i in range(300))   # ~15 messages
    ok, bodies, clock = _delivery(m, ([(429, {"retry_after": 9.0})] * 3 + ["ok"]) * 40, content)
    assert not ok and clock.t <= m.DELIVERY_BUDGET + 31, (ok, clock.t)


@case
def split_counts_utf16():
    m = load(make_home())
    text = "\n".join(["- 📌🇸🇬 " + "🇸🇬" * 40] * 60) + "\n" + "😀" * 3000
    chunks = m._split(text, 1880)
    u16 = lambda c: len(c.encode("utf-16-le")) // 2          # independent of the engine's own counter
    assert chunks and all(u16(c) <= 1880 for c in chunks), max(u16(c) for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


# ---------- recurrence / state ----------
@case
def same_day_runs_do_not_fake_recurrence():
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| old.py |"))
    for _ in range(4):
        rc, out, err, m = run(h, [])
    assert "recurring" not in out, out
    st = json.load(open(os.path.join(h, "scripts/.vault_audit_state.json")))
    for i, d in enumerate(["2026-01-05", "2026-01-12"]):
        st["runs"][i]["date"] = d + " 10:30"
    json.dump(st, open(os.path.join(h, "scripts/.vault_audit_state.json"), "w"))
    rc, out, err, m = run(h, [])
    assert "recurring (3 of the last" in out, out


@case
def bad_state_shape_does_not_crash():
    h = make_home()
    open(os.path.join(h, "scripts/.vault_audit_state.json"), "w").write('{"runs": null}')
    rc, out, err, m = run(h, [])
    assert rc in (0, 1) and "recurrence state was unreadable" in out, (rc, out, err)
    rc, out, err, m = run(h, [])
    assert "recurrence state was unreadable" not in out


# ---------- parsers ----------
@case
def float_looking_version_kept_verbatim():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.10).\n"})
    open(os.path.join(h, "skills/cat/tool/SKILL.md"), "w").write("---\nname: tool\nversion: 1.10\n---\n")
    rc, out, err, m = run(h, ["--fix"])
    assert "(v1.10)" in open(vpath(h, "Hermes Agent/Skills/Tool.md")).read() and "v1.1 " not in out, out


@case
def vip_roster_line_not_the_first_mention():
    h = make_home()
    open(os.path.join(h, "skills/email-gmail-triage/SKILL.md"), "w").write(
        "---\nname: email-gmail-triage\n---\nThe VIP senders list is below.\n**VIP senders:** `mom@example.com` (Mom), `new@example.com` (New)\n")
    rc, out, err, m = run(h, [])
    assert "new@example.com" in out, out


@case
def curator_string_not_trusted():
    h = make_home()
    open(os.path.join(h, "config.yaml"), "w").write('model:\n  default: model-a\n  fallback: model-b\ncurator:\n  enabled: "false"\n')
    rc, out, err, m = run(h, ["--fix"])
    assert "not a boolean" in out and "`curator.enabled: false`" in open(vpath(h, "Hermes Agent/Operations.md")).read(), out


@case
def wikilink_forms_not_false_dangling():
    h = make_home(extra_notes={"Links.md": "[[Hermes Agent/Routines]] [[Routines.md]] | [[Routines\\|cron]] | `[[Nope]]`\n"
                                            "```\n[[AlsoNope]]\n```\n[[Really Missing]]\n"})
    rc, out, err, m = run(h, [])
    assert out.count("dangling wikilink") == 1 and "Really Missing" in out, out


@case
def archive_exact_names_in_section():
    h = make_home()
    os.makedirs(os.path.join(h, "skills/.archive/old"), exist_ok=True)       # "old" ⊂ "old-skill" textually
    rc, out, err, m = run(h, [])
    assert "archived skill 'old' not listed" in out, out


@case
def learned_rule_guards():
    h = make_home()
    m = load(h)
    with redirect_stderr(io.StringIO()):
        assert m.add_rule(json.dumps({"id": "k", "kind": "value_equals", "note": "Hermes Agent/Operations.md",
                                      "anchor": "x `([^`]+)`", "truth_source": "config", "truth_path": "model.api_key"})) == 2
        assert m.add_rule(json.dumps({"id": "r", "kind": "require_in_note", "note": "Home.md", "must": "zz", "sev": "auto"})) == 2
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "dup", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": r"\*\*`([^`]+)`\*\*",
         "truth_source": "config", "truth_path": "model.fallback", "sev": "auto"}]}))
    rc, out, err, m = run(h, ["--fix"])
    assert "matched 2 times" in out and "**`model-a`**" in open(vpath(h, "Hermes Agent/Operations.md")).read(), out


# ---------- write safety ----------
@case
def failed_write_downgrades_and_continues():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    d = vpath(h, "Hermes Agent/Skills")
    os.chmod(d, 0o555)
    try:
        rc, out, err, m = run(h, ["--fix"])
    finally:
        os.chmod(d, 0o755)
    assert rc == 1 and "auto-fix skipped: write failed" in out and "Vault Audit" in out, (rc, out, err)


@case
def note_mode_preserved():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    p = vpath(h, "Hermes Agent/Skills/Tool.md")
    os.chmod(p, 0o600)
    run(h, ["--fix"])
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


@case
def footer_counts_fixable():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    rc, out, err, m = run(h, [])
    assert "1 fixable" in out, out


@case
def clean_vault_is_clean():
    h = make_home()
    rc, out, err, m = run(h, ["--fix"])
    assert rc == 0 and "Vault in sync" in out, out


# ---------- gate lap 1: targeted cases for intended changes the first suite didn't pin ----------
@case
def empty_job_list_refuses_cleanup():
    h = make_home(jobs=[], out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02"])
    rc, out, err, m = run(h, ["--fix"])
    assert len(os.listdir(os.path.join(h, "cron/output"))) == 2 and "lists no jobs" in out, out


@case
def missing_jobs_json_is_unknown_not_empty():
    h = make_home(out_dirs=["aaaaaaaaaa01"])
    os.remove(os.path.join(h, "cron/jobs.json"))
    rc, out, err, m = run(h, [])
    assert "missing, unreadable" in out and "has no job in jobs.json" not in out, out


@case
def backup_mtime_is_this_run():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    p = vpath(h, "Hermes Agent/Skills/Tool.md")
    os.utime(p, (1, 1))
    rc, out, err, m = run(h, ["--fix"])
    assert os.path.getmtime(p + ".bak-audit-" + m.TS) > time.time() - 300


@case
def row_width_mismatch_is_reported():
    h = make_home(routines=ROUTINES.replace("| morning.py | [[Ops]] |", "| morning.py | [[Ops]] | extra |"))
    rc, out, err, m = run(h, [])
    assert "has 8 cells but the header has 7" in out, out


@case
def row_without_trailing_pipe_still_fixed():
    r = ROUTINES.replace("| morning.py | [[Ops]] |", "| morning.py | [[Ops]]")
    jobs = [dict(JOBS[0], schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=r)
    run(h, ["--fix"])
    row = [l for l in open(vpath(h, "Hermes Agent/Routines.md")).read().splitlines() if "aaaaaaaaaa01" in l][0]
    assert row.endswith("| morning.py | [[Ops]]") and "07:35 (`35 7 * * 2,3,4`)" in row, row


@case
def payload_flags_and_safe_error_lines():
    m = load(make_home())
    ok, bodies, clock = _delivery(m, ["valueerror", "ok"])
    assert ok and bodies[0]["flags"] == 4 and "SECRETPART" not in clock.err, clock.err


@case
def fid_keeps_digits_inside_ids():
    m = load(make_home())
    f = lambda d: m._fid(dict(check="c", target="t", detail=d))
    assert f("dir `177415f08261` x") != f("dir `288415f08261` x") and f("count 3") == f("count 4")


@case
def bom_frontmatter_version_read():
    h = make_home()
    open(os.path.join(h, "skills/cat/tool/SKILL.md"), "w", encoding="utf-8").write("﻿---\nname: tool\nversion: 1.4.0\n---\n")
    rc, out, err, m = run(h, [])
    assert "version note=v1.2.0 actual=v1.4.0" in out, out


@case
def family_section_at_end_of_note():
    h = make_home(extra_notes={"People/Anthony (Ham).md": "# Ham\nVIP `mom@example.com`. Channel 1000000000000000001.\n"
                                                         "## Family\n- Mom mom@example.com\n- Bro bro@example.com\n"})
    rc, out, err, m = run(h, [])
    assert "family email bro@example.com" in out, out


@case
def archive_listed_but_not_archived():
    h = make_home(extra_notes={"Hermes Agent/Skills/Skills MOC.md": "# MOC\n## Built by Ham\n- [[Tool]]\n\n## Bundled / Hermes Agent\n\n"
                                                                    "## Archived (in skills/.archive/)\n- `old-skill`, `ghost-skill`\n"})
    rc, out, err, m = run(h, [])
    assert "lists 'ghost-skill' but it is not in skills/.archive/" in out, out


@case
def secret_filter_checks_every_component():
    h = make_home()
    m = load(h)
    with redirect_stderr(io.StringIO()):
        assert m.add_rule(json.dumps({"id": "t", "kind": "value_equals", "note": "Home.md", "anchor": "x `([^`]+)`",
                                      "truth_source": "config", "truth_path": "auth.token.value"})) == 2
    assert m._resolve_truth("config", "auth.token.value") is None


@case
def legacy_auto_require_rule_is_review():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "req", "kind": "require_in_note", "note": "Home.md", "must": "never-there", "sev": "auto"}]}))
    rc, out, err, m = run(h, ["--fix"])
    assert "Fixable drift" not in out and "[req] missing required text" in out.split("Review")[1], out


@case
def unreadable_rules_file_is_reported():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write('{"rules": [')
    rc, out, err, m = run(h, [])
    assert "learned_rules.json is unreadable" in out, out


@case
def one_failing_fix_does_not_stop_the_rest():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n",
                               "Hermes Agent/Operations.md": OPS.replace("**`model-b`**", "**`model-x`**")})
    m = load(h)
    real, n = m.apply_fix, []
    def flaky(*a):
        n.append(1)
        if len(n) == 1:
            raise RuntimeError("boom")
        return real(*a)
    m.apply_fix = flaky
    m.urllib.request.urlopen, saved = _no_network, m.urllib.request.urlopen
    try:
        with redirect_stdout(io.StringIO()) as o:
            rc = m.main(["--fix"])
    finally:
        m.urllib.request.urlopen = saved
    out = o.getvalue()
    assert rc == 1 and "auto-fix skipped: RuntimeError" in out and "Auto-fixed (1)" in out, out


@case
def atomic_write_leaves_no_tmp_on_failure():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    m = load(h)
    real = m.os.replace
    m.os.replace = lambda a, b: (_ for _ in ()).throw(OSError("disk full"))
    try:
        ok, err = m.apply_fix("Hermes Agent/Skills/Tool.md", "(v1.0.0)", "(v1.2.0)")
    finally:
        m.os.replace = real
    left = [f for f in os.listdir(vpath(h, "Hermes Agent/Skills")) if ".tmp-audit-" in f]
    assert not ok and not left, (ok, err, left)


@case
def internal_error_exits_4_with_trace():
    h = make_home()
    m = load(h)
    m.run_checks = lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))
    with redirect_stdout(io.StringIO()) as o:
        rc = m.main([])
    assert rc == 4 and "internal error" in o.getvalue() and "kaboom" in o.getvalue()


@case
def nonstandard_paused_markers():
    r = ROUTINES.replace("| Morning Thing |", "| Morning Thing [PAUSED] |")       # active job, loose marker
    r = r.replace("Paused Thing **[PAUSED]**", "Paused Thing [PAUSED]")            # paused job, loose marker
    h = make_home(routines=r)
    rc, out, err, m = run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Routines.md")).read()
    assert "`aaaaaaaaaa01` is active in jobs.json but its row still says paused" in out, out
    row = [l for l in t.splitlines() if "bbbbbbbbbb02" in l][0]
    assert row.count("PAUSED") == 1 and "**[PAUSED]**" in row, row


# ---------- gate lap 1 (adversarial): one case per reproduced break ----------
@case
def lookahead_rule_cannot_splice():
    h = make_home(extra_notes={"Hermes Agent/Operations.md": OPS + "- timeout `script_timeout_seconds: 300`\n"})
    open(os.path.join(h, "config.yaml"), "a").write("cron:\n  script_timeout_seconds: 600\n")
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "la", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": "script_timeout_seconds: (?=(\\d+))",
         "truth_source": "config", "truth_path": "cron.script_timeout_seconds", "sev": "auto"}]}))
    for _ in range(2):
        run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Operations.md")).read()
    assert "script_timeout_seconds: 300`" in t, t


@case
def partially_malformed_jobs_are_unknown():
    for bad in ({"id": ""}, {"id": "aaaaaaaaaa01 "}, 7, {"name": "no id"}):
        base = {k: v for k, v in JOBS[0].items() if k != "id"}
        jobs = [dict(base, **bad) if isinstance(bad, dict) else bad] + JOBS[1:]
        h = make_home(jobs=jobs, out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03"])
        rc, out, err, m = run(h, ["--fix"])
        assert len(os.listdir(os.path.join(h, "cron/output"))) == 3 and "missing, unreadable" in out, (bad, out)
        assert "has no job in jobs.json" not in out, out


@case
def secret_filter_word_anywhere():
    m = load(make_home())
    for path in ("GITHUB_TOKEN_RO", "SLACK_WEBHOOK_URL", "mcp.headers.Authorization", "DISCORD_BOT_TOKEN ", "a..b"):
        assert m._is_secret_key(path), path
    assert not m._is_secret_key("model.default") and not m._is_secret_key("DISCORD_HOME_CHANNEL")


@case
def only_the_jobs_table_is_edited():
    extra = ("\n## Notes\n| ID | Reason | Resume when |\n|---|---|---|\n| `bbbbbbbbbb02` | evenings busy since 23:51 | later |\n"
             "\n| Job ID | Schedule | Name |\n|---|---|---|\n| `aaaaaaaaaa01` | daily 07:28 (`28 7 * * *`) | x |\n| `cccccccccc03` |\n")
    jobs = [JOBS[0], dict(JOBS[1], enabled=True, paused_at=None), JOBS[2]]
    h = make_home(jobs=jobs, routines=(ROUTINES + extra).replace("**2 active + 1 paused**", "**3 active + 0 paused**"))
    rc, out, err, m = run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Routines.md")).read()
    assert extra.replace("\n", "") in t.replace("\n", "") and "raised" not in out, (out, t)
    assert "remove [PAUSED] marker" in out, out                       # the real table is still checked


@case
def hm_fix_never_hits_the_wrong_token():
    for gloss in ("48:00 cadence, cron daily 07:28", "since 2026-09-01T17:30:00, daily 07:28"):
        h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25", gloss))
        run(h, ["--fix"])
        row = [l for l in open(vpath(h, "Hermes Agent/Routines.md")).read().splitlines() if "aaaaaaaaaa01" in l][0]
        assert gloss.replace("07:28", "07:25") in row, row


@case
def unreconcilable_time_after_expr_fix_is_reported():
    jobs = [dict(JOBS[0], schedule={"expr": "0 8 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=ROUTINES.replace("Tue/Wed/Thu 07:25", "07:25 / 07:35"))
    for _ in range(2):                                                   # must STILL be reported next week
        rc, out, err, m = run(h, ["--fix"])
        assert "fix the row by hand" in out and "`25 7 * * 2,3,4`" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out
    jobs = [dict(JOBS[0], schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]          # the text already shows the new time
    h = make_home(jobs=jobs, routines=ROUTINES.replace("07:25 (`25 7", "07:35 (`25 7"))
    rc, out, err, m = run(h, ["--fix"])
    assert "fix the row by hand" not in out and "Review" not in out and "07:35 (`35 7 * * 2,3,4`)" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def risky_rule_regex_refused_and_skipped():
    h = make_home()
    m = load(h)
    with redirect_stderr(io.StringIO()):
        assert m.add_rule(json.dumps({"id": "r", "kind": "forbid_in_note", "note": "Home.md", "pattern": "(\\w+\\s?)+!!!"})) == 2
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "r", "kind": "forbid_in_note", "note": "Home.md", "pattern": "(\\w+\\s?)+!!!"}]}))
    rc, out, err, m = run(h, [])
    assert "nested quantifier" in out, out
    for pat in ("(?:(\\w+)\\s?)+!!!", "((\\w+\\s?))+!!!", "(a|aa)+$", "(x+)+y"):
        assert m._risky_regex(pat), pat
    for pat in ("provider `([\\w-]+)`", "\\*\\*`([^`]+)`\\*\\*", "(\\w+) *", "(\\d{2}){2}"):
        assert not m._risky_regex(pat), pat


@case
def corrupt_rules_file_is_never_overwritten():
    h = make_home()
    rf = os.path.join(h, "scripts/learned_rules.json")
    open(rf, "w").write('{"rules": [{"id": "keep"')
    rc, out, err, m = run(h, ["--list-rules"])
    assert rc == 2
    rc, out, err, m = run(h, ["--learn-rule", json.dumps({"id": "n", "kind": "require_in_note", "note": "Home.md", "must": "x"})])
    assert rc == 2 and open(rf).read() == '{"rules": [{"id": "keep"', open(rf).read()
    open(rf, "w").write('{"rules": "oops"}')
    rc, out, err, m = run(h, [])
    assert "learned_rules.json is unreadable" in out, out


@case
def unlistable_quarantine_does_not_abort():
    h = make_home(extra_notes={"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.0.0).\n"})
    q = os.path.join(h, "scripts/.vault_audit_orphans")
    os.makedirs(q)
    os.chmod(q, 0)
    try:
        rc, out, err, m = run(h, ["--fix"])
    finally:
        os.chmod(q, 0o755)
    assert rc in (0, 1) and "old quarantine not pruned" in out and "Auto-fixed (1)" in out, (rc, out)


@case
def unreadable_note_is_still_a_link_target():
    h = make_home()
    p = vpath(h, "Hermes Agent/Routines.md")
    os.chmod(p, 0)
    try:
        rc, out, err, m = run(h, [])
    finally:
        os.chmod(p, 0o644)
    assert "dangling wikilink [[Routines]]" not in out and "could not read" in out, out


@case
def unreadable_env_is_reported_not_guessed():
    h = make_home()
    e = os.path.join(h, ".env")
    os.chmod(e, 0)
    try:
        rc, out, err, m = run(h, [])
    finally:
        os.chmod(e, 0o644)
    assert "could not read ~/.hermes/.env" in out and "not a key in .env" not in out, out


@case
def odd_state_dates_do_not_count():
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| old.py |"))
    rc, out, err, m = run(h, [])
    st = json.load(open(os.path.join(h, "scripts/.vault_audit_state.json")))
    fid = st["runs"][-1]["ids"][0]
    st["runs"] = [{"date": d, "ids": [fid]} for d in (None, 5, "2099-01-01 10:00", "garbage", "")]
    json.dump(st, open(os.path.join(h, "scripts/.vault_audit_state.json"), "w"))
    rc, out, err, m = run(h, [])
    assert "recurring" not in out, out


@case
def empty_post_arg_is_an_error():
    rc, out, err, m = run(make_home(), ["--post", ""], post=lambda n: True)
    assert rc == 2 and not m.post_calls


@case
def per_attempt_timeout_fits_budget():
    m = load(make_home())
    seen = []
    clock = _Clock()
    clock.t = 0.0
    def urlopen(req, timeout=None):
        seen.append(timeout)
        clock.t += timeout
        raise TimeoutError()
    saved = (m.urllib.request.urlopen, m.time.monotonic, m.time.sleep)
    m.urllib.request.urlopen, m.time.monotonic, m.time.sleep = urlopen, clock.mono, clock.sleep
    try:
        with redirect_stderr(io.StringIO()):
            ok = m._post_chunk("https://discord.com/api/v10/channels/1/messages", "t", "c", "n", deadline=40.0)
    finally:
        m.urllib.request.urlopen, m.time.monotonic, m.time.sleep = saved
    assert not ok and clock.t <= 40.0 + 1e-6 and all(t <= 30 for t in seen), (clock.t, seen)


# ---------- gate lap 2: one case per reproduced break / surviving mutant ----------
@case
def short_row_does_not_crash():
    h = make_home(routines=ROUTINES + "| `cccccccccc03` |\n")
    rc, out, err, m = run(h, [])
    assert "raised" not in out, out


@case
def missing_jobs_table_header_is_reported():
    h = make_home(routines=ROUTINES.replace("| ID | Name | Schedule (SGT) |", "| Job | Title | When |"))
    rc, out, err, m = run(h, [])
    assert "could not find the jobs table" in out, out


@case
def decisions_record_never_rewritten():
    h = make_home()
    open(os.path.join(h, "config.yaml"), "w").write("model:\n  default: model-a\n  fallback: model-b\ncurator:\n  enabled: true\n")
    rc, out, err, m = run(h, ["--fix"])
    for note in ("Decisions.md", "Operations.md"):                  # report-only in both: never rewritten
        assert "`curator.enabled: false`" in open(vpath(h, "Hermes Agent/" + note)).read(), note
    assert out.count("curator.enabled is true in config.yaml") == 2, out


@case
def secret_path_rule_is_reported_not_silent():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "s", "kind": "value_equals", "note": "Home.md", "anchor": "x `([^`]+)`", "truth_source": "env", "truth_path": "BROWSER_SESSION_TIMEOUT"}]}))
    rc, out, err, m = run(h, [])
    assert "rule [s] skipped" in out, out


@case
def paused_word_in_active_name_is_not_a_marker():
    h = make_home(routines=ROUTINES.replace("| Morning Thing |", "| Paused-job sweeper |"))
    rc, out, err, m = run(h, [])
    assert "still says paused" not in out, out
    h = make_home(routines=ROUTINES.replace("| morning.py |", "| morning.py (PAUSED) |"))
    rc, out, err, m = run(h, [])
    assert "`aaaaaaaaaa01` is active in jobs.json but its row still says paused" in out, out


@case
def vip_line_that_parses_to_nothing_is_reported():
    h = make_home()
    open(os.path.join(h, "skills/email-gmail-triage/SKILL.md"), "w").write("---\nname: email-gmail-triage\n---\n**VIP senders:** mom@example.com\n")
    rc, out, err, m = run(h, [])
    assert "no `email` (Name) entries" in out, out


@case
def vip_skill_found_in_a_category_folder():
    h = make_home()
    os.makedirs(os.path.join(h, "skills/productivity"), exist_ok=True)
    shutil.move(os.path.join(h, "skills/email-gmail-triage"), os.path.join(h, "skills/productivity/email-gmail-triage"))
    open(os.path.join(h, "skills/productivity/email-gmail-triage/SKILL.md"), "a").write("")
    with open(os.path.join(h, "skills/productivity/email-gmail-triage/SKILL.md"), "w") as f:
        f.write("---\nname: email-gmail-triage\n---\n**VIP senders:** `mom@example.com` (Mom), `new@example.com` (New)\n")
    rc, out, err, m = run(h, [])
    assert "new@example.com" in out, out


@case
def unknown_rule_kind_is_reported():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [{"id": "k", "kind": "forbid_in_notes", "note": "Home.md"}]}))
    rc, out, err, m = run(h, [])
    assert "rule [k] skipped: unknown kind" in out, out


@case
def crash_line_names_the_check():
    h = make_home()
    m = load(h)
    m.CHECKS[2] = m.check_crons = (lambda: 1 / 0)
    m.CHECKS[2].__name__ = "check_crons"
    with redirect_stdout(io.StringIO()) as o:
        m.main([])
    assert "check_crons raised ZeroDivisionError" in o.getvalue(), o.getvalue()


@case
def dangling_symlink_note_is_not_a_target():
    h = make_home(extra_notes={"Notes.md": "[[Gone]]\n"})
    os.symlink(vpath(h, "nowhere.md"), vpath(h, "Gone.md"))
    rc, out, err, m = run(h, [])
    assert "dangling wikilink [[Gone]]" in out, out


@case
def rules_file_without_rules_key_is_empty():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write("{}")
    rc, out, err, m = run(h, ["--list-rules"])
    assert rc == 0
    rc, out, err, m = run(h, [])
    assert "unreadable" not in out, out


@case
def header_without_trailing_pipe():
    r = ROUTINES.replace("| ID | Name | Schedule (SGT) | Script | Skill / deliver |", "| ID | Name | Schedule (SGT) | Script | Skill / deliver")
    jobs = [dict(JOBS[0], schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=r)
    rc, out, err, m = run(h, ["--fix"])
    assert "07:35 (`35 7 * * 2,3,4`)" in open(vpath(h, "Hermes Agent/Routines.md")).read() and "cells" not in out, out


@case
def reread_guard_alone_blocks_bad_splice():
    h = make_home(extra_notes={"Hermes Agent/Operations.md": OPS + "- engine v1\n"})
    open(os.path.join(h, "config.yaml"), "a").write("engine: 12\n")
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "v", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": "engine v(\\d)",
         "truth_source": "config", "truth_path": "engine", "sev": "auto"}]}))
    for _ in range(2):
        rc, out, err, m = run(h, ["--fix"])
    assert "- engine v1\n" in open(vpath(h, "Hermes Agent/Operations.md")).read(), out


@case
def unreadable_vip_file_is_reported():
    h = make_home()
    p = os.path.join(h, "skills/email-gmail-triage/SKILL.md")
    os.chmod(p, 0)
    try:
        rc, out, err, m = run(h, [])
    finally:
        os.chmod(p, 0o644)
    assert "could not read the VIP roster" in out, out


@case
def non_hex_job_ids_skip_orphan_cleanup():
    jobs = JOBS + [{"id": "my-custom-job", "name": "x", "schedule": {"expr": "0 1 * * *"}, "script": "x.py", "enabled": True}]
    h = make_home(jobs=jobs, out_dirs=["aaaaaaaaaa01", "bbbbbbbbbb02", "cccccccccc03", "dddddddddd04"])
    rc, out, err, m = run(h, ["--fix"])
    assert "dddddddddd04" in os.listdir(os.path.join(h, "cron/output")) and "orphan cleanup skipped" in out, out


@case
def surrogate_names_cannot_crash():
    m = load(make_home())
    f = dict(check="links", sev="review", target="bad\udcff.md", detail="dangling wikilink [[x\udcfe]]", fix=None, action=None, applied=False)
    assert m._fid(f) and "\\udcff" in m._line(f)


# ---------- gate lap 3: one case per reproduced break / surviving mutant ----------
def _drifted(expr="0 8 * * 2,3,4"):
    return [dict(JOBS[0], schedule={"expr": expr})] + JOBS[1:]


@case
def finder_ignores_code_fence_and_retired_tables():
    tpl = "```\n| ID | Name | Schedule | Script |\n|---|---|---|---|\n| `0123456789ab` | Example | daily 07:00 (`0 7 * * *`) | x.py |\n```\n"
    retired = "## Retired\n| ID | Name | Schedule | Script |\n|---|---|---|---|\n| `abcabcabc123` | Old | daily 06:00 (`0 6 * * *`) | old.py |\n\n"
    h = make_home(jobs=_drifted("35 7 * * 2,3,4"), routines=ROUTINES.replace("# Routines\n", "# Routines\n" + tpl + retired))
    rc, out, err, m = run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Routines.md")).read()
    assert "07:35 (`35 7 * * 2,3,4`)" in t and "0123456789ab" not in out and tpl in t, out
    assert "`abcabcabc123` (Old) has no job" in out, out


@case
def blank_line_inside_table_rows_still_checked():
    r = ROUTINES.replace("| `bbbbbbbbbb02`", "\n| `bbbbbbbbbb02`")
    jobs = [JOBS[0], dict(JOBS[1], enabled=True, paused_at=None), JOBS[2]]
    h = make_home(jobs=jobs, routines=r.replace("**2 active + 1 paused**", "**3 active + 0 paused**"))
    rc, out, err, m = run(h, [])
    assert "`bbbbbbbbbb02` remove [PAUSED] marker" in out, out


@case
def job_only_in_a_foreign_table_is_missing():
    r = ROUTINES.replace("| `cccccccccc03` | Hourly | every hour, 24/7 (`0 * * * *`) | hourly.py | [[Ops]] |\n", "") + \
        "\n| ID | Owner |\n|---|---|\n| `cccccccccc03` | Ham |\n"
    h = make_home(routines=r)
    rc, out, err, m = run(h, [])
    assert "active job `cccccccccc03` (Hourly) missing from Routines table" in out, out


@case
def decoy_backtick_span_never_rewritten():
    for cell in ("Tue/Wed/Thu 07:25 `25 7 * * 2,3,4` (`after aaaaaaaaaa01`)", "after (`morning.py`) at 07:25 (`25 7 * * 2,3,4`)"):
        h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", cell))
        rc, out, err, m = run(h, ["--fix"])
        assert cell in open(vpath(h, "Hermes Agent/Routines.md")).read() and "Auto-fixed" not in out, (cell, out)


@case
def spaces_inside_backticks_still_fixed():
    h = make_home(jobs=_drifted("35 7 * * 2,3,4"), routines=ROUTINES.replace("07:25 (`25 7 * * 2,3,4`)", "07:25 ` 25 7 * * 2,3,4 `"))
    rc, out, err, m = run(h, ["--fix"])
    assert "07:35 `35 7 * * 2,3,4`" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def correct_ampm_time_is_quiet():
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7", "Tue/Wed/Thu 7:25am (`25 7"))
    for _ in range(2):
        rc, out, err, m = run(h, ["--fix"])
        assert "Review" not in out and "Auto-fixed" not in out, out
    h = make_home(jobs=_drifted("35 7 * * 2,3,4"), routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7", "Tue/Wed/Thu 7:25am (`25 7"))
    rc, out, err, m = run(h, ["--fix"])
    assert "fix the row by hand" in out, out


@case
def time_drift_without_expr_drift_reported():
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25", "07:28 or 07:40 (`25"))
    rc, out, err, m = run(h, ["--fix"])
    assert "fix the row by hand" in out, out


@case
def unverifiable_value_rule_is_reported():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "gone", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": "x `([^`]+)`", "truth_source": "config", "truth_path": "nope.missing"},
        {"id": "noanchor", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": "zzz `([^`]+)`", "truth_source": "config", "truth_path": "model.default"}]}))
    rc, out, err, m = run(h, [])
    assert "rule [gone] not verified" in out and "rule [noanchor] not verified: anchor not found" in out, out


@case
def slow_learned_rule_is_time_boxed():
    words = " ".join("word%d" % i for i in range(1500))
    h = make_home(extra_notes={"Big.md": words + "\n"})
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "slow", "kind": "forbid_in_note", "note": "Big.md", "pattern": "\\w+.*\\w+.*\\w+.*=Q"}]}))
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "quick", "kind": "require_in_note", "note": "Big.md", "must": "never-there"},
        {"id": "slow", "kind": "forbid_in_note", "note": "Big.md", "pattern": "\\w+.*\\w+.*\\w+.*=Q"}]}))
    assert not load(h)._risky_regex("\\w+.*\\w+.*\\w+.*=Q")        # premise: the static check can't see it
    code = ("import sys; sys.path.insert(0, %r); import vault_audit as v; v.RULES_TIMEOUT = 3; "
            "import urllib.request; urllib.request.urlopen = None; sys.exit(v.main([]))" % os.path.dirname(ENGINE))
    p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, HERMES_HOME=h), capture_output=True, text=True, timeout=40)
    assert "learned rule [slow] took > 3s" in p.stdout and "[quick] missing required text" in p.stdout, p.stdout


@case
def bounded_around_unbounded_regex_refused():
    m = load(make_home())
    for pat in ("(?:\\s*\\w+\\s*){1,20}:x", "(\\w+\\s?){1,10}!", "(?:a+){2,40}b"):
        assert m._risky_regex(pat), pat


@case
def wrong_ampm_time_is_reported():
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25", "Tue/Wed/Thu 9:10am (`25"))
    rc, out, err, m = run(h, ["--fix"])
    assert "fix the row by hand" in out and "9:10am" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def multi_expr_cell_reported_and_time_left_alone():
    cell = "Tue/Wed/Thu 09:00 (`0 0 * * *`) was `30 23 * * *`"
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", cell))
    rc, out, err, m = run(h, ["--fix"])
    assert "has 2 exprs" in out and cell in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def invalid_time_text_is_not_a_time():
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25", "Tue/Wed/Thu 31:25 (`25"))
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out, out


# ---------- gate lap 4: one case per reproduced break / surviving mutant ----------
@case
def gfm_separators_and_headings_end_the_jobs_table():
    jobs = [dict(JOBS[0], schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]
    for sep in ("|-|-|-|-|-|", "| -- | -- | -- | -- | -- |", "|:-:|:-:|:-:|:-:|:-:|", "|:--|:--|:--|:--|:--|"):
        hist = ("\n## Schedule history\n| ID | Name | Old schedule | Changed | Why |\n" + sep + "\n"
                "| `aaaaaaaaaa01` | Morning (v1) | `30 7 * * *` | 2026-05-01 | moved earlier |\n")
        h = make_home(jobs=jobs, routines=ROUTINES + hist)
        rc, out, err, m = run(h, ["--fix"])
        t = open(vpath(h, "Hermes Agent/Routines.md")).read()
        assert hist in t and "07:35 (`35 7 * * 2,3,4`)" in t and "Script column says `2026" not in out, (sep, out)
    hist = "\nNotes below.\nID | Name | Schedule | Script\n---|---|---|---\n`aaaaaaaaaa01` | x | daily 06:00 (`0 6 * * *`) | y\n"
    h = make_home(routines=ROUTINES + hist)
    rc, out, err, m = run(h, ["--fix"])
    assert hist in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def horizontal_rule_after_last_row_is_not_a_header():
    jobs = JOBS[:2] + [dict(JOBS[2], schedule={"expr": "30 * * * *"})]
    h = make_home(jobs=jobs, routines=ROUTINES + "---\n")
    rc, out, err, m = run(h, [])
    assert "'0 * * * *' -> '30 * * * *'" in out and "missing from Routines" not in out, out


@case
def named_cron_tokens_compared_not_edited():
    jobs = [dict(JOBS[0], schedule={"expr": "25 7 * * 2-4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=ROUTINES.replace("(`25 7 * * 2,3,4`)", "(`25 7 * * TUE-THU`)"))
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out and "Auto-fixed" not in out, out                  # equivalent: quiet
    h = make_home(jobs=[dict(JOBS[0], schedule={"expr": "25 7 * * 2-4"})] + JOBS[1:],
                  routines=ROUTINES.replace("07:25 (`25 7 * * 2,3,4`)", "07:28 (`25 7 * * TUE-THU`)"))
    run(h, ["--fix"])
    assert "07:25 (`25 7 * * TUE-THU`)" in open(vpath(h, "Hermes Agent/Routines.md")).read()   # time still kept right
    jobs = [dict(JOBS[0], schedule={"expr": "0 3 * * *"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", "daily (`@daily`)"))
    rc, out, err, m = run(h, ["--fix"])
    assert "a named expr (`@daily`) — keep one current expr, `0 3 * * *`" in out and "(`@daily`)" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def fences_follow_commonmark():
    tpl = "~~~\n```\n| ID | Name | Schedule | Script |\n|---|---|---|---|\n| `aaaaaaaaaa01` | Example | daily 06:00 (`0 6 * * *`) | x.py |\n```\n~~~\n"
    h = make_home(routines=ROUTINES.replace("# Routines\n", "# Routines\n" + tpl))
    rc, out, err, m = run(h, ["--fix"])
    assert tpl in open(vpath(h, "Hermes Agent/Routines.md")).read() and "Auto-fixed" not in out, out
    h = make_home(routines=ROUTINES.replace("# Routines\n", "# Routines\n```inline``` code mention\n"))
    rc, out, err, m = run(h, [])
    assert "could not find the jobs table" not in out, out


@case
def every_anchor_match_must_agree():
    h = make_home(extra_notes={"Hermes Agent/Operations.md": OPS + "- timeout `t: 600`\n- old copy `t: 120`\n"})
    open(os.path.join(h, "config.yaml"), "a").write("t: 600\n")
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "dup", "kind": "value_equals", "note": "Hermes Agent/Operations.md", "anchor": "`t: (\\d+)`",
         "truth_source": "config", "truth_path": "t", "sev": "auto"}]}))
    rc, out, err, m = run(h, ["--fix"])
    assert "note='120' actual='600'" in out and "`t: 120`" in open(vpath(h, "Hermes Agent/Operations.md")).read(), out


@case
def crashed_rules_child_is_reported():
    h = make_home()
    open(os.path.join(h, "scripts/learned_rules.json"), "w").write(json.dumps({"rules": [
        {"id": "r", "kind": "require_in_note", "note": "Home.md", "must": "zz"}]}))
    m = load(h)
    bad = os.path.join(h, "scripts", "boom.py")
    open(bad, "w").write("import sys; sys.exit(3)\n")
    m.__file__ = bad
    with redirect_stdout(io.StringIO()) as o:
        m.main([])
    assert "learned rules could not be evaluated" in o.getvalue(), o.getvalue()


@case
def no_expr_cell_time_not_rewritten():
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", "Tue/Wed/Thu 07:28"))
    rc, out, err, m = run(h, ["--fix"])
    assert "Tue/Wed/Thu 07:28 |" in open(vpath(h, "Hermes Agent/Routines.md")).read() and "fix the row by hand" in out, out


@case
def pm_times_are_converted():
    jobs = [dict(JOBS[0], schedule={"expr": "25 19 * * 2,3,4"})] + JOBS[1:]
    h = make_home(jobs=jobs, routines=ROUTINES.replace("07:25 (`25 7 * * 2,3,4`)", "7:25pm (`25 19 * * 2,3,4`)"))
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out, out
    h = make_home(routines=ROUTINES.replace("07:25 (`25 7 * * 2,3,4`)", "7:25pm (`25 7 * * 2,3,4`)"))
    rc, out, err, m = run(h, ["--fix"])
    assert "fix the row by hand" in out, out


@case
def right_plus_stale_exprs_reported():
    cell = "07:25 (`25 7 * * 2,3,4`) was `30 6 * * *`"
    h = make_home(routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", cell))
    rc, out, err, m = run(h, ["--fix"])
    assert "has 2 exprs" in out and cell in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def five_word_decoy_is_not_an_expr():
    jobs = [dict(JOBS[0], schedule={"expr": "35 7 * * 2,3,4"})] + JOBS[1:]
    cell = "07:25 (`25 7 * * 2,3,4`) (`after the backup job runs`)"
    h = make_home(jobs=jobs, routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", cell))
    run(h, ["--fix"])
    t = open(vpath(h, "Hermes Agent/Routines.md")).read()
    assert "07:35 (`35 7 * * 2,3,4`) (`after the backup job runs`)" in t, t



@case
def day_field_change_goes_to_review_and_stays():
    jobs = [dict(JOBS[0], schedule={"expr": "25 7 * * *"})] + JOBS[1:]
    h = make_home(jobs=jobs)
    for _ in range(2):
        rc, out, err, m = run(h, ["--fix"])
        assert "(the schedule changed) — fix the row by hand" in out, out
        assert "Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)" in open(vpath(h, "Hermes Agent/Routines.md")).read()



# ---------- gate lap 5 ----------
@case
def hour_change_next_to_schedule_words_goes_to_review():
    for gloss, old, new in (("every hour, 24/7", "0 * * * *", "0 8-21 * * *"), ("hourly, 08-21", "0 8-21 * * *", "0 7-22 * * *"),
                            ("every 2h", "0 */2 * * *", "0 */3 * * *")):
        jobs = JOBS[:2] + [dict(JOBS[2], schedule={"expr": new})]
        cell = "%s (`%s`)" % (gloss, old)
        h = make_home(jobs=jobs, routines=ROUTINES.replace("every hour, 24/7 (`0 * * * *`)", cell))
        for _ in range(2):
            rc, out, err, m = run(h, ["--fix"])
            assert "(the schedule changed) — fix the row by hand" in out and cell in open(vpath(h, "Hermes Agent/Routines.md")).read(), (gloss, out)


@case
def minute_only_change_on_hourly_job_still_fixed():
    jobs = JOBS[:2] + [dict(JOBS[2], schedule={"expr": "30 * * * *"})]
    h = make_home(jobs=jobs)
    rc, out, err, m = run(h, ["--fix"])
    assert "every hour, 24/7 (`30 * * * *`)" in open(vpath(h, "Hermes Agent/Routines.md")).read(), out


@case
def equivalent_cron_fields_are_equal():
    m = load(make_home())
    assert m._cron_equal("25 7 * * 2-4", "25 7 * * 2,3,4") and m._cron_equal("0 9 * * 7", "0 9 * * 0")
    assert m._cron_equal("0 9 * * MON-FRI", "0 9 * * 1-5") and not m._cron_equal("0 9 * * 1-5", "0 9 * * 1-6")
    h = make_home(routines=ROUTINES.replace("(`25 7 * * 2,3,4`)", "(`25 7 * * 2-4`)"))
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out and "Auto-fixed" not in out, out
    jobs = [dict(JOBS[0], schedule={"expr": "25 7 * * TUE-THU"})] + JOBS[1:]          # named on the job side
    h = make_home(jobs=jobs)
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out and "Auto-fixed" not in out, out


@case
def header_without_leading_pipe_keeps_columns():
    r = ROUTINES.replace("| ID | Name | Schedule (SGT) | Script | Skill / deliver |", "ID | Name | Schedule (SGT) | Script | Skill / deliver")
    r = r.replace("|----|------|----------------|--------|-----------------|", "---|---|---|---|---")
    jobs = [JOBS[0], dict(JOBS[1]), JOBS[2]]
    h = make_home(jobs=jobs, routines=r)
    rc, out, err, m = run(h, ["--fix"])
    assert "Review" not in out and "Script column" not in out, out



# ---------- gate lap 6 LOWs ----------
@case
def other_time_spellings_block_the_expr_fix():
    for gloss in ("daily 8am", "daily 7.30", "every evening", "every hour on the hour", "hourly at :00"):
        jobs = [dict(JOBS[0], schedule={"expr": "0 20 * * 2,3,4"})] + JOBS[1:]
        cell = "%s (`25 7 * * 2,3,4`)" % gloss
        h = make_home(jobs=jobs, routines=ROUTINES.replace("Tue/Wed/Thu 07:25 (`25 7 * * 2,3,4`)", cell))
        rc, out, err, m = run(h, ["--fix"])
        assert cell in open(vpath(h, "Hermes Agent/Routines.md")).read() and "fix the row by hand" in out, (gloss, out)


@case
def job_refs_compare_semantically():
    extra = {"Hermes Agent/Skills/Tool.md": "**Built by Ham.** Skill: `~/.hermes/skills/cat/tool` (v1.2.0).\n- Morning `aaaaaaaaaa01` - `25 7 * * 2-4`\n"}
    rc, out, err, m = run(make_home(extra_notes=extra), [])
    assert "aaaaaaaaaa01" not in out, out


@case
def huge_digit_field_does_not_crash():
    m = load(make_home())
    big = "9" * 5000
    assert m._field_set(big) is None and m._expr_hm("%s 7 * * *" % big) is None and not m._cron_equal(big + " 7 * * *", "0 7 * * *")


if __name__ == "__main__":
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, msg in RESULTS:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  — " + msg[:300]) if msg else ""))
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    sys.exit(1 if failed else 0)
