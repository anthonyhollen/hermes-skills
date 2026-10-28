"""Bulk re-import of statement PDFs (maintenance only).

    python3 scripts/import_all.py <pdf1> [pdf2 ...]

Each PDF is processed in its own subprocess (process_pdf auto-detects the card
and period from the PDF body). A summary at the end lists any failures so a
single bad PDF in the middle of the batch isn't easy to miss. Exit 1 on any
failure. (The Discord upload cache keeps PDFs only ~7 days, so pass the paths
of copies you kept — the old hardcoded Jan–May list no longer exists.)
"""
import subprocess
import sys
import time

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from scripts.parse_ocbc import init_db, get_db

PER_PDF_TIMEOUT = 180  # seconds — a wedged parse must not hang the whole batch


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    init_db().close()
    skill_root = str(__file__).rsplit("/", 2)[0]
    failures = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        try:
            result = subprocess.run(
                [sys.executable, "-c",
                 ("import sys; sys.path.insert(0, '.');"
                  "from scripts.parse_ocbc import process_pdf;"
                  f"r = process_pdf({path!r});"
                  "print(f'OK: {r[\"card_suffix\"]} {r[\"year_month\"]} "
                  "{len(r[\"transactions\"])} txns')")],
                cwd=skill_root, capture_output=True, text=True, timeout=PER_PDF_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"  ✗ {name}: timed out after {PER_PDF_TIMEOUT}s")
            failures.append((name, -1, f"timed out after {PER_PDF_TIMEOUT}s"))
            continue
        ok = result.returncode == 0 and result.stdout.startswith("OK:")
        print(f"  {'✓' if ok else '✗'} {name}: {result.stdout.strip()} {result.stderr.strip()[-300:]}")
        if not ok:
            failures.append((name, result.returncode,
                             result.stderr.strip()[-300:] or result.stdout.strip()))
        time.sleep(0.5)

    print("\n=== MERCHANT SUMMARY ===")
    conn = get_db()
    for row in conn.execute(
            "SELECT card_suffix, merchant_normalized, txn_count, total_spent, approved "
            "FROM merchants ORDER BY card_suffix, txn_count DESC"):
        flag = "✓" if row[4] == 1 else "✗" if row[4] == -1 else "?"
        print(f"  [{row[0]}] {row[1]} | {row[2]} txns | ${row[3]:.2f} | {flag}")
    conn.close()
    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for name, rc, msg in failures:
            print(f"  ✗ {name} (rc={rc}): {msg}")
        return 1
    print("=== ALL IMPORTS OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
