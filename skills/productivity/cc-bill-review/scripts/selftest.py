#!/usr/bin/env python3
"""End-to-end self-test for cc-bill-review.

Run as `python3 scripts/selftest.py` from the skill directory, or with a real
PDF path: `python3 scripts/selftest.py /path/to/statement.pdf` to also exercise
the live parser end-to-end without writing to the DB.

Exit code 0 on success, 1 on any failure. Designed so the Discord bot (or any
CI) can run this as a sanity gate before relying on imports.
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import parse_ocbc as core
from scripts.parse_ocbc import (
    COUNTRY_CODES,
    ParseError,
    canonicalize_merchant,
    detect_card_and_period,
    get_config,
    init_db,
    next_reminders_for,
    normalize_merchant,
    parse_amount,
    parse_date,
    read_pdf_text,
    validate_parsed_data,
    verify_cron_health,
)


_PASS = "✓"
_FAIL = "✗"


class SelfTest:
    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.passed = 0

    # ── reporting ────────────────────────────────────────────────────────
    def _ok(self, msg: str) -> None:
        self.passed += 1
        if self.verbose:
            print(f"  {_PASS} {msg}")

    def _fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  {_FAIL} {msg}")

    def _warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  ⚠ {msg}")

    def assert_eq(self, label: str, got, expected) -> None:
        if got == expected:
            self._ok(f"{label}: {got!r}")
        else:
            self._fail(f"{label}: expected {expected!r}, got {got!r}")

    def assert_raises(self, label: str, exc_type: type, fn, *args, **kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
        except exc_type as e:
            self._ok(f"{label}: raised {exc_type.__name__} ({str(e)[:50]})")
            return
        except Exception as e:
            self._fail(f"{label}: expected {exc_type.__name__}, got {type(e).__name__}: {e}")
            return
        self._fail(f"{label}: expected {exc_type.__name__}, returned {result!r}")

    # ── tests ────────────────────────────────────────────────────────────
    def test_parse_amount(self) -> None:
        print("--- parse_amount ---")
        cases = [
            ("123.45", 123.45),
            ("1,234.56", 1234.56),
            ("(123.45)", -123.45),
            ("(123.45", -123.45),
            ("S$45.00", 45.00),  # falls into digit-extraction fallback
            ("", 0.0),
        ]
        for inp, expected in cases:
            self.assert_eq(f"parse_amount({inp!r})", parse_amount(inp), expected)

    def test_parse_date(self) -> None:
        print("--- parse_date ---")
        self.assert_eq("DD/MM same month", parse_date("15/05", "2026-05"), "2026-05-15")
        self.assert_eq("DD/MM previous-year wrap",
                       parse_date("28/12", "2026-01"), "2025-12-28")
        self.assert_eq("DD/MM/YY explicit year",
                       parse_date("28/12/25", "2026-01"), "2025-12-28")
        self.assert_raises("empty input raises", ParseError, parse_date, "", "2026-05")
        self.assert_raises("malformed raises", ParseError, parse_date, "not-a-date", "2026-05")

    def test_normalize_and_canonicalize(self) -> None:
        print("--- normalize_merchant / canonicalize ---")
        self.assert_eq(
            "P-code prefix stripped",
            normalize_merchant("-2030 APPLE.COM/SG SINGAPORE SG"),
            "APPLE.COM/SG SINGAPORE",
        )
        self.assert_eq(
            "Spotify P-code collapse",
            normalize_merchant("SPOTIFY P3DE59542B STOCKHOLM SE"),
            "SPOTIFY STOCKHOLM",
        )
        self.assert_eq(
            "Cross-card canonical: TPER",
            canonicalize_merchant("5999", "TPER SPA FOREIGN CURRENCY EUR 2.30"),
            "TPER SPA",
        )
        self.assert_eq(
            "Card-specific canonical (5999)",
            canonicalize_merchant("5999", "THE WOK PEOPLE SINGAPORE"),
            "THE WOK PEOPLE",
        )

    def test_esc_md(self) -> None:
        print("--- esc_md (Discord markdown escaping) ---")
        from scripts import reviewlib
        self.assert_eq(
            "OCBC ** masking escaped",
            reviewlib.esc_md("SMP**JUNG SUNG HAIR"),
            "SMP\\*\\*JUNG SUNG HAIR",
        )
        self.assert_eq(
            "single * (GRAB*) escaped",
            reviewlib.esc_md("GRAB* A-8RWXUPAGWVI SINGAPORE"),
            "GRAB\\* A-8RWXUPAGWVI SINGAPORE",
        )
        self.assert_eq(
            "plain names untouched",
            reviewlib.esc_md("FAIRPRICE FINEST"),
            "FAIRPRICE FINEST",
        )
        self.assert_eq(
            "backslash itself escaped first",
            reviewlib.esc_md("A\\B"),
            "A\\\\B",
        )

    def test_detect_card_and_period(self) -> None:
        print("--- detect_card_and_period ---")
        samples = [
            (
                "OCBC STATEMENT\nCard No.: XXXX-XXXX-XXXX-5999\nStatement Date 01-05-2026\nTRANSACTION DATE",
                ("5999", "2026-05"),
            ),
            (
                "OCBC STATEMENT\nCARD NUMBER ENDING 7008\nStatement Date 15-04-2026\nTRANSACTION DATE",
                ("7008", "2026-04"),
            ),
            ("not an OCBC statement", (None, None)),
        ]
        for text, expected in samples:
            self.assert_eq(f"detect({text[:40]!r}...)", detect_card_and_period(text), expected)

    def test_country_codes(self) -> None:
        print("--- COUNTRY_CODES ---")
        for code in ("SG", "US", "GB", "JP", "IT", "AU"):
            if code in COUNTRY_CODES:
                self._ok(f"{code} present")
            else:
                self._fail(f"{code} missing from COUNTRY_CODES")
        if "XX" in COUNTRY_CODES:
            self._fail("XX should not be in COUNTRY_CODES")
        else:
            self._ok("XX correctly absent")

    def test_pdf_sniff(self) -> None:
        print("--- read_pdf_text sanity sniffing ---")
        self.assert_raises("missing file", ParseError, read_pdf_text, "/no/such/file.pdf")
        self.assert_raises("non-PDF file", ParseError, read_pdf_text, "/etc/hostname")

    def test_validate_parsed_data(self) -> None:
        print("--- validate_parsed_data ---")
        today = date.today()
        yesterday = (today - timedelta(days=1)).strftime("%d-%m-%Y")
        in_3w = (today + timedelta(days=21)).strftime("%d-%m-%Y")
        good = {
            "statement_date": yesterday,
            "due_date": in_3w,
            "total_due": 500.0,
            "transactions": [
                {"txn_date": (today - timedelta(days=5)).strftime("%Y-%m-%d"),
                 "merchant_raw": "FOO", "merchant_normalized": "FOO",
                 "amount": 100.0, "country_code": "SG"},
            ],
        }
        warns = validate_parsed_data(good)
        self.assert_eq("healthy data → 0 warnings", warns, [])

        bad_country = {**good, "transactions": [
            {**good["transactions"][0], "country_code": "ZZ"},
        ]}
        warns = validate_parsed_data(bad_country)
        if any("ZZ" in w for w in warns):
            self._ok("unknown country flagged")
        else:
            self._fail(f"unknown country not flagged: {warns}")

        # Zero txns + nonzero total → warning
        empty = {**good, "transactions": []}
        warns = validate_parsed_data(empty)
        if any("0 transactions" in w for w in warns):
            self._ok("empty txns warning emitted")
        else:
            self._fail(f"empty txns not warned: {warns}")

        # Hard failures
        self.assert_raises("negative total raises", ParseError,
                           validate_parsed_data, {**good, "total_due": -1.0})
        self.assert_raises("due before stmt raises", ParseError,
                           validate_parsed_data,
                           {**good, "due_date": (today - timedelta(days=10)).strftime("%d-%m-%Y")})

    def test_cron_health(self) -> None:
        print("--- verify_cron_health ---")
        h = verify_cron_health()
        keys = {"found", "job_id", "state", "schedule", "next_run", "last_run",
                "expected_paused", "healthy", "notes"}
        missing = keys - set(h.keys())
        if missing:
            self._fail(f"missing keys: {missing}")
        else:
            self._ok(f"all keys present (state={h['state']}, healthy={h['healthy']})")
        if not h["found"]:
            self._warn("cron job not found in ~/.hermes/cron/jobs.json — reminders cannot fire")
        elif not h["healthy"]:
            self._warn(f"cron is unhealthy: {h['notes']}")

    def test_next_reminders(self) -> None:
        print("--- next_reminders_for ---")
        events = next_reminders_for()
        self._ok(f"returned {len(events)} upcoming events")
        today = date.today()
        for ev in events:
            if ev["fire_date"] < today:
                self._fail(f"past event leaked through: {ev}")

    def test_live_data_invariants(self) -> None:
        """Read-only checks on the REAL DB: re-normalizing every stored raw name
        reproduces the stored identity (normalizer drift = 0), every transaction
        has a merchants row, and no statement total is glued onto a name."""
        print("--- live-data invariants (read-only) ---")
        conn = core.get_db()
        try:
            rows = conn.execute("SELECT card_suffix, merchant_raw, merchant_normalized "
                                "FROM transactions").fetchall()
            drift = [r[1] for r in rows if normalize_merchant(r[1], r[0]) != r[2]]
            self.assert_eq(f"normalizer drift over {len(rows)} stored rows", len(drift), 0)
            orphans = conn.execute(
                "SELECT COUNT(*) FROM transactions t LEFT JOIN merchants m ON "
                "t.card_suffix=m.card_suffix AND t.merchant_normalized=m.merchant_normalized "
                "WHERE m.merchant_normalized IS NULL").fetchone()[0]
            self.assert_eq("transactions without a merchants row", orphans, 0)
            glued = sum(conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE card_suffix=? AND year_month=? "
                "AND merchant_raw LIKE ?", (c, ym, f"% {tot:,.2f}")).fetchone()[0]
                for c, ym, tot in conn.execute(
                    "SELECT card_suffix, year_month, total_due FROM statements").fetchall())
            self.assert_eq("statement totals glued onto merchant names", glued, 0)
        finally:
            conn.close()

    def test_real_pdf(self, pdf_path: str) -> None:
        print(f"--- real PDF: {pdf_path} ---")
        try:
            text = read_pdf_text(pdf_path)
        except ParseError as exc:
            self._fail(f"read_pdf_text raised: {exc}")
            return
        self._ok(f"read {len(text)} chars from PDF")

        card, ym = detect_card_and_period(text)
        self.assert_eq("card detected", bool(card), True)
        self.assert_eq("year_month detected", bool(ym), True)
        if not (card and ym):
            return

        try:
            data = core.parse_statement(text, card, ym)
        except ParseError as exc:
            self._fail(f"parse_statement raised: {exc}")
            return
        self._ok(f"parsed {len(data['transactions'])} transactions, total={data['total_due']:.2f}")

        try:
            warns = validate_parsed_data(data)
        except ParseError as exc:
            self._fail(f"validate_parsed_data raised: {exc}")
            return
        if warns:
            for w in warns:
                self._warn(w)
        self._ok(f"validation produced {len(warns)} warnings")

    # ── runner ───────────────────────────────────────────────────────────
    def run(self, pdf_path: str | None = None) -> int:
        # Migrations ensure imports table exists; harmless if already current.
        try:
            init_db()
            self._ok(f"init_db ok (schema_version={get_config('schema_version')})")
        except Exception as exc:
            self._fail(f"init_db failed: {exc}")
            traceback.print_exc()

        self.test_parse_amount()
        self.test_parse_date()
        self.test_normalize_and_canonicalize()
        self.test_esc_md()
        self.test_detect_card_and_period()
        self.test_country_codes()
        self.test_pdf_sniff()
        self.test_validate_parsed_data()
        self.test_cron_health()
        self.test_next_reminders()
        try:
            from scripts import selftest_regressions
            selftest_regressions.run_all(self)
        except Exception as exc:  # a crash in the harness is a failure, not a skip
            self._fail(f"v3.5 regression harness crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        self.test_live_data_invariants()
        if pdf_path:
            self.test_real_pdf(pdf_path)

        print()
        print(f"=== {self.passed} passed, {len(self.failures)} failed, "
              f"{len(self.warnings)} warning(s) ===")
        if self.failures:
            print("FAILURES:")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        return 0


def main(argv: list[str]) -> int:
    pdf = argv[1] if len(argv) > 1 else None
    return SelfTest().run(pdf)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
