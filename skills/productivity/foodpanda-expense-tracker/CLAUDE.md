# CLAUDE.md — Food Expense Tracker (editing guide)

Read before editing the scripts. Overview + I/O: `SKILL.md` · setup: `SETUP.md` · runtime diagnosis: `TROUBLESHOOTING.md`.

Multi-provider since v1.4.0 (FoodPanda + Grab). The scripts and dir keep the `foodpanda` name for cron continuity; internally they loop over a `PROVIDERS` list.

## Canonical vs live copies

Cron runs `/home/amh/.hermes/scripts/foodpanda_{sync,weekly_report,payment_reminder}.py`; the canonical copies are this skill's `scripts/`. **Edit → deploy to BOTH** — they must stay byte-identical, or the change won't take effect. Verify with `sha256sum`, then run the offline suite against the deployed copies (see *Testing*). `scripts/test_foodpanda.py` lives only in the skill dir.

## Adding / editing a provider

Providers live in the `PROVIDERS` list at the top of `foodpanda_sync.py`. Each entry:

```python
{
  "name": "Grab",                 # also the value written to the Provider column (col F)
  "sender": "no-reply@grab.com",
  "subject": "Your Grab E-Receipt",
  "lookback": "2026/05/01",       # Gmail after: cutoff for THIS provider
  "date_mode": "pickup",          # "field" = date_re captures YYYY-MM-DD; "pickup" = 'DD Mon YY'
  "date_re": r"(?:Pickup|Delivery) time:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{2})",
  "merchant_re": r"Order from:\s*(.+?)(?:Delivery location|Profile:|$)",
  "total_re": r"TOTAL \(INCL\. TAX\)\s*S\$\s*([\d,]+\.?\d*)",
  "total_re_fallback": r"TOTAL\s*S\$\s*([\d,]+\.?\d*)",   # optional; used only if total_re misses
  "order_id_re": r"Booking code\s+(\S+)",
}
```

The rest of the flow (dedup, sheet, Discord, watermark) is generic. Adding a provider is config-only **plus** confirming its receipt actually parses (fetch a real email and test the regexes before deploying). The `weekly_report` script has no provider list — it only reads the sheet — but add the provider name to its `KNOWN_PROVIDERS` set so its rows get a label.

## Key constants (in all three scripts unless noted — change in all three)

- `SHEET_ID = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"`
- `DISCORD_CHANNEL_ID = "1499972245437550602"` (the reminder can override it via `FOODPANDA_REMINDER_CHANNEL`)
- `SGT = UTC+8` (all date logic)
- `PROVIDERS` (sync only) — see above; per-provider `lookback` replaces the old single `LOOKBACK`. Grab also has `nonfood_re` (ride receipts → skipped).
- Timeout budget: `CALL_TIMEOUT` (sync 30, weekly/reminder 20) and `RUN_DEADLINE_SECS = 300` in all three (see the timeout gotcha).
- `KNOWN_PROVIDERS` (weekly only) — provider names to label; unknown values in col F are treated as unlabelled (tolerates un-migrated tabs).
- State: `~/.hermes/foodpanda_sync_state.json` → **per-provider** `{"providers": {"<name>": {"last_checked_email_ts": iso}}, "reported_skip_ids": [...]}`. `load_state()` migrates the old flat `{"last_checked_email_ts": ...}` schema → FoodPanda's watermark. `reported_skip_ids` dedups the parse/foreign alerts across runs (see Gotchas).

## Sheet layout (v1.4.0)

`Date(A) | Merchant(B) | Amount(C) | Email ID(D) | Order ID(E) | Provider(F)`, and **MTD summary in `G` (`G1="MTD Total"`, `G2==SUM(C2:C)`)** — moved out of F to make room for Provider. A blank spacer row 2 (A–E empty, only the summary populated) exists on some tabs; all row logic uses **real sheet row numbers**, not positional offsets, so spacer rows don't matter.

## Dependencies

- Gmail/Sheets via `~/.hermes/skills/productivity/google-workspace/scripts/google_api.py`; OAuth `~/.hermes/google_token.json` (needs `spreadsheets` scope).
- `~/.hermes/.env` holds `DISCORD_BOT_TOKEN`, read **directly with `open()`** (not `os.environ`).

## Sync flow (`foodpanda_sync.py`)

1. Load per-provider state.
2. **For each provider:** Gmail search (`--max 50`, via `run_gapi` → retries transients) with that provider's sender/subject and `after:<watermark.date − 1 day>` (or `lookback` on first run) — a narrow window so the search stays ~2s (see the search-window gotcha). `run_gapi` spawns `google_api.py` with `sys.executable`, and maps its non-JSON `No messages found.` (the non-gws branch's empty search) to `[]`.
3. Per message: read `id`/`date` with `.get()` (no id → skip; empty/unparseable `Date` → `date_failures++` + a `⚠️ Unreadable Email Dates` alert). `parse_email_date` accepts the RFC 5322 variants (`(UTC)`, `GMT`, no weekday) via `email.utils`. Skip if `email_ts <= that provider's last_checked`.
4. Fetch body (fetch failure → `fetch_failures`, holds that provider's watermark back, retried; the run then exits 1). `extract_expenses(body, provider)` returns a **status**:
   - `("ok", (date, merchant, total, order_id))`
   - `("nonfood", None)` — the provider's `nonfood_re` matches (a Grab **ride**: "Hope you enjoyed your ride") AND `merchant_re` doesn't; checked before the currency test (so an overseas ride isn't a "non-SGD" note). Skipped silently, advances the watermark.
   - `("foreign", None)` — no `S$` anywhere → a non-SGD receipt; recorded in `foreign_skips`, noted once (via `reported_skip_ids`), advanced past (can't be summed into the S$ MTD).
   - `("fail", None)` — S$ present but a field regex missed, a FoodPanda `Order date` that isn't a real date, or a merchant capture > `MAX_MERCHANT_LEN` (200 — the end label was missing) → `parse_failures`, alerted once (via `reported_skip_ids`), advanced past.
5. **Within-batch dedup by (provider, order_id)** — when a refund produces two emails for one order, keep the latest (highest `email_ts`). Keyed by provider so FoodPanda order ids and Grab booking codes can't collide.
6. Group by month → dedup vs sheet by **Gmail message id** (col D, provider-agnostic — ids are globally unique), **(provider, order_id)** (col E+F → update existing row's amount), content fallback `(provider, date, merchant, norm(amount))` for legacy rows with neither id.
7. Migrate every existing tab: `read_all_tabs()` reads every tab's `A1:G` in **one** in-process `values.batchGet` (it used to be 2 `google_api.py` spawns per tab — ~18 s at 9 tabs, growing monthly), then per tab `ensure_order_id_column(name, data)` → `ensure_provider_column(name, data)` on that snapshot (the first updates the snapshot's E1 after writing it, so a new/empty tab gets all headers in one run). A batch-read failure skips the migration (warning); a per-tab failure skips only that tab. Create missing tabs with the full `A:F` header + `G2` MTD formula.
8. **Updates** target the row's **real sheet row** (from `read_rows`, which returns `(sheet_row, date, merchant, amount, email_id, order_id, provider)`). **Appends** use `append_rows()` → a single explicit `values.update` at `next_data_row()` (computed from column A). **Do NOT use `gws sheets append`** — its table-detection shifts columns on legacy tabs with empty D/E.
9. **Verify writes** by reading back the last N rows for the new email ids; a failed read of an existing sheet aborts that month (never treated as empty → no duplicate re-append).
10. Post combined Discord summary + MTD; each line tagged with its provider. Updated rows get `🔄 _(updated)_`. If the run deadline has passed at the top of a month, post ONE `⚠️ Food Sync — ran out of time` alert for the remaining months and stop (`any_failure`).
11. Save state only if `not any_failure` (else exit 1). **Each provider's** watermark = `max(processed_ts)` (its recorded receipts + parse failures + foreign skips), never past that provider's earliest **fetch** failure.

## Weekly flow (`foodpanda_weekly_report.py`)

Runs **Sunday 09:05** and reports the **previous complete Sun–Sat week** (`week_range(today)` → `(start_sunday, end_saturday)` of the week before the one `today` is in, so it's independent of the run day). Until v1.7.0 it reported the *current* Mon–Sun at Sunday 09:05, so Sunday's orders (synced 23:30 Sunday) never appeared in any report — S$599 across 11 of 18 reports. (v1.7.0 briefly moved it to Monday for the previous Mon–Sun; v1.7.1 went back to Sunday with a Sun–Sat window at the user's request — same completeness.) Reads the month tab the week **ended** in (`A:F`) — **plus the previous month's tab when the week spans a boundary** (`read_month_rows`, which treats a missing tab as benign empty via the Sheets "Unable to parse range" 400). Merges, filters Sun–Sat SGT, and computes **MTD = that month's rows dated ≤ the week's Saturday** (rows with an unparseable date count, as in G2) — not the tab's `G2`, which would also count days after it. Posts a single combined report. A *genuine* read failure is alerted and exits 1.

## Monthly payment reminder (`foodpanda_payment_reminder.py`)

Third cron script. Fires days 1–5 of each month at 09:00 (`0 9 1-5 * *`; day 1 is the real fire, days 2–5 are the catch-up window if an earlier run failed). The daily 23:30 sync on the month's last night has already put the month's orders in the sheet (orders after 23:30 are deliberately not designed for — the user doesn't order that late; a catch-up sync was tried in 1.7.0 and removed in 1.7.2). `sync_covered_month_end()` reads `~/.hermes/cron/jobs.json` (the scheduler's own records — no CLI/PATH dependency): if no `foodpanda_sync.py` job has `last_status == "ok"` with `last_run_at` (its END time; naive = SGT) on/after the previous month's last day, the message gets a "the receipt sync didn't complete on the month's last night" heads-up line; unreadable jobs.json / no sync job → no caveat. Reads the PREVIOUS month's tab (`A:C`; sum of col C — verified equal to that tab's `G2`; a missing tab or zero rows = benign 'nothing to pay', marked fired and silent), posts a plain-content @-mention reminder (cc-bill-review style, NOT an embed) to Discord, then records the cycle.

- State: `~/.hermes/foodpanda_reminder_state.json` → `{"last_fired_cycle": "Aug 2026"}` (label = the month reminded ABOUT). Atomic tmp+rename write. Successful send → mark; failed send → alert + non-zero exit, NOT marked (next day in the window retries). Posted-but-state-write-failed → a specific "WAS posted" alert + exit 1.
- The POST carries Discord `nonce` = `fp-remind-<MonYYYY>` + `enforce_nonce`, so it may retry a 5xx safely (Discord returns the existing message within a few minutes). Residual: a read timeout after Discord created the message → reported as failed, and the next window day posts again (one possible duplicate, never a missed reminder).
- Env knobs: `FOODPANDA_REMINDER_CHANNEL` (default `1499972245437550602`; tests use the playground channel), `FOODPANDA_REMINDER_NOTIFY_USER` (default `470611394698412052`, empty = no mention), `FOODPANDA_REMINDER_NO_PING=1` (keep the mention text, suppress the ping — tests only). `allowed_mentions` allow-lists exactly the notify id.
- Test flags: `--dry-run` (print only; no post, no state), `--simulate-date YYYY-MM-DD` (pretend a date — **implies `--dry-run`** unless `--force` is also given; no sync caveat in previews), `--force` (bypasses window+state; posts AND marks that cycle fired — never combine with a FUTURE `--simulate-date`, or the real fire is skipped). `--dry-run --force --simulate-date 2026-09-01` = a safe "what was August's total?" query.
- Same timeout budget as weekly (20/300) and the same crash-alert conventions (`quick_alert` + `friendly_error`, full traceback → stdout, re-raise).

## Parser regexes

FoodPanda (`date_mode="field"`):
- `Order date:\s*(\d{4}-\d{2}-\d{2})`
- `Name:\s*(.+?)(?:Partner address|$)`
- `Order total.*?S\$\s*([\d,]+\.?\d*)` — non-greedy to the first `S$`; tolerates an intervening `S`. **Don't revert to `[^S]*`.**
- `Order number:\s*(\S+)`

Grab (`date_mode="pickup"`):
- `(?:Pickup|Delivery) time:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{2})` → `07 Jul 26`, parsed locale-independently via `MONTHS` (Grab has no ISO order date; we log the pickup date, per the product decision).
- `Order from:\s*(.+?)(?:Delivery location|Profile:|$)`
- `TOTAL \(INCL\. TAX\)\s*S\$\s*([\d,]+\.?\d*)` with fallback `TOTAL\s*S\$\s*([\d,]+\.?\d*)` — **group-order receipts have no "(INCL. TAX)" line**, only a bare `TOTAL S$`. Case-sensitive `TOTAL` so it never matches `Subtotal`.
- `Booking code\s+(\S+)` — no colon after the label (e.g. `A-9IBAAN9WWE6DAV`).

## Gotchas — don't regress these

- **SGT, not UTC** cron. Verify `next_run_at` after a change.
- **No `shell=True`** — `run_gapi`/Gmail search use list-form `subprocess.run`. Keep it list-form.
- **`.env` not exported** — read directly.
- **Tab names locale-independent** — hardcoded `MONTHS` list (`May 2026`), shared by all three scripts. Don't use `strftime("%b %Y")`. Grab's pickup-date parse also uses `MONTHS`, not `%b`.
- **Provider column = col F; MTD moved to col G.** `create_month_sheet` writes the `A:F` header via `values.update` (not append) + `G1:G2` MTD. Amounts in col C must stay numeric (not strings) or `=SUM` breaks. MTD reads are lenient (`parse_money` strips `$`/commas) and fall back to summing col C.
- **`ensure_provider_column` is self-healing + idempotent** — it computes the desired F column ROW-BY-ROW (real data row → existing provider, else `FoodPanda`; blank/spacer row → empty) and only writes when something differs. This repairs earlier off-by-one backfills and stray F values. **Never relabels a row already reading `FoodPanda`/`Grab`.** The earlier blind `F2:F{n+1}` block backfill was buggy on tabs with a spacer row 2 — don't reintroduce it.
- **Appends via `append_rows()` (explicit `values.update` at `next_data_row()`), never `gws sheets append`** — the append tool's table detection shifts writes into the wrong columns on legacy tabs (empty D/E). `next_data_row` is computed from **column A only** (ignores the MTD in G) so it never anchors on the summary.
- **Dedup keys = message id (col D, global) + (provider, order id) (cols F+E) + (provider, content) fallback.** Order-id dedup enables refund updates; scoping by provider prevents cross-provider id collisions.
- **State is per-provider and gated.** Any per-month failure aborts the save; each provider's watermark never passes that provider's fetch failure. `parse_failures` and `foreign_skips` advance the watermark via their `email_ts`. Atomic write: `.tmp` + `os.rename()`.
- **`reported_skip_ids` (in state) dedups the parse/foreign alerts across runs.** A deterministic skip that sits *newer* than a still-retryable fetch failure can't advance the watermark past it, so without this it re-alerts every run. Alerts fire only for msg-ids not in the persisted set, which is saved **early** (before the `any_failure` guard) so the "report once" holds even if the run later aborts. Bounded to the last 500 ids (skips are rare). Don't move the early save after the `any_failure` return.
- **Non-SGD receipts are skipped, not errored.** `extract_expenses` returns `("foreign", None)` when `"S$"` is absent (checked first, before the S$/English-only field regexes). They advance the watermark and get an `ℹ️` note once. Tracking a non-SGD provider needs currency handling + a separate sheet/column — it can't sum into the S$ MTD.
- **Timeout budget — every blocking call is bounded and cumulative retries can't hit the cron SIGKILL** (a SIGKILL posts NO alert). The watchdog is the GLOBAL `cron.script_timeout_seconds` in `~/.hermes/config.yaml` — **600 s** (it was assumed to be ~120 s until v1.7.0; that figure was the OS TCP timeout, not the scheduler). `socket.setdefaulttimeout(CALL_TIMEOUT)` bounds in-process googleapiclient/httplib2 calls; `run_gapi`'s `subprocess.run` has `timeout=CALL_TIMEOUT`; `retry()` is gated by a run-global `_run_deadline` (`time.monotonic() + RUN_DEADLINE_SECS=300`, set at the top of `main()`) **pre-start and pre-sleep**. Discord posts are NOT deadline-gated (so a slow run still alerts) but become single-shot (≤15 s) past it, and the sync's month loop stops at the deadline with ONE alert. Simulated worst case (slow Google + Discord failing slowly, any number of months): **360 s**. **If the global watchdog is ever lowered (e.g. back to 120), lower `RUN_DEADLINE_SECS` to keep `RUN_DEADLINE + CALL_TIMEOUT + ~60 s < watchdog`.** (`CALL_TIMEOUT` was 20 in v1.5.0 and false-tripped the ~18s Gmail search — measure a call's real cold latency before setting its timeout.)
- **Discord POSTs (`discord_send`) retry ONLY when Discord definitely didn't create the message** — HTTP 429 (honours `retry_after`, capped 10 s) / 503, or a `URLError` (connect/send). **Never after a read timeout** (urllib raises a bare `TimeoutError` from `getresponse()`, not a `URLError`), and not on 500/502/504 — the message may exist and a re-send duplicates it (the old `retry()` wrapper re-POSTed on "timed out" → double pings). Exception: `retry_ambiguous=True` for a payload with a `nonce` (the reminder). Known cost: a transient 502 on the daily summary loses that notification (rows are in the sheet; stdout logs it). Keep `HTTPError` caught before `URLError` (it's a subclass).
- **Exit codes:** every `main()` returns an int — 0 = OK/benign (nothing new, ride-only, outside the window); 1 = a handled failure (alert already posted) or a pending fetch failure. For `deliver: local` that only turns the job red in `hermes cron list` (no push), which is why each failure path also posts.
- **Google token float expiry:** when google-auth refreshes, `expiry` in `google_token.json` can become a Unix-timestamp float, which `from_authorized_user_info()` chokes on. `_fix_token_expiry()` converts it — copy that into any new Google API consumer (see `references/float-expiry-crash-2026-06-13.md`).
- **Unreadable Date headers are NOT held back.** A watermark hold was tried in the v1.7.0 tune-up and reverted: a permanently-undated email froze the provider for ~50 receipts (daily alerts, 20× the fetches, deleted rows re-added). Such an email is re-seen only while inside the `after:<watermark − 1 day>` window; the `⚠️ Unreadable Email Dates` alert is the signal to fix the Gmail layer promptly.
- **The Gmail search window follows the watermark, not the fixed lookback.** `after:<watermark.date − 1 day>` (falls back to `provider["lookback"]` on first run / a bad watermark). The loop already skips everything `≤ watermark`, so this returns the SAME messages — it just stops hydrating the full `--max 50` of historical receipts every run (a wide `after:lookback` search took ~16–25s and false-tripped `CALL_TIMEOUT`; the narrow window is ~2s). The 1-day overlap is covered by message-id dedup. Don't revert to `after:{lookback}`.
- **Crash handler posts a SHORT, plain-English summary via `friendly_error(e)` + `quick_alert()` — NOT a traceback dump.** `friendly_error` maps common causes (run deadline / timeout / 429 / 503 / auth) to a readable line and falls back to `"<ExcType>: <first line>"`; the **full traceback goes to stdout** (the cron log) only. `quick_alert` is a single `urlopen(timeout=8)`, NO retry — a retry-storm alert fired late in a run can itself be SIGKILLed, losing the sole failure signal on a `deliver: local` job. Keep the crash alert single-shot, bounded, and human-readable.
- **`san()` sanitizes the merchant in Discord output** (neutralizes backticks / `@` / `](` / `://`) — a merchant name is provider-controlled text; don't interpolate it raw into the embed.
- **`norm()` is currency-tolerant (via `parse_money`)** so a legacy currency-formatted amount cell (`$30.32`) reaching the content-fallback dedup doesn't crash `float()`. On plain numerics it returns the identical string, so dedup equality is unchanged.
- **`_parse_pickup_date` validates via `datetime(...)`** (returns None on an impossible date) and the Grab `date_re` ends with `(?!\d)` so a 4-digit year can't silently truncate to the wrong year — both degrade to a clean parse-failure instead of a wrong tab / a crash.
- **Top-level crash handler** — `main()` wrapped in try/except that posts a `friendly_error()` summary via `quick_alert()` (full traceback → stdout) before re-raising. Keep it on every script.
- **`retry()`** — 3× backoff on lowercased `429`/`503`/`ratelimit`/`timeout`/`transient`/`timed out`. Keep patterns lowercase and `str(e).lower()`. `get_all_sheets()` and all direct `.execute()` calls stay wrapped in `retry()`. The `_run_deadline` gate (above) bounds it.
- **Weekly reads two tabs on boundary weeks** and treats a missing tab as benign — don't simplify to a single month read. It reports the PREVIOUS complete Sun–Sat week regardless of run day — never change it back to "the current week" (that's what dropped every Sunday's orders).

## Testing

```bash
# Offline regression suite (no network, temp HERMES_HOME, Google/Discord stubbed) — one check per
# fixed bug. Run after every edit, against the skill-dir copy AND the deployed copies:
python ~/.hermes/skills/productivity/foodpanda-expense-tracker/scripts/test_foodpanda.py
FP_SCRIPTS_DIR=~/.hermes/scripts python ~/.hermes/skills/productivity/foodpanda-expense-tracker/scripts/test_foodpanda.py

# NOT a dry run — migrates tabs, appends + posts if new receipts exist
python /home/amh/.hermes/scripts/foodpanda_sync.py
python /home/amh/.hermes/scripts/foodpanda_weekly_report.py

# Reminder — SAFE preview (no post, no state write); swap the simulated date as needed
python /home/amh/.hermes/scripts/foodpanda_payment_reminder.py --dry-run --simulate-date 2026-10-01
cd ~/.hermes/skills/productivity/google-workspace/scripts && python google_api.py gmail list  # auth
```

A pre-change sheet backup lives at `~/.hermes/foodpanda_sheet_backup_20260718.json` (values-only snapshot from the Grab rollout).
