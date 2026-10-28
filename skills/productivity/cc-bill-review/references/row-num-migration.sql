-- One-time migration: add row_num tiebreaker column to transactions table
-- Required for DBs created before May 2026 to support genuine duplicate txns in OCBC PDFs
-- Run this ONCE on an existing cc_review.db

-- Step 1: Rename old table
ALTER TABLE transactions RENAME TO transactions_old;

-- Step 2: Create new table with row_num in UNIQUE constraint
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_suffix TEXT NOT NULL,
    year_month TEXT NOT NULL,
    txn_date TEXT,
    merchant_raw TEXT,
    merchant_normalized TEXT,
    country_code TEXT,
    amount REAL,
    currency TEXT DEFAULT 'SGD',
    row_num INTEGER DEFAULT 0,
    is_approved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(card_suffix, year_month, txn_date, merchant_normalized, amount, row_num)
);

-- Step 3: Copy data (row_num defaults to 0 for all existing rows — safe)
INSERT INTO transactions (
    id, card_suffix, year_month, txn_date, merchant_raw, merchant_normalized,
    country_code, amount, currency, is_approved, created_at
)
SELECT
    id, card_suffix, year_month, txn_date, merchant_raw, merchant_normalized,
    country_code, amount, currency, is_approved, created_at
FROM transactions_old;

-- Step 4: Verify row count matches
-- SELECT 'Old rows:', COUNT(*) FROM transactions_old;
-- SELECT 'New rows:', COUNT(*) FROM transactions;

-- Step 5: Drop old table
DROP TABLE transactions_old;
