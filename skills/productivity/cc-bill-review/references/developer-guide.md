# cc-bill-review — developer guide

Long-form maintenance notes (relocated from the per-command `CLAUDE.md`, 2026-10-02,
and updated for v3.5.0). `SKILL.md` is the agent's contract; this file is for editing the
code. Why each rule exists: `CHANGELOG.md`.

## Layout

```
scripts/
  parse_ocbc.py           PDF parser, SQLite schema + migrations, normalize/canonicalize,
                          import (process_pdf), cron-health check (read-only)
  reviewlib.py            SINGLE source of truth for every flag query + rendering, and the
                          "last shown" record that `approve all` acts on
  import_and_review.py    ENTRYPOINT on upload: import PDF(s) + post the review (--post)
  review.py               review / merchant list (print, or --post)
  approve.py              ENTRYPOINT on approval replies (strict fail-closed grammar)
  discord_post.py         byte-safe Discord delivery (session target, ≤2000-unit chunks,
                          nonce + enforce_nonce, retries, 90 s deadline for interactive posts)
  reminder.py             reminder windows: due_messages() / mark_fired() / upcoming() /
                          reminder_confirmation(); read-only CLI
  find_duplicate_merchants.py   read-only same-shop sweep with raw evidence
  migrate_identities.py   re-normalize every stored row + fold changed identities, approvals
                          kept (run once per normalizer fix: bump MARKER — v4 = v3.5.0,
                          v5 = v3.5.1 footer-bleed fix + Coffee Hive merge)
  import_all.py           bulk re-import of PDFs passed on the command line (maintenance)
  debug_parse.py          dump a PDF's text lines (maintenance)
  selftest.py + selftest_regressions.py   the regression suite (run after ANY change)
  cc_review.db            the live DB
~/.hermes/scripts/cc_payment_reminder.py  no-agent cron wrapper (imports reminder + discord_post)
```

Stack: Python 3.11 (system `/usr/bin/python3` has `pymupdf` in `~/.local`; the gateway venv —
which runs the cron wrapper and `execute_code` — has no pymupdf), stdlib `sqlite3`. Run from the
skill dir; `DB_PATH` resolves relative to `scripts/`.

## Import (process_pdf)

- Parse first, write second: `read_pdf_text` → `detect_card_and_period` → `parse_statement` →
  `validate_parsed_data` (all may raise `ParseError`; nothing is written), then ONE `with conn:`
  transaction does the pre-delete of the (card, month) + insert + merchants UPSERT. A failure
  leaves the old rows. Every call has an `imports` audit row (`in_progress` → `ok` /
  `parse_error` / `write_error`; interrupts included).
- A missing pymupdf raises `ParseError` with the remedy; `import_and_review.py` pre-checks it
  (exit 4, nothing imported).
- `strip_glued_total()`: the PDF text has the statement's TOTAL AMOUNT DUE figure right after
  the last transaction, which used to be appended to its description (18/18 statements). Only
  that exact trailing figure on the LAST transaction is removed.
- Column layout: `NAME CITY    CC` — a ≥3-space gap before a valid ISO code sets the country
  (description unchanged). A 2-space gap is a truncated name (`HUGGS COFFEE PTE  LT`), not a
  country.
- Warnings (shown in the review): dates outside the statement window (the check resolves DD/MM
  with `parse_date`), unknown country codes, a single debit > total due, and the
  **reconciliation check** `previous total + all transactions == total due` (held to the cent on
  every real statement — a mismatch means the parser missed or misread a line).
- New cycle (`year_month` > the card's latest): clears that card's reminder markers and turns a
  manual pause (`reminders_disabled='1'`) back off — the user asked for "next reminder set when I
  share my statement" (2026-05-22). The notice is remembered per (card, cycle) in
  `config.reminders_resumed_{card}` so a re-run renders an identical review.
- It never touches the cron job. (≤ v3.4 ran `hermes cron resume` on every import; resume_job()
  recomputes next_run_at from NOW, so an import in the 09:00 dispatch window skipped that day's
  run. Removed in v3.5.0.)

## Merchant identity

1. `normalize_merchant()` — text cleaning: footer bleed (`strip_footer_junk`; the last row on
   a page gets the page footer glued on — the strip must KEEP that row's city, or the shop gets a
   second identity: #108 vs #109, fixed v3.5.1), the
   `FOREIGN CURRENCY XXX n.nn` segment (billing currency, not the name — one merchant, one
   identity), leading `-NNNN` codes, ISO country tokens except the keep-list `{MP, LA}`, numeric
   tokens, merchant-specific rules (SPOTIFY, PARKING.SG, M1LTD, WATSONS).
   ⚠️ Any change here changes stored identities and so approvals: prove **drift = 0** (re-normalize
   every stored `merchant_raw`, compare to `merchant_normalized` — `selftest.py` does this on the
   live DB) or bump `MARKER` in `migrate_identities.py` and run it in the deploy (plan, then
   `--apply`: snapshot, one transaction, approvals max-merged, verified before commit).
2. `canonicalize_merchant()` — card maps (`_CARD_CANNONICAL_MAP_5999/7008`, exact) win, then the
   cross-card substring map (`_CROSSCARD_CANONICAL_MAP`). This is where an OCBC rename is fixed
   (`references/duplicate-merchant-sweep.md`). The `CANNONICAL` typo is kept to avoid churn.
- The leading `-NNNN` of a raw description is a payment-channel code shared by many merchants
  (`-9113` covers 25 shops) — never evidence of "the same shop".
- `merchant_uid` (`#id` in reviews): assigned once; a high-water mark (`config.next_merchant_uid`)
  means a deleted row's id is never reused.

## Schema invariants

- Tables: `transactions`, `statements`, `merchants`, `config`, `imports`. `init_db()` is the
  schema source of truth; migrations are additive (`config.schema_version`).
- `transactions.row_num` numbers identical (date, merchant, amount) rows of one statement. Those
  are **real, billed repeat charges** (two coffees; reconciliation proves it) — reviews show every
  one (v3.5.0; the old `row_num = 0` display filter hid double charges).
- `merchants.approved`: `0` unapproved, `1` approved, `-1` hidden (system rows, the Italian
  holiday merchants). The UPSERT omits `approved`, so re-imports keep approvals. A re-import also
  refreshes the counts of merchants that lost rows (those with 0 rows drop out of the backlog).
  **Never `DELETE FROM merchants`** (wipes approvals; see `merchant-approval-enhancement.md`).

## Review (reviewlib)

- High-value: debit > `REVIEW_AMOUNT_THRESHOLD` (100), not system rows, not ANNUAL FEE (own
  section). Foreign: country ∉ {SG, ''} OR `FOREIGN CURRENCY` in `merchant_raw`; the line shows
  the FX detail. Annual fee: `%ANNUAL FEE%` debits.
- Approval lists = unapproved merchants on the statement (LEFT JOIN — a txn whose merchants row
  is missing still shows, as `#?`), minus system rows, ANNUAL FEE, and `HOLIDAY_NAMES` (exact
  canonical names — substrings like `%TPER%` also hid SPORTPERFORMANCE / OUTPERFORM).
- `esc_md()` escapes `\ * _ ~` backtick (OCBC masks names with `**`) and defangs `](`, `://`,
  `||`, `<@ <# <t: <a: <:`. Pings are blocked server-side too (`allowed_mentions {"parse": []}`).
- `render_statement_ex()` returns (lines, shown); the multi-card callers print the approve hint
  once.

## "Last shown" — what `approve all` acts on

`config.last_shown_merchants` = `{source, at, target, merchants:[{uid, new}], blocked?, carry?}`.
Written by `import_and_review.py --post` and `review.py --post`: `blocked="posting in progress"`
BEFORE the post, the shown list after a full post, `blocked=<reason>` after a failed/partial post
or a print-only run inside an agent session; a busy DB on any of these writes is exit 3, never a
crash. Lists are NOT merged: after two separate uploads `approve all` covers the latest review
only (a merge re-approved merchants the user had just un-approved — gate lap 3; simplified away).
`approve all` refuses when the record is blocked, malformed, from another thread (or has no
thread), or older than 14 days (exact); with no record at all it falls back to the latest
statement per card. The thread check uses the session even without `--post`. All-modes skip hidden rows and only touch rows currently at the opposite state; holiday
rows never change; ANNUAL FEE is never approvable/hideable. `reject` = keep flagged (0).

## Delivery (discord_post)

Target = `HERMES_SESSION_KEY` parts[4] (the thread), over any explicit id (agents pass the
triggering MESSAGE id; it equals the thread id only for the thread-starting message);
`--force-target` lets an explicit id win. Chunks ≤ 2000 UTF-16 units (emoji count 2) on
paragraph → line → hard boundaries. Nonce = hash(target, seed, index, chunk) + `enforce_nonce`:
`import_and_review` uses a deterministic seed (a re-run dedupes what already landed —
`already_there`), `approve.py` / `review.py` a per-run salt (a repeated reply must post again).
Retries: 429 ≤ 10 s; 5xx / timeout / network ≤ 2 more attempts ONLY with a nonce; any 2xx is
success. Interactive posts have a 90 s WALL-clock deadline: checked before each message and
retry, and each request runs on a daemon thread joined with the remaining budget (urllib's
timeout is per socket op and per resolved address — 5 silent Discord addresses took 5 × 20 s).
The cron wrapper is not deadline-limited (600 s watchdog). Token from `~/.hermes/.env`, never argv/
stdout. Exit codes:
- 0 ok;
- 1 a PDF failed, the scripts failed to load, or an approve/review crash;
- 2 usage / empty input / nothing to post to / refused;
- 3 not delivered, DB busy, or an import crash;
- 4 no pymupdf.

Only the IMPORT's 3 means "re-run the same command once" (re-import replaces, delivered parts
dedupe). For `approve.py` (3 = DB busy, saved but the confirmation wasn't posted, or nothing
changed and the post failed) and `review.py`, the agent replies with the line and never re-runs.

**stdout contract (v3.5.1):** an entry script's stdout (with `--post`) is ONE line written for
the user — the agent sends it as its whole reply. No thread ids and no instructions to the
agent: a trailing "Relay this; add nothing." (v3.5.0) made a deepseek-flash agent spend ~6 min
and 59 tool calls investigating where the instruction came from (session search, state.db,
the vault, /tmp), all visible as tool progress in the user's thread, then echo it to the user.
- Every reply goes through `reviewlib.say()` (one line, whitespace collapsed); delivery errors
  through `reviewlib.brief()` (HTML stripped, capped). The only bare `print` is the print-only
  review body.
- Nothing goes to stderr: the terminal tool hands stderr to the agent too. Target notes, busy-DB
  details and crash tracebacks go to `skill.log` (if it can't be opened: a NullHandler inside an
  agent turn — stderr only for cron/manual runs; `logging.raiseExceptions = False`; MuPDF's
  diagnostics go to skill.log).
  Each script has a top-level crash guard and a guarded import that print one line.
- What the agent does on a failure is keyed by the EXIT CODE in SKILL.md.
- Tests (`_v351`):
  - a static scan of EVERY string literal in the entry scripts (f-strings with `{holes}`,
    docstrings excepted) against `_AGENT_TALK`, plus a ban on stderr writers (`sys.stderr`,
    `print_exc`, `warnings.warn`) and on bare prints;
  - a 27-path matrix (success, re-run dedupe, refusal, post failure, HTML 503, busy DB, crash,
    no session, empty input, typo / merged / already-approved id, no-op post failure) asserting exit code,
    exactly one line, no snowflake, no agent talk and empty stderr;
  - the `__main__` crash guards (RuntimeError + Ctrl-C), a broken import and an unwritable
    skill.log, run as real subprocesses.
- Merged ids: `migrate_identities.py` records `{old: kept}` in config `merged_merchant_ids`
  (`approve #18` → "skipped #18 (merged into #109 — use that id)") and repoints the
  last-shown list at the kept ids.

## Reminders

`due_messages(today)`: per card (skip total ≤ 0), 3-day reminder when due−3 ≤ today ≤ due−1 and
`reminded_3day_{card}` ≠ cycle; due reminder when due ≤ today ≤ due+2 (`OVERDUE` text after the
date) and `reminders_fired_{card}` ≠ cycle; `reminders_disabled='1'` silences all. The wrapper
sends each with a nonce per (card, kind, cycle), marks it fired right after its own send, alerts
(@-mention + unsent text) on failure and exits 1; a broken skill import or a crash also alerts.
`upcoming(now)` mirrors those windows from the next 09:00 run (still ahead until 09:02) — the
confirmation shown after imports/approvals. `verify_cron_health()` reads `~/.hermes/cron/jobs.json`
(CLI only if unreadable): unhealthy on paused/disabled, `last_status=error`, `failure_streak>0`,
state error/failed/completed or no `next_run_at`, `next_run_at` > 2 h overdue, or no run in 50 h;
a schedule other than `0 9 * * *`; `determinable=False` only when the state can't be read at
all (soft note, no 🚨). The skill never
pauses/resumes/edits the cron; `hermes cron edit 7b68c1ef7afe …` for config changes, never
`create`.

## Facts the code relies on

- System rows, never merchants: `%PAYMENT%INTERNET%`, `%NETS FLASHPAY TOPUP%`, `%CASH REBATE%`,
  `%CCY CONVERSION FEE%` (`reviewlib._SYSTEM_LIKE`). Credits (`amount <= 0`, e.g.
  `ANNUAL FEE REVERSAL`) are never flagged.
- OCBC layout: TOTAL AMOUNT DUE is below the transaction list (the header's totals are the
  credit limit / minimum); transaction dates are indented ≥ 2 spaces; `SINGAPORE` inside a
  description is a city, not a stop word.
- The DB is in the weekly Hermes backup (cron `999de4b21eec`).
- The cron wrapper keeps a PEP-668 shim that puts `~/.local/lib/python3.11/site-packages` on
  `sys.path` (the venv drops the user site); nothing it imports needs it any more (pymupdf is
  imported lazily, only for PDFs) — harmless, kept.
- Disaster recovery ONLY if job `7b68c1ef7afe` is gone (a second job double-sends every
  reminder): `hermes cron create "0 9 * * *" --name "CC Payment Reminder (both cards)"
  --script cc_payment_reminder.py --no-agent --deliver local`, then put the new id in
  `config.cron_job_id` (`python3 -c "from scripts.parse_ocbc import set_config;
  set_config('cron_job_id','<id>')"`). (Until v3.5.0 SKILL.md pointed to a "create command in
  CHANGELOG.md" that was never there.)

## When changing code

- Run `python3 scripts/selftest.py`. On the Pi it also drives the cron wrapper
  (`~/.hermes/scripts/cc_payment_reminder.py`, or `CC_REVIEW_WRAPPER=<path>`) on a sandbox copy.
  It isolates `db_snapshots/` and `skill.log`; its live-data section is read-only and fails if
  normalizer drift ≠ 0, a transaction lacks a merchants row, or a statement total is glued
  onto a name.
- Under both interpreters for anything the cron wrapper imports (`reminder.py`, `discord_post.py`,
  `parse_ocbc.py` minus PDF parsing).
- After removing code, check nothing still uses it (an index-slice removal once deleted a live
  function along with the dead one).
- Approval grammar (`approve.py`): ONE leading verb, then ids / ranges (`-`, `–`, `—`, `to`) /
  `all [new] [card…] [except ids]` / `all backlog [except ids]`; fillers (please, thanks, ok…)
  ignored; only the gateway's prefixes are stripped (`[Triggering message id: …]`,
  `[System note: …]`, the user's `[name]` tag — `CC_REVIEW_NAME_TAGS`, default `ham`);
  negations (checked before stripping), questions, amounts, two verbs, `hide all`, `reject all`
  and unknown words are refused with nothing changed. The user's real replies are regression
  cases in `selftest_regressions.py`.
