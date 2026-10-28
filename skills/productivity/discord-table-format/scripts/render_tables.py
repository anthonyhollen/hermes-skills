#!/usr/bin/env python3
"""
Consolidated Discord table renderer for the discord-table-format skill.

Performance design (replaces the old per-table, two-interpreter pipeline):
  1. Single interpreter   — tabulate + PIL both import in whichever Python runs
                            this (system python3 or the gateway venv). No venv
                            subprocess just to call tabulate.
  2. One process per report — render ALL sections in a single invocation, so
                            interpreter startup + PIL import + codec load are
                            paid once, not once per table.
  3. Direct retina render — draw straight at the 52pt target size instead of
                            rendering at 26pt on a 6000x2000 canvas and then
                            LANCZOS-upscaling 2x. Same output, less work/memory.
  4. Fast PNG save        — `quality` is a JPEG no-op for PNG; use
                            compress_level=1 (flat dark images compress trivially).
  5. Font singletons      — fonts loaded once at import, reused for every row.

Usage:
  # Batch (preferred): sections as JSON on stdin or a file arg.
  #   -> prints {"<title>": "<png path>", ...} as JSON on stdout
  python3 render_tables.py sections.json
  python3 render_tables.py < sections.json

  # Single table (ad-hoc):
  #   -> prints the png path on stdout
  python3 render_tables.py --one '<headers_json>' '<rows_json>' [out_prefix]

Section JSON (a list, or {"sections": [...]}):
[
  {"title": "Last 7 Days",
   "headers": ["Date", "Est. Cost"],
   "rows": [["2026-05-30", "$0.04"]],
   "bold_match": ["overdue", "failed", "critical"],   # optional, defaults below
   "prefix": "daily"}                                  # optional filename prefix
]

A section with no rows renders no image and maps its title to null.
Titles must be unique (the output is keyed by title). Bad input exits 1 with a
one-line "render_tables: error: ..." on stderr; a usage error exits 2.
"""
import sys, os, json, time, re, unicodedata
from collections.abc import Iterable, Mapping

# Pillow is imported from the system dist-packages in BOTH interpreters so the
# output is identical whichever Python runs this (the gateway venv has its own,
# newer Pillow, which renders different bytes). The path entry is removed again
# right after the imports: left at sys.path[0] it would shadow the venv's own
# requests/urllib3/certifi/... for anything an in-process caller imports later.
_DIST = "/usr/lib/python3/dist-packages"
sys.path.insert(0, _DIST)
try:
    import tabulate as _tabulate_mod
    from tabulate import tabulate
    from PIL import Image, ImageDraw, ImageFont
finally:
    sys.path.remove(_DIST)

FONT_PATH      = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_SIZE      = 52            # retina target (logical 26pt x2) — drawn directly
LINE_H         = 56            # (26pt + 2px line-spacing) x2
BORDER         = 8             # 4px x2
DISCORD_BG     = (30, 31, 34)  # #1e1f22 dark mode
DISCORD_TEXT   = (224, 224, 224)
CACHE_DIR      = "/tmp/hermes_cache"
DEFAULT_BOLD   = ("overdue", "failed", "critical")
# Per-character cell clean-up (str.translate table). tabulate counts every
# character as one column; the font draws these with zero width (variation
# selectors such as the U+FE0F in "⚠️", zero-width space/joiners, BOM, soft
# hyphen, bidi controls) or as a stray box (tab and other control characters),
# so leaving them in shifts every later column. "\x01" would even make tabulate
# treat the row as a separator line and drop it.
_CELL_CLEAN = {
    **dict.fromkeys([*range(0x00, 0x20), 0x7F]),                    # C0 controls, DEL
    **dict.fromkeys(map(ord, "\t\n\v\f\r\x85\u2028\u2029"), " "),     # line/tab breaks -> space
    **dict.fromkeys([*range(0xFE00, 0xFE10), 0x200B, 0x200C, 0x200D, 0x00AD, 0xFEFF,
                     0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2060, 0x2070)]),
}
# Status emoji the font lacks (they would draw as empty boxes) -> monochrome
# symbols it has, in both weights (user's choice, 2026-09-29). ✔ ✖ ☑ ⚠ ➡ ⬆ ⬇
# exist already once their U+FE0F is stripped; any other emoji stays a box.
_EMOJI_SUBST = {
    **dict.fromkeys(map(ord, "✅"), "✓"),
    **dict.fromkeys(map(ord, "❌❎"), "✗"),
    **dict.fromkeys(map(ord, "⭐🌟"), "★"),
    **dict.fromkeys(map(ord, "🔴🟠🟡🟢🔵🟣🟤⚫"), "●"),
    **dict.fromkeys(map(ord, "⚪"), "○"),
}
_CELL_CLEAN.update(_EMOJI_SUBST)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # colour/cursor escape sequences

# Module-level singletons: loaded once, reused for every section/row.
_FONT      = ImageFont.truetype(FONT_PATH, FONT_SIZE)
_FONT_BOLD = ImageFont.truetype(FONT_BOLD_PATH, FONT_SIZE)
_SCRATCH   = ImageDraw.Draw(Image.new("RGB", (4, 4)))
_seq       = 0  # disambiguates filenames generated within the same millisecond


def _esc(v):
    """Sanitize a cell value for image rendering.

    This is the IMAGE path: output is a PNG, not a markdown code block, so
    backticks/pipes need no escaping (escaping them only adds visual noise).
    We turn newlines/tabs into spaces, drop control, zero-width and ANSI
    characters, map common status emoji to symbols the font has (see
    _CELL_CLEAN / _EMOJI_SUBST), NFC-normalize (a combining accent is drawn
    in its letter's cell) and truncate over-long values. None (JSON null)
    renders as an empty cell. The text fallback path does its own escaping.
    """
    if v is None:
        return ""
    s = unicodedata.normalize("NFC", _ANSI.sub("", str(v)).translate(_CELL_CLEAN))
    return s if len(s) <= 200 else s[:199] + "…"


def _keywords(bold_match):
    """Normalize bold_match to a tuple of lowercase keywords (None -> defaults)."""
    if bold_match is None:
        bold_match = DEFAULT_BOLD
    elif isinstance(bold_match, str):
        bold_match = (bold_match,)
    elif not isinstance(bold_match, (list, tuple)):
        raise ValueError(f"bold_match must be a list of keywords, got {type(bold_match).__name__}")
    return tuple(k for k in (str(x).lower() for x in bold_match if x is not None) if k)


def _is_emph(line, keywords):
    """True if a content row should render bold. Border/separator lines never match."""
    if not line.startswith("│"):  # fancy_grid content rows start with │; borders with ╒ ╞ ├ ╘
        return False
    low = line.lower()
    return any(k in low for k in keywords)


def _table_lines(headers, rows, raw_rows=None):
    """The fancy_grid text lines for one table (cells already escaped; raw_rows = the
    caller's values, used to give float OBJECTS in numeric columns tabulate's short form)."""
    # Keep tabulate's alignment (numeric columns right/decimal-aligned) but not
    # its number reformatting, which rewrites the caller's strings ("0.50" ->
    # "0.5", "1.10" -> "1.1", "1e5" -> "100000"): classify each column the way
    # tabulate would, then render with number parsing off.
    column_type = getattr(_tabulate_mod, "_column_type", None)
    opts = {}
    if column_type is not None:
        cols = list(zip(*rows)) or [()] * len(headers)
        numeric = [column_type(c) in (int, float) for c in cols]
        if raw_rows is not None and len(raw_rows) == len(rows) and any(numeric):
            # tabulate printed float objects in a numeric column with format(v, "g"); keep that
            # (strings are never reformatted; a float in a text column stays str(v), as before).
            rows = [[format(raw, "g") if numeric[j] and isinstance(raw, float) else cell
                     for j, (cell, raw) in enumerate(zip(row, raw_row))]
                    for row, raw_row in zip(rows, raw_rows)]
        opts = {"disable_numparse": True,
                "colalign": ["decimal" if n else "left" for n in numeric]}
    # Every glyph this font draws is one cell wide (emoji/CJK fall back to a
    # one-cell box), so count width as characters even when wcwidth is
    # installed (the gateway venv has it; system python3 does not).
    wide = _tabulate_mod.WIDE_CHARS_MODE
    _tabulate_mod.WIDE_CHARS_MODE = False
    try:
        return tabulate(rows, headers=headers, tablefmt="fancy_grid", **opts).split("\n")
    finally:
        _tabulate_mod.WIDE_CHARS_MODE = wide


_pruned = False
_OWN_PNG = re.compile(r".+_\d{13}_\d+\.png$")  # <prefix>_<epoch ms>_<seq>.png — this renderer's names


def _prune_cache(max_age_s=24 * 3600):
    """Once per process, delete this renderer's PNGs older than a day.

    Callers delete an image after a confirmed send, but a failed send (or a
    MEDIA: reply, which the agent cannot confirm) leaves it behind, and nothing
    else sweeps /tmp/hermes_cache (the gateway only cleans ~/.hermes/cache).
    """
    global _pruned
    if _pruned:
        return
    _pruned = True
    cutoff = time.time() - max_age_s
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    for name in names:
        if not _OWN_PNG.match(name):
            continue
        p = os.path.join(CACHE_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass  # vanished or not ours to remove — best effort


def _safe_prefix(prefix):
    """Filename-safe prefix: no path separators or leading dots, at most 120 UTF-8 bytes."""
    p = re.sub(r"[^\w.-]+", "_", str(prefix)).lstrip(".")
    p = p.encode()[:120].decode(errors="ignore")  # filenames are limited to 255 BYTES
    return p or "table"


def _as_cells(x, what):
    """A header list / row as a list; ValueError for a str/bytes/mapping/set (split into
    characters or ints, drawn as KEYS, or in arbitrary order) or a non-iterable."""
    if isinstance(x, (str, bytes, bytearray, memoryview, Mapping, set, frozenset)) or not isinstance(x, Iterable):
        raise ValueError(f"render_table: {what} must be a list, got {type(x).__name__}")
    return list(x)


def _check_shape(headers, rows):
    """Raise ValueError unless headers and every row are lists of equal width.

    Refuse to silently misalign: tabulate pads/shifts a row whose width does
    not match the header count (and draws a dict row's KEYS, or splits a string
    row into characters), producing a wrong-but-plausible image with no
    warning. Fail loud so a caller's schema drift surfaces instead of shipping.
    """
    headers = _as_cells(headers, "headers")
    rows = [_as_cells(r, f"row {i}") for i, r in enumerate(rows)]
    n = len(headers)
    if n == 0:
        raise ValueError("render_table: a table needs at least one column")
    for i, r in enumerate(rows):
        if len(r) != n:
            raise ValueError(
                f"render_table: header/row width mismatch — headers has {n} columns "
                f"but row {i} has {len(r)}. Fix the caller's data; refusing to "
                f"render a misaligned table."
            )
    return headers, rows


def render_table(headers, rows, out_prefix="table", bold_match=DEFAULT_BOLD):
    """Render one fancy_grid table to a tight, retina-resolution PNG.

    Returns the output path, or None when there are no rows.
    """
    rows = list(rows or [])
    if not rows:
        return None
    headers, rows = _check_shape(headers, rows)
    _prune_cache()

    h = [_esc(x) for x in headers]
    d = [[_esc(c) for c in r] for r in rows]
    lines = _table_lines(h, d, rows)
    keywords = _keywords(bold_match)

    # Measure exact content width (per-line font matters: bold rows are wider).
    # lines[1] is the header row: emphasis is for data rows, never the header.
    max_w = 0
    fonts = []
    for i, ln in enumerate(lines):
        f = _FONT_BOLD if i != 1 and _is_emph(ln, keywords) else _FONT
        fonts.append(f)
        right = _SCRATCH.textbbox((0, 0), ln, font=f)[2]
        if right > max_w:
            max_w = right

    img = Image.new("RGB", (max_w + 2 * BORDER, len(lines) * LINE_H + 2 * BORDER), DISCORD_BG)
    draw = ImageDraw.Draw(img)
    y = BORDER
    for ln, f in zip(lines, fonts):
        draw.text((BORDER, y), ln, font=f, fill=DISCORD_TEXT)
        y += LINE_H

    global _seq
    os.makedirs(CACHE_DIR, exist_ok=True)
    prefix = _safe_prefix(out_prefix)
    while True:  # O_EXCL: never overwrite another process's same-millisecond image
        _seq += 1
        path = f"{CACHE_DIR}/{prefix}_{int(time.time() * 1000)}_{_seq}.png"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            continue
    try:
        with os.fdopen(fd, "wb") as fh:
            img.save(fh, format="PNG", compress_level=1)  # PNG ignores `quality`; level 1 = fast
    except BaseException:
        os.unlink(path)  # never leave a half-written image behind
        raise
    return path


def _slug(title, idx):
    s = "".join(c.lower() if c.isalnum() else "_" for c in title).strip("_")
    return s[:24] or f"sec{idx}"


def render_sections(sections):
    """Render a list of section dicts in this single process. Returns {title: path|None}."""
    # Validate every section before rendering any, so bad input leaves no orphan PNGs.
    if isinstance(sections, dict):
        sections = [sections]
    prepared = []
    for i, s in enumerate(sections):
        if not isinstance(s, dict):
            raise ValueError(f"render_sections: section {i} must be an object, got {type(s).__name__}")
        title = s.get("title")
        title = f"section{i}" if title is None else str(title)
        if any(title == p[0] for p in prepared):
            raise ValueError(f"render_sections: duplicate section title {title!r} — the output "
                             f"is keyed by title, so one image would be lost. Make titles unique.")
        rows = s.get("rows")
        rows = [] if rows is None else _as_cells(rows, f"section {title!r} rows")  # materialize once
        _keywords(s.get("bold_match"))  # type-check now, not mid-render
        headers = s.get("headers")
        if rows:  # an empty section needs no headers (it renders nothing)
            if headers is None:
                raise ValueError(f"render_sections: section {title!r} has no 'headers' list.")
            headers, rows = _check_shape(headers, rows)
        prepared.append((title, s, headers, rows))
    out = {}
    for i, (title, s, headers, rows) in enumerate(prepared):
        out[title] = render_table(
            headers,
            rows,
            out_prefix=s.get("prefix") or _slug(title, i),
            bold_match=s.get("bold_match"),
        )
    return out


def _usage(msg):
    print(f"render_tables: {msg}\nusage: render_tables.py [sections.json]   (or sections JSON on stdin)\n"
          f"       render_tables.py --one '<headers_json>' '<rows_json>' [out_prefix]", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    argv = sys.argv[1:]
    try:
        if argv and argv[0] == "--one":
            if len(argv) < 3:
                _usage("--one needs headers and rows")
            headers = json.loads(argv[1])
            rows = json.loads(argv[2])
            prefix = argv[3] if len(argv) > 3 else "custom"
            print(render_table(headers, rows, out_prefix=prefix) or "")
        elif argv and argv[0] in ("-h", "--help"):
            print(__doc__)
        else:
            if argv and argv[0] != "-":
                try:
                    with open(argv[0]) as f:
                        payload = json.load(f)
                except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                    _usage(f"no such file: {argv[0]}")
            else:
                payload = json.load(sys.stdin)
            sections = payload["sections"] if isinstance(payload, dict) and "sections" in payload else payload
            if not isinstance(sections, (list, dict)):
                raise ValueError("expected a JSON list of sections (or {\"sections\": [...]})")
            print(json.dumps(render_sections(sections)))
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as e:
        print(f"render_tables: error: {e}", file=sys.stderr)
        sys.exit(1)
