#!/usr/bin/env python3
"""vault_audit.py — deterministic accuracy audit for Hermes's Obsidian vault.

Compares what the vault NOTES claim against the live GROUND TRUTH on the Pi
(config.yaml, cron/jobs.json, installed SKILL.md frontmatter, .env key names,
the email-gmail-triage VIP roster). Pure Python — zero LLM tokens.

Two severities:
  - auto   : machine-verifiable + safely fixable in place (model names, cron
             schedule expr (+ the HH:MM beside it), cron [PAUSED] marker, skill
             versions, Skills-MOC grouping, orphaned cron output dirs). Applied
             only with --fix, always after a per-file .bak-audit-<ts> backup
             (orphaned output dirs are MOVED to a 30-day quarantine, never
             deleted outright), and only when the stale token is found EXACTLY
             ONCE (else downgraded to review). The file is re-validated after
             each edit.
  - review : flagged for a human/LLM (VIP roster, dangling wikilinks, missing
             skill paths, env-key drift, archive bookkeeping, channel IDs, cron
             script/name/count drift, retired job ids in notes, and any check
             that could NOT run — a missing note or an unreadable jobs.json is
             reported, never silently passed). Never auto-edited.

Usage:
  vault_audit.py                 # detect, print report to stdout (no edits, no post)
  vault_audit.py --fix           # also apply the `auto` fixes
  vault_audit.py --post CHANNEL  # self-post report to a Discord channel (only if findings)
  vault_audit.py --post-home     # post to DISCORD_HOME_CHANNEL
  vault_audit.py --quiet         # suppress stdout unless delivery fails
Exit code: 0 = clean or all drift auto-fixed; 1 = anything unresolved (unfixed drift or review items);
2 = bad CLI input; 3 = a requested Discord post was not (fully) delivered; 4 = internal error.
"""
import argparse, datetime, glob, hashlib, json, math, os, re, shutil, subprocess, sys, time, traceback, urllib.request, urllib.error

import yaml  # PyYAML 6.x, present in the hermes venv
try:
    from re import _parser as _sre_parse   # 3.11+ (sre_parse is deprecated there)
except ImportError:                        # pragma: no cover — older interpreters
    import sre_parse as _sre_parse

# realpath: _vault_path() compares realpath()s against VAULT, so VAULT itself must be
# canonical — a symlink anywhere in the path used to make every read() "escape the vault"
# and silently skip most checks.
HOME = os.path.realpath(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
VAULT = os.path.realpath(os.path.join(HOME, "memories", "ObsidianHam"))
HA = os.path.join(VAULT, "Hermes Agent")
SKILLS_NOTES = os.path.join(HA, "Skills")
CONFIG = os.path.join(HOME, "config.yaml")
ENVF = os.path.join(HOME, ".env")
JOBS = os.path.join(HOME, "cron", "jobs.json")
CRON_OUT = os.path.join(HOME, "cron", "output")
INSTALL = os.path.join(HOME, "skills")
ARCHIVE = os.path.join(INSTALL, ".archive")
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Feedback-loop state (kept beside the engine so both the live and skill-dir copies share it)
RULES_FILE = os.path.join(HOME, "scripts", "learned_rules.json")   # data-driven, learned drift rules
STATE_FILE = os.path.join(HOME, "scripts", ".vault_audit_state.json")  # recurrence log
ORPHAN_TRASH = os.path.join(HOME, "scripts", ".vault_audit_orphans")   # quarantined cron output dirs
RECUR_WINDOW = 6      # look back over this many distinct audit DAYS
RECUR_THRESHOLD = 3   # flag a review finding seen on >= this many of them
KEEP_DAYS = 30        # backups / quarantined dirs older than this are pruned on --fix
JOB_ID_RE = re.compile(r"[0-9a-f]{12}")   # Hermes cron job ids (and their output dir names)

FINDINGS = []          # dicts: check, sev ('auto'|'review'), target, detail, fix (rel,old,new)|None, action, applied bool
_BACKED_UP = set()
_WARNED = set()        # (kind, key) pairs already reported this run, so a missing input is flagged once
_JOBS = []             # one jobs.json snapshot per run, shared by every cron check: [loaded?, jobs|None]


def add(check, sev, target, detail, fix=None, action=None):
    FINDINGS.append(dict(check=check, sev=sev, target=target, detail=detail, fix=fix, action=action, applied=False))


def _warn_once(kind, key, check, target, detail):
    if (kind, key) not in _WARNED:
        _WARNED.add((kind, key))
        add(check, "review", target, detail)


# ---------- ground truth ----------
def _yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def gt_models():
    m = (_yaml(CONFIG).get("model") or {})
    profiles = {}
    for pc in sorted(glob.glob(os.path.join(HOME, "profiles", "*", "config.yaml"))):
        prof = os.path.basename(os.path.dirname(pc))
        profiles[prof] = (_yaml(pc).get("model") or {}).get("default")
    return {"default": m.get("default"), "fallback": m.get("fallback"), "profiles": profiles}


def gt_curator():
    return (_yaml(CONFIG).get("curator") or {}).get("enabled")


def _load_jobs():
    """The job dicts from jobs.json, or None when it is missing / unreadable / an unexpected
    shape. None means UNKNOWN — never 'no jobs': treating it as an empty list made --fix
    delete every cron output dir. Parsed as leniently as Hermes's own loader (raw control
    characters, a bare list, an id-keyed map), so a file the scheduler runs fine is read too."""
    try:
        with open(JOBS, encoding="utf-8") as f:
            data = json.loads(f.read(), strict=False)
    except (OSError, ValueError):
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [dict(v, id=v.get("id") or k) for k, v in jobs.items() if isinstance(v, dict)]
    if not isinstance(jobs, list):
        return None
    # All-or-nothing: one entry without a clean string id means the file is not what we think it
    # is, and acting on "the rest" would mark live jobs' output dirs as orphans.
    if not all(isinstance(j, dict) and isinstance(j.get("id"), str) and j["id"] and j["id"] == j["id"].strip()
               for j in jobs):
        return None
    return jobs


def gt_crons():
    """[{id,name,expr,script,paused}] or None if jobs.json can't be read (reported once)."""
    if not _JOBS:
        _JOBS.extend([True, _load_jobs()])
    jobs = _JOBS[1]
    if jobs is None:
        _warn_once("jobs", JOBS, "cron", "cron/jobs.json",
                   "cron/jobs.json is missing, unreadable, or has an entry without a clean job id — cron and orphan checks skipped, so this run is NOT a clean bill for them")
        return None
    out = []
    for j in jobs:
        paused = (not j.get("enabled", True)) or bool(j.get("paused_at"))
        sched = j.get("schedule") if isinstance(j.get("schedule"), dict) else {}
        out.append({"id": str(j.get("id")), "name": j.get("name"), "expr": sched.get("expr"),
                    "script": j.get("script"), "paused": paused})
    return out


def _frontmatter_text(path):
    """Raw frontmatter text (BOM-tolerant) or None."""
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read()
    except Exception:
        return None
    t = t.lstrip("\ufeff")
    if not t.startswith("---"):
        return None
    end = t.find("\n---", 3)
    return t[3:end] if end != -1 else None


def _frontmatter(path):
    fm = _frontmatter_text(path)
    if fm is None:
        return {}
    try:
        d = yaml.safe_load(fm)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fm_version(path):
    """The version exactly as written: YAML would read an unquoted `1.10` as the float 1.1."""
    fm = _frontmatter_text(path)
    m = re.search(r"(?m)^version:[ \t]*['\"]?([^'\"\s#]+)", fm or "")
    return m.group(1) if m else ""


def gt_skills():
    """{ rel_dir: {name, version} } for installed skills (excl. .archive / backups)."""
    out = {}
    for sk in sorted(glob.glob(os.path.join(INSTALL, "**", "SKILL.md"), recursive=True)):
        rel = os.path.relpath(os.path.dirname(sk), INSTALL)
        if {".archive", "backups"} & set(rel.split(os.sep)):
            continue
        out[rel] = {"name": _frontmatter(sk).get("name"), "version": _fm_version(sk)}
    return out


def gt_vip():
    sk = os.path.join(INSTALL, "email-gmail-triage", "SKILL.md")
    if not os.path.exists(sk):    # moved into a category folder?
        sk = next(iter(sorted(p for p in glob.glob(os.path.join(INSTALL, "**", "email-gmail-triage", "SKILL.md"), recursive=True)
                              if ".archive" not in p)), sk)
    try:
        t = open(sk, encoding="utf-8").read()
    except FileNotFoundError:
        return []
    except Exception as e:
        _warn_once("vip", sk, "vip", "email-gmail-triage/SKILL.md", "could not read the VIP roster (%s) — not verified" % type(e).__name__)
        return []
    # The roster is the line that STARTS with the "VIP senders:" label (prose can mention it too).
    line = next((l for l in t.splitlines() if re.match(r"\s*(?:[-*]\s+)?\**VIP senders\**\s*:", l)), None)
    if line is None:
        if "VIP senders" in t:
            _warn_once("vip", sk, "vip", "email-gmail-triage/SKILL.md", "could not find the `VIP senders:` roster line — VIP roster not verified")
        return []
    vip = re.findall(r"`([^`]+@[^`]+)`\s*\(([^)]+)\)", line)
    if not vip:
        _warn_once("vip", sk, "vip", "email-gmail-triage/SKILL.md", "the `VIP senders:` line has no `email` (Name) entries I can parse — VIP roster not verified")
    return vip


def gt_env_keys():
    """Key NAMES in .env, or None if the file can't be read (unknown, not 'no keys')."""
    keys = set()
    try:
        for l in open(ENVF, encoding="utf-8"):
            m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*=", l)
            if m:
                keys.add(m.group(1))
    except Exception:
        return None
    return keys


def gt_env_value(key):
    try:
        for l in open(ENVF, encoding="utf-8"):
            m = re.match(r"\s*(?:export\s+)?%s\s*=\s*(.+)" % re.escape(key), l)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except Exception:
        pass
    return None


# ---------- vault io ----------
def _vault_path(rel):
    """Resolve rel inside the vault; return None if it escapes (defense for data-driven rules)."""
    p = os.path.realpath(os.path.join(VAULT, rel))
    return p if p.startswith(VAULT + os.sep) else None


def read(rel):
    p = _vault_path(rel)
    if not p:
        raise ValueError("path escapes vault: %s" % rel)
    return open(p, encoding="utf-8").read()


def _read_core(rel, what):
    """Read a note a check depends on; if it can't be read, say so (once) instead of passing silently."""
    try:
        return read(rel)
    except Exception as e:
        _warn_once("note", rel, "vault", rel, "could not read `%s` (%s) — %s not verified" % (rel, type(e).__name__, what))
        return None


def _atomic_write(path, text):
    tmp = path + ".tmp-audit-" + TS
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)   # keep the note's mode (a 600 note stays 600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


_NOTES = {}
_TITLES = set()        # every note's title, readable or not (an unreadable note is still a link target)


def all_notes():
    if _NOTES or _TITLES:
        return _NOTES
    out = _NOTES
    for p in sorted(glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True)):
        if "/.obsidian/" in p or ".bak-" in os.path.basename(p):
            continue
        rel = os.path.relpath(p, VAULT)
        if not os.path.isfile(p):          # a dir named *.md, or a dangling symlink: not a note
            continue
        _TITLES.add(os.path.splitext(os.path.basename(rel))[0].lower())
        try:
            out[rel] = open(p, encoding="utf-8").read()
        except Exception as e:
            _warn_once("note", rel, "vault", rel, "could not read `%s` (%s) — skipped by every vault-wide check" % (rel, type(e).__name__))
    return out


def _backup(path):
    if path not in _BACKED_UP:
        # copy (not copy2): the backup must carry THIS run's mtime — copy2 kept the note's old
        # mtime, so _prune_backups deleted the fresh backup of any note untouched for 30 days.
        shutil.copy(path, path + ".bak-audit-" + TS)
        _BACKED_UP.add(path)


def apply_fix(rel, old, new):
    """Exactly-once anchored replacement with backup + atomic write + validation. Returns (ok, err)."""
    path = _vault_path(rel)
    if not path:
        return False, "path escapes vault"
    try:
        text = open(path, encoding="utf-8").read()
    except Exception as e:
        return False, "unreadable: %s" % e
    n = text.count(old)
    if n != 1:
        return False, "anchor not unique (%d matches)" % n
    new_text = text.replace(old, new, 1)
    if new_text == text:
        return False, "no change"
    try:
        _backup(path)
        _atomic_write(path, new_text)
        if open(path, encoding="utf-8").read() != new_text:
            return False, "write-back validation failed"
    except OSError as e:
        return False, "write failed: %s" % type(e).__name__
    return True, None


def _quarantine(dirname):
    """Move an orphaned cron output dir into the dated quarantine (reversible; pruned after KEEP_DAYS)."""
    src = os.path.join(CRON_OUT, dirname)
    if not JOB_ID_RE.fullmatch(dirname) or os.path.islink(src) or not os.path.isdir(src):
        return False, "not a plain job-output directory"
    dst_dir = os.path.join(ORPHAN_TRASH, TS)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        os.rename(src, os.path.join(dst_dir, dirname))   # same filesystem: atomic, nothing copied
    except OSError as e:
        return False, "move failed: %s" % type(e).__name__
    return True, None


_TS_SUFFIX = re.compile(r"\.(bak|tmp)-audit-(\d{8}_\d{6})$")


def _ts_age_days(stamp):
    try:
        return (datetime.datetime.now() - datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")).days
    except ValueError:
        return None


def _prune_backups(days=KEEP_DAYS):
    """Delete vault audit backups/temp files, and quarantined cron output dirs, older than
    `days` — judged by the timestamp IN THE NAME (never mtime, never this run's TS). Matches
    only the exact `<name>.bak|tmp-audit-<TS>` suffix, does NOT descend directory symlinks,
    and never deletes a path that resolves outside the vault / quarantine."""
    for root, _dirs, files in os.walk(VAULT, followlinks=False):
        for name in files:
            m = _TS_SUFFIX.search(name)
            if not m or m.group(2) == TS:
                continue
            age = _ts_age_days(m.group(2))
            p = os.path.join(root, name)
            if age is None or age < days or not os.path.realpath(p).startswith(VAULT + os.sep):
                continue
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        stamps = os.listdir(ORPHAN_TRASH) if os.path.isdir(ORPHAN_TRASH) and not os.path.islink(ORPHAN_TRASH) else []
    except OSError as e:
        add("prune", "review", "(audit)", "could not list scripts/.vault_audit_orphans (%s) — old quarantine not pruned" % type(e).__name__)
        stamps = []
    if stamps:
        for stamp in stamps:
            age = _ts_age_days(stamp)
            p = os.path.join(ORPHAN_TRASH, stamp)
            if stamp != TS and age is not None and age >= days and os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)


# ---------- checks ----------
def check_models():
    rel = "Hermes Agent/Operations.md"
    if os.path.exists(CONFIG) and not _yaml(CONFIG):
        add("config", "review", "config.yaml", "config.yaml is present but unreadable/empty — model & curator checks are skipped, so this run is NOT a clean bill for them")
    gm = gt_models()
    if not (gm["default"] or gm["fallback"] or gm["profiles"].get("pro")):
        return
    text = _read_core(rel, "model names")
    if text is None:
        return
    # Anchor each pattern to the START of a definition line (^[\t *-]* before the
    # backticked label) so a mid-sentence mention of `default`/`fallback`/`pro`
    # elsewhere in the note can't be picked as the edit target. The line-prefix is
    # captured in group 1 so the replacement (group1+truth+group3) preserves it.
    specs = [("default", gm["default"], r"(?m)(^[\t *-]*`default`[^\n]*?\*\*`)([^`]+)(`\*\*)"),
             ("fallback", gm["fallback"], r"(?m)(^[\t *-]*`fallback`[^\n]*?\*\*`)([^`]+)(`\*\*)"),
             ("pro profile", gm["profiles"].get("pro"), r"(?m)(^[\t *-]*`pro`[^\n]*?: `)([^`]+)(`)")]
    for label, truth, pat in specs:
        if not truth:
            continue
        m = re.search(pat, text)
        if not m:
            add("models", "review", rel, "could not find the `%s` model line in the expected format — not verified (actual '%s')" % (label.split()[0], truth))
        elif m.group(2) != truth:
            old, new = m.group(0), m.group(1) + truth + m.group(3)
            add("models", "auto", rel, "%s: note says '%s', actual '%s'" % (label, m.group(2), truth), (rel, old, new))


def check_curator():
    truth = gt_curator()
    if truth is None:
        return
    if not isinstance(truth, bool):   # a quoted "false" is truthy — never rewrite the notes from it
        add("curator", "review", "config.yaml", "curator.enabled is %r, not a boolean — curator notes not verified" % (truth,))
        return
    want = "true" if truth else "false"
    for rel in ["Hermes Agent/Operations.md", "Hermes Agent/Decisions.md"]:
        text = _read_core(rel, "curator state")
        if text is None:
            continue
        m = re.search(r"(`curator\.enabled:\s*)(true|false)(`)", text)
        if not m:
            add("curator", "review", rel, "no `curator.enabled: <bool>` token found — curator state not verified (actual %s)" % want)
        elif m.group(2) != want:
            # Report, never rewrite: both notes pair the flag with the decision behind it
            # ("disabled deliberately — do not re-enable"), and a flipped config is exactly the
            # event they exist to flag — rewriting the token would leave the prose contradicting it.
            add("curator", "review", rel, "curator.enabled is %s in config.yaml but this note says %s — changed on purpose?" % (want, m.group(2)))


def _every_hour(expr):
    f = (expr or "").split()
    return len(f) == 5 and f[0] == "0" and f[1] == "*"


def _expr_hm(expr):
    """(hour, minute) when the expr fires at ONE fixed time of day, else None."""
    f = str(expr or "").split()
    if len(f) == 5 and re.fullmatch(r"\d{1,2}", f[0]) and re.fullmatch(r"\d{1,2}", f[1]):
        return int(f[1]), int(f[0])
    return None


_HM_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")


def _hm_tokens(text):
    """Valid times in text as (hour, minute) — 24h HH:MM, and h:MM am/pm converted to 24h."""
    out = []
    for m in re.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])(\s*([ap])\.?m\b)?", text, re.I):
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(4):
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if m.group(4).lower() == "p" else 0)
        if h < 24 and mi < 60:
            out.append((h, mi))
    return out


def _fix_hm(cell, want):
    """Rewrite the ONE valid 24h HH:MM outside backtick spans to `want`, by its exact span.
    Returns the new cell, or None when there isn't exactly one such time, it is written
    with am/pm, or the result doesn't re-read as exactly [want] (never guess)."""
    parts = re.split(r"(`[^`]*`)", cell)
    hits = [(i, m) for i, part in enumerate(parts) if not part.startswith("`")
            for m in _HM_RE.finditer(part) if int(m.group(1)) < 24 and int(m.group(2)) < 60
            and not re.match(r"\s*[ap]\.?m\b", part[m.end():], re.I)]
    if len(hits) != 1:
        return None
    i, m = hits[0]
    parts[i] = parts[i][:m.start()] + "%02d:%02d" % want + parts[i][m.end():]
    new = "".join(parts)
    return new if _hm_tokens(re.sub(r"`[^`]*`", "", new)) == [want] else None


def _cron_tokens(text):
    return [t.strip() for t in re.findall(r"`([^`]+)`", text)
            if len(t.split()) == 5 and re.fullmatch(r"[0-9*,/ -]+", t)]


_CRON_NAMES = {n: i for i, n in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
_CRON_NAMES.update({n: i + 1 for i, n in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])})
_CRON_MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
                "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}


def _cron_norm(t):
    """A backticked token as a numeric 5-field expr if it IS a cron expr (digits, or day/month
    names, or an @macro), else None — so `after 999de4b21eec` or a 5-word phrase never qualifies."""
    t = t.strip()
    if t.lower() in _CRON_MACROS:
        return _CRON_MACROS[t.lower()]
    f = t.split()
    if len(f) != 5:
        return None
    for k, field in enumerate(f):
        if k >= 3:
            field = re.sub(r"[A-Za-z]{3}", lambda m: str(_CRON_NAMES.get(m.group(0).upper(), "x")), field)
        if not re.fullmatch(r"[0-9*,/-]+", field):
            return None
        f[k] = field
    return " ".join(f)


def _field_set(f, dow=False):
    """The set of numbers a plain cron field denotes (`2-4` == `2,3,4`; dow 7 == 0), or None
    when it uses `*` or `/` (then fields compare as text)."""
    if not re.fullmatch(r"\d{1,3}(?:-\d{1,3})?(?:,\d{1,3}(?:-\d{1,3})?)*", f):
        return None
    out = set()
    for part in f.split(","):
        a, _, b = part.partition("-")
        a, b = int(a), int(b or a)
        if b < a or b - a > 400:
            return None
        out.update(range(a, b + 1))
    return frozenset(0 if dow and v == 7 else v for v in out)


def _cron_equal(a, b, fields=range(5)):
    """Do two cron exprs mean the same schedule (on the given field indexes)?"""
    na, nb = _cron_norm(str(a or "")), _cron_norm(str(b or ""))
    if not (na and nb):
        return str(a or "").strip() == str(b or "").strip()
    fa, fb = na.split(), nb.split()
    for k in fields:
        if fa[k] != fb[k] and (_field_set(fa[k], k == 4) is None or _field_set(fa[k], k == 4) != _field_set(fb[k], k == 4)):
            return False
    return True


def _cells(line):
    """Markdown table cells, splitting only on UNescaped pipes (`\\|` is literal text)."""
    return re.split(r"(?<!\\)\|", line)


def _header_cols(l):
    """({'id','name','schedule','script': col}, width) if `l` is a jobs-table header, else None."""
    t = l.strip()
    t = (t if t.startswith("|") else "|" + t)
    t = (t if t.endswith("|") else t + "|")
    hdr = [c.strip().lower() for c in _cells(t)]
    col = {}
    for j, c in enumerate(hdr):
        for key in ("id", "name", "schedule", "script"):
            if key not in col and (c == key or (key in ("schedule", "script") and c.startswith(key))):
                col[key] = j
    return (col, len(hdr)) if {"id", "name", "schedule"} <= set(col) else None


_DELIM = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")   # a GFM table delimiter row


def _job_rows(lines):
    """[(line, cols, width)] for every job row whose table header (the line above its
    `|---|` delimiter row) is a jobs header with ID + Name + Schedule. A row under any other
    header, after a heading/prose line that ended its table, or inside a code fence is not
    ours and is never checked or edited; there is no positional fallback."""
    out, cur, fence = [], None, None
    for i, l in enumerate(lines):
        fm = re.match(r"\s{0,3}(`{3,}|~{3,})(.*)$", l)
        if fence:                                       # inside a fence: only its closer ends it
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) and not fm.group(2).strip():
                fence = None
            continue
        if fm and not (fm.group(1)[0] == "`" and "`" in fm.group(2)):   # ```x``` is inline code
            fence, cur = fm.group(1), None
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in l and "|" in nxt and _DELIM.fullmatch(nxt) and not _DELIM.fullmatch(l):
            cur = _header_cols(l)                       # a new table: its header decides
            continue
        if _DELIM.fullmatch(l) and "|" in l:
            continue
        if not l.strip():
            continue                                    # blank lines don't end the current table
        if not l.lstrip().startswith("|"):
            cur = None                                  # a heading / prose line ends it
            continue
        if cur and re.match(r"\|\s*`([0-9a-f]{6,})`\s*\|", l):
            out.append((l, cur[0], cur[1]))
    return out


def check_crons():
    rel = "Hermes Agent/Routines.md"
    text = _read_core(rel, "the Routines table")
    if text is None:
        return
    crons = gt_crons()
    if crons is None:
        return
    byid = {c["id"]: c for c in crons}
    lines = text.splitlines()
    canon = re.compile(r"\s*\*\*\[PAUSED\]\*\*", re.I)
    loose = re.compile(r"\s*(?:\*\*\[PAUSED\]\*\*|\[PAUSED\]|\(PAUSED\)|\*\*PAUSED\*\*)", re.I)
    rows = _job_rows(lines)
    if not rows:
        add("cron", "review", rel, "could not find the jobs table (a header with ID | Name | Schedule columns) — cron rows not checked")
    documented = set()
    for line, col, width in rows:
        jid = re.match(r"\|\s*`([0-9a-f]{6,})`\s*\|", line).group(1)
        documented.add(jid)
        trail = line.rstrip().endswith("|")     # a row may omit the closing pipe (valid markdown)
        cells = _cells(line if trail else line + "|")
        name = cells[col["name"]].strip() if len(cells) > col["name"] else ""
        job = byid.get(jid)
        if not job:
            add("cron", "review", rel, "Routines row `%s` (%s) has no job in jobs.json — deleted on purpose? then drop the row; if not, the job is gone" % (jid, name or "?"))
            continue
        if len(cells) != width:
            add("cron", "review", rel, "Routines row `%s` has %d cells but the header has %d (stray `|`?) — row not checked" % (jid, len(cells), width))
            continue
        sched_cell = cells[col["schedule"]]
        new_cells, notes = cells[:], []
        # The documented expr: the ONE cron-shaped backticked token in the schedule cell —
        # daily 07:30 (`30 7 * * *`) or bare. Zero or several candidates ⇒ never edited, so a
        # decoy backtick span (`after 999de4b21eec`, `backup_hermes.py`) can't be overwritten.
        claimed = raw = None
        raws = [t for t in re.findall(r"`([^`]+)`", sched_cell) if _cron_norm(t)]
        named = [t for t in raws if not re.fullmatch(r"[0-9*,/ -]+", t.strip())]
        hand = False                                  # True once this cell is sent to review: edit nothing in it
        if len(raws) > 1 or named:
            # Several exprs, or a named one (MON-FRI, @daily): compared, never edited.
            if not all(_cron_equal(t, job["expr"]) for t in raws):
                add("cron", "review", rel, "`%s` schedule cell has %s (%s) — keep one current expr, `%s`, and fix the row by hand"
                    % (jid, "%d exprs" % len(raws) if len(raws) > 1 else "a named expr",
                       ", ".join("`%s`" % t.strip() for t in raws), job["expr"]))
                hand = True
        elif raws:
            raw, claimed = raws[0], raws[0].strip()
        gloss = re.sub(r"`[^`]*`", "", re.sub(r"\(`[^`]*`\)", "", sched_cell))
        want = _expr_hm(job["expr"])
        # The human HH:MM beside the expr is the same fact. The time is fixed with the expr only
        # in the plain case (one valid 24h time, a job that fires at one time); otherwise the
        # whole cell goes to review and STAYS there until a human edits it — fixing just the expr
        # used to leave rows like "07:28 (`25 7 * * 2,3,4`)" that the audit then called clean.
        cell = sched_cell
        expr_drift = claimed is not None and bool(job["expr"]) and not _cron_equal(claimed, job["expr"])
        if expr_drift:
            # Auto-fix only a change whose human copy can be rewritten too: the days unchanged AND
            # either a fixed time of day both before and after (its HH:MM is rewritten with it), or
            # only the minute changed. Anything else ("24/7", "08-21", "every 2h", "Sun", "daily",
            # "1st–5th" around a changed hour or day) would be left contradicting the new expr.
            old, new = claimed.split(), str(job["expr"]).split()
            # A time written any other way ("8am", "7.30", ":00", "evening", "on the hour") can't be
            # rewritten either, so it rules the auto-fix out.
            other_time = re.search(r"\b\d{1,2}\s*[ap]\.?m\b|\b\d{1,2}\.\d{2}\b|(?<![\d:]):\d{2}\b|\b(?:morning|afternoon|evening|night|noon|midnight|on the hour)\b",
                                   re.sub(r"(?<![\d:])\d{1,2}:\d{2}(?![\d:])(\s*[ap]\.?m\b)?", "", gloss), re.I)
            safe = len(new) == 5 and not other_time and _cron_equal(claimed, job["expr"], fields=(2, 3, 4)) and (
                (_expr_hm(claimed) and want) or (old[1] == new[1] and old[0].isdigit() and new[0].isdigit()))
            if not safe:
                add("cron", "review", rel, "`%s` schedule cell '%s' doesn't match jobs.json `%s` (the schedule changed) — fix the row by hand"
                    % (jid, sched_cell.strip(), job["expr"]))
                expr_drift, hand = False, True      # nothing else in this cell is checked or edited
        if expr_drift:
            cell = cell.replace("`%s`" % raw, "`%s`" % job["expr"], 1)
        hms = _hm_tokens(gloss)
        time_ok = not hms or bool(want and hms == [want])
        # Rewrite the time only when the cell's ONE expr is (now) right — never next to stale exprs.
        if not time_ok and want and len(hms) == 1 and raws and not hand:
            fixed = _fix_hm(cell, want)
            if fixed is not None:
                cell, time_ok = fixed, True
                notes.append("schedule time %02d:%02d -> %02d:%02d" % (hms[0][0], hms[0][1], want[0], want[1]))
        if not time_ok and (expr_drift or (want and want not in hms)) and not hand:
            add("cron", "review", rel, "`%s` schedule cell '%s' doesn't match jobs.json `%s` — fix the row by hand"
                % (jid, sched_cell.strip(), job["expr"]))
            cell, notes = sched_cell, []
        elif expr_drift:
            notes.insert(0, "schedule expr '%s' -> '%s'" % (claimed, job["expr"]))
        new_cells[col["schedule"]] = cell
        if _every_hour(job["expr"]) and re.search(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b", gloss):
            add("cron", "review", rel, "`%s` schedule gloss '%s' implies an hour-window but expr runs every hour" % (jid, gloss.strip()))
        script = os.path.basename(str(job.get("script") or ""))
        if script and "script" in col and len(cells) > col["script"]:
            claimed_script = cells[col["script"]].strip().strip("`").strip()
            if claimed_script and claimed_script != script:
                add("cron", "review", rel, "`%s` Script column says `%s` but the job runs `%s`" % (jid, claimed_script, script))
        # Paused-state marker: the canonical bold **[PAUSED]** token in the Name cell. ADD it when
        # the job is paused (normalising a non-standard [PAUSED]/(PAUSED)/**PAUSED** there instead
        # of stacking a second one), REMOVE it when the job is active. Any other marker form on an
        # active job's row is reported, never guessed at; the plain word "paused" is not a marker.
        name_cell = new_cells[col["name"]]
        if job["paused"] and not canon.search(name_cell):
            base = loose.sub("", name_cell).rstrip() if loose.search(name_cell) else name_cell.rstrip()
            new_cells[col["name"]] = base + " **[PAUSED]** "
            notes.append("add [PAUSED] marker (paused in jobs.json)")
        elif not job["paused"] and canon.search(name_cell):
            stripped = canon.sub("", name_cell)
            new_cells[col["name"]] = stripped if stripped.endswith(" ") else stripped + " "
            notes.append("remove [PAUSED] marker (active in jobs.json)")
        elif not job["paused"] and loose.search(line):
            add("cron", "review", rel, "`%s` is active in jobs.json but its row still says paused" % jid)
        new_line = "|".join(new_cells)
        if not trail:
            new_line = new_line[:-1]
        if notes and new_line != line:
            # ONE finding per row, so two drifts on the same row can't invalidate each other's anchor.
            add("cron", "auto", rel, "`%s` %s" % (jid, "; ".join(notes)), (rel, line, new_line))
    for c in crons:
        if not c["paused"] and c["id"] not in documented:
            add("cron", "review", rel, "active job `%s` (%s) missing from Routines table" % (c["id"], c["name"]))
    mc = re.search(r"\*\*(\d+) active \+ (\d+) paused\*\*", text)
    if mc:
        act = sum(1 for c in crons if not c["paused"])
        pau = len(crons) - act
        if (int(mc.group(1)), int(mc.group(2))) != (act, pau):
            add("cron", "review", rel, "header says %s active + %s paused; jobs.json has %d active + %d paused" % (mc.group(1), mc.group(2), act, pau))


def check_job_refs():
    """Job ids / schedules quoted OUTSIDE the Routines table (skill notes, People notes):
    a backticked 12-hex id that is no longer a job, and — on a line naming exactly one known
    job — a cron expr or HH:MM that disagrees with it. Review only."""
    crons = gt_crons()
    if not crons:          # unreadable (already reported) or empty: nothing to compare against
        return
    byid = {c["id"]: c for c in crons}
    for rel, text in all_notes().items():
        if rel == os.path.join("Hermes Agent", "Routines.md"):
            continue
        for line in text.splitlines():
            ids = sorted({t for t in re.findall(r"`([0-9a-f]{12})`", line)
                          if re.search(r"[a-f]", t) and re.search(r"\d", t)})
            unknown = [t for t in ids if t not in byid]
            for t in unknown:
                _warn_once("jobref", (rel, t), "cron-ref", rel, "mentions job `%s`, which is not in jobs.json (retired/renamed?)" % t)
            known = [t for t in ids if t in byid]
            if len(known) != 1 or unknown:
                continue
            job = byid[known[0]]
            exprs = _cron_tokens(line)
            if len(exprs) == 1 and job["expr"] and not _cron_equal(exprs[0], job["expr"]):
                _warn_once("jobexpr", (rel, known[0]), "cron-ref", rel, "says `%s` runs `%s`; jobs.json has `%s`" % (known[0], exprs[0], job["expr"]))
                continue
            want = _expr_hm(job["expr"])
            hms = _hm_tokens(re.sub(r"`[^`]*`", "", line))
            if want and len(hms) == 1 and hms[0] != want:
                _warn_once("jobexpr", (rel, known[0]), "cron-ref", rel, "says `%s` runs at %02d:%02d; jobs.json fires at %02d:%02d" % (known[0], hms[0][0], hms[0][1], want[0], want[1]))


def _orphan_label(d):
    """'"<job name>", last run <date>' from the newest output file's header, for a readable report."""
    try:
        files = sorted(f for f in os.listdir(os.path.join(CRON_OUT, d)) if f.endswith(".md"))
        if not files:
            return "no runs saved"
        with open(os.path.join(CRON_OUT, d, files[-1]), encoding="utf-8", errors="replace") as f:
            head = f.readline().strip()
        name = re.sub(r"^#\s*Cron Job:\s*", "", head) or "?"
        return '"%s", last run %s' % (name[:60], files[-1][:10])
    except OSError:
        return "unreadable"


def check_orphaned_output():
    """Cron output dirs whose job no longer exists in jobs.json (one-shot reminders, deleted
    jobs). Auto-fixable: --fix MOVES them into ORPHAN_TRASH/<ts>/ (pruned after KEEP_DAYS).
    Never acts on an unknown/empty job list, on names that aren't job ids, on symlinks, or
    on a bulk "orphaning" that looks like broken ground truth rather than stale jobs."""
    if not os.path.isdir(CRON_OUT):
        return
    crons = gt_crons()
    if crons is None:
        return
    dirs = sorted(d for d in os.listdir(CRON_OUT)
                  if JOB_ID_RE.fullmatch(d) and os.path.isdir(os.path.join(CRON_OUT, d))
                  and not os.path.islink(os.path.join(CRON_OUT, d)))
    if not crons:
        if dirs:
            add("cron-output-orphan", "review", "system", "jobs.json lists no jobs but %d output dirs exist — not cleaning anything; check the scheduler" % len(dirs))
        return
    active = {c["id"] for c in crons}
    if not all(JOB_ID_RE.fullmatch(i) for i in active):
        add("cron-output-orphan", "review", "system", "jobs.json has job ids in an unexpected format — orphan cleanup skipped")
        return
    orphans = [d for d in dirs if d not in active]
    if len(orphans) > max(3, len(dirs) // 4):
        add("cron-output-orphan", "review", "system", "%d of %d cron output dirs have no job in jobs.json — too many to be stale one-shots; not cleaning (check jobs.json)" % (len(orphans), len(dirs)))
        return
    for d in orphans:
        add("cron-output-orphan", "auto", "system", "orphaned cron output dir `%s` (%s) — job no longer in jobs.json" % (d, _orphan_label(d)), action=d)


def check_skills():
    inv = gt_skills()  # rel_dir -> {name, version}
    for nf in sorted(glob.glob(os.path.join(SKILLS_NOTES, "*.md"))):
        if ".bak-" in os.path.basename(nf) or os.path.basename(nf) == "Skills MOC.md":
            continue
        rel = os.path.relpath(nf, VAULT)
        try:
            text = open(nf, encoding="utf-8").read()
        except Exception as e:
            _warn_once("note", rel, "skill-version", rel, "could not read `%s` (%s) — version not verified" % (rel, type(e).__name__))
            continue
        pm = re.search(r"~/\.hermes/skills/([\w./-]+?)`?\s*\(v([\d.]+)\)", text)
        if not pm:
            mdir = re.search(r"~/\.hermes/skills/([\w./-]+)", text)
            if mdir:
                d = mdir.group(1).rstrip("/`")
                if d not in inv and not os.path.isdir(os.path.join(INSTALL, d)):
                    add("skill-path", "review", rel, "references skills/%s which is not installed (renamed/archived?)" % d)
            continue
        d = pm.group(1).rstrip("/`")
        claimed = pm.group(2)
        if d not in inv and not os.path.isdir(os.path.join(INSTALL, d)):
            add("skill-path", "review", rel, "references skills/%s which is not installed (renamed/archived?)" % d)
            continue
        truth = inv.get(d, {}).get("version", "")
        if truth and truth != claimed:
            anchor = pm.group(0)
            new = anchor.replace("(v%s)" % claimed, "(v%s)" % truth, 1)
            add("skill-version", "auto", rel, "%s version note=v%s actual=v%s" % (d, claimed, truth), (rel, anchor, new))


def _note_origin(title):
    f = os.path.join(SKILLS_NOTES, title + ".md")
    if not os.path.exists(f):
        return None
    t = open(f, encoding="utf-8").read().lower()
    if "built by ham" in t or "author: ham" in t or "user-tuned" in t:
        return "ham"
    if "origin: bundled" in t or "bundled (" in t or "origin: hermes agent" in t:
        return "bundled"
    return None


def check_moc_grouping():
    rel = "Hermes Agent/Skills/Skills MOC.md"
    text = _read_core(rel, "Skills MOC grouping and archive bookkeeping")
    if text is None:
        return
    lines = text.splitlines()
    def hdr(prefix):
        return next((i for i, l in enumerate(lines) if l.startswith(prefix)), None)
    i_ham = hdr("## Built by Ham")
    i_bun = hdr("## Bundled / Hermes Agent")
    if i_ham is None or i_bun is None or i_bun < i_ham:
        add("moc", "review", rel, "could not locate the 'Built by Ham' / 'Bundled' sections to verify grouping")
        return
    i_next = next((i for i in range(i_bun + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    ham_hdr = lines[i_ham]
    bun_hdr = lines[i_bun]
    ham_body = lines[i_ham + 1:i_bun]
    bun_body = lines[i_bun + 1:i_next]
    cur_ham = [l for l in ham_body if l.startswith("- [[")]
    cur_bun = [l for l in bun_body if l.startswith("- [[")]
    def title_of(b):
        m = re.search(r"\[\[([^\]\|#]+)", b)
        return m.group(1).strip() if m else ""
    new_ham, new_bun, moved = [], [], []
    for b in cur_ham + cur_bun:
        t = title_of(b)
        o = _note_origin(t)
        cur = "ham" if b in cur_ham else "bundled"
        dest = o or cur
        (new_ham if dest == "ham" else new_bun).append(b)
        if dest != cur:
            moved.append("%s -> %s" % (t, dest))
    if not moved:
        return
    # Safety: never auto-rewrite if a section holds anything other than bullets/blank lines —
    # the rebuild only re-emits the collected bullets, so other content would be lost.
    unexpected = [l for l in ham_body + bun_body if l.strip() and not l.startswith("- [[")]
    if unexpected:
        add("moc", "review", rel, "grouping looks off (%s) but a section has non-bullet content; not auto-regrouping — fix by hand" % "; ".join(moved))
        return
    new_ham.sort(key=title_of)
    new_bun.sort(key=title_of)
    block = [ham_hdr] + new_ham + ["", bun_hdr] + new_bun
    old_block = "\n".join(lines[i_ham:i_next]).rstrip("\n")
    new_block = "\n".join(block)
    add("moc", "auto", rel, "regroup by page origin: " + "; ".join(moved), (rel, old_block, new_block))


def check_vip():
    vip = {e for e, _ in gt_vip()}
    if not vip:
        return
    anthony_rel = "People/Anthony (Ham).md"
    anthony = _read_core(anthony_rel, "the VIP roster") or ""
    try:
        triage = read("Hermes Agent/Skills/Email Gmail Triage.md")   # optional second roster
    except Exception:
        triage = ""
    if not (anthony or triage):
        return
    roster = anthony + "\n" + triage
    for e in sorted(vip):
        if e not in roster:
            add("vip", "review", anthony_rel, "VIP sender %s is in the skill but not in the vault roster" % e)
    fam = re.search(r"## Family[^\n]*\n(.*?)(?:\n## |\Z)", anthony, re.S)
    if fam:
        for e in re.findall(r"[\w.+-]+@[\w.-]+", fam.group(1)):
            if e not in vip and e != "centre_admin@myfirstskool.com":
                add("vip", "review", anthony_rel, "family email %s in vault is not in the skill VIP_EMAILS" % e)


def check_env_keys():
    rel = "Hermes Agent/API Reference.md"
    text = _read_core(rel, "env key names")
    if text is None:
        return
    envk = gt_env_keys()
    if envk is None:
        _warn_once("env", ENVF, "env", ".env", "could not read ~/.hermes/.env — env key names and channel ids not verified")
        return
    for k in sorted(set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", text))):
        if k.endswith(("KEY", "TOKEN", "SECRET")) and k not in envk:
            add("env", "review", rel, "API Reference names `%s` but it is not a key in .env" % k)


def check_ids():
    if gt_env_keys() is None:
        _warn_once("env", ENVF, "env", ".env", "could not read ~/.hermes/.env — env key names and channel ids not verified")
        return
    home = gt_env_value("DISCORD_HOME_CHANNEL")
    if not home:
        return
    for rel in ["Hermes Agent/Operations.md", "People/Anthony (Ham).md"]:
        text = _read_core(rel, "channel ids")
        if text is None:
            continue
        if home not in text:
            add("ids", "review", rel, "DISCORD_HOME_CHANNEL=%s not found in note (channel changed?)" % home)


def check_wikilinks():
    notes = all_notes()                      # read the vault once (was read twice: here + note_titles)
    titles = _TITLES
    seen = set()
    for rel, text in notes.items():
        # Links inside code are examples, not links: blank fenced blocks and inline spans first.
        body = re.sub(r"(?ms)^(```|~~~).*?^\1", "", text)
        body = re.sub(r"`[^`\n]*`", "", body)
        # (?<!!) skips Obsidian embeds ![[file.png]] (attachments, not note links);
        # Obsidian resolves links case-insensitively and by basename, so compare that.
        for raw in re.findall(r"(?<!!)\[\[([^\]]+)\]\]", body):
            tgt = re.split(r"\\?\|", raw)[0].split("#")[0].strip().rstrip("\\").strip()
            key = re.sub(r"\.md$", "", tgt.split("/")[-1], flags=re.I).lower()
            if tgt and key not in titles and (rel, tgt) not in seen:
                seen.add((rel, tgt))
                add("links", "review", rel, "dangling wikilink [[%s]]" % tgt)


def check_archive():
    rel = "Hermes Agent/Skills/Skills MOC.md"
    if not os.path.isdir(ARCHIVE):
        return
    try:
        text = read(rel)
    except Exception:
        return            # check_moc_grouping already reported the unreadable MOC
    m = re.search(r"(?ms)^## Archived[^\n]*\n(.*?)(?=^## |\Z)", text)
    if not m:
        add("archive", "review", rel, "no '## Archived' section in the MOC — archive bookkeeping not verified")
        return
    listed = set(re.findall(r"`([\w.-]+)`", m.group(1)))
    archived = {d for d in os.listdir(ARCHIVE) if not d.startswith(".")}
    for d in sorted(archived - listed):
        add("archive", "review", rel, "archived skill '%s' not listed in the MOC Archived section" % d)
    for d in sorted(listed - archived):
        add("archive", "review", rel, "MOC Archived section lists '%s' but it is not in skills/.archive/" % d)


# ---------- learned (data-driven) rules — the feedback loop ----------
def _risky_regex(p):
    """True if an UNBOUNDED repeat contains another repeat or an alternation — the
    catastrophic-backtracking shapes ((\\w+\\s?)+, (a|aa)+). Python's re can't be interrupted,
    so such a learned rule could hang the unattended run until the watchdog kills it.
    Conservative: a few safe nestings (v(\\d+(?:\\.\\d+)*)) are refused too — write ([\\d.]+)."""
    try:
        tree = _sre_parse.parse(str(p))
    except Exception:
        return False
    rep_ops = {_sre_parse.MAX_REPEAT, _sre_parse.MIN_REPEAT} | {getattr(_sre_parse, "POSSESSIVE_REPEAT", None)}

    def contains_unbounded_or_branch(items):
        for op, av in items:
            if op in rep_ops and (av[1] == _sre_parse.MAXREPEAT or contains_unbounded_or_branch(av[2])):
                return True
            if op == _sre_parse.BRANCH:
                return True
            if op == _sre_parse.SUBPATTERN and contains_unbounded_or_branch(av[-1]):
                return True
        return False

    def walk(items, inside):
        for op, av in items:
            if op in rep_ops:
                lo, hi, sub = av
                if inside and hi > 1:
                    return True
                if hi > 1 and contains_unbounded_or_branch(sub):
                    return True
                if walk(sub, inside or hi == _sre_parse.MAXREPEAT):
                    return True
            elif op == _sre_parse.BRANCH:
                if inside or any(walk(alt, inside) for alt in av[1]):
                    return True
            elif op == _sre_parse.SUBPATTERN:
                if walk(av[-1], inside):
                    return True
            elif op in (_sre_parse.ASSERT, _sre_parse.ASSERT_NOT):
                if walk(av[1], inside):
                    return True
            elif op == getattr(_sre_parse, "ATOMIC_GROUP", None):
                if walk(av, inside):
                    return True
        return False
    return walk(tree, False)


def _read_rules():
    """(rules, None) or (None, reason) — an unreadable file is never treated as 'no rules'."""
    try:
        data = json.load(open(RULES_FILE, encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except Exception as e:
        return None, type(e).__name__
    rules = data.get("rules", []) if isinstance(data, dict) else None
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        return None, "unexpected shape"
    return rules, None


def load_rules():
    rules, err = _read_rules()
    if rules is None:
        _warn_once("rules", RULES_FILE, "learned", "(rules)", "learned_rules.json is unreadable (%s) — learned rules skipped" % err)
        return []
    return rules


_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASS", "PRIVATE", "AUTH", "CRED", "WEBHOOK", "COOKIE", "BEARER", "SESSION")
_TRUTH_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")


def _is_secret_key(name):
    """True unless `name` is a plain dotted path with no credential-looking word in ANY
    component (model.api_key, auth.token.value, GITHUB_TOKEN_RO, SLACK_WEBHOOK_URL, …)."""
    name = str(name or "")
    return not _TRUTH_PATH.fullmatch(name) or any(w in name.upper() for w in _SECRET_WORDS)


def _resolve_truth(source, path):
    """Resolve a ground-truth value for a learned rule. source: config|profile|env."""
    if _is_secret_key(path):   # never resolve (and thus never print/post) a credential value
        return None
    if source == "env":
        return gt_env_value(path)
    if source == "config":
        d = _yaml(CONFIG)
    elif source == "profile":
        prof, _, path = path.partition(".")           # e.g. "pro.model.default"
        d = _yaml(os.path.join(HOME, "profiles", prof, "config.yaml"))
    else:
        return None
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None or isinstance(cur, (dict, list)):
        return None
    return str(cur).lower() if isinstance(cur, bool) else str(cur)


RULES_TIMEOUT = 30    # seconds for ALL learned rules (run in a child: Python's re can't be interrupted)


def check_learned_rules():
    """Evaluate the declarative rules in learned_rules.json in a CHILD process with a timeout:
    a catastrophic regex can't hang the unattended run (re has no timeout and ignores signals
    mid-match). This is how new drift types are added without code changes (drift-patterns.md)."""
    rules = load_rules()
    if not rules:
        return
    out, slow = "", None
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--_eval-learned"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=RULES_TIMEOUT, env=dict(os.environ, HERMES_HOME=HOME))
        out = p.stdout
        if p.returncode != 0:
            raise RuntimeError("exit %s" % p.returncode)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        slow = True
    except Exception as e:
        add("learned", "review", "(rules)", "learned rules could not be evaluated (%s) — some may not have run" % type(e).__name__)
    started, done = None, 0
    for line in out.splitlines():     # one JSON line per rule: {"start": id} then {"done": id, "findings": [...]}
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "start" in msg:
            started = msg["start"]
        elif "findings" in msg:
            FINDINGS.extend(f for f in msg["findings"] if isinstance(f, dict))
            done, started = done + 1, None
    if slow:
        add("learned", "review", "(rules)", "learned rule [%s] took > %ds (its regex is too slow) — it and any later rules were skipped; simplify or remove it"
            % (started or "?", RULES_TIMEOUT))


def _eval_learned_rules():
    """Child side: report each rule as one JSON line, so a timeout keeps the finished rules
    and names the slow one."""
    rules = load_rules()
    _emit_findings(None, 0)
    for r in rules:
        print(json.dumps({"start": r.get("id", "?")}), flush=True)
        n = len(FINDINGS)
        _eval_one_rule(r)
        _emit_findings(r.get("id", "?"), n)


def _emit_findings(rid, n):
    print(json.dumps({"done": rid, "findings": FINDINGS[n:]}), flush=True)


def _eval_one_rule(r):
    for r in [r]:
        rid = r.get("id", "?")
        try:
            kind = r.get("kind")
            sev = r.get("sev", "review")
            if sev not in ("auto", "review") or kind != "value_equals":
                sev = "review"        # only value_equals can be fixed in place
            rel = r.get("note")
            if kind not in ("require_in_note", "forbid_in_note", "value_equals") or not isinstance(rel, str) or not rel:
                add("learned", "review", "(rules)", "rule [%s] skipped: unknown kind %r or missing note" % (rid, kind))
                continue
            try:
                text = read(rel)
            except Exception:
                add("learned", "review", rel, "rule [%s]: note not found" % rid)
                continue
            why = (" — " + re.sub(r"\s+", " ", str(r["why"]))) if r.get("why") else ""
            if _risky_regex(r.get("pattern", "")) or _risky_regex(r.get("anchor", "")):
                add("learned", "review", rel, "rule [%s] skipped: its regex has a nested quantifier (could hang the audit) — rewrite it" % rid)
                continue
            if kind == "require_in_note" and r["must"] not in text:
                add("learned", sev, rel, "[%s] missing required text %r%s" % (rid, r["must"], why))
            elif kind == "forbid_in_note":
                m = re.search(r["pattern"], text)
                if m:
                    add("learned", sev, rel, "[%s] forbidden pattern present: %r%s" % (rid, m.group(0)[:60], why))
            elif kind == "value_equals":
                if _is_secret_key(r["truth_path"]):
                    add("learned", "review", rel, "rule [%s] skipped: truth_path %r looks like a credential (never resolved or printed)" % (rid, r["truth_path"]))
                    continue
                truth = _resolve_truth(r["truth_source"], r["truth_path"])
                ms = list(re.finditer(r["anchor"], text))
                m = ms[0] if ms else None
                if truth is not None and ms:
                    m = next((x for x in ms if x.group(1) != truth), None)   # every match must agree
                    if m is None:
                        continue
                if truth is None or not m:
                    add("learned", "review", rel, "rule [%s] not verified: %s" % (rid, "truth_path %r not found in %s" % (r["truth_path"], r["truth_source"])
                                                                             if truth is None else "anchor not found in the note"))
                elif m.group(1) != truth:
                    inside = m.start(0) <= m.start(1) <= m.end(1) <= m.end(0)
                    new = None
                    if sev == "auto" and m.group(1).strip() and len(ms) == 1 and inside:
                        s, e = m.start(1) - m.start(0), m.end(1) - m.start(0)
                        old = m.group(0)
                        new = old[:s] + truth + old[e:]
                        # The edit must read back as the truth through the SAME anchor (a lookahead
                        # or a greedy group could otherwise splice text in every week).
                        after = text.replace(old, new, 1)
                        ma = list(re.finditer(r["anchor"], after))
                        if len(ma) != 1 or ma[0].group(1) != truth:
                            new = None
                    if new is not None:
                        add("learned", "auto", rel, "[%s] %s = '%s' (actual '%s')%s" % (rid, r["truth_path"], m.group(1), truth, why), (rel, old, new))
                    else:
                        add("learned", "review", rel, "[%s] %s note='%s' actual='%s'%s%s" % (rid, r["truth_path"], m.group(1), truth, why,
                            " (anchor matched %d times — not auto-fixed)" % len(ms) if len(ms) > 1 else ""))
        except Exception as e:
            add("learned", "review", "(rules)", "rule [%s] raised %s: %s" % (rid, type(e).__name__, e))


CHECKS = [check_models, check_curator, check_crons, check_job_refs, check_orphaned_output, check_skills,
          check_moc_grouping, check_vip, check_env_keys, check_ids, check_wikilinks, check_archive,
          check_learned_rules]


# ---------- run / report / post ----------
def run_checks():
    for c in CHECKS:
        try:
            c()
        except Exception as e:  # a broken check must never break the whole audit
            add(c.__name__, "review", "(audit)", "%s raised %s: %s — that check did not run" % (c.__name__, type(e).__name__, e))


def do_fix():
    for f in FINDINGS:
        if f["sev"] != "auto" or not (f["fix"] or f["action"]):
            continue
        try:
            ok, err = _quarantine(f["action"]) if f["action"] else apply_fix(*f["fix"])
        except Exception as e:   # one bad fix must not abort the rest (or the report)
            ok, err = False, "%s: %s" % (type(e).__name__, e)
        if ok:
            f["applied"] = True
            if f["action"]:
                f["detail"] += " — moved to scripts/.vault_audit_orphans/%s/" % TS
        else:
            f["sev"] = "review"
            f["detail"] += " [auto-fix skipped: %s]" % err


def _fid(f):
    # Mask only free-standing numbers (counts, versions); digits inside ids like `177415f08261`
    # are kept, so two different job ids can't collapse into one recurrence id.
    base = "%s|%s|%s" % (f["check"], f["target"], re.sub(r"(?<![0-9A-Za-z])\d+(?![0-9A-Za-z])", "#", f["detail"])[:120])
    return hashlib.sha1(base.encode("utf-8", "backslashreplace")).hexdigest()[:10]


def _load_state():
    """The run history, or [] — a malformed file is backed up and reported, not silently wiped."""
    try:
        data = json.load(open(STATE_FILE, encoding="utf-8"))
        hist = data.get("runs") if isinstance(data, dict) else None
        if isinstance(hist, list) and all(isinstance(r, dict) and isinstance(r.get("ids", []), list) for r in hist):
            return hist
        raise ValueError("unexpected shape")
    except FileNotFoundError:
        return []
    except Exception as e:
        try:
            shutil.copy(STATE_FILE, STATE_FILE + ".bak-" + TS)
            kept = "old file kept as .vault_audit_state.json.bak-%s" % TS
        except OSError:
            kept = "old file could NOT be backed up"
        add("state", "review", "(audit)", "recurrence state was unreadable (%s) — started fresh; %s" % (type(e).__name__, kept))
        return []


def annotate_recurrence_and_log():
    """Tag review findings that keep recurring (candidates to codify) and log this run.
    Recurrence counts distinct audit DAYS, so a burst of on-demand runs can't fake a pattern."""
    hist = _load_state()
    review = [f for f in FINDINGS if f["sev"] == "review"]
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    days = []
    for run in reversed(hist):
        d = str(run.get("date", ""))[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < today and d not in days:
            days.append(d)
    days = days[:RECUR_WINDOW - 1]
    ids_now = []
    for f in review:
        fid = _fid(f)
        ids_now.append(fid)
        seen = 1 + len({str(run.get("date", ""))[:10] for run in hist
                        if str(run.get("date", ""))[:10] in days and fid in run.get("ids", [])})
        if seen >= RECUR_THRESHOLD:
            f["detail"] += "  \U0001F4CC recurring (%d of the last %d audit days) — fix it in the vault, or codify the check (drift-patterns.md)" % (seen, len(days) + 1)
    hist.append({"date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "ids": ids_now})
    try:
        _atomic_write(STATE_FILE, json.dumps({"runs": hist[-30:]}, indent=2))
    except Exception:
        pass


def add_rule(js):
    """Validate and append a learned drift rule to learned_rules.json (with backup)."""
    try:
        r = json.loads(js)
    except Exception as e:
        sys.stderr.write("invalid JSON: %s\n" % e)
        return 2
    if not isinstance(r, dict):
        sys.stderr.write("a rule must be a JSON object\n")
        return 2
    req = {"value_equals": ["id", "kind", "note", "anchor", "truth_source", "truth_path"],
           "require_in_note": ["id", "kind", "note", "must"],
           "forbid_in_note": ["id", "kind", "note", "pattern"]}
    k = r.get("kind")
    if k not in req:
        sys.stderr.write("unknown kind %r; use one of %s\n" % (k, list(req)))
        return 2
    missing = [f for f in req[k] if f not in r]
    if missing:
        sys.stderr.write("missing fields for %s: %s\n" % (k, missing))
        return 2
    bad = [f for f in req[k] + ["why"] if f in r and not isinstance(r[f], str)]
    if bad or any("\n" in str(r.get(f, "")) for f in ("id", "why", "note")):
        sys.stderr.write("fields must be single-line strings: %s\n" % (bad or "id/why/note"))
        return 2
    for fld in ("pattern", "anchor"):
        if fld in r:
            try:
                re.compile(r[fld])
            except Exception as e:
                sys.stderr.write("bad regex in %s: %s\n" % (fld, e))
                return 2
            if _risky_regex(r[fld]):
                sys.stderr.write("regex in %s has a nested quantifier (catastrophic backtracking risk) — rewrite it\n" % fld)
                return 2
    if k == "value_equals" and re.compile(r["anchor"]).groups < 1:
        sys.stderr.write("value_equals anchor must have a capture group for the claimed value\n")
        return 2
    if k == "value_equals" and r.get("truth_source") not in ("config", "profile", "env"):
        sys.stderr.write("truth_source must be one of config|profile|env\n")
        return 2
    if k == "value_equals" and _is_secret_key(r.get("truth_path")):
        sys.stderr.write("refusing a rule on a secret-looking key (%s) — values would leak into the report\n" % r.get("truth_path"))
        return 2
    if r.get("sev", "review") not in ("auto", "review"):
        sys.stderr.write("sev must be 'auto' or 'review'\n")
        return 2
    if k != "value_equals" and r.get("sev") == "auto":
        sys.stderr.write("only value_equals rules can be sev=auto (the others have nothing to fix in place)\n")
        return 2
    if not _vault_path(r["note"]):
        sys.stderr.write("note path must be inside the vault\n")
        return 2
    rules, err = _read_rules()
    if rules is None:
        sys.stderr.write("learned_rules.json is unreadable (%s) — fix or move it first; refusing to overwrite it\n" % err)
        return 2
    if any(x.get("id") == r["id"] for x in rules):
        sys.stderr.write("rule id %r already exists\n" % r["id"])
        return 2
    r.setdefault("sev", "review")
    r.setdefault("added", datetime.datetime.now().strftime("%Y-%m-%d"))
    rules.append(r)
    if os.path.exists(RULES_FILE):
        shutil.copy2(RULES_FILE, RULES_FILE + ".bak-" + TS)
    _atomic_write(RULES_FILE, json.dumps({"rules": rules}, indent=2))
    print("added rule '%s' (%s, sev=%s). Total rules: %d" % (r["id"], k, r.get("sev"), len(rules)))
    return 0


def _line(f):
    s = "- %s — %s" % (os.path.basename(f["target"]), re.sub(r"\s+", " ", f["detail"]).strip())
    return s.encode("utf-8", "backslashreplace").decode("utf-8")   # a non-UTF-8 filename can't crash print()


def build_report(fix_mode):
    applied = [f for f in FINDINGS if f["applied"]]
    fixable = [f for f in FINDINGS if f["sev"] == "auto" and not f["applied"]]
    review = [f for f in FINDINGS if not f["applied"] and f["sev"] != "auto"]
    date = datetime.datetime.now().strftime("%d-%b-%Y")
    L = ["\U0001FA7A **Vault Audit** — %s" % date]
    if applied:
        L.append("\n✅ **Auto-fixed (%d):**" % len(applied))
        L += [_line(f) for f in applied]
    if fixable:
        L.append("\n\U0001F527 **Fixable drift (%d, run with --fix):**" % len(fixable))
        L += [_line(f) for f in fixable]
    if review:
        L.append("\n⚠️ **Review (%d):**" % len(review))
        L += [_line(f) for f in review]
    if not (applied or fixable or review):
        L.append("\n✓ Vault in sync — %d checks passed, 0 issues." % len(CHECKS))
    else:
        L.append("\n_(%d checks · %d fixed · %d fixable · %d to review)_" % (len(CHECKS), len(applied), len(fixable), len(review)))
    return "\n".join(L), bool(applied or fixable or review)


DISCORD_LIMIT = 1900          # UTF-16 units per message (Discord's hard cap is 2000)
DELIVERY_BUDGET = 240.0       # seconds for the whole post, well inside the 600 s cron watchdog


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _retry_after(e):
    """Seconds to wait after a 429, from the JSON body or the Retry-After header (finite, >= 0)."""
    ra = None
    try:
        ra = json.loads(e.read().decode()).get("retry_after")
    except Exception:
        pass
    if ra is None:
        try:
            ra = e.headers.get("Retry-After", 1)
        except Exception:
            ra = 1
    try:
        ra = float(ra)
    except (TypeError, ValueError):
        return 1.0
    return ra if math.isfinite(ra) and ra >= 0 else 1.0


def _post_chunk(url, token, chunk, nonce, deadline, attempts=4):
    """POST one chunk; retry on 429 (honoring retry_after), 5xx and network errors with
    bounded backoff, inside `deadline`. Every attempt carries the same `nonce` with
    enforce_nonce, so Discord returns the already-created message instead of posting a
    duplicate when a retry follows a timeout. Returns True only on a 2xx; a 4xx is permanent."""
    body = json.dumps({"content": chunk, "allowed_mentions": {"parse": []}, "flags": 4,
                       "nonce": nonce, "enforce_nonce": True}).encode()
    for attempt in range(attempts):
        if time.monotonic() >= deadline:
            break
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": "Bot %s" % token, "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (hermes-agent/1.0)"})
        wait = 1.5 * (attempt + 1)
        try:
            urllib.request.urlopen(req, timeout=max(1.0, min(30.0, deadline - time.monotonic())))
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(_retry_after(e), 10) + 0.25
            elif not 500 <= e.code < 600:
                sys.stderr.write("discord post failed: HTTP %s (permanent, not retrying)\n" % e.code)
                return False
        except Exception as e:
            sys.stderr.write("discord post error: %s\n" % type(e).__name__)   # never str(e): it can echo request headers
        if attempt + 1 < attempts and time.monotonic() + wait < deadline:
            time.sleep(wait)
        elif attempt + 1 < attempts:
            break
    sys.stderr.write("discord post failed after retries / delivery budget\n")
    return False


def post_discord(channel, content):
    """Self-post the report. Returns True only if EVERY chunk was delivered, so the
    caller can fall back (print to stdout) when delivery fails — critical on the
    deliver:local cron, where the scheduler pushes no failure alert of its own."""
    token = gt_env_value("DISCORD_BOT_TOKEN")
    if not token:
        sys.stderr.write("no DISCORD_BOT_TOKEN; cannot post\n")
        return False
    url = "https://discord.com/api/v10/channels/%s/messages" % channel
    chunks = _split(content, DISCORD_LIMIT - 20)
    if len(chunks) > 1:
        chunks = ["%s\n_(part %d/%d)_" % (c.rstrip("\n"), i + 1, len(chunks)) for i, c in enumerate(chunks)]
    deadline = time.monotonic() + DELIVERY_BUDGET
    for i, chunk in enumerate(chunks):
        nonce = hashlib.sha1(("%s|%s|%d|%s" % (channel, TS, i, chunk)).encode()).hexdigest()[:24]
        if not _post_chunk(url, token, chunk, nonce, deadline):
            sys.stderr.write("[vault-audit] delivered %d of %d message(s)\n" % (i, len(chunks)))
            return False
        time.sleep(0.4)
    return True


def _split(text, limit):
    """Split into chunks of <= limit UTF-16 units (Discord counts UTF-16, so emoji count
    double). Hard-splits any single line longer than the limit — without cutting a
    character in half — and never emits an empty chunk."""
    out, cur = [], ""
    for line in text.split("\n"):
        while _u16(line) >= limit:              # hard-split an over-long single line (>= leaves room for the "\n")
            if cur:
                out.append(cur)
                cur = ""
            piece, n = "", 0
            for ch in line:
                w = 2 if ord(ch) > 0xFFFF else 1
                if n + w > limit - 1:
                    break
                piece, n = piece + ch, n + w
            out.append(piece)
            line = line[len(piece):]
        if cur and _u16(cur) + _u16(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += (line + "\n")
    if cur.strip():
        out.append(cur)
    return [c for c in out if c.strip()]


def _channel_arg(s):
    """Accept a channel/thread id, `<#id>` or `discord:id`; None if it isn't a snowflake."""
    s = re.sub(r"^discord:", "", str(s or "").strip())
    m = re.fullmatch(r"<#(\d+)>|(\d+)", s)
    cid = m and (m.group(1) or m.group(2))
    return cid if cid and cid.isascii() and 17 <= len(cid) <= 20 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit the Hermes Obsidian vault for drift.")
    ap.add_argument("--fix", action="store_true", help="apply auto-fixable drift in place (with backups)")
    ap.add_argument("--post", metavar="CHANNEL", help="self-post the report to this Discord channel id (only if findings)")
    ap.add_argument("--post-home", action="store_true", help="self-post to DISCORD_HOME_CHANNEL")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout unless delivery fails")
    ap.add_argument("--learn-rule", metavar="JSON", help="append a learned drift rule (JSON) and exit")
    ap.add_argument("--list-rules", action="store_true", help="print the learned rules and exit")
    ap.add_argument("--_eval-learned", dest="eval_learned", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.eval_learned:      # internal: the time-boxed child of check_learned_rules
        _eval_learned_rules()
        return 0
    if args.list_rules:
        rules, err = _read_rules()
        if rules is None:
            sys.stderr.write("learned_rules.json is unreadable (%s)\n" % err)
            return 2
        print(json.dumps(rules, indent=2))
        return 0
    if args.learn_rule:
        return add_rule(args.learn_rule)
    channel = None
    if args.post is not None:
        channel = _channel_arg(args.post)
        if not channel:
            sys.stderr.write("--post needs a Discord channel/thread id (17-20 digits), got %r\n" % args.post)
            return 2

    try:
        run_checks()
        if args.fix:
            do_fix()
            _prune_backups()
        annotate_recurrence_and_log()
        report, has_findings = build_report(args.fix)
    except Exception:
        # Last-resort guard: whatever was already applied must still be visible in the saved output.
        print("[vault-audit] internal error — run did not complete:\n" + traceback.format_exc(), flush=True)
        try:
            print(build_report(args.fix)[0], flush=True)
        except Exception:
            pass
        return 4

    if not args.quiet:
        print(report, flush=True)
    if args.post_home and not channel:
        channel = _channel_arg(gt_env_value("DISCORD_HOME_CHANNEL"))
        if not channel and has_findings:
            sys.stderr.write("[vault-audit] --post-home but DISCORD_HOME_CHANNEL is missing/invalid in .env\n")
            if args.quiet:
                print(report, flush=True)
            return 3
    post_failed = False
    if channel and has_findings and not post_discord(channel, report):
        post_failed = True
        sys.stderr.write("[vault-audit] Discord delivery FAILED — the report above was not (fully) delivered\n")
        if args.quiet:   # deliver:local fallback: ensure the saved job-output isn't empty
            print(report, flush=True)

    # 3 = a requested post failed (the only real failure of a completed run); 1 = something is
    # unresolved (unfixed drift or review items) — the cron wrapper maps 1 to 0, so a routine
    # week with review items doesn't read as a failed job.
    if post_failed:
        return 3
    return 1 if any(not f["applied"] for f in FINDINGS) else 0


if __name__ == "__main__":
    sys.exit(main())
