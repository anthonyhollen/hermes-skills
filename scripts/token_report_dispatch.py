#!/usr/bin/env python3
"""Token usage report dispatch — sends header+image pairs atomically to Discord.

Uses dynamic image discovery to find the latest PNGs from the report run.
Must be [SILENT]-suppressing no_agent script for the cron scheduler.
"""
import subprocess, os, glob, time

# CRITICAL: [SILENT] must be the FIRST line of output to suppress scheduler delivery.
# The scheduler delivers this script's stdout as a Discord message, so everything
# after this marker must be invisible to the scheduler (only curl sees it).
print("[SILENT]", flush=True)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
CHANNEL = "1499972245437550602"  # production work home

# --- Dynamic image discovery ---
# Find the most recently created PNGs from this report run.
# The image generator writes to /tmp/hermes_cache/ with timestamps in filenames.
# We glob and sort by mtime to get the latest set.
pngs = sorted(glob.glob("/tmp/hermes_cache/daily_*.png"), key=os.path.getmtime)
mtd_pngs = sorted(glob.glob("/tmp/hermes_cache/mtd_*.png"), key=os.path.getmtime)
alltime_pngs = sorted(glob.glob("/tmp/hermes_cache/alltime_*.png"), key=os.path.getmtime)

daily_img = pngs[-1] if len(pngs) >= 1 else None
mtd_img   = mtd_pngs[-1] if len(mtd_pngs) >= 1 else None
alltime_img = alltime_pngs[-1] if len(alltime_pngs) >= 1 else None

if not all([daily_img, mtd_img, alltime_img]):
    # Fallback: find any PNG in order of mtime
    all_pngs = sorted(glob.glob("/tmp/hermes_cache/*.png"), key=os.path.getmtime)
    # Match by prefix
    for p in reversed(all_pngs):
        if p.startswith("/tmp/hermes_cache/daily") and not daily_img:
            daily_img = p
        elif p.startswith("/tmp/hermes_cache/mtd") and not mtd_img:
            mtd_img = p
        elif p.startswith("/tmp/hermes_cache/alltime") and not alltime_img:
            alltime_img = p
        if all([daily_img, mtd_img, alltime_img]):
            break

# Build sections list — only include images that exist
sections = []
if daily_img:
    sections.append((
        "## 🧠 Token Usage Weekly Report\n\n**Last 7 Days — Daily Trends**",
        daily_img,
    ))
if mtd_img:
    sections.append((
        "**Month-to-Date Totals**",
        mtd_img,
    ))
if alltime_img:
    sections.append((
        "**All Time by Model**",
        alltime_img,
    ))

if not sections:
    os._exit(0)

# Suppress all curl output — only stderr goes to terminal (not captured by scheduler)
for content, img_path in sections:
    subprocess.run([
        'curl', '-s', '-X', 'POST',
        f'https://discord.com/api/v10/channels/{CHANNEL}/messages',
        '-H', f'Authorization: Bot {TOKEN}',
        '-F', f'content={content}',
        '-F', f'file=@{img_path}',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)  # Rate-limit Discord API

# Cleanup — suppress all output
cleanup_paths = [p for p in [daily_img, mtd_img, alltime_img] if p]
cleanup_paths.append("/tmp/hermes_cache/token_report_data.json")
for p in cleanup_paths:
    try:
        os.remove(p)
    except OSError:
        pass

# Exit immediately — no further stdout/stderr
os._exit(0)