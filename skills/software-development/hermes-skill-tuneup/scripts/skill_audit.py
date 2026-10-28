#!/usr/bin/env python3
"""
hermes-skill-tuneup — skill_audit.py

No-LLM first-pass auditor for a Hermes skill. Mechanizes the parts of the
four-pass tune-up that don't need judgement, so the agent can focus on the rest.

Given a skill directory (or its SKILL.md) it reports:
  • TOKEN BUDGET  — SKILL.md tokens, broken down by H2 section, flagging
                    reference-only content that belongs in references/.
  • LINK INTEGRITY— every references/… , CHANGELOG.md, scripts/… path mentioned
                    in SKILL.md (backticked OR markdown-link) must exist on disk.
  • FRONTMATTER   — name / description / version present; description length
                    (handles `description: >` / `|` block scalars).
  • ANTI-PATTERNS — scripts/*.py scanned for known bug / reliability smells
                    (hermes-send, MEDIA: delivery, a subprocess call with no
                    timeout=, DISCORD token from os.environ [downgraded to CHECK
                    if the file also reads ~/.hermes/.env], unguarded file loads).
                    Docstrings/comments are skipped via the Python tokenizer, so
                    example code in docs is not flagged.
  • DUPLICATION   — verbatim-repeated substantial lines in SKILL.md (wasted
                    hot-path tokens) + broken/merged numbered lists.
  • STRAY FILES   — __pycache__ / *.pyc / *.bak / *.bak-<tag>-<date> clutter to delete.

Token counts are a rough chars/4 estimate — good enough to rank, not exact.

COVERAGE CAVEAT: the anti-pattern scan only reads the skill-dir scripts/.
A skill whose live code lives in ~/.hermes/scripts/ (common for no_agent crons
and interactive skills) has no skill-dir scripts/, so the scan finds nothing —
a *false-clean*. When that happens the tool says so and you can re-point it with
--scripts <dir>.

Usage:
  python3 skill_audit.py <skill_dir | path/to/SKILL.md>
  python3 skill_audit.py <skill_dir> --json
  python3 skill_audit.py <skill_dir> --scripts ~/.hermes/scripts   # scan live code elsewhere
"""
import os, re, sys, json, glob, io, tokenize

def tok(s):                      # rough token estimate
    return round(len(s) / 4)

# H2 section names that usually hold cold / reference-only content.
REF_ONLY_HINTS = ("changelog", "canonical", "recovery", "verification",
                  "history", "appendix", "troubleshooting", "task list")
LARGE_SECTION_TOK = 700          # a single H2 above this is worth trimming/splitting

# (compiled regex, severity, pass, message). Scanned per line over scripts/*.py.
ANTIPATTERNS = [
    (re.compile(r'(["\'])hermes\1\s*,\s*(["\'])send\2|hermes\s+send'), "HIGH", "reliability",
     "hermes-send delivery: `hermes` is not on PATH in non-login shells (cron/agent terminal), and MEDIA: tags get re-batched. Use direct Discord REST multipart — references/delivery-reliability.md."),
    (re.compile(r'MEDIA:(?!\s)'), "HIGH", "reliability",
     "MEDIA: tag delivery: the gateway's extract_media() batches text and images separately, losing pairing/order. Send content+file in ONE curl multipart request. (A real tag is `MEDIA:<path>`/`MEDIA:{var}`; the `(?!\\s)` lookahead skips prose like \"MEDIA: tags…\". A single MEDIA: on a no_agent-deliver/terminal path can be legitimate — see delivery-reliability.md — so eyeball before acting.)"),
    (re.compile(r'os\.environ\s*(?:\[|\.get\()\s*["\']DISCORD_BOT_TOKEN'), "HIGH", "reliability",
     "DISCORD_BOT_TOKEN read from os.environ: the cron scheduler does NOT export it to no_agent scripts. Read ~/.hermes/.env directly."),
    (re.compile(r'json\.load\(\s*open\(|=\s*open\([^)]*\)\.read\(\)'), "MED", "bug",
     "file opened without an obvious guard: a missing/corrupt file crashes with a traceback. Wrap load in try/except and exit cleanly."),
]

def find_skill_dir(arg):
    p = os.path.abspath(os.path.expanduser(arg))
    if os.path.isdir(p):
        return p
    if os.path.isfile(p) and os.path.basename(p) == "SKILL.md":
        return os.path.dirname(p)
    sys.exit(f"ERROR: not a skill dir or SKILL.md: {arg}")

def _fm_value(fm, key):
    """Extract a frontmatter scalar, handling `key: >` / `key: |` block scalars
    (which a same-line regex would otherwise read as just ">"/"|")."""
    lines = fm.split("\n")
    for i, ln in enumerate(lines):
        m = re.match(rf"^([ \t]*){re.escape(key)}:[ \t]*(.*)$", ln)
        if not m:
            continue
        indent, val = len(m.group(1)), m.group(2).strip()
        if val[:1] in (">", "|"):                 # block scalar — gather indented body
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() == "":
                    continue
                if (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                body.append(nxt.strip())
            return " ".join(body).strip()
        return val.strip().strip('"')
    return None

def split_h2(text):
    """Return [(heading, body_with_heading)] split on level-2 headings, plus a
    leading (frontmatter + intro) entry. Headings INSIDE fenced code blocks
    (``` / ~~~) are ignored, so example ## lines in a fence aren't counted as
    real sections."""
    sections = [["(frontmatter + intro)", []]]
    fence = None                                  # active fence marker when inside a code block
    for line in text.split("\n"):
        stripped = line.lstrip()
        fm = re.match(r'(`{3,}|~{3,})', stripped)
        if fm:
            mk = fm.group(1)[:3]
            if fence is None:
                fence = mk
            elif stripped.startswith(fence):
                fence = None
        elif fence is None and re.match(r'## .+$', line):
            sections.append([line.strip().lstrip("# ").strip(), []])
        sections[-1][1].append(line)
    return [(name, "\n".join(body)) for name, body in sections]

def _doc_only_lines(src):
    """1-based line numbers that carry NO real code token (only string/comment/
    whitespace) — docstring/comment lines to skip in the anti-pattern scan.
    Uses the Python tokenizer so a multi-line docstring full of example code
    isn't flagged, and a one-line statement that merely contains a string
    literal (e.g. os.environ["DISCORD_BOT_TOKEN"]) is NOT skipped. Returns None
    if the source won't tokenize, so the caller can fall back."""
    SKIP = {tokenize.STRING, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
            tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
    all_rows, code_rows = set(), set()
    try:
        for ttype, _, (srow, _), (erow, _), _ in tokenize.generate_tokens(io.StringIO(src).readline):
            rows = range(srow, erow + 1)
            all_rows.update(rows)
            if ttype not in SKIP:
                code_rows.update(rows)
    except Exception:
        return None
    return all_rows - code_rows

def _blank_strings_and_comments(src):
    """Source with every string literal's CONTENT and every comment replaced by spaces
    (same offsets), via the tokenize module — so paren-balancing can't be thrown off
    by a ')' inside a string (cc-bill-review import_all.py builds a `python -c` snippet
    whose ')' closed the subprocess.run(...) span early → a false "no timeout=" MED),
    and a `timeout=` inside a string/comment can't satisfy the check. None if the
    source doesn't tokenize."""
    import io
    import tokenize
    lines = src.splitlines(keepends=True)
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln)
    out = list(src)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type not in (tokenize.STRING, tokenize.COMMENT) and \
                    getattr(tokenize, "FSTRING_MIDDLE", None) != tok.type:
                continue
            (sr, sc), (er, ec) = tok.start, tok.end
            a, b = starts[sr - 1] + sc, starts[er - 1] + ec
            keep = 1 if tok.type == tokenize.STRING else 0   # keep the quote chars
            for k in range(a + keep, b - keep):
                if out[k] != "\n":
                    out[k] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError, IndexError):
        return None
    return "".join(out)


def _subprocess_calls_missing_timeout(code_text):
    """Count subprocess.(run|Popen|check_output|call) calls whose OWN argument span
    lacks a timeout= kwarg. Uses paren-BALANCING (not a regex) so arbitrarily-nested
    args — os.path.join(...), list-comps, f-strings with calls — don't defeat it, and
    an UNRELATED requests/socket timeout= elsewhere can't credit a missing one. The
    caller passes source with string contents + comments blanked
    (`_blank_strings_and_comments`), so parens inside strings can't end a span early."""
    missing = 0
    for m in re.finditer(r'subprocess\.(?:run|Popen|check_output|call)\(', code_text):
        i = m.end() - 1                      # the opening '('
        depth, j = 0, i
        while j < len(code_text):
            c = code_text[j]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if 'timeout=' not in code_text[i:j + 1]:
            missing += 1
    return missing

def audit(skill_dir, scripts_dir=None):
    result = {"skill_dir": skill_dir, "token_budget": {}, "link_integrity": [],
              "frontmatter": {}, "anti_patterns": [], "scripts_scanned": 0, "skipped_self": 0}
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        sys.exit(f"ERROR: no SKILL.md in {skill_dir}")
    text = open(skill_md, encoding="utf-8").read()

    # ── frontmatter ──
    fm = text.split("---", 2)[1] if text.startswith("---") else ""
    for key in ("name", "description", "version"):
        result["frontmatter"][key] = _fm_value(fm, key)
    desc = result["frontmatter"].get("description") or ""
    result["frontmatter"]["description_len"] = len(desc)

    # ── token budget by section ──
    total = tok(text)
    sections, ref_only_savings = [], 0
    for name, body in split_h2(text):
        t = tok(body)
        is_ref = any(h in name.lower() for h in REF_ONLY_HINTS)
        if is_ref:
            ref_only_savings += t
        sections.append({"section": name, "tokens": t,
                         "flag": "MOVE-TO-REFERENCES" if is_ref
                                 else ("LARGE-TRIM/SPLIT" if t > LARGE_SECTION_TOK else "")})
    result["token_budget"] = {"total_tokens": total, "sections": sections,
                              "reference_only_savings": ref_only_savings}

    # ── reference-link integrity (backticked AND markdown-link targets) ──
    backticked = re.findall(r'`(references/[^`]+|CHANGELOG\.md|scripts/[^`]+)`', text)
    mdlinks = re.findall(r'\]\((references/[^)\s]+|scripts/[^)\s]+|CHANGELOG\.md)\)', text)
    # A backticked span can carry a trailing invocation (`scripts/x.py --json`) and a
    # link can carry a #fragment (references/x.md#sec) — strip both before the exists
    # check, else a real file reads as a dangling link.
    cleaned = {raw.split()[0].split("#", 1)[0] for raw in (backticked + mdlinks) if raw.strip()}
    for path in sorted(cleaned):
        result["link_integrity"].append({"path": path,
                                          "exists": os.path.exists(os.path.join(skill_dir, path))})

    # ── anti-pattern scan over scripts/*.py ──
    self_path = os.path.realpath(__file__)  # never flag the auditor's own detector source
    scan_root = os.path.expanduser(scripts_dir) if scripts_dir else os.path.join(skill_dir, "scripts")
    for py in sorted(glob.glob(os.path.join(scan_root, "**", "*.py"), recursive=True)):
        # Skip ONLY the auditor's own running source (it holds the ANTIPATTERNS regexes
        # as string literals and would self-flag). Match by realpath, NOT basename — a
        # legit skill script that happens to be named skill_audit.py must still be scanned.
        if os.path.realpath(py) == self_path:
            result["skipped_self"] += 1
            continue
        result["scripts_scanned"] += 1
        rel = os.path.relpath(py, skill_dir)
        src = open(py, encoding="utf-8").read()
        lines = src.splitlines()
        doc_lines = _doc_only_lines(src)   # set, or None → fall back to the triple-quote toggle
        in_doc = False
        for n, line in enumerate(lines, 1):
            if doc_lines is not None:
                skip = n in doc_lines
            else:
                tq = line.count('"""') + line.count("'''")
                skip = in_doc or tq % 2 == 1
                if tq % 2 == 1:
                    in_doc = not in_doc
            if skip or line.strip().startswith("#"):
                continue  # documentation/comment, not executable delivery code
            for rx, sev, phase, msg in ANTIPATTERNS:
                if rx.search(line):
                    result["anti_patterns"].append({"file": rel, "line": n, "severity": sev,
                                                     "pass": phase, "message": msg,
                                                     "code": line.strip()[:100]})
        # file-level subprocess hygiene heuristics (over code lines only, so a
        # `timeout=` in a comment/docstring doesn't falsely satisfy the check)
        code_text = _blank_strings_and_comments(src)
        if code_text is None:
            if doc_lines is not None:
                code_text = "\n".join(l for i, l in enumerate(lines, 1)
                                      if i not in doc_lines and not l.strip().startswith("#"))
            else:
                code_text = src
        n_calls = len(re.findall(r'subprocess\.(?:run|Popen|check_output|call)\(', code_text))
        if n_calls:
            # Only the reliably-detectable signal: a subprocess call with NO timeout=
            # (can hang forever). Paren-balanced per-call so nested args don't misfire
            # and an unrelated timeout= can't mask a missing one. The old "TimeoutExpired
            # not caught" check was dropped: it can't see try-block scoping (a broad
            # `except` catches it; a broad except in an unrelated fn falsely suppressed),
            # so it churned false-positives/negatives — leave "is the timeout caught?"
            # to the human bug-hunt (references/bug-hunt.md "Uncaught TimeoutExpired").
            n_missing = _subprocess_calls_missing_timeout(code_text)
            if n_missing:
                result["anti_patterns"].append({"file": rel, "line": 0, "severity": "MED", "pass": "bug",
                    "message": f"{n_missing} of {n_calls} subprocess call(s) pass no timeout= — a call without a timeout can hang forever.",
                    "code": ""})

        # token-from-env is a LINE-LOCAL flag: a file that ALSO reads ~/.hermes/.env
        # (the recommended fallback, env-key-loading.md) is already correct, so DOWNGRADE
        # the HIGH the reference otherwise tells the agent to ignore. Downgrade, not drop:
        # the file might read .env for an unrelated key, so keep a CHECK to verify.
        if re.search(r'\.hermes/\.env\b|_load_env|open\([^)]*\.env\b', code_text):
            for a in result["anti_patterns"]:
                if a["file"] == rel and a["message"].startswith("DISCORD_BOT_TOKEN read from os.environ"):
                    a["severity"] = "CHECK"
                    a["message"] += " — BUT this file also reads ~/.hermes/.env; a fallback is likely present. Verify it covers this read before acting (line-local flag)."

    # ── duplication in SKILL.md (wasted hot-path tokens + broken numbered lists) ──
    from collections import Counter as _Counter
    md_lines = [l.rstrip() for l in text.split("\n")]
    line_counts = _Counter(l for l in md_lines if len(l.strip()) > 30)
    dup_lines = sorted(({"text": l[:100], "count": n} for l, n in line_counts.items() if n > 1),
                       key=lambda d: -d["count"] * len(d["text"]))
    wasted = sum((d["count"] - 1) * (len(d["text"]) + 1) for d in dup_lines)
    nums = _Counter(m.group(1) for l in md_lines for m in [re.match(r'^\s*(\d+)\. ', l)] if m)
    # Two short lists legitimately reuse 1,2,3 — only flag a repeat that's almost
    # certainly a single broken list: a value >=5, or any value repeated >=3x.
    dup_nums = {n: c for n, c in nums.items() if c > 1 and (int(n) >= 5 or c >= 3)}
    result["duplication"] = {"dup_lines": dup_lines[:10], "wasted_tokens": round(wasted / 4),
                             "dup_list_numbers": dup_nums}

    # ── stray artifacts: clutter, not skill content ──
    # NOTE `*.bak-*` as well as `*.bak`: the real backup convention
    # (safe-edit-workflow.md) is `<file>.bak-<tag>-<date>`, which does NOT end in
    # `.bak` — a bare `*.bak` glob silently misses every dated backup. (Don't use
    # `*.bak*` — it over-matches a legit `.bakup`.)
    strays = []
    for pat in ("**/__pycache__", "**/*.pyc", "**/*.bak", "**/*.bak-*"):
        strays += [os.path.relpath(s, skill_dir)
                   for s in glob.glob(os.path.join(skill_dir, pat), recursive=True)]
    result["stray_files"] = sorted(set(strays))

    return result

def render(r):
    fm = r["frontmatter"]
    print(f"\n=== hermes-skill-tuneup audit: {r['skill_dir']} ===")
    print(f"\n[FRONTMATTER]  name={fm['name']}  version={fm['version']}  desc_len={fm['description_len']}")
    if not fm["name"] or not fm["version"]:
        print("  ⚠ missing name/version")
    if fm["description_len"] == 0:
        print("  ⚠ no description (skill discovery relies on it)")
    elif fm["description_len"] > 240:
        print(f"  ⚠ description is long ({fm['description_len']} chars) — it's always-on; tighten it")

    tb = r["token_budget"]
    print(f"\n[TOKEN BUDGET]  SKILL.md ~{tb['total_tokens']} tok  (hot path — loaded on every trigger)")
    for s in sorted(tb["sections"], key=lambda x: -x["tokens"]):
        tag = f"  <-- {s['flag']}" if s["flag"] else ""
        print(f"  {s['tokens']:>5} tok  {s['section']}{tag}")
    if tb["reference_only_savings"]:
        print(f"  → ~{tb['reference_only_savings']} tok is reference-only and could move to references//CHANGELOG.md")

    print("\n[LINK INTEGRITY]")
    miss = [l for l in r["link_integrity"] if not l["exists"]]
    if not r["link_integrity"]:
        print("  (no references/ or scripts/ links found in SKILL.md)")
    for l in r["link_integrity"]:
        print(f"  {'OK ' if l['exists'] else 'MISSING'} {l['path']}")
    if miss:
        print(f"  ⚠ {len(miss)} dangling reference link(s)")

    print("\n[ANTI-PATTERNS]  (scripts/*.py)")
    if not r["anti_patterns"]:
        print("  none detected")
    for a in sorted(r["anti_patterns"], key=lambda x: {"HIGH": 0, "MED": 1, "CHECK": 2}.get(x["severity"], 3)):
        loc = f"{a['file']}:{a['line']}" if a["line"] else a["file"]
        print(f"  [{a['severity']}/{a['pass']}] {loc}\n      {a['message']}")
        if a["code"]:
            print(f"      > {a['code']}")
    if r.get("scripts_scanned", 0) == 0:
        if r.get("skipped_self", 0) > 0:
            print("  (only the auditor's own skill_audit.py is in scripts/ — nothing else to scan;")
            print("   this is NOT a false-clean — the tune-up skill has no other script.)")
        else:
            print("  ⚠ no scripts/*.py scanned in this skill dir — if the live code lives elsewhere")
            print("    (e.g. ~/.hermes/scripts/ for a no_agent cron), re-run with --scripts <dir>;")
            print("    'none detected' above is a FALSE-CLEAN, not a pass.")

    dup = r.get("duplication", {})
    print("\n[DUPLICATION]  (SKILL.md)")
    if dup.get("dup_lines"):
        print(f"  ~{dup['wasted_tokens']} tok wasted in {len(dup['dup_lines'])} verbatim-repeated line(s):")
        for d in dup["dup_lines"][:6]:
            print(f"   x{d['count']}: {d['text'][:80]}")
    else:
        print("  no repeated substantial lines")
    if dup.get("dup_list_numbers"):
        print(f"  ⚠ duplicate list numbers {dup['dup_list_numbers']} — likely a broken/merged numbered list")

    strays = r.get("stray_files", [])
    if strays:
        print("\n[STRAY FILES]  (delete — clutter, not skill content)")
        for s in strays:
            print(f"  {s}")
    print()

def main():
    # Parse positionally so --scripts consumes its value by INDEX, not by value
    # (a value-equality strip would also drop a skill-dir arg equal to it).
    argv = sys.argv[1:]
    as_json, scripts_dir, positional = "--json" in argv, None, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--scripts":
            if i + 1 >= len(argv):
                sys.exit("ERROR: --scripts requires a directory argument")
            scripts_dir = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            i += 1               # other flags (e.g. --json) handled above / ignored
        else:
            positional.append(a)
            i += 1
    if not positional:
        sys.exit("usage: skill_audit.py <skill_dir | SKILL.md> [--json] [--scripts <dir>]")
    r = audit(find_skill_dir(positional[0]), scripts_dir=scripts_dir)
    print(json.dumps(r, indent=2)) if as_json else render(r)

if __name__ == "__main__":
    main()
