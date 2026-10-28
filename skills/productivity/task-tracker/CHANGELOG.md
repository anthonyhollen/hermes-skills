# Changelog

## 1.8.0 — 2026-09-26

New `move <items> <category>` write subcommand — change an existing task's frequency in place. Until now the only path was remove + add, which resets `added`, reshuffles ordering, and costs two undo entries per task.

- Items resolve like `done` (row numbers against the thread's last-shown table, names, ids, unique partial names); `--target` supported; every write is `--dry-run`-able.
- Preserves `name`, `id`, `last_done`, `added` in a single `atomic_write` (one backup, one undo entry). Output: `↔ Name (id): monthly → quarterly`; `= …` when already in the target category (clean no-op); invalid category / unknown row refused with nothing written; partial successes exit 3.
- `scripts/test_task_tracker.py`: +7 checks (row-number resolution, preservation, undo byte round-trip, invalid category, unknown row, no-op, partial exit 3; `move` also added to the undo-chain round-trip).

## 1.7.0 — 2026-09-24

Full tune-up (hermes-skill-tuneup 1.9.25): usage forensics on 28 real sessions (Jul 11–Sep 21) + 3 blind reviewers (control-flow/delivery, edge-case/data-shape, live-usage forensics) + an adversarial refuter during the hunt, then a **five-lap** terminal gate (regression + adversarial reviewers each lap; every lap until the last found a real bug in the previous lap's fix). Read path is behavior-preserving: `--json` fingerprint (ids stripped) byte-identical to the pre-change baseline on live data under both the gateway venv and system python3.

**Root problem — the write path was prose, and the agent re-implemented it every time.** 39 writes in 72 days, ~4.6 tool calls each; the agent hand-wrote ~40 lines of atomic-write Python per "done" and it broke repeatedly: 3× gateway "Blocked" guard on heredocs, `KeyError`/`JSONDecodeError` from `read_file`'s line-numbered output, an assert bug, a prune leaving 11 backups, a fake `_000000` backup timestamp, 2 in-place `patch` edits of the live JSON (one briefly renamed a task to "Sayang bottle watch"), the whole 6 KB file re-typed via `write_file` several times, and a stray one-off `~/.hermes/scripts/task-tracker-mark-done.py`. SKILL.md itself listed the write order as "…rename → backup…" (backup AFTER write), contradicting its own reference.

**New — write subcommands in `scripts/task-tracker.py`** (the script is no longer read-only; user-approved design change):
- `done <items> [--date] [--target] [--yes]`, `set <item> <date|never>`, `add "<name>" <cat> [--last-done]`, `remove <item> --yes`, `rename <item> "<new>"`, `undo [--previous] [--force]`; every write `--dry-run`-able.
- One `atomic_write`: flock → read → mutate → skip if unchanged → validate changed tasks → fsynced backup of exact pre-bytes → tmp+fsync → `os.replace` → undo-stack push → prune by filename timestamp (10 regular + 10 `_undo`).
- **Row numbers resolve against the table the user actually saw** (user's choice): delivery saves `{row: {id, name}}` per thread in `task-tracker.last-shown.json` (only sections that actually arrived); `done N --target <thread>` uses it, and refuses a row whose task was renamed/removed since. Replaces "run `--json` and map numbers yourself", which re-sorted after every write.
- Name matching: exact (case/space/quote/punctuation-insensitive) first, then a unique contiguous whole-word run ≥3 chars, excluding stop-words; remove/rename exact only. Args re-joined then split on commas only (`done 2 and 5` no longer marks "Toilet bowl **and** sink wash"). Never moves a date backwards; bare `all` needs `--yes`; names that are digits / "all <category>" / contain commas are rejected.
- **Undo = a stack** (`task-tracker.undo.json`, `{backup, pre, post}` sha256 per write): pops only the top entry, restores exact bytes, and refuses after a no-op/failed last command (`--previous`), after an outside edit (`--force`), with a missing backup (clears the stack; use `set`), or with an empty stack (always — no "newest backup" fallback, so legacy pre-change backups are by-hand recovery only). Crash-after-restore is detected and consumed. Undo never deletes backups; backup names always sort after existing ones (a same-second name is never reused after pruning).
- `scripts/test_task_tracker.py` — 55-check regression suite on a temp copy (never touches live data or Discord); every gate finding has a case.

**Read/delivery hardening**
- A bad `categories` entry (`interval_days` `"7"`/`null`, non-dict, `categories: null`, `tasks: null`) no longer crashes the report + resolver; falls back to 7/14/30/90 with a warning. Section titles are built from the interval actually used (no more "every 7 days" over a 30-day calculation).
- `--json` sections carry `ids` (renderer ignores it).
- Discord: first 401/403/404 aborts the run with a hint (the agent once passed the *Triggering message id* → 5× 404); 429 `retry_after` capped at 10 s (was unbounded — `3600` slept an hour), list-shaped 429 bodies no longer crash, second 429 now an ERROR; `WARN:` → `WARNING:` (SKILL.md tells the agent to surface `WARNING:`); target must be `discord:`; empty task list posts "No tasks are being tracked." instead of failing.

**Docs**
- `SKILL.md` rewritten around the subcommands (hot path no longer needs `data-and-writes.md`): `--target` = thread id (never a message id), `&`/"and" → separate items, correction recipe (`set <id> <old>` + `done 23 --date <same>`), undo refusal handling, no redo.
- `references/data-and-writes.md` now documents what the script does (state files, atomic_write, undo stack) + by-hand recovery; `references/canonical-tasks.md` regenerated from live data (was 30 tasks with "Change toothbrush head" wrongly monthly; now 32 + the `categories` block); `references/discord-delivery.md` hand-send snippet now checks HTTP status.

**Housekeeping (user-approved):** archived `~/.hermes/scripts/task-tracker-mark-done.py` and the stale Jun-17 `~/.hermes/data/task-tracker.json.bak` into `task-tracker backups/_archive/`.

**Considered and not done:** backup retention stays 10 (user's choice; ≈17 days at current write rate). Clock-skew backup ordering rejected: the Pi has an RTC + NTP (Asia/Singapore). Tie-order of equal `last_done` rows left as array order (stable in every backup; changing it would renumber once).

## 1.6.0 — 2026-07-11

Full tune-up (hermes-skill-tuneup method): bug hunt → performance → delivery → terminal validation gate → docs. Driven by 3 independent blind reviewers (control-flow / data-shape / delivery-security lenses) + an adversarial refuter during the hunt, then a fresh terminal-gate fan-out over the cumulative diff. Every finding reproduced on a clone of the live data; the `--json` output fingerprint is byte-identical before and after (behavior-preserving on real data). No performance change — the compute path is ~0.5 ms; the ≤5 curl sends + one render subprocess dominate, and the CLI→curl migration (1.3.0) already captured that win.

**Bug fixes — `scripts/task-tracker.py`**
- **A single malformed `last_done` no longer crashes the entire report (and the `--json` "done N" resolver).** `build_sections` parsed `date.fromisoformat(ld)` with no guard, so one non-zero-padded/garbage date (`"2026-7-6"`, which the natural-language write path can realistically produce — confirmed a `ValueError` on the Pi's Python 3.11) took down all tasks in every mode. Now each row is parsed defensively: an unparseable date is caught, warned to stderr, and shown as a **"Bad date"** row (kept visible so the running counter stays stable — no silent drop). Also hardened the same loop against a missing `tasks` key (`data.get("tasks", [])`), a missing/empty `category` (→ `uncategorized`, still surfaced), and a missing `name` (→ falls back to `id`).
- **An out-of-range far-future date (near year 9999) no longer crashes at the interval arithmetic.** *This was a regression the terminal-gate adversarial refuter caught in the above fix itself:* the `try/except` guarded only the parse, but a date that parses yet sits within `interval_days` of `date.max` then overflowed at `ld + timedelta(...)` (`OverflowError`, uncaught) — the exact failure the fix promised to eliminate. Now the dated computation is wrapped too; an overflowing date degrades to "Bad date" instead of aborting.

**Delivery reliability — `scripts/task-tracker.py`**
- **Message body now sent via `--form-string payload_json={json.dumps(...)}` instead of `-F content=`.** curl's `-F` treats a `;` in the value as a form-part parameter separator — silently **truncating** any caption containing `;` (proven on the Pi's curl 7.88.1: `-F content='… ; …'` dropped everything after the `;`) — and a leading `@`/`<` as a file-read directive. The new form sends the content verbatim (newlines, `;`, `@`, `<` all literal) and carries **`allowed_mentions: {"parse": []}`** in the same atomic request, so a task name containing `@everyone`/`<@id>` (reachable via `--text-only`, which puts task names in the body) can never ping the channel. Copies the hardened multipart pattern from `sg-commute`/`token-usage` (see `references/discord-delivery.md`). Validated without posting: `GET /channels/<id>` → 200, and an empty-content `payload_json` probe → Discord `400/50006` (proves the new request shape is parsed).

**Docs**
- `references/discord-delivery.md`: corrected the Status-column description to what the script actually emits (`Xd overdue` / `On track` / `Never done` / `Bad date`; removed the stale `days_ago == 0 → "Today"` claim the code never produced), and replaced the stale `-F content=` curl example with the `payload_json` + `allowed_mentions` pattern.
- `references/data-and-writes.md`: the write-path Validate step now requires `last_done` to be zero-padded ISO `YYYY-MM-DD` or `null` (root-cause guard for the bad-date crash on the write side).
- `SKILL.md`: version 1.5.0 → 1.6.0; added the ISO-date pitfall; compressed two Key Pitfalls (backup-before-write, undo-staleness) that fully duplicated `references/data-and-writes.md` down to imperative + pointer.

## 1.5.0 — 2026-06-06

Harden "Mark Done" against the most common write bug: row numbers ≠ JSON array indices.

**Root cause** — agent read `task-tracker.json` and used array positions as row numbers. But the script sorts by category (weekly→fortnightly→monthly→quarterly), then by `last_done` oldest first, with a running counter — diverging from insertion order. Result: 3 of 5 tasks marked to wrong entries.

**Fixes — `SKILL.md`**
- Mark Done section: added mandatory `--json` pre-step. When user provides row numbers, agent MUST run the script with `--json`, build a `{row_number: task_id}` lookup, then resolve. Never assume array position = table row.
- Key Pitfalls: added "Row numbers ≠ JSON array indices" with incident reference.
- Key Pitfalls: added "Undo must restore the pre-update state, not an older backup" — backups may lag by days; blindly restoring loses legitimate updates.

**Incident** — During undo, restored from a Jun 1 backup that predated legitimate "done" updates (Mop house, Toothbrush charge updated Jun 1). Lost those updates. Fixed by comparing backup against the known pre-update state and re-applying the two lost updates.

## 1.3.0 — 2026-06-06

Reliability + speed fix after two live failures: (1) the agent improvised a raw Markdown table instead of using the skill, and (2) the script hung/timed out.

**Root causes**
- **`hermes` is not on `PATH` in non-login shells** (how the agent's terminal tool and cron invoke scripts), so the `subprocess.run(["hermes", …])` sends from v1.1.0 failed, and an over-the-timeout `hermes send` raised an uncaught `TimeoutExpired` → traceback/hang.
- The v1.1.0 "P1" change delivered via `hermes send` + `MEDIA:` tags — the exact anti-pattern the `token-usage` and `sg-commute` skills document as unreliable: the gateway's `extract_media()` re-batches text and images separately, breaking header↔image pairing/order.

**Fixes — `scripts/task-tracker.py`**
- **Delivery rewritten to direct Discord REST multipart** (proven pattern from `token-usage`/`sg-commute`): read `DISCORD_BOT_TOKEN` from `~/.hermes/.env`, then **one `curl` multipart POST per section** with `content` (caption) + `file` (image) in the same request — atomic header+image, correct order, no `hermes` dependency. Args passed as a subprocess list (no shell) so real newlines survive (avoids the literal-`\n` bug). Removed the `hermes`-based `send()`.
- **Hardened:** `--max-time` per request, `429` rate-limit retry honoring `retry_after`, `time.sleep(0.5)` between sends (Discord 5 msg/5 s/channel), explicit HTTP-status checks, and `render_tables()` now catches `TimeoutExpired`/`OSError`/non-JSON instead of crashing.
- **Target parsing:** `discord:<id>` or `discord:<channel>:<thread>` → posts to the trailing numeric id (a thread is a channel for the messages endpoint). Non-numeric target → clean error.
- **Cron-safe stdout:** success/diagnostics now go to **stderr**, so an auto-deliver scheduler won't treat status text as a message. `--json` still prints to stdout.

**Fixes — docs**
- `SKILL.md` "Show Status" is now imperative: run exactly the script; never hand-render Markdown; never use `send_message`/`MEDIA:`; on error, surface it rather than falling back to a table. (Directly addresses failure #1.)
- `references/discord-delivery.md` "Sending" replaced the `MEDIA:` guidance with the curl-multipart pattern + the `extract_media()` warning.

Validated end-to-end without posting: token loads, channel id parses, `GET /channels/<id>` → HTTP 200 (bot can reach the thread). Compute/render unchanged (4 sections / 30 rows).

## 1.2.0 — 2026-06-06

Documentation restructured for token efficiency (no behavior change). `SKILL.md` — the doc the agent loads on every write op — was ~4,180 tokens; reference-only and verbose content moved out so the hot path is leaner:

- Moved this changelog out of `SKILL.md` into `CHANGELOG.md`.
- Moved the recovery task list to `references/canonical-tasks.md`.
- Moved the atomic-write procedure + validation checklist + schema/date notes to `references/data-and-writes.md`.
- Moved the Discord display format + image pipeline + per-category header/image rules to `references/discord-delivery.md`.
- Condensed the Operations section and deduplicated the pitfalls (several restated Operations content).
- Added a References table so the agent loads each detail file only when needed.
- Note for write ops: read the **raw** `task-tracker.json` (compact), not the script's `--json` computed output.

## 1.1.0 — 2026-06-06

Bug hunt + performance pass on `scripts/task-tracker.py` (the standalone, read-only status/delivery script). The done/add/remove/rename write path is unchanged.

**Bug fixes**

1. **Unknown categories no longer silently dropped.** `build_sections` previously iterated only `CATEGORY_ORDER`, so a task whose `category` wasn't one of the four known values vanished from the report *and* shifted every later task's running number (breaking "done N"). Unknown categories are now appended in their own section (sorted) with a `WARNING` to stderr.
2. **No more `KeyError` on a category missing `interval_days`.** The `intervals` comprehension now uses `.get("interval_days", DEFAULT_INTERVAL)` (30) instead of an unguarded `["interval_days"]`.
3. **`hermes send` failures are no longer swallowed.** `send()` now checks the subprocess return code, logs stderr, and returns success/failure; `main()` counts failures and exits non-zero. Previously a failed delivery still printed "Delivered …" and exited 0 — silent failure for cron.
4. **Images deleted only after confirmed delivery.** A failed image send no longer has its file cleaned up, so it survives for retry/debugging.
5. **Removed dead `save_data()`** (plus unused `shutil` / `datetime` imports and `BACKUP_DIR`). This script is read-only; the atomic-write + backup logic lives in the LLM agent flow. Its presence here was untested and its step order disagreed with the documented procedure.
6. **`load_data()` fails cleanly.** A missing or corrupt `task-tracker.json` now prints a clear error and exits non-zero instead of dumping a traceback.
7. **`summary_text()` takes `today` as a parameter** instead of recomputing `date.today()`, so the title date and computed status can't disagree across a midnight boundary.

**Performance / efficiency**

Profiling on the RPi showed the work is dominated by `hermes send` process startup (~500–620 ms cold-import floor *per call*, plus the platform round-trip), not by compute (~50 ms for load + `build_sections`) or rendering (~650–730 ms for all tables in one process — already batched and import-bound, not draw-bound). So the only worthwhile lever is the number of `hermes` invocations.

- **P1 — header + image merged into one `hermes send` per category.** Each section now sends `"{header}\nMEDIA:{image}"` in a single message; hermes' `send_message_tool` extracts the `MEDIA:` path as a native attachment and keeps the header as the caption. Default delivery dropped from **9 sequential sends → 5** (1 title + 4 captioned images), saving ≈ 2.8 s wall-clock (~40–45%). Side benefit: header+image are now atomic, removing the "header sent, image failed" half-state. Removed the now-unused `send_image()`.
- **P4 — `cleanup()` uses a single `os.remove` in `try/except OSError`**, dropping a redundant `stat` and the TOCTOU window.

**Considered but not changed:** running the script under the hermes venv to import `send_message_tool` in-process (one import for all sends) — biggest possible win but couples to a private API, so deferred. `render_tables.py` per-line `textbbox` width measurement could use a single monospace advance, but saves only ~10 ms against a ~700 ms import and it's a shared skill — not worth it.
