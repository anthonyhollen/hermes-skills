---
name: foodpanda-expense-tracker
description: Tracks food-delivery expenses (FoodPanda + Grab) by parsing receipt emails from Gmail and logging them to a Google Sheet, with weekly summaries and a monthly payment reminder (1st of month) posted to Discord
version: 1.7.2
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

Parses food-delivery order emails from Gmail for **multiple providers** (FoodPanda `no-reply@foodpanda.sg`, Grab `no-reply@grab.com`), dedupes against a Google Sheet (by Gmail message id, order id, or content fallback), auto-creates a tab per month with a **Provider** column, and posts daily + weekly summaries and a monthly payment reminder to Discord.

> The skill/dir name is still `foodpanda-expense-tracker` (and the scripts are still `foodpanda_*.py`) for continuity with the existing cron jobs — it now covers Grab as well. Adding a third provider is a config-only change (see `CLAUDE.md`).

| Piece | Detail |
|-------|--------|
| **Providers** | FoodPanda (`Thank you for your order!`) + Grab (`Your Grab E-Receipt` — Grab **ride** receipts share that subject and are skipped silently). Config list in `foodpanda_sync.py`. |
| **Gmail** | per provider: `from:<sender> subject:"<subject>" after:<watermark − 1 day>` |
| **Parser** | Order date, merchant, order total, **order ID** — provider-specific regexes (Grab maps: Booking code→order id, "Order from:"→merchant, `TOTAL (INCL. TAX)`→amount, Pickup time→date) |
| **Sheet** | `1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4` — tab per month (`May 2026`); cols `Date \| Merchant \| Amount \| Email ID \| Order ID \| Provider` (D = email dedup key, E = order update key, **F = provider**), **`G2 = =SUM(C2:C)`** (MTD) |
| **Discord** | channel `1499972245437550602` — one **combined** summary/report across providers + the monthly payment reminder |

## Scripts

| Script | Cron (SGT) | Job ID | Purpose |
|--------|-----------|--------|---------|
| `scripts/foodpanda_sync.py` | Daily 11:30pm (`30 23 * * *`) | `b3b0042bd9cf` | For each provider: new Gmail receipts → dedup (email id + provider/order id) → append/update → combined Discord summary |
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

The Sunday report is a combined list for the previous Sun–Sat + weekly total + MTD (month the week ended in, through that Saturday). Other embeds: `ℹ️ Non-SGD receipts skipped`, `⚠️ Unparseable Receipts` (an S$ food receipt that didn't parse — likely a format change), `⚠️ Unreadable Email Dates`, `⚠️ Food Sync — ran out of time`, and `❌ FoodPanda sync couldn't finish this run` (short plain-English cause; full traceback stays in the cron log). Samples → **`TROUBLESHOOTING.md`**.

## Monthly payment reminder

On the **1st of each month** at 09:00 SGT (days 2–5 are a catch-up retry window), `foodpanda_payment_reminder.py` posts a plain-content reminder that **@-mentions you** (so it pings) to make payment for the **previous** month's food expenses, with that month's total from the sheet. Idempotent per month; if the sync on the month's last night didn't complete OK it adds a heads-up that orders may be missing:

```
<@470611394698412052> 🍔 Reminder: make payment for **Aug 2026** food expenses. Total **S$872.89**.
```

State: `~/.hermes/foodpanda_reminder_state.json`. Test flags: `--dry-run`; `--simulate-date YYYY-MM-DD` (**implies `--dry-run`**); `--force` (bypasses window/state — posts AND marks that cycle fired; never combine with a future `--simulate-date`).

**"How much did I spend on food in <month>?"** — run `~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/foodpanda_payment_reminder.py --dry-run --force --simulate-date <YYYY-MM-01 of the NEXT month>` (prints that month's total; posts nothing, writes nothing), or read the month tab (`'<Mon YYYY>'!A:F`) via `google_api.py sheets get`.

## More

- First-time setup / creating the cron jobs → **`SETUP.md`**
- Debugging a run (empty-date storm, non-SGD skips, "did it run?", catch-up, run deadline) → **`TROUBLESHOOTING.md`**
- Editing the scripts (provider config, flow internals, regexes, timeout budget, Discord retry rules, **gotchas — read before any edit**) → **`CLAUDE.md`**
- Incident / change records: `references/float-expiry-crash-2026-06-13.md`, `references/timeout-crash-2026-07-17.md`, `references/grab-provider-added-2026-07-18.md`, `references/tuneup-2026-07-18.md` (timeout hardening), **`references/tuneup-2026-09-28.md`** (weekly Sunday gap, migration cost, reminder hardening, Grab rides, POST de-dup)
