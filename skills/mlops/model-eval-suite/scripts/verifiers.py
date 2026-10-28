#!/usr/bin/env python3
"""
verifiers.py — deterministic objective verifiers + reliability metrics for
model-eval-suite v2. Each verifier returns (passed: bool, reason: str).

verify(task, run) dispatches on task["verify"]["type"]; reliability(run) derives
the timeout/error/refusal/malformed-tool-call signals from a RunResult.
"""
from __future__ import annotations

import json
import re

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")
_REFUSAL = re.compile(
    r"(?i)\b(i\s*(can'?t|cannot|am\s+unable|am\s+not\s+able)|i'?m\s+sorry,?\s+but|"
    r"unable\s+to\s+(help|assist|comply)|i\s+won'?t\s+be\s+able)\b"
)
_FILLER = ("great question", "happy to help", "sure,", "i'd be happy",
           "certainly!", "of course!", "let me", "here is", "here's")


def _nums(s: str) -> list[float]:
    out = []
    for m in _NUM.findall(s or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _strip_json(s: str) -> str:
    s = (s or "").strip()
    if "```" in s:
        parts = s.split("```")
        # take the largest fenced block
        cand = max((p for p in parts), key=len, default=s)
        cand = re.sub(r"^(json|python)\s*", "", cand.strip(), flags=re.I)
        s = cand.strip()
    return s


def _find_json(s: str, kind):
    """Best-effort: find the first JSON value of the requested type."""
    s = _strip_json(s)
    try:
        v = json.loads(s)
        if isinstance(v, kind):
            return v
    except Exception:
        pass
    # fall back to bracket scan
    opens = "[" if kind is list else "{"
    closes = "]" if kind is list else "}"
    i = s.find(opens)
    if i >= 0:
        depth = 0
        for j in range(i, len(s)):
            if s[j] == opens:
                depth += 1
            elif s[j] == closes:
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[i:j + 1])
                        if isinstance(v, kind):
                            return v
                    except Exception:
                        return None
    return None


def verify(task: dict, run) -> tuple[bool, str]:
    spec = task.get("verify") or {}
    t = spec.get("type")
    ans = run.answer or ""
    low = ans.lower()

    if not run.ok:
        return False, f"run not ok (exit={run.exit_code}, timed_out={run.timed_out})"

    if t == "numeric":
        want = float(spec["expected"]); tol = float(spec.get("tol", 0))
        got = _nums(ans)
        hit = any(abs(g - want) <= tol for g in got)
        return hit, f"expected {want}±{tol}; nums={got[:8]}"

    if t == "contains":
        # normalize thousands separators so "4,000" matches "4000"
        norm = re.sub(r"(?<=\d),(?=\d)", "", low)
        need = [re.sub(r"(?<=\d),(?=\d)", "", str(x).lower()) for x in spec["must_include"]]
        missing = [n for n in need if n not in norm]
        return (not missing), ("all present" if not missing else f"missing {missing}")

    if t == "ordered_values":
        seq = spec["must_include_in_order"]
        pos = []
        cur = 0
        ok = True
        for v in seq:
            idx = low.find(str(v).lower(), cur)
            if idx < 0:
                ok = False
                break
            pos.append(idx); cur = idx + 1
        return ok, ("in order" if ok else f"order/﻿presence failed at {seq[len(pos)] if len(pos)<len(seq) else '?'}")

    if t == "regex":
        ok = bool(re.search(spec["pattern"], ans))
        return ok, ("matched" if ok else f"no match for {spec['pattern']!r}")

    if t == "word_limit":
        wc = len(ans.split())
        within = wc <= int(spec["max_words"])
        filler_ok = True
        if spec.get("no_filler"):
            filler_ok = not any(low.startswith(f) or f in low[:40] for f in _FILLER)
        return (within and filler_ok), f"words={wc} (max {spec['max_words']}), filler_ok={filler_ok}"

    if t == "markdown_table":
        has = ("|" in ans) and bool(re.search(r"\|?\s*:?-{2,}", ans))
        cells = spec.get("require_cells", [])
        missing = [c for c in cells if not re.search(rf"(?<![A-Za-z]){re.escape(c)}(?![A-Za-z])", ans)]
        ok = has and not missing
        return ok, f"table={has}, missing_cells={missing}"

    if t == "json_array_check":
        arr = _find_json(ans, list)
        if arr is None:
            return False, "no JSON array parsed"
        if len(arr) < int(spec.get("min_len", 1)):
            return False, f"len {len(arr)} < {spec.get('min_len')}"
        cond = spec.get("any_object_has")
        if cond:
            ok = any(isinstance(o, dict) and all(str(o.get(k, "")).lower() == str(v).lower()
                     for k, v in cond.items()) for o in arr)
            return ok, ("found matching object" if ok else f"no object matching {cond}")
        return True, f"array len {len(arr)}"

    if t == "json_fields":
        obj = _find_json(ans, dict)
        if obj is None:
            return False, "no JSON object parsed"
        reasons = []
        ok = True
        for k, v in (spec.get("expect") or {}).items():
            gv = obj.get(k)
            if isinstance(v, (int, float)):
                try:
                    if abs(float(gv) - float(v)) > 0.001:
                        ok = False; reasons.append(f"{k}={gv}!={v}")
                except (TypeError, ValueError):
                    ok = False; reasons.append(f"{k}={gv} not numeric")
            else:
                if str(gv).lower() != str(v).lower():
                    ok = False; reasons.append(f"{k}={gv}!={v}")
        for k, sub in (spec.get("contains_field_substr") or {}).items():
            if str(sub).lower() not in str(obj.get(k, "")).lower():
                ok = False; reasons.append(f"{k} lacks {sub!r}")
        return ok, ("fields ok" if ok else "; ".join(reasons))

    if t == "skill_loaded":
        want = [w.lower() for w in spec.get("expect_any", [])]
        for m in run.trajectory:
            for tc in m.get("tool_calls", []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                if fn.get("name") in ("skill_view", "skill_run", "skill", "skills_list"):
                    args = fn.get("arguments", "")
                    al = (args if isinstance(args, str) else json.dumps(args)).lower()
                    if any(w in al for w in want):
                        return True, f"loaded a target skill ({want})"
        return False, f"no skill_view for {want}"

    if t == "judge_only":
        return True, "no objective check (judged only)"

    return False, f"unknown verify type {t!r}"


def reliability(run) -> dict:
    """Per-run reliability signals (aggregated across runs in the report)."""
    ans = run.answer or ""
    refusal = bool(_REFUSAL.search(ans)) and len(ans) < 600
    api_error = (not run.timed_out) and (run.exit_code != 0 or not ans.strip())
    malformed = 0
    for m in run.trajectory:
        for tc in m.get("tool_calls", []):
            if not isinstance(tc, dict):
                malformed += 1; continue
            if "_unparsed" in tc:
                malformed += 1; continue
            fn = tc.get("function", {})
            if not fn.get("name"):
                malformed += 1; continue
            argstr = fn.get("arguments", "")
            if isinstance(argstr, str) and argstr.strip():
                try:
                    json.loads(argstr)
                except Exception:
                    malformed += 1
    return {
        "timed_out": run.timed_out,
        "api_error": api_error,
        "refusal": refusal,
        "malformed_tool_calls": malformed,
        "trajectory_len": len(run.trajectory),
        "elapsed_s": run.elapsed_s,
    }
