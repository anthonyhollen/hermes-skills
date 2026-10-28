# Changelog — skill-usage-report

## 1.1.0 — 2026-10-02

Fixes for skills missing from / miscounted in the report (user request 2026-10-02, after the
2026-10-01 run omitted skill-usage-report and skill-library-audit — 1 load each, rank 43/44,
cut by the top-15 limit).

- Rows: top N, then a `— used in last 7 days —` divider and every lower-ranked skill loaded in
  the last 7 days (true rank numbers). New flag `--recent-days N` (default 7; 0 = off); the
  caption notes the extra rows.
- Uses = successful SKILL.md loads only: `skill_view` calls with `file_path` (supporting-file
  reads), results other than `success: true` (failed / `Skipped: …`) and dedup stubs are
  skipped. Results are matched by `tool_call_id` (older rows have no `tool_name`); non-JSON
  results count only when they are compaction placeholders or JSON truncated after
  `"success": true`.
- Rows keyed by the skill's directory name: frontmatter names map to their directory, and a
  category prefix is dropped only for real categories (`productivity:task-tracker` →
  `task-tracker`; `plugin:skill` kept). Frontmatter `name:` read in full (spaces, quotes).
  "installed" = SKILL.md directories. Ties at the top-N cut broken by most recent use.
- stderr confirmation now reports skipped file reads / failed / repeat views.
- Docs: SKILL.md (Uses definition, rows, flag, `.usage.json` pitfall), report-internals.md
  (data rules, caveats, known-good values recomputed: 513 → 356 loads on the same data).
- Independent review (2 offline reviewers) caught non-JSON failures being counted as loads and
  a frontmatter-alias row split; both fixed before deploy.

## 1.0.0 — 2026-09-29

Initial release — ad-hoc only (no cron, per user decision 2026-09-29).

- `scripts/skill_usage_report.py` — self-posting "most-used skills" report:
  skill_view load counts + last-used dates from `state.db`; last-updated /
  version / changelog note from the skills tree; rendered via the shared
  `render_tables.py`; posted to the current Discord thread as one
  caption + PNG multipart POST (status-checked, one clamped 429 retry,
  PNG deleted only after a confirmed 2xx).
- no_agent-style contract: `[SILENT]` first line, `os._exit(0/1)`, stdlib
  `fcntl.flock` run guard, noisy failures (traceback → home channel),
  token from `~/.hermes/.env`, target precedence `--to` → `--thread-id` →
  `SKILL_USAGE_TARGET` → `HERMES_SESSION_KEY` → thread/chat env.
- Built from plan `.hermes/plans/2026-09-29_212947-skill-usage-report-skill.md`.
