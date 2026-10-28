#!/usr/bin/env python3
"""
Food Expense Sync (multi-provider: FoodPanda + Grab)
- Searches Gmail for receipt emails from each configured provider
- Parses Date, Merchant, Order Total, Order ID (provider-specific regexes)
- Deduplicates against Google Sheet (by Gmail message id, order id, or content fallback)
- If an existing row shares the same (provider, order id), updates its amount to the latest receipt
- Creates new month sheet if needed (Provider column + the MTD =SUM formula in G2)
- Appends new rows or updates existing rows in the correct sheet
- Sends a combined Discord summary if new receipts found
"""

import json, os, re, sys, html, email.utils, subprocess, urllib.request, urllib.error, time, socket
from datetime import datetime, timezone, timedelta

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SKILLS_DIR  = os.path.join(HERMES_HOME, "skills", "productivity", "google-workspace", "scripts")

SHEET_ID           = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"
DISCORD_CHANNEL_ID = "1499972245437550602"
STATE_FILE = os.path.join(HERMES_HOME, "foodpanda_sync_state.json")
SGT                = timezone(timedelta(hours=8))

# ── Timeout budget. The cron scheduler SIGKILLs a no_agent script at the GLOBAL
# `cron.script_timeout_seconds` (600s in ~/.hermes/config.yaml; 120s if that key is ever
# removed) with NO Discord alert, so every blocking call must be bounded and the retry
# envelope must leave headroom for the alert posts. CALL_TIMEOUT bounds each individual API
# call; RUN_DEADLINE_SECS caps the *cumulative* API/retry time across the run. Discord posts
# are NOT gated by the deadline (they have their own small bound) so a slow run still alerts. ──
CALL_TIMEOUT      = 30    # seconds per subprocess/HTTP call. A cold `google_api.py` spawn +
                          # OAuth + the API call is ~1-2s for a narrow search but up to ~25s
                          # for a wide first-run backfill (50-msg hydration) — 30s covers both.
RUN_DEADLINE_SECS = 300   # no new API call/sleep past this. Invariant: RUN_DEADLINE + CALL_TIMEOUT
                          # + the Discord posts after it (single-shot ≤15s each; the month loop
                          # stops at the deadline with ONE alert) < the 600s watchdog
socket.setdefaulttimeout(CALL_TIMEOUT)  # bounds in-process googleapiclient/httplib2 sockets
_run_deadline = None      # set at the start of main(); a monotonic-clock cutoff for retries

# Locale-independent month abbreviations (sheet tab names must not depend on the
# system locale, otherwise dedup/lookups silently miss when run under a non-en locale).
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

MAX_MERCHANT_LEN = 200    # a longer capture means the merchant regex overran its end label

# ── Provider config ──────────────────────────────────────────────────────────
# Each provider drives one Gmail search + one set of parse regexes. Add a provider
# by appending a dict here; the rest of the flow (dedup, sheet, Discord) is generic.
#   date_mode "field"  → date_re captures a YYYY-MM-DD string directly (FoodPanda).
#   date_mode "pickup" → date_re captures 'DD Mon YY' (e.g. '07 Jul 26'), parsed
#                        locale-independently via MONTHS (Grab has no ISO order date;
#                        we log it under the receipt's Pickup/Delivery time date).
PROVIDERS = [
    {
        "name":        "FoodPanda",
        "sender":      "no-reply@foodpanda.sg",
        "subject":     "Thank you for your order!",
        "lookback":    "2026/05/01",
        "date_mode":   "field",
        "date_re":     r"Order date:\s*(\d{4}-\d{2}-\d{2})",
        "merchant_re": r"Name:\s*(.+?)(?:Partner address|$)",
        # non-greedy to the first S$; tolerates an intervening 'S' ('Service'/'Singapore').
        # Don't revert to [^S]*.
        "total_re":    r"Order total.*?S\$\s*([\d,]+\.?\d*)",
        "order_id_re": r"Order number:\s*(\S+)",
    },
    {
        "name":        "Grab",
        "sender":      "no-reply@grab.com",
        # The sender also emails promos ('free trial', etc.) — the subject filter is
        # what restricts the search to actual e-receipts.
        "subject":     "Your Grab E-Receipt",
        "lookback":    "2026/05/01",
        "date_mode":   "pickup",
        "date_re":     r"(?:Pickup|Delivery) time:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{2})(?!\d)",
        "merchant_re": r"Order from:\s*(.+?)(?:Delivery location|Profile:|$)",
        # Anchor on 'TOTAL (INCL. TAX)' specifically — the receipt also has a bare
        # 'TOTAL S$ ...' near the top and a 'Subtotal'. This picks the tax-inclusive total.
        "total_re":    r"TOTAL \(INCL\. TAX\)\s*S\$\s*([\d,]+\.?\d*)",
        # Group-order receipts have no '(INCL. TAX)' line — only a bare 'TOTAL S$ ...' (which
        # for a standard receipt equals the tax-inclusive total anyway). Case-sensitive TOTAL
        # avoids matching 'Subtotal'. Used only when total_re misses.
        "total_re_fallback": r"TOTAL\s*S\$\s*([\d,]+\.?\d*)",
        # 'Booking code A-9IBAAN9WWE6DAV' — no colon after the label.
        "order_id_re": r"Booking code\s+(\S+)",
        # Grab TAXI rides share the 'Your Grab E-Receipt' subject ('Hope you enjoyed your ride!
        # … Booking ID: … Total Paid S$13.30'). They aren't food: skipped silently, but only when
        # the food merchant label is ALSO absent, so a food receipt can never be skipped this way.
        "nonfood_re":  r"Hope you enjoyed your ride",
    },
]

# Load Discord token from .env file (not exported, so direct read required).
DISCORD_BOT_TOKEN = ""
ENV_FILE = os.path.join(HERMES_HOME, ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN=") and not line.startswith("DISCORD_BOT_TOKEN=#"):
                DISCORD_BOT_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")

# ── Helpers ────────────────────────────────────────────────────────────────────

def retry(fn, max_attempts=3, backoff=5):
    """Retry a no-argument function on transient HTTP/API errors with exponential backoff.
    A run-global deadline (`_run_deadline`) bounds cumulative time: no new attempt STARTS and
    no backoff sleep CROSSES the deadline, so the worst case (one in-flight call bounded by
    CALL_TIMEOUT) plus the crash-alert post stays under the cron SIGKILL watchdog."""
    for attempt in range(max_attempts):
        if _run_deadline is not None and time.monotonic() >= _run_deadline:
            raise TimeoutError("run deadline exceeded — aborting to stay under the cron watchdog")
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            err_str = str(e).lower()
            if any(k in err_str for k in ["429", "503", "ratelimit", "timeout", "transient", "timed out"]):
                if _run_deadline is not None and time.monotonic() + backoff >= _run_deadline:
                    raise  # a backoff sleep would cross the deadline — fail now
                print(f"Transient error, retrying in {backoff}s: {e}")
                time.sleep(backoff)
                backoff *= 2
            else:
                raise

def run_gapi(args):
    """Run google_api.py and return parsed JSON. The returncode check lives INSIDE the
    retried call so transient API failures (429/503 in stderr) are actually retried. The
    subprocess is bounded by CALL_TIMEOUT so a hung google_api.py raises TimeoutExpired
    (which retry() treats as transient) instead of blocking until the cron SIGKILL."""
    # sys.executable (the gateway venv under cron), never a bare "python" that resolves via PATH.
    cmd = [sys.executable, f"{SKILLS_DIR}/google_api.py"] + args
    def call():
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"gapi failed: {result.stderr}")
        # google_api.py's non-gws branch prints plain text (not JSON) for an empty search.
        if result.stdout.strip() == "No messages found.":
            return []
        return json.loads(result.stdout)
    return retry(call)

def load_state():
    """Return {'providers': {name: {'last_checked_email_ts': iso}}}.
    Migrates the legacy flat schema {'last_checked_email_ts': ...} → FoodPanda's watermark."""
    s = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
    if "providers" not in s:
        legacy = s.get("last_checked_email_ts")
        s = {"providers": {}}
        if legacy:
            s["providers"]["FoodPanda"] = {"last_checked_email_ts": legacy}
    return s

def provider_last_ts(state, name):
    return state["providers"].get(name, {}).get("last_checked_email_ts")

def save_state(state):
    """Atomic write: write to temp file then rename to target."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.rename(tmp, STATE_FILE)

def parse_email_date(date_str):
    """Parse receipt email Date header: 'Sat, 2 May 2026 04:25:07 +0000' — plus the RFC 5322
    variants ('… +0000 (UTC)', 'GMT', no weekday/seconds) via email.utils, so a slightly
    different header doesn't turn into an unreadable-date skip. Raises on an empty/garbage one."""
    try:
        dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        dt = email.utils.parsedate_to_datetime(date_str)   # ValueError/TypeError if unparseable
        if dt.tzinfo is None:
            raise ValueError(f"no timezone in Date header {date_str!r}")
    return dt.astimezone(SGT)

def _parse_pickup_date(s):
    """'07 Jul 26' → '2026-07-07' (locale-independent via MONTHS). None if unparseable or not a
    real calendar date (so a malformed pickup line degrades to a parse-failure rather than
    building an invalid 'YYYY-MM-DD' that would later crash sheet_month_name)."""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})", s.strip())
    if not m:
        return None
    dd, mon, yy = m.group(1), m.group(2), m.group(3)
    if mon not in MONTHS:
        return None
    try:
        return datetime(2000 + int(yy), MONTHS.index(mon) + 1, int(dd)).strftime("%Y-%m-%d")
    except ValueError:
        return None

def extract_expenses(body_html, provider):
    """Parse a receipt HTML body using the provider's regexes.
    Returns one of:
      ("ok", (date, merchant, total, order_id))
      ("foreign", None)  — a non-SGD receipt (e.g. Grab abroad, 'TOTAL Rp ...'); it cannot be
                           summed into the S$ MTD, so it is skipped cleanly (not an error).
      ("nonfood", None)  — a non-food receipt with the same subject (a Grab ride); skipped quietly.
      ("fail", None)     — a genuine parse failure on an S$ receipt (likely a format change)."""
    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"\s+", " ", text)
    text = html.unescape(text)

    # A receipt with no 'S$' anywhere is denominated in another currency (e.g. a Grab order
    # placed abroad, all-Indonesian layout with 'TOTAL Rp ...'). It can't be summed into the
    # S$ MTD, so skip it cleanly up front — before the field regexes, which are S$/English-only
    # and would otherwise fail and misreport it as a parse error.
    # A non-food receipt sharing the provider's subject (a Grab ride — incl. one abroad): skip it
    # quietly. Checked before the currency test so an overseas ride isn't noted as 'non-SGD'.
    if (provider.get("nonfood_re") and re.search(provider["nonfood_re"], text)
            and not re.search(provider["merchant_re"], text)):
        return ("nonfood", None)

    if "S$" not in text:
        return ("foreign", None)

    date_match = re.search(provider["date_re"], text)
    if not date_match:
        return ("fail", None)
    if provider["date_mode"] == "pickup":
        order_date = _parse_pickup_date(date_match.group(1))
        if not order_date:
            return ("fail", None)
    else:
        order_date = date_match.group(1)
        try:  # an impossible date (2026-09-31) would crash sheet_month_name → every run
            datetime.strptime(order_date, "%Y-%m-%d")
        except ValueError:
            return ("fail", None)

    merchant_match = re.search(provider["merchant_re"], text)
    if not merchant_match:
        return ("fail", None)
    merchant = merchant_match.group(1).strip()
    if len(merchant) > MAX_MERCHANT_LEN:
        # The end-label the regex stops at was missing, so it ran to the end of the email.
        return ("fail", None)

    total_match = re.search(provider["total_re"], text)
    if not total_match and provider.get("total_re_fallback"):
        total_match = re.search(provider["total_re_fallback"], text)
    if not total_match:
        # 'S$' is present (checked at the top) but no total matched → a genuine parse failure
        # on an S$ receipt, likely a format change worth surfacing.
        return ("fail", None)
    total_str = total_match.group(1).replace(",", "")
    try:
        total = round(float(total_str), 2)
    except ValueError:
        return ("fail", None)

    # Extract order id for dedup/update across receipt variants (e.g. refund)
    order_id = ""
    if provider.get("order_id_re"):
        order_id_match = re.search(provider["order_id_re"], text)
        if order_id_match:
            order_id = order_id_match.group(1).strip()

    return ("ok", (order_date, merchant, total, order_id))

def sheet_month_name(date_str):
    """Convert '2026-05-02' → 'May 2026' (locale-independent)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{MONTHS[dt.month - 1]} {dt.year}"

def norm(amount):
    """Normalize an amount to a consistent 2-decimal-place string for dedup comparison.
    Currency-tolerant (via parse_money) so a legacy currency-formatted cell ('$30.32') that
    reaches the content-fallback dedup doesn't crash the whole sync on float('$30.32')."""
    return f"{parse_money(amount) or 0.0:.2f}"

def parse_money(x):
    """Lenient numeric parse tolerant of currency formatting ('$30.32', 'S$ 1,037.17').
    Returns a float, or None if there's nothing numeric."""
    s = re.sub(r"[^\d.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None

def san(s):
    """Neutralize Discord markdown in an interpolated untrusted field (a merchant name comes
    from receipt HTML the provider controls). Strips newlines, defangs backticks, mentions,
    and masked-link / bare-autolink syntax so a crafted name can't break formatting or inject
    a clickable link into the channel."""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return (s.replace("`", "'").replace("@", "@​")
             .replace("](", "]​(").replace("://", ":​//"))

_sheets_service = None

def _fix_token_expiry(tok):
    """Convert float Unix-timestamp expiry → ISO string for google-auth compat."""
    expiry = tok.get("expiry")
    if isinstance(expiry, (int, float)):
        from datetime import datetime, timezone
        tok["expiry"] = datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_sheets_service():
    """Reuse a single Sheets API service instance across all calls."""
    global _sheets_service
    if _sheets_service is None:
        import googleapiclient.discovery, google.oauth2.credentials
        token_data = json.loads(open(os.path.join(HERMES_HOME, "google_token.json")).read())
        _fix_token_expiry(token_data)
        creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
            token_data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _sheets_service = googleapiclient.discovery.build("sheets", "v4", credentials=creds)
    return _sheets_service

def get_all_sheets():
    """Return list of sheet tab titles via Sheets API (with retry for transient failures)."""
    service = get_sheets_service()
    result = retry(lambda: service.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets.properties.title"
    ).execute())
    return [s["properties"]["title"] for s in result.get("sheets", [])]

def read_rows(sheet_name):
    """Read data rows (skipping header) from a sheet as
    (sheet_row, date, merchant, amount, email_id, order_id, provider), where sheet_row is the
    real 1-based row number (blank/spacer rows are skipped, so indices are NOT contiguous).
    Raises on read failure — callers must treat a failed read of an EXISTING sheet as a
    hard error, never as 'empty' (otherwise we'd append duplicates)."""
    data = run_gapi(["sheets", "get", SHEET_ID, f"'{sheet_name}'!A:F"])
    rows = []
    for idx, row in enumerate(data):
        sheet_row = idx + 1
        if sheet_row == 1:
            continue  # header
        if len(row) >= 3 and row[0] and row[1] and row[2]:
            email_id = row[3].strip() if len(row) >= 4 and row[3] else ""
            order_id = row[4].strip() if len(row) >= 5 and row[4] else ""
            provider = row[5].strip() if len(row) >= 6 and row[5] else ""
            rows.append((sheet_row, row[0].strip(), row[1].strip(), row[2].strip(),
                         email_id, order_id, provider))
    return rows

def get_existing_rows(sheet_name):
    return read_rows(sheet_name)

def get_last_rows(sheet_name, n):
    """Fetch the last n data rows from a sheet (skipping header)."""
    rows = read_rows(sheet_name)
    return rows[-n:] if len(rows) >= n else rows

def next_data_row(sheet_name):
    """First free row for appending, computed from column A only (the header + receipt dates).
    Ignoring the MTD summary in G avoids the append-into-wrong-column bug that the Sheets
    `values.append` table-detection hits on legacy tabs with empty D/E columns."""
    col = run_gapi(["sheets", "get", SHEET_ID, f"'{sheet_name}'!A:A"])
    return len(col) + 1

def create_month_sheet(month_name):
    service = get_sheets_service()
    body = {
        "requests": [{
            "addSheet": {
                "properties": {
                    "title": month_name,
                    "index": 0
                }
            }
        }]
    }
    retry(lambda: service.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body=body).execute())
    # Header row (Order ID for refund dedup + Provider for multi-provider rows). Written with
    # an explicit values.update (not append) so it lands in A1:F1 deterministically.
    retry(lambda: service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{month_name}'!A1:F1",
        valueInputOption="USER_ENTERED",
        body={"values": [["Date", "Merchant", "Amount", "Email ID", "Order ID", "Provider"]]}
    ).execute())
    # Seed the MTD total formula in G2 (label in G1). USER_ENTERED so =SUM is interpreted,
    # not stored as text. G (not F) because F now holds Provider.
    retry(lambda: service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{month_name}'!G1:G2",
        valueInputOption="USER_ENTERED",
        body={"values": [["MTD Total"], ["=SUM(C2:C)"]]}
    ).execute())

def read_all_tabs(sheet_names):
    """{tab: its A1:G values} for every tab in ONE in-process values.batchGet — replaces the two
    google_api.py spawns per tab (~0.9s each) the migration checks used to cost on every run,
    a cost that grew by a tab a month. Same values/rendering as `sheets get` (values().get)."""
    if not sheet_names:
        return {}
    service = get_sheets_service()
    res = retry(lambda: service.spreadsheets().values().batchGet(
        spreadsheetId=SHEET_ID,
        ranges=[f"'{n.replace(chr(39), chr(39) * 2)}'!A1:G" for n in sheet_names]  # ' → '' 
    ).execute())
    vrs = res.get("valueRanges", [])
    if len(vrs) != len(sheet_names):
        raise RuntimeError(f"batchGet returned {len(vrs)} ranges for {len(sheet_names)} tabs")
    return {n: vr.get("values", []) for n, vr in zip(sheet_names, vrs)}

def ensure_order_id_column(sheet_name, data):
    """Add Order ID header (column E) to existing sheets that don't have it yet. `data` = the
    tab's A1:G values (from read_all_tabs)."""
    if data and data[0] and len(data[0]) >= 5 and data[0][4] == "Order ID":
        return  # already has it
    service = get_sheets_service()
    try:
        retry(lambda: service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!E1",
            valueInputOption="USER_ENTERED",
            body={"values": [["Order ID"]]}
        ).execute())
        print(f"  Migrated: added Order ID column to '{sheet_name}'")
        # Reflect the write in `data` so ensure_provider_column (same snapshot) sees E1 — as the
        # old per-tab re-read did (matters for an empty/new tab: it then gets F1 + the G MTD too).
        if not data:
            data.append([])
        data[0].extend([""] * (5 - len(data[0])))
        data[0][4] = "Order ID"
    except Exception as e:
        print(f"  Warning: could not add Order ID column to '{sheet_name}': {e}")

def ensure_provider_column(sheet_name, data):
    """Bring a sheet to the multi-provider layout and keep it healthy: Provider header + value
    in column F, MTD =SUM relocated to column G. Self-healing and idempotent — it computes the
    desired F column ROW-BY-ROW (every real data row → its existing provider, or 'FoodPanda'
    for legacy rows; blank/spacer rows → empty) and only writes when something differs, so it
    also repairs an earlier off-by-one backfill or a stray value in F. Never relabels a row
    that already reads 'FoodPanda'/'Grab'. `data` = the tab's A1:G values (from read_all_tabs)."""
    if not data:
        return

    header = data[0] if data else []
    # Desired F column, row-aligned to the sheet.
    desired_f, current_f = [], []
    for idx, row in enumerate(data):
        cur = row[5].strip() if len(row) >= 6 and row[5] else ""
        current_f.append(cur)
        if idx == 0:
            desired_f.append("Provider")
            continue
        a = row[0].strip() if len(row) >= 1 and row[0] else ""
        b = row[1].strip() if len(row) >= 2 and row[1] else ""
        c = row[2].strip() if len(row) >= 3 and row[2] else ""
        if a and b and c:                       # a real data row
            desired_f.append(cur if cur in ("FoodPanda", "Grab") else "FoodPanda")
        else:                                    # blank/spacer row (e.g. the MTD row)
            desired_f.append("")

    service = get_sheets_service()
    wrote = False
    if desired_f != current_f:
        retry(lambda: service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!F1:F{len(desired_f)}",
            valueInputOption="USER_ENTERED",
            body={"values": [[v] for v in desired_f]}
        ).execute())
        wrote = True
    # Relocate/ensure the MTD summary in G. Formula is deterministic; only write it when the
    # G1 label is missing so we don't clobber a live formula every run.
    if not (len(header) >= 7 and header[6] == "MTD Total"):
        retry(lambda: service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!G1:G2",
            valueInputOption="USER_ENTERED",
            body={"values": [["MTD Total"], ["=SUM(C2:C)"]]}
        ).execute())
        wrote = True
    if wrote:
        n_data = sum(1 for v in desired_f[1:] if v)
        print(f"  Migrated/healed '{sheet_name}': Provider column + MTD in G "
              f"({n_data} data row(s))")

def append_rows(sheet_name, rows):
    """Append rows deterministically via an explicit values.update at the first free row
    (computed from column A), rather than the Sheets `values.append` table-detection which
    misaligns columns on legacy tabs. rows = [[date, merchant, amount, email_id, order_id,
    provider], ...]. Returns the 1-based sheet row of the first appended row."""
    if not rows:
        return None
    base = next_data_row(sheet_name)
    service = get_sheets_service()
    retry(lambda: service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!A{base}:F{base + len(rows) - 1}",
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute())
    return base

def update_sheet_row(sheet_name, row_num, values):
    """Update a specific data row (row_num is the real 1-based sheet row). Writes A:F only,
    never touching the MTD summary in column G."""
    service = get_sheets_service()
    range_name = f"'{sheet_name}'!A{row_num}:F{row_num}"
    retry(lambda: service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body={"values": [values]}
    ).execute())

def get_mtd_total(sheet_name):
    try:
        result = run_gapi(["sheets", "get", SHEET_ID, f"'{sheet_name}'!G2"])
        if result and result[0] and result[0][0]:
            v = parse_money(result[0][0])
            if v is not None:
                return v
    except Exception:
        pass
    # Fallback: sum column C manually in case the G2 formula hasn't been evaluated (or the
    # tab hasn't been migrated to the Provider layout yet). Tolerant of currency formatting.
    try:
        data = run_gapi(["sheets", "get", SHEET_ID, f"'{sheet_name}'!C:C"])
        total = sum(parse_money(row[0]) or 0.0 for row in data[1:] if row and row[0])
        return round(total, 2)
    except Exception:
        return None

def _past_deadline():
    return _run_deadline is not None and time.monotonic() >= _run_deadline

def discord_send(channel_id, payload, timeout=15, attempts=3, retry_ambiguous=False):
    """POST one Discord message. Retries ONLY when Discord definitely did not create it — HTTP
    429 (honours retry_after, capped at 10s) / 503, or a URLError raised while connecting/sending.
    A read TIMEOUT after the request went out is NOT retried (the message may already exist and
    a re-send would duplicate it), nor is a 500/502/504 — unless `retry_ambiguous` (only for a
    payload carrying a Discord `nonce` + `enforce_nonce`, which dedupes the re-send). Posts still go out after the run
    deadline — so a slow run can still alert — but then single-shot (≤ `timeout` each), which
    keeps the post-deadline tail bounded; before it, worst case ~51s per post."""
    data = json.dumps(payload).encode()
    delay = 2
    for attempt in range(attempts):
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=data,
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (hermes-agent, 1.0)"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:   # (subclass of URLError — must come first)
            # 429/503 mean "not processed"; a 500/502/504 may still have created the message.
            retryable = e.code in (429, 503) or (retry_ambiguous and 500 <= e.code < 600)
            if attempt == attempts - 1 or not retryable or _past_deadline():
                raise
            wait = delay
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", delay))
                except Exception:
                    pass
            print(f"Discord HTTP {e.code}, retrying in {min(wait, 10):.1f}s")
            time.sleep(min(wait, 10) + 0.25)
            delay *= 2
        except urllib.error.URLError as e:     # connect/send failed — nothing was created
            if attempt == attempts - 1 or _past_deadline():
                raise
            print(f"Discord connection error, retrying in {delay}s: {e}")
            time.sleep(delay)
            delay *= 2

def discord_embed(channel_id, title, description, color=0xD70F64):
    """Post a Discord embed (description capped to the 4096-char embed limit)."""
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN not set, skipping Discord notification")
        return
    try:
        return discord_send(channel_id, {
            "content": "",
            "embeds": [{"title": title, "description": description[:4000], "color": color}]
        })
    except Exception as e:
        print(f"Discord notification failed: {e}")
        raise

def discord_alert(title, msg):
    """Best-effort Discord error notification — never raises."""
    try:
        discord_embed(DISCORD_CHANNEL_ID, title, msg)
    except Exception:
        pass

def quick_alert(title, msg):
    """Single bounded best-effort Discord post for the top-level crash handler — NO retry and a
    short timeout, so the sole failure signal always fits under the cron SIGKILL watchdog even
    when the crash happens late in a slow/degraded run (the retry-based path could not)."""
    if not DISCORD_BOT_TOKEN:
        return
    try:
        data = json.dumps({"content": "",
                           "embeds": [{"title": title, "description": msg, "color": 0xD70F64}]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            data=data,
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                     "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (hermes-agent, 1.0)"},
            method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass

def friendly_error(e):
    """A short, plain-English summary of an exception for the Discord crash alert — so the
    channel gets a readable one-liner, not a Python traceback dump (the full traceback still
    goes to stdout / the cron log for debugging)."""
    first = (str(e).splitlines() or [""])[0]
    low = first.lower()
    if "run deadline exceeded" in low:
        return "The Google API calls were too slow overall this run, so it stopped early to stay under the cron time limit."
    if isinstance(e, subprocess.TimeoutExpired) or "timed out" in low or "timeout" in low:
        return "A Google API request (Gmail/Sheets) was too slow and timed out — likely a slow or unreachable network."
    if "429" in low or "rate" in low:
        return "Google or Discord rate-limited the requests (HTTP 429)."
    if "503" in low or "unavailable" in low:
        return "A Google API was temporarily unavailable (HTTP 503)."
    if any(k in low for k in ("credential", "token", "unauthor", "invalid_grant", "refresh")):
        return "Google sign-in failed — the OAuth token may need re-authorising (see the google-workspace skill)."
    return f"{type(e).__name__}: {first[:200]}" if first else type(e).__name__

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    global _run_deadline
    _run_deadline = time.monotonic() + RUN_DEADLINE_SECS
    state = load_state()
    # msg-ids of parse/foreign skips already reported, so a skip that sits NEWER than a
    # transient fetch failure (which pins the watermark) is alerted once, not every run.
    reported = set(state.get("reported_skip_ids", []))

    new_entries = []       # (order_date, merchant, total, order_id, email_ts, msg_id, provider)
    fetch_failures = []    # (provider, email_ts) — transient fetch failures hold the watermark back
    parse_failures = []    # (provider, msg_id, email_ts) — unparseable emails, advance past, report once
    foreign_skips = []     # (provider, msg_id, email_ts) — non-SGD receipts, advance past, note once
    nonfood_skips = []     # (provider, email_ts) — e.g. Grab rides: advance past silently
    date_failures = 0      # emails with an empty/unparseable Date header (usually the gws bug)


    for provider in PROVIDERS:
        pname = provider["name"]
        last_email_ts = provider_last_ts(state, pname)

        # Narrow the Gmail `after:` window to the provider's watermark (minus a 1-day overlap
        # for tz/boundary safety; dedup by message id makes the overlap harmless). The loop
        # below already skips anything <= the watermark, so this returns the SAME messages we
        # would process — it just stops hydrating dozens of already-synced receipts every run
        # (a wide `after:lookback` search hydrates the full --max 50 and took ~16-25s; the
        # narrow window is ~2s). First run for a provider (no watermark) uses the full lookback.
        if last_email_ts:
            try:
                after = (datetime.fromisoformat(last_email_ts).astimezone(SGT).date()
                         - timedelta(days=1)).strftime("%Y/%m/%d")
            except Exception:
                after = provider["lookback"]
        else:
            after = provider["lookback"]

        # Subject uses double quotes to protect any ! from bash; run_gapi uses list-form
        # subprocess (no shell) so the ! is not history-expanded, and retries transients.
        query = f'from:{provider["sender"]} subject:"{provider["subject"]}" after:{after}'
        messages = run_gapi(["gmail", "search", query, "--max", "50"])

        if not messages:
            print(f"[{pname}] No receipts found.")
            continue

        for msg in messages:
            msg_id = msg.get("id")
            email_ts_str = msg.get("date", "")
            if not msg_id:
                print(f"[{pname}] Skipping Gmail result with no message id: {msg}")
                continue
            try:
                email_dt = parse_email_date(email_ts_str)
            except Exception as e:
                print(f"[{pname}] Could not parse date '{email_ts_str}': {e}")
                date_failures += 1
                continue
            email_ts = email_dt.isoformat()

            if last_email_ts and email_ts <= last_email_ts:
                continue

            try:
                full = run_gapi(["gmail", "get", msg_id])
            except Exception as e:
                print(f"[{pname}] Failed to fetch email {msg_id}: {e}")
                fetch_failures.append((pname, email_ts))  # transient — must be retried next run
                continue

            body = full.get("body", "")
            status, payload = extract_expenses(body, provider)
            if status == "nonfood":
                print(f"  [{pname}] Skipping non-food receipt {msg_id} (e.g. a ride)")
                nonfood_skips.append((pname, email_ts))
                continue
            if status == "foreign":
                print(f"  [{pname}] Skipping non-SGD receipt {msg_id} (not summable into S$ MTD)")
                foreign_skips.append((pname, msg_id, email_ts))
                continue
            if status != "ok":
                print(f"[{pname}] Could not parse expense from email {msg_id}")
                parse_failures.append((pname, msg_id, email_ts))
                continue

            order_date, merchant, total, order_id = payload
            print(f"  [{pname}] Found: {order_date} | {merchant} | {total} | order: {order_id or '(none)'}")
            new_entries.append((order_date, merchant, total, order_id, email_ts, msg_id, pname))

    # Surface unparseable / non-SGD receipts ONCE. A deterministic skip that sits newer than
    # a transient fetch failure can't advance the watermark past it (the failure pins it), so
    # it would re-alert every run — dedup on a persisted set of already-reported msg-ids so
    # each skip is reported exactly once regardless of watermark progress.
    newly_reported = []

    parse_new = [(pn, mid, ts) for pn, mid, ts in parse_failures if mid not in reported]
    if parse_new:
        by_p = {}
        for pn, mid, _ts in parse_new:
            by_p.setdefault(pn, []).append(mid)
            newly_reported.append(mid)
        detail = "; ".join(f"{pn}: {ids}" for pn, ids in by_p.items())
        discord_alert(
            "⚠️ Food Sync — Unparseable Receipts",
            f"Could not parse {len(parse_new)} receipt email(s): {detail}\n"
            f"The email format may have changed — check that provider's regexes in extract_expenses()."
        )

    # Empty/unparseable Date headers usually mean the Gmail layer stripped headers (the
    # documented "empty-date storm" gws bug). Such emails have no usable timestamp, so the
    # watermark can't be pinned to them: they're re-seen while inside the Gmail search window
    # (watermark − 1 day), but once other receipts move the watermark past that, they drop out —
    # so this alert is the signal to fix the Gmail layer promptly. (Holding the watermark instead
    # was tried and reverted: a permanently undated email froze the provider for ~50 receipts.)
    if date_failures:
        discord_alert(
            "⚠️ Food Sync — Unreadable Email Dates",
            f"{date_failures} email(s) had an empty/unparseable Date header and were skipped.\n"
            f"This often means the Gmail layer stripped headers (see the 'empty-date storm' "
            f"pitfall) — real receipts may be missed until it is fixed."
        )

    # Non-SGD receipts (e.g. a Grab order placed abroad, 'TOTAL Rp ...') can't be summed into
    # the S$ MTD, so they're skipped. Note each one once (same persisted-id dedup as above).
    foreign_new = [(pn, mid, ts) for pn, mid, ts in foreign_skips if mid not in reported]
    if foreign_new:
        by_p = {}
        for pn, mid, _ts in foreign_new:
            by_p.setdefault(pn, []).append(mid)
            newly_reported.append(mid)
        detail = "; ".join(f"{pn}: {ids}" for pn, ids in by_p.items())
        discord_alert(
            "ℹ️ Food Sync — Non-SGD receipts skipped",
            f"Skipped {len(foreign_new)} receipt(s) not denominated in S$ (can't be summed "
            f"into the S$ total): {detail}"
        )

    # Persist the newly-reported ids immediately (independent of the watermark and of the
    # any_failure guard below) so an alert never re-fires, even if this run later aborts.
    if newly_reported:
        merged = list(state.get("reported_skip_ids", [])) + newly_reported
        state["reported_skip_ids"] = merged[-500:]  # bounded — parse/foreign skips are rare
        reported.update(newly_reported)
        save_state(state)

    if not new_entries and not parse_failures and not foreign_skips and not nonfood_skips:
        print("No new receipts to process.")
        return 1 if fetch_failures else 0

    # ── Within-batch dedup by (provider, order_id) ─────────────────────────────
    # When a refund causes two emails for the same order (different totals), both arrive
    # in the same batch. Keep only the latest (highest email_ts) per (provider, order_id)
    # so the sheet reflects the corrected total. Keyed by provider so booking codes and
    # FoodPanda order ids can never collide.
    by_key = {}
    no_order_id = []
    for entry in new_entries:
        oid = entry[3]       # order_id
        prov = entry[6]      # provider
        if oid:
            key = (prov, oid)
            if key in by_key:
                if entry[4] > by_key[key][4]:  # email_ts — keep the later email
                    by_key[key] = entry
            else:
                by_key[key] = entry
        else:
            no_order_id.append(entry)
    deduped_entries = list(by_key.values()) + no_order_id
    if len(deduped_entries) < len(new_entries):
        print(f"Within-batch dedup: {len(new_entries)} → {len(deduped_entries)} entries "
              f"(merged {len(new_entries) - len(deduped_entries)} duplicate order receipts)")

    new_entries = deduped_entries
    new_entries.sort(key=lambda x: x[4])  # sort by email_ts

    from collections import defaultdict
    by_month = defaultdict(list)
    for entry in new_entries:
        month_name = sheet_month_name(entry[0])
        # Monthly entry: (date, merchant, total, order_id, msg_id, provider) — drop email_ts
        by_month[month_name].append((entry[0], entry[1], entry[2], entry[3], entry[5], entry[6]))

    # ── Migrate existing sheets: Order ID column, then Provider column ─────────
    all_sheets = get_all_sheets()
    try:
        tab_data = read_all_tabs(all_sheets)
    except Exception as e:
        # Same as the old per-tab read failure: skip the migration; a later read of an
        # existing sheet will surface real trouble.
        print(f"  Warning: could not read tabs for the migration check: {e}")
        tab_data = {}
    for sheet_name in all_sheets:
        if sheet_name in tab_data:
            try:   # one bad tab (e.g. a name the range syntax rejects) skips only that tab
                ensure_order_id_column(sheet_name, tab_data[sheet_name])
                ensure_provider_column(sheet_name, tab_data[sheet_name])
            except Exception as e:
                print(f"  Warning: migration check failed for '{sheet_name}': {e}")
    all_sheets = get_all_sheets()  # refresh after migration

    any_failure = False

    months = list(by_month.items())
    for i, (month_name, entries) in enumerate(months):
        if _past_deadline():
            # Out of time: ONE alert for everything left (not one per month — each post after
            # the deadline still costs up to 15s), then stop; state isn't saved, so it retries.
            left = [m for m, _e in months[i:]]
            discord_alert("⚠️ Food Sync — ran out of time",
                          f"The Google API calls were slow this run; {', '.join(left)} weren't "
                          f"processed. Nothing was lost — the next run retries them.")
            any_failure = True
            break
        # Read existing rows. A read failure on an EXISTING sheet must abort this month —
        # treating it as empty would re-append everything as duplicates.
        if month_name in all_sheets:
            try:
                existing = get_existing_rows(month_name)
            except Exception as e:
                discord_alert("⚠️ Food Sync Error",
                              f"Failed to read existing rows for {month_name}: {e}")
                any_failure = True
                continue
        else:
            existing = []

        # Build lookup structures (provider-scoped where a cross-provider collision is possible).
        # read_rows tuples are (sheet_row, date, merchant, amount, email_id, order_id, provider).
        existing_email_ids = set(r[4] for r in existing if r[4])  # message ids are globally unique
        # (provider, order_id) → sheet_row (the real 1-based row to update)
        existing_order_map = {}
        for r in existing:
            if r[5]:
                existing_order_map[(r[6], r[5])] = r[0]
        # Content-based fallback: only for truly legacy rows (no email_id AND no order_id)
        existing_content = set((r[6], r[1], r[2], norm(r[3]))
                               for r in existing if not r[4] and not r[5])

        to_add = []     # (date, merchant, total, order_id, msg_id, provider)
        to_update = []  # (sheet_row, entry_tuple)

        for entry in entries:
            order_date, merchant, total, order_id, msg_id, provider = entry

            # 1) Skip if email already recorded
            if msg_id in existing_email_ids:
                continue

            # 2) If (provider, order_id) matches an existing row → update that row's amount
            if order_id and (provider, order_id) in existing_order_map:
                sheet_row = existing_order_map[(provider, order_id)]
                to_update.append((sheet_row, entry))
                continue

            # 3) Content-based dedup for legacy rows (no email_id, no order_id in sheet)
            if not order_id and (provider, order_date, merchant, norm(total)) in existing_content:
                continue

            # 4) New entry — append
            to_add.append(entry)

        if not to_add and not to_update:
            print(f"No new entries for {month_name} (all duplicates)")
            continue

        if month_name not in all_sheets:
            print(f"Creating new sheet: {month_name}")
            try:
                create_month_sheet(month_name)
            except Exception as e:
                discord_alert("⚠️ Food Sync Error",
                              f"Failed to create sheet '{month_name}': {e}")
                any_failure = True
                continue
            all_sheets = get_all_sheets()

        # ── Perform updates (existing rows with matching order_id) ─────────────
        update_errors = []
        for sheet_row, entry in to_update:
            try:
                # Columns: Date(A), Merchant(B), Amount(C), Email ID(D), Order ID(E), Provider(F)
                update_sheet_row(month_name, sheet_row,
                                 [entry[0], entry[1], entry[2], entry[4], entry[3], entry[5]])
                print(f"  Updated: {entry[0]} | {entry[1]} | S${entry[2]:.2f} "
                      f"({entry[5]}, order: {entry[3]})")
            except Exception as e:
                update_errors.append((entry, e))

        if update_errors:
            error_rows = [str(e[0][:3]) for e in update_errors]
            discord_alert(f"⚠️ Food Sync Partial Failure — {month_name}",
                          f"Failed to update {len(update_errors)} row(s): {error_rows}")
            any_failure = True
            continue

        # ── Perform appends (one deterministic block write) ────────────────────
        if to_add:
            try:
                # [date, merchant, amount, email_id, order_id, provider]
                append_rows(month_name,
                            [[r[0], r[1], r[2], r[4], r[3], r[5]] for r in to_add])
                for r in to_add:
                    print(f"  Added: {r[0]} | {r[1]} | S${r[2]:.2f} ({r[5]})")
            except Exception as e:
                error_rows = [str(r[:3]) for r in to_add]
                mtd_val = get_mtd_total(month_name)
                mtd_display = f"S${mtd_val:.2f}" if mtd_val is not None else "N/A"
                discord_alert(
                    f"⚠️ Food Sync Partial Failure — {month_name}",
                    f"Failed to append {len(to_add)} row(s): {error_rows} ({e})\n"
                    f"MTD total still: {mtd_display}"
                )
                any_failure = True
                continue

        # Verify writes (by email id) before counting this month as clean.
        if to_add:
            try:
                verified = get_last_rows(month_name, len(to_add))
                verified_ids = set(r[4] for r in verified if r[4])
                expected_ids = set(r[4] for r in to_add)
                if not expected_ids.issubset(verified_ids):
                    discord_alert(
                        "⚠️ Food Sync Error",
                        f"Write verification failed for {month_name}: expected {len(expected_ids)} "
                        f"new row(s), confirmed {len(expected_ids & verified_ids)} in sheet"
                    )
                    any_failure = True
                    continue
            except Exception as e:
                discord_alert("⚠️ Food Sync Error",
                              f"Write verification read failed for {month_name}: {e}")
                any_failure = True
                continue

        mtd = get_mtd_total(month_name)
        mtd_display = f"S${mtd:.2f}" if mtd is not None else "N/A"
        lines = []
        for sheet_row, row in to_update:
            lines.append(f"🔄 `{row[0]}` · *{san(row[1])}* ({row[5]}) · **S${row[2]:.2f}** _(updated)_")
        for row in to_add:
            lines.append(f"• `{row[0]}` · *{san(row[1])}* ({row[5]}) · **S${row[2]:.2f}**")
        lines.append(f"\n*Sheet updated — MTD total: {mtd_display}*")

        try:
            discord_embed(DISCORD_CHANNEL_ID,
                          f"🍔 Food Receipts Synced — {month_name}",
                          "\n".join(lines))
        except Exception as e:
            print(f"Discord notification failed: {e}")

    # Only save state if no failures occurred
    if any_failure:
        print("One or more months failed — not saving state; will retry next run.")
        return 1   # non-zero → the job shows red in `hermes cron list` (alerts already posted)

    # Advance each provider's watermark independently, but never past an email we failed to
    # FETCH (transient) for that provider, so it gets retried next run instead of skipped
    # forever. Recorded receipts, deterministic parse failures, and skipped non-SGD receipts
    # are all safe to advance past (only a transient fetch failure holds us back).
    for provider in PROVIDERS:
        pname = provider["name"]
        p_processed = ([e[4] for e in new_entries if e[6] == pname]
                       + [ts for pn, _mid, ts in parse_failures if pn == pname]
                       + [ts for pn, _mid, ts in foreign_skips if pn == pname]
                       + [ts for pn, ts in nonfood_skips if pn == pname])
        p_fetch = [ts for pn, ts in fetch_failures if pn == pname]
        last_ts = provider_last_ts(state, pname)
        if p_fetch:
            earliest_failure = min(p_fetch)
            safe = [t for t in p_processed if t < earliest_failure]
            new_ts = max(safe) if safe else last_ts
        else:
            new_ts = max(p_processed) if p_processed else last_ts
        if new_ts and new_ts != last_ts:
            state["providers"].setdefault(pname, {})["last_checked_email_ts"] = new_ts
            print(f"[{pname}] watermark → {new_ts}")

    save_state(state)
    print("Done.")
    if fetch_failures:
        print(f"{len(fetch_failures)} receipt(s) couldn't be fetched — they'll be retried next run.")
        return 1
    return 0

if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        import traceback
        print(traceback.format_exc())   # full traceback → stdout / cron log for debugging
        # Post a short, readable alert (not a code dump). Nothing was saved on a crash (the
        # any_failure / watermark guards), so it self-heals on the next run.
        quick_alert("❌ FoodPanda sync couldn't finish this run",
                    f"{friendly_error(e)}\n\nThe sync position wasn't saved, so the next scheduled "
                    f"run retries these receipts (any rows already written are de-duplicated). "
                    f"Full details are in the cron log.")
        raise
    sys.exit(code or 0)
