# Changelog — discord-table-format

## 2.0.0 — 2026-09-29 — hermes-skill-tuneup (bug hunt, delivery, docs)
Four blind reviewers (control-flow/contract, edge-case/data-shape, session-history forensics, doc↔code/delivery) + an
adversarial refuter over the live copy, a 200-session state.db export and Pi probes; then a re-entrant terminal gate over
the cumulative code diff, four laps: lap 1 = regression/contract + adversarial + delivery/security agents (26 LOW/MED
findings); lap 2 = adversarial agent (nonce ignored headers → a corrected table would be silently dropped, float rounding
in mixed columns, abort-report windows) — its fix-verification agent died on a budget cap, so that lens ran as the
author's mutation-checked probes; lap 3 = adversarial agent (multi-signal race, a `Set` check rejecting `dict.keys()`);
lap 4 = the signal machinery removed (simplify, not patch) + small fixes, self-probed clean: every new probe fails on the
previous lap's code, all earlier suites pass, an 8000-table original-vs-new differential is identical.

**Why the skill mostly didn't work in practice (session forensics).** 213 raw markdown pipe tables in 70 of 191 Discord
threads (Discord doesn't render them); only 2 of 35 agent-composed tables went out as images under the June SKILL.md.
The description promised "Auto-loads" and the frontmatter's `trigger_conditions` — which **no gateway code reads** — so the
agent assumed it was already applied. When it was loaded, the documented procedure cost ~3 tool calls per table and gave
no Discord target: `send_message(target="discord")` posts to the home channel #alerts (9 table messages once landed there),
several `MEDIA:` tags in one reply were re-batched (all text, then all images — the user complained 06-25), and
`echo '<json>' |` broke on an apostrophe. The built-in Discord platform hint even says "tables are NOT supported — use
bullet lists". Hand-written curl deliveries printed the bot token into the session DB.

**Renderer (`scripts/render_tables.py`) — fixed** (all reproduced on the Pi first):
- Module-level `sys.path.insert(0, dist-packages)` was never undone: any in-process importer under the venv got system
  requests 2.28 / urllib3 1.26 / a 2022 certifi for later imports. Now inserted only around the tabulate/PIL imports.
- Same input rendered differently per interpreter (venv tabulate has wcwidth → emoji/CJK counted 2 cells; the font draws
  1) and zero-width characters (the U+FE0F in "⚠️", ZWJ, SHY, BOM) were counted as columns → misaligned rows. Now width
  counting is forced to characters, control/zero-width/ANSI/bidi characters are stripped (strip, then NFC), tabs/newlines
  → space; 46/46 sections pixel-identical across interpreters (was 45/46).
- Status emoji the font lacks drew as identical empty boxes (✅ and ❌ indistinguishable): now ✅→✓ ❌→✗ ⭐→★ 🔴🟢…→● ⚪→○
  (user's choice).
- tabulate's number parsing rewrote caller strings ("0.50"→"0.5", "1.00"→"1", "1.10"→"1.1", "1e5"→"100000") — real in
  model-eval's win-rate table. Columns are now classified with tabulate's own `_column_type` and rendered verbatim with
  decimal alignment; float *objects* keep the old short `g` form in numeric columns (exactly the original output).
- The header row was bolded when a header contained a keyword ("Flaky/Failed"); keywords weren't lower-cased; a string
  `bold_match` was split into characters; `null`/non-list crashed; `""` bolded everything; a cell containing `─` blocked bold.
- Duplicate titles silently dropped an image (output is keyed by title) → ValueError. Dict/string rows drew keys /
  characters and a generator produced a header-only image → ValueError for str/bytes/mappings/sets, other iterables (tuples, `sqlite3.Row`, dict views) accepted.
  All sections are validated before any PNG is written (no orphans). `title: null` crashed; JSON `null` cells showed "None".
- A mistyped file argument silently read stdin and hung; `--one` with missing args / bad input printed tracebacks → one-line
  errors, exit 1 (bad input) / 2 (usage); `-`, `/dev/stdin`, `-h` work.
- Same-millisecond filename collisions across processes (seq restarts at 1) → `O_EXCL` create; a failed save leaves no file;
  `prefix` sanitized (no path separators, ≤ 120 UTF-8 bytes); the renderer now prunes its own PNGs older than 24 h (nothing else
  sweeps `/tmp/hermes_cache`).
- Validated: pixel md5 old vs new over fixtures + all 33 real-input sections (21 agent-built inputs, live task-tracker,
  skill-usage) — identical except the intended changes; task-tracker's regression suite ALL PASS; every consumer's call
  style (see `references/implementation.md`) re-run on the live file; timing unchanged (~200 ms per batch).

**New `scripts/send_tables.py`** — the agent's one-command delivery (the method's "take the LLM out of the delivery path"):
renders all sections in-process and posts each as ONE curl multipart message (`--form-string payload_json` + file,
`allowed_mentions` none) to the current conversation from `HERMES_SESSION_KEY` (verified: 84/84 recent sessions' parts[4]
== the thread/chat id; the terminal tool injects the current turn's session vars). Fail-closed target (17–20 ASCII digits,
Discord only); `--to` elsewhere or to the home channel needs `--force-target`; token read from `~/.hermes/.env` and passed
to curl on stdin (`-K -`, never argv; `-q` ignores `~/.curlrc`); nonce + `enforce_nonce` so a re-send within minutes
returns the existing message; only a short 429 is retried; stops at the first failure with `may_have_posted`; always prints
its JSON on every normal exit, Ctrl-C or internal error, and a flushed `posted:` line per message on stderr (the record if
it is killed — signal handling was tried for three gate laps, each opening a new hole, and removed); deletes each PNG
after a 2xx. Validated with a stub curl (no real posts), then one live post to
the playground channel.

**Docs.** SKILL.md rewritten around the one command (4.1K → ~1.3K tokens); the June content relocated to
`references/implementation.md` (renderer internals, caller contract, consumers) and `references/delivery.md`; false claims
removed or corrected: "tabulate installed system-wide" (it's `~/.local` / the venv), "execute_code cannot import PIL",
the nonexistent `delete_table_image()` / `prefer_image` / `MAX_TEXT_WIDTH`, "use `hermes send` for no_agent scripts —
task-tracker uses this pattern" (false since task-tracker 1.3.0; warframe-scraper copied it), "must call send_message" in a
no_agent script, a "Cron Delivery" section in token-usage that doesn't exist, the Pipeline-"above" reference, duplicated
sections. Added version/metadata; removed the inert `trigger_conditions`. `~/.hermes/config.yaml` gained
`platform_hints.discord.append` (user-approved) so every Discord turn is told to use this skill for tables.

**Known limits (documented):** right-to-left text and combining marks without a precomposed form still misalign; no image
size cap (split big tables); emoji other than the mapped status set draw as a box.

## 1.x — May–June 2026 (reconstructed; no changelog existed)
- 2026-05-09/10: skill built (fancy_grid, image-first, DejaVu Sans Mono, tight crop, retina) — see
  `references/development-log.md` (historical).
- 2026-06-05: consolidated into one shared `render_tables.py` (single interpreter, one process per report, direct 52 px
  drawing, `compress_level=1`, font singletons); header/row width mismatch raises `ValueError` (from the token-usage
  tune-up, `token-usage/references/2026-06-consolidation-bugfix-perf.md`).
- 2026-06-25: SKILL.md last edited (delivery-context sections).
