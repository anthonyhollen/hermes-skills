#!/usr/bin/env python3
"""
Generate token usage report table images for Discord.

This is now a THIN WRAPPER over the consolidated renderer in the
discord-table-format skill (scripts/render_tables.py). The CLI/JSON contract is
unchanged, so every documented invocation still works — but rendering now runs
in a single system-Python process with no venv subprocess, draws directly at the
retina point size (no 6000x2000 canvas + LANCZOS upscale), and saves PNGs with
compress_level=1. See discord-table-format SKILL.md "Performance design".

Usage (unchanged):
    # From a JSON file with keys daily/mtd/alltime (flat row arrays):
    python3 gen_token_report_images.py /path/to/data.json
    # From stdin:
    python3 gen_token_report_images.py < input.json
    # Single custom table:
    python3 gen_token_report_images.py --daily '["col1","col2"]' '[["v1","v2"]]'

Data format (JSON) — FLAT row arrays (NOT {headers, rows} objects):
{
  "daily":  [["2026-05-04", "4.5M", "0.1M", "8.8M", "0.4M", "6", "$0.0000"], ...],
  "mtd":    [["35.1M", "2.1M", "146.4M", "12.7M", "$0.0793"], ...],
  "alltime":[["MiniMax-M2.7", "333", "35.1M", "2.1M", "146.4M", "12.7M", "0.1399"], ...]
}

Note: The `src` (MTD by Source) and `top` (Top 5 Sessions) keys were removed in
June 2026. The production cron report only sends Daily, MTD, and All Time. Do
not add `src`/`top` keys to the JSON — they will be silently ignored.
"""
import sys, os, json

# Reuse the single shared renderer from the discord-table-format skill.
_RENDERER_DIR = "/home/amh/.hermes/skills/productivity/discord-table-format/scripts"
sys.path.insert(0, _RENDERER_DIR)
from render_tables import render_table  # noqa: E402

# Hardcoded column headers per report type. Header count MUST equal each row's
# value count (see token-usage SKILL.md "Column Schema").
HEADERS = {
    "daily":   ["Date", "Input", "Output", "Cache Read", "Cache Write", "Sessions", "Est. Cost"],
    "mtd":     ["Sessions", "Input", "Output", "Cache Read", "Cache Write", "Est. Cost"],
    "alltime": ["Model", "Sessions", "Input", "Output", "Cache Read", "Cache Write", "Cost"],
}
PREFIX = {"daily": "daily", "mtd": "mtd", "alltime": "alltime"}


def generate_table_image(headers, data, out_prefix):
    """Back-compat shim — delegates to the shared renderer."""
    return render_table(headers, data, out_prefix=out_prefix)


def _render_report(data):
    """Render every report table present in `data`. One process, one pass."""
    out = {}
    for key, headers in HEADERS.items():
        rows = data.get(key)
        if rows:  # skip tables not present / empty
            out[key] = render_table(headers, rows, out_prefix=PREFIX[key])
    return out


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--daily":
        # Single custom table: --daily '<headers_json>' '<rows_json>'
        headers = json.loads(argv[1])
        rows = json.loads(argv[2])
        print(render_table(headers, rows, out_prefix="custom"))
    else:
        if argv and os.path.isfile(argv[0]):
            with open(argv[0]) as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
        print(json.dumps(_render_report(data)))
