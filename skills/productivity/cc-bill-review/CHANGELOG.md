# cc-bill-review — Changelog

Per-version history and the provenance behind each rule. `SKILL.md` keeps only
what the agent needs to *act*; the war stories live here.

---

## v3.5.1 — 2026-10-04 — Follow-up to the first live run

Review of the first real upload under v3.5.0 (2026-10-03 21:24, thread
1555933537457274922): both statements imported and the review posted in 2 s;
"Approve all new merchants" approved all 11. Three problems found:

- **stdout spoke to the agent.** Every success line ended "Relay this; add
  nothing." The agent (deepseek-flash) treated that as an instruction of unknown
  origin. It spent ~6 minutes and 59 of the session's 62 tool calls investigating
  it (session search, state.db, the Obsidian vault, tune-up kits left in /tmp), all
  visible as tool-progress messages in the user's thread. It then echoed "Relay
  this line; add nothing." to the user. The post-session curator also created a
  global `script-relay` skill about it, which was archived at the user's request.
  Now:
  - Every reply line of `import_and_review.py` / `review.py` / `approve.py` is
    written for the user and goes through `reviewlib.say()`: one line, no thread
    ids, no "tell the user…" / "re-run…" / "do NOT paste…".
  - Delivery errors go through `brief()`, so HTML is stripped.
  - Nothing goes to stderr (the terminal tool shows stderr to the agent too).
    Target notes and tracebacks go to skill.log, and each script has a top-level
    crash guard that prints one line.
  - The agent's failure handling is keyed by exit code in SKILL.md: import 3 → run
    it once more, import 4 → `/usr/bin/python3` once, approve/review → reply with
    the line and never re-run.
  - A no-op approval no longer says ✅: `approve #99999` →
    "⚠️ Nothing changed — skipped #99999 (no such id)".
- **Footer bleed split a merchant.** The last row on page 2 of the 5999
  statement gets OCBC's page footer glued on. The strip ate that row's city too,
  so the row normalized without "SINGAPORE" and got its own identity: #108
  `COFFEE HIVE INNOVI` next to #109 `COFFEE HIVE INNOVI SINGAPORE`, a false "new
  merchant". The map had hand-patched this for three shops (WOK PEOPLE, COFFEE
  HIVE - INNOV, SHOPEE MP). The pattern now keeps the city (lookbehind). Checked
  against all 466 stored rows, only two identities change: #108 folds into #109,
  and #92 `DAPUR PENYET` becomes `DAPUR PENYET SINGAPORE` (id and approval kept).
- **User merge:** #18 `COFFEE HIVE - INNOV` (OCBC's descriptor until the Sep 2026
  statement) merged into #109, which now has 61 transactions and is approved.
- `migrate_identities.py` is now the general "normalizer changed" repair: bump
  `MARKER` (v5) and run it in the deploy. It also records merged ids in config
  `merged_merchant_ids` (so `approve #18` answers "merged into #109 — use that id")
  and repoints the last-shown list at the surviving ids.
- Gate: 3 independent reviewers (agent contract, identity/migration,
  adversarial), then a lap-2 adversarial reviewer on the fixes. No HIGH findings.
  Their MED/LOW findings are fixed above, plus:
  - skill.log's fallback was stderr. Inside an agent turn it is now a
    NullHandler (cron still gets the stderr warning in its job output), with
    `logging.raiseExceptions = False`.
  - MuPDF's own diagnostics go to skill.log instead of stderr.
  - Imports are guarded: a broken imported module still prints one line and
    logs the traceback. A syntax error in the entry script itself still can't.
  - Empty input from the agent (no PDF paths, an empty reply) gets a line
    written for the user instead of a usage message.
  - A no-op approval whose post failed said "Saved".
  - `brief()` ate `<urlopen error …>` reasons.
  - `say()` masks any Discord id.
  - The merged-id map tolerates a bad entry, and folded list entries keep "new"
    if either was new.
  - A latent migration bug: renaming a row onto a same-day, same-amount charge
    of the merchant it folds into breaks the `(…, merchant, amount, row_num)`
    unique index of a fresh schema. row_num is now parked on negatives first.
    The live table was rebuilt without that index, and every live row_num is
    identical after the run.
- A lap-3 reviewer then found a test time bomb: the fake statement was
  hard-coded to Nov 2026, which would have failed once the real one was
  imported. It now uses the month after the latest real statement.
- Lap 4 was a self-probe: the reviewers' scenarios (paused reminders, a Nov
  statement already imported) and the full mutant set re-run on the final code.
- Suite: 209 → 296 checks. The crash guards, a broken import and an unwritable
  skill.log run as real subprocesses. 45 mutants: 44 killed, 1 equivalent
  (`init_db()` sets the id mark before the migration does).

---

## v3.5.0 — 2026-10-02 — Tune-up (bug hunt + delivery + reminders + docs)

Full hermes-skill-tuneup pass: Phase 0 on the live Pi copy (selftest 39/0 system
python, normalizer drift 0/152, archived cron runs, 17 real agent sessions exported
from state.db), 4 blind reviewers (control-flow, data-shape, usage forensics,
delivery) + an adversarial refuter, then a terminal gate over the cumulative diff
that ran **4 laps** (laps 1–3 each found holes in the previous lap's fixes; lap 3's
fix was to REMOVE a list-merging heuristic lap 2 had added; lap 4 self-probed by
re-running lap 3's harnesses + mutation sets). Four behaviour decisions were the
user's (marked ★). Suite: 39 → 209 checks, every fix mutation-checked.

**Approvals (`approve.py`) — fail CLOSED**
- The parser failed open: "approve all except #18" approved #18, "approve
  everything except #5" the whole backlog, "don't approve #18" / "not #18, approve
  the rest" approved #18, "approve #18 and hide #99" approved #99, prose numbers
  and `08-27` dates became ids. Now: one leading verb; ids / ranges / `all [new]
  [card] [except ids]` / `all backlog [except ids]`; anything else (negations,
  questions, 2 verbs, amounts, unknown words, `hide all`, `reject all`) is refused
  with nothing changed and the user told how to phrase it. The user's 7 real
  replies (May–Sep) all still work.
- ★ `reject` = keep flagged (0); it used to HIDE the merchant (-1) forever.
- `approve all` acts on exactly the list last POSTED in this thread (recorded
  only after a full post; blocked while posting / after a failed post / a print-only
  run in a session; refused from another thread or when > 14 days old). It used to
  use "latest statement per card", which included a card the user hadn't uploaded.
- Hidden rows never change in all-modes; holiday rows never change; the reply is
  passed on stdin (quoted heredoc — the old double-quoted template let bash turn
  `S$13.50` into `S3.50` → id 50).

**Delivery (`discord_post.py`, the three `--post` scripts)**
- Reviews ran 1,222–1,913 UTF-16 units in normal months and ~3,050 in Jan 2026;
  `content[:2000]` sliced code points, so an over-limit review was rejected (50035)
  and the body fell back to the agent — the byte-unsafe path. Now split into
  ≤2000-unit messages on paragraph/line boundaries.
- The agent passed the triggering MESSAGE id as `<thread_id>` in both live uses
  (works only for a thread-starting message). The scripts now post to the session's
  thread (`HERMES_SESSION_KEY`) themselves; `--force-target` to override.
- Nonce + `enforce_nonce` on every message (safe retries of 5xx/timeouts; an import
  re-run de-duplicates what already landed); 90 s wall-clock deadline for interactive
  posts (a request runs on a thread joined with the remaining budget — urllib's timeout
  is per resolved address);
  `allowed_mentions: {"parse": []}`; links/mentions/spoilers in merchant names
  defanged; stdout is one factual line for the agent (no merchant names) with exit
  codes 0/1/2/3/4 that say what to do. `review.py --post` added.

**Reminders**
- ★ A reminder fired only on exactly due−3 / due; one failed send (e.g. a Discord
  500) or a missed run lost it for good. Now: retried (nonce), the 3-day reminder
  catches up on due−2/−1, the due reminder goes out as OVERDUE up to due+2; still
  once per cycle. S$0 statements never ping.
- The June 2026 cron failed every day for a month (the June due-date reminder never
  went out) while the upload said "✅ reminders are SET": the health check only
  looked at "exists + active". Now unhealthy on last_status error, failure streak,
  error/no next run, overdue next run, no run in 50 h, or a schedule other than daily
  09:00.
- `process_pdf()` ran `hermes cron resume` on every import, documented as a
  "harmless no-op" — resume_job() recomputes next_run_at from now, so an import in
  the 09:00 dispatch window skipped that day's run (0.56 s/PDF too). Removed, with
  the dead pause/resume helpers.
- Wrapper: skill import guarded (a broken edit now alerts instead of failing
  silently), failure alerts @-mention and include the unsent text, top-level guard.
  `reminder.py` CLI is read-only by default (it used to mark reminders fired without
  sending). The confirmation lists exactly what the cron will send.

**Data / parser**
- ★ Every charge is shown: `row_num > 0` rows are real billed repeat charges (totals
  reconcile only with them) — the `row_num = 0` display filter hid double charges.
- ★ The statement total was glued onto the last transaction's name on 18/18
  statements (junk identities: `SHOPEE MP SINGAPORE 2,689.54`, `ANNUAL FEE 516.87`,
  a `CASH REBATE nnn` per month) and `FOREIGN CURRENCY USD n.nn` split NANONOBLE three
  ways. Parser fixed; `scripts/migrate_identities.py` repaired the stored rows
  (20 rows, 8 identities folded, every approval kept; snapshot + verify + marker).
- Foreign also via the raw `FOREIGN CURRENCY` marker (shown as `SG · USD 10.00`);
  holiday exclusion by exact names (substrings also hid e.g. SPORTPERFORMANCE);
  column-layout country codes captured; reconciliation warning (previous total +
  transactions == total due); the date-window check actually runs; merchant ids are
  never reused; re-import refreshes merchants that lost rows; LEFT JOIN so no
  transaction vanishes from the approval list; pymupdf missing → clear error.
- `find_duplicate_merchants.py`: stop words now apply, raw evidence printed,
  verified-distinct clusters suppressed. The docs' "same terminal code = same shop"
  was wrong — `-9113` is shared by 25 merchants (correction in the reference).

**Docs:** SKILL.md slimmed (~3,960 → ~1,600 tokens) to the one-command contract;
CLAUDE.md (attached to every agent command in the dir) cut to a few lines, long form
→ `references/developer-guide.md`; dated corrections in the references. The 08-20
"agent→gateway hop drops backslashes" diagnosis was the model dropping them when
retyping the review (the stored assistant message already lacked them) — the rule
(scripts post, the agent never relays) is unchanged.

---

## v3.4.1 — 2026-08-20 — Byte-safe delivery: script self-posts reviews via REST

**Bug (the v3.4.0 fix did not take):** the escaped review (`SMP\*\*JUNG`) was
relayed through the agent's own reply — and the agent→gateway hop silently
dropped the backslashes (`\*` arrived as `*`; a sibling `\ ` in the same message
survived, proving the loss was in the composed text, not Discord). Discord
stored the raw `SMP**JUNG` again, and the same bold-pair shift corrupted the
rendering. Transport test (REST POST → fetch-back) proved Discord's API stores
backslash escapes byte-perfect — the agent hop was the only lossy link.

**Fix:** new `scripts/discord_post.py` (REST POST, token from `~/.hermes/.env`,
same mechanism as the reminder cron wrapper). `import_and_review.py` and
`approve.py` gained `--post <channel_id>`: the script POSTs the message itself
(script bytes == stored bytes) and stdout carries only a short confirmation for
the agent to relay. The agent must NEVER relay review bodies through its own
reply again. Verified: `discord-markdown` (Discord's actual parser semantics —
stock markdown-it does NOT reproduce the cross-line emphasis pairing) renders
the REST-posted message with bold segments perfectly symmetric across cards.
Also reproduced the original corruption with `discord-markdown`, confirming
`SMP**JUNG` was the sole culprit. Selftest 39/0/0. Workflow contract in
SKILL.md updated (upload + approve steps use `--post`).

---

## v3.4.0 — 2026-08-20 — Escape Discord markdown in merchant names

**Bug:** the Aug-26 review's high-value row contained `SMP**JUNG SUNG HAIR` — an
OCBC-masked merchant name carrying raw `**`. Interpolated unescaped into the
Discord message, the stray `**` (an unclosed bold delimiter) corrupts Discord's
emphasis parsing for the rest of the message: bold pairs shift, so the two card
sections render inconsistently (5999's `Due`/`Total due` lost their bolding
while 7008's didn't). The stored message content was byte-identical for both
cards — pure rendering corruption from untrusted data.

**Fix:** new `reviewlib.esc_md()` escapes Discord markdown-significant chars
(`\`, `*`, `_`, `~`, backtick) and is applied to every merchant-name
interpolation: `render_statement()` (high-value, annual fee, foreign, new,
previously-seen), `review.py --merchants-only`, and `approve.py` confirmations.
Stored merchant names are untouched (no normalize-time stripping — that would
churn merchant identities and approvals). Verified by re-rendering the fixed
review through markdown-it in both paragraph and whole-message (Discord-style)
modes: bold segments mirror exactly across cards. Selftest 39/0/0 (4 new
`esc_md` cases).

---

## v3.3.1 — 2026-07-11 — Payment reminders @-mention the account holder

Reminder messages now prefix `<@CC_REVIEW_NOTIFY_USER>` (default the account
holder `470611394698412052`, env-overridable; `""` disables) so they ping instead
of being easy to miss. The wrapper's `allowed_mentions` allow-lists exactly that
one user id (`{"users": [id]}`), not `{"parse": ["users"]}`, so a stray `<@id>`
in content can never ping anyone else. `reminder.py` + `cc_payment_reminder.py`;
selftest 35/0/0.

---

## v3.3.0 — 2026-07-11 — Tune-up (bug hunt + delivery + docs)

Full hermes-skill-tuneup pass. Bugs found by an independent-reviewer fan-out +
parser self-probes, all validated on the live DB (a clone; the live DB was never
mutated). The terminal validation gate then caught two regressions the fixes
themselves introduced (below). Behaviour-preserving where it counts: the review
output and merchant-normalization are byte-identical on all live data (review
fingerprint unchanged, normalize drift 0/130); selftest 35/0/0.

**HIGH**
- **False "🚨 reminders may NOT fire" on every import/approval.** `verify_cron_health()`
  shelled out to `hermes cron list`, but `hermes` is not on the non-login PATH the
  agent terminal and cron scheduler use (it's at `~/.local/bin/hermes`; PATH is
  `/usr/local/bin:/usr/bin:/bin`). The `FileNotFoundError` made the check report
  the job absent, so the post-upload confirmation cried wolf while the cron was
  healthy and firing daily. Fix: read cron state from `~/.hermes/cron/jobs.json`
  directly (the scheduler's source of truth, no PATH/CLI-format dependency), CLI
  only as a fallback; new `determinable` flag distinguishes "state unreadable"
  (→ soft note, don't cry wolf) from "job confirmed absent/paused" (→ real 🚨).
- **Duplicate payment reminders on a partial send.** The cron wrapper sent all due
  messages then called `mark_fired(items)` once after the loop; if the 1st card's
  POST succeeded and the 2nd raised (429/5xx/network), nothing was marked → the
  delivered card re-sent the next day. Both cards routinely share a due date, so
  this was live-reachable. Fix: mark each item fired immediately after ITS own
  successful send; per-send try/except; bounded 429 Retry-After retry.
- **Silent reminder-delivery failure.** A `deliver: local` `no_agent` job's failure
  is pushed nowhere by the scheduler, and the wrapper only `print()`ed. A missed
  due-date reminder — the one thing this skill exists to prevent — was invisible.
  Fix: on any send failure the wrapper self-notifies Discord (best-effort), dumps
  the UNSENT reminder text to stdout (saved to the job-output file), and exits
  non-zero (turns the job red in `hermes cron list`).

**MED**
- **False "🆕 new merchant" on re-import (MP/LA).** `normalize_merchant` stripped
  country codes from *any* position using the full ISO alpha-2 set, so legitimate
  2-letter merchant tokens were deleted: "MP" (Shopee marketplace → MP=Northern
  Mariana) and the leading "LA" of "LA BOTTEGA DEL TART" (→ Laos). This fragmented
  the merchant into a new `merchant_normalized` on the next import and defeated the
  canonical map (whose key no longer matched). Fix: keep-list `{MP, LA}` (both are
  required verbatim by a canonical-map key); every other embedded code — incl. the
  "BY"/"LT"/"SG" that canonical keys *rely* on being stripped — is unchanged, so
  normalization is byte-identical on all 130 live merchants (drift=0).
- **Same-month re-import re-fired the due-date reminder.** `process_pdf` cleared the
  per-card reminder markers on *every* import; re-uploading the current statement
  ("did that go through?") wiped an already-fired marker → the due-date reminder
  fired again. Fix: clear markers only for a genuinely newer cycle
  (`year_month > previous latest`).
- **`approve #18 53` dropped the bare 53.** `re.findall("#(\\d+)") or re.findall("\\b\\d+\\b")`
  short-circuited: any `#`-id skipped the bare-number branch. Fix: resolve `#`-ids
  and bare ids; accept bare ids as ids only when the reply is an id-list, not prose
  (so `approve #18 within 3 days` no longer toggles merchant #3 — a regression the
  first fix introduced, caught by the terminal gate).
- **Subprocess calls had no timeout.** `hermes` invocations could hang an import/
  approve forever. Fix: `_hermes_bin()` absolute-path resolution + `timeout=` +
  `TimeoutExpired` handling.
- **`parse_amount` aborted the whole statement on a multi-dot token.** `float("1.2.3")`
  raised, bypassing the documented digit-extraction fallback. Fix: guard every
  `float()` so a malformed amount degrades to 0.0 (row-failure) instead of aborting.
- **Reminder path imported `pymupdf` needlessly.** `reminder.py` → `parse_ocbc`'s
  top-level `import pymupdf` coupled the delivery-critical reminder to a heavy PDF
  dep it never touches (and would kill it silently at import under the venv if the
  wheel broke). Fix: `import pymupdf` lazily inside `read_pdf_text`.

**LOW**
- ANNUAL FEE > $100 was listed in both High-value and its own Annual-fee section;
  excluded `%ANNUAL FEE%` from the high-value query (it keeps its dedicated section).
- `parse_date` accepted impossible dates ("13/13" → "2025-13-13"); now raises
  ParseError (consistent with its existing raise-on-bad-input contract).

**Regressions caught by the terminal gate (fixed before ship)**
- `reminder.py` left an `elif active:` referencing a variable deleted when the
  health check was reworked → `NameError` whenever no reminders were pending
  (steady state). Now gated on `cron.get("healthy") or determinable is False`.
- `approve.py`'s first id-parse fix over-captured prose numbers (see MED above).
- Hardening: `verify_cron_health` now also treats an explicit `state:"paused"`/
  `"disabled"` in jobs.json as paused (not just `paused_at`/`enabled`).

**Docs:** created this CHANGELOG (relocated the dated fix logs out of SKILL.md);
slimmed SKILL.md hot-path tokens; fixed doc↔code drift (`CANONICAL_MERCHANT_MAP`
name, `process_pdf` signature, the removed auto-pause lifecycle); removed the
phantom top-level `approve.py` and empty `cc_review.db`.

---

## Relocated history (moved from SKILL.md 2026-07-11 — preserved verbatim)

### July 2026 — v3.2.0
- `scripts/find_duplicate_merchants.py` — read-only sweep that finds same-shop,
  two-normalized-name splits BEFORE they surface as false-positive "🆕 new merchant"
  flags.
- `references/duplicate-merchant-sweep.md` — full lesson + verified clusters
  (JUNGSUNGHAIR, NETFLIX, GOMO, SHOPEE) + fix recipe.
- New canonical-map entries: `JUNGSUNGHAIR PTE SINGAPORE` → `SMP**JUNG SUNG HAIR`;
  `NETFLIX.COM SINGAPORE` → `NETFLIX.COM LOS GATOS`; `GOMO SINGTEL SINGAPORE` →
  `GOMO MOBILE PLAN SINGAPORE` (5999); `SHOPEE SINGAPORE SINGAPORE` →
  `SHOPEE SINGAPORE MP SINGAPORE` (7008). Backfilled live DB; 0 approval flags lost.
- New pitfall: OCBC renames merchants across statements (canonical map must be kept
  up to date). Run `find_duplicate_merchants.py` monthly before the import.

### May 29 2026 — Audit fixes (silent-failure sweep)
- **`resume_reminder_cron()` was unconditionally broken** — `parse_ocbc.py`
  referenced `subprocess`/`CRON_JOB_ID` without importing/defining either; every
  `process_pdf()` raised `NameError` after data was stored. Fixed by importing
  `subprocess` + defining `CRON_JOB_ID`; both cron helpers log to stderr on failure
  rather than crashing.
- **Per-card reminder scoping** — old logic set a single global
  `reminders_disabled='1'` when *any* card hit its due date, silently dropping the
  other card's pending reminder. State is now per-card (`reminders_fired_{card}`,
  `reminded_3day_{card}`); `reminders_disabled` is a manual override only.
- **Atomic `process_pdf`** — parse first (no DB writes), then a single `with conn:`
  transaction for delete + insert + flag reset; `parse_statement()` raises
  `ParseError` on missing header / `TOTAL AMOUNT DUE` / dates instead of persisting
  silent zeros.
- **Header-regex fragility** — the transaction-header search is case-insensitive and
  tolerant of `(SGD)` spacing; raises `ParseError` if not found.
- **`parse_amount` fallback** — digit-extraction fallback for malformed amounts
  (see `references/parse-ocbc-errors.md` item #3).
- **`parse_date`** — raises `ParseError` on empty/malformed input instead of
  returning `""` (which passed `NOT NULL`).
- **Dead code removed** — the original `extract_transactions` state-machine prototype
  (superseded by v2, since renamed to `extract_transactions`).
- **Schema drift** — `init_db()` now matches the live schema exactly.
- **`review.py` new-merchants query** — `LEFT JOIN merchants` +
  `COALESCE(m.approved,0)=0` + explicit `AND t.row_num=0` to stop silent row drops
  and page-break double-counts.
- **`import_all.py`** — ✓/✗ per file, end-of-run failure list, exit 1 on any failure.

### May 2026 — Session fixes
- **row_num deduplication** — OCBC PDFs repeat transactions at page breaks; the
  parser assigns `row_num` (0 = first occurrence). All per-transaction review
  queries include `AND row_num = 0`. Do NOT remove it — page-break duplicates
  reappear silently otherwise.
- **Italian merchant filter** — filter patterns must match actual normalized DB
  values (`ATB SERVIZI` not `A T B SERVIZI`; added `TPER`, `CLESS`, `START ROMAGNA`,
  `LA BOTTEGA DEL TART`, `TAP & GO`). These now live in `reviewlib._HOLIDAY_LIKE`.
- **Working-directory pitfall** — scripts resolve `DB_PATH` relative to
  `scripts/`, so the live DB is `scripts/cc_review.db`; a phantom empty
  `cc_review.db` at the skill root is ignored. (The phantom was removed in v3.3.0.)
- **APPROVALS ARE LOST ON `DELETE FROM merchants`** — normal re-imports use an
  UPSERT that preserves `approved`; only an explicit full rebuild wipes flags, and
  must save/restore them (see `references/merchant-approval-enhancement.md`).
