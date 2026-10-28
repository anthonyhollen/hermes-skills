#!/usr/bin/env python3
"""
render_discord.py — turn report.json into Discord table image(s) via the
discord-table-format skill. Best-effort: if that skill's renderer is missing,
prints the table specs as JSON so they can be rendered/sent manually.

Usage: python3 render_discord.py [report.json]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPORT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.path.expanduser("~/agent-eval/report.json"))
RENDERER = Path(os.path.expanduser(
    "~/.hermes/skills/productivity/discord-table-format/scripts/render_tables.py"))


def build_specs(report: dict) -> list:
    obj = report.get("objective", {})
    pair = report.get("pairwise", {})
    specs = []

    if pair:
        rows = []
        for m, c in sorted(pair.items(), key=lambda x: -x[1]["weighted_win_rate"]):
            rows.append([m, f"{c['weighted_win_rate']:.2f}", str(c["win"]), str(c["tie"]), str(c["loss"])])
        specs.append({
            "title": f"Win-rate vs incumbent ({report.get('judge','?')} judge)",
            "headers": ["Model", "WinRate", "W", "T", "L"], "rows": rows,
        })

    rows = []
    for m, c in sorted(obj.items(), key=lambda x: -x[1]["objective_pass_rate"]):
        rows.append([m, f"{c['objective_pass_rate']:.2f}", f"{c['pass']}/{c['n']}",
                     str(c["timeouts"]), str(c["api_errors"]), str(c["refusals"]),
                     str(c["malformed"]), f"{c['p50_latency_s']:.0f}s"])
    specs.append({
        "title": "Objective pass-rate + reliability",
        "headers": ["Model", "Pass", "n", "TO", "ApiErr", "Refuse", "Malf", "p50"], "rows": rows,
    })
    return specs


def main():
    report = json.loads(REPORT.read_text())
    specs = build_specs(report)
    if RENDERER.exists():
        proc = subprocess.run([sys.executable, str(RENDERER)], input=json.dumps(specs),
                              text=True, capture_output=True)
        print(proc.stdout or proc.stderr)
    else:
        print("# discord-table-format renderer not found; table specs below:")
        print(json.dumps(specs, indent=2))


if __name__ == "__main__":
    main()
