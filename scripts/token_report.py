#!/usr/bin/env python3
"""Token Usage Weekly Report — programmatic, no LLM."""
import sqlite3, os, time
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from tabulate import tabulate as tp

DISCORD_BG = (30, 31, 34)
DISCORD_TEXT = (224, 224, 224)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = 26
LINE_SPACING = 2
BORDER = 4
SCALE = 2

CACHE_DIR = "/tmp/hermes_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def generate_table_image(headers, data):
    esc = lambda s: str(s).replace("`", "\\")
    headers_esc = [esc(h) for h in headers]
    data_esc = [[esc(c) for c in row] for row in data]
    table_text = tp(data_esc, headers=headers_esc, tablefmt="fancy_grid")
    lines = table_text.split("\n")
    out_path = f"{CACHE_DIR}/table_{int(time.time()*1000)}.png"
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    canvas_w, canvas_h = 6000, 2000
    img = Image.new('RGB', (canvas_w, canvas_h), color=DISCORD_BG)
    d = ImageDraw.Draw(img)
    y = BORDER
    max_x = 0
    for line in lines:
        d.text((BORDER, y), line, font=font, fill=DISCORD_TEXT)
        bbox = d.textbbox((0, 0), line, font=font)
        if bbox[2] > max_x: max_x = bbox[2]
        y += FONT_SIZE + LINE_SPACING
    img_cropped = img.crop((BORDER, BORDER, max_x + BORDER, y + BORDER))
    scaled = img_cropped.resize(
        (img_cropped.width * SCALE, img_cropped.height * SCALE), Image.LANCZOS
    )
    scaled.save(out_path, quality=95)
    return out_path

def fmt(v, suffix=''):
    if v is None: return '0' + suffix
    if suffix == 'M': return f"{v/1e6:.1f}M"
    if suffix == 'K': return f"{v/1e3:.1f}K"
    return str(v)

# ── Data collection ──────────────────────────────────────────────────────────
db = '/home/amh/.hermes/state.db'
now = datetime.now()
week_ago_ts = now.timestamp() - 7 * 86400

conn = sqlite3.connect(db)
c = conn.cursor()
c.execute("""
    SELECT started_at, SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens),
           COUNT(*), SUM(estimated_cost_usd)
    FROM sessions WHERE started_at >= ?
    GROUP BY strftime('%%Y-%%m-%%d', started_at, 'unixepoch') ORDER BY started_at
""", (week_ago_ts,))
days = c.fetchall()

c.execute("""
    SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
    FROM sessions WHERE started_at >= unixepoch('now', 'start of month')
""")
mtd = c.fetchone()

c.execute("""
    SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),
           SUM(cache_read_tokens), SUM(cache_write_tokens), SUM(estimated_cost_usd)
    FROM sessions GROUP BY model ORDER BY SUM(input_tokens) DESC
""")
alltime = c.fetchall()
conn.close()

rows_daily = []
for r in days:
    rows_daily.append([
        datetime.fromtimestamp(r[0]).strftime('%Y-%m-%d'),
        fmt(r[1],'M'), fmt(r[2],'M'), fmt(r[3],'M'), fmt(r[4],'M'),
        r[5], f"${r[6]:.4f}"
    ])

rows_mtd = [['TOTAL', mtd[0], fmt(mtd[1],'M'), fmt(mtd[2],'M'),
             fmt(mtd[3],'M'), fmt(mtd[4],'M'), f"${mtd[5]:.4f}"]]

rows_alltime = []
for r in alltime:
    cost = r[6] if r[6] is not None else 0
    rows_alltime.append([r[0] or 'unknown', r[1],
        fmt(r[2],'M'), fmt(r[3],'M'), fmt(r[4],'M'), fmt(r[5],'M'), f"${cost:.4f}"])

# ── Generate images ───────────────────────────────────────────────────────────
p_daily   = generate_table_image(['Date','Input','Output','Cache Read','Cache Write','Sessions','Est. Cost'], rows_daily)
p_mtd     = generate_table_image(['Sessions','Count','Input','Output','Cache Read','Cache Write','Est. Cost'], rows_mtd)
p_alltime = generate_table_image(['Model','Sessions','Input','Output','Cache Read','Cache Write','Total Cost'], rows_alltime)

# ── Output for scheduler: extract_media() reads MEDIA: tags from stdout ─────────
print(f"""## 🧠 Token Usage Weekly Report — May 2026

**Last 7 Days — Daily Trends**
MEDIA:{p_daily}

**Month-to-Date Totals — May 2026**
MEDIA:{p_mtd}

**All Time by Model**
MEDIA:{p_alltime}""")