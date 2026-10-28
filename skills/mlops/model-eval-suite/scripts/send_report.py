#!/usr/bin/env python3
"""Send the complete model-eval report to Discord #alerts (header + table images)."""
import json, os, subprocess, time, sys

HA = "/home/amh/.hermes"
CHANNEL = "1499972245437550602"  # #alerts
RENDERER = f"{HA}/skills/productivity/discord-table-format/scripts/render_tables.py"

# bot token from .env
TOKEN = ""
for l in open(f"{HA}/.env"):
    if l.startswith("DISCORD_BOT_TOKEN="):
        TOKEN = l.split("=", 1)[1].strip()

report = json.load(open("/home/amh/agent-eval/report.json"))
cons = json.load(open("/home/amh/agent-eval/consistency.json"))
obj, pair = report["objective"], report["pairwise"]

# ── build table specs ──
pair_rows = [[m, f"{c['weighted_win_rate']:.2f}", str(c['win']), str(c['tie']), str(c['loss'])]
             for m, c in sorted(pair.items(), key=lambda x: -x[1]['weighted_win_rate'])]
obj_rows = [[m, f"{c['objective_pass_rate']:.2f}", f"{c['pass']}/{c['n']}",
             str(c['timeouts'] + c['api_errors'] + c['refusals']), f"{c['p50_latency_s']:.0f}s"]
            for m, c in sorted(obj.items(), key=lambda x: -x[1]['objective_pass_rate'])]
cons_rows = []
for m, c in sorted(cons.items(), key=lambda x: (-x[1]['passk_rate'], -x[1]['mean_pass_rate'])):
    flaky = ",".join(c.get('flaky_tasks', []) + c.get('failed_tasks', [])) or "—"
    cons_rows.append([m, f"{c['passk_rate']:.2f}", f"{c['mean_pass_rate']:.2f}",
                      f"{c['p50_latency_s']:.0f}s", f"{c['latency_stdev_s']:.0f}", flaky])

specs = [
    {"title": "Pairwise win-rate vs MiniMax-M2.7 (0.50 = parity)",
     "headers": ["Model", "WinRate", "W", "T", "L"], "rows": pair_rows},
    {"title": "Objective pass-rate + reliability (15 tasks)",
     "headers": ["Model", "Pass", "n", "Errors", "p50"], "rows": obj_rows},
    {"title": "Consistency: 8 hard tasks x 3 reps",
     "headers": ["Model", "pass^k", "mean", "p50", "latSD", "Flaky/Failed"], "rows": cons_rows},
]

proc = subprocess.run([sys.executable, RENDERER], input=json.dumps(specs),
                      text=True, capture_output=True)
imgs = json.loads(proc.stdout.strip().splitlines()[-1])
print("rendered:", imgs)
img_by_title = {t: p for t, p in imgs.items()}

# ── message sections (header text -> optional image) ──
overview = (
    "# 🧪 Model Evaluation Report — Hermes Agent\n"
    "**Incumbent:** `MiniMax-M2.7`  ·  **Candidates:** MiniMax-M3, mimo-v2.5, mimo-v2.5-pro, "
    "deepseek-v4-flash, deepseek-v4-pro\n"
    "**Method:** real `hermes -z` agent loop in an isolated sandbox with a restricted toolset. "
    "Two evals — (1) **15 usage-grounded tasks** judged **pairwise vs incumbent** by a neutral judge (Claude); "
    "(2) **8 hard / error-recovery tasks run 3×** for consistency.\n"
    "**Reliability:** every model had **zero** timeouts, API errors, refusals, or malformed tool calls.\n\n"
    "**1️⃣ Pairwise win-rate vs incumbent** _(0.50 = parity; <0.50 loses to incumbent)_:"
)
verdict = (
    "**🏁 Verdict vs incumbent (MiniMax-M2.7)**\n"
    "🥇 **deepseek-v4-pro** — only model at **quality parity** (0.50); faster typical turn; ⚠️ but a **197s latency tail**.\n"
    "⚡ **deepseek-v4-flash** — **fastest** (~2× incumbent), steadiest latency, zero flakes; weakest on output polish (0.23). "
    "_(your current default — well justified on speed/reliability)_\n"
    "⚠️ **mimo-v2.5** — fast, but the **only model that flaked** (multi-file-join: 2/3); avoid as default.\n"
    "➖ **mimo-v2.5-pro** — no edge over incumbent; one factual error (wrong year).\n"
    "➖ **MiniMax-M3** — no quality gain; **147s latency tail**.\n\n"
    "**Bottom line:** the incumbent remains the **safest** (flawless reliability, but slowest). "
    "Only **deepseek-v4-pro** (max quality) or **deepseek-v4-flash** (max speed) are defensible swaps."
)

sections = [
    (overview, img_by_title.get(specs[0]["title"])),
    ("**2️⃣ Objective pass-rate + reliability** _(usage-weighted)_:", img_by_title.get(specs[1]["title"])),
    ("**3️⃣ Consistency** — hard / error-recovery tasks, `pass^k` = passed all 3 reps:", img_by_title.get(specs[2]["title"])),
    (verdict, None),
]

for content, img in sections:
    cmd = ['curl', '-s', '-X', 'POST',
           f'https://discord.com/api/v10/channels/{CHANNEL}/messages',
           '-H', f'Authorization: Bot {TOKEN}', '-F', f'content={content}']
    if img:
        cmd += ['-F', f'file=@{img}']
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = '"id"' in r.stdout and '"code"' not in r.stdout.split('"id"')[0][:50]
    print("sent:", "OK" if r.returncode == 0 and '"id"' in r.stdout else "FAIL",
          "| img:", bool(img), "| resp:", r.stdout[:120])
    time.sleep(0.6)
print("done")
