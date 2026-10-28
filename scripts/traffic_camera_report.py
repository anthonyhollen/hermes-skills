#!/usr/bin/env python3
"""
traffic_camera_report.py — self-delivering traffic-camera route report.

Runs the validated route pipeline (commute_camera_map.py), renders a
`# / CamID / Dist / Address` table, and delivers the map + table image to the
CURRENT Discord thread ITSELF (direct REST multipart curl). The destination is
resolved from the gateway session (HERMES_SESSION_KEY) — the caller never picks
a channel, so it delivers to the conversation it was invoked from and never to a
fixed/home channel like #alerts.

The agent's only job is to run this once with the route; it does NO delivery.

Usage (run exactly this; do NOT add a channel):
    python3 ~/.hermes/scripts/traffic_camera_report.py --start "Home" --end "670238"

Override thread target (when session env vars are stale/wrong):
    python3 ~/.hermes/scripts/traffic_camera_report.py --start Home --end 449308 \
            --thread-id 1513509978827391077

Testing / override (post to an explicit channel instead of the current thread):
    python3 ~/.hermes/scripts/traffic_camera_report.py --start Home --end Innovis \
            --to discord:1501742338639401071
"""

import argparse
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.expanduser("~/.hermes/scripts")
sys.path.insert(0, SCRIPT_DIR)


def load_token():
    """Discord bot token from ~/.hermes/.env (no quotes-stripping needed)."""
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def resolve_discord_target(explicit=None, thread_id_override=None):
    """Return the Discord channel/thread id to POST into.

    Precedence: explicit --to  →  --thread-id  →  current gateway session
    (HERMES_SESSION_KEY) →  HERMES_SESSION_THREAD_ID/CHAT_ID (cron/cli compat).
    NEVER falls back to a fixed channel — returns None if it can't tell which
    conversation it is in, so the caller fails loud instead of misfiring.
    """
    if explicit:
        ref = explicit.split("discord:", 1)[-1] if explicit.startswith("discord:") else explicit
        parts = [p for p in ref.split(":") if p]
        return parts[-1] if parts else None  # thread id if given, else channel id

    # Explicit thread-id override — the agent passes this when it knows the
    # actual thread ID from session context (HERMES_SESSION_THREAD_ID can be
    # stale for Discord auto-threads; see gateway issue analysis).
    if thread_id_override:
        return thread_id_override.strip()

    # Session key format: agent:main:<platform>:<chat_type>:<chat_id>[:<suffix>...]
    # parts[4] (chat_id) is the channel/thread to post into.
    sk = os.environ.get("HERMES_SESSION_KEY", "")
    parts = sk.split(":")
    if len(parts) >= 5 and parts[2] == "discord" and parts[4]:
        return parts[4]

    tid = os.environ.get("HERMES_SESSION_THREAD_ID", "").strip()
    cid = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    return tid or cid or None


def discord_post(token, channel, content, img=None, _retry=True):
    """One content (+ optional image) message via direct REST multipart curl.
    Returns (http_code, body). Retries once on 429."""
    import json as _json
    payload = _json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    cmd = ["curl", "-sS", "--max-time", "20", "-w", "\n%{http_code}",
           "-X", "POST", url, "-H", f"Authorization: Bot {token}",
           "-F", f"payload_json={payload}"]
    if img and os.path.exists(img):
        cmd += ["-F", f"file=@{img}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return "timeout", ""
    body, _, code = r.stdout.rpartition("\n")
    code = code.strip()
    if code == "429" and _retry:
        try:
            time.sleep(float(json.loads(body).get("retry_after", 1)) + 0.25)
        except Exception:
            time.sleep(1)
        return discord_post(token, channel, content, img, _retry=False)
    return code, body


def render_table_png(headers, rows, out_path):
    """Render a Discord-dark table image (tabulate fancy_grid + Pillow),
    following the discord-table-format skill's approach."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.path.insert(0, "/usr/lib/python3/dist-packages")
        from PIL import Image, ImageDraw, ImageFont
    from tabulate import tabulate

    FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    BG, FG, PAD, SP = (30, 31, 34), (224, 224, 224), 30, 8
    lines = tabulate(rows, headers=headers, tablefmt="fancy_grid").split("\n")
    font = ImageFont.truetype(FONT, 20)
    cw = font.getlength("0")
    bb = font.getbbox("╞")
    ch = bb[3] - bb[1]
    W = int(max(len(l) for l in lines) * cw) + PAD * 2
    H = len(lines) * (ch + SP) + PAD * 2
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = PAD
    for l in lines:
        d.text((PAD, y), l, font=font, fill=FG)
        y += ch + SP
    img.save(out_path)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Self-delivering traffic-camera route report")
    ap.add_argument("--start", default="Home", help="Start address or shortcut")
    ap.add_argument("--end", default="Innovis", help="End address, shortcut, or postal code")
    ap.add_argument("--threshold", type=int, default=500, help="Max camera distance from route (m)")
    ap.add_argument("--no-geocode", action="store_true", help="Skip address lookup (faster)")
    ap.add_argument("--to", help="Override target: discord:<channel_id>[:<thread_id>] (testing)")
    ap.add_argument("--thread-id", help="Explicit Discord thread ID (bypasses stale session env)")
    args = ap.parse_args()

    token = load_token()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not found in ~/.hermes/.env", file=sys.stderr)
        sys.exit(1)

    channel = resolve_discord_target(args.to, args.thread_id)
    if not channel:
        print("ERROR: could not resolve the current Discord thread (HERMES_SESSION_KEY unset "
              "and no --to). Refusing to guess a channel.", file=sys.stderr)
        sys.exit(2)

    # Validate dependencies before incurring API costs
    try:
        from PIL import Image, ImageDraw, ImageFont
        from tabulate import tabulate
    except ImportError:
        try:
            sys.path.insert(0, "/usr/lib/python3/dist-packages")
            from PIL import Image, ImageDraw, ImageFont
            from tabulate import tabulate
        except ImportError as e:
            print(f"ERROR: missing dependency — {e}. Install with: pip install Pillow tabulate",
                  file=sys.stderr)
            sys.exit(1)

    # Run the validated route pipeline (produces the map PNG + JSON with addresses)
    cache_dir = "/tmp/hermes_cache"
    os.makedirs(cache_dir, exist_ok=True)
    stamp = int(time.time())
    map_png = os.path.join(cache_dir, f"tc_route_{stamp}.png")
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "commute_camera_map.py"),
           "--start", args.start, "--end", args.end,
           "--threshold", str(args.threshold), "--output", map_png]
    if args.no_geocode:
        cmd.append("--no-geocode")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("ERROR: route pipeline timed out after 120s", file=sys.stderr)
        sys.exit(3)
    except FileNotFoundError:
        print("ERROR: commute_camera_map.py not found", file=sys.stderr)
        sys.exit(3)
    if proc.returncode != 0 or "{" not in proc.stdout:
        print(f"ERROR: route pipeline failed (rc={proc.returncode}): "
              f"{(proc.stderr or proc.stdout)[-300:]}", file=sys.stderr)
        sys.exit(3)
    try:
        res = json.loads(proc.stdout[proc.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: could not parse route pipeline JSON output: {e}", file=sys.stderr)
        sys.exit(3)

    # Render the # / CamID / Dist / Address table
    table_png = os.path.join(cache_dir, f"tc_table_{stamp}.png")
    rows = [[c["label"], c["camera_id"], f"{c['distance_m']}m", c.get("address") or "?"]
            for c in res["cameras"]]
    if rows:
        render_table_png(["#", "CamID", "Dist", "Address"], rows, table_png)

    # Deliver map + table INTO the current thread
    cap = (f"## 🚗 Traffic camera route map\n"
           f"**{res['start']}** → **{res['end']}**  "
           f"{res['distance_km']} km · ~{res['duration_min']} min\n"
           f"📸 {res['cameras_count']} cameras along route")
    code_map, _ = discord_post(token, channel, cap, map_png)
    ok = code_map.startswith("2")
    code_tbl = "skip"
    if rows:
        time.sleep(0.6)
        code_tbl, _ = discord_post(token, channel, "### 📋 List of cameras along route", table_png)
        ok = ok and code_tbl.startswith("2")

    for p in (map_png, table_png):
        try:
            os.remove(p)
        except OSError:
            pass

    if ok:
        print(f"[delivered] route {res['start']} → {res['end']} "
              f"({res['cameras_count']} cameras) to discord:{channel}")
    else:
        print(f"ERROR: delivery failed (map HTTP {code_map}, table HTTP {code_tbl}) "
              f"to discord:{channel}", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
