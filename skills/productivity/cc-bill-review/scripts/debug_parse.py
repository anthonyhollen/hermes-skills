"""Dump the first lines of a statement PDF's text (to see the layout the parser gets).

    python3 scripts/debug_parse.py <pdf> [n_lines]
"""
import sys

import pymupdf

if len(sys.argv) < 2:
    sys.exit(__doc__)
doc = pymupdf.open(sys.argv[1])
text = "\n".join(page.get_text() for page in doc)
doc.close()
for i, line in enumerate(text.split("\n")[: int(sys.argv[2]) if len(sys.argv) > 2 else 60]):
    print(f"{i:3}: {line!r}")
