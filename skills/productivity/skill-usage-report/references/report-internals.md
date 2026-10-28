# skill-usage-report — report internals

Data rules, caveats, validation recipe, and known-good values for
`scripts/skill_usage_report.py`.

## Data: usage (state.db)

- DB: `~/.hermes/state.db`, opened read-only (`mode=ro`, timeout 10 s).
- Query: `SELECT timestamp, tool_calls FROM messages WHERE role='assistant' AND tool_calls LIKE '%"skill_view"%'`.
  Keep the `role='assistant'` guard — tool-result rows also contain the string. The prefilter is exact
  and fast (token-usage perf notes: prefiltered LIKE ≈ 60 ms warm vs ~850 ms scanning all rows).
- Parse: each row's `tool_calls` JSON → calls where `function.name == "skill_view"`. Unparseable rows
  are counted and warned on stderr (never fatal).
- **A use = a successful load of the skill itself (v1.1.0).** Skipped:
  - `arguments.file_path` set — a supporting-file read, not a load (2026-10-02: 147 of 513 calls;
    hermes-skill-tuneup showed 33 "uses" of which 28 were file reads);
  - the tool result is anything but `success: true` — failed (`hermes` phantom, not-found names) or
    `Skipped: another tool call in this turn used an invalid name…` (15 calls: 10 loads + 5 file reads);
  - the tool result is a dedup stub (`dedup: true` — repeat view of an unchanged, already-loaded skill).
- Result lookup: `SELECT tool_call_id, content FROM messages WHERE role='tool' AND tool_call_id IN (…)`,
  chunked 400 ids per query (~70 ms for 513 ids on the Pi). Match **by id** — 101 older result rows
  have `tool_name` NULL. A missing result row counts as ok. A **non-JSON** result counts as ok ONLY
  when it is a compaction placeholder (`[skill_view] name=X (N chars)`, 25 rows) or JSON truncated
  after `{"success": true`; everything else is a failure — of 27 non-JSON results, one was a
  truncated `{"success": false, … not found` and one a `Skipped: …` notice (reviewer catch, v1.1.0).
- Skill key = the skill's **directory name** (`make_canon`): the result's `name` (else the arg) is
  mapped through the frontmatter-name aliases, and a leading category is dropped only when it is a
  real category of the tree (`productivity:task-tracker` / `productivity/task-tracker` →
  `task-tracker`; `plugin:skill` stays). Keying by directory means a later compaction (placeholder →
  arg fallback) can't move a load between `ideation` and `creative-ideation`.
- Aggregates: per-skill count + newest timestamp (last used); window start = oldest counted load; totals
  for the caption. Skipped counts go to the stderr confirmation line.
- Included: all rows regardless of active/compacted flags — they were real loads. Rows lost in the
  2026-09-13 `state.db` repair are simply absent; the window is "since 2026-05-02, minus repair losses".

## Data: last updated (skills tree)

- Walk `~/.hermes/skills/**/SKILL.md`; skip hidden dirs, `.archive/`, `__pycache__`, `.git`.
- **Content files** = everything except suffixes `.pyc .db .log .json .csv .png .jpg .jpeg .gif
  .sqlite .wal .shm` and names containing `.bak` (backups are not edits). Data caches
  (`gmail_triage_state.json`, `cc_review.db`, `spotify.db`, `ev_*.csv`, `*.log`) are therefore
  excluded; docs/code/config are included.
- `last_updated` = max mtime over the skill dir's content files.
- **Note rule:** the topmost `## ` heading of the skill's root `CHANGELOG.md`, but only when the
  changelog's mtime is within **24 h** of `last_updated` ("same update episode"). A stale changelog
  must never supply the note — traffic-camera's CHANGELOG is from 2026-06-08 while its SKILL.md is
  2026-09-27, so its row shows `v1.9.3` (frontmatter version) instead. Fallback chain:
  changelog heading → `v<version>` → `-`.
- Version = frontmatter `version:` field (first 4 KB of SKILL.md). Notes truncate to 44 chars + `…`.
- Keyed by directory name; `scan_skills` also returns `aliases` = frontmatter `name:` → directory where
  they differ (skill_view results report the frontmatter name: `creative/creative-ideation` →
  `ideation`, plus 3 bundled mlops skills; the whole value is read, quotes stripped, so names with
  spaces work; an alias equal to a real directory name is dropped) and `categories` = every directory
  that holds skills. "installed" = SKILL.md directories.

## Rows (v1.1.0)

Top `--top` (15) by uses — ties broken by most recent use, then name, so the cut is deterministic —
then a divider row `— used in last N days —` and every lower-ranked skill whose last load is within
`--recent-days` (7; `0` = off), in rank order, numbered with its true rank.
Why: a top-N cut alone hid a skill used minutes earlier — on 2026-10-01 skill-usage-report and
skill-library-audit (1 load each, rank 43/44 of 44) were missing from the table the user had just asked for.

## Caveats

- "Uses" registers only **agent loads** (`skill_view` in sessions on this machine). Cron-run scripts and
  tune-ups executed over SSH from another machine never load the skill → "last used" can look stale
  (hermes-skill-tuneup: heavy recent activity, last recorded load June 9).
- The post-session curator (bg-review fork, `agent/background_review.py` sets `_session_db = None`) calls
  `skill_view` before every skill edit, but its messages never reach `state.db` — curator reads are
  invisible here, which is intended (they are maintenance, not use).
- `~/.hermes/skills/.usage.json` (Hermes' Curator telemetry) is deliberately NOT the source: it counts
  those curator reads and file reads too (2026-10-02: sg-commute 159 views there vs 31 loads here,
  discord-messaging 53 vs 2).
- Skill key = directory name (see above), resolved against the CURRENT skills tree: renaming, moving
  or deleting a skill (or its category) splits its history again (e.g. `ideation` vs
  `creative-ideation`); an unknown key shows `(not on disk)`.
- Cumulative: with the tree unchanged, counts only go up; a skill starts at 1 on its first load.
- Repeat loads in one session are counted: 18 (16 session/skill pairs, May–July 2026) as of
  2026-10-02. A `dedup: true` stub — Hermes' reply to an unchanged repeat view — is skipped; none
  in the data so far.
- Duplicate directory names across categories: last one walked wins (none today).

## Validation recipe (stub dry runs)

```bash
# stub curl: logs argv (token redacted) to /tmp/sur_stub/payloads.log,
# prints '{"stub": true}' + $STUB_CODE (no trailing newline), exit 0
mkdir -p /tmp/sur_stub && chmod +x /tmp/sur_stub/curl
S=~/.hermes/skills/productivity/skill-usage-report/scripts/skill_usage_report.py
VENV=~/.hermes/hermes-agent/venv/bin/python

# happy path → rc 0, one call with the caption + file=@png, PNG deleted after the stub's 2xx
PATH=/tmp/sur_stub:$PATH SKILL_USAGE_TARGET=000000000000000000 $VENV "$S"

# failure path → rc 1, stderr diagnostic, PNG kept for inspection
PATH=/tmp/sur_stub:$PATH STUB_CODE=500 SKILL_USAGE_TARGET=000000000000000000 $VENV "$S"

# target-unresolved (env cleared, no flags) → rc 1, "could not resolve a Discord target", no artifact
```

Stub details: `no-agent-cron-scripts` skill → `references/stub-binary-dry-run.md`.
Fail-fast order: token + target resolve BEFORE rendering (a config failure leaves no artifact);
a failed POST keeps its PNG.

## Known-good values (2026-10-02 SGT, v1.1.0 rules — total 356 loads / 38 skills / 146 installed)

task-tracker 55 · hermes-agent 45 · sg-commute 31 · token-usage 30 · morning-brief 22 · maps 16 ·
traffic-camera 15 · email-gmail-triage 14 · discord-table-format 12 · cc-bill-review 12 ·
foodpanda-expense-tracker 12 · spotify-playlist-refresh 11 · obsidian 9 · google-workspace 8 ·
hermes-agent-skill-authoring 6. Skipped: 147 file reads, 10 failed loads (5 more failed calls were
file reads). Divider block on 2026-10-02: plan, spotify, vault-audit, no-agent-cron-scripts,
skill-library-audit, skill-usage-report. Small upward drift is normal (new loads). The v1.0.0 rules
gave 513 / 44 on the same data.

## Runner commands

```bash
python3 $S --thread-id <thread_id>            # normal — agent passes the current thread
python3 $S                                    # target from HERMES_SESSION_KEY
python3 $S --to discord:1501742338639401071   # playground live test
python3 $S --dry-run                          # render only; prints the PNG path
```

Flags: `--top N` (default 15) / `--recent-days N` (default 7; 0 = off) / `--thread-id` / `--to` / `--dry-run`. Conventions: `[SILENT]` first
line; `os._exit(0/1)`; stdlib `fcntl.flock` run guard (`/tmp/hermes_cache/skill_usage_report.lock`);
crashes post a traceback to the home channel; `SKILL_USAGE_TARGET` env = test kill-switch only.

## Scheduling is off by design

Ad-hoc only (user decision 2026-09-29). If ever croned: deploy a byte-identical copy to
`~/.hermes/scripts/` and create the job; the scheduler runs it with the gateway venv (the script
inserts dist-packages itself for PIL/tabulate).
