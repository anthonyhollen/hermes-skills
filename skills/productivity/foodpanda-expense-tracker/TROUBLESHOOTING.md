# Food Expense Tracker — Troubleshooting

Runtime diagnosis. Setup: `SETUP.md` · editing internals: `CLAUDE.md`.

## What the Discord output looks like

Daily combined per-month summary (provider-labelled; `🔄` = amount updated via a (provider, order ID) match, e.g. a refund):
```
🍔 Food Receipts Synced — Jul 2026
🔄 `2026-07-02` · *Sushi Express (White Sands)* (FoodPanda) · **S$85.00** _(updated)_
• `2026-07-07` · *Yuen Kee Dumpling (Pasir Ris Mall)* (Grab) · **S$34.40**
• `2026-07-08` · *KFC (White Sands)* (FoodPanda) · **S$42.50**

*Sheet updated — MTD total: S$161.90*
```
Sunday weekly report — the previous Sun–Sat week (combined, provider-labelled; MTD = the month the week ended in, through that Saturday):
```
🍔 Food Weekly Report — Jul 2026 (Jul 12 – Jul 18)
2026-07-15 · A Kitchen (Downtown East) (FoodPanda) · S$18.49
2026-07-18 · Yuen Kee Dumpling (Pasir Ris Mall) (Grab) · S$38.40

*Weekly Total: S$56.89*
**MTD Total: S$585.29**
```
Info/error embeds: `ℹ️ Non-SGD receipts skipped`, `⚠️ Unparseable Receipts`, `⚠️ Unreadable Email Dates`, `⚠️ Food Sync — ran out of time`, `❌ FoodPanda sync couldn't finish this run` (a short plain-English cause + a "will retry, nothing saved" note — the full traceback stays in the cron log, not Discord).

## "Did the sync run?" — check the logs when nothing arrived

**v1.7.0+:** Every handled failure posts an alert **and** exits non-zero, so `hermes cron list` shows the job red (`last_status: error`) — check that first. Silence in Discord is *usually* success, but not proof: a failed alert post (Discord down) or a lost summary post (a transient 502 isn't retried, to avoid duplicates) is also silent. A pending **fetch** failure (one receipt couldn't be downloaded) posts nothing — it's retried next run — but exits 1.

Read the latest run log to distinguish a crash from a legitimate zero-receipt run:

```bash
d=~/.hermes/cron/output/b3b0042bd9cf; cat "$d/$(ls -t "$d" | head -1)"
```

- `No new receipts to process.` → legitimate zero-state.
- A `Could not parse date ''` storm → upstream gws bug (below); also fires a `⚠️ Unreadable Email Dates` Discord alert.
- `run deadline exceeded` in the traceback, or a `⚠️ Food Sync — ran out of time` alert → the run's cumulative Google API time hit the internal deadline (`RUN_DEADLINE_SECS` = 300s) and stopped *before* the 600s cron SIGKILL (`cron.script_timeout_seconds`) so the alert could still post. State wasn't saved, so the next run retries cleanly. A normal run takes ~2s (nothing new) to ~10s (receipts to record); hitting 300s means Google was badly slow or hung — persisting means a genuine network problem (see the timeout incident record).

## Non-SGD receipts skipped (`ℹ️ Food Sync — Non-SGD receipts skipped`)

A Grab receipt in another currency (e.g. a trip abroad, `TOTAL Rp …`, all-Indonesian layout) has **no `S$`**, so it can't be summed into the S$ MTD. The sync skips it cleanly, notes it **once**, and advances that provider's watermark past it (so it isn't re-reported). This is expected, not an error. Tracking a non-SGD provider would need currency handling and probably a separate sheet/column.

## Empty-date storm — `Could not parse date ''` repeated many times

**This means the Gmail layer is broken, not the parser.** The search returns the right number of message **ids**, but every `date`/`from`/`subject` field is `""`. The parser rejects empty dates and skips them; it counts and alerts them. An empty-date email has no usable timestamp, so it can't pin the watermark: it's re-seen every run **only while it's inside the search window** (`after:<watermark − 1 day>`). In a total storm (every date empty) nothing advances, so all of them are picked up once the upstream bug is fixed; if only *some* are undated while other receipts keep syncing, the watermark moves on and they drop out of the window within ~a day — **fix the Gmail layer promptly and re-sync those manually** (widen the window by editing the provider's watermark in `~/.hermes/foodpanda_sync_state.json`, back it up first). *(Corrected 2026-09-28: this said they were always retried until fixed.)*

**Root cause (verified 2026-06-02):** the `gws` CLI on the gateway's PATH (`/home/amh/.npm-global/bin`) strips `payload.headers` when `metadataHeaders=[...]` is passed. Cron always hits the broken branch; an interactive shell without that dir on PATH uses the raw `googleapiclient` branch and the bug doesn't show. See the **google-workspace** skill pitfall.

**Diagnose / catch up** from a shell **without** `/home/amh/.npm-global/bin` on PATH (use the venv interpreter — bare `python` in an ssh shell is the system one):
```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/foodpanda_sync.py
```
(That shell takes `google_api.py`'s non-gws branch, which prints `No messages found.` for an empty search; since v1.7.0 the sync treats that as zero results instead of crashing.)

## Provider column / MTD migration

On the first v1.4.0 run, each existing tab is migrated: a **Provider** column is added (col F, existing rows backfilled as `FoodPanda`) and the MTD `=SUM` formula moves from **F to G**. `ensure_provider_column` is **self-healing and idempotent** — it re-derives the Provider column row-by-row every run and only writes when something differs, so a partial/interrupted migration self-corrects on the next run. It never relabels a row already marked `FoodPanda`/`Grab`.

If a Grab (or other) row ever appears in the **wrong columns**, it was written by the old `gws sheets append` path — appends now use an explicit `values.update` at a computed row, which avoids the table-detection column-shift on legacy tabs (empty D/E). See `references/grab-provider-added-2026-07-18.md`.

## Grab ride receipts

Grab emails taxi/car rides with the **same subject** as food orders (`Your Grab E-Receipt`: "Hope you enjoyed your ride! … Booking ID … Total Paid S$…"). Since v1.7.0 they're skipped **silently** (logged in the cron output as `Skipping non-food receipt`) and the watermark moves past them. Before that each ride raised a false `⚠️ Unparseable Receipts — the email format may have changed` alert (e.g. 2026-08-19, ids `1a019c7b89d5dbbc`, `1a01879685f954fe` — both rides; no food expense was missed). A ride is recognised only when the ride phrase is present **and** there's no `Order from:` merchant, so a food receipt is never skipped this way; a genuinely new Grab receipt type still raises the unparseable alert.

## Weekly report looks "late"

It runs **Sunday 09:05** and covers the previous Sun–Sat (last Sunday through last night) — every day of that window has been through its 23:30 sync, so nothing is missed; today's orders land in next week's report. (Until 2026-09-27 it reported the *current* Mon–Sun at Sunday 09:05, so Sunday's orders were synced only after that report and never appeared in any.) The window is computed from the run date, so a manual re-run on any day reports the last complete Sun–Sat.

## Operational notes

- **Gmail search caps at 50 results/run per provider** — fine for daily volume, but a backlog >50 (e.g. after an outage or a first run with an old `lookback`) silently truncates the **oldest** (the watermark then moves past them — no alert). Raise `--max` when backfilling.
- **`lookback`** (per provider) bounds which emails are fetched; the state file separately tracks each provider's last-synced timestamp for incremental sync.
- **MTD reads are lenient** — `parse_money` strips `$`/commas. The sync summary reads `G2` (falling back to summing col C); the weekly report sums the week's end month through its Saturday; the reminder sums col C of the previous month — so a currency-formatted cell won't produce `N/A`.
- **Legacy tabs** (pre-v1.1.0) lack the `Email ID` column / had different formatting; scripts fall back to content dedup `(provider, date, merchant, norm(amount))` + a manual column-C sum.

## Script crashed silently (no Discord message, exit code ≠ 0)

```
AttributeError: 'float' object has no attribute 'rstrip'
```

**Cause (v1.2.0 and earlier):** Google's auth library writes `expiry` as a Unix timestamp float to `google_token.json` when refreshing an expired token; `from_authorized_user_info()` calls `.rstrip("Z")` on it and crashes. Both scripts now have `_fix_token_expiry()` and a top-level crash handler.

**Recover:** re-run from any terminal — unprocessed receipts are picked up because state wasn't saved (the crash guard prevents it), and the run fixes the float expiry in-place:
```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/foodpanda_sync.py
```
