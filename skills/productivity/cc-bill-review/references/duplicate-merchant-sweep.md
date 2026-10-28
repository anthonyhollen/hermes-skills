# Duplicate Merchant Sweep

> ⚠️ **Correction (2026-10-02, v3.5.0):** the leading `-NNNN` of a raw description is
> **not** a per-shop "terminal code" — it is a payment-channel code shared by many
> merchants (in the live DB `-9113` covers 25 different shops, `-9330` 11, `-8130` 8; the
> Italian holiday merchants and JUNGSUNG HAIR all carry `-9113`). Same code ≠ same shop:
> judge a cluster on the **names**, show the user the evidence the sweep prints, and let
> the **user** decide — a merge moves an approval. The verified merges below are still
> right on their names. The sweep now prints every raw description per name itself, so
> no hand-written SQL is needed, and it no longer runs before every import (on demand only).

A recurring failure mode: OCBC rephrases the same merchant's description in their
PDF statements (e.g. `-9113 SMP**JUNG SUNG HAIR SG` → `-9113 JUNGSUNGHAIR PTE LT
SINGAPORE`). The normalizer strips terminal code and country suffixes from each
variant independently, producing two distinct `merchant_normalized` strings for
the same real-world shop. The merchants table then creates a new row, and the
review flags it as "🆕 new merchant" forever — until someone catches it.

This reference documents how to detect, verify, and fix these splits.

## When to run

When the user asks "why is X flagged as new?" (or a familiar shop shows up under 🆕).
(Until v3.5.0 the docs said "before every import" — that made every upload a multi-step,
ad-hoc-SQL session; the upload is now one command.)

```bash
cd ~/.hermes/skills/productivity/cc-bill-review
python3 scripts/find_duplicate_merchants.py
```

The script is **read-only** — it prints candidate clusters with their raw descriptions.
Clusters already verified as different shops are listed in `VERIFIED_DISTINCT` in the
script and not re-reported. Open 2026-10: on 5999, `SHOPEE MP SINGAPORE` (raw
`-1534 SHOPEE SG MP …`) vs `SHOPEE SINGAPORE MP` (`-1534 SHOPEE SINGAPORE MP …`) look like
one Shopee rail renamed — both are approved, so nothing is wrongly flagged; ask the user
before merging.

## Verified clusters (July 2026 sweep)

| Card | Variant A (canonical) | Variant B (orphan) | Signal | Same shop? |
|------|----------------------|---------------------|--------|------------|
| 5999 | `SMP**JUNG SUNG HAIR` | `JUNGSUNGHAIR PTE SINGAPORE` | same terminal `-9113` | ✅ |
| 5999 | `NETFLIX.COM LOS GATOS` | `NETFLIX.COM SINGAPORE` | same terminal `-5445`, same amount | ✅ |
| 5999 | `GOMO MOBILE PLAN SINGAPORE` | `GOMO SINGTEL SINGAPORE` | same monthly S$12, renamed May 2026 | ✅ |
| 7008 | `SHOPEE SINGAPORE MP SINGAPORE` | `SHOPEE SINGAPORE SINGAPORE` | same terminal `-1302` | ✅ |
| 5999 | `SHOPEE SINGAPORE MP` | `SHOPEE APPLEPAY SINGAPORE` | different terminals `-1534` vs `-9330` | ❌ distinct (Apple Pay rail) |

## Verification (superseded — see the correction at the top: the code is NOT a shop id)

The most reliable single signal that two normalized names represent the same shop
is the **5-digit terminal code prefix** in `merchant_raw` (the first whitespace-
separated token, e.g. `-9113`, `-5445`, `-1302`). If both raw variants share a
terminal code AND the same card, they are the same shop with very high
probability — OCBC's terminal codes are stable per-merchant.

Example raw pairs from the verified clusters:

```
-9113 SMP**JUNG SUNG HAIR SG          (Jan–May 2026)
-9113 JUNGSUNGHAIR PTE LT SINGAPORE   (Jun 2026)   ← rephrased

-5445 NETFLIX.COM LOS GATOS           (Dec, Feb, Mar, May)
-5445 NETFLIX.COM SINGAPORE           (Jan, Apr, Jun)   ← alternates monthly

GOMO MOBILE PLAN SINGAPORE            (Dec–Apr)
GOMO BY SINGTEL SINGAPORE             (May)   ← renamed

-9113 COFFEE HIVE - INNOV SINGAPORE   (Jan–Sep 2026 statements)
-9113 COFFEE HIVE  INNOVI SINGAPORE   (Oct 2026)   ← renamed; the user merged #18 into #109 (2026-10-04)
```

## False positives to expect

The token-Jaccard heuristic (see `find_duplicate_merchants.py`) is conservative —
it requires either ≥0.5 overlap, a shared distinctive (len≥5) token, or a
strict prefix match — but it can still pair genuinely distinct merchants that
happen to share a common word:

| Paired | Why it's wrong |
|--------|----------------|
| `SHOPEE SINGAPORE MP` ↔ `SHOPEE APPLEPAY SINGAPORE` | Different pay rails, different terminals (`-1534` vs `-9330`). SHOPEE MP is in-app credit card top-up; SHOPEE APPLEPAY is the Apple Pay rail. Both legitimately flagged as new merchants. |
| `GRAB* AZPAKGNPMCCJ- SINGAPORE` ↔ other `GRAB*` codes | The token `AZPAKGNPMCCJ` is a per-merchant sub-account code; not all GRAB* rows are the same merchant. |

Always show the user the raw descriptions (the sweep prints them) and let them decide before fixing.

## Fix recipe (order matters) — only after the USER confirms it's the same shop

**Preferred since v3.5.1 (2026-10-04) — a code change, so for whoever maintains the skill,
not the agent:**
1. In `_CARD_CANNONICAL_MAP_<card>`, the KEY is the old rows' cleaned name *before* the map
   (what `normalize_merchant()` produces with the map ignored — usually the stored name plus
   its city, e.g. `COFFEE HIVE - INNOV SINGAPORE`), the VALUE is the kept merchant's name. The
   map is applied once: if the old name is itself a map VALUE, change that entry's value
   instead (v3.5.1 did exactly that), and retarget any other entry whose value is the old name.
2. Bump `MARKER` in `scripts/migrate_identities.py`, run it WITHOUT `--apply` and check the plan
   lists the old rows → the kept name. **A 0-row plan means the key is wrong — stop; don't
   `--apply` it** (that would use up the marker and merge nothing).
3. `--apply`: snapshot, one transaction, the kept row keeps its id, the higher approval wins,
   but a hidden (-1) side makes the kept row hidden (unlike the SQL below, which keeps the
   approval) — un-hide afterwards if that's wrong. It also records the merged id (`approve
   #old` then says where it went) and repoints the last-shown list.
The Coffee Hive merge (#18 → #109) was done this way. The manual SQL below still works, but
it doesn't record the merged id or repoint the last-shown list: `approve #old` then says "no
such id", and `approve all` skips it until the next posted list.

Keep the higher approval: if the orphan was approved and the canonical row wasn't, set
the canonical row's `approved` to 1 before deleting the orphan (step 2a below). Then add
the map entry, and run `python3 scripts/selftest.py` — its live-data checks must stay
green (normalizer drift 0, no transaction without a merchants row). Merchant ids are
never reused after the delete (high-water mark).

```python
import sqlite3
db = sqlite3.connect("/home/amh/.hermes/skills/productivity/cc-bill-review/scripts/cc_review.db")
db.row_factory = sqlite3.Row

card, orphan, canon = "5999", "JUNGSUNGHAIR PTE SINGAPORE", "SMP**JUNG SUNG HAIR"

# 1. Repoint transactions
db.execute("""
    UPDATE transactions SET merchant_normalized=?
     WHERE card_suffix=? AND merchant_normalized=?
""", (canon, card, orphan))

# 2a. Carry the approval: the canonical row ends approved if either row was
db.execute("""
    UPDATE merchants SET approved = MAX(approved, (SELECT approved FROM merchants
           WHERE card_suffix=? AND merchant_normalized=?))
     WHERE card_suffix=? AND merchant_normalized=? AND approved >= 0
""", (card, orphan, card, canon))

# 2. Delete orphan merchants row FIRST (UNIQUE constraint pitfall)
db.execute("""
    DELETE FROM merchants WHERE card_suffix=? AND merchant_normalized=?
""", (card, orphan))

# 3. Refresh aggregates on the surviving row
db.execute("""
    UPDATE merchants
       SET txn_count   = (SELECT COUNT(*) FROM transactions WHERE card_suffix=? AND merchant_normalized=?),
           total_spent = (SELECT COALESCE(SUM(amount),0) FROM transactions WHERE card_suffix=? AND merchant_normalized=?),
           first_seen  = (SELECT MIN(txn_date) FROM transactions WHERE card_suffix=? AND merchant_normalized=?),
           last_seen   = (SELECT MAX(txn_date) FROM transactions WHERE card_suffix=? AND merchant_normalized=?)
     WHERE card_suffix=? AND merchant_normalized=?
""", (card, canon, card, canon, card, canon, card, canon, card, canon))

db.commit()
```

Then add the variant to the appropriate map in `parse_ocbc.py` so future imports
collapse automatically:

```python
_CARD_CANNONICAL_MAP_5999 = {
    # ...
    "JUNGSUNGHAIR PTE SINGAPORE":         "SMP**JUNG SUNG HAIR",
    "NETFLIX.COM SINGAPORE":              "NETFLIX.COM LOS GATOS",
    "GOMO SINGTEL SINGAPORE":             "GOMO MOBILE PLAN SINGAPORE",
}
```

The map is **dict-lookup, exact-match** — not substring. The variant key must be
the full normalized string exactly as it appears in the DB.

## Why this isn't fully automated

The heuristic is high-recall but not high-precision: pairing is mechanical, but
"is this the same shop?" is a judgement on the names (the `-NNNN` code proves nothing —
see the correction at the top), and a merge moves an approval, so the **user** decides.
The sweep prints the raw descriptions to make that easy.

## Related skill sections

- SKILL.md → "Merchant naming is two layers" (canonicalization architecture)
- SKILL.md → "Working Directory Requirement" (run scripts from skill dir, not scripts/)
- SKILL.md → "Italian Keyword Mismatch" pitfall (same shape of bug, different trigger)