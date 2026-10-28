## 1.9.36 — 2026-10-04 — cc-bill-review 3.5.1 follow-up (first live run, writeback)

- **`delivery-reliability.md`**: corrected the 1.9.34 stdout bullet.
  - Script stdout is ONE line written for the user (the agent's whole reply), sent through one `say()`, with nothing on stderr (the terminal tool shows stderr to the agent).
  - Failure handling is keyed by exit code, per script, in SKILL.md.
  - The tests must scan evaluated literal text and run every failure path.
  - The old advice (tell the agent the next step in stdout) produced "Relay this; add nothing.". A deepseek-flash agent spent ~6 min / 59 tool calls investigating it, live in the user's thread, before echoing it to the user.
- **`bug-hunt.md`** (cc-bill-review section):
  - A per-item canonical-map entry is a symptom of a normalizer class. Here footer bleed ate the city of a page's last row and split one shop per statement. Probe: `normalize(raw + footer)` vs `normalize(raw)` over every stored raw.
  - Review a first live run from the session transcript and the thread, not just the script log.
- **`safe-edit-workflow.md`** §6:
  - Remove the Phase 0 kit from the host's /tmp at deploy (agents roam /tmp).
  - After the first live run, check the curator ledger. Archive only curator-CREATED skills, with the user's OK, using `hermes curator archive <name>` (never `purge`). Diff and reconcile a curator PATCH.
  - A scheduled backup cleaner can delete `.bak-*` files before the user confirms.
- **`SKILL.md`**: the delivery pass states the stdout rule for self-posting (`--post`) scripts.
- **Lap-2 additions** (`delivery-reliability.md`):
  - the log's fallback must be a NullHandler, not stderr, with library diagnostics silenced;
  - entry scripts need a guarded import;
  - scan every string literal and ban every stderr writer;
  - exercise the `__main__` guards in a subprocess.
- **`bug-hunt.md`**: re-keying rows under a composite unique index; park the rank column first.

## 1.9.35 — 2026-10-02 — vault-audit 1.3 tune-up (writeback)

- **`bug-hunt.md`** gained a vault-audit section: an unreadable ground truth collapsed into an empty one feeding a destructive cleanup (the weekly `--fix` would have rmtree'd every cron output dir on a bare-list / control-char `jobs.json` that Hermes itself accepts) — three-valued truth, parse like the producer but all-or-nothing, shape-checked names, a bulk cap, quarantine not delete; `shutil.copy2` keeps the old mtime so a same-run mtime prune deleted fresh backups; a realpath containment check against an un-realpath'd base silently disabled 7 of 12 checks under a symlinked path (found only because the review kit sat under macOS `/tmp`); an auto-fix that rewrites the machine token but not its human copy, and the "review once, then quiet" half-fix trap; table-column logic that oscillated across gate laps until simplified to "only the one table we own"; learned-regex guards (capture outside the match splices text weekly; uninterruptible ReDoS ⇒ parse-tree check; suffix-only secret filters are bypassable); per-run "seen N times" counters inflated by manual bursts; `deliver: local` + `--quiet` + exit-1-on-review leaves useless archives; an agent's undocumented in-place edit of the skill (read the whole session to learn what the user approved — a filtered export hid the approving question); harness traps (self-measuring tests, stubbing only the high-level poster, `open(p,"w").write(open(p).read())`, unstubbed posting by reviewers).
- **`SKILL.md`**: bug-hunt pass names the unreadable-vs-empty ground-truth check for skills that delete/rewrite unattended; intro list.
- **Method notes:** Phase 0 baseline came from a sandboxed run on the Pi (detect mode writes the recurrence state, `--fix` deletes) and an offline replica kit (real vault, real jobs.json, stub SKILL.md frontmatters, fake token) that reproduced it exactly once realpath'd. 4 blind lenses + a refuter (which downgraded both HIGHs by finding the Sunday 03:00 Hermes backup that zips `cron/output`, and replaced a risky VIP heuristic); 2 behaviour questions to the user (keep on-demand auto-fix; leave the 7 stale vault notes for the weekly report). Terminal gate: 6 agent laps + a self-probed lap 7 — laps 1–5 each found MEDs in the previous lap's fixes, lap 6 passed (3 LOWs, fixed and re-verified by re-running every lap's probes/fuzzers) (table parsing oscillated until "GFM delimiter rows + a heading ends a table + no fallback"; the static ReDoS check was replaced by a child-process time-box; an expr fix may only touch fields whose human copy it can also rewrite — days first, then hours); between laps the previous breaker's own probes + fuzzers were re-run as a self-probed check (the schedule fuzzer went 165 silent / 17 false reviews → 0 / 0); every lap's new tests mutation-checked against the previous lap's engine (suite 0 → 112).

## 1.9.34 — 2026-10-02 — cc-bill-review 3.5 tune-up (writeback)

- **`bug-hunt.md`** gained a cc-bill-review section: a consent parser that fails OPEN (negations, "except", two verbs, `all` anywhere) and the fail-closed fix with the user's real replies as the must-still-work set; "all" bound to what was actually delivered in that thread is a state machine (four gate laps; a lap-2 "merge two uploads" refinement re-approved explicit un-approvals in lap 3 and was removed — simplify); a display dedupe on an unverified premise hid double charges, falsified by the document's reconciliation identity (previous total + items == total, 16/16); a document's trailing total glued onto the last item (junk identity each month, special cases bolted on) + an approval-preserving migration; an id-looking field that isn't an identity (`-9113` shared by 25 merchants — the skill's own reference was wrong); exact-day reminders lose a reminder on one failure; a health check blind to `last_status`/`failure_streak` (a month of failed runs read ✅); a "harmless no-op" CLI call (`hermes cron resume` recomputes `next_run_at` from now); a fallback path that re-enters the relay bug the design avoids; usage-forensics findings (message id as thread id, a false per-command CLAUDE.md line repeated by the agent, doc contradiction → ad-hoc SQL every upload, double-quoted free text); probe the agent's interpreter per tool from real session output, not the gateway environ.
- **`delivery-reliability.md`**: split-never-slice; nonce policy (deterministic for re-run recovery + `already_there` detection, per-run salt for legitimate repeats); wall-clock bounds for interactive posts (urllib's timeout is per address); record what was delivered; one factual agent-facing stdout line.
- **`safe-edit-workflow.md`**: selftest sandboxes must isolate every side channel (a test snapshot evicted the migration's rollback copy); an AST undefined-name check after slice removals (one deleted a live function); mutation runners need `stdin=DEVNULL`.
- **`SKILL.md`**: bug-hunt pass names fail-closed consent parsing, delivered-set binding, and reconciliation identities.
- **`scripts/skill_audit.py`**: the subprocess-`timeout=` check now blanks string literals + comments with `tokenize` before paren-balancing (a `)` inside a `python -c` snippet string ended the span early → a false MED on cc-bill-review's `import_all.py`; a `timeout=` inside a string/comment no longer satisfies the check). Verified on a fixture under Python 3.9 and 3.14 (f-string tokenization differs).
- **Method notes:** Phase 0 found no PDFs left (the upload cache keeps ~7 days; the originals are password-protected — not decrypted without the user), so parser fixes were validated on synthetic OCBC-layout text + the stored raw descriptions (drift 0). 4 blind lenses + a refuter (the refuter downgraded several severities and refuted 4 claims as design); 4 behaviour decisions went to the user in one question batch. Terminal gate: 4 laps (3 agent laps — each found real holes in the previous lap's fixes — + a self-probed lap re-running lap 3's harnesses); every fix mutation-checked (suite 39 → 209; survivors only equivalent/redundant-guard mutants). The user paused Pi access mid-tune-up: all code/doc work continued on local copies with drift-guarded one-connection deploys prepared for later.

## 1.9.33 — 2026-09-30 — discord-table-format 2.0 tune-up (writeback)

- **`bug-hunt.md`** gained a discord-table-format section: a shared helper's module-level `sys.path.insert` downgrading callers' later imports; "system python" docs vs the agent's `python3` actually being the gateway venv (per-interpreter output divergence); text-layout-vs-font width mismatch (zero-width chars, wcwidth vs a monospace fallback box); a formatting library's type inference rewriting caller strings (tabulate numparse); an interactive skill that's almost never loaded because its frontmatter trigger key is read by nothing — measure *compliance* in the session history, and check the built-in platform hint for a contradicting default; an inner-function harness missing a wrapper regression; five harness traps (non-`.py` backup import, stub mode-file newline, a reviewer probe bypassing the real code path, scratch-build parity ≠ deployed-file evidence, an unredirected timing loop filling the live cache); a dedup nonce keyed on a payload subset; signal-handling laps that oscillated until removed; session exports carrying the bot token.
- **`delivery-reliability.md`**: current `send_message` facts (target required; platform-only → home channel; caption + one `MEDIA:` in one call = one message) and the hardened one-command agent delivery (token on curl stdin via `-K -` + `-q`; session-bound target with `--force-target`; nonce + `enforce_nonce`; `may_have_posted`; JSON printed on every path incl. SIGTERM; stub-curl validation under a sandbox HOME with a fake token).
- **`token-efficiency.md`**: for an apply-every-time interactive skill the `description` is the only loader — make it directive; `platform_hints.<platform>.append` is the supported platform-wide lever.
- **`SKILL.md`**: bug-hunt pass now names interactive-skill compliance and shared-helper interpreter/import checks.
- **Method notes:** 4 blind reviewers (control-flow/contract, edge-case/data-shape, session forensics over a 200-session export, doc↔code/delivery) + an adversarial refuter; the forensics lens found the biggest problems (never loaded, misrouted to #alerts, re-batched `MEDIA:`), none visible from the code. 3 decisions to the user (emoji map, platform hint, live test). Terminal gate, 4 laps: lap 1 = 3 agents (regression/contract, adversarial, delivery/security) → 26 LOW/MED findings, all fixed; the author's own probe caught a regression in those fixes (tuple-shape change broke every multi-section input) before deploy; lap 2 = adversarial agent (MED: a nonce over a payload subset silently drops a corrected re-send) — its fix-verification twin died on a daily budget cap, so that lens ran as mutation-checked self-probes; lap 3 = adversarial agent (MED: multi-signal race in the new abort handling; a `Set` check rejecting `dict.keys()`); lap 4 = removed the signal machinery (simplify) + LOW fixes, self-probed clean (every new probe fails on lap 3; all earlier suites pass; 8000-table original-vs-new differential identical). Live: 2 messages to the playground channel + a nonce re-send that returned the same message id. Every Pi step batched into one ssh; ~16 connections total, no lockout.

## 1.9.32 — 2026-09-30 — sg-commute 1.11 tune-up (writeback)

- **`bug-hunt.md`** gained eleven sg-commute classes: a label promising a sub-window while the datum's own `validPeriod` covers a superset (the evening "6–8am" line printed the 6pm→6pm forecast, 28/40 wrong); a rate classifier applied to an accumulation (5-minute rain totals labelled with hourly bands, ~12× low); counting a history of snapshots as current state (flood alerts ×25, cleared alerts "in effect", 429 read as all-clear); a paginated API read as one page and masked by today's row order (+ "fixing a truncation changes the ranking pool"); `list[0]` on a list of typed entries; an outage rendered as a factual negative and a failed scan cached as a negative result; an interactive script whose results the agent can't read (rows only in PNGs → misquoted counts); a doc recipe that teaches the agent to hand-roll the outward action (the bot token ended up in `state.db`); "post where I am" needs the session's id, not the message's (+ the exit/nonce/deadline contract); a region-restricted geocoder answering garbage with the region itself; a description missing the user's vocabulary.
- **`token-efficiency.md`**: a skill-dir `CLAUDE.md` is attached on every agent run of a script in that dir — a per-USE cost, not an edit-time one (sg-commute's 24.5 KB rode on every parking query).
- **`SKILL.md`**: the bug-hunt pass now says to read each value's own metadata (validity window, unit/interval, snapshot-vs-state, paging) and, for an interactive skill, to check the agent can read the results and that no doc teaches it to hand-roll the outward action.
- **Method notes:** 4 blind lenses (control-flow — killed by the daily budget cap and re-run after the reset, not skipped — data-shape, scheduled-output/doc-drift, usage-forensics over 12 real sessions) + 2 refuters split by surface (cron vs interactive) on an offline kit with a URL-mapping fake `curl`; 3 behavioural decisions went to the user (evening format, rain wording, parking ranking — kept). Terminal gate: 4 laps (lap 1 two agents, laps 2–3 one each, lap 4 self-probed by re-running lap 3's harnesses); laps 1–3 each broke the previous lap's fix. Secrets found printed in two old sessions were redacted from the kit and flagged for rotation. Deployed 06:46 SGT, 39 min before the 07:25 run, as one drift-guarded connection that validated every entry point and did one authorised #playground `--send`. ~15 SSH connections over the whole tune-up, all single-shot.

## 1.9.31 — 2026-09-28 — email-gmail-triage 2.1 tune-up (writeback)

- **`bug-hunt.md`** gained eight email-gmail-triage classes: a guard comparing the wrong representation (label names vs Gmail's label IDs) with a fixture sharing the mistake; an exemption written as `pass` falling through to the next early return (no-reply security alerts / renewals archived); a documented rule whose regex never matches the real format (OCBC `SGD38.41`); per-item persistence making a run-wide stamp unsafe for a strict watermark (+ the reader/writer legacy-fallback symmetry trap in the tie-break); an ambiguous mutating-call failure leaving the item out of its retry path; `ConnectionError` covering post-send disconnects; a slow bulk call inside `retry(3)`; Discord's 2000-unit limit wedging a success-gated watermark.
- **`SKILL.md`**: check fixture shapes against the real API; a randomized interleaving simulation over the real functions for any reader/writer protocol change, from every starting state incl. the legacy on-disk format.
- **`delivery-reliability.md`**: `--failure-deliver` now configured on a real skill (not yet seen firing).
- **Method notes:** 3 blind reviewers (control-flow/state, data-shape, perf/delivery) + a refuter, offline kit incl. `state.db` user messages; the refuter demoted most severities and found the one the others missed (security alerts archived by branch order). 4 behavioral decisions went to the user in one question batch. Terminal gate: lap 1 agents (regression hunter found the tuner's own watermark race), lap 2 agent (20k-interleaving simulation found the legacy tie-set wipe), lap 3 self-probed by re-running lap 2's harnesses; every lap's new tests mutation-checked against the previous lap. ~9 SSH connections, all batched.

## 1.9.30 — 2026-09-28 — foodpanda follow-up: don't design for an edge the user says can't happen

- **`bug-hunt.md`** (*report scheduled before its window's data lands*): weigh the user's real behaviour before building for a timing edge. The foodpanda month-end catch-up sync — for orders after the last 23:30 sync — was removed the same day at the user's direction (they essentially never order that late); only the failed-sync heads-up remained. Also that day, the user moved the weekly report back to Sunday 09:05 with a previous-Sun–Sat window (their reason, DeepSeek off-peak pricing, doesn't apply to no_agent jobs — said so, then did it; both windows are complete).
- **Method note:** this writeback's deploy was drift-guarded and **aborted** — a concurrent morning-brief session had written 1.9.29 to this skill in between; rebased on the live copy instead of clobbering it (the *another session may have changed the skill* class, from both sides the same day).
- **`safe-edit-workflow.md`**: one tar per skill. Two skills' `SKILL.md` files shared a path in one archive, so the target's `SKILL.md` went live as this skill's file for about a minute. A post-push re-hash caught it; always run that re-hash last.

## 1.9.29 — 2026-09-28 — morning-brief 1.12 follow-up (writeback)

- **`bug-hunt.md`**: an editorial reviewer lens for curation skills (frozen-clock replay,
  judge each pick, scan the unpicked); removing candidates exposes a missing quality bar;
  provider-side reasoning-token changes empty `max_tokens`-bound outputs; stale defensive
  heuristics misfire; another session may have changed the skill since your deploy.
- **`SKILL.md`** terminal-gate section: an editor among the reviewers for curation skills.
- **Method note:** the reader's complaint two days after 1.10 was right; 1.10's ten laps had
  no editorial lens. The 1.12 fix ran 3 gate laps (one editorial) and was deployed in one
  SSH connection with a drift guard against the live 1.11.1 snapshot.

## 1.9.28 — 2026-09-28 — foodpanda-expense-tracker 1.7 tune-up (writeback)

- **`bug-hunt.md`** gained eight foodpanda bug-classes + one method note: a report scheduled before its own window's data lands (the weekly report never contained Sunday — 11/18 reports short, S$599; the month-end reminder had the same shape); a deadline sized for a watchdog that isn't the real one (docs said ~120 s, `config.yaml` said 600) with the alert path gated by that same deadline → silent stall; a per-partition fixed cost that grows into a fixed deadline; `retry()` around a non-idempotent POST (read timeout → duplicate ping; urllib `URLError` vs bare `TimeoutError`; Discord `nonce`); a shared subject catching a sibling product (Grab rides → false "format changed" alerts); a test flag that performs the outward action (`--simulate-date` posted and cancelled the real fire); a cursor hold for untimestamped items freezing on a permanent one; an escaping fix at one call site upgrading a skip into a crash; mutation-check each lap's tests against the previous lap.
- **`delivery-reliability.md`**: non-idempotent POST retry policy, post-deadline single-shot/once, and the `--failure-deliver` cron option (noted, not exercised).
- **`performance.md`**: count spawns as a function of data growth; batch per-partition reads; size deadlines from the real watchdog.
- **`safe-edit-workflow.md`**: check the host clock before any time-relative cron change (a "today 10:00" one-shot got scheduled for next year).
- **`SKILL.md`** bug-hunt targets + the gate's mutation-check rule.
- **Method notes:** 3 blind reviewers + a refuter (the first refuter stalled on the watchdog and was relaunched — counted as no result, not a pass) all offline from a local kit; the terminal gate ran **3 laps** and laps 1–2 each found real holes in the previous lap's fixes (the headline: the tuner's own watermark hold was a MEDIUM regression → reverted, "simplify, don't add a rule"); lap 3 was self-probed by re-running the lap reviewers' own simulations against the final code. Two user decisions (weekly → Monday for the previous week; a catch-up sync before the reminder). ~10 SSH connections, all batched, no lockout.

## 1.9.27 — 2026-09-26 — morning-brief 1.10 tune-up (writeback)

- **`bug-hunt.md`** gained seven morning-brief 1.10 lessons: tally archived OUTPUT against
  the user's complaint history (4/12 "green" briefs were short); re-measure a doc's
  "validated" threshold over a full captured pool (the 12% dedup's "zero false positives"
  was ~50% wrong); capture one raw input on the host and replay it offline; stateful
  multi-day heuristics need a multi-day simulation; when laps oscillate, simplify; fixtures
  that pre-set pipeline fields hide crashes (and mutation-test the suite); compute derived
  state where its inputs exist.
- **`safe-edit-workflow.md`**: sandboxed host runs — `HOME` override + `PYTHONUSERBASE`, run
  with the wrapper's interpreter, prove the live data untouched.
- **`SKILL.md`**: bug-hunt targets and the terminal-gate section note the above.
- **Method notes:** the gate ran 10 laps (the most so far), all offline from a local kit —
  no SSH lockout this time (~9 connections for the whole tune-up, each batched). Two user
  decisions shaped the design (C-tier must-read needs corroboration; hold back already-shown
  stories).

## 1.9.26 — 2026-09-24 — task-tracker 1.7 tune-up (writeback)

- **`bug-hunt.md`** gained four task-tracker bug-classes:
  - **A prose write procedure that the LLM re-implements every run is the bug.** It was found only by *usage forensics*: a `state.db` session export put in the offline kit, with a dedicated reviewer lens. 39 writes, 8 broken or retried hand-written scripts, and 2 in-place edits of the live JSON. The fix is script subcommands.
  - **A displayed index must resolve against the view the user saw** (a per-thread snapshot of delivered rows), not a fresh re-sort.
  - **Seven undo-design traps**, each found in turn by a terminal-gate lap. The end state is an undo stack of pre/post hashes, exact-byte restore, no "newest backup" fallback, and never deleting backups.
  - **Ship a regression suite with the skill** once it writes data. Rerun it several times: a flaky check was a real same-second name-reuse bug.
- **`SKILL.md`** bug-hunt targets now name the prose-procedure-re-implemented class.
- **Method notes:**
  - The terminal gate ran **6 laps**. Laps 1–5 each found a real HIGH or MED in the previous lap's fix: word-splitting and fuzzy over-match; undo crossing hand edits; the double-undo hole; undo wedging and deleting history; and the dry run mutating state plus the `--force` advice. Lap 6 checked the final two fixes.
  - The hunt's refuter correctly showed that the snapshot design the user chose would still mis-resolve the one real correction incident. The docs and the id-based correction recipe handle it; the user's design choice was kept.
  - **The orchestrator's own deploy loop tripped the Pi's SSH ban again**, from about 3 ssh calls per deploy × 6 deploys, even though the reviewers were offline. Batch every deploy and validate step into ONE ssh connection (a single `tar | ssh 'tar x && …'`). A reviewer subagent also stalled (watchdog, 15 min); it was relaunched rather than counted as a pass.

## 1.9.25 — 2026-09-23 — spotify-playlist-refresh 2.17 tune-up (writeback)

- **`bug-hunt.md`** gained nine bug-classes from this tune-up. The headline: an inferred quantity built on an unverified timestamp semantic. Spotify's `played_at` is a play's END, so every listen time was shifted one row, and all 6 permanent bans were false. The fix was one gap-vs-duration correlation query. The others:
  - one-direction upsert rules that encode the old semantics;
  - sample-then-filter underfill, with the dry-run *section breakdown* as the tell;
  - a biased cumulative-weight sampler;
  - cache TTL equal to the job period;
  - a partial refetch overwriting good cache;
  - a migration marker written before commit;
  - a logging-only call starving the essential write's stage budget;
  - a fail-fast threshold stricter than baseline;
  - virtual-clock probe artifacts.
- **`performance.md`**: memo + pacing + TTL levers for rate-limited catalogue APIs. Cold API calls went from 92 to 52, warm from 49 to 1.
- **`SKILL.md`** bug-hunt targets now name timestamp-semantics and sample-then-filter.
- **Method notes:**
  - The terminal gate ran 3 laps. Lap 1 found a HIGH in the author's own stage-budget fix: the snapshot GET could starve the PUT. Lap 2 found a MEDIUM in the migration marker. Lap 3 was clean.
  - The adversarial refuter corrected two over-claims: "cold every run" was really every other run, and the upsert rule had to change too.
  - Reviewers worked offline from a local kit (code + DB clone, no SSH), per the fail2ban lesson. Live verification was done by staging on the Pi in `/tmp` with read-only API calls before deploying.

## 1.9.24 — 2026-09-15 — entry-point coverage + the shared-state bug class

Found immediately after morning-brief 1.9.0 shipped, by running the skill's on-demand tool.

- **Phase 0 now requires enumerating and exercising EVERY entry point**, not just the cron
  path. morning-brief's `feed_doctor.py` reuses `summarize.call_model` without arming its
  module-level deadline; the tune-up's freshly-extracted bounding helper turned that `inf`
  default into `Thread.join(timeout=inf)` → `OverflowError`, killing every LLM
  candidate-generation call. Four gate laps missed it because all of them drove the cron path.
- **`bug-hunt.md`** gained the bug class: extracting shared code breaks callers that never
  initialize its state; make the helper tolerate an un-armed sentinel rather than trusting
  callers; and the tell — a tool that catches its own errors prints a normal-looking report
  with a silently degraded result, so grep run output for "failed/unavailable/skipping" even
  on exit 0.

## 1.9.23 — 2026-09-15 — morning-brief full tune-up (writeback)

Provenance for the morning-brief 1.9.0 tune-up. What the method learned:

- **The terminal gate needed FOUR laps**, and every lap found a real regression in the
  previous lap's fix. `SKILL.md`'s re-entrancy rule now says so explicitly, because "fix it and
  re-run" reads like a one-lap formality. The worst was self-inflicted: making a
  feature-downgrade flag process-wide (a legitimate optimisation) lost **7 of 8** LLM summaries
  under the skill's own concurrency, and the first fix for THAT lost 8 of 8 when two params
  were rejected.
- **A comment asserting a bound is a claim to be tested.** Three successive "a dripping
  provider can no longer overrun the budget" docstrings were false. New golden rule; the
  three-hole anatomy (`timeout=` re-arms · `iter_content` blocks · the header phase precedes
  any watchdog) and the working pattern (joined daemon thread + `socket.shutdown` watchdog +
  bounded incremental inflate) are in `performance.md`.
- **`bug-hunt.md`** gained: one publisher subscribed twice breaks identity *and* corroboration;
  a rank cap that discards instead of demoting inverts the ranking; a dedup that drops an item
  must record it; a shared ledger's reader and writer must share one retention filter;
  `except (JSONDecodeError, IOError)` misses `UnicodeDecodeError`; everything between
  "delivered" and "state written" must be crash-proof; `no_agent` cron discards stderr on
  success; a wrapper's own stdout is block-buffered on the cron pipe.
- **`delivery-reliability.md`** gained: Discord counts UTF-16 units; record delivered-state
  per message so a partial send re-sends only what didn't arrive; a fixed body cap doesn't
  bound the message; `--dry-run` must share the message builder; sanitize the LINK not just
  the title; the free-text field is the one nobody collapses to one line.
- **`token-efficiency.md`**: resolved a real inconsistency in this skill's own sequence —
  runtime prompt trimming is a CODE change and belongs in the code passes, under the gate; only
  its documentation belongs in the docs-last pass.
- **`safe-edit-workflow.md`**: stage an unverified round in a scratch dir ON THE HOST when a
  scheduled run is imminent (and let that run be your live validation); a reviewer fan-out can
  lock the *orchestrator* out of the host; **a reviewer that lost host access still returns a
  report** — demand pasted evidence and treat its "no findings" as no result; late gate laps are
  cheaper to run yourself than to fan out (one reviewer died on a daily spend cap).

## 1.9.22 — 2026-07-19 — foodpanda follow-up: measure-before-you-timeout

The first scheduled cron run after the 1.9.21 foodpanda tune-up **failed** — the hardening's `CALL_TIMEOUT=20` false-tripped the cold `gmail search` subprocess (measured 16–25s, because it hydrates the full `--max 50` of historical receipts). It failed *cleanly* (bounded retry + a posted alert, not a silent SIGKILL — the hardening worked), but a guessed timeout value turned "occasionally slow" into "fails nightly." Fixed in foodpanda 1.5.1 by narrowing the Gmail search to `after:<watermark−1d>` (~2s, behaviour-preserving) and raising the timeout to 30 / deadline to 75; the crash alert was also made a short plain-English `friendly_error()` line instead of a traceback dump.

Lesson written back to **bug-hunt.md** (appended to the timeout bug-class): when you add a `timeout=`, MEASURE the call's real **cold** latency (fresh process, real auth/network) before picking the value — offline import-tests and the gate's timing *math* both pass while the real subprocess is far slower; and prefer shrinking the work (narrow the query window to the incremental watermark) so the call is fast *and* the timeout is comfortable. A `no_agent` cron exposes a too-tight timeout unattended on the very next run.

## 1.9.21 — 2026-07-18 — foodpanda-expense-tracker tune-up writeback

Full tune-up of foodpanda-expense-tracker (shipped its 1.5.0), run to assess the prior day's timeout-crash fix (2026-07-17) and the same-day new Grab multi-provider code. Phase-0 baseline from archived cron outputs (the last *scheduled* sync was the 07-17 `TimeoutError` crash). Bug hunt fanned out 3 blind reviewers (control-flow / data-shape / failure-modes) over a local snapshot (no SSH); the re-entrant terminal gate re-ran a regression lens + an adversarial refuter over the cumulative diff and **caught a regression in the tuner's own headline fix**.

Headline: the 07-17 fix was correct but **incomplete** — it wrapped only the one call that crashed (`get_all_sheets().execute()`) and left `run_gapi`'s subprocess + every other `.execute()` unbounded, so a hang elsewhere still hit the ~120s cron SIGKILL (no alert). Completed with `socket.setdefaulttimeout(20)` + `subprocess timeout=20` + a run-global `_run_deadline` gating `retry()` pre-start/pre-sleep. The gate's round-1 refuter showed the first cut still breached 120s (deadline checked only *between* attempts + the crash alert ran the full retry storm); round-2 added the pre-start/pre-sleep gates + a single-shot `quick_alert(timeout=8)` and lowered the budget to 80 (80+20+8=108<120). Other fixes: report-once alert re-fire when a skip is newer than a fetch failure (persisted `reported_skip_ids`), `norm()` currency crash, `_parse_pickup_date` calendar validation, merchant Discord-markdown sanitization, crash-traceback truncation. A 1-reviewer FoodPanda-regex claim was **refuted by a real-receipt probe** (probe beat vote), not fixed.

Three method lessons written back:
- **bug-hunt.md** — a timeout/hang fix that bounds only the crashing call is INCOMPLETE, and `retry()` around an unbounded per-attempt call can itself exceed the watchdog; bound every boundary (`subprocess timeout=`, `socket.setdefaulttimeout` for in-process googleapiclient) + a run-global deadline gating retry() pre-start/pre-sleep, keeping `RUN_DEADLINE + CALL_TIMEOUT + crash-alert < watchdog`. Plus a sharpening of the report-once class: an alert re-fires when a deterministic skip is *newer* than a still-retryable fetch failure that pins the watermark — dedup on a persisted reported-ids set saved independently of the watermark/any_failure guard.
- **delivery-reliability.md** — the top-level crash alert must be a SINGLE bounded post (short timeout, no retry, truncated body), not the normal retry envelope: a retry-storm alert fired late in a run gets SIGKILLed with the run, losing the only failure signal on a `deliver: local` job.

## 1.9.20 — 2026-07-14 — spotify-playlist-refresh tune-up writeback

Full four-pass tune-up of spotify-playlist-refresh (shipped its 2.13.1) after recent filter edits. Bug hunt fanned out control-flow + data-shape + adversarial-refuter reviewers over a local snapshot + DB clone (SSH forbidden, per the fail2ban lesson); the terminal gate re-ran them over the cumulative diff and caught a leftover in the author's own fix. Fixes: a self-introduced live-filter over-match (`Live in/on …` dropped studio titles), a successful-refresh-reported-as-failure (post-replace snapshot/vacuum sat inside the replace try/except), em-dash normalization in all dash branches, and dead-code removal. Perf + delivery: no change (once-daily API-bound cron; scheduler-delivered no_agent already correct). Regression suite 70→79.

Three method lessons written back:
- **performance.md** — a fingerprint that embeds `now()` (recency decay, days-until-due) is **not a stable baseline**: it drifts every call with identical code+data, so a changed hash isn't a regression. Prove behavior-preservation by a byte-identical **source hash** (for an untouched module) or a time-invariant projection (count + top-N ordering), not the float hash. (compute_affinity's hash moved `6f98…`→`a9b1…` with n=52 steady — pure decay.)
- **bug-hunt.md** — (a) removing a dead function orphans its imports, and a naive `re.search(r"X\.", src)` usage-check **matches the `import` line itself** → the orphan ships; check usage excluding the import statement (AST or non-import grep), for every name on a multi-import line (the terminal gate caught `urllib.parse` this way). (b) A **self-introduced false-positive is a defect to fix even when the refuter shows "0 rows hit today"** — distinct from a genuine behavioral-breadth call; "un-blocks real items, blocks nothing new" is a bug fix, not a retune.

## 1.9.19 — 2026-07-13 — token-usage zero-fill follow-up

Same-engagement follow-up to 1.9.18: after anchoring token-usage's daily table to 7 SGT calendar days, the user immediately hit the *other* half of the windowed-report class — the query's `GROUP BY` dropped a zero-session day (Jul 8), so "Last 7 Days" rendered 6 rows. Fixed in token-usage 1.1.1 by zero-filling from a Python date spine (always 7 rows; a no-session day is an explicit `0`/`$0.0000` row); validated on live data (7 rows incl. Jul 8, width unchanged, `py_compile` + stubbed end-to-end run clean) and docs synced. Extended the `bug-hunt.md` calendar-window bug-class bullet with the zero-fill half + the note that it's a foreseeable follow-up to surface *with* the window change.

## 1.9.18 — 2026-07-12 — token-usage tune-up writeback

Applied the method to `token-usage` (Phase 0 → bug hunt fan-out [3 blind lenses + adversarial refuter] → perf [no-op, already fast] → delivery rewrite → terminal gate [2 reviewers over the cumulative diff, re-entrant] → docs-last). Live copy = `~/.hermes/scripts/token_report_no_agent.py` (byte-identical to the skill-dir copy — no drift). Behavior-preserving delivery proven by identical rendered-PNG md5s under a stubbed curl; a read-only `GET /channels/{id}` (200) confirmed reachability without posting. token-usage bumped 1.0.0 → 1.1.0.

**What shipped in token-usage:** hardened Discord delivery (`discord_post`: `--max-time`/`timeout` + `TimeoutExpired`, HTTP-status check, one clamped 429 retry, `--form-string payload_json=` + `allowed_mentions`, cleanup-only-after-confirmed-send, `os._exit(0/1)` so a silent failure now alerts); daily window anchored to exactly 7 SGT calendar days (was a rolling 168h → up to 8 partial buckets); deleted the deprecated `token_report_dispatch.py`; SKILL.md ~2916 → ~2807 tok while syncing all the new behavior.

**New lessons fed back:**
- `bug-hunt.md`: (a) a rolling `now − Nh` window GROUPED BY calendar day gives a variable partial-bucket count that shifts with the run's clock — anchor to a calendar boundary for "N calendar days" (behavioral/framing call, surface it); (b) a consolidated skill can leave orphaned same-stem sibling scripts in `~/.hermes/scripts/` that no cron runs but still carry old anti-patterns — grep same-stem siblings vs `jobs.json` in Phase 0.
- `delivery-reliability.md`: prove a delivery-transport change (`-F content=` → `payload_json`) is content-preserving without a real send by stubbing curl to emit `\n200` and md5-ing the would-be-attached file (then flip to `\n500` for the failure path).
- `SKILL.md` (Phase 0): scope `skill_audit.py --scripts` to the skill's own live code — `--scripts ~/.hermes/scripts` scans every skill and buries the target's findings; default scan when the live copy is a byte-identical skill-dir copy.

## 1.9.17 — 2026-07-12 — self-tune-up (airtightening the method + auditor)

Applied the method to itself (Phase 0 → fan-out on `skill_audit.py` + the method docs → adversarial refuter → terminal gate → docs-last). Live copy = the skill-dir `skill_audit.py` (interactive method, no cron). Two blind reviewers (auditor-code lens + method-airtightness lens) + the fresh sg-commute pass as a stress-test. All fixes validated on real skills (sg-commute, self) + synthetic edge cases.

**`skill_audit.py` fixes (the one executable — validated end-to-end):**
- **Stray-file glob missed the real backup convention (HIGH, self-demonstrated).** Glob was `*.bak`; the actual convention (now reconciled in `safe-edit-workflow.md`) is `<file>.bak-<tag>-<date>`, which does NOT end in `.bak`. Added `**/*.bak-*` (NOT `*.bak*` — the refuter showed it over-matches a legit `.bakup`). The fixed glob immediately surfaced **~24 accumulated backups** the broken pattern had hidden in this skill's own dir across prior tune-ups.
- **`MEDIA:` matched explanatory prose (MED FP).** `print("…MEDIA: tags…")` flagged HIGH. Changed regex to `MEDIA:(?!\s)` — real tags are `MEDIA:<path>`/`MEDIA:{var}` (no space); kills the prose FP, keeps every real emission. Message now notes a MEDIA: on a legit no_agent/terminal path is expected — eyeball.
- **"TimeoutExpired not caught" fired under a broad `except` (MED FP)** and **the `timeout=` count was global (MED false-negative).** Restructured: count subprocess-call-specific `timeout=` (an unrelated `requests`/socket `timeout=` no longer credits a missing subprocess timeout), and suppress the TimeoutExpired flag when a broad `except`/`except Exception` is present (it catches TimeoutExpired). Note: the FP fix would have *created* the FN, so both had to ship together.
- **token-from-env HIGH now downgrades to CHECK** when the same file also reads `~/.hermes/.env` — implementing the refinement `bug-hunt.md` had recorded as "worth making" but never built (a dead recommendation, now closed).
- **Link-integrity** strips a trailing `--flag`/`#anchor` before the exists-check (a backticked `scripts/x.py --json` no longer reads as a dangling link). **Self-exclusion** now also skips by basename (a *copy* of the auditor would self-flag on its own regex literals), and the "0 scripts scanned" false-clean warning now distinguishes "only the auditor's own source was here" from a real false-clean.

**Method-doc airtightening:**
- Promoted the two headline sg-commute lessons to **Golden Rules**: *validate a scheduled skill at its REAL trigger time* (archived cron outputs; a time-relative output looks right off-schedule) and *the skill's OWN references can mislead the fan-out* (a wrong "verified" doc manufactures reviewer agreement against a true finding — re-verify the doc, and fixing it is part of the fix). Both were buried in `bug-hunt.md` prose.
- Phase 0: added the scheduled-skill baseline source (archived cron outputs) + a "the anti-pattern flags are line-local heuristics — eyeball before fixing working code" note describing the new CHECK/suppression behavior.
- "Always finish with": clarified the closer audit is **expected-noisy** (in-flight backups under `[STRAY FILES]`, a deferred `LARGE-TRIM`) and that only new dangling links / new anti-patterns / unsynced drift block "done".
- `safe-edit-workflow.md`: reconciled the backup-naming convention to the `<file>.bak-<tag>-<date>` form actually in use, and fixed the cleanup `find` to `\( -name '*.bak' -o -name '*.bak-*' \)`.
- `token-efficiency.md`: added a doc-sync check that every writeback has a `## <version>` header and exactly one top-level `#` title.

**Self-inflicted doc drift fixed (F5):** the `1.9.14` (task-tracker) writeback had shipped with **no version header** (invisible to a version grep) and a stray `# Changelog` H1 sat mid-file — added the header, removed the stray H1.

## 1.9.16 — 2026-07-12 — sg-commute re-tune-up writeback

Second full tune-up of `sg-commute` (v1.9.8 → 1.9.9). Live copies: `~/.hermes/scripts/morning_commute.py` + `evening_weather.py` (no_agent crons), `scripts/nearest_carpark.py` (interactive) — all matched their live shasums (no curator drift). Auditor: SKILL.md ~5.7K tok, links OK; the two `MEDIA:`/subprocess anti-pattern flags were verified false-positives (interactive terminal path + explanatory print; every `subprocess.run` has a timeout and is wrapped). Baselines captured for all three; the 24hr-period label question was settled against **archived 07:25 cron outputs**, not a fresh off-schedule run.

**Headline finding — a HIGH bug two blind reviewers REFUTED by citing a wrong doc.** `morning_commute.py` `section1` hardcoded the three 24hr-forecast period labels; the API's periods slide with the clock, so at the 07:25 cron time they were all shifted forward one slot (proof: three archived outputs printed `6am–Midday tomorrow: Partly Cloudy (Night)` — a night code under a daytime label). Fixed by deriving each label from the period's own `timePeriod`. The terminal gate then caught a **regression in my own fix** (reading the new `timePeriod.text` field outside section1's inner guard → a future dict-migration would crash the whole section) — hardened before ship.

Three lessons written into `references/bug-hunt.md`:
1. **Bug-class — hardcoded label/index list over a TIME-RELATIVE API window** (forecast periods, "next N hours", tide/market windows): correct in the window you test, wrong at the scheduled run time. Validate at the REAL trigger time via archived cron-output docs; look for an internal-consistency tell (a Day/Night code contradicting its label) rather than trusting the labels.
2. **Fan-out lesson — a stale/wrong reference DOC can manufacture reviewer AGREEMENT against a true finding.** When a reviewer cites a doc (esp. one saying "verified") to refute a live-confirmed finding, re-verify the doc against live data; fixing the wrong doc is part of the fix (else it re-arms the same false refutation next tune-up). The doc-side of *don't trust a reviewer's "I verified it"* + a live *documented ≠ verified*.
3. **Bug-class — a fix that starts consuming a NEW field inherits that field's failure modes** (null, future shape-migration a sibling field already underwent); guard the new read as tightly as the code it replaces and normalize via the skill's existing dict-or-string helper.

Also: EV availability was NOT the "always-0" bug one reviewer suspected from a stale committed sample — the live `evIds[].status` is populated (refuted via a raw-API probe); the real (LOW) issue was a *failed live refresh* rendering `0/N` (looks full), fixed with an `available_live` confirmed-flag → `?/N` when unconfirmed. Deferred: the SKILL.md "Daily Morning Commute Report" section is still `LARGE-TRIM` per the auditor — left for a focused token pass to avoid introducing doc drift immediately after the code changes (docs-last discipline).

## 1.9.15 — 2026-07-11 — morning-brief tune-up writeback

Full tune-up of `morning-brief` (v1.7.0 → 1.8.0): a 3-stage RSS→LLM→Discord pipeline
run by a `no_agent` + `deliver: local` cron whose `script` is a thin wrapper
(`~/.hermes/scripts/morning_brief_run.py`) that shells out to the skill-dir pipeline
scripts under `/usr/bin/python3`. Live copy = the skill-dir `scripts/` (the wrapper points
there — no copy↔copy drift). Green baseline: `send_brief.py --dry-run` over the frozen
`articles_summarized.json` (deterministic sha), footer math balanced. Performance measured
~23s (fetch 20s + summarize 3.6s + send 0.2s) vs the box's 600s watchdog — nothing to
optimize (2026-06-06 concurrency tuning already won). Bug-hunt fan-out (control-flow /
data-shape / security) + adversarial refuter, then a fresh terminal-gate fan-out over the
cumulative diff; reviewers ran **offline against a local snapshot** (no SSH during fan-out).
Dry-run stayed byte-identical to baseline through every push (behavior-preserving).

Shipped fixes: `allowed_mentions:{"parse":[]}` + `format_headline`/summary sanitization
(untrusted feed titles — Reddit self-posts); best-effort feed-health notice; atomic
seen/health writes; guarded intermediate loads; a maybe-chunk oversized-split; and the
headline reliability win — a wrapper-level `fail()` self-notify + per-step subprocess
timeouts (deliver:local silent-failure fix). Docs: SKILL.md slimmed ~9.6k → ~5.8k tokens
(31 pitfalls relocated to `references/pitfalls-log.md`), doc↔code drift fixed, cron
`skill`/`prompt` fields cleared to prevent agent-mode relapse.

New lessons written back:
- `references/bug-hunt.md` — **the auditor's `token-from-env` HIGH is a line-local check**:
  a `~/.hermes/.env` fallback *later in the same file* mitigates it (the recommended
  env-key-loading pattern), so it's a false-positive; verify the whole file (and that
  delivery works) before "fixing" working code. Method refinement noted: the anti-pattern
  scan should downgrade the HIGH when the file also reads `~/.hermes/.env`.
- `references/delivery-reliability.md` — **deliver:local self-notify for a MULTI-STEP
  wrapper**: put `fail()` + per-step `subprocess.run(timeout=…)` in the wrapper (sum < the
  global watchdog so a hung step fails *with* a notify before SIGKILL), budget the caps by
  worst-case (delivery step gets the largest slice), catch launch `OSError` **and**
  `TimeoutExpired` **and** non-zero exit (catching only `TimeoutExpired` lets a launch error
  crash past the notify — an *except-narrower-than-the-failures* gap the gate caught in the
  fix's own code), use stdlib `urllib` (venv runs the wrapper, children use system python3)
  with a `User-Agent` set, and suppress the post under `--dry-run`.
- `references/safe-edit-workflow.md` — **the orchestrator's OWN rapid `ssh` loop can trip a
  local EPERM connection block** (distinct from the reviewer/fail2ban lockout: local
  `Operation not permitted` on connect, not remote `Permission denied`). Batch remote work
  into ONE connection (pull via one `tar` stream, push via `tar | ssh 'tar x && …'`, validate
  via `ssh 'bash -s' < validate.sh`); on EPERM stop looping and wait a quiet interval.



## 1.9.14 — 2026-07-11 — task-tracker tune-up writeback

Full tune-up of `task-tracker` (the founding skill), taken 1.5.0 → 1.6.0. An
*interactive* (non-cron) skill: no `jobs.json` entry, so the live copy is the
skill-dir `scripts/task-tracker.py` the SKILL.md tells the agent to run — and the
auditor's anti-pattern scan is **not** a false-clean here (the code IS in the skill
dir, unlike a `no_agent` cron). 3 blind reviewers (control-flow / data-shape /
delivery-security) + an adversarial refuter during the hunt, then a fresh
terminal-gate fan-out over the cumulative diff. Reviewers ran against a **local
snapshot + data clone** (no SSH into the Pi during the fan-out — the cc-bill-review
fail2ban lesson). The `--json` fingerprint was byte-identical before/after
(behavior-preserving on real data). No perf change — compute is ~0.5 ms; the CLI→curl
migration (1.3.0) already won.

New bug-classes added to `references/bug-hunt.md`:
- **An unguarded per-row parse (`date.fromisoformat`) lets ONE malformed row crash
  the whole report — and kills a *secondary* consumer that reads the same output.**
  task-tracker's `build_sections` parsed each `last_done` with no guard; a single
  non-zero-padded date (`"2026-7-6"` — realistically produced by the natural-language
  write path, and *not* caught by key-presence validation) `ValueError`'d and blanked
  all 31 tasks in every mode **including `--json`**, which the agent runs to resolve
  "done N" → table row. Fix: guard per-row, keep the bad row **visible** ("Bad date")
  so the running counter the resolver depends on stays stable; add an ISO-format check
  to the write path too.
- **A `try/except` scoped to the *parse* but not the *arithmetic that consumes the
  parsed value* leaves a second crash site — caught by the gate as a regression in the
  fix's own claim.** The bad-date guard wrapped `fromisoformat` but not the later
  `ld + timedelta(...)`, so a date near `date.max` (year 9999) that *parses* then threw
  an uncaught `OverflowError` — defeating the "one row can't crash the report" promise.
  The non-adversarial regression reviewer confirmed byte-identical good-data output and
  missed it; only the **refuter's year-9999 probe** surfaced it. Guard a parsed value's
  *whole lifetime*, not just its parse. (A guard-placement cousin of *`except` narrower
  than the failures*.)

Sharpened `references/delivery-reliability.md` (Hardened multipart section):
- **`-F content=<dynamic text>` is itself a bug** — curl's `-F` treats `;` as a
  form-part parameter separator and **silently truncates** the caption at the first `;`
  (and a leading `@`/`<` reads a local file). Reachable wherever stored/untrusted text
  reaches the caption (a task name with `;` in `--text-only`). Proven on the Pi's curl
  7.88.1 via a local-socket byte capture. So prefer `payload_json` for *any* non-literal
  caption, not just for mention-suppression.
- **The fix MUST use `--form-string payload_json=`, NOT `-F payload_json=`** — the
  adversarial refuter killed the naive `-F payload_json=` fix: a `;` in the content
  truncates the JSON blob mid-string → invalid JSON → HTTP 400 (worse than the silent
  drop it replaced). Only `--form-string` disables `;`/`@`/`<` interpretation. Verify
  on-wire bytes with `--trace-ascii`/a local socket — don't assume `-F` ≡ `--form-string`.

Process wins reaffirmed: the **terminal gate again caught a regression in the author's
own fix** (the OverflowError) — the single most valuable thing it did; and the
**adversarial refuter killed a plausible-but-wrong fix** (`-F payload_json=`) before it
shipped. Both are textbook *review-your-own-edits* / *the refuter earns its keep at the
gate*. Delivery change validated without posting: `GET /channels/<id>` → 200 and an
empty-content `payload_json` probe → `400/50006` (proves the new request shape is parsed).

## 1.9.13 — 2026-07-11 — cc-bill-review tune-up writeback

Ran a full tune-up on `cc-bill-review` (OCBC statement parser/reviewer/reminder).
New bug-classes + method notes added to `references/bug-hunt.md`:

- **CLI-not-on-non-login-PATH health check false-alarms** — a check that shells
  out to a user-installed CLI (`hermes`, at `~/.local/bin`) fails with
  FileNotFoundError in the non-login shells the agent terminal + cron use, and
  reports the subject absent. cc-bill-review's `verify_cron_health` cried
  "🚨 reminders may NOT fire" after every import. Fix: read the structured source
  of truth (`~/.hermes/cron/jobs.json`) directly; resolve the binary by absolute
  path only for mutating actions.
- **Health check must be three-valued** (healthy / unhealthy / undeterminable) —
  collapsing "can't tell" into "bad" false-alarms on any transient source outage.
- **Validate under BOTH interpreters + lazy heavy imports** — an interactive
  script (system python3, user-site on) vs its no_agent cron (gateway venv,
  user-site off) can import differently; a module-top `import pymupdf` coupled the
  reminder path to a dep it never uses (silent import-death risk on deliver:local).
- **Normalizer strip-set eats legit tokens; prove behaviour-preservation by
  re-normalizing every stored raw and diffing** — full ISO country-code set
  deleted merchant tokens "MP"/"LA" → false "new merchant". Fix accepted only at
  drift 0/130. And: fixing an over-broad strip can regress lookups riding on it
  (BY/LT were load-bearing for canonical-map keys) — keep the fix minimal + drift-
  verified, not broadly "principled".
- **Per-item send bookkeeping** — a no_agent wrapper that marks all N messages
  "sent" after the loop re-delivers the successful ones when a later send fails;
  mark each item immediately after its own success (independent sends ≠ one unit).
- **Fan-out reviewers hammering one host can trip its sshd auth throttle
  (fail2ban) and lock the whole tune-up out** — point reviewers at a local
  snapshot + DB clone (forbid SSH), cap concurrency, and don't retry into a ban.

The terminal gate again earned its keep: it caught **two regressions the fixes
themselves introduced** (a `NameError` from a half-renamed variable in the
reworked health check, and prose-number over-capture in the approve id-parser)
before ship. Target shipped at cc-bill-review v3.3.0; SKILL.md 13.4K→3.5K tok.

## 1.9.12 — 2026-07-07

Lessons fed back from a full tune-up of the freshly-built `backup-cleaner` skill (a DELETER:
removes stale `.bak` files only when the update they preceded was validated by a successful
cron run). 3 blind reviewers (control-flow · data-shape · delivery/security) + an adversarial
refuter; every finding reproduced on synthetic fixtures; a shipped regression suite
(`scripts/test_backup_cleaner.py`) added as the baseline and extended with a case per fix. The
**terminal gate caught a regression in the author's own fix** (a resolver disambiguation-order
change that mis-resolved a backup-of-a-backup) — the single most valuable thing the gate did.

New bug-classes added to `references/bug-hunt.md`:
- **Suffix-stripping resolver: ambiguous-marker + disambiguation-order traps** — an over-broad
  `.<word>.bak` marker matches real extensions (`SKILL.md.bak`→`SKILL`), and "first-marker" vs
  "longest-existing-candidate" have opposite failure modes (first-marker over-strips a
  backup-of-a-backup). Fix: drop extension-colliding markers + resolve to the longest existing
  candidate.
- **Basename-only owner/job match** validates a same-named file elsewhere in the tree — gate on
  the exact path the unit runs (`~/.hermes/scripts/<script>`), not the basename.
- **A disabled/paused source's stale `ok`** is not validation — exclude `enabled:false` from the
  evidence set.
- **Hardcoded location roots block fixture testing + couple to one layout** — make skills/scripts
  roots env-overridable so the shipped suite can drive the location-classifier branches.

Also reconfirmed (already documented, applied here): the `deliver: local` self-post-failure
surfacing (delivery-reliability.md / vault-audit 1.1.2) and naive-timestamp-assumed-UTC both bit
backup-cleaner and were fixed the documented way.

## 1.9.11 — 2026-07-04

Lessons fed back from a full re-tune-up of `spotify-playlist-refresh` (affinity-scored 20-track daily playlist; three `no_agent` crons). 3 independent blind reviewers (control-flow · data-shape · integration/docs/security lenses) + an adversarial refuter during the hunt, then a fresh terminal-gate fan-out over the cumulative diff — every finding reproduced on live data / a cloned DB under the gateway venv. Shipped 9 code fixes (2 HIGH-latent: None-name compose crash, curl Retry-After silent-nop) + a token relocation; SKILL.md hot-path 6430 → 5580 tok.

Two process wins worth restating: (1) the **terminal gate caught a regression in the author's own fix** — the new fallback per-artist cap collapsed all empty-`artist_ids` tracks into one `""` bucket → undershoot; (2) the **adversarial refuter killed both a plausible bug *and* its proposed fix** (a "NULL ms_played stranding" that can't occur on the real poll cadence, whose fix would have *inflated* affinity) — a fix worse than the (non-)bug.

New bug-classes added to `references/bug-hunt.md`:
- **Per-item cap keyed on an optional field buckets every keyless item together** → a per-distinct-entity limit silently caps the whole keyless class and undershoots (the empty-`artist_ids` → `""` bucket; caught by the gate on the author's own cap fix).
- **`.get(key, default)` does NOT protect against a present-but-`null` value** — `{"name": null}.get("name","")` returns `None`, not `""`; use `.get(k) or ""`. (The null-value mirror of *KeyError on a missing optional field*.)
- **A skill's own mandated regression suite/tooling hardcodes a relative path a later dir reshuffle invalidates** → the safety net crashes before running (FileNotFoundError), invisibly. Phase-0 "run the shipped suite" is what surfaces it — a suite that won't *start* is a finding.
- **An invalid curl `--write-out` selector degrades silently** — `%{header{x}}` (double-brace, invalid) vs `%header{x}` (correct); status still parses so nothing looks broken, but the header-driven behavior (Retry-After) never fires. Verify write-out selectors by a **probe on the box**, not a version table (one reviewer wrongly blamed a curl-version threshold; a 10-second probe disproved it).

Added to `references/token-efficiency.md`: the auditor's DUPLICATION / duplicate-numbered-list heuristic false-positives on intentional example repetition and on multiple distinct numbered lists — advisory, confirm by eye (a heuristic-approximates-a-parser case in the auditor itself).

## 1.9.10 — 2026-07-02

Lessons fed back from a full tune-up of `sg-commute` (routes/weather/carpark; two `no_agent` Discord crons — `morning_commute.py`, `evening_weather.py` — plus the interactive `nearest_carpark.py`). 3 independent blind reviewers (control-flow · data-shape · security/delivery lenses) + an adversarial refuter, then a fresh terminal-gate fan-out over the cumulative diff — every finding reproduced on live/synthetic data under the gateway venv. The gate caught **two regressions in the author's own fixes** before shipping (the single most valuable thing it did).

- **`references/bug-hunt.md` — new bug-class: a "final safety" guard gated on an already-set flag/note never fires for the compound case it was added for.** `if not note and BAD(x): note = warn` skips `BAD(x)` whenever an earlier, mutually-exclusive branch already set `note` for a different reason. (sg-commute `morning_commute.py`: the Innovis final-safety `if not route_note and route_has_detour(legs)` never flagged a weaving *fallback* because the fallback branch had already set `route_note`; caught by the terminal gate on the author's own fix.) Fix: run the guard on the final chosen value unconditionally and *append*, deduped by content; and scope it to where it's meaningful. A control-flow cousin of *the dead defense below an earlier `return`*.
- **Auditor triage note: `skill_audit.py`'s `[HIGH/reliability] MEDIA: tag delivery` is a FALSE POSITIVE for a single-tag `no_agent` cron.** For a `no_agent` job with `deliver: discord:…`, printing ONE `MEDIA:/path` line to stdout IS the intended attach mechanism (the scheduler's `extract_media` strips the tag and attaches the file); the reorder problem only bites INTERACTIVE delivery or MULTIPLE `MEDIA:` tags. Confirm the job type before "fixing" it — don't rewrite a no_agent stdout `MEDIA:` to curl-multipart. (sg-commute `morning_commute.py:412` is correct as-is; `nearest_carpark.py`'s MEDIA prints ARE a discouraged interactive fallback — correctly flagged.)
- Reaffirmed (no new rule): the recurring **sibling-divergence** class — two scripts reimplementing the same helper inline drift again (sg-commute's `wx_emoji` vs `evening_weather.emoji_for` had re-diverged on drizzle/lightning/storm; re-diff both after any weather-helper edit); and **doc↔code drift in a doc a *previous* session forgot to sync** — `CLAUDE.md` still carried the old single-`via:` waypoint, `> 30 km` gate, and a wrong `07:28` cron time after the ECP-restore session updated only SKILL.md/references. Grep **every** doc (incl. `CLAUDE.md`), not just SKILL.md.

## 1.9.9 — 2026-06-16

Lessons fed back from a full tune-up of `warframe-scraper` (a ScraperAPI-backed WARFRAME-Wiki Coda-Weapons scraper + a `no_agent` + `deliver: local` email cron that self-gates on a 4-day parity and had **never run** — first report day 2026-06-21, so every fix landed pre-first-send). Driven by 3 independent blind reviewers (control-flow/cron-contract · parser/data-shape · security/doc-drift lenses) + an adversarial refuter, then a clean terminal gate. All fixes validated against a captured page fixture; the live ScraperAPI was hit once for the baseline and the parser proven byte-identical to it afterward.

- **`references/bug-hunt.md` — new bug-class: a `no_agent` script that spawns a child via bare `"python"`/`"python3"` works only by PATH accident.** The cron launches the script under the gateway **venv** (first on `PATH`), so a child spawned as `["python", helper]` resolves to the venv *today* — but is one PATH change away from the **system** interpreter that lacks the gateway deps (the way a sibling cron died on `ModuleNotFoundError: pymupdf`), and the failure is invisible under `deliver: local` + exit-0-on-error. Fix: spawn every child (and sub-tools like `google_api.py`) with `sys.executable`. (warframe-scraper `warframe_email_report.py`.)
- **`references/bug-hunt.md` — sharpening of the `deliver: local` failure contract:** for a `deliver: local` job, exit-non-zero pushes **no** alert (unlike `deliver: discord:`/`origin`, which the scheduler maps to a pushed "⚠️ Cron job failed") — it only sets `last_status=error`/`last_error` (passive, visible in `hermes cron list`). So the **active** failure signal must be an in-script self-post. And a parity/cadence-gated script needs a **split exit contract**: a pre-gate benign skip exits 0 silently; a post-gate real failure exits non-zero **and** self-posts. "Always exit 0" (the tempting/documented choice) makes a broken report indistinguishable from an off-day. Verified against `cron/scheduler.py` (`_resolve_delivery_targets` returns `[]` for local). The mirror of sg-car-plate-range's *exit-1-for-benign-empty* (a `deliver: discord:` job crying wolf).
- **`references/bug-hunt.md` — new bug-class + method note: a bare-`<tr>` row regex when the SAME page already emits attributed `<tr>` elsewhere.** `<tr>(.*?)</tr>` extracts zero rows (→ "no data" raise → silent break) the moment the target rows gain an attribute. The risk isn't speculative when the dangerous variant already exists in the document — the wiki's sibling rotation table rendered `<tr class="tableRow-active">` on the same page. Method: **grep the live fixture for the attributed form before downgrading the risk to theoretical**; tolerate attributes (`<tr\b[^>]*>`). A concrete-risk instance of *heuristic-that-approximates-a-real-parser*. (warframe-scraper `_ROW_PAT`.)
- Reaffirmed (no new rule): the Phase-0 auditor false-clean for skills whose live code is in `~/.hermes/scripts/` (skill dir had no `scripts/`); the captured-raw-HTML baseline let the whole parser-fix set be proven offline byte-identical without burning ScraperAPI credits (bug-hunt.md *single-fact baseline* method); and the `--scripts <copydir>` gotcha — pointing the auditor at an isolated copy of just one skill's scripts means you must **refresh that copy after editing**, or it re-flags already-fixed anti-patterns (the warframe email-report's `TimeoutExpired` flag cleared only after refreshing the copy).

## 1.9.8 — 2026-06-13

Lessons fed back from a full tune-up of `vault-audit` (a deterministic Obsidian-vault accuracy auditor whose weekly `no_agent` + `deliver: local` cron runs `--fix` unattended and self-posts to Discord). Driven by 3 independent blind reviewers (auto-fix/state · parser/edge-case · delivery/cron/safety lenses) + an adversarial terminal gate over the cumulative diff (which caught 3 regressions the fixes themselves introduced — a prune symlink-escape, a cron decoy-token pick, and a `_split` off-by-one). Every fix was validated against synthetic `HERMES_HOME` vaults; the real vault was never mutated.

- **`references/bug-hunt.md` — new bug-class: an auto-fix anchored by a first-match / non-line-anchored regex edits the WRONG occurrence.** A find-and-replace whose anchor isn't bound to the definition line takes the first match (a decoy/example/sibling cell), silently rewriting it while the real stale value survives — and the exactly-once guard doesn't help because the chosen span is itself unique. Fix: anchor to the line (`(?m)^[\t *-]*` + label) capturing the prefix in the replacement group; when a region holds several candidates, act only on exactly one. (vault-audit `check_models` + the cron-expr fix.)
- **`references/delivery-reliability.md` — sharpening of the `deliver: local` self-notify rule:** a self-posting script that runs `--quiet` loses its whole report if the post fails (nothing posted, nothing saved). The post must return success/failure; on failure, print the report to stdout even under `--quiet` (so the saved job-output isn't empty) and exit non-zero. (vault-audit `post_discord` was fire-and-forget.)
- Reaffirmed (no new rule): the improved `skill_audit.py` `--scripts` flag correctly surfaced the live engine in `~/.hermes/scripts/` (the false-clean the 1.9.7 self-tune-up fixed); validating an auto-editing engine via a synthetic `HERMES_HOME` is the clean isolation method for "destructive to trigger live" logic (bug-hunt.md Method step 4).

## 1.9.7 — 2026-06-13

**Self-tune-up** — ran this skill's own method against itself (Phase 0 baseline → 3 independent blind reviewers: skill_audit.py code lens / doc-consistency lens / adversarial self-application lens → targeted probes → terminal gate → docs-last). Found that the no-LLM auditor `scripts/skill_audit.py` had bugs in its own headline analyses (caught only by *running it on live skills*, not reading it — exactly what the bug hunt preaches), plus the skill was violating several of its own cleanup/docs rules.

`scripts/skill_audit.py` fixes (code pass, then a clean terminal gate that itself caught & fixed a `--scripts` arg-parsing regression):
- **H2 section split is now code-fence-aware.** A bare `^## ` regex counted example `##` headings *inside* ```` ``` ````/`~~~` fences as real sections and truncated the section containing the fence (reproduced: `creative/design-md` reported phantom `Overview`/`Colors`/`Typography`/`Components`). Now tracks fence state line-by-line.
- **Anti-pattern docstring skipping now uses the Python `tokenize` module** (with fallback to the old toggle). The per-line triple-quote *parity toggle* desynced on a single line mixing `"""` and `'''` → it skipped the next line as "documentation" and **hid a real HIGH anti-pattern** (false-clean on the very smells the tool exists to catch). Reproduced and fixed.
- **Frontmatter `description: >`/`|` block scalars** now read correctly (were reported as length 1, breaking the no-description / too-long checks — e.g. `apple/macos-computer-use`).
- **Link integrity now checks markdown `[text](references/…)` links**, not only backticked paths (skills like `mlops/inference/vllm` link refs only via markdown → were unchecked).
- **The false-clean is now surfaced at runtime:** the auditor warns whenever 0 scripts were scanned and accepts `--scripts <dir>` to point the scan at live code in `~/.hermes/scripts/` (verified: 26 scripts / 27 anti-patterns). Previously the caveat lived only in SKILL.md prose.
- **Subprocess `timeout=`/`TimeoutExpired` heuristics** count over code-only lines (a `timeout=` in a comment/docstring no longer credits a missing one).

New bug-class added to `references/bug-hunt.md`:
- **A heuristic that approximates a real parser misfires on the constructs it doesn't model** — and because it still emits plausible output, the error hides. (All four auditor bugs above were instances; each surfaced only by running the tool on live inputs containing the awkward construct.) Fix: use the real parser for the domain (fence-state scan for markdown, `tokenize` for Python string/comment state, block-scalar-aware frontmatter), and validate the auditor itself against inputs that contain the awkward constructs.

Self-discipline / hygiene fixes (docs pass, after code freeze):
- **Deleted 6 stale `*.lessons-1.9.3-*.bak` files** the skill's own auditor had been flagging under `[STRAY FILES]` for 3 versions — safe-edit §6 ("delete .bak after a confirmed run; scan for strays") self-violated.
- **`SKILL.md` intro re-collapsed to a one-line pointer.** The "distilled-from" provenance paragraph had silently re-bloated back to ~556 tok (the largest section), reversing the 1.9.0 relocation and tripping the skill's own "Don't undo your own work" rule — the dated per-skill war stories live in this CHANGELOG.
- **Fixed 3 stale `references/changelog.md` cross-references** in this file (1.9.2–1.9.4 entries said "full provenance in `references/changelog.md`," now a stub since the 2026-06-09 consolidation) → point to the *Per-skill tune-up provenance* section below. The "grep all references to the old path" rule (token-efficiency.md), self-violated.
- **`SKILL.md` Phase-0 auditor description synced** to the new behavior (fence-aware budget, markdown-link integrity, block-scalar descriptions, duplication + stray-files sections, the runtime false-clean warning + `--scripts`).
- **`references/safe-edit-workflow.md`** stale "(see Pass 1 Step 0)" → "(see SKILL.md *Phase 0* / bug-hunt.md Step 0)" (1.9.0 hoisted find-the-live-copy to Phase 0).

Method confirmations (no new rule): the terminal-gate fan-out caught a regression the author's *own* `--scripts` fix introduced (reinforces "review your own applied edits"); and the fenced-heading bug only surfaced by running the auditor on a live skill, not reading it (reinforces "validate against live data, never code-reading alone").

## 1.9.6 — 2026-06-12

Lessons fed back from a full tune-up of `sg-commute` (SG commute planner: two `no_agent` Discord crons — `morning_commute.py`, `evening_weather.py` — plus the interactive `nearest_carpark.py`). The pass was driven by 3 independent blind reviewers (control-flow / data-shape / security lenses) + an adversarial refuter, then a clean terminal gate.

New bug-classes added to `references/bug-hunt.md`:
- **`curl -o` saves an HTTP-error body with exit 0, and the outbound cron media filter has no magic-byte check** → an ~88-byte text error body ships as a broken `MEDIA:` PNG. Fix: `curl -f` + validate the `\x89PNG` signature before emitting the tag. (Reproduced against the live gateway `extract_media`→`filter_media_delivery_paths`→`validate_media_delivery_path` chain — `_looks_like_image` is wired only into *inbound* caching.)
- **Sibling scripts that reimplement shared logic drift** — `morning_commute.py` reimplemented emoji/forecast-text inline and carried bugs that `evening_weather.py`'s hardened `fc_text()`/heavy-rain branch had already fixed (a string forecast crashed the whole Weather section; "Heavy Rain" rendered as the light glyph). Found by importing *both* modules and feeding the same edge cases.

New delivery lessons added to `references/delivery-reliability.md`:
- **Confirm a send-path change without posting**, via probes the API rejects at validation: empty content → `50006` (proves the field was read); non-empty content + invalid `message_reference` → `50035` (proves it passed validation). Settled "is `payload_json` honored for a text-only multipart message?" with live evidence, no real send.
- **`MEDIA:` on the `no_agent`-deliver path is valid** (single trailing image; the scheduler extracts+attaches it) — distinct from the multi-image agent-reply path the "never use MEDIA:" rule targets. Caveats: the wake-gate parses the last stdout line as JSON; the outbound filter does no magic-byte check.
- **Hardened multipart: bundle `content` + `allowed_mentions:{"parse":[]}` in one `payload_json` field** (still complements, not replaces, the untrusted-field sanitizer).

Also reaffirmed (no new rule needed): the Phase-0 auditor false-clean for interactive skills whose cron scripts live in `~/.hermes/scripts/` (the auditor scanned only the skill-dir `scripts/`, flagging the two live cron scripts as MISSING links — they were reviewed by hand); and the doc↔code drift sweep (stale traffic thresholds + a stale EV-scan algorithm in `CLAUDE.md` that the authoritative reference had already corrected — only the duplicate copy had rotted).

## 1.9.5 — 2026-06-11

Lessons fed back from a full tune-up of `sg-car-plate-range` (a curator-rewritten LTA car-plate scraper, no_agent weekly cron → Discord). Notably the live page's data **changed mid-tune-up** (LTA opened prefixes for every vehicle type overnight, then the API quota was exhausted), which stress-tested baseline/validation discipline.

- **`references/bug-hunt.md` — new bug-class: a negative-signal substring veto over a span that also carries the positive signal.** `if "error-text" in section …: continue` silently drops a good item when the marker co-occurs — worsened by a section regex with **no end-anchor** (the last `(?=…|$)` match absorbs the page tail to EOF). Fix: make button/value *presence* authoritative; consult the negative marker only when the positive signal is absent.
- **`references/bug-hunt.md` — new bug-class: single-category scope creep.** A skill named for one slice (Car) iterated all 8 LTA vehicle types and pooled them under the "Car Plate" banner — correct only by luck until a sibling type had data (live data caught a motorcycle/bus/trailer prefix about to be reported as a car plate). Resolve the wanted category from the page's own labels and filter; raise rather than guess.
- **`references/bug-hunt.md` — new bug-class: HTTP-200-but-wrong-page == "no results".** Only guarding `status_code != 200` lets a block/empty/JS-unrendered page read as a legitimate empty result. Add a structure-sanity check that raises (alert) — and make the sanity regex and the extraction regex share **one** tolerant pattern, or a markup tweak passes the check yet splits to zero (a gate-caught silent false-empty).
- **`references/bug-hunt.md` + `references/delivery-reliability.md` — the no_agent exit-code/stdout delivery contract.** exit 0 + stdout → delivered; exit 0 + empty → silent; non-zero → "⚠️ Cron job failed" alert. `print(...); sys.exit(1)` on a benign empty week cries wolf; a breakage that exits 0 delivers silence/wrong data. The scheduler already delivers for a `deliver: discord:` job — **don't add a self-`curl`**; just get the exit code right.
- **`references/bug-hunt.md` — method: when the reviewer fan-out is budget-killed (429/quota), substitute deterministic self-probes per lens** (and say which lenses were agent-run vs self-probed) rather than concluding on the survivors. Here the security + adversarial-refuter agents died on a daily cost cap; their lenses were done as a `grep -rn` exposure scan and a synthetic-HTML unit test.
- **`references/bug-hunt.md` — method: a single-fact green baseline is ambiguous when upstream data changes between runs.** A parser change and a page change look identical in the output. Isolate by fetching **once** and running old vs new parser over the *same* body + dumping raw matches; capture the baseline as the raw payload, and budget live external reads (one baseline + one confirm) — a costed/quota'd API (Scrapeninja) can run dry mid-tune-up.
- Also reaffirmed (Phase 0): the **auditor anti-pattern scan is a false-clean for skills whose code lives in `~/.hermes/scripts/`** (no skill-dir `scripts/`) — review those by hand; and the **background curator** can rewrite a skill (here repointing the cron from a dead `.sh` to the new `.py` and editing the script twice in one evening) — re-pull + diff before trusting any copy.

## 1.9.4 — 2026-06-09

Lessons fed back from a token-efficiency pass on `email-gmail-triage`. Full per-skill provenance in the *Per-skill tune-up provenance* section below.

- **`references/token-efficiency.md` — bug history tables are the #1 sink and section-level audits undercount them.** The auditor reported "Bugs Found & Fixed" as 4,162 + 1,359 tok across two H2 sections (45% of SKILL.md), but the actual relocation saved 85% (12,309 → 1,854 tok) — because the bug rows are dense multi-clause prose that tokenizes heavier than word-count projects. Always move bug tables to `CHANGELOG.md` first; it's almost always the single biggest win.
- **`references/token-efficiency.md` — CLAUDE.md file maps go stale after relocations.** When files move from SKILL.md to `references/` or `CHANGELOG.md`, the CLAUDE.md file map isn't auto-updated. Stale maps confuse editing agents. Added to doc-sync sweep.
- **`SKILL.md` Phase 0 — auditor path assumption.** The auditor assumes `~/.hermes/skills/<category>/<skill-name>/` but some skills live flat at `~/.hermes/skills/<skill-name>/`. A "not a skill dir" error means the path is wrong — `search_files` first.
- **`CHANGELOG.md` consolidation — don't duplicate changelogs across `CHANGELOG.md` and `references/changelog.md`.** The 1.9.4 entry was written to `references/changelog.md` but missed the root `CHANGELOG.md` — exactly the kind of drift that duplicate docs create. Merged into one file; `references/changelog.md` is now a pointer.

## 1.9.3 — 2026-06-08

Lessons from a multi-pass `traffic-camera` session (evaluate 1.2.0 → remediate 1.2.1 → table feature 1.3.0 → interactive-misroute fix 1.4.0). Full per-skill provenance in the *Per-skill tune-up provenance* section below.

- **`references/delivery-reliability.md` — Discord delivery internals.** (1) A no-target `send_message` routes to the **home channel** (`DISCORD_HOME_CHANNEL`, here #alerts) — the root cause of "the agent keeps posting to #alerts" on interactive replies (crons pass targets explicitly, so they're unaffected). (2) `MEDIA:` paths are gated by `validate_media_delivery_path`: default mode delivers any *real* file but **silently drops a path that doesn't resolve to a file** (`Skipping unsafe MEDIA directive path`) — the real failure mode is an unsubstituted template path (`/tmp/route_<dest>.png`); a script writing a real PNG (discord-table-format) works. (3) Discord is behind Cloudflare, which **1010-blocks the bare `Python-urllib` UA** — probe/deliver with curl, or set a `User-Agent`. (4) New pattern: when the agent keeps misrouting and a soft instruction won't hold, **take the LLM out of the delivery path** — a self-delivering script that resolves the current thread from `HERMES_SESSION_KEY` (post to `parts[4]` chat_id; **never** `parts[5]` = user_id for `group` chats) and **fails closed** (never a fixed/home channel).
- **`references/bug-hunt.md` — new bug-class: positional parse of a structured key whose trailing element varies by record type** (session key `parts[5]` = thread_id for threads but user_id for `group`). Caught by the terminal-gate fan-out, not the author.
- **`references/safe-edit-workflow.md` — a background skill-curator edits skills *between* passes** (the "update the skill library" post-session agent; no version bump, can misdiagnose) → re-pull + `diff` before *every* push, reconcile its undocumented edits, don't trust its diagnoses.
- **`SKILL.md` Phase 0 — auditor false-clean:** `skill_audit.py`'s anti-pattern scan only reads the skill-dir `scripts/`; a skill whose code lives in `~/.hermes/scripts/` gets `[ANTI-PATTERNS] none` over **zero** files. Review live scripts manually. Also: for an interactive (non-cron) skill the live copy is whatever SKILL.md tells the agent to run.

## 1.9.2 — 2026-06-08

Lessons fed back from auditing the `traffic-camera` 1.2.0 tune-up against this method, then remediating it (→ traffic-camera 1.2.1). Full per-skill provenance in the *Per-skill tune-up provenance* section below.

- **`references/bug-hunt.md` — new bug-class: asymmetric guarding of a paired field, and a filter-site guard that doesn't cover the format site.** Guards that test `latitude is not None` but omit `longitude` (or guard the `filter_*` functions but not a later `f"{c['latitude']:.6f}"` format/label site), plus the truthy-`0.0` trap (`if c["latitude"]` drops a legitimate `0.0`). Grep every paired-key dereference and confirm each site guards the same way, with `is not None` for numeric fields that can be `0`.
- **Method reinforcement — docs-last and the terminal gate are what catch relocated-doc staleness and non-executable workflows.** The 1.2.0 pass wrote its `references/` *before* the code froze and skipped the cumulative-diff reviewer gate, shipping a stale function name (`assign_center_cameras`) and a Workflow 2 pointing at a nonexistent script mode. No new rule needed — a live confirmation of the 1.9.0 *docs-last* + *terminal-gate* sequencing; the remediation followed them (code frozen via an adversarial gate over the cumulative diff, then docs).
- **Writeback hygiene:** the 1.2.0 pass recorded provenance only in `references/changelog.md` (misfiled under the `## 1.9.0` heading) and never updated `bug-hunt.md`, the intro list, or this file — so the bug-class wouldn't have reached the next tune-up's checklist. Corrected here; the provenance block is re-filed under a dedicated *Per-skill tune-up provenance* section.

## 1.9.1 — 2026-06-07

Lessons from a `food-recommender` tune-up (12 bug fixes, SKILL.md 3,073→2,838 tok, Pitfalls 957→539 tok):

- **`references/bug-hunt.md` — new bug-class: a sentinel/default value receiving a systematic scoring bonus.** A diversity scoring system that gives +10 for being "different" from the user's history can give the highest bonus to a catch-all "General" category that is never in the user's top-N — because it's a label for unclassifiable items, not a real cuisine. Fix: exempt the sentinel from variety/affinity/recency bonuses.
- **`references/bug-hunt.md` — new bug-class: a display name used as a unique dedup key when names aren't unique.** Chain restaurants ("McDonald's", "Starbucks") have multiple physical locations; dedup by name silently drops all but the first. Fix: deduplicate by `(name, lat, lon)` tuple.
- **Refuter value: 3 of 5 initial findings disproved** — the refuter showed the cache "stale re-activation" claim was false (staleness check on lines 44-51 already handles it), the non-atomic write was sub-millisecond with auto-recovery, and the KNOWN_CUISINES duplicates were same-value (no actual wrong mappings). The refuter's highest-value move was overturning the solo reviewer's top finding, exactly as the method predicts.

## 1.9.0 — 2026-06-07

**First self-tune-up: re-sequenced the method itself.** Triggered by a design review of *this* skill's own activity ordering, and audited with the skill's own mandatory independent-reviewer fan-out — 4 blind reviewers (control-flow/reachability, documentation-drift, an adversarial refuter, and a practitioner-simulation lens) over the skill's own `SKILL.md` + references. The fan-out corroborated the core critique, corrected two overstatements, and surfaced a sharper finding than the solo critique — exactly the *review-your-own-work* value the skill preaches, now applied to the skill itself. The structural problem: preconditions and the heavy validation were **front-loaded into Pass 1**, while 3 of the 4 passes edit code *after* it, and the doc/token pass ran *before* the delivery rewrite — so a single-pass engagement (perf-only / token-only / delivery-only) could skip the live-copy check and the fan-out entirely, and a relocated doc could go stale when a later pass changed the code it described.

- **`SKILL.md` — re-sequenced into explicit phases.** Added *How a tune-up runs* (the canonical order), a universal **Phase 0 — Preconditions** (find-live-copy + run the auditor on the live copy + **capture a green baseline**) that runs for *every* engagement, then sequenced the work as **code passes → a re-entrant Terminal validation gate (fan-out + baseline diff over the *cumulative* code diff; a regression it finds re-runs the gate to a fixpoint) → the token-efficiency *docs* pass LAST, only after the gate is clean** — so a relocated/slimmed doc can't describe code a later gate-found fix changed (the docs pass deliberately runs *after* the gate, not before). Folded the old *Quick Start* into Phase 0. Two new Golden Rules: *preconditions are universal; validation is terminal* and *freeze code before docs (the gate decides when it's frozen)*.
- **New method finding — find-live-copy is a precondition for *all* passes, not just the bug hunt** (`bug-hunt.md` Step 0 + `SKILL.md` Phase 0). A perf/token/delivery-only engagement that never enters Pass 1 could profile/slim/rewrite the divergent `~/.hermes/scripts/` copy — the same "half the bugs were in dead code" waste, reachable from three more entry points. Hoisted to Phase 0.
- **New method finding — the independent-reviewer fan-out is a *terminal* gate over the cumulative diff, not a Pass-1-internal step** (`bug-hunt.md` fan-out section + `SKILL.md` Terminal validation gate + `safe-edit-workflow.md` §6). It now explicitly runs twice: to *find* bugs during the hunt, and to re-review *your own* shipped diff before done — mandatory for every engagement that changed code. (The refuter correctly noted `safe-edit` §6 already composed a terminal gate; the surviving gap was that its adversarial teeth were textually scoped to "every bug hunt" — now widened.)
- **New bug-class — a relocated doc goes stale when a later code pass changes what it describes** (`token-efficiency.md` *Relocate* section + `safe-edit-workflow.md` §5). Pass 3 moving a delivery/flow description into `references/` *before* Pass 4 rewrites that code strands a stale copy in a load-on-demand file; the per-change "grep for old values" misses it because the value was just cut from `SKILL.md`. Fixes: (1) run the docs/relocation pass **after** code freeze (docs-last); (2) widen the doc-sync grep to the recursive whole-dir `grep -rn '<old-name>' <skill_dir>` the secret-scrub rule already uses, and re-read relocated *prose* semantically.
- **New finding — the CLI→`curl` change is owned by the Delivery pass, not the perf pass** (`performance.md` Lever 1 + `delivery-reliability.md` intro). It's both a perf lever and the delivery rewrite; doing it in the perf pass ships a bare `curl` without the content-limit / sanitization / state-ordering guardrails. Now single-owned in Delivery.
- **New precondition — capture a green baseline before touching anything** (`SKILL.md` Phase 0). The terminal gate needs something to diff against; for the majority of skills with no committed test suite, hash an output fingerprint of the live dry-run.
- **Docs-last token pass on this skill's own `SKILL.md`** (run *after* the re-sequencing settled, per the new docs-last rule). Relocated the ~1,170-token "distilled-from" provenance paragraph — duplicated per-version in this CHANGELOG — to a one-line pointer, trimming the hot-path body ~4,140 → ~3,100 tokens with no capability loss (every named tune-up still has its full war story in the entries below).

This entry documents a change to the *method*, not to a target skill — the lessons fed back here came from reviewing the skill's own design.

## 1.8.0 — 2026-06-07

First tune-up of a **non-skill `no_agent` cron script** (the weekly Hermes Backup, job `999de4b21eec`), triggered by a live `Script timed out after 120s` failure. New bug-classes, a performance lesson, and a hard-won reviewer-safety rule:

- **`references/bug-hunt.md` — new bug-class: a `no_agent` script whose real work exceeds the scheduler's *global* timeout watchdog.** The scheduler runs each script under `subprocess.run(timeout=_get_script_timeout())` and SIGKILLs on expiry — one global cap (module-patch → env `HERMES_CRON_SCRIPT_TIMEOUT` → config `cron.script_timeout_seconds` → 120 s default), **no per-job timeout**. Work that grows past it (zip a 444 MB DB + upload ~360 MB) dies every run, and a SIGKILL can't self-notify. Fix: raise the global knob with headroom (measure first), bound internal steps below it; note the all-jobs blast radius.
- **`references/bug-hunt.md` — new bug-class: doc prescribed the fix but the config was never applied.** Both `backup-debugging.md` and `SKILL.md` said "set the timeout to 600 s," yet `config.yaml` still had the 120 s default. A doc-stated setting is a claim to verify against the live config, not trust.
- **`references/bug-hunt.md` — new bug-class: a killed parent leaves an orphaned grandchild.** `subprocess.run` kills only the direct child, not the process group; a spawned `hermes backup` finished writing its zip ~3 s *after* the job was marked `error` (a valid-but-unprocessed "ghost" artifact whose mtime post-dates `last_run_at`).
- **`references/bug-hunt.md` + `references/delivery-reliability.md` — new bug-class: `deliver: local` makes a `no_agent` failure silent.** The scheduler pushes nothing for local-deliver jobs, so a script whose only alert is its last step pings nobody on a timeout/early-exit. Route every hard-failure through a `fail()` that notifies *then* exits non-zero.
- **`references/bug-hunt.md` — new bug-class: a full off-box backup ships live credentials in the clear** (`google_token.json`, `.env`, `auth.json`, `profiles/*/.env`). A policy call to surface, not silently change; if encrypting, gpg symmetric AES256 is cron-safe (`--batch --pinentry-mode loopback --passphrase-file`) but the passphrase must live OFF the Pi.
- **`references/performance.md` — new section: when the bottleneck is an irreducible transfer, the lever is the timeout, not the code.** Plus the method refinement **measure throughput against the REAL endpoint, not a generic speed test** — a reviewer's 495 KB/s (generic) vs the actual Drive resumable path's 3.3 MB/s (7×) flipped the 600 s decision; a targeted 50 MB throwaway-upload probe through the same client settled it.

## 1.7.1 — 2026-06-07

Follow-on method/technique lessons from the same `spotify-playlist-refresh` session (1.7.0 captured the *process* gaps; these are *how-to* refinements):

- **`references/bug-hunt.md` — Method step 4: validate hard/destructive-to-trigger logic deterministically in isolation.** "Ran the dry mode, looked fine" doesn't prove a conflict/upsert policy, a retry/backoff state machine, or a rarely-firing correction. Clone the DB to a temp file and drive the conflict rows one at a time; or stub the I/O layer and **assert the recorded side-effects** (the backoff sleeps, Retry-After honored, 404 not retried). Both the self-correcting upsert and the 429/5xx retry machine were proven this way before shipping.
- **`references/bug-hunt.md` — new bug-class: "code contradicts its docs/config, but the *code* may be the deliberate, correct side."** The mirror of *implementation-contradicts-its-own-SKILL.md*: don't reflexively "fix" code to match a doc — the divergence can be an intentional, unrecorded tune (spotify: hardcoded 7/7 scoring was right; the 30/14 in config + SKILL.md was stale). Establish which side is canonical first; behavioral/ranking/scoring params are usually the user's call.
- **`references/bug-hunt.md` — report format: flag forward-only fixes and stored-signal changes.** A forward-only fix can show *no* live change on a quiet system — report that honestly as a no-op (and prove the mechanism deterministically) rather than implying a correction; note that historical metrics aren't comparable across a signal-changing fix. (The inverse of performance.md's behavior-preserving fingerprint.)
- **`references/safe-edit-workflow.md` §6 — sequencing: "done" comes last.** The fan-out on your *own* edits + the doc-sync gate completion — run them *before* deleting backups or declaring done. (This session shipped fixes and deleted backups first, then reviewed; the user had to prompt the review, which then found a shipped regression.)

## 1.7.0 — 2026-06-07

Process-hardening from a `spotify-playlist-refresh` tune-up (a SQLite affinity→playlist skill with 3 `no_agent` crons). Three method changes, plus the new bug-classes that motivated them:

- **Independent-reviewer fan-out is now MANDATORY, not "for a thorough pass."** Pass 1 in `SKILL.md` and the protocol header in `references/bug-hunt.md` now state every bug hunt must fan out independent blind reviewers (diverse lenses + an adversarial refuter) and **never audit solo** — added as a Golden Rule too. The motivating event: on this tune-up I skipped the fan-out, the user flagged it, and running it afterward **caught a regression my own fix introduced** (an atomic `auth.json` write that fell back to `{}` and would have wiped other providers' credentials). New `bug-hunt.md` bullet: **run the fan-out on your own applied fixes, not just the original code** — your edits are the least-reviewed code in the repo.
- **New "Always finish a tune-up with (required)" section in `SKILL.md`** codifying two mandatory closers: (1) **sync the target skill's docs to the shipped code** — re-sweep `SKILL.md`/`CLAUDE.md`/`references/` for doc↔code drift your fixes introduced and fix every stale claim (also reinforced in `safe-edit-workflow.md` §5 as the per-change half); (2) **feed every new bug-class/lesson back into THIS skill** before declaring done. Both added as Golden Rules.
- **New bug-classes in `references/bug-hunt.md`** (from this tune-up): *a latent crash the skill's own docs arm* (a blocklist `TypeError` that only fires once the user follows the "manually blocklist" instructions); *a config block that's dead because the code hardcodes its values* (config drifted from hardcoded constants, never loaded); *a "self-correction"/backfill that's a no-op or hits the wrong target* (recomputed an already-correct value; `INSERT OR IGNORE` blocked the real fix); and *the empty/zero path returning a different shape than the happy path* (`{}` vs `({}, ts)` → caller unpack `ValueError`).

## 1.6.0 — 2026-06-06

Incorporated lessons from an `email-gmail-triage` tune-up — a `no_agent` Gmail→Discord inbox-triage skill (the live `gmail_triage.py` / `gmail_report.py` run under `~/.hermes/scripts/`; the skill dir holds **docs + 4 committed regression suites**, no runtime `scripts/` copy). New failure surface vs the prior skills: **a guard that's dead because an earlier `return` preempts it, a reminder scoped to the batch instead of the backlog, a non-atomic state write that bricks every future run, a destructive action gated on a swallowed failure — plus the first skill whose runtime token cost was *already ~0* (so the pass became a `no_agent`-mode verification + regression-hardening), and the first whose inputs are attacker-controllable (a Discord content-injection surface).** Audited with a 3-way blind reviewer fan-out + my own pass; two confident reviewer claims (display-name VIP spoofing, resolved-Unsure going invisible) were killed by a literal probe of the parser / a code-path read.

- **Bug hunt** (`references/bug-hunt.md`): four new/refined bug-classes — **a defense that exists but is dead below an earlier unconditional `return` in an ordered short-circuit pipeline** (the "humans are never auto-filed" rescue sat at the bottom while a promo/receipt *subject-keyword* branch returned `Auto-filed` first — the guard reads as present on a skim but is unreachable; trace every early `return` and ask whether a later guard needed to run; the SKILL.md even forbade it); **a periodic "remind me about outstanding X" job built from the current run's items, not the live backlog** (the 9pm reminder only saw mail triaged in the 21:00 run, so it nagged about ~nothing); **non-atomic state write + unguarded read = one crash bricks every future run** (`open(STATE_FILE,"w")` truncates before the advisory `flock`; a power-loss/OOM mid-`json.dump` leaves corrupt state and every later run aborts at `read_state` forever → fix with temp+`fsync`+`os.replace` *and* a guarded read that self-heals to empty); and **a destructive follow-on action that runs even when the prior step silently failed — and removes the item from its own retry path** (`apply_labels` swallowed its error + warned, then `main` unconditionally `archive`+`mark_read`'d the Auto-filed email → filed out of the inbox with no label, invisible, and no longer `in:inbox` so never re-discovered to retry). Plus a fan-out refinement: **set a fix-threshold up front** (here ≥ Med severity *and* ≥2 reviewers) and **disprove a confident security/spoofing claim with a literal probe of the actual parser**, not a vote.
- **Token efficiency** (`references/token-efficiency.md`): new **"scheduled skill: the execution mode IS the token lever"** section — an agent-mode cron loads the whole `SKILL.md` (+`CLAUDE.md`+tool schemas) into context *every tick*; converting it to a `no_agent` script the scheduler runs directly (with `deliver: local`) drops that to ~0. **Measure it for free via cron-output size** (this skill's ticks went 36 KB agent-mode → 567 B script-log at the migration — that ratio *is* the per-run token delta). When a skill is **already `no_agent`**, the honest finding is "runtime ≈ 0; the migration already won" and the real work is **hardening against regression** (stale `skill`/`skills`/`prompt` fields on the job mean an accidental `no_agent` flip reloads the whole skill — clear them / monitor the flag). Also: **a `no_agent` script that defers to a human conversation has an indirect per-deferral token cost** (each "Unsure → ask in Discord" wakes the interactive agent when the human replies).
- **Delivery reliability** (`references/delivery-reliability.md`): new **"Untrusted content reaching Discord"** section — email subjects/senders are attacker-controlled; **sanitize *every* interpolated field, not just the obvious one** (the digest sanitized `subject` but interpolated the `sender` raw — the tell is one field cleaned, a sibling not), defang `@`mentions + send `allowed_mentions:{parse:[]}`, strip newlines/backticks, **and break Discord masked links `[label](url)` and bare `://` autolinks** (a crafted subject can otherwise inject a clickable phishing link into the trusted channel; `allowed_mentions` blocks the ping, not the link).
- **Safe edit** (`references/safe-edit-workflow.md`): **if the target skill ships its own regression suites, they are your validation harness** — run all of them against the *deployed* copy as the final gate and add a case per fix (suites that hardcode the live script path can't validate an edit in isolation — swap the path or stage); and **after editing, `diff` live-vs-edited and confirm the diff is exactly your intended hunks** (catches unseen drift *and* stray edits), complementing the pre-edit `shasum`.
- No **performance** pass — the scripts aren't slow; the only per-item cost (a `get_message_body` subprocess per email) is the already-documented process-spawn class, and a perf pass wasn't requested.

## 1.5.0 — 2026-06-06

Incorporated lessons from a `google-route-map` tune-up — a stdlib skill that calls **two Google HTTP APIs** (Routes v2 `computeRoutes` + Static Maps) to render a route PNG; no cron, no Discord, file output only. New failure surface vs the prior cron/DB/LLM skills: **doc-vs-code contract drift, response validation for binary output, and a token cost that lives in the agent↔script protocol rather than in `SKILL.md`.** Audited with a 3-way blind reviewer fan-out; one confident false positive (over-encoding) was killed by a `quote`→`unquote` round-trip.

- **Token efficiency** (`references/token-efficiency.md`): new **"keep large intermediates out of the orchestrator's context"** lever — a value produced in step 1 and consumed in step 2 round-trips through the agent's context (out via stdout, back via argv) when the flow is split across CLI calls; **collapse it into one end-to-end script** so the blob stays in-process (the encoded polyline, 300–3,000+ chars, billed twice/run → 0). Plus **don't echo large values to stdout**, and a no-`references/`-dir trim note (cut verbose code blocks, link to the script; SKILL.md 5.2K→2.8K / −45%).
- **Bug hunt** (`references/bug-hunt.md`): five new bug-classes — **implementation contradicts a hard requirement its own SKILL.md declares** (SKILL.md said "ALWAYS `format=png32`"; the script never set it); **HTTP 2xx ≠ a valid payload for binary/file output** (Static Maps returns 200 + an error *image* — validate Content-Type + PNG magic bytes, not "bytes written"); **a configured default that violates the API's input limit** (`size=700x450` > the 640px cap → the default always 400s); **a limit the code measures but never enforces** (printed `len(url)` vs the 8KB ceiling, no guard); **hardcoded secret in skill files** (a live key in the script *and* SKILL.md → env/`.env`, rotate, grep all files); plus **doc↔code drift / a doc example that breaks the doc's own rule**, and a fan-out refinement — **for an encoding dispute the probe is a literal round-trip** (full percent-encoding is correct/required, not a bug).
- **Performance** (`references/performance.md`): new **"single-shot case — sometimes the honest answer is *nothing to optimize*"** — a one-call I/O-bound utility is dominated by the lone round-trip; resist inventing micro-opts, the only levers are payload size (here pinned by a correctness requirement) and batching-only connection reuse; prove any "simplification" (e.g. `urlencode`) byte-identical and label it readability, not perf.
- **Delivery** (`references/delivery-reliability.md`): **image *format* can break rendering, not just transport** — PNG8 silently drops thin colored overlays in Discord's pipeline; emit PNG32.
- **Safe edit** (`references/safe-edit-workflow.md`): after removing a hardcoded secret, **grep every file** to confirm it's gone (easy to fix the script and miss the SKILL.md copy) and rotate it regardless.

## 1.4.0 — 2026-06-06

Incorporated lessons from a `foodpanda-expense-tracker` tune-up (a Gmail→Sheet→Discord receipt tracker; two `no_agent` cron scripts that self-post via direct REST). No performance pass — the scripts aren't slow; the new surface was **incremental-cursor correctness, period-boundary windows, and a phase-split token refactor**, audited with **diverse-lens reviewers resolved by live probe**.

- **Bug hunt** (`references/bug-hunt.md`): four new bug-classes — **a watermark advanced only over *successes*** re-fires a "report once" side-effect every run for any deterministically-skipped item newer than the last success (fix: advance over successes *and* permanent skips, bounded below the earliest *retryable* failure; and count+surface silent `except: continue` drops); **a window query that reads only the current partition** when a Mon–Sun week spans a month tab (read both, merge); **not-found conflated with hard error** → a benign new-month state posts an error alert (match the API's not-found signal, treat as empty — the inverse of rate-limit-mimics-empty); and **`[^X]*` skip-until breaks when X legitimately appears** (`Order total[^S]*S\$` died on "Service"/"Singapore" → use `.*?`). Plus a fan-out refinement: **diverse lenses + an adversarial refuter**, and **resolve a reviewer disagreement with a targeted live probe, not a vote** (a "verified" locale bug was disproved on the Pi because the code never calls `setlocale`; a missing-tab claim was confirmed the same way).
- **Token efficiency** (`references/token-efficiency.md`): **split reference content by phase** (`SETUP.md` / `TROUBLESHOOTING.md` / `CLAUDE.md`) so each loads only when needed; **track per-trigger vs edit-time budgets separately**; **de-dupe the `CLAUDE.md`↔`SKILL.md` overlap** so editing doesn't pay twice; verify the split is lossless with one coverage grep across the whole doc set. Took the target `SKILL.md` 3,940 → 640 tokens (−84%).

## 1.3.0 — 2026-06-06

Incorporated lessons from an `sg-commute` tune-up (a routes/weather/carpark skill with **split cron scripts** — `nearest_carpark.py` in the skill dir, `morning_commute.py` / `evening_weather.py` under `~/.hermes/scripts/`). The new failure surface was concurrency-under-load and provider/fallback correctness:

- **Performance** (`references/performance.md`): new **"a speedup can hide a regression — measure success, not just wall-clock"** section. (1) A keep-alive + higher-concurrency EV scan (~5.6× faster) *raised* the upstream HTTP-500 rate the old curl path had silently swallowed — measure the true 2xx count, not just ms. (2) **Fan-out concentrates load on one host** — parallelizing same-host calls trips its per-IP rate/concurrency limit and synchronized retries re-collide; bound concurrency, key it, back off.
- **Bug hunt** (`references/bug-hunt.md`): four new bug-classes — **substring/priority-order shadowing** (a broad `"rain"` match shadowed `"heavy rain"`); **provider-fallback order rides on the primary's failure modes** (fixing Google geocode's `KeyError` silently demoted OneMap's richer building-level address); **a rate-limit/error body that mimics empty data** (a `data:null` 429 read as "no data" → misdiagnosed as a parser bug, surfaced as "Air Quality unavailable"); and **an uncaught exception in a stdout-delivered `no_agent` script posts its traceback to the channel**.

## 1.2.0 — 2026-06-06

Incorporated lessons from a `morning-brief` tune-up — the **first LLM-calling skill** tuned here (a provider-agnostic per-article summarizer over a 44-feed RSS pipeline), and the first audited with **independent multi-reviewer fan-out**. New failure surface vs the earlier render/DB skills:

- **Token efficiency** (`references/token-efficiency.md`): new **runtime per-call LLM prompt** section (the pass was previously only about `SKILL.md` size). Drop fields the model doesn't use, cap input context per task type, trim the prompt slice (not the stored field that also feeds scoring), **`max_tokens` doesn't bill**, watch no-op slices — measured on real data, honest that the absolute footprint is tiny.
- **Bug hunt** (`references/bug-hunt.md`): three bug-classes — **empty-string normalization → false-equality merge** (`normalize_url("")==""` fabricated cross-source corroboration), **`except` narrower than the failures** (a `RequestException`-only catch let a malformed-200 `KeyError` escape the retry loop), and **unbounded producer vs a hard downstream limit** (LLM output → Discord 2000-char). Plus a method: **fan out independent *blind* reviewers and fix by agreement count** (raises recall, filters false positives).
- **Performance** (`references/performance.md`): **prove a behavior-preserving refactor with an output fingerprint** (sha256 of a canonical projection, before/after); **raise a concurrency cap to collapse waves** + **isolate the win with a fixed-latency stub**; watch **duplicated work across a helper boundary** and cache per-item values in O(n²) loops.
- **Delivery** (`references/delivery-reliability.md`): **content size limits** (2000-char 50035 / empty-content 50006 / safe chunking) and the **multi-message state-ordering invariant** — record delivered-state only after all sends; on a permanent failure exit before the state write so it re-sends cleanly instead of duplicating.
- **Safe edit** (`references/safe-edit-workflow.md`): checksum-verify the local copy against the live file before editing (don't clobber unseen drift).

## 1.1.0 — 2026-06-06

Incorporated lessons from a `token-usage` tune-up (10 bug fixes, a SQL-query speedup, a cron fix, and a 13.5K→2.6K-token SKILL.md slim) — the failure surface differed from the founding task-tracker pass (cron-executed script, a SQLite data layer, a bloated SKILL.md), so several patterns were new:

- **Bug hunt** (`references/bug-hunt.md`): added **Step 0 — find the copy that actually executes** (a `no_agent`+`profile`-unset cron runs `~/.hermes/scripts/X.py`, not the skill-dir copy; they drift, and reviewing the wrong one wastes the pass). Added a bug-class: **shared helper that silently emits wrong-but-plausible output** (e.g. a renderer column-shift on header/row mismatch) → make it fail loud.
- **Performance** (`references/performance.md`): added a **database/query-layer** section (`EXPLAIN QUERY PLAN`, push filters into SQL — a `LIKE` prefilter measured ~14×, covering-index tradeoff, check `freelist_count` before VACUUM) and the **cold-vs-warm cache** caveat (one query: ~10.5 s cold vs ~0.85 s warm — run twice, report both).
- **Safe edit** (`references/safe-edit-workflow.md`): compile/dry-run with the **gateway venv interpreter** (`~/.hermes/hermes-agent/venv/bin/python`), not system `python3` (imports differ — user-site off, PIL needs the dist-packages insert); plus §5b — change cron config with **`hermes cron edit`**, never hand-edit `jobs.json` under a live gateway; watch for a wrong `deliver` target / stray `profile`.
- **Token efficiency** (`references/token-efficiency.md`): **relocate, don't delete** — append unique blocks to a reference, pointer-replace duplicated ones, then verify the move landed.
- **`scripts/skill_audit.py`**: now also reports **DUPLICATION** (verbatim-repeated SKILL.md lines + broken/merged numbered lists) and **STRAY FILES** (`__pycache__`/`*.pyc`/`*.bak`).

## 1.0.0 — 2026-06-06

Initial release. Distilled from a live task-tracker tune-up (4 token-efficiency changes, 7 bug fixes, and a Discord-delivery rewrite) into a reusable four-pass method:

- **Pass 1 — Bug hunt** (`references/bug-hunt.md`): bug-class checklist + "validate against live data" discipline.
- **Pass 2 — Performance** (`references/performance.md`): profile first; the bottleneck is usually process spawns / sequential network calls, not compute.
- **Pass 3 — Token efficiency** (`references/token-efficiency.md`): progressive-disclosure refactor of SKILL.md (took the source skill ~4,180 → ~1,200 hot-path tokens).
- **Pass 4 — Delivery reliability** (`references/delivery-reliability.md`): rip out `hermes send`/`MEDIA:` tags; use direct Discord REST multipart (token from `~/.hermes/.env`).
- **Safe edit workflow** (`references/safe-edit-workflow.md`): backup → push → compile/dry-run → validate-without-spamming → version+changelog → cleanup.
- **`scripts/skill_audit.py`**: no-LLM auditor — SKILL.md token budget by section, reference-link integrity, frontmatter lint, and a scripts anti-pattern scan.

---

## Per-skill tune-up provenance

### email-gmail-triage (2026-06-09 — token-efficiency pass)
- **SKILL.md:** 12,309 → 1,854 tok (−85%). Bug tables (5,521 tok) → `CHANGELOG.md`; Category Detection Logic (1,076 tok) → `references/category-rules.md`; Architecture (829 tok) → `references/architecture.md`; resolved implementation notes + duplicate rules → `CHANGELOG.md` or removed.
- **CLAUDE.md file map** updated to include new `CHANGELOG.md`, `references/architecture.md`, `references/category-rules.md`.
- **All 40 key terms** verified present across the doc set after relocation.
- **9 old backup files** cleaned up; latest kept as safety net.
- **Version bumped** to 2.0.0 in target skill frontmatter.
- **Method lessons fed back:** bug-table-sink pitfall, CLAUDE.md staleness pitfall, auditor path assumption → `references/token-efficiency.md`.

### traffic-camera (1.1.0 → 1.2.0 → 1.2.1, 2026-06-08)
- **Bug hunt fan-out:** 3 reviewers (control-flow, data-shape, adversarial refuter). 3/3 agreed on null-lon crash + corrupt cache; refuter killed hardcoded-API-key and format=png32 as non-bugs for this use case.
- **New bug-class:** asymmetric null-checking on lat/lon — every guard checked `latitude is not None` but not `longitude`. Affected all 3 scripts uniformly. **Now recorded in `references/bug-hunt.md`** (checklist), widened in the 1.2.1 follow-up to cover the filter-site-vs-format-site gap and the truthy-`0.0` trap.
- **Code fixes (1.2.0):** 6 (null-lon guards, atomic cache write, double-geocode elimination, error handling on downloads, dead code removal — 53 lines).
- **Docs (1.2.0):** SKILL.md 2305 → 1038 tok (−55%). Removed duplicate "Numbered Map Markers" sections, moved inline code to references, fixed MEDIA: anti-pattern.
- **Result:** All 3 scripts produce identical output to baseline. 90 cameras, 29 East, 18 on commute.
- **1.2.1 follow-up (post-method-audit remediation):** 1.2.0 was later audited against this method and left residuals — Workflow 2 was non-executable (docs named a single-camera mode + an "image + map" download that don't exist), `region-boundaries.md` named a nonexistent `assign_center_cameras()` and a wrong Thomson→South example (verified: resolves to North), the label cap was off-by-one (`Z`=35 not 36; >35 emitted invalid labels), and the named asymmetric-null class was applied only to filters, not the format/label sites. **Root method lesson:** the relocated references were written *before* code froze (docs-not-last) and the *terminal cumulative-diff gate* wasn't run — exactly how the stale `assign_center_cameras` name and the non-executable Workflow 2 shipped. Reinforces the 1.9.0 *docs-last* + *terminal-gate* rules; the remediation followed them (code frozen via an adversarial gate over the cumulative diff, then docs).
- **1.3.0 (table feature):** Workflow 3 now reverse-geocodes each along-route camera and emits a `# / CamID / Dist / Address` table; the shared `reverse_geocode` learned to prefer a non-`plus_code` result (street/route name instead of a bare Plus Code). Map output byte-identical. Gate clean.
- **1.4.0 (interactive-misroute fix):** interactive route replies kept landing in **#alerts**. Root cause: a no-target `send_message` → the **home channel** (`DISCORD_HOME_CHANNEL` = #alerts); a soft `agent.environment_hint` instruction didn't hold (reverted). Fix: a self-delivering `traffic_camera_report.py` that resolves the current thread from `HERMES_SESSION_KEY` and posts the map + table itself (agent does no delivery, can't misfire). The gate caught a positional-key bug (`parts[5]` = user_id for non-threaded `group` chats; switched to the stable `parts[4]`).
- **Method lessons fed back (skill → 1.9.3):** Discord delivery internals (home-channel default, the `validate_media_delivery_path` real-file gate, Cloudflare-1010-blocks-`urllib`, and the self-resolve-thread-from-`HERMES_SESSION_KEY` pattern) → `delivery-reliability.md`; the positional-key bug-class → `bug-hunt.md`; the background skill-curator that edits skills between passes → `safe-edit-workflow.md`; the auditor's skill-dir-only anti-pattern false-clean → `SKILL.md` Phase 0.

### discord-table-format (1.x → 2.0.0, 2026-09-29/30)
- **Scope:** bug hunt + delivery + docs of a shared renderer (9 consumers) that the interactive agent was supposed to use for every Discord table. Phase 0 found the agent's `python3` is the venv (docs said system), the gateway doesn't read `trigger_conditions`, and `/tmp/hermes_cache` has no sweeper.
- **Findings that mattered:** 213 raw markdown tables sent (skill ~never loaded); tables misrouted to #alerts via no-target `send_message`; multi-`MEDIA:` replies re-batched; renderer import downgraded callers' requests/urllib3/certifi; per-interpreter rendering; zero-width/emoji misalignment; tabulate rewriting "0.50"→"0.5"; header bolding; silent image loss on duplicate titles; stdin hang; bot token leaked into state.db by hand-written deliveries.
- **Shipped:** renderer fixes (byte-identical on all 33 real-input sections, 46/46 cross-interpreter pixel-identical); new `send_tables.py` one-command delivery (session-bound target, token on curl stdin, nonce dedupe); SKILL.md 4.1K → 1.4K tok with internals relocated; `config.yaml` `platform_hints.discord.append` (user-approved); emoji→symbol map (user's choice).
- **Method lessons fed back:** 1.9.33 (bug-hunt.md, delivery-reliability.md, token-efficiency.md, SKILL.md).
