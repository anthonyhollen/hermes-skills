---
name: foodpanda-expense-tracker
description: Track FoodPanda/Grab food expenses in Sheets + Discord
version: 1.9.0
author: ham
license: MIT
metadata:
  hermes:
    tags: [FoodPanda, Grab, Expenses, Gmail, Google Sheets, Discord]
  triggers:
    - "foodpanda expense"
    - "foodpanda tracker"
    - "foodpanda receipts"
    - "grab expense"
    - "grab receipts"
    - "food expense tracker"
    - "food payment reminder"
---

# Food Expense Tracker (FoodPanda + Grab)

## When to Use

Any question about logged food-delivery spend — "list food expenses this month", "how much did I spend on food in <month>", "why isn't <order> recorded" — plus maintenance of the sync / weekly report / monthly payment-reminder cron jobs.

Parses food-delivery order emails from Gmail for **multiple providers** (FoodPanda `no-reply@foodpanda.sg`, Grab `no-reply@grab.com`), dedupes against a Google Sheet (by Gmail message id, order id, or content fallback), auto-creates a tab per month with a **Provider** column, and posts daily + weekly summaries and a monthly payment reminder to Discord.

> The skill/dir name is still `foodpanda-expense-tracker` (and the scripts are still `foodpanda_*.py`) for continuity with the existing cron jobs — it now covers Grab as well. Adding a third provider is a config-only change (see `CLAUDE.md`).

| Piece | Detail |
|-------|--------|
| **Providers** | FoodPanda (`Thank you for your order!`) + Grab (`Your Grab E-Receipt` — Grab **ride** receipts share that subject and are skipped silently). Config list in `foodpanda_sync.py`. |
| **Gmail** | per provider: `from:<sender> subject:"<subject>" after:<watermark − 1 day>` |
| **Parser** | Order date, merchant, order total, **order ID** — provider-specific regexes (Grab maps: Booking code→order id, "Order from:"→merchant, `TOTAL (INCL. TAX)`→amount, Pickup time→date) |
| **Sheet** | `1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4` — tab per month, title is the **3-letter month**: `Nov 2026` (never `November 2026` — a full name fails with `Unable to parse range`); cols `Date \| Merchant \| Amount \| Email ID \| Order ID \| Provider` (D = email dedup key, E = order update key, **F = provider**), **`G2 = =SUM(C2:C)`** (MTD) |
| **Discord** | channel `1499972245437550602` — one **combined** summary/report across providers + the monthly payment reminder |

## Scripts

| Script | Cron (SGT) | Job ID | Purpose |
|--------|-----------|--------|---------|
| `scripts/foodpanda_sync.py` | Daily 11:30pm (`30 23 * * *`) | `b3b0042bd9cf` | For each provider: new Gmail receipts → dedup (email id + provider/order id) → append/update → combined Discord summary; then reconciles the current month's tab against `tab_snapshot` and restores any vanished rows |
| `scripts/foodpanda_weekly_report.py` | **Sun** 9:05am (`5 9 * * 0`) | `fabb5d12aba9` | The **previous complete Sun–Sat week** (last Sunday → last night; + previous-month tab on boundary weeks) → expense list (provider-labelled) + weekly total + MTD through that Saturday |
| `scripts/foodpanda_payment_reminder.py` | Days 1–5, 9:00am (`0 9 1-5 * *`) | `ea9a404f4c36` | 1st of each month: @-mention + previous month's food total to make payment for (days 2–5 = catch-up if the 1st's run fails) |

All jobs run **`no_agent: true`** — the scheduler runs each script directly under the gateway venv, so this skill is **not** loaded at runtime; they self-post to Discord, so `deliver` stays `local`. A handled failure posts a Discord alert **and** exits non-zero (the job shows red in `hermes cron list`).

> ⚠️ **Two copies must stay byte-identical.** Cron runs `/home/amh/.hermes/scripts/<name>.py`; the canonical copies live here in `scripts/`. Deploy to **both** on every edit, then run `scripts/test_foodpanda.py` (see `CLAUDE.md`).

## Discord output

One **combined** per-month summary (provider-labelled lines; `🔄` = amount updated via a (provider, order ID) match, e.g. a refund):

```
🍔 Food Receipts Synced — Jul 2026
🔄 `2026-07-02` · *Sushi Express* (FoodPanda) · **S$85.00** _(updated)_
• `2026-07-07` · *Yuen Kee Dumpling* (Grab) · **S$34.40**
*Sheet updated — MTD total: S$161.90*
```

The Sunday report is a combined list for the previous Sun–Sat + weekly total + MTD (month the week ended in, through that Saturday). Other embeds: `♻️ Food Receipts Restored` (rows a manual edit removed — re-added), `ℹ️ Non-SGD receipts skipped`, `⚠️ Unparseable Receipts` (an S$ food receipt that didn't parse — likely a format change), `⚠️ Unreadable Email Dates`, `⚠️ Food Sync — ran out of time`, and `❌ FoodPanda sync couldn't finish this run` (short plain-English cause; full traceback stays in the cron log). Samples → **`TROUBLESHOOTING.md`**.

## Monthly payment reminder

On the **1st of each month** at 09:00 SGT (days 2–5 are a catch-up retry window), `foodpanda_payment_reminder.py` posts a plain-content reminder that **@-mentions you** (so it pings) to make payment for the **previous** month's food expenses, with that month's total from the sheet. Idempotent per month; if the sync on the month's last night didn't complete OK it adds a heads-up that orders may be missing:

```
<@470611394698412052> 🍔 Reminder: make payment for **Aug 2026** food expenses. Total **S$872.89**.
```

State: `~/.hermes/foodpanda_reminder_state.json`. Test flags: `--dry-run`; `--simulate-date YYYY-MM-DD` (**implies `--dry-run`**); `--force` (bypasses window/state — posts AND marks that cycle fired; never combine with a future `--simulate-date`).

**"How much did I spend on food in <month>?"** — run `~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/foodpanda_payment_reminder.py --dry-run --force --simulate-date <YYYY-MM-01 of the NEXT month>` (prints that month's total; posts nothing, writes nothing), or read the month tab (`'<Mon YYYY>'!A:F`) via `google_api.py sheets get`.

## Ad-hoc expense queries ("list all food expenses for this month")

1. Read the month tab directly: `~/.hermes/hermes-agent/venv/bin/python ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py sheets get <SHEET> "'Nov 2026'!A1:G200"`. If the range fails to parse the tab title is wrong — fetch `sheets.properties.title` via `spreadsheets().get`; titles are 3-letter month + year, never a full month name.
2. Sum and list the rows in code; cross-check the computed sum against **G2** — in an `A1:G` read the label `MTD Total` lands in row 1 col G and the value in row 2 col G, so don't read row 1's label as the total. Report one bullet per row (`date · merchant · **S$amount**`, provider noted) then the MTD total — no markdown table.
3. Column D is the origin: `manual` / `manual (wife account)` = hand-entered; a Gmail message id = auto-parsed. This split is the answer to "does it include the manually added expenses?" — always sum both and say which is which.
4. A receipt dated today is not missing — the daily 23:30 SGT run has not fired yet; say so instead of calling it a gap.

## Rows that vanish from the sheet

A manual rewrite of a month tab can silently drop cron-added rows (the Gmail watermark has moved
past those receipts, so the normal scan never looks at them again). The sync now **repairs this
automatically**: every run remembers the Gmail message ids present in the current month's tab —
plus the previous month's around a boundary — in `tab_snapshot` (sync state), and any id that has
disappeared is re-fetched from Gmail, re-inserted **in date order**, and announced with a
`♻️ Food Receipts Restored` embed. Manual rows (`manual`, `manual (wife account)`) are never
tracked. A receipt deleted again and again is left out after 3 restores (`reconcile_blocked_ids`)
with a one-time alert.

Older forensics (a row older than the snapshot, one that never got a receipt, or proving what a
manual edit removed) → **`references/sheet-row-forensics.md`**: cron `Added:` line + Drive
revision exports around the edit.

## More

- First-time setup / creating the cron jobs → **`SETUP.md`**
- Debugging a run (empty-date storm, non-SGD skips, "did it run?", catch-up, run deadline) → **`TROUBLESHOOTING.md`**
- A row the user expects to see but the month tab lacks (prove sync vs tab rewrite, revision exports, safe restore) → **`references/sheet-row-forensics.md`**
- Editing the scripts (provider config, flow internals, regexes, timeout budget, Discord retry rules, **gotchas — read before any edit**) → **`CLAUDE.md`**
- Testing an edit (offline suite, fake-Google rules, what the checks pin down) → **`references/testing.md`**
- Incident / change records: `references/float-expiry-crash-2026-06-13.md`, `references/timeout-crash-2026-07-17.md`, `references/grab-provider-added-2026-07-18.md`, `references/tuneup-2026-07-18.md` (timeout hardening), **`references/tuneup-2026-09-28.md`** (weekly Sunday gap, migration cost, reminder hardening, Grab rides, POST de-dup). The reconcile guard has no incident file — its rules live in CLAUDE.md (flow step 12 + the reconcile gotcha) and the operator view in `references/sheet-row-forensics.md`.
