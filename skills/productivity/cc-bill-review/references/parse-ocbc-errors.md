# OCBC PDF Parsing — Error Log & Fixes

## PDF text quirks

### 1. Indented dates
Raw PDF text from pymupdf indents transaction dates with 2+ spaces:
```
          12/03
     S$234.56
     MERCHANT NAME
```
**Fix:** `^\s{2,}(\d{1,2}/\d{1,2}(?:/\d{2})?)$` — must match leading whitespace.

### 2. Amounts on same line as date sometimes
```
  12/03  S$234.56
  MERCHANT
```
Strip and detect date anchor on the raw line before `.strip()`.

### 3. Malformed amounts
Some lines: `(676.87` — open paren with no closing, or `S$1,234` with embedded currency.
```python
def parse_amount(s):
    s = s.strip()
    try:
        return float(s.replace(",", "").replace("S$", "").replace("(", "-").replace(")", ""))
    except:
        return float("".join(c for c in s if c in ".-0123456789"))
```

### 4. Footer bleed
```
COFFEE HIVE - INNOV SINGAPORE STREET, OCBC CENTRE, SINGAPORE 049481
OCBC Bank 65 Chulia Street #07-01 Singapore 049513
OCBC Bank Limited
TOTAL 780.78 SUBTOTAL 780.78
Page 2 of 3
```
> **2026-10-04 correction:** the live `strip_footer_junk()` is a list of patterns (see
> `scripts/parse_ocbc.py`), not the sketch below. Since v3.5.1 the "glued footer" pattern
> keeps the merchant's city (`(?<=SINGAPORE)\s+OCBC\s+BANK…`): eating it gave the
> page's last row its own identity (`COFFEE HIVE INNOVI` vs `COFFEE HIVE INNOVI SINGAPORE`).

**Fix — `strip_footer_junk()`:**
```python
def strip_footer_junk(s):
    # Remove OCBC Bank address bleed, page numbers, TOTAL/SUBTOTAL lines
    s = re.sub(r'\bOCBC\b.*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'TOTAL\s+[\d,]+\.?\d*\s+SUBTOTAL.*$', '', s)
    s = re.sub(r'^Page\s+\d+\s+of\s+\d+\s*$', '', s, flags=re.M)
    return s
```

## parse_date state bug

**Wrong:** Passing `stmt_year` and `stmt_month` as refs that get mutated:
```python
def parse_date(date_str, stmt_year, stmt_month, dates_list):
    dates_list.append((stmt_year, stmt_month, date_str))  # MUTATES
```

**Correct:** Compute return value without mutation:
```python
def parse_date(date_str, stmt_year, stmt_month):
    # MM/DD or MM/DD/YY format
    parts = date_str.strip().split("/")
    month = int(parts[0])
    year = stmt_year
    if month > stmt_month:  # wrapped to previous year
        year = stmt_year - 1
    return f"{year}-{month:02d}-{day:02d}"
```

## BLOCK_STOP patterns

**Too broad (wrong):**
```python
BLOCK_STOP = ["SINGAPORE", "OCBC", "NETS", "TOTAL", "MASTERCARD"]
```

**Correct (exact-match or full-line):**
```python
BLOCK_STOP = ["OCBC", "TOTAL"]  # exact match only, checked per-line
# plus full-line patterns:
# "SINGAPORE" removed — it's a country code in merchant names
```

## Merchant normalization

Full implementation:
```python
def normalize_merchant(raw):
    s = raw.upper()
    s = re.sub(r'[,\s]+', ' ', s).strip()
    # Strip trailing country codes
    s = re.sub(r'\s+[A-Z]{2}\s*$', '', s)
    # Strip trailing numeric codes
    s = re.sub(r'\s+\d+\s*$', '', s)
    s = re.sub(r'\s+[PN]/?[A-Z]?\s*$', '', s)
    # Strip transaction ID suffixes
    s = re.sub(r'\s*-\s*\d{6,}.*$', '', s)
    # Strip known footers
    s = re.sub(r'\s*OCBC\s+BANK\s+LIMITED.*$', '', s, flags=re.IGNORECASE)
    return s.strip()
```

## State machine for transaction extraction

```
State machine:
1. DATE state: look for indented date line → enter AMT state
2. AMT state: parse amount → enter DESC state (collect lines until stop)
3. DESC state: collect lines until BLOCK_STOP or next DATE → store txn
```

Key: the description lines are collected as raw PDF lines (before strip) so indentation is preserved if needed for debugging.

## Schema summary

```sql
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_suffix TEXT,
    year_month TEXT,       -- YYYY-MM
    txn_date TEXT,         -- YYYY-MM-DD
    merchant_raw TEXT,
    merchant_normalized TEXT,
    country_code TEXT,
    amount REAL,
    currency TEXT,
    is_approved INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE merchants (
    card_suffix TEXT,
    merchant_normalized TEXT PRIMARY KEY,
    approved INTEGER DEFAULT 0,
    first_seen TEXT,       -- YYYY-MM-DD
    last_seen TEXT,
    txn_count INTEGER,
    total_spent REAL
);

CREATE TABLE statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_suffix TEXT,
    year_month TEXT,
    statement_date TEXT,
    due_date TEXT,
    total_due REAL,
    minimum_due REAL
);
```


## Quirks found in the v3.5.0 tune-up (2026-10-02)

### 5. Statement total glued onto the last transaction
The TOTAL AMOUNT DUE figure appears in the text right after the last transaction, so it
was appended to that transaction's description on every statement (`SHOPEE SG MP …
SG 2,689.54`, `CASH REBATE 771.52`, `ANNUAL FEE 516.87`) — a junk merchant identity each
month. `strip_glued_total()` removes exactly that trailing figure from the last
transaction; `scripts/migrate_identities.py` repaired the stored rows.

### 6. Column layout puts the country on the description line
`NTUC FAIRPRICE APP SINGAPORE    SG` / `SHOPEE SG MP       SINGAPORE    SG`: a ≥3-space gap
before an ISO code sets the country. A 2-space gap is a truncated name (`HUGGS COFFEE
PTE  L SINGAPORE`), not a country.

### 7. `FOREIGN CURRENCY USD 10.00` inside the description
The billing currency + original amount. Kept in `merchant_raw` (the foreign flag reads it
there; the review shows `SG · USD 10.00`) but stripped from the merchant identity, so one
merchant isn't split per charge amount.

### 8. Completeness check
Previous statement's total + every transaction (incl. the payment credit) == this
statement's TOTAL AMOUNT DUE, to the cent, on every real statement. A mismatch is reported
as a parser warning in the review — it means a transaction was missed or misread.
Same-day identical charges (`row_num > 0`) are part of that sum: they are real charges.
