#!/usr/bin/env python3
"""
judge.py — pluggable pairwise judge for model-eval-suite v2.

Compares each candidate's (trajectory + answer) against the incumbent
MiniMax-M2.7 on the same task. A/B anonymized + order-randomized to kill
position bias. Two backends, identical rubric + schema:

  - "minimax-m3"     (DEFAULT, autonomous): MiniMax-M3 judges via API on-host.
                     If a candidate IS the judge model, fall back to mimo-v2.5-pro.
  - "claude-external" (on instruction, neutral): writes judge_queue.jsonl (+ a
                     private judge_key.json mapping) and STOPS. Claude reads the
                     queue, writes judge_verdicts.jsonl; rerun with resume to score.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import requests

DEFAULT_JUDGE = "MiniMax-M3"
FALLBACK_JUDGE = "mimo-v2.5-pro"
INCUMBENT_ID = "MiniMax-M2.7"

RUBRIC = (
    "You are a strict, neutral evaluator comparing two AI agent responses (A and B) to the "
    "SAME task. Judge on, in priority order: (1) correctness of the final answer, "
    "(2) instruction-following / output format, (3) completeness, (4) tool-use efficiency "
    "(fewer wasted steps is better), (5) reliability (a refusal, error, or empty answer loses). "
    "Ignore verbosity and style unless the task asked for concision. Pick the better response. "
    "Only answer 'tie' if they are genuinely indistinguishable in quality. "
    'Reply with ONLY a JSON object: {"winner":"A"|"B"|"tie","confidence":0.0-1.0,"rationale":"one sentence"}.'
)


def _env() -> dict:
    e = {}
    p = Path(os.environ.get("HERMES_REAL_HOME", os.path.expanduser("~/.hermes"))) / ".env"
    if p.exists():
        for l in p.read_text().splitlines():
            l = l.strip()
            if "=" in l and not l.startswith("#"):
                k, _, v = l.partition("="); e[k.strip()] = v.strip()
    return e


def compact_trajectory(run, max_steps: int = 12, max_chars: int = 400) -> str:
    """Readable trajectory summary for a judge packet (no secrets — runs were
    executed with HERMES_REDACT_SECRETS=1)."""
    lines = []
    for m in run.trajectory[:max_steps * 2]:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            names = [tc.get("function", {}).get("name", "?") if isinstance(tc, dict) else "?"
                     for tc in m["tool_calls"]]
            lines.append(f"[assistant calls: {', '.join(names)}]")
        elif role == "tool":
            lines.append(f"[tool {m.get('tool_name')}] -> {m.get('content','')[:160]}")
        elif role == "assistant" and m.get("content"):
            lines.append(f"[assistant] {m['content'][:max_chars]}")
    return "\n".join(lines) or "(no intermediate steps)"


def _packet(task: dict, cand_run, inc_run, rng: random.Random) -> tuple[dict, dict]:
    """Return (queue_packet_without_mapping, key_entry)."""
    cand_view = {"answer": cand_run.answer, "trajectory": compact_trajectory(cand_run),
                 "steps": cand_run.tool_calls}
    inc_view = {"answer": inc_run.answer, "trajectory": compact_trajectory(inc_run),
                "steps": inc_run.tool_calls}
    flip = rng.random() < 0.5
    a, b = (cand_view, inc_view) if not flip else (inc_view, cand_view)
    pair_id = f"{cand_run.model}__{task['id']}"
    packet = {
        "pair_id": pair_id, "task_id": task["id"], "candidate": cand_run.model,
        "task_prompt": task["prompt"], "rubric": RUBRIC,
        "A": a, "B": b,
    }
    key = {"pair_id": pair_id, "A_is": ("candidate" if not flip else "incumbent"),
           "B_is": ("incumbent" if not flip else "candidate"),
           "candidate": cand_run.model, "task_id": task["id"]}
    return packet, key


def build_pairs(results: list, tasks_by_id: dict, seed: int = 1234) -> tuple[list, list]:
    """results: list of RunResult. Build pairs of (candidate vs incumbent) per task.
    Returns (packets, keys)."""
    rng = random.Random(seed)
    inc = {(r.task_id): r for r in results if r.model == INCUMBENT_ID}
    packets, keys = [], []
    for r in results:
        if r.model == INCUMBENT_ID:
            continue
        inc_run = inc.get(r.task_id)
        if inc_run is None:
            continue
        p, k = _packet(tasks_by_id[r.task_id], r, inc_run, rng)
        packets.append(p); keys.append(k)
    return packets, keys


# ── MiniMax / mimo API judge ───────────────────────────────────────
def _call_minimax(model: str, system: str, prompt: str, key: str) -> str:
    url = "https://api.minimax.io/anthropic/v1/messages"
    r = requests.post(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "Content-Type": "application/json"},
                      json={"model": model, "max_tokens": 1024, "system": system,
                            "messages": [{"role": "user", "content": prompt}], "temperature": 0.0},
                      timeout=120)
    d = r.json()
    if "error" in d:
        return f"ERROR: {d['error']}"
    blocks = [b for b in d.get("content", []) if b.get("type") == "text"]
    if blocks:
        return blocks[0]["text"]
    if d.get("content"):
        last = d["content"][-1]
        return last.get("text", last.get("thinking", json.dumps(last)))
    return "ERROR: empty"


def _parse_verdict(text: str) -> dict:
    s = text
    if "```" in s:
        s = max(s.split("```"), key=len)
        s = re.sub(r"^json\s*", "", s.strip(), flags=re.I)
    try:
        i, j = s.find("{"), s.rfind("}")
        obj = json.loads(s[i:j + 1])
        w = str(obj.get("winner", "tie")).strip().upper()
        w = "A" if w.startswith("A") else "B" if w.startswith("B") else "tie"
        return {"winner": w, "confidence": float(obj.get("confidence", 0.5)),
                "rationale": str(obj.get("rationale", ""))[:300]}
    except Exception:
        wl = text.lower()
        w = "A" if "winner\": \"a" in wl or "winner: a" in wl else \
            "B" if "winner\": \"b" in wl or "winner: b" in wl else "tie"
        return {"winner": w, "confidence": 0.3, "rationale": "parse-fallback"}


def judge_minimax(packets: list, judge_model: str = DEFAULT_JUDGE) -> list:
    """Judge each packet via the MiniMax/mimo API. Holds out the judge model:
    if a packet's candidate == judge_model, use FALLBACK_JUDGE for that packet."""
    env = _env()
    mm_key = env.get("MINIMAX_API_KEY", "")
    verdicts = []
    for p in packets:
        jm = FALLBACK_JUDGE if p["candidate"] == judge_model else judge_model
        prompt = (f"TASK:\n{p['task_prompt']}\n\n"
                  f"--- Response A ---\nFINAL ANSWER:\n{p['A']['answer']}\n\nSTEPS ({p['A']['steps']} tool calls):\n{p['A']['trajectory']}\n\n"
                  f"--- Response B ---\nFINAL ANSWER:\n{p['B']['answer']}\n\nSTEPS ({p['B']['steps']} tool calls):\n{p['B']['trajectory']}\n")
        if jm == FALLBACK_JUDGE:   # mimo (OpenAI-compatible) — route via minimax disabled; use mimo path
            txt = _call_mimo(jm, p["rubric"], prompt, env)
        else:
            txt = _call_minimax(jm, p["rubric"], prompt, mm_key)
        v = _parse_verdict(txt)
        v.update({"pair_id": p["pair_id"], "judge": jm, "candidate": p["candidate"], "task_id": p["task_id"]})
        verdicts.append(v)
        time.sleep(0.5)
    return verdicts


def _call_mimo(model: str, system: str, prompt: str, env: dict) -> str:
    url = f"{env.get('XIAOMI_BASE_URL', 'https://token-plan-sgp.xiaomimimo.com/v1')}/chat/completions"
    r = requests.post(url, headers={"Authorization": f"Bearer {env.get('XIAOMI_API_KEY','')}",
                                    "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": 4096},
                      timeout=120)
    d = r.json()
    if "error" in d:
        return f"ERROR: {d['error']}"
    return d["choices"][0]["message"]["content"]


# ── Claude-external backend ────────────────────────────────────────
def write_queue(packets: list, keys: list, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    q = out_dir / "judge_queue.jsonl"
    with q.open("w") as f:
        for p in packets:
            f.write(json.dumps(p) + "\n")
    (out_dir / "judge_key.json").write_text(json.dumps({k["pair_id"]: k for k in keys}, indent=2))
    return q


def load_verdicts(path: Path) -> list:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


# ── Scoring ────────────────────────────────────────────────────────
def score(verdicts: list, keys: list, tasks_by_id: dict) -> dict:
    """Map A/B verdicts back to candidate/incumbent and aggregate per candidate,
    usage-weighted by task weight."""
    keymap = {k["pair_id"]: k for k in keys}
    per_cand = {}
    for v in verdicts:
        k = keymap.get(v["pair_id"])
        if not k:
            continue
        winner = v["winner"]
        if winner == "tie":
            outcome = "tie"
        else:
            who = k["A_is"] if winner == "A" else k["B_is"]
            outcome = "win" if who == "candidate" else "loss"   # candidate's perspective
        cand = k["candidate"]; tid = k["task_id"]
        w = float(tasks_by_id.get(tid, {}).get("weight", 1))
        c = per_cand.setdefault(cand, {"win": 0, "tie": 0, "loss": 0,
                                       "w_win": 0.0, "w_tie": 0.0, "w_loss": 0.0,
                                       "w_total": 0.0, "tasks": []})
        c[outcome] += 1
        c[f"w_{outcome}"] += w
        c["w_total"] += w
        c["tasks"].append({"task": tid, "outcome": outcome, "winner": winner,
                           "confidence": v.get("confidence"), "rationale": v.get("rationale")})
    for cand, c in per_cand.items():
        wt = c["w_total"] or 1
        # weighted win-rate vs incumbent: wins + 0.5*ties over total
        c["weighted_win_rate"] = round((c["w_win"] + 0.5 * c["w_tie"]) / wt, 3)
        c["raw_win_rate"] = round((c["win"] + 0.5 * c["tie"]) / max(1, c["win"] + c["tie"] + c["loss"]), 3)
    return per_cand
