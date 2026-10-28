# Merchant Approval Enhancement Options

*From: cc-bill-review session, May 2026*

## Problem Statement
The `approved` flag lives in the `merchants` table, but that table gets rebuilt (DELETE + re-aggregate) when fixing normalization bugs or schema changes. Every rebuild = all approvals lost.

The normal import path (`process_pdf` → `store_statement`) already preserves flags correctly via `ON CONFLICT DO UPDATE` (UPSERT) where `approved` is excluded from the SET clause. The risk only arises when running `DELETE FROM merchants` as part of a debug/rebuild session.

Three options were evaluated:

---

## Option A: Permanent Approvals JSON File

Keep `~/.hermes/skills/productivity/cc-bill-review/approved_merchants.json` as authoritative. On every import, after merchants table is built, sync approved flags from JSON. On every approval, update both DB and JSON.

```json
{
  "5999": ["CAPITOL OPTICAL CO- SINGAPORE", "THE WOK PEOPLE", ...],
  "7008": ["SP DIGITAL PL-UTILITIE SINGAPORE", ...]
}
```

**Risks:**
- Dual sync burden: DB + JSON must always agree or they permanently diverge
- `review.py` reads from DB — cron job needs extra step to sync JSON → DB before generating review
- Normalization key drift: if `normalize_merchant()` changes, old JSON keys won't match new output
- JSON file not backed up automatically (DB is included in Hermes weekly backup)
- Only as good as the discipline to always update both stores

---

## Option B: Separate `merchant_flags` Table (Recommended)

Create a new table `merchant_flags` as a standalone entity never touched by imports:

```sql
CREATE TABLE merchant_flags (
    card_suffix TEXT NOT NULL,
    merchant_normalized TEXT NOT NULL,
    approved INTEGER DEFAULT 0,     -- 0=unreviewed, 1=approved, -1=rejected
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (card_suffix, merchant_normalized)
);
```

**How it works:**
- `process_pdf` / `store_statement` — **never touch `merchant_flags`** (only touch `merchants` for counts/totals)
- `review.py` JOINs `transactions` → `merchants` → `merchant_flags` for approval status
- Approval workflow (`approve #N`, `approve all`) — updates `merchant_flags` only
- Full DB rebuild — delete `merchants` and re-import PDFs freely; `merchant_flags` untouched

**Advantages:**
- Single source of truth — no file sync, no drift risk
- Survives any DB rebuild without any extra steps
- Natural JOIN in `review.py` adds `merchant_flags.approved` to existing queries
- `approved` column can be dropped from `merchants` table entirely (cleanup)

**Migration:**
```sql
-- Create the new table
CREATE TABLE merchant_flags (
    card_suffix TEXT NOT NULL,
    merchant_normalized TEXT NOT NULL,
    approved INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (card_suffix, merchant_normalized)
);

-- Seed from existing merchants table
INSERT OR IGNORE INTO merchant_flags
    (card_suffix, merchant_normalized, approved, updated_at)
SELECT card_suffix, merchant_normalized, approved, CURRENT_TIMESTAMP
FROM merchants WHERE approved != 0;

-- Verify counts match
SELECT 'merchants approved' as src, COUNT(*) as cnt FROM merchants WHERE approved != 0
UNION ALL
SELECT 'merchant_flags approved', COUNT(*) FROM merchant_flags WHERE approved != 0;
```

---

## Option C: No Architectural Change — Discipline Only

Keep the current schema. Document that `DELETE FROM merchants` must **never** be run without saving flags first.

The UPSERT in `store_statement` already preserves `approved` in normal operation. The only danger is the full rebuild procedure.

**Pros:** No code changes needed.
**Cons:** Relies on discipline; future debugging sessions (or new agents) will make the same mistake.

---

## Decision

Option B was recommended to the user (May 2026 session). User has not yet responded. When implemented:
1. Create `merchant_flags` table
2. Seed from current `merchants.approved` values
3. Update `review.py` to JOIN on `merchant_flags`
4. Update approval workflow to write to `merchant_flags` only
5. (Optional) Drop `approved` column from `merchants` table

> **Note (2026-10-02):** a full rebuild re-imports PDFs — but Discord uploads are kept in
> `~/.hermes/cache/documents/` for only ~7 days, so none of the past PDFs exist any more.
> A rebuild is only possible from PDFs you saved yourself; prefer fixing rows in place
> (`references/duplicate-merchant-sweep.md`, `scripts/migrate_identities.py`).

## Italian Merchant Safeguard (existing)

> **Correction (2026-10-02):** the live DB has these merchants at `approved = -1` (hidden), and
> `reviewlib.HOLIDAY_NAMES` (exact canonical names) keeps them out of the foreign and approval
> lists — so they are NOT "visible in the unapproved list" as written below. `approve.py`
> refuses to change them at all. The paragraph below is the May 2026 design, kept for provenance.

Italian transactions (TPER, CLESS, ATB SERVIZI, START ROMAGNA, LA BOTTEGA DEL TART, TAP & GO) are kept as `approved=0` (not `=-1`) so they remain visible in the unapproved list, preventing accidental `approve all` sweeps from clearing them silently. This safeguard applies regardless of which Option is implemented.
