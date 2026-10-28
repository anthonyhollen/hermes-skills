---
name: cc-bill-review
category: productivity
version: 3.5.1
description: Review OCBC credit card statements (cards 5999 / 7008) — import the bill PDFs, flag high-value / foreign / annual-fee / new-merchant charges, handle "approve #…" replies, and send automatic payment-due reminders.
triggers:
  - "review my credit card bill"
  - "credit card statement review"
  - "cc bill review"
  - "parse credit card statement"
  - "card statement"
---

## ⚡ Agent Operating Contract (READ FIRST)

You are a **router**: run ONE script. For steps 1–3 below, its one-line stdout is
written for the user and IS your whole reply — send it exactly as printed, nothing
before or after; don't look into it (no session search, logs or file reads). The
scripts post to Discord themselves (to the current thread — no id needed). Never
hand-write a review, never paste merchant names, never query or edit the DB yourself.
Use the terminal tool (not `execute_code`) with a **300 s timeout** for every command below.

```bash
cd ~/.hermes/skills/productivity/cc-bill-review    # ALWAYS run from here
```

1. **Statement PDF(s) uploaded** → one command imports AND posts the review:
   ```bash
   python3 scripts/import_and_review.py --post <pdf1> [<pdf2> ...]
   ```
   Exit **3** → run the SAME command once more (safe: re-import replaces, delivered
   parts are de-duplicated) and reply with that run's line. Exit **4** → re-run it once
   with `/usr/bin/python3`. Any other exit → reply with the line. Never use `execute_code`.
2. **The user replies about merchants** ("approve #18 #53", "approve all", …) → pass
   their message verbatim on stdin, in a QUOTED heredoc (so the shell can't alter `$`,
   backticks or quotes):
   ```bash
   python3 scripts/approve.py --post - <<'CCBR_EOF'
   <the user's message, exactly>
   CCBR_EOF
   ```
   The script refuses (and tells the user) anything ambiguous — negations, "don't",
   two actions, questions, amounts, unknown words. Don't rephrase the reply for it.
   Whatever the exit code, reply with its line and never re-run it (a re-run would
   apply the reply twice or hide what the first run did).
3. **"Show me the unapproved / new merchants"** → `python3 scripts/review.py --merchants-only --post`
   ("approve all" afterwards approves exactly that list). **Re-show the latest
   review** → `python3 scripts/review.py --post`. Reply with its line, whatever the exit code.
4. **"Why is X flagged as new?"** → `python3 scripts/find_duplicate_merchants.py` and
   show the user its clusters + raw evidence; merging two names moves an approval, so
   the USER decides (recipe: `references/duplicate-merchant-sweep.md`).
5. **Reminders are fully automatic** (daily no-agent cron) — never run, pause or
   edit them yourself.

**Hard rules:** never auto-approve (the user is the sole source of truth); `ANNUAL FEE`
is always flagged and never approvable; never `DELETE FROM merchants` (it wipes
approvals); never write a review, a confirmation or merchant names into your reply
yourself (you drop the `\*` escapes in names like `SMP**JUNG` and corrupt the formatting).

## What the review flags

| Flag | Condition |
|------|-----------|
| 🔴 High-value | debit > S$100 (not system rows, not the annual fee — it has its own section) |
| 🌍 Foreign | country ≠ SG/blank, or a `FOREIGN CURRENCY` marker in the raw description (shows e.g. `SG · USD 10.00`) |
| 💳 Annual Fee | any `ANNUAL FEE` debit — always shown, call the bank to reverse |
| 🆕 New / 🔁 previously-seen | unapproved merchants on this statement (new = first ever seen this month) |

Every charge is shown — a same-day repeat charge (two coffees, or a double charge)
appears twice. System rows (payments, rebates, FX fees, NETS top-ups) and the
pre-rejected Italian holiday merchants (`reviewlib.HOLIDAY_NAMES`, hidden) never
appear in approval lists.

**Approval replies** (`approve.py`): `approve #18 #53` · `approve #9-12` · `approve all`
(= everything still unapproved in the list last posted in this thread) · `approve all new`
· `approve all 5999` · `approve all 5999 except #18` · `approve all backlog` (every month) ·
`unapprove #18` · `unapprove all` · `reject #18` (= keep flagged; it keeps showing) ·
`hide #99` (bank/system rows only, by id). After two separate uploads, `approve all`
covers the latest review only — approve the earlier one's ids explicitly. Anything left
unapproved keeps showing every month — that is the "remind me" state. Never use emoji
reactions.

## Reminders (cron `7b68c1ef7afe`, daily 09:00, no-agent, `deliver: local`)

`~/.hermes/scripts/cc_payment_reminder.py` posts to #alerts (`1499972245437550602`),
@-mentioning the account holder. Per card and cycle: one reminder 3 days before the due
date (sent late on due−2/−1 if that run failed or the statement came late) and one on
the due date (sent as **OVERDUE** up to 2 days later if missed). S$0 statements never
ping. Failures self-alert and turn the job red; the upload/approval confirmation then
shows 🚨 (it reads `last_status`/`failure_streak` from `~/.hermes/cron/jobs.json`).
A new statement turns a manual pause (`config.reminders_disabled = '1'`) back off.
**Never `hermes cron create`** (a duplicate double-sends) — edit with
`hermes cron edit 7b68c1ef7afe …`.

## Manual / maintenance

```bash
python3 scripts/selftest.py                                   # regression suite (all must pass)
python3 ~/.hermes/scripts/cc_payment_reminder.py --dry-run    # what the cron would send today
python3 scripts/reminder.py                                   # same, read-only
python3 scripts/review.py [--all|--card 5999|--month 2026-09] # print only — inside a Discord
                                                              # session this pauses "approve all"
                                                              # until a list is posted again
```

## References

| File | Read when |
|------|-----------|
| `references/developer-guide.md` | Changing any code: layout, flow, schema, normalization, parser quirks, delivery, testing |
| `references/duplicate-merchant-sweep.md` | A merchant is wrongly flagged new (OCBC renamed it) — verify + merge recipe |
| `references/parse-ocbc-errors.md` | A statement fails to parse / parser warnings |
| `references/merchant-approval-enhancement.md` | Planning a full DB rebuild (approvals must be saved first) |
| `references/reminder-lifecycle.md`, `references/row-num-migration.sql` | History only (pre-v3 reminder design; a one-time May-2026 migration) |
| `CHANGELOG.md` | Why a rule exists — every past bug and fix |
