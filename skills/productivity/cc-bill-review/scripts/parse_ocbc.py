#!/usr/bin/env python3
"""
OCBC Credit Card Statement Parser
Parses OCBC 365 (5999) and OCBC 90.N Visa (7008) statements.
Stores transactions + metadata in SQLite.
"""
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# NOTE: pymupdf is imported lazily inside read_pdf_text() (the only place it is
# used), NOT at module top. Importing this module for its DB/config helpers —
# which is exactly what the reminder path does (reminder.py → get_config/get_db/
# set_config) — must not require pymupdf. The cron reminder wrapper runs under
# the gateway venv (user-site OFF); a top-level `import pymupdf` there would
# couple the delivery-critical reminder to a heavy PDF dep it never touches, and
# a missing/broken wheel would kill the reminder at import time — silently, on a
# deliver:local job. Keeping the import lazy decouples them.

SKILL_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(__file__).parent / "cc_review.db"
LOG_PATH = SKILL_DIR / "skill.log"
SNAPSHOT_DIR = SKILL_DIR / "db_snapshots"
SNAPSHOT_KEEP = 5
CRON_JOB_ID = "7b68c1ef7afe"  # default; can be overridden via config['cron_job_id']
CRON_JOB_NAME = "CC Payment Reminder (both cards)"
SCHEMA_VERSION = 3

# ---------------------------------------------------------------------------
# Structured logging — INFO+ goes to skill.log (rotated). Failures previously
# vanished into stderr; now they have a durable home.
# ---------------------------------------------------------------------------
log = logging.getLogger("cc-bill-review")
if not log.handlers:
    log.setLevel(logging.INFO)
    try:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
        log.addHandler(handler)
    except OSError as exc:
        if os.environ.get("HERMES_SESSION_KEY"):
            # Inside an agent turn NOT stderr (v3.5.1): the terminal tool shows stderr to
            # the agent, so notes/tracebacks there are thread ids + puzzles in its context.
            log.addHandler(logging.NullHandler())
        else:   # cron / manual runs: stderr is the job output nobody else reads
            sys.stderr.write(f"warning: could not open {LOG_PATH}: {exc}\n")
            log.addHandler(logging.StreamHandler(sys.stderr))
    log.propagate = False
# A failing handler (disk full, a rollover race with the cron) must not print a
# "--- Logging error ---" traceback to stderr either.
logging.raiseExceptions = False


class ParseError(Exception):
    """Statement PDF could not be parsed reliably."""

# -----------------------------------------------------------------------------
# Canonical merchant map — collapses raw_cleaned variants to canonical names.
# Key = card_suffix (or '*' for all cards). Value = {raw_cleaned → canonical}.
# This is the second layer of a two-layer canonicalization:
#   Layer 1 (raw → raw_cleaned):  strip_footer_junk(), remove P-codes, etc.
#   Layer 2 (raw_cleaned → canonical): this map + canonicalize_merchant()
#
# Rules for this map:
#   - MERGE entries: different raw_cleaned strings that represent the same
#     logical merchant → one canonical name.  Applies to both genuine merchant
#     name variants (e.g. "THE WOK PEOPLE SINGAPORE" vs "THE WOK PEOPLE") and
#     to system entries where the embedded amount causes fragmentation
#     (e.g. "CASH REBATE 1,308.10" → "CASH REBATE").
#   - Normalize, then look up.  All keys in this map are already UPPERCASE
#     and have been through strip_footer_junk() + P-code removal.
#   - Adding a new variant here is the ONLY place you need to edit when a
#     new raw variant of an existing merchant appears — existing approvals
#     (which attach to canonical names) are never affected. Changing an entry
#     that ALREADY maps stored rows (a merge, v3.5.1's Coffee Hive) renames a
#     stored identity: bump MARKER in migrate_identities.py and run it.
# -----------------------------------------------------------------------------
_CARD_CANNONICAL_MAP_5999 = {
    # Merchant name variants (genuine same-merchant fragmentation)
    "THE WOK PEOPLE SINGAPORE":           "THE WOK PEOPLE",
    # OCBC renamed the -9113 Coffee Hive descriptor on the Oct 2026 statement
    # ("COFFEE HIVE - INNOV" -> "COFFEE HIVE  INNOVI"); the user merged #18 into #109.
    "COFFEE HIVE - INNOV SINGAPORE":      "COFFEE HIVE INNOVI SINGAPORE",
    "SHOPEE SINGAPORE MP SINGAPORE":      "SHOPEE SINGAPORE MP",
    # OCBC rephrased the description on the Jun 2026 statement onwards (same shop by name; the
    # shared -9113 prefix is a payment-channel code, not evidence of the same shop).
    "JUNGSUNGHAIR PTE SINGAPORE":         "SMP**JUNG SUNG HAIR",
    # NETFLIX: same -5445 terminal, OCBC alternates between LOS GATOS and SINGAPORE suffixes.
    "NETFLIX.COM SINGAPORE":              "NETFLIX.COM LOS GATOS",
    # GOMO: same monthly plan; OCBC renamed to GOMO BY SINGTEL from May 2026.
    "GOMO SINGTEL SINGAPORE":             "GOMO MOBILE PLAN SINGAPORE",
    # System entries — CASH REBATE fragments by embedded amount
    "CASH REBATE 1,308.10":               "CASH REBATE",
    "CASH REBATE 1,515.38":               "CASH REBATE",
    "CASH REBATE 3,431.10":               "CASH REBATE",
    "CASH REBATE 603.23":                 "CASH REBATE",
    "CASH REBATE 780.88":                 "CASH REBATE",
    # System entries — CCY CONVERSION FEE fragments by embedded amount
    "CCY CONVERSION FEE FOR: 12.17 SGD":  "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 12.50 SGD":  "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 15.00 SGD":  "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 2.83 SGD":   "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 2.98 SGD":   "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 6.98 SGD":   "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 7.25 SGD":   "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 20.98 SGD":   "CCY CONVERSION FEE",
    "CCY CONVERSION FEE FOR: 30.00 SGD":  "CCY CONVERSION FEE",
}

_CARD_CANNONICAL_MAP_7008 = {
    # Merchant name variants
    "SP DIGITAL PL-UTIL-RE SINGAPORE":    "SP DIGITAL PL-UTILITIE SINGAPORE",
    # OCBC alternates between SHOPEE SINGAPORE MP SINGAPORE and SHOPEE SINGAPORE SINGAPORE; same -1302 terminal.
    "SHOPEE SINGAPORE SINGAPORE":         "SHOPEE SINGAPORE MP SINGAPORE",
}

# Cross-card system patterns — these entries are always system-type regardless
# of which card they appear on.  Format: {normalized_name_substring → canonical}
_CROSSCARD_CANONICAL_MAP = {
    "TPER SPA":                           "TPER SPA",
    "ATB SERVIZI":                       "ATB SERVIZI",
    "CLESS TICKET ATM MI":               "CLESS TICKET",
    "LA BOTTEGA DEL TART":               "LA BOTTEGA DEL TART",
    "START ROMAGNA":                      "START ROMAGNA",
    "TAP & GO":                           "TAP & GO",
    "A T B SERVIZI S.P.A":               "ATB SERVIZI",  # variant spacing
    "XIAOMI MIMO":                       "XIAOMI MIMO API",  # MiMo API (USD) — flagged foreign via merchant_raw
    "PAYMENT INTERNET":                  "PAYMENT BY INTERNET",  # unify with standard form
}


def canonicalize_merchant(card_suffix: str, cleaned_name: str) -> str:
    """
    Layer-2 canonicalization: map a cleaned merchant name to its canonical form.

    First checks card-specific map, then cross-card system patterns.
    Returns the original cleaned_name if no mapping applies.
    """
    # Card-specific map
    card_map = (_CARD_CANNONICAL_MAP_5999 if card_suffix == "5999"
                else _CARD_CANNONICAL_MAP_7008 if card_suffix == "7008"
                else {})
    if cleaned_name in card_map:
        return card_map[cleaned_name]

    # Cross-card system pattern matching (substring check)
    for pattern, canonical in _CROSSCARD_CANONICAL_MAP.items():
        if pattern in cleaned_name:
            return canonical

    return cleaned_name

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_config(key: str, default: str = None) -> str | None:
    """Read a config value from the config table."""
    conn = get_db()
    cur = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key: str, value: str) -> None:
    """Write a config value (upsert)."""
    conn = get_db()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def init_db():
    """Create base tables (no-op if present), then apply pending migrations."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_suffix TEXT NOT NULL,
            year_month TEXT NOT NULL,          -- e.g. "2026-01"
            txn_date TEXT NOT NULL,            -- ISO date string "2026-01-09"
            merchant_raw TEXT NOT NULL,        -- exact description from PDF
            merchant_normalized TEXT NOT NULL, -- cleaned / canonical
            country_code TEXT,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'SGD',
            row_num INTEGER DEFAULT 0,         -- tiebreaker for genuine duplicate txns in PDF
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card_suffix, year_month, txn_date, merchant_normalized, amount, row_num)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_suffix TEXT NOT NULL,
            year_month TEXT NOT NULL,
            statement_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            total_due REAL NOT NULL,
            minimum_due REAL,
            subtotal REAL,
            last_balance REAL,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card_suffix, year_month)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS merchants (
            card_suffix TEXT NOT NULL,
            merchant_normalized TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            txn_count INTEGER DEFAULT 0,
            total_spent REAL DEFAULT 0,
            merchant_uid INTEGER,              -- stable per-merchant id for the approval workflow
            PRIMARY KEY (card_suffix, merchant_normalized)
        )
    """)
    conn.commit()
    _apply_migrations(conn)
    _assign_merchant_uids(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# DB snapshots (defensive backups before destructive ops)
# ---------------------------------------------------------------------------
def snapshot_db(label: str) -> Path | None:
    """Copy the live DB to db_snapshots/cc_review.db.{label}.{ts}.

    Returns the snapshot path, or None on failure (logged, not raised — a
    snapshot failure should not block the operation it was protecting).
    Keeps only the last SNAPSHOT_KEEP snapshots to bound disk use.
    """
    if not DB_PATH.exists():
        return None
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = SNAPSHOT_DIR / f"cc_review.db.{label}.{ts}"
        shutil.copy2(DB_PATH, dest)
        log.info("snapshot.created label=%s path=%s", label, dest.name)
        # Trim to last SNAPSHOT_KEEP
        snaps = sorted(SNAPSHOT_DIR.glob("cc_review.db.*"), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-SNAPSHOT_KEEP]:
            try:
                old.unlink()
            except OSError as exc:
                log.warning("snapshot.cleanup_failed path=%s err=%s", old.name, exc)
        return dest
    except OSError as exc:
        log.error("snapshot.failed label=%s err=%s", label, exc)
        return None


# ---------------------------------------------------------------------------
# Migrations — additive only. Each version applies idempotently inside a
# transaction. Existing data is never silently rewritten; raise instead.
# ---------------------------------------------------------------------------
def _migration_2_add_imports_table(conn) -> None:
    """v2: add `imports` audit log."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_path TEXT NOT NULL,
            card_suffix TEXT,
            year_month TEXT,
            txn_count INTEGER,
            total_due REAL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT NOT NULL,           -- 'in_progress' | 'ok' | 'parse_error' | 'write_error'
            error_message TEXT,
            parse_warnings TEXT             -- JSON array of strings
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_imports_card_ym ON imports (card_suffix, year_month)"
    )


def _migration_3_add_merchant_uid(conn) -> None:
    """v3: stable per-merchant integer id (`merchant_uid`) for the approval
    workflow. Lets `approve <id>` work without any conversation memory —
    the id printed in a review is the same id `approve.py` resolves later.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(merchants)")]
    if "merchant_uid" not in cols:
        conn.execute("ALTER TABLE merchants ADD COLUMN merchant_uid INTEGER")
    # _assign_merchant_uids (run by init_db after migrations) backfills ids.


_MIGRATIONS = {
    # version → callable taking a conn (no commit; runner commits)
    2: _migration_2_add_imports_table,
    3: _migration_3_add_merchant_uid,
}


def _assign_merchant_uids(conn) -> None:
    """Assign a stable `merchant_uid` to any merchant row missing one.

    Ids are sequential and never reused: a high-water mark in config
    (`next_merchant_uid`) survives deleted rows, so an id a past review showed
    can't later name a different merchant (a late "approve #105" would
    otherwise approve whatever got #105 next). A full `DELETE FROM merchants`
    rebuild reassigns them (documented) — fine, since rebuilds are rare and manual.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(merchants)")]
    if "merchant_uid" not in cols:
        return
    missing = conn.execute(
        "SELECT rowid FROM merchants WHERE merchant_uid IS NULL "
        "ORDER BY card_suffix, merchant_normalized"
    ).fetchall()
    if not missing:
        _bump_uid_mark(conn)
        return
    nxt = (conn.execute("SELECT COALESCE(MAX(merchant_uid), 0) FROM merchants")
           .fetchone()[0]) + 1
    hw = conn.execute("SELECT value FROM config WHERE key='next_merchant_uid'").fetchone()
    if hw and str(hw[0]).isdigit():
        nxt = max(nxt, int(hw[0]))
    for (rid,) in missing:
        conn.execute("UPDATE merchants SET merchant_uid=? WHERE rowid=?", (nxt, rid))
        nxt += 1
    conn.execute("INSERT INTO config (key, value) VALUES ('next_merchant_uid', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(nxt),))


def _bump_uid_mark(conn) -> None:
    """Keep next_merchant_uid >= MAX(uid)+1 (establishes the mark on old DBs)."""
    top = conn.execute("SELECT COALESCE(MAX(merchant_uid), 0) FROM merchants").fetchone()[0]
    hw = conn.execute("SELECT value FROM config WHERE key='next_merchant_uid'").fetchone()
    if not hw or not str(hw[0]).isdigit() or int(hw[0]) <= top:
        conn.execute("INSERT INTO config (key, value) VALUES ('next_merchant_uid', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(top + 1),))


def _apply_migrations(conn) -> None:
    cur = conn.execute("SELECT value FROM config WHERE key='schema_version'")
    row = cur.fetchone()
    current = int(row["value"]) if row else 1
    if current >= SCHEMA_VERSION:
        return
    snapshot_db(f"premigration_v{current}_to_v{SCHEMA_VERSION}")
    for ver in range(current + 1, SCHEMA_VERSION + 1):
        migration = _MIGRATIONS.get(ver)
        if migration is None:
            log.warning("migration.missing version=%s — skipping", ver)
            continue
        log.info("migration.apply version=%s", ver)
        migration(conn)
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(ver),),
        )
        conn.commit()
        log.info("migration.done version=%s", ver)


# ---------------------------------------------------------------------------
# Imports audit log
# ---------------------------------------------------------------------------
def _record_import_start(cur, pdf_path: str, card_suffix: str | None,
                         year_month: str | None) -> int:
    cur.execute(
        "INSERT INTO imports (pdf_path, card_suffix, year_month, status) "
        "VALUES (?, ?, ?, 'in_progress')",
        (pdf_path, card_suffix, year_month),
    )
    return cur.lastrowid


def _record_import_finish(
    cur,
    import_id: int,
    *,
    status: str,
    card_suffix: str | None = None,
    year_month: str | None = None,
    txn_count: int | None = None,
    total_due: float | None = None,
    error_message: str | None = None,
    warnings: list[str] | None = None,
) -> None:
    cur.execute(
        "UPDATE imports SET finished_at=CURRENT_TIMESTAMP, status=?, "
        "card_suffix=COALESCE(?, card_suffix), "
        "year_month=COALESCE(?, year_month), "
        "txn_count=?, total_due=?, error_message=?, parse_warnings=? "
        "WHERE id=?",
        (
            status,
            card_suffix,
            year_month,
            txn_count,
            total_due,
            error_message,
            json.dumps(warnings) if warnings else None,
            import_id,
        ),
    )


def get_last_import(card_suffix: str, year_month: str) -> sqlite3.Row | None:
    """Return the most recent imports row for a (card, year_month), or None."""
    conn = get_db()
    try:
        cur = conn.execute(
            "SELECT * FROM imports WHERE card_suffix=? AND year_month=? "
            "ORDER BY started_at DESC LIMIT 1",
            (card_suffix, year_month),
        )
        return cur.fetchone()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
# Full ISO 3166-1 alpha-2 set. Used both as a stop-set during PDF parsing
# (to recognise the country code line that ends each transaction block) and
# as the validation set for `validate_parsed_data`. Previously held only
# ~40 codes; an unknown country was silently dropped.
COUNTRY_CODES = {
    "AD","AE","AF","AG","AI","AL","AM","AO","AQ","AR","AS","AT","AU","AW","AX","AZ",
    "BA","BB","BD","BE","BF","BG","BH","BI","BJ","BL","BM","BN","BO","BQ","BR","BS",
    "BT","BV","BW","BY","BZ","CA","CC","CD","CF","CG","CH","CI","CK","CL","CM","CN",
    "CO","CR","CU","CV","CW","CX","CY","CZ","DE","DJ","DK","DM","DO","DZ","EC","EE",
    "EG","EH","ER","ES","ET","FI","FJ","FK","FM","FO","FR","GA","GB","GD","GE","GF",
    "GG","GH","GI","GL","GM","GN","GP","GQ","GR","GS","GT","GU","GW","GY","HK","HM",
    "HN","HR","HT","HU","ID","IE","IL","IM","IN","IO","IQ","IR","IS","IT","JE","JM",
    "JO","JP","KE","KG","KH","KI","KM","KN","KP","KR","KW","KY","KZ","LA","LB","LC",
    "LI","LK","LR","LS","LT","LU","LV","LY","MA","MC","MD","ME","MF","MG","MH","MK",
    "ML","MM","MN","MO","MP","MQ","MR","MS","MT","MU","MV","MW","MX","MY","MZ","NA",
    "NC","NE","NF","NG","NI","NL","NO","NP","NR","NU","NZ","OM","PA","PE","PF","PG",
    "PH","PK","PL","PM","PN","PR","PS","PT","PW","PY","QA","RE","RO","RS","RU","RW",
    "SA","SB","SC","SD","SE","SG","SH","SI","SJ","SK","SL","SM","SN","SO","SR","SS",
    "ST","SV","SX","SY","SZ","TC","TD","TF","TG","TH","TJ","TK","TL","TM","TN","TO",
    "TR","TT","TV","TW","TZ","UA","UG","UM","US","UY","UZ","VA","VC","VE","VG","VI",
    "VN","VU","WF","WS","YE","YT","ZA","ZM","ZW",
}

def strip_footer_junk(s: str) -> str:
    """Strip page-footer bleed (OCBC Bank address, subtotal/total lines)."""
    # Footer bleed patterns: OCBC Bank address, page numbers, contact info
    # Applied in order; more-specific patterns first; patterns use re.IGNORECASE
    FOOTER_PATTERNS = [
        # Long concatenated bleed (footer glued to merchant without space)
        # e.g. "THE WOK PEOPLE SINGAPORE OCBC Bank 65 Chulia Street, OCBC Centre..."
        # The lookbehind KEEPS the merchant's city: the last row on a page is the
        # one that gets the footer, and eating its "SINGAPORE" gave that one row a
        # second identity (#108 COFFEE HIVE INNOVI vs #109 … SINGAPORE, 2026-10).
        r'(?<=SINGAPORE)\s+OCBC\s+BANK\s+\d+\s+CHULIA',
        # Strip everything from SINGAPORE followed by address-like content
        r'SINGAPORE\s+\d{4,}.*',           # Singapore 049513 Contact Us...
        r'SINGAPORE\s+OVERSEA[\-\s]CHINESE.*',  # Singapore Oversea-Chinese...
        # Individual footer fragments (whitespace-prefixed in normal case)
        r'\s+OCBC\s+BANK\s+\d+\s+CHULIA',
        r'\s+OCBC\s+BANK\s+65\s+CHULIA',
        r'\s+CONTACT\s+US\s+\d+',
        r'\s+WHEN\s+OVERSEAS',
        r'\s+OVERSEA[\-\s]CHINESE\s+BANKING',
        r'\s+CO\.\s+REG\.\s+NO\.:?\s+\w+',
        r'\s+\d{6,}\s*[-–]\s*PAGE\s+\d+\s+OF\s+\d+',
        r'\s+HCBS\d+\(\d+\)',
        r'\s+SUBTOTAL\s+[\d,]+\.?\d*\s+TOTAL',
        r'\s+SUBTOTAL\s+[\d,]+\.?\d*',
        r'\s+TOTAL(?:\s+\S+){1,3}$',
        # Direct concatenation fragments (no leading whitespace)
        r'OCBC\s+BANK\s+\d+\s+CHULIA',
        r'OCBC\s+BANK\s+65\s+CHULIA',
        r'STREET,\s*OCBC\s+CENTRE',
        r'TRANSACTION\s+DATE\s+DESCRIPTION\s+AMOUNT',
        r'LIMITED\s+TRANSACTION\s+DATE',
        r'CORPORATION\s+LIMITED\s+TRANSACTION',
        # Partial address fragments
        r'\s+\d{4,}.*$',
    ]
    result = s
    for pat in FOOTER_PATTERNS:
        result = re.sub(pat, '', result, flags=re.IGNORECASE)
    return result.strip()

_FX_SEGMENT = re.compile(r"\s*\bFOREIGN\s+CURRENCY\s+[A-Z]{3}\s+[\d,]+(?:\.\d{1,3})?\b",
                         re.IGNORECASE)   # zero-decimal currencies too (JPY 1,500)


def normalize_merchant(raw: str, card_suffix: str = "") -> str:
    """Strip codes, trailing garbage, normalize whitespace, uppercase, then canonicalize."""
    s = strip_footer_junk(raw)
    # "FOREIGN CURRENCY USD 10.00" is the billing currency + original amount, not
    # part of the merchant's name — keeping it gave every USD charge its own
    # identity (NANONOBLE x3 → a false "new merchant" each month). The foreign
    # flag reads the marker from merchant_raw, which keeps it.
    s = _FX_SEGMENT.sub(" ", s)
    parts = s.strip().split()
    # Remove trailing country code(s), numeric codes, and full country names that
    # appear at the end of system descriptions (e.g. "CCY CONV FEE FOR: 20.98 SGD SINGAPORE")
    # but NOT from merchant names where SINGAPORE is part of the business name.
    COUNTRY_NAMES = {"SINGAPORE", "SINGAPORE.", "REPUBLIC", "REPUBLIC OF SINGAPORE"}
    # Strip trailing/embedded country codes and numeric/placeholder codes, EXCEPT
    # a small denylist of 2-letter ISO codes that are also legitimate merchant
    # tokens AND are required verbatim by a canonical-map key. COUNTRY_CODES is
    # the full ISO alpha-2 set, so a blanket strip silently deletes real name
    # tokens: "MP" (Shopee marketplace → MP=Northern Mariana) and the leading
    # "LA" of "LA BOTTEGA DEL TART" (→ Laos). Deleting them both fragments the
    # merchant into a spurious "new merchant" on the next import and defeats the
    # canonical map (whose key — "SHOPEE SINGAPORE MP SINGAPORE", the "LA BOTTEGA
    # DEL TART" substring — no longer matches). Every other embedded code (e.g.
    # the "SG" in "GOOGLE*IQIYI SG SINGAPORE", the "BY"/"LT" that canonical keys
    # rely on being stripped) is preserved-as-stripped to keep normalization
    # byte-identical on the live data (drift=0).
    _KEEP_TOKENS = {"MP", "LA"}
    cleaned_parts = []
    for p in parts:
        if p in COUNTRY_CODES and p not in _KEEP_TOKENS:
            continue
        if re.match(r'^\d+$', p) or p == 'P' or p == 'N/A':
            continue
        cleaned_parts.append(p)
    # If the last word is a full country name and any prior part contains a decimal
    # number (i.e. the description is a system entry with an amount like
    # "CCY CONVERSION FEE FOR: 20.98 SGD SINGAPORE"), strip the trailing country name.
    if (len(cleaned_parts) >= 2
            and cleaned_parts[-1] in COUNTRY_NAMES
            and any(re.search(r'\d+\.\d+', p) for p in cleaned_parts)):
        cleaned_parts = cleaned_parts[:-1]

    name = " ".join(cleaned_parts).upper().strip()

    # ── Merchant-specific normalizations ───────────────────────────────────────

    # Strip leading P-code (e.g. "-2030 APPLE.COM/SG" -> "APPLE.COM/SG")
    # These are merchant category codes, not part of the merchant name.
    name = re.sub(r'^[\-−]\d+\s+', '', name)

    # Spotify: strip P-codes (e.g. "P3DE59542B") — keep "SPOTIFY STOCKHOLM"
    if "SPOTIFY" in name and "STOCKHOLM" in name:
        return "SPOTIFY STOCKHOLM"

    # Parking.SG Bill: strip coupon codes (C55, 5BE, CCB, etc.)
    if re.match(r'^PARKING\.SG BILL [A-Z0-9]+( SINGAPORE)?$', name):
        return re.sub(r' [A-Z0-9]+( SINGAPORE)?$', ' SINGAPORE', name)

    # M1Ltd Recurring: strip statement total suffix (e.g. "M1LTD RECURRING SINGAPORE 186.98")
    if re.match(r'^M1LTD RECURRING SINGAPORE \d+\.\d+$', name):
        return "M1LTD RECURRING SINGAPORE"

    # Watsons: strip statement total suffix (e.g. "WATSONS - WHITE SAN SINGAPORE 396.64")
    if re.match(r'^WATSONS.* SINGAPORE \d+\.\d+$', name):
        return re.sub(r' \d+\.\d+$', '', name)

    # Layer 2: canonicalize (merge variants, collapse system fragments)
    if card_suffix:
        name = canonicalize_merchant(card_suffix, name)

    return name

def parse_amount(s: str) -> float:
    """Parse OCBC amount strings — handles "(123.45)", "(123.45", "1,234.56", etc.

    Falls back to a digit-only extraction so a single malformed line never
    aborts the whole statement parse. Returns 0.0 only if nothing numeric
    is present (caller should treat that as a parse failure for that row).
    """
    s = s.strip().replace(",", "").replace(" ", "")
    # "(123.45)" or "(123.45" (credits/payments; closing paren may wrap a line)
    m = re.match(r'^\(([\d.]+)\)?$', s)
    if m:
        try:
            return -float(m.group(1))
        except ValueError:
            pass  # multi-dot / malformed → fall through to digit extraction
    # "123.45" plain
    m = re.match(r'^([\d.]+)$', s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # Last-resort fallback: extract whatever digits/decimal/sign we can find.
    # Documented in references/parse-ocbc-errors.md item #3. A float() on any
    # branch above can still raise on a multi-dot token (e.g. "1.2.3"); the
    # try/except routes it here rather than aborting the whole statement parse.
    cleaned = "".join(c for c in s if c in ".-0123456789")
    if cleaned and cleaned not in {".", "-", "-."}:
        try:
            return float(cleaned)
        except ValueError:
            pass
    return 0.0

def parse_date(s: str, year_month: str) -> str:
    """
    Convert DD/MM or DD/MM/YY to YYYY-MM-DD.

    Raises ParseError if the input is not a valid date string. Returning
    "" silently used to produce empty txn_date values that NOT NULL did
    not block.

    The statement year_month is the billing period identifier (e.g. "2026-01" for
    the Jan statement).  Transaction dates DD/MM within the statement text may
    refer to the previous calendar year when the transaction month is GREATER
    than the statement month — e.g. in the Jan statement (month=01), a Dec 5
    transaction (month=12) is 2025-12-05, NOT 2026-12-05.
    """
    s = s.strip()
    if not s or "/" not in s:
        raise ParseError(f"empty or malformed date: {s!r}")
    parts = s.split("/")
    if len(parts) not in (2, 3):
        raise ParseError(f"unexpected date format: {s!r}")
    day = parts[0].zfill(2)
    month = parts[1].zfill(2)
    stmt_month = int(year_month[5:7])
    txn_month = int(month)
    yr = year_month[:4]

    if len(parts) == 3:
        # 3-part date DD/MM/YY — always expand year directly
        yr = "20" + parts[2] if len(parts[2]) == 2 else parts[2]
    else:
        # 2-part date DD/MM — if txn month > stmt month, transaction is from
        # the previous calendar year (wrap-around, e.g. Dec txns in Jan statement)
        if txn_month > stmt_month:
            yr = str(int(yr) - 1)

    result = f"{yr}-{month}-{day}"
    # Reject impossible dates (e.g. "13/13" -> "2025-13-13", "00/00") rather than
    # emitting an invalid txn_date that only blows up much later at read time.
    # Consistent with this function's contract of raising ParseError on bad input.
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise ParseError(f"impossible date {s!r} -> {result}") from exc
    return result

# ---------------------------------------------------------------------------
# Extract transactions from raw page text
# ---------------------------------------------------------------------------
# Block-stop: words that indicate a page footer / non-transaction section.
# Only stop if the entire stripped line IS the keyword (not just starts with it).
_EXACT_STOP = {
    "OCBC", "CHULIA", "CONTACT", "OVERSEAS", "LIMITED", "REG.",
    "TOTAL", "SUBTOTAL", "HCBS", "PAGE", "PROCESSING", "GIRO",
    "AXS", "CHEQUE", "INTEREST", "ANNUAL", "FEES", "CARD",
}
_NON_TXN_PREFIXES = ("Singapore ", "CO. ", "Please ")


def extract_transactions(text: str) -> list:
    """Parse transaction blocks from PDF text.

    State machine: date -> amount -> [description lines] -> country -> next date.
    Raises ParseError if the transaction-section header is missing — that
    used to silently return [] and let an empty statement get persisted.
    """
    transactions = []

    # Tolerant of casing/spacing changes — OCBC has been known to tweak headers.
    header_match = re.search(
        r'TRANSACTION\s+DATE\s+DESCRIPTION\s+AMOUNT\s*\(?\s*SGD\s*\)?',
        text,
        re.IGNORECASE,
    )
    if not header_match:
        raise ParseError(
            "transaction-section header not found — OCBC may have changed the "
            "PDF layout, or this is not a credit-card statement"
        )

    start = header_match.end()
    end_match = re.search(r'TOTAL AMOUNT DUE', text[start:], re.IGNORECASE)
    block = text[start:start + (end_match.start() if end_match else 0)]
    lines = block.split("\n")
    n = len(lines)

    first_date_idx = None
    for idx, line in enumerate(lines):
        if re.match(r'^\s{2,}\d{1,2}/\d{1,2}(?:/\d{2})?\s*$', line):
            first_date_idx = idx
            break
    if first_date_idx is None:
        return transactions

    i = first_date_idx
    while i < n:
        raw_line = lines[i]
        line = raw_line.strip()

        if not line or line == ")":
            i += 1
            continue

        if line in _EXACT_STOP:
            break

        if any(line.startswith(p) for p in _NON_TXN_PREFIXES):
            break

        date_m = re.match(r'^(\s{2,})(\d{1,2}/\d{1,2}(?:/\d{2})?)', raw_line)
        if not date_m:
            i += 1
            continue

        txn_date_raw = date_m.group(2)
        i += 1
        amount = 0.0
        desc_parts = []
        country = ""

        while i < n:
            peek = lines[i].strip()
            i_next = i + 1

            if not peek or peek == ")":
                i = i_next
                continue

            if amount == 0.0 and re.search(r'\d+\.\d{2}\)?$', peek):
                amount = parse_amount(peek)
                i = i_next
                continue

            if country == "" and re.match(r'^[A-Z]{2}$', peek) and peek in COUNTRY_CODES:
                country = peek
                i = i_next
                continue

            if re.match(r'^\d{1,2}/\d{1,2}(?:/\d{2})?$', peek):
                break

            if peek in _EXACT_STOP:
                break

            desc_parts.append(peek)
            i = i_next

        merchant_raw = " ".join(desc_parts).strip()
        # Column layout ("AMAZON MKTPLACE   SEATTLE   US") puts the country on the
        # description line itself; without this a foreign charge laid out that
        # way had country '' and was never flagged foreign. Only sets the
        # country — the description (and so the merchant identity) is unchanged.
        if not country:
            for part in reversed(desc_parts):
                if re.fullmatch(r"\(?[\d,]+\.\d{2}\)?", part):
                    continue   # the glued statement total (stripped later)
                # the country column is a WIDE gap (real rows: "...SINGAPORE    SG",
                # 4 spaces); a 2-space gap is a truncated name ("HUGGS COFFEE PTE  LT")
                m_cc = re.search(r"\S\s{3,}([A-Z]{2})$", part)
                if m_cc and m_cc.group(1) in COUNTRY_CODES:
                    country = m_cc.group(1)
                break
        if merchant_raw and amount != 0.0:
            transactions.append({
                "txn_date": txn_date_raw,
                "merchant_raw": merchant_raw,
                "amount": amount,
                "country_code": country,
            })

    return transactions


def strip_glued_total(transactions: list, total_due: float) -> None:
    """The PDF text puts the statement's TOTAL AMOUNT DUE figure right after the
    last transaction, so it was appended to that transaction's description in
    every statement ("SHOPEE SG MP ... SG 2,689.54") — a junk merchant identity
    and a false "new merchant" unless a special case happened to strip it.
    Remove exactly that trailing figure from the LAST transaction only."""
    if not transactions or total_due is None:
        return
    tail = " " + f"{total_due:,.2f}"
    last = transactions[-1]
    if last["merchant_raw"].endswith(tail) and len(last["merchant_raw"]) > len(tail):
        last["merchant_raw"] = last["merchant_raw"][: -len(tail)].rstrip()


def parse_statement(text: str, card_suffix: str, year_month: str) -> dict:
    """Extract metadata + transactions from a statement.

    Raises ParseError when critical fields (statement date, due date, total due,
    or transaction-section header) are missing. Previously these failed open
    with empty strings or 0.0, allowing nonsense to land in the DB.
    """
    header_end = text.find("TRANSACTION DATE")
    header = text[:header_end] if header_end != -1 else text

    date_vals = re.findall(r'(\d{2}-\d{2}-\d{4})', header)
    if len(date_vals) < 2:
        raise ParseError(
            f"could not find statement_date and due_date in header for "
            f"{card_suffix}/{year_month} (found {len(date_vals)} DD-MM-YYYY values)"
        )
    stmt_date_str = date_vals[0]
    due_date_str = date_vals[1]

    # The header section contains TOTAL CREDIT LIMIT / TOTAL MINIMUM DUE — NOT
    # the actual amount due. The real TOTAL AMOUNT DUE appears below the
    # transaction list. Search the full text for the label and grab what follows.
    total_match = re.search(r'TOTAL AMOUNT DUE\s*\n?\s*([\d,]+\.\d{2})', text)
    if not total_match:
        raise ParseError(
            f"TOTAL AMOUNT DUE not found in {card_suffix}/{year_month} — "
            f"a $0.00 reminder would have been sent."
        )
    total_due = float(total_match.group(1).replace(",", ""))

    min_match = re.search(r'TOTAL MINIMUM DUE\s*\n?\s*S\$([\d,]+\.\d{2})', header)
    min_due = float(min_match.group(1).replace(",", "")) if min_match else 0.0

    transactions = extract_transactions(text)
    strip_glued_total(transactions, total_due)

    return {
        "card_suffix": card_suffix,
        "year_month": year_month,
        "statement_date": stmt_date_str,
        "due_date": due_date_str,
        "total_due": total_due,
        "minimum_due": min_due,
        "transactions": transactions,
    }


# ---------------------------------------------------------------------------
# PDF sanity sniffing — catch encrypted / scanned / wrong-document inputs
# *before* trying to parse. A clean error here is much better than a
# misleading partial parse.
# ---------------------------------------------------------------------------
def read_pdf_text(pdf_path: str) -> str:
    """Open a PDF and return its full text. Raises ParseError on common
    failure modes (missing, encrypted, scanned, not a CC statement).

    The Discord upload pipeline can route PDFs that aren't actually OCBC
    statements (a bank letter, a screenshot of a screenshot, etc.); this
    is the first line of defense against parsing those.
    """
    p = Path(pdf_path)
    if not p.exists():
        raise ParseError(f"PDF not found: {pdf_path}")
    if p.stat().st_size == 0:
        raise ParseError(f"PDF is empty: {pdf_path}")

    try:
        import pymupdf  # lazy — see module-top note; keeps the reminder path pymupdf-free
    except ImportError as exc:
        raise ParseError(
            f"pymupdf is not importable under {sys.executable} ({exc}). Run the skill "
            "with the terminal's python3 (/usr/bin/python3, which has it in ~/.local) — "
            "not execute_code / the gateway venv.") from exc
    try:   # MuPDF prints its own warnings/errors to stderr, which the agent reads
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:  # noqa: BLE001 — another pymupdf API: the diagnostics stay on
        pass
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise ParseError(f"could not open PDF {pdf_path}: {exc}") from exc

    try:
        if getattr(doc, "is_encrypted", False) and not doc.authenticate(""):
            raise ParseError(f"PDF is password-protected: {pdf_path}")
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    try:   # the diagnostics switched off above go to skill.log instead
        warns = pymupdf.TOOLS.mupdf_warnings()
        if warns:
            log.info("mupdf warnings for %s: %s", Path(pdf_path).name,
                     " | ".join(warns.splitlines())[:500])
    except Exception:  # noqa: BLE001
        pass

    if len(text.strip()) < 100:
        raise ParseError(
            f"PDF contains almost no extractable text ({len(text.strip())} chars) — "
            "likely a scanned image without OCR. cc-bill-review needs digital text."
        )

    upper = text.upper()
    if "OCBC" not in upper or "STATEMENT" not in upper:
        raise ParseError(
            "PDF does not look like an OCBC credit-card statement "
            "(missing 'OCBC' and/or 'STATEMENT' tokens)."
        )

    return text


# ---------------------------------------------------------------------------
# Detect card_suffix and year_month from the PDF body itself, so an upstream
# routing mistake (wrong filename → wrong card) cannot silently corrupt data.
# ---------------------------------------------------------------------------
_MONTHS_3 = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def detect_card_and_period(text: str) -> tuple[str | None, str | None]:
    """Return (card_suffix, year_month) inferred from PDF text, or None for
    each field that couldn't be confidently determined.

    Card suffix: the last 4 digits in any "CARD NO XXXX-XXXX-XXXX-NNNN" or
    "ENDING NNNN" pattern. We accept the first match — OCBC statements list
    the card number near the top; later occurrences are footers.

    Year-month: from the statement_date in DD-MM-YYYY format (preferred), or
    "Statement Period DD MMM YY - DD MMM YY" (legacy).
    """
    upper = text.upper()
    card = None

    # Patterns seen in OCBC statements:
    #   "Card No.: XXXX-XXXX-XXXX-5999"   (masked — most common)
    #   "Card Number ending 5999"
    #   "1234-5678-9012-5999"             (rare full-print)
    # Strategy: find the "CARD NO/NUMBER" prefix, then take the LAST 4-digit
    # group within ~80 chars — that's the unmasked tail in every layout.
    m = re.search(r"\bCARD\s+(?:NO\.?|NUMBER)[^\n]{0,80}", upper)
    if m:
        digits = re.findall(r"\b(\d{4})\b", m.group(0))
        if digits:
            card = digits[-1]
    if card is None:
        m = re.search(r"ENDING(?:\s+IN)?\s+(\d{4})\b", upper)
        if m:
            card = m.group(1)
    if card is None:
        # Last fallback: a fully-printed 16-digit card number anywhere
        m = re.search(r"(?:[X\d*]{4}[\s-]){3}(\d{4})\b", upper)
        if m:
            card = m.group(1)

    # Year-month from statement_date (first DD-MM-YYYY occurrence in header)
    header_end = text.find("TRANSACTION DATE")
    header = text[:header_end] if header_end != -1 else text[:2000]
    ym = None
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", header)
    if m:
        _, mm, yyyy = m.groups()
        ym = f"{yyyy}-{mm}"
    if ym is None:
        # Legacy "DD MMM YY" (rare in modern OCBC PDFs but kept as fallback)
        m = re.search(r"\b\d{1,2}\s+([A-Z]{3})\s+(\d{2})\b", upper)
        if m:
            mon, yy = m.groups()
            mm = _MONTHS_3.get(mon)
            if mm:
                ym = f"20{yy}-{mm}"

    return card, ym


# ---------------------------------------------------------------------------
# Post-parse validation — flag implausible parsed data before it lands in
# the DB. Some checks raise ParseError (definite parser failure), others
# return warnings the bot can surface in the import response.
# ---------------------------------------------------------------------------
def validate_parsed_data(data: dict) -> list[str]:
    """Validate a parse_statement() result. Mutates `data` to add a
    'warnings' key. Raises ParseError for definite failures (impossible
    dates, missing transactions when total_due > 0, etc.). Returns the
    warnings list as a convenience.
    """
    warnings: list[str] = []
    today = datetime.now().date()

    def _parse_dmy(s: str):
        try:
            return datetime.strptime(s, "%d-%m-%Y").date()
        except (ValueError, TypeError):
            return None

    stmt = _parse_dmy(data.get("statement_date", ""))
    due = _parse_dmy(data.get("due_date", ""))

    if stmt is None:
        raise ParseError(f"unparseable statement_date: {data.get('statement_date')!r}")
    if due is None:
        raise ParseError(f"unparseable due_date: {data.get('due_date')!r}")

    if stmt > today:
        warnings.append(f"statement_date {stmt} is in the future")
    if (today - stmt).days > 90:
        warnings.append(
            f"statement_date {stmt} is more than 90 days old — re-importing an old PDF?"
        )
    if due < stmt:
        raise ParseError(f"due_date {due} is before statement_date {stmt}")
    if (due - stmt).days > 35:
        warnings.append(f"due_date is {(due - stmt).days} days after statement_date (unusually far)")

    total_due = data.get("total_due", 0)
    if total_due is None or total_due < 0:
        raise ParseError(f"total_due must be non-negative, got {total_due!r}")

    txns = data.get("transactions", [])
    if not txns and total_due > 0:
        warnings.append(
            f"total_due is S${total_due:.2f} but parser found 0 transactions — "
            "likely a parser bug or new statement layout"
        )

    # Sum-of-debits sanity. OCBC's total_due bakes in last_balance and
    # payments which we don't fully extract, so this is a soft check: the
    # largest single *debit* can never exceed total_due by more than 5%.
    # (Credits/payments — e.g. a full "PAYMENT BY INTERNET" of last month's
    # balance — are legitimately larger than this month's total_due, so only
    # debits are considered here to avoid a false warning on every import.)
    if txns and total_due > 0:
        max_single = max((t["amount"] for t in txns if t["amount"] > 0), default=0.0)
        if max_single > total_due * 1.05:
            warnings.append(
                f"largest transaction (S${max_single:.2f}) exceeds total_due "
                f"(S${total_due:.2f}) — likely amount mis-parse"
            )

    # Country-code validation: any non-empty code must be in COUNTRY_CODES.
    bad_countries = sorted({
        t["country_code"] for t in txns
        if t.get("country_code") and t["country_code"] not in COUNTRY_CODES
    })
    if bad_countries:
        warnings.append(
            f"unknown country codes (will pass through unfiltered): {bad_countries}"
        )

    # Per-transaction date sanity — should fall in the ~60-day window leading
    # up to the statement date.
    earliest = stmt.replace(day=1)
    earliest = earliest.replace(year=earliest.year - (1 if earliest.month <= 2 else 0))
    out_of_window = 0
    for t in txns:
        d = _parse_dmy_iso(t["txn_date"])
        if d is None and data.get("year_month"):
            # txn dates are raw DD/MM here; resolve them the way the writer will
            try:
                d = _parse_dmy_iso(parse_date(t["txn_date"], data["year_month"]))
            except ParseError as exc:
                raise ParseError(f"transaction date unusable: {exc}") from exc
        if d is None:
            continue
        if d > stmt + _DAYS(2) or d < stmt - _DAYS(75):
            out_of_window += 1
    if out_of_window:
        warnings.append(f"{out_of_window} transaction(s) have dates outside the expected window")

    data["warnings"] = warnings
    return warnings


# Small helpers for validate_parsed_data
def _parse_dmy_iso(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _DAYS(n: int):
    from datetime import timedelta
    return timedelta(days=n)


# ---------------------------------------------------------------------------
# Cron health (read-only) — looked up by id/name in ~/.hermes/cron/jobs.json.
#
# The skill never pauses/resumes/edits the reminder cron. (Until v3.5 every
# import ran `hermes cron resume`, documented as a "harmless no-op" — it is not:
# resume_job() rewrites jobs.json and recomputes next_run_at from NOW, so an
# import between 09:00:00 and the scheduler tick pushed that day's reminder run
# to tomorrow, and it silently undid a deliberate pause.)
#
# State is read from jobs.json directly (the scheduler's source of truth) —
# `hermes` is NOT on the non-login PATH the agent terminal and cron use, so a
# CLI-only check false-alarmed on every import. The CLI (absolute path) is only
# a fallback when jobs.json is unreadable.
# ---------------------------------------------------------------------------
_CRON_CLI_TIMEOUT = 15  # seconds — a wedged CLI must not hang an import/approve


def _hermes_bin() -> str | None:
    """Absolute path to the hermes CLI, or None. It is not on the non-login
    PATH, so probe its known install locations before falling back to which()."""
    for cand in (
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/bin/hermes"),
        "/usr/local/bin/hermes",
    ):
        if os.path.exists(cand):
            return cand
    return shutil.which("hermes")


def _jobs_json_path() -> str:
    home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
    return os.path.join(home, "cron", "jobs.json")


def _read_jobs_json() -> list[dict] | None:
    """Return the cron jobs from ~/.hermes/cron/jobs.json (source of truth) as a
    list, or None if it can't be read (an empty list means "readable, no jobs").
    Preferred over scraping the `hermes` CLI."""
    try:
        with open(_jobs_json_path()) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("cron.jobs_json_unreadable path=%s err=%s", _jobs_json_path(), exc)
        return None
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else None


def _find_reminder_job(jobs: list[dict]) -> dict | None:
    """Locate our reminder job in a jobs.json list — by cached/compiled id first,
    then by name."""
    want_id = get_config("cron_job_id", CRON_JOB_ID)
    by_id = next((j for j in jobs if j.get("id") == want_id), None)
    if by_id:
        return by_id
    return next((j for j in jobs if j.get("name") == CRON_JOB_NAME), None)


def _hermes_cron_list_all() -> str:
    """Run `hermes cron list --all` and return its stdout. Empty string on failure.
    Used only as a fallback when jobs.json is unreadable."""
    hermes = _hermes_bin()
    if not hermes:
        log.error("cron.cli_missing")
        return ""
    try:
        result = subprocess.run(
            [hermes, "cron", "list", "--all"],
            capture_output=True,
            text=True,
            timeout=_CRON_CLI_TIMEOUT,
        )
    except FileNotFoundError:
        log.error("cron.cli_missing")
        return ""
    except subprocess.TimeoutExpired:
        log.error("cron.list_timeout after=%ss", _CRON_CLI_TIMEOUT)
        return ""
    if result.returncode != 0:
        log.error("cron.list_failed rc=%s stderr=%s", result.returncode, result.stderr.strip())
        return ""
    return result.stdout


def _parse_cron_listing(text: str) -> list[dict]:
    """Parse `hermes cron list --all` output into structured rows.

    Output format (whitespace-indented blocks):
        7b68c1ef7afe [paused]
            Name:      CC Payment Reminder (both cards)
            Schedule:  0 9 20-22 * *
            Next run:  2026-06-20T09:00:00+08:00
            Deliver:   discord:1499972245437550602
            Last run:  2026-05-22T09:06:44.625437+08:00  ok
    """
    jobs = []
    current = None
    for raw in text.splitlines():
        m = re.match(r"^\s*([a-f0-9]{8,})\s*\[(\w+)\]\s*$", raw)
        if m:
            if current:
                jobs.append(current)
            current = {"id": m.group(1), "state": m.group(2), "raw": []}
            continue
        if current is None:
            continue
        current["raw"].append(raw)
        m = re.match(r"^\s*(Name|Schedule|Next run|Deliver|Skills|Last run):\s+(.+?)\s*$", raw)
        if m:
            key = m.group(1).lower().replace(" ", "_")
            current[key] = m.group(2)
    if current:
        jobs.append(current)
    return jobs


def _last_run_problem(job: dict) -> str | None:
    """A failing or stalled job is not healthy even if it is "active". (June
    2026: the wrapper failed every day for a month while this check said ✅,
    and that month's due-date reminder never went out.)"""
    try:
        streak = int(job.get("failure_streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    if job.get("last_status") == "error" or streak > 0:
        err = (str(job.get("last_error") or "unknown error").splitlines() or ["?"])[0][:160]
        return (f"the reminder cron's last run FAILED ({err})"
                + (f" — {streak} failed run(s) in a row" if streak > 1 else ""))
    sched = job.get("schedule")
    expr = (sched.get("expr") if isinstance(sched, dict) else sched) or ""
    if expr and " ".join(str(expr).split()) != "0 9 * * *":
        return (f"the reminder cron's schedule is {expr!r} — reminders need a DAILY 09:00 "
                f"run (`0 9 * * *`)")
    state = str(job.get("state") or "").lower()
    if state in ("error", "failed", "completed", "expired") or (
            state and not job.get("next_run_at")):
        return f"the reminder cron is in state {state!r} with no next run — it won't fire"
    nxt = job.get("next_run_at")
    if nxt:
        try:
            due = datetime.fromisoformat(str(nxt))
            now = datetime.now(due.tzinfo) if due.tzinfo else datetime.now()
            if (now - due).total_seconds() > 2 * 3600:
                return f"the reminder cron's next run ({str(nxt)[:16]}) is overdue — it isn't firing"
        except (ValueError, TypeError):
            pass
    last = job.get("last_run_at")
    if last:
        try:
            ran = datetime.fromisoformat(str(last))
            now = datetime.now(ran.tzinfo) if ran.tzinfo else datetime.now()
            if (now - ran).total_seconds() > 50 * 3600:
                return f"the reminder cron hasn't run since {str(last)[:16]} (it should run daily)"
        except (ValueError, TypeError):
            pass
    return None


def verify_cron_health() -> dict:
    """Return a structured snapshot of the reminder cron's health.

    Reads ~/.hermes/cron/jobs.json (the scheduler's source of truth) first, and
    only falls back to scraping the `hermes` CLI if that file is unreadable.
    This is deliberate: `hermes` is not on the non-login PATH the agent terminal
    and cron use, so the old CLI-only check reported a permanent false alarm.

    Keys:
        found:           True if the job exists in jobs.json (or the CLI)
        job_id:          Current resolved job ID (or None)
        state:           'active' | 'paused' | None
        schedule:        cron schedule string (or None)
        next_run, last_run: as reported by the scheduler (or None)
        expected_paused: retained for compatibility; always False in v3.
        source:          'jobs.json' | 'hermes-cli' | 'unknown'
        determinable:    True if we could actually read the cron state anywhere.
                         When False, `found`/`healthy` are unknown — callers must
                         NOT report "reminders broken" (see reminder_confirmation).
        healthy:         job exists AND state == 'active' (only meaningful when
                         determinable is True).
        notes:           list of human-readable warnings
    """
    notes: list[str] = []

    # Primary source: jobs.json (structured, no PATH/CLI-format dependency).
    jobs = _read_jobs_json()
    source = "jobs.json" if jobs is not None else "unknown"
    job = _find_reminder_job(jobs) if jobs else None
    determinable = jobs is not None

    def _fields_from_jobs_json(j):
        # Treat as paused if paused_at is set, enabled is False, OR the scheduler
        # wrote an explicit paused/disabled state string (belt-and-suspenders —
        # the live active job carries state "scheduled" + enabled true).
        state_str = str(j.get("state") or "").lower()
        paused = (bool(j.get("paused_at")) or j.get("enabled") is False
                  or state_str in ("paused", "disabled"))
        sched = j.get("schedule")
        if isinstance(sched, dict):
            sched = sched.get("expr")
        return ("paused" if paused else "active", sched,
                j.get("next_run_at"), j.get("last_run_at"), j.get("id"))

    run_problem = None

    def _fields_from_cli(j):
        return (j.get("state"), j.get("schedule"),
                j.get("next_run"), j.get("last_run"), j.get("id"))

    if job is not None:
        state, schedule, next_run, last_run, job_id = _fields_from_jobs_json(job)
        run_problem = _last_run_problem(job)
    elif jobs is not None:
        # jobs.json readable but our job is not in it — genuinely absent. (The
        # CLI reads the same file, so asking it would prove nothing.)
        state = schedule = next_run = last_run = job_id = None
    else:
        # jobs.json unreadable. Fall back to the CLI.
        text = _hermes_cron_list_all()
        if text:
            cli_jobs = _parse_cron_listing(text)
            cjob = (next((j for j in cli_jobs if j.get("name") == CRON_JOB_NAME), None)
                    or next((j for j in cli_jobs if j.get("id") == get_config("cron_job_id", CRON_JOB_ID)), None))
            source = "hermes-cli"
            determinable = True
            if cjob is not None:
                state, schedule, next_run, last_run, job_id = _fields_from_cli(cjob)
                job = cjob
            else:
                state = schedule = next_run = last_run = job_id = None
        else:
            # Neither jobs.json nor CLI could be read — state is UNKNOWN, not absent.
            determinable = False
            state = schedule = next_run = last_run = None
            job_id = get_config("cron_job_id", CRON_JOB_ID)

    found = job is not None
    expected_paused = False  # v3: daily cron, never auto-paused.

    conn = get_db()
    try:
        latest = list(conn.execute(
            """
            SELECT s.card_suffix, s.year_month, s.due_date FROM statements s
            JOIN (SELECT card_suffix, MAX(year_month) ym FROM statements GROUP BY card_suffix) l
              ON s.card_suffix = l.card_suffix AND s.year_month = l.ym
            """
        ).fetchall())
    finally:
        conn.close()
    if not latest:
        notes.append("no statements imported yet; reminders have nothing to fire")

    if not determinable:
        healthy = None
        notes.append("could not read cron state (jobs.json + hermes CLI both "
                     "unavailable) — assuming the daily reminder cron is intact")
    else:
        healthy = found and state == "active" and not run_problem
        if found and state == "active" and run_problem:
            notes.append(run_problem)
        if not found:
            notes.append("cron job not found — reminders cannot fire")
        elif state == "paused":
            notes.append(f"cron is paused — v3 expects it always active; run "
                         f"`hermes cron resume {job_id or CRON_JOB_ID}`")
        elif state != "active":
            notes.append(f"unexpected cron state: {state!r}")

    return {
        "found": found,
        "job_id": job_id,
        "state": state,
        "schedule": schedule,
        "next_run": next_run,
        "last_run": last_run,
        "expected_paused": expected_paused,
        "source": source,
        "determinable": determinable,
        "healthy": healthy,
        "notes": notes,
    }


def next_reminders_for(card_suffix: str | None = None) -> list[dict]:
    """LEGACY (exact-day dates, no catch-up windows) — only fills
    process_pdf()["next_reminders"], which no script shows any more; the user-facing
    confirmation uses reminder.upcoming(). Compute *upcoming* reminder events
    (fire_date >= today) for the given card or all cards based on current DB state. Past events are filtered out
    so callers don't surface stale reminders.

    Returns a list of dicts:
        {"card": "5999", "kind": "3day"|"due", "fire_date": date,
         "year_month": "2026-05", "due_date": "22-05-2026",
         "already_fired": bool}
    """
    from datetime import date, datetime as _dt, timedelta as _td

    today = date.today()

    conn = get_db()
    try:
        if card_suffix:
            cur = conn.execute(
                "SELECT s.card_suffix, s.year_month, s.due_date FROM statements s "
                "JOIN (SELECT card_suffix, MAX(year_month) ym FROM statements GROUP BY card_suffix) l "
                "ON s.card_suffix=l.card_suffix AND s.year_month=l.ym WHERE s.card_suffix=?",
                (card_suffix,),
            )
        else:
            cur = conn.execute(
                "SELECT s.card_suffix, s.year_month, s.due_date FROM statements s "
                "JOIN (SELECT card_suffix, MAX(year_month) ym FROM statements GROUP BY card_suffix) l "
                "ON s.card_suffix=l.card_suffix AND s.year_month=l.ym"
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    events = []
    for r in rows:
        try:
            due = _dt.strptime(r["due_date"], "%d-%m-%Y").date()
        except (ValueError, TypeError):
            continue
        three_day_marker = get_config(f"reminded_3day_{r['card_suffix']}")
        due_marker = get_config(f"reminders_fired_{r['card_suffix']}")
        for kind, fire_date, marker in (
            ("3day", due - _td(days=3), three_day_marker),
            ("due", due, due_marker),
        ):
            if fire_date < today:
                continue
            events.append({
                "card": r["card_suffix"],
                "kind": kind,
                "fire_date": fire_date,
                "year_month": r["year_month"],
                "due_date": r["due_date"],
                "already_fired": marker == r["year_month"],
            })
    events.sort(key=lambda e: (e["fire_date"], e["card"], e["kind"]))
    return events


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _write_statement(cur, data: dict) -> None:
    """Insert statement + transactions + merchants for one parsed statement.

    Caller owns the connection / transaction.
    """
    card_suffix = data["card_suffix"]
    year_month = data["year_month"]

    cur.execute(
        """
        INSERT OR REPLACE INTO statements
            (card_suffix, year_month, statement_date, due_date, total_due, minimum_due)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            card_suffix,
            year_month,
            data["statement_date"],
            data["due_date"],
            data["total_due"],
            data["minimum_due"],
        ),
    )

    # row_num tiebreaker keyed on NORMALIZED merchant — see SKILL.md.
    seen = {}
    row_nums = []
    normalized = []
    for txn in data["transactions"]:
        norm = normalize_merchant(txn["merchant_raw"], card_suffix)
        normalized.append(norm)
        key = (txn["txn_date"], norm, txn["amount"])
        row_nums.append(seen.get(key, 0))
        seen[key] = seen.get(key, 0) + 1

    for i, txn in enumerate(data["transactions"]):
        txn_date = parse_date(txn["txn_date"], year_month)
        merchant_norm = normalized[i]
        row_num = row_nums[i]

        cur.execute(
            """
            INSERT INTO transactions
                (card_suffix, year_month, txn_date, merchant_raw, merchant_normalized,
                 country_code, amount, row_num)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_suffix,
                year_month,
                txn_date,
                txn["merchant_raw"],
                merchant_norm,
                txn["country_code"],
                txn["amount"],
                row_num,
            ),
        )

        # Idempotent merchants UPSERT — counts/totals recalculated from
        # `transactions`; `approved` deliberately omitted so it survives re-imports.
        cur.execute(
            """
            INSERT INTO merchants
                (card_suffix, merchant_normalized, first_seen, last_seen,
                 txn_count, total_spent, approved)
            VALUES (?, ?, ?, ?, 1, ?, 0)
            ON CONFLICT(card_suffix, merchant_normalized) DO UPDATE SET
                txn_count = (
                    SELECT COUNT(*) FROM transactions
                    WHERE card_suffix=merchants.card_suffix
                      AND merchant_normalized=merchants.merchant_normalized
                ),
                total_spent = (
                    SELECT COALESCE(SUM(amount), 0) FROM transactions
                    WHERE card_suffix=merchants.card_suffix
                      AND merchant_normalized=merchants.merchant_normalized
                ),
                first_seen = (
                    SELECT MIN(txn_date) FROM transactions
                    WHERE card_suffix=merchants.card_suffix
                      AND merchant_normalized=merchants.merchant_normalized
                ),
                last_seen = (
                    SELECT MAX(txn_date) FROM transactions
                    WHERE card_suffix=merchants.card_suffix
                      AND merchant_normalized=merchants.merchant_normalized
                )
            """,
            (card_suffix, merchant_norm, txn_date, txn_date, txn["amount"]),
        )

    # Assign stable ids to any merchants first seen in this statement.
    _assign_merchant_uids(cur.connection)


def _refresh_merchant_aggregates(cur, card_suffix: str, names) -> None:
    for name in names:
        cur.execute(
            """
            UPDATE merchants SET
                txn_count = (SELECT COUNT(*) FROM transactions
                             WHERE card_suffix=? AND merchant_normalized=?),
                total_spent = (SELECT COALESCE(SUM(amount), 0) FROM transactions
                               WHERE card_suffix=? AND merchant_normalized=?),
                first_seen = COALESCE((SELECT MIN(txn_date) FROM transactions
                             WHERE card_suffix=? AND merchant_normalized=?), first_seen),
                last_seen = COALESCE((SELECT MAX(txn_date) FROM transactions
                            WHERE card_suffix=? AND merchant_normalized=?), last_seen)
            WHERE card_suffix=? AND merchant_normalized=?
            """,
            (card_suffix, name) * 5,
        )


def reconcile_warning(cur, data: dict) -> str | None:
    """Completeness check: previous statement's total + every transaction on this
    one == this statement's total due (the payment of last month's balance is
    one of the credits). Held to the cent on all 16 checkable real statements,
    so a mismatch means the parser missed or mis-read a transaction."""
    card, ym = data["card_suffix"], data["year_month"]
    try:
        y, m = int(ym[:4]), int(ym[5:7])
    except ValueError:
        return None
    prev_ym = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    row = cur.execute("SELECT total_due FROM statements WHERE card_suffix=? AND year_month=?",
                      (card, prev_ym)).fetchone()
    if not row or row[0] is None:
        return None
    expected = round(row[0] + sum(t["amount"] for t in data["transactions"]), 2)
    gap = round(data["total_due"] - expected, 2)
    if abs(gap) < 0.01:
        return None
    return (f"transactions don't add up: last statement S${row[0]:,.2f} + this "
            f"statement's transactions = S${expected:,.2f}, but TOTAL AMOUNT DUE is "
            f"S${data['total_due']:,.2f} (off by S${gap:,.2f}) — the parser may have "
            f"missed or mis-read a transaction; check the PDF")


def store_statement(data: dict) -> None:
    """Public entrypoint: persist a parsed statement in its own transaction."""
    conn = get_db()
    try:
        with conn:
            _write_statement(conn.cursor(), data)
    finally:
        conn.close()


def process_pdf(
    pdf_path: str,
    card_suffix: str | None = None,
    year_month: str | None = None,
) -> dict:
    """Parse a PDF and persist its data atomically.

    `card_suffix` and `year_month` are optional; if omitted (or `None`) they
    are detected from the PDF body. If both are provided AND the PDF body
    disagrees with what was passed, this raises `ParseError` — preventing a
    misrouted file (wrong filename → wrong card) from silently corrupting
    data for the wrong card.

    Parsing happens before any DB writes. If parsing fails (ParseError,
    encrypted PDF, scanned image, wrong document type), the existing rows
    for (card_suffix, year_month) are untouched. Writes run inside a single
    `with conn:` transaction so a write failure leaves the DB consistent.

    Every call records a row in `imports` (status = in_progress → ok |
    parse_error | write_error) so the bot can audit success/failure
    without scraping stdout.
    """
    log.info("import.start pdf=%s card=%s ym=%s", pdf_path, card_suffix, year_month)
    init_db()  # ensure schema/migrations are current — needed for `imports` table

    # Open audit row first so failures are recorded even if parse blows up.
    # card/year_month here may be None — _record_import_finish back-fills them.
    conn = get_db()
    try:
        with conn:
            import_id = _record_import_start(
                conn.cursor(), pdf_path, card_suffix, year_month
            )
    finally:
        conn.close()

    def _finalise(**kwargs):
        conn2 = get_db()
        try:
            with conn2:
                _record_import_finish(conn2.cursor(), import_id, **kwargs)
        finally:
            conn2.close()

    try:
        text = read_pdf_text(pdf_path)  # raises ParseError on bad PDF
        detected_card, detected_ym = detect_card_and_period(text)

        # Verify or fill in
        if card_suffix is None:
            card_suffix = detected_card
        elif detected_card and detected_card != card_suffix:
            raise ParseError(
                f"PDF body says card {detected_card}, but caller passed {card_suffix}. "
                "Refusing to import — fix the routing."
            )
        if year_month is None:
            year_month = detected_ym
        elif detected_ym and detected_ym != year_month:
            raise ParseError(
                f"PDF body says period {detected_ym}, but caller passed {year_month}. "
                "Refusing to import — fix the routing."
            )
        if not card_suffix or not year_month:
            raise ParseError(
                f"could not determine card_suffix ({card_suffix!r}) or year_month "
                f"({year_month!r}) from PDF body or arguments"
            )

        data = parse_statement(text, card_suffix, year_month)
        validate_parsed_data(data)  # may raise ParseError; sets data['warnings']
    except ParseError as exc:
        log.error("import.parse_error card=%s ym=%s err=%s", card_suffix, year_month, exc)
        _finalise(
            status="parse_error",
            card_suffix=card_suffix,
            year_month=year_month,
            error_message=str(exc),
        )
        raise
    except BaseException as exc:  # incl. KeyboardInterrupt — never leave 'in_progress'
        log.exception("import.read_error card=%s ym=%s", card_suffix, year_month)
        _finalise(
            status="parse_error",
            card_suffix=card_suffix,
            year_month=year_month,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise

    conn = get_db()
    try:
        w = reconcile_warning(conn.cursor(), data)
        if w:
            data.setdefault("warnings", []).append(w)
    except sqlite3.Error:
        pass
    finally:
        conn.close()

    conn = get_db()
    try:
        with conn:
            cur = conn.cursor()
            # Is this a genuinely NEW cycle for the card, or a re-import of an
            # existing/older statement? Decide BEFORE the deletes.
            prev_latest = cur.execute(
                "SELECT MAX(year_month) FROM statements WHERE card_suffix=?",
                (card_suffix,),
            ).fetchone()[0]
            is_new_cycle = prev_latest is None or year_month > prev_latest
            removed = [r[0] for r in cur.execute(
                "SELECT DISTINCT merchant_normalized FROM transactions "
                "WHERE card_suffix=? AND year_month=?", (card_suffix, year_month))]
            was_paused = cur.execute(
                "SELECT value FROM config WHERE key='reminders_disabled'").fetchone()
            cur.execute(
                "DELETE FROM transactions WHERE card_suffix=? AND year_month=?",
                (card_suffix, year_month),
            )
            cur.execute(
                "DELETE FROM statements WHERE card_suffix=? AND year_month=?",
                (card_suffix, year_month),
            )
            # Clear the per-card reminder markers ONLY for a new cycle. Clearing
            # them on a same-month re-import (the user re-uploading to check it
            # went through) would make an already-fired due-date reminder fire
            # AGAIN — a duplicate ping. `due_messages` keys idempotency on the
            # marker's stored year_month, so a new cycle fires correctly whether
            # or not we clear; only the re-import case needs the guard.
            if is_new_cycle and was_paused and str(was_paused[0]) == "1":
                # remembered per (card, cycle), written in THIS transaction, so a re-run
                # of the same import renders the same notice (identical review body)
                cur.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) "
                            "DO UPDATE SET value = excluded.value",
                            (f"reminders_resumed_{card_suffix}", year_month))
            if is_new_cycle:
                cur.execute(
                    "DELETE FROM config WHERE key IN (?, ?)",
                    (f"reminders_fired_{card_suffix}", f"reminded_3day_{card_suffix}"),
                )
                cur.execute(
                    "INSERT INTO config (key, value) VALUES ('reminders_disabled', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value = '0'"
                )
            _write_statement(cur, data)
            # A merchant that lost its rows in this re-import (e.g. after a
            # canonical-map change) must not keep stale counts — with 0 rows it
            # drops out of the backlog lists (reviewlib filters txn_count > 0).
            _refresh_merchant_aggregates(cur, card_suffix, removed)
            row = cur.execute("SELECT value FROM config WHERE key=?",
                              (f"reminders_resumed_{card_suffix}",)).fetchone()
            paused_now = cur.execute("SELECT value FROM config WHERE "
                                     "key='reminders_disabled'").fetchone()
            resumed = bool(row and row[0] == year_month
                           and (prev_latest is None or year_month >= prev_latest)
                           and not (paused_now and str(paused_now[0]) == "1"))
    except BaseException as exc:
        log.exception("import.write_error card=%s ym=%s", card_suffix, year_month)
        _finalise(
            status="write_error",
            card_suffix=card_suffix,
            year_month=year_month,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        conn.close()

    data["reminders_resumed"] = resumed
    _finalise(
        status="ok",
        card_suffix=card_suffix,
        year_month=year_month,
        txn_count=len(data["transactions"]),
        total_due=data["total_due"],
        warnings=data.get("warnings"),
    )
    log.info(
        "import.ok card=%s ym=%s txns=%d total=%.2f warnings=%d",
        card_suffix, year_month, len(data["transactions"]), data["total_due"],
        len(data.get("warnings") or []),
    )

    # Enrich the returned dict with audit info, next-reminder schedule, and
    # cron health snapshot. Existing keys (transactions, total_due, etc.) are
    # preserved so existing callers keep working.
    data["ok"] = True
    data["next_reminders"] = [
        {**ev, "fire_date": ev["fire_date"].isoformat()}
        for ev in next_reminders_for(card_suffix)
    ]
    data["cron"] = verify_cron_health()
    return data


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)
