> **HISTORICAL — May 9–10, 2026. Superseded; do not follow its instructions.** (Correction note added 2026-09-29, 2.0.0.)
> The design it records was replaced by the shared `scripts/render_tables.py` (June 2026) and `scripts/send_tables.py`
> (2.0.0). Stale here: "tabulate only in the venv / always subprocess to the venv for tabulate" (both interpreters import
> tabulate directly now), the font sizes/padding (now 52 px, `LINE_H` 56, border 8, drawn directly — no 6000×2000 canvas or
> LANCZOS upscale), `table_*.png` / `delete_table_image()` / `prefer_image` (don't exist), and the `send_message`-based
> multi-section fix (use `send_tables.py`). "execute_code can't import PIL" was only true without the dist-packages path.
> Current behaviour: SKILL.md, `references/implementation.md`, `references/delivery.md`, `CHANGELOG.md`.

# Discord Table Format — Design Decisions Log

## Session: Building the discord-table-format skill (May 9, 2026)

---

## 1. Requirements Gathering

User's requirements:
- Use Tabulate library
- Properly formatted ASCII table in Discord code block format (triple backticks)
- Applied EVERY time a table is generated — no exceptions, survives new sessions
- fancy_grid vs grid: user needed to see samples before deciding
- Prolog as code block language identifier
- Auto-detect numeric columns for alignment
- Skill default for format options
- Auto-load on table tasks, 100% consistency
- Empty data → "No data"
- Wide tables → warn
- Special characters (|, `) → escape

---

## 2. Style Decision: fancy_grid vs grid

Sample data (8 columns, 8 rows) rendered in both styles:

**fancy_grid** uses box-drawing characters (╒ ╕ ╞ ╡ ╘). **grid** uses plain ASCII (+-|). Both render correctly in Discord code blocks. User confirmed fancy_grid preferred for its cleaner look.

---

## 3. Mobile/Desktop Rendering Issues

Testing revealed:
- Desktop code block visible area: ~93 chars (fancy_grid separator line = 102 chars → overflow)
- Mobile Discord visible: ~39 chars (4 of 8 columns visible for 8-col table)
- The bottom border row (`╘════════════╧═════════...`) wraps to next line on desktop, breaking the table visually

**Solution**: Render as PNG image using Pillow, dark Discord-themed background.

---

## 4. Image Generation — Technical Issues Encountered

### Issue: PIL not in venv
- `~/.hermes/hermes-agent/venv/bin/python3` has tabulate but NOT PIL
- System Python has PIL at `/usr/lib/python3/dist-packages/PIL/`
- **Workaround**: subprocess tabulate generation to venv Python, do image rendering via system Python with `sys.path.insert(0, '/usr/lib/python3/dist-packages')`

### Issue: execute_code sandbox can't import PIL
- `from PIL import Image` fails in execute_code sandbox even with sys.path workaround
- **Workaround**: Must use terminal() for image generation code, not execute_code

### Issue: Default font (bitmap) doesn't support Unicode
- `ImageFont.load_default()` → getmask() fails on box-drawing Unicode chars with `UnicodeEncodeError`
- **Workaround**: Use TrueType font `DejaVuSansMono.ttf` at `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`

### Issue: char height measured from 'M' causes clipping
- `'M' bbox_h = 9px` at 12pt, but `'╞' bbox_h = 15px` at 12pt (box chars ~40% taller)
- **Fix**: Use `font.getbbox('╞')` for height, `font.getlength('M')` for width

### Issue: Low-res output at 12pt
- 12pt font → image 852×175px (blurry on retina)
- **Fix**: 20pt font → image 1457×380px (3× more pixels, crisp)

---

## 5. Image Cleanup

Generated images stored in `/tmp/hermes_cache/table_*.png` (~27KB each). `delete_table_image(path)` called after confirmed delivery — best-effort, won't fail if file already gone.

---

## 6. Token Cost Analysis

| Method | Tokens per appearance |
|---|---|
| Text fancy_grid (20-row table) | ~1,500–2,500 |
| Image MEDIA: path | ~20 |

Image is better on display quality AND token cost. Text fallback only for copy-paste scenarios.

---

## 7. Default Mode Change

Changed from text-first to **image-first** (prefer_image=True default). Text retained as `prefer_image=False` opt-in.

---

## 8. Self-Consistency Failures

**Failure 1 & 2**: Agent typed markdown tables manually instead of using the skill pipeline twice during development.

**Root cause**: Activation gap — skill was loaded but not actively invoked when composing reply text.

**Why the rule survives**: The skill file is the enforcement mechanism. In any new session, loading the skill correctly enforces all tables go through the pipeline regardless of context.

---

## 9. Final Configuration (May 9)

| Parameter | Value |
|---|---|
| FONT_SIZE | 20pt |
| LINE_SPACING | 8px |
| PADDING | 30px |
| DISCORD_BG | #1e1f22 |
| DISCORD_TEXT | #e0e0e0 |
| tablefmt | fancy_grid |

---

## 10. Technical Environment Reference

```
Pillow:      /usr/lib/python3/dist-packages/PIL/ (system, NOT venv)
Tabulate:    ~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/
Font:        /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf
Temp dir:    /tmp/hermes_cache/ (created on demand)
Venv python: /home/amh/.hermes/hermes-agent/venv/bin/python3
```

Always use subprocess to venv Python for tabulate, system Python + sys.path.insert for PIL.

---

## 11. May 10 Iteration: Tight Crop via Bounding Box

**Problem:** Test images had too much empty space at right and bottom edges. Font=18pt, padding=30px, line_spacing=10px produced loose, visually wasteful images.

**User signals (first-class skill triggers):**
- "image width should just be slightly wider than table width. Same goes for height."
- "none - all of them looks bad" — rejected multiple renders at fs=18, 26, 30
- Reference image `img_c1e7326094e6.webp` (1059×284px) identified as target quality

**Root cause:** `len(line) * char_w` is inaccurate for fancy_grid — box-drawing chars have different widths than 'M'. The calculation systematically overestimates rendered width.

**Fix (bounding box crop):**
1. Render on oversized canvas (6000×2000)
2. Track `max_x` from `d.textbbox()` while drawing each line
3. `crop((BORDER, BORDER, max_x + BORDER, y + BORDER))` — exact to content
4. `resize((w*2, h*2), Image.LANCZOS)` — retina upscale after crop

**Final params:** FONT_SIZE=26, LINE_SPACING=2, BORDER=4, SCALE=2.

**Results:**
- 8-row daily table → 2944×1072px
- 5-row top sessions → 2860×1080px
- Reference image 1059×284px (3.73:1 ratio) — target achieved

**Font size evolution:** 12pt (blurry) → 18pt (still too loose) → 20pt (May 9 final) → 26pt (May 10 final)

---

## 12. May 10: Multi-Section Batching Pitfall

**Problem:** When sending a multi-section report (e.g. "Last 7 Days" + "Top Sessions" + "MTD by Source"), all section headers were batched in one message, then all images in the next. Discord delivered them out of order.

**Signal:** "These 3 section headers below got sent out all at once, then all 3 table images sent out one by one. Each section header and table image should always be sent together."

**Fix:** Pitfall added to SKILL.md + `token-usage` cron job prompt updated.

**Pattern (correct):** For each section: header → image → delete → next. Never batch multiple headers or multiple images in one send_message call.

---

## 13. May 10: Final Image Params Confirmed

| Parameter | Value |
|---|---|
| FONT_SIZE | 26pt base |
| LINE_SPACING | 2px base (4px at 2x) |
| BORDER | 4px base (8px at 2x) |
| SCALE | 2x LANCZOS upscale after crop |
| Canvas | 6000×2000 |
| Crop | `img.crop((BORDER, BORDER, max_x + BORDER, y + BORDER))` then resize 2x |

User confirmed "image looks perfect."