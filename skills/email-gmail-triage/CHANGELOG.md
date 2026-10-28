# CHANGELOG — email-gmail-triage

## Version 2.1.0 (2026-09-28) — hermes-skill-tuneup pass (bug hunt + perf + delivery)

Method: Phase-0 baseline (4 suites green, 150/150 archived digests delivered, 0 failed runs in the
50-run archive), 3 blind reviewers + an adversarial refuter over an offline kit (live code, real state,
archived outputs, session history), then a re-entrant terminal gate: lap 1 (regression hunter +
refuter, agents), lap 2 (adversarial agent + a 20,000-interleaving watermark simulation), lap 3
(deterministic self-probes re-running lap 2's harnesses). Every new test was mutation-checked
against the previous lap's code (it must fail there).

**Bugs fixed (confirmed on live data / by probe):**
- **Hermes-label pre-check and old-label removal were dead code.** Gmail returns label IDs
  (`Label_26`…), the code tested `startswith("Hermes/")`. Only `processed_ids` prevented
  re-processing; after state loss every inbox email of the last day would have been re-labelled,
  label-stacked, re-reported and possibly re-archived. The suite passed because its fixture used
  names. Now ID-based (`hermes_id_to_name`), fixtures use IDs.
  *Dated correction:* the 2026-06-06 notes (and the `read_state()` comment) said a corrupt state is
  safe because "the pre-check skips already-labeled mail, so nothing is re-archived" — that was false
  until this fix.
- **Security alerts from no-reply senders were archived** (Google "Critical security alert", Yahoo
  "Unexpected sign-in attempt", Reddit password resets, Dropbox new sign-in, …): the no-reply
  Auto-filed return ran before the security check. ham chose → **Action** (subject-level gate 4b;
  PROMOTIONS + security subject → Unsure; one-time codes stay Auto-filed).
- **Renewals from no-reply senders were still Auto-filed** despite the 2026-05-10 request (the
  exemption only skipped one branch). ham chose → **ReadLater** (`RENEWAL_SUBJECT_RE`, excluding
  promo-worded subjects and `GOV_AD_SENDERS`).
- **The documented OCBC transaction rule never fired** (regex missed "SGD38.41", SGD branch compared
  lowercase against upper-cased text, no-reply branch ran first; 155/155 alerts auto-filed). ham chose
  to revive it: OCBC "transaction" alerts whose first amount is non-SGD or > SGD 100 → ReadLater.
  Backtest on 40 real bodies: 2 → ReadLater, 38 unchanged.
- `is_human_sender`: org display words matched as substrings ("Vincent" ⊃ `inc`, "Corpuz" ⊃ `corp`)
  → whole tokens + explicit plural forms (ham approved).
- Digest length checked against 4000; Discord's bot limit is **2000 UTF-16 units** — a 2001–4000
  digest was rejected, the watermark didn't advance, and later digests grew (replay: stuck for days
  after a 3–6 slot outage). Now capped everywhere (digest, summary, reminder, Unsure).
- Digest send failure exited 0 on a `deliver: local` job → silent. Now: full digest to stdout, exit 1,
  and the 3 digest jobs have `failure_deliver: discord:1499972245437550602`.
- Unsure: recorded before its Discord post, so one failed post meant it was never asked. Now queued
  in `unsure_pending` until a post shows it (≤24 failed attempts, ≤50 queued, logged).
- A crash/kill mid-run left already-archived Auto-filed mail out of state and every digest → state
  is persisted after every email; label + archive + mark-read is one atomic modify; an ambiguous
  modify failure is parked (`modify_unknown`) and verified next run.
- No subprocess timeout → `GAPI_TIMEOUT` 90s + `RUN_BUDGET` 360s (worst case ≈532s < 600s watchdog).
- Non-atomic writes / unguarded reads (label cache — a torn one stopped all triage; report log;
  correspondents cache — a failed harvest could wipe it; OAuth token in the report) → atomic + guarded.
- 9 PM reminder only on a run starting 21:00–21:09 → first run ≥21:00 not yet done today
  (`reminder_date`); query adds `label:hermes-action` so 100 newer unread mails can't hide Action mail.
- Body-fetch failure categorized on an empty body (an OCBC alert would lose its amount) → deferred
  (≤3 runs). `extract_email` via `parseaddr` (trailing space / `a@b (Name)` bypassed the VIP gate);
  `.gov.sg` is a domain test; None-safe fields; NBSP-safe subject matching.
- "Triage complete" logged the start time → real end time + duration.

**Regressions the terminal gate caught in this tune-up's own fixes** (all fixed + regression-tested):
per-email persist + one run-wide stamp let a digest reading a half-written run drop the rest of it
(→ per-email stamps + a watermark tie-break, `reported_at_watermark`, with the legacy-log fallback
mirrored in `advance_watermark`); the first tie-break wiped the tie set on an empty same-second
digest (duplicates in 12k/20k simulated interleavings); a parked email seen by the pre-check got an
EPOCH stamp; `ConnectionError` retry could duplicate a post (→ connect-phase only); a slow full
Sent harvest retried 3× cost ~273s per run (→ one attempt); renewal/promo and org-plural over-matches.

**Performance:** the Sent harvest (`in:sent newer_than:1y --max 500`, 14.5s of a ~23s run, every
hour) is now incremental (`newer_than:2d`, 0.6s) with one full pass per 24h; Auto-filed takes 1 modify
call instead of 3; the reminder no longer re-fetches each email.

**Residual limits (documented, not chased):** an unreadable report log falls back to its mtime, so an
email filed in exactly that second can be missed; a missed 21:00–23:59 reminder is not carried past
midnight; "Renewed skin in 7 days"-style non-promo-worded marketing without the PROMOTIONS label
would go to ReadLater. The 4 cron jobs still carry legacy agent-mode `prompt` text (inert while
`no_agent: true`).

Backups: `~/.hermes/scripts/gmail_{triage,report}.py.bak-tu-20260928`, `test_*.py.bak-tu-20260928`.

## Change (2026-09-05) — OCBC credit card e-Statement never Auto-filed
ham asked that credit card e-Statements from `documents@ocbc.com` always land in ReadLater and
never be Auto-filed. A blanket sender rule would have been too broad (that address also sends
marketing), and a literal subject match too brittle (OCBC rewords the line — today it's "The
e-Statement for your credit card is ready"). Added a third form of the hard top gate:
`READ_LATER_SUBJECT_RULES`, mapping an exact sender → (description, list of required subject
patterns). New helpers `normalize_subject()` (lowercase + collapse non-alphanumeric runs to one
space, so `e-Statement` / `e Statement` / `eStatement` all reduce to matchable text) and
`matches_read_later_subject_rule()` (ALL patterns must match). The OCBC entry requires
`\bcards?\b` AND `\be?statements?\b` — concept-level, so any card-statement rewording fires.
The gate sits directly after the READ_LATER domain gate, i.e. above every Auto-filed path AND
above the ambiguity gate → deterministic ReadLater, never Auto-filed, never Unsure. Matching is
subject-only (golden rule: never the body). Sender-scoped, so a look-alike `ocbc-secure.com`
phish gets no free pass, and subject-scoped, so OCBC dining promos still auto-file. 11 regression
cases added to `test_gates.py` (6 subject wordings, CATEGORY_PROMOTIONS override, non-statement
promo, look-alike sender, and both half-matches). All four suites pass. Docs updated (SKILL.md,
CLAUDE.md, references/category-rules.md).

**Backtest against real history (read-only, 23 `documents@ocbc.com` emails in state, Jun–Sep 2026):**
8 would flip to ReadLater (all credit card), 15 stay Auto-filed (13 deposit account, 1 CPFIA/SRS,
counting duplicates) — exactly the intended split. Two facts the backtest established:
1. **The rewording is real, not hypothetical** — OCBC used "OCBC: Your Credit Card e-Statement for
   Jun 2026 is attached" on 4 Jun, then switched to "OCBC Notification: The e-Statement for your
   credit card is ready" from 3 Jul onward. A literal subject match would have caught only one of
   the two wordings; the concept match (card token AND statement token) catches both. This is why
   the rule must stay loose on wording — do not tighten it to a phrase.
2. **Statements arrive in PAIRS** — each cycle sends two emails with distinct message IDs, 1–4h
   apart (6 Aug 20:18:18 sent two in the same second). Expect two ReadLater items per statement
   cycle; that is OCBC's behaviour, not a triage duplicate.

Historical mail was deliberately **left untouched** at ham's instruction — the 8 past credit card
statements keep `Hermes/Auto-filed` and stay archived (invariant #7: fixes prevent future
misclassification, they don't undo past damage). Scope is credit card only, also at ham's
instruction: deposit-account and CPFIA/SRS e-Statements from the same address continue to
auto-file by design. To widen later, drop the `\bcards?\b` requirement from the rule.

## Change (2026-07-06) — My First Skool: single address → whole domain
The Always-ReadLater rule for My First Skool was widened from the single address
`centre_admin@myfirstskool.com` to the entire `@myfirstskool.com` domain (any local-part,
subdomains included) at ham's request — the school mails enrolment/admin notices from more
than one address. Added a new `READ_LATER_DOMAINS = {"myfirstskool.com": ...}` config set;
`READ_LATER_EMAILS` (exact addresses) is retained but now empty. The READ_LATER hard top gate
in `categorize()` now matches `sender in READ_LATER_EMAILS OR sender_domain == d / endswith("."+d)
for d in READ_LATER_DOMAINS`, still short-circuiting to ReadLater above every Auto-filed path.
Look-alike safety verified (`notmyfirstskool.com` does NOT match). Regression cases added to
`test_gates.py` (new local-part, billing@, subdomain, original address, look-alike). Docs updated
(SKILL.md, CLAUDE.md, references/category-rules.md).

## Version 2.0.0 (2026-06-09)
Token-efficiency tune-up: relocated bug history, category rules, and architecture details
from SKILL.md to load-on-demand references. SKILL.md reduced from ~12,300 to ~3,800 tokens.

## Resolved implementation notes (relocated from SKILL.md Critical Implementation Notes)

### ✅ RESOLVED (2026-06-03) — silent-skip + already-reported = data loss (orphans)
Under the old design the report decided "new" by `state_ids − last_reported_ids` and rendered
each, doing `if not cat: continue`. An entry with empty `category`/`from`/`subject` (an "orphan")
was silently dropped while its ID stayed in `last_reported_ids`, so the email vanished from all
future digests (bit us 2026-06-02: 335 orphans, blank `` `: `` lines). **This class is now
structurally impossible.** Selection is the `triaged_at` watermark, not an id-diff; orphans carry
an EPOCH stamp and are simply never selected — they can't render blank and can't "use up" a report
slot. The triage merge no longer creates category-less entries (the old `gmail_triage.py` "Legacy
entry or already-triaged with no category recorded" fallback was removed). `references/orphan-state-recovery.md`
and `scripts/gmail_state_heal_once.py` are **deprecated** — kept for history only.

### ✅ RESOLVED (2026-06-03) — state format matters for report performance
The old report had a Gmail-API "legacy fallback" for bare-string state, which was slow (~0.27s/msg)
and could blow the 120s cron timeout if `last_reported_ids` emptied. That fallback is **removed**.
The report reads only the dict-form state and selects by `triaged_at`; a bare-string state simply
yields nothing until the next triage normalizes it (with `triaged_at=EPOCH`). No API calls, no
timeout path, no empty-`last_reported_ids` failure mode — `last_reported_ids` no longer affects
selection at all.

### Two-stacked-bug pattern: symptom is one, root causes are multiple (lesson from 2026-05-25)
When a category is wrong (e.g., VIP email → Auto-filed), there are often multiple independent bugs
that each contribute. In the 2026-05-25 Penny incident:
1. `CATEGORY_UPDATES + "offer"/"renewal"` in body fired before VIP check → auto-filed
2. `is_human_sender()` returned `False` for `akanestar` (no dot) → fallback to Auto-filed default

Both must be fixed independently. Fixing only one leaves the other path active.

---

## Bugs Found & Fixed (code review 2026-06-06)

Surfaced by a code review + 3 independent reviewer passes; only issues that were **≥ Medium severity and corroborated by ≥ 2 reviewers** were fixed in this batch. Each fix has regression coverage in the skill-dir suites.

| Bug | Fix |
|---|---|
| Real human (non-VIP) Auto-filed on a bare promo/receipt **subject** keyword — "offer"/"weekly"/"invoice"/"sale"/"order"… (HIGH; flagged by main + R1) | The `PROMO_KEYWORDS`/`RECEIPT_KEYWORDS` subject loops in `categorize()` returned `Auto-filed` **before** the `is_human_sender() → Action` default, with no sender check — violating the documented rule that "offer" (and friends) must never be a standalone promo signal. A `KNOWN_HUMANS`/`KNOWN_CORRESPONDENTS` email like "Weekly update" or "Invoice attached" was archived + marked read. **Fix:** wrapped both subject loops in `if not is_human_sender(msg):` so a known human falls through to the Action default; non-humans still Auto-file on the keyword. VIP/READ_LATER/`.gov.sg` gates are unaffected (they short-circuit earlier). Verified: `hector90@gmail.com` "Special offer"/"Weekly update"/"Invoice…" → Action; `deals@megashop.com` "Special offer" → still Auto-filed. Regression cases added to `test_gates.py`. |
| 9 PM Action reminder almost never fired (HIGH; flagged by main + R1) | `send_action_reminder(action_emails)` only received emails **first triaged in the 21:00 run**; an Action email that arrived earlier in the day was already in `processed_ids` and skipped, so it never made the list — the reminder effectively only covered mail that arrived 20:00–21:00. **Fix:** the 9 PM block now queries `in:inbox is:unread` fresh and filters by the `Hermes/Action` label id, so it nags about the entire unread-Action backlog. Also made the per-message unread re-check **fail open** (a transient API error now keeps the email in the reminder instead of silently dropping it / suppressing the whole reminder). |
| Untrusted sender unsanitized in the digest + masked-link injection in `sanitize_discord` (MED; flagged by main + R3) | `gmail_report.py` interpolated `extract_email(m["from"])` into the digest (full body + compact summary) without `sanitize_discord` — the one untrusted field still going through raw (the triage reminder already sanitized it). Separately, `sanitize_discord` in **both** scripts did not neutralize Discord masked links `[label](url)` or bare `://` autolinks, so a crafted subject could inject a clickable phishing link into the trusted #alerts digest (`allowed_mentions parse:[]` blocks the ping, not the link/format break). **Fix:** sanitize the sender in all four report spots; `sanitize_discord` now inserts a zero-width space into `](` and `://`. Sanitizer regression cases added to `test_gates.py`. |
| `run_gapi` could abort the whole triage on one malformed response, or loop on unexpected output (MED; flagged by main + R1 + R3) | `json.loads(stdout)` was unguarded — a truncated/garbled API response raised `JSONDecodeError` straight out of `main()`, killing the hourly run. And an `rc==0` response that was neither JSON nor a known sentinel (`""`/"No messages found.") fell through, burning all 3 retries (**re-issuing any mutating call** such as a label/archive) before raising with an empty stderr. **Fix:** `json.loads` wrapped in try/except (retry, then raise loudly with context); an unexpected `rc==0` stdout now raises deterministically instead of silently retrying a call that already succeeded. |
| State file write was non-atomic + read was unguarded → a crash mid-write could brick every future run (HIGH; flagged by R3 only, fixed at ham's request) | `write_state` used `open(STATE_FILE, "w")`, which truncates the **live** file before the `flock` is taken, so a crash mid-`json.dump` (power loss / OOM kill on the Pi) left an empty or partial state — and `read_state`'s `json.load` was unguarded, so every subsequent run aborted at startup, indefinitely. **Fix:** `write_state` now serializes to a temp file + `flush`/`os.fsync`, then `os.replace` into place (atomic on POSIX — a reader always sees a complete old or new file, never a torn one); `read_state` wraps `json.load` in try/except and, on a corrupt file, logs loudly and starts from an empty state (the run's own `write_state` rewrites it cleanly; the Gmail Hermes-label pre-check keeps the re-discovery safe — nothing is re-archived, at worst a still-unlabeled Unsure email is re-surfaced once). Regression: atomic round-trip + corrupt-file recovery in `test_triage_state.py` + a unit check. |
| Auto-filed email could be archived + marked-read with NO Hermes label when labeling failed (MED; flagged by R3 only, fixed at ham's request) | `apply_labels` swallowed the add-labels API error as a `[warn]` and returned, then `main` unconditionally `mark_read` + `archive`d any Auto-filed email — so a transient label failure filed the email away from the inbox with no `Hermes/` label: invisible, and never re-discovered (it is no longer `in:inbox`). **Fix:** `apply_labels` now returns a bool; on failure `main` skips the destructive archive/mark-read **and** does not record the email in state, so the next hourly run re-discovers it (still unlabeled → the pre-check won't skip it) and retries. Emails reaching this path never had a Hermes label (the pre-check above skips ones that do), so they are left exactly as they were. Regression added to `test_triage_state.py`. |

---

## Bugs Found & Fixed (session 2026-05-03)

| Bug | Fix |
|---|---|
| `ensure_hermes_labels()` dead function with broken Google API code (~55 lines) | Deleted — `ensure_hermes_labels_simple()` does the real work |
| `run_gapi()` passed error dicts straight into message loop | Added `isinstance(parsed, dict) and "error" in parsed` guard → raises RuntimeError |
| `snippet` undefined in `build_report()` entry dict | Changed to `msg.get("snippet", "")` |
| `processed_ids` cap at 500 caused ID loss on rollover | Raised to 5000 |
| Gmail API error responses could become fake emails with `msg_id = ""` | Added `isinstance(msg, dict) and "id" not in msg` validation + Gmail pre-check for existing Hermes labels |
| Auto-filed section missing backticks around sender email | Added backticks for consistency |
| VIP shown as 4th section in digest | Removed — VIP is a flag on Action/ReadLater, not a separate bucket |
| Human sender (Boon Chong Hao) auto-filed despite being a real person | Added `is_human_sender()` check + `HUMAN_SENDER_SIGNALS` list — real humans default to Action, not Auto-filed, even with promo keywords |
| Discord spacing inconsistent between digest sections (2026-05-07) | Replaced `"\\n".join(parts)` with single `---\n` between header+Action, blank line between sections, blank line before footer. Format now: header + `---` + Action + blank + ReadLater + blank + Auto-filed + blank + footer |
| `centre_admin@myfirstskool.com` auto-filed despite READ_LATER_EMAILS entry (2026-05-19) | `"offer"` in subject matched `PROMO_KEYWORDS` → email went to Auto-filed before explicit sender check was reached. Moved `READ_LATER_EMAILS` check to fire **before** subject-based promo checks (lines 201–206). Also fixed `CATEGORY_UPDATES` block which used `"offer"` as a promo indicator — same collision path. |
| Penny (akanestar@hotmail.com) school enrolment emails auto-filed despite VIP status + restoration (2026-05-25) | Two bugs stacked: (1) `CATEGORY_UPDATES + "offer"/"renewal"` in body fired before VIP check; (2) `is_human_sender()` returned `False` for `akanestar` (no dot in name part) → fell to default Auto-filed. Fix: exempt VIP senders from `CATEGORY_UPDATES → Auto-filed` block AND add `sender in VIP_EMAILS → return True` to `is_human_sender()`. Restored archived emails: removed `Label_29` (Auto-filed) and re-added `INBOX` via Gmail API. Email remained in `processed_ids` — correctly skipped on next run since it now has correct labels. |
| Penny (akanestar@hotmail.com) **invoice** email auto-filed AGAIN despite VIP status (2026-05-29) | Root cause: `categorize()` had **multiple** unguarded `Auto-filed` return paths; the 2026-05-25 fix only patched the `CATEGORY_UPDATES` branch. This email was `CATEGORY_PERSONAL` with `"invoice"` in the subject → hit the **`RECEIPT_KEYWORDS` subject check** (which runs *before* the `Fw:` reply-prefix Action check) → Auto-filed, no VIP guard. **Absolute architecture fix:** VIP is now a hard gate at the top of `categorize()` — VIP senders short-circuit to `categorize_vip()`, which returns ONLY Action/ReadLater, never Auto-filed. Plus a `main()` safety net forces any VIP→Auto-filed to Action and strips `Hermes/Auto-filed` from VIP label sets (3 independent layers). Restored the archived email (removed Label_29, added Label_26+INBOX+UNREAD). |
| `READ_LATER_EMAILS` override could still be auto-filed via `CATEGORY_PROMOTIONS`/`CATEGORY_UPDATES` (audit 2026-05-30) | Same structural flaw as VIP: the explicit ReadLater sender check sat *below* the `CATEGORY_*` auto-file branches, so a My First Skool email tagged `CATEGORY_PROMOTIONS` (or `CATEGORY_UPDATES`+`offer`) was auto-filed before the override fired — the 2026-05-19 bug, still live. Fix: promoted `READ_LATER_EMAILS` to a **hard top gate** in `categorize()` (directly after the VIP gate), short-circuiting to ReadLater before any Auto-filed path. Removed two now-dead duplicate READ_LATER checks. |
| Untrusted email subjects posted to Discord unsanitized (audit 2026-05-30) | Subjects/senders went straight into Discord `content` with no `allowed_mentions` — a subject like `@everyone …` would ping the channel, and newlines/backticks could break the digest format. Fix: `sanitize_discord()` (strip newlines, escape backticks, defang `@`) on every interpolated field + `allowed_mentions={"parse": []}` on every send, in both `gmail_triage.py` and `gmail_report.py`. |
| Digest could re-report old emails over time (audit 2026-05-30) | `gmail_report.py` did `list(set(old+new))[-500:]` — `set()` drops order, so the 500-cap kept an *arbitrary* subset and old ids could fall out of `last_reported_ids` (while still in `processed_ids`) and get re-reported. Fix: order-preserving de-dup, then slice the most-recent 500. |
| Golden rule "if unsure, ask ham" was never implemented (audit 2026-05-30) | `categorize()` always force-picked a bucket. Added a two-tier **ambiguity gate** that returns `"Unsure"` when a strong Auto-filed signal collides with an Action signal: **CRITICAL** (security/billing language) flags from ANY sender (phishing-looking-like-promo); **SOFT** (reply/forward prefix or personal-action phrases) flags only when `is_human_sender()` is true — so no-reply/marketing blasts saying "order"/"please" are NOT flagged. `main()` leaves Unsure mail untouched (unread, in inbox, no label) and posts one batched Discord question via `send_unsure_query()`. Measured 0 false-positives over 21 days of real mail (vs 15 if the SOFT tier ignored sender). |
| First-ever Unsure was a false positive on a Standard Chartered `<ADV>` promo (sc.singapore@sc.com, 2026-05-30 15:01) | The SOFT tier fired on a `CATEGORY_PROMOTIONS` email because (1) `is_human_sender()` returns True for the dotted corporate local-part `sc.singapore` (the `First.Last`→human heuristic), and (2) "important" appears in `<ADV>` boilerplate (a soft action phrase). It was unambiguous promo and should have been Auto-filed. Fix: the SOFT tier now also requires `"CATEGORY_PROMOTIONS" not in labels` — Gmail's promo verdict is authoritative and the SOFT tier may not override it (only the CRITICAL security/billing tier may, for phishing). Also added `<adv>` to PROMO_KEYWORDS. Confirmed: the live email now → Auto-filed; 0 Unsure over 21 days incl. spam/trash. Deeper latent issue noted: `is_human_sender()` mis-flags dotted corporate senders as human — not yet hardened (regression risk). |
| `is_human_sender()` reworked — allowlist-based, backtested (2026-05-30) | Replaced the fragile `First.Last`→human guess (which mis-flagged `sc.singapore`, `estatement.sg`, the `appointment.booking` clinic, etc.) with: POSITIVE allowlists first — `HUMAN_SENDER_SIGNALS`, `VIP_EMAILS`, a manual `KNOWN_HUMANS` set (people ham knows IRL: Hector Sim, Oh Mandy, Agasta), and `KNOWN_CORRESPONDENTS` (every address ham has SENT mail to, harvested from the Sent mailbox into `gmail_correspondents_cache.json`, union-cached so it only grows). Then NEGATIVE signals — `CATEGORY_PROMOTIONS`/`SOCIAL`, no-reply prefixes, social domains, an org-display-name list (center/clinic/pte/ltd/…), a generic-word check, and a role/place token denylist on the local-part. The `First.Last` dot rule remains only as a last-resort positive. Backtest over 1 year (237 distinct senders): precision 30%→100% on the human class; all 4 known real people (Hector, Mandy, Agasta, May Loh) classified human; no true bulk sender misclassified. Note: org/role addresses ham actively emails (clinic, school admin, Thomson Medical) are now 'human' by the correspondence signal — intended (their replies should surface, not auto-file). |
| Thread-participation signal added — recognizes repliers (2026-05-30) | A real human who replies from a personal address to a general inbox ham wrote to (e.g. Oh Mandy <mandyoh@cdl.com.sg> answering `enquiries@cdl.com.sg`) is NOT in the Sent-recipients set, so the address-only correspondence signal missed her. Added `KNOWN_SENT_THREADS`: `load_correspondents()` also harvests the threadIds ham has sent in; `is_human_sender()` returns True if the inbound message's `threadId` is one of them. main() then LEARNS the replier's address into the correspondents cache so a future email from them in a NEW thread is also recognized. Verified: Mandy's reply threads to her sent message; classified human via the thread signal alone (with KNOWN_HUMANS emptied). |
| 99.co (Coco Palms monthly update) labeled **Action** despite being non-human marketing email (2026-05-30) | `ACTION_PHRASES` check at line 327 fired `"important"` from email body on non-human sender `hello@e.99.co` — missing `is_human_sender()` guard. Non-human senders using words like "important" in marketing emails were incorrectly flagged Action. Fix: added `is_human_sender(msg)` requirement to the Action-phrase branch. Corrected 99.co email: removed Label_26 (Hermes/Action), added Label_29 (Hermes/Auto-filed), archived, marked read. |
| CPF InTouch (`intouch@e.cpf.gov.sg`) labeled **ReadLater** despite being advertising/marketing (2026-05-30) | `is_gov_sg()` catches all `.gov.sg` senders → ReadLater, but CPF InTouch blast emails are bulk marketing, not personal transactional mail. Added `GOV_AD_SENDERS` list with `intouch@e.cpf.gov.sg` as top-gate override before `is_gov_sg()` check. Corrected email: removed Label_27 (Hermes/ReadLater), added Label_29 (Hermes/Auto-filed), archived, marked read. |
| ⚠ Concurrent-edit hazard — full-file redeploys clobber prior fixes (2026-05-30; mostly historical) | **Historical context:** until the 2026-05-31 `no_agent` conversion the hourly triage ran as an LLM agent that could edit this code+SKILL.md; a full-file overwrite of `gmail_triage.py` once silently clobbered the `GOV_AD_SENDERS` and action-phrase `is_human_sender` guard (its SKILL.md rows survived, so code & docs diverged). Both were restored and verified (99.co & CPF InTouch → Auto-filed). The triage is now a plain `no_agent` script and no longer self-edits, but the lesson still holds for manual edits: before deploying, re-pull or use surgical edits; reconcile code against the SKILL.md bug table. |
| Gmail Evening Digest timeout — report re-fetches all emails from Gmail (2026-05-31) | Report log's `last_reported_ids` went empty → next run saw ALL 335 processed IDs as "new" → fetched each via Gmail API (~0.27s each) → 168s total → exceeded 120s timeout. Root cause: `gmail_triage.py` stored only bare IDs in `processed_ids`, so `gmail_report.py` had to re-fetch each email to check its labels. **Architectural fix:** `gmail_triage.py` now stores `{id, category}` dicts. Report script reads categories from state — zero Gmail API calls, <1s regardless of batch size. Also added Discord 4000-char limit handling (auto-truncates to summary). |
| Digest listed blank `` `: `` lines for every email — empty sender/subject (2026-06-01 1:10 PM) | **Direct regression of the 2026-05-31 timeout fix.** That fix made the report read from state with zero API calls, but state stored only `{id, category}` — not sender/subject, which the digest body lists per email. The report's fast path hardcoded `{"from": "", "subject": ""}`, so once state was in the new dict format every listed email rendered as `` N. `: `` (counts stayed correct, so it failed silently). The 7:10 AM digest masked it (0 new emails → "No new emails" branch); 1:10 PM was the first digest with mail under the new format. **Fix:** `gmail_triage.py` now also persists (sanitized) `from`/`subject` in each `processed_ids` entry; `gmail_report.py` fast path reads them via `meta_map`. Still zero API calls — keeps the timeout fix intact. Older entries (pre-fix) lack the fields and render blank, but they're already reported so never reappear. Verified live: 1:10 PM-style digest now renders senders + subjects. |
| 7 of 10 auto-filed lines blank in morning digest — **orphan state entries** with empty `category` and `from`/`subject` (2026-06-02 7:10 AM) | **Different failure mode from the 1:10 PM bug above** — that one had `category` set but `from`/`subject` empty; this one has `category=""` too. Caused by an even earlier code path (pre-2026-05-31 dict format) where `gapi gmail search` returned messages with empty headers (see `gws` binary warning in `google_api.py:219-222` — under certain conditions headers get stripped), so triage persisted `{id, category: ""}` entries (line 873 of `gmail_triage.py`: the "Legacy entry or already-triaged with no category recorded" fallback path). 335 of 353 state entries are this kind of orphan (95% of state). `gmail_report.py` line 194 then does `if not cat: continue` — silent drop, AND the ID is already in `last_reported_ids`, so the email is **gone forever from digests**. Morning digest rendered 7 of 10 auto-filed entries as blank lines because the 3 fully-populated entries (post-2026-06-01 fix) sorted last in the state file's insertion order. **Fix shipped (2026-06-02):** (B) `gmail_report.py` now auto-heals entries with empty `category`/`from`/`subject` via a Gmail metadata fetch with **service-rebuild retry** (the original 335-call heal hit an OAuth token-refresh race that silently stripped `payload.headers` on 11 of 335 IDs — fixed by detecting empty-header IDs and rebuilding the singleton `_service` before retrying). (A) One-shot `scripts/gmail_state_heal_once.py` ran the same heal in batch mode: 335/335 metadata fetched (with the retry), 334 enriched (1 unhealable), state pruned 353→28 entries, 10 morning IDs un-marked from report log so the 1:10 PM digest re-emits them with full data. Verified: simulated 1:10 PM digest renders 28 emails with **zero blank lines**. **Lesson — silent-skip + already-reported is data loss:** any code path that drops an entry with `continue` AND leaves the ID in `last_reported_ids` is unrecoverable. The system is now self-healing forward AND has a one-shot recovery tool for future regressions. |
| LifeSG (`notifications@life.gov.sg`, `.gov.sg`) Auto-filed despite the `.gov.sg → ReadLater` rule (2026-06-01) | An "ECDA: Your consent required for Form 1" enrolment email was auto-filed/archived. Root cause is the **same structural flaw as the old VIP/READ_LATER regressions**: the `is_gov_sg() → ReadLater` check sat at gate 6, *below* the `CATEGORY_PROMOTIONS → Auto-filed` branch (gate 4). Gmail reclassifies LifeSG mail as `CATEGORY_PROMOTIONS` after delivery, so it auto-filed before the gov rule ever ran. **Fix:** promoted `.gov.sg → ReadLater` to a **hard top gate** in `categorize()` (right after the VIP and READ_LATER gates), above every Auto-filed path — excluding `GOV_AD_SENDERS` (CPF InTouch still auto-files, falls through) and escalating security/billing language to Action. Removed the now-dead mid-function `is_gov_sg → ReadLater` check. Added 3 regression tests to `test_gates.py` (gov + CATEGORY_PROMOTIONS / CATEGORY_UPDATES+promo → ReadLater, gov + security → Action). Restored the damaged email: removed Label_29, added Label_27 + INBOX + UNREAD. |
| **Digest redesign — `triaged_at` watermark replaces the id-diff selection (2026-06-03)** | **Root cause of the recurring "old emails reappear" + missing sender/subject reports.** The report computed "new" as `processed_ids − last_reported_ids`, with state capped at 5000 and `last_reported_ids` capped at **500**. Once cumulative volume passed 500, IDs that aged out of the report log but remained in state were re-classified as new and re-reported on every digest (simulation: re-report starts ~day 9, grows to 1000+ old emails per digest). The earlier "order-preserving de-dup" patch (2026-05-30) did NOT fix this — it only chose *which* 500 to keep. **Fix:** every state entry now carries `triaged_at` (`gmail_triage.py`); the digest selects `triaged_at > last_report_ts` and advances the watermark to the newest stamp shown (`gmail_report.py`). One mechanism, no second list to drift; an email past the watermark is never re-selected, one aged out of state is gone. Also: removed the orphan-creating triage merge branch (orphans now carry EPOCH and never render); removed the report's legacy Gmail-API fallback; footer total now reconciles with section counts and surfaces `Unsure` separately (RC#4); added a shared `flock` to the report's state read (report stays read-only on state, triage is sole writer); blank fields render placeholders + a `[warn]` instead of `` `: ``. New suites: `test_report_window.py` (proves reported-once-never-again past 500) + `test_triage_state.py` (proves stamping + no orphans). `scripts/gmail_state_heal_once.py` and `references/orphan-state-recovery.md` deprecated. Deployed + validated live; first production triage run on the new code clean, 0 orphans. |
