# Grab Provider Added — July 18, 2026 (Change Record)

On 2026-07-18 the tracker was generalized from FoodPanda-only to **multi-provider** (FoodPanda + Grab), per an explicit set of product decisions. This records the design, the bugs hit during rollout, and how they were fixed — so the next provider addition is smoother.

## Product decisions (from the requester)

- **Sheet layout:** shared monthly tabs + a new **Provider** column (col F). One combined MTD/weekly total via `=SUM(C:C)`.
- **Code shape:** generalize the existing two scripts over a `PROVIDERS` list (keep the same two cron jobs), not separate Grab scripts.
- **Discord:** one combined summary/report, each line provider-labelled.
- **Grab date:** logged under the receipt's **Pickup time** date (Grab has no explicit "Order date" line).

## Grab field mapping

| Field | Source in the Grab e-receipt |
|-------|------------------------------|
| Subject filter | `Your Grab E-Receipt` (the sender also emails promos) |
| Merchant | `Order from: … ` up to `Delivery location:` |
| Order ID | `Booking code A-…` (no colon after the label) |
| Amount | `TOTAL (INCL. TAX)  S$ …`, fallback bare `TOTAL S$ …` |
| Date | `Pickup time: 07 Jul 26 …` → `2026-07-07` (parsed via `MONTHS`, locale-independent) |

## Bugs hit during rollout & fixes

1. **`gws sheets append` column-shift.** The first run appended a Grab row into columns **E/F/G** instead of A/B/C on the legacy **May** tab. Root cause: `values.append` anchors on the *detected table's first column*; on a legacy tab with an empty D column and near-empty E column, the table was detected on the populated right-side columns, shifting the write. It also left a stray `0`. Jul (all columns populated) was unaffected — which is why it slipped past the first success.
   **Fix:** append via an explicit `values.update` at `next_data_row()` (computed from column A only). No table detection, no shift.

2. **Provider-backfill off-by-one.** The initial migration wrote `FoodPanda` to `F2:F{n+1}`, assuming data starts at row 2. Tabs with a **blank spacer row 2** (A–E empty, only the summary populated) have data starting at row 3, so the last data row was left provider-less and the spacer row wrongly got `FoodPanda`.
   **Fix:** `ensure_provider_column` is now **row-accurate and self-healing** — it derives the desired Provider column row-by-row from the actual data and only writes on a diff. Idempotent; repairs partial migrations.

3. **Group-order receipts had no total.** Grab **Group Order** receipts have only a bare `TOTAL S$ …`, no `TOTAL (INCL. TAX)` line, so the strict regex missed a valid S$ order (Jun 17, S$10.48).
   **Fix:** `total_re_fallback` (`TOTAL\s*S\$…`, case-sensitive so it never hits `Subtotal`).

4. **Non-SGD receipt misclassified.** A Grab order placed abroad (May 21, `TOTAL Rp 193600`, Indonesian labels) first tripped the parse-failure path (its Indonesian date label failed the date regex before any currency check).
   **Fix:** `extract_expenses` checks `"S$" not in text` **first** and returns `("foreign", None)` → skipped cleanly with an `ℹ️` note, watermark advanced.

5. **Currency-formatted MTD/amount cells.** Some legacy tabs (Jan/Feb) display amounts and `G2` as currency (`$30.32`), so a bare `float()` read failed.
   **Fix:** `parse_money()` strips `$`/commas in both scripts' MTD reads and the weekly amount parse.

## Other structural changes

- **State is now per-provider:** `{"providers": {"<name>": {"last_checked_email_ts": …}}}`. `load_state()` migrates the old flat schema → FoodPanda. Each provider's watermark advances independently and never past that provider's fetch failure.
- **MTD moved F → G** to free col F for Provider. `read_rows` returns the real sheet row so updates/appends never rely on positional offsets.
- **Weekly `retry()` drift fixed:** the canonical weekly copy still had the case-sensitive `retry()`; all four copies are now byte-identical with the lowercase patterns.

## Recovery during rollout

The `any_failure` guard meant state was never saved on the buggy first run, so re-running after each fix reprocessed cleanly (dedup by message id prevented duplicating the one correct Grab row). The malformed May rows were cleared with `values.clear` on `May 2026!A24:G25` before the clean re-run. A values-only pre-change snapshot was taken at `~/.hermes/foodpanda_sheet_backup_20260718.json`.

## Verification

- All 7 tabs: every data row has a valid Provider; no provider on blank rows; `G2` MTD matches the col-C sum.
- 4 Grab rows landed correctly (Jul 7 / Jul 18 / Jun 17 group-order / May 30); May 21 IDR skipped.
- Idempotent re-run: "No new receipts to process."
- Weekly report posted with provider-labelled lines and a combined total.

## Prevention / for the next provider

- Fetch a **real** receipt and test the regexes before deploying (currencies, group orders, and localized layouts vary).
- Never use `gws sheets append` for data rows — explicit `values.update` only.
- Keep migrations row-accurate and idempotent; assume spacer rows and legacy header gaps exist.
- A non-S$ provider needs currency handling — it can't share the S$ MTD.
