# Renderer internals & caller contract (`scripts/render_tables.py`)

Relocated from SKILL.md in 2.0.0 (2026-09-29) and corrected against the code and live Pi probes. Read before
editing the renderer or wiring it into another skill.

## Defaults
| Setting | Value |
|---|---|
| Table style | tabulate `fancy_grid` (╒ ╤ ╕ ╞ ╪ ╡ ├ ┼ ┤ ╘ ╧ ╛, content rows start with `│`) |
| Font | DejaVu Sans Mono 52 px (regular + bold, `/usr/share/fonts/truetype/dejavu/`), drawn directly at retina size |
| Line height / border | `LINE_H = 56`, `BORDER = 8` (fixed — the canvas is sized from `textbbox()` of each line) |
| Colours | background `#1e1f22` (Discord dark), text `#e0e0e0` |
| PNG save | `compress_level=1` (PNG ignores `quality`) |
| Output | `/tmp/hermes_cache/<prefix>_<epoch-ms>_<seq>.png`, created with `O_EXCL` (never overwrites another process's file) |
| Empty section | no rows → no image, title maps to `null` |

## Caller contract (every consumer depends on these — keep them)
- **CLI batch**: `render_tables.py [sections.json | - | /dev/stdin]` or sections JSON on stdin (a list, a single section
  object, or `{"sections": [...]}`) → stdout is exactly one JSON object `{title: path|null}`, input order. Titles must be
  unique; an empty section may omit `headers`.
- **CLI single**: `--one '<headers_json>' '<rows_json>' [prefix]` → stdout is the path, or an empty line for no rows.
- **Exit codes**: 0 ok; 1 bad input (one line `render_tables: error: …` on **stderr** — nothing on stdout);
  2 usage (missing `--one` args, no such file). `-h/--help` prints the docstring.
- **Python**: `render_table(headers, rows, out_prefix="table", bold_match=DEFAULT_BOLD)` → path or `None`
  (no rows); raises `ValueError` on a header/row width mismatch, a str/bytes/mapping (or non-iterable) where a header
  list or row belongs, or a set (arbitrary order) / bytearray / memoryview (other iterables — tuples, `sqlite3.Row`,
  generators, `dict.keys()` / `.items()` views — are accepted), zero columns, a bad
  `bold_match` type, duplicate titles or a non-empty section without `headers` — **before** any PNG is written. `render_sections(sections)` → `{title: path|None}`.
  `bold_match=()` means no bold; `None` means the defaults. Unknown section keys (e.g. task-tracker's `ids`) are ignored.
- **Diagnostics never go to stdout** — task-tracker / warframe / sg-commute `json.loads` all of stdout;
  backup_cleaner reads the last stdout line and checks the PNG magic bytes.

| Consumer | Call style | Interpreter |
|---|---|---|
| token-usage `token_report_no_agent.py` (cron, 2 identical copies) | in-process `render_table(..., bold_match=())` | gateway venv |
| token-usage `references/gen_token_report_images.py` | in-process | agent `python3` |
| skill-usage-report `skill_usage_report.py` | lazy in-process import (inserts dist-packages itself) | agent / venv |
| backup-cleaner `backup_cleaner.py` (2 copies) | subprocess `--one`, last stdout line + PNG magic | `/usr/bin/python3` |
| task-tracker `task-tracker.py` | subprocess batch on stdin (extra `ids` key) | `sys.executable` |
| warframe-scraper `warframe_email_report.py` (cron) | subprocess batch on stdin | venv |
| sg-commute `nearest_carpark.py` | subprocess batch on stdin; globs `carpark_*.png` | bare `python3` |
| model-eval-suite `render_discord.py` / `send_report.py` | subprocess batch on stdin, last line JSON | `sys.executable` |
| `send_tables.py` (this skill) | in-process `render_sections` | agent `python3` |
(traffic-camera's `traffic_camera_report.py` carries its own inline renderer copy — not a consumer; it drifts.)

## Two interpreters, one output
The agent's terminal `python3` resolves to the **gateway venv** (it's first on the gateway's PATH); crons and some
scripts use `/usr/bin/python3`. They differ: system python finds tabulate 0.10.0 in `~/.local` (NOT dist-packages —
`python3 -s` can't import it) and has no `wcwidth`; the venv has its own tabulate 0.10.0 **with** wcwidth 0.6.0 and
its own Pillow 12.3.0 (which renders different bytes). To get identical images from both:
- The module inserts `/usr/lib/python3/dist-packages` at `sys.path[0]` only while importing tabulate + PIL, then
  **removes it** — so both interpreters use the system Pillow 9.4.0, and an in-process caller's later imports keep
  the venv's own requests/urllib3/certifi (before 2.0.0 the entry stayed and silently downgraded them to
  requests 2.28 / urllib3 1.26 / a 2022 CA bundle).
- Widths are counted in characters: `tabulate.WIDE_CHARS_MODE` is forced off around the call (and restored), because
  every glyph this font draws — including the fallback box for emoji/CJK — is one cell wide. With wcwidth on, the
  venv counted emoji/CJK as 2 cells and misaligned those rows.
Verified 2026-09-29 on the deployed file: 46/46 test + real sections pixel-identical between the two interpreters (was 45/46).

## Cell clean-up (`_esc`)
Cells are `str()`-ed (`None` → `""`; a float object uses `format(v, "g")`, the short form tabulate used to show),
ANSI escape sequences removed, `_CELL_CLEAN` applied, then NFC-normalized (a decomposed accent draws in its letter's
cell; stripping first lets e.g. `e`+ZWSP+U+0301 compose). `_CELL_CLEAN`: C0 controls/DEL dropped (`\x01` would make tabulate treat the row as
a separator line and drop it), `\t \n \v \f \r` NEL LS PS → space, zero-width characters dropped (variation
selectors U+FE00–FE0F such as the one in "⚠️", ZWSP/ZWNJ/ZWJ, soft hyphen, BOM, bidi marks/embeddings/isolates) —
tabulate counts each as a column but the font draws them with zero width, which shifted every later column. Then
the status-emoji map `_EMOJI_SUBST` (user's choice, 2026-09-29): ✅→✓, ❌❎→✗, ⭐🌟→★, 🔴🟠🟡🟢🔵🟣🟤⚫→●, ⚪→○ (all
present in both DejaVu weights; ✔ ✖ ☑ ⚠ ➡ ⬆ ⬇ exist already once the U+FE0F is stripped). Other emoji, CJK, Hebrew
and Thai draw as a box. Finally cells over 200 characters are truncated to 199 + "…".
The image path needs no backtick/pipe escaping (it's a PNG, not a code block).

## Numbers
tabulate's number parsing rewrote strings ("0.50"→"0.5", "1.00"→"1", "1.10"→"1.1", "1e5"→"100000", thousands
separators dropped, 6-significant-digit floats). The renderer classifies each column with tabulate's own
`_column_type` and renders with `disable_numparse=True` + `colalign` = `decimal` for numeric columns, `left`
otherwise — same alignment as before, strings verbatim. Output for int/str columns is byte-identical to tabulate's
default (checked on 20k random tables and all real inputs). If a tabulate upgrade drops `_column_type`, the renderer
falls back to tabulate's default behaviour.

## Bold rows
`bold_match` keywords are lower-cased and matched as substrings against the whole line of a **data row** (a line
starting with `│`, excluding `lines[1]`, the header). `None` → defaults `("overdue", "failed", "critical")`; a bare
string → one keyword; empty keywords are dropped. Substring matching means "not overdue yet" also goes bold — pass a
narrower keyword list if that matters.

## Housekeeping
Once per process the renderer deletes its own PNGs (`<prefix>_<13-digit ms>_<n>.png`) older than 24 h in
`/tmp/hermes_cache` — failed sends and `MEDIA:` replies leave them behind, and the gateway's 24 h cache sweep only
covers `~/.hermes/cache/*`. Each file is checked separately (one vanishing mid-sweep doesn't stop it); lock/JSON files
and other names are never touched. `prefix` is sanitized to `[\w.-]`, no leading dots, ≤ 120 UTF-8 bytes (filenames are limited to 255 bytes). A failed save
removes its half-written file.

## Performance (measured on the Pi, 2026-09)
Batch of 3 small tables ≈ 200 ms (system) / 215 ms (venv), of which ~110–125 ms is interpreter + PIL/tabulate
import (tabulate's own import pulls `importlib.metadata`, ~30 ms). A 40-row table: ~370 ms, save-dominated. 150 rows
× 110 chars → 3742×16984 px, 4.6 MB, 2.8 s, ~280 MB RSS. Nothing worth optimizing: callers already batch one process
per report; `compress_level` 6/9 shrink a big PNG ~17–20 % for 1.6×/5.7× the save time.
The consolidation that set this design (June 2026): one interpreter (no venv subprocess for tabulate), one process
per report (a 5-section report went from 5 cold starts to 1), direct 52 px drawing instead of 26 pt on a 6000×2000
canvas + LANCZOS 2× upscale (same size, less memory), `compress_level=1`, font singletons.

## Known limits
- Right-to-left text: raqm reverses a whole line whose first strong character is RTL, and Arabic ligatures draw
  narrower than their character count — columns misalign. No real input has RTL text.
- Combining marks with no precomposed NFC form draw with zero width → that row is one cell short.
- No size cap: a 200-char cell is ~6.6k px wide; 8 such columns ~50k px. Split big tables (SKILL.md input rules).

## Why images at all (history)
Raw `fancy_grid` in a Discord code block broke in practice: 8-column tables are ~102 chars wide and Discord desktop
clips code blocks at ~93 (border rows wrap), mobile shows ~39 chars (4 of 8 columns), a 20-row text table costs
~1.5–2.5K tokens every time it's in context (an image path ~20), measuring line height from 'M' under-measured the
taller box-drawing glyphs and clipped the bottom row, and loose padding wasted space. Markdown pipe tables aren't
rendered by Discord at all. The image path fixes all of these; text stays only for explicit copy-paste requests.
