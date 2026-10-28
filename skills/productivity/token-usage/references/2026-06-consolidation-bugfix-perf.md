# 2026-06 — Script Consolidation, Bugfixes & Performance Review

Authoritative record of the June 2026 review session. Covers the two-copy
divergence that was the root cause of several regressions, the 10 bugs fixed,
the cron-config fix, the shared-renderer hardening, and the measured
performance review. Read this before touching `token_report_no_agent.py`,
`render_tables.py`, or the cron job.

---

## 0. The root cause — two divergent copies of the script

`token_report_no_agent.py` existed in **two places that had drifted apart**:

| Path | Role |
|---|---|
| `/home/amh/.hermes/scripts/token_report_no_agent.py` | **The copy the cron actually runs.** |
| `/home/amh/.hermes/skills/productivity/token-usage/scripts/token_report_no_agent.py` | The "source of truth" the docs point at. |

**Why the cron runs the `~/.hermes/scripts/` copy:** the job is `no_agent: true`
with `profile: null`, so the scheduler resolves `script: token_report_no_agent.py`
under `<profile-home>/scripts/` → `~/.hermes/scripts/`. (When a stray `profile: "pro"`
was set it looked under `~/.hermes/profiles/pro/scripts/` and failed with
`Script not found` — see `cron-job-configuration.md`.)

The skill-dir copy had been "refactored" to delegate rendering to
`render_tables.py` but **regressed** in the process (it reintroduced the `%%`
strftime bug, dropped `fmt()` auto-scaling, dropped the filelock, and added a
local-vs-UTC date-label bug). Meanwhile the cron-executed copy was healthier but
still carried the MTD column bug and an unguarded MTD cost format.

**Resolution:** one canonical script is now deployed **byte-identical to both
paths**. Keep them in sync — if you edit one, copy it to the other. The
canonical script's docstring states this invariant.

### Scheduler interpreter (verified)

The scheduler runs no_agent scripts via `sys.executable`, which is the **gateway
venv**: `/home/amh/.hermes/hermes-agent/venv/bin/python` (NOT `/usr/bin/python3`).
Verified under that venv:

- `filelock` → OK (it is a venv dependency).
- `tabulate` → OK (venv).
- `PIL` → **fails** without `sys.path.insert(0, "/usr/lib/python3/dist-packages")`.
- `render_tables` → OK (it performs that dist-packages insert itself before
  importing PIL), so the canonical script delegates rendering and needs no
  direct PIL import.
- `site.ENABLE_USER_SITE` is `False` in the venv, so `~/.local/...` packages are
  NOT importable — anything the script needs must be in the venv or dist-packages.

---

## 1. Bugs fixed (10)

| # | Bug | Fix |
|---|-----|-----|
| 1 | Cron job in error state (stray `pro` profile → `Script not found`) | profile cleared to `null`; verified live config clean |
| 2 | `deliver` pointed at the **playground** channel `1501742338639401071` | `hermes cron edit cbb893c8a65d --deliver discord:1499972245437550602` |
| 3 | `%%Y-%%m-%%d` in the daily `GROUP BY` collapsed all 7 days into ONE bogus row | single `%` (`strftime('%Y-%m-%d', started_at, 'unixepoch', '+8 hours')`) |
| 4 | `fmt()` showed sub-1M values as `0.0M` | restored K auto-scaling (`… else f"{v/1e3:.1f}K"`) |
| 5 | MTD/daily cost crashed with `TypeError` when `SUM(estimated_cost_usd)` was NULL | `money()` helper guards `None` |
| 6 | No filelock → scheduler 60s re-ticks could double-fire | `filelock.FileLock(..., timeout=0)` acquired right after `[SILENT]` |
| 7 | MTD rendered 7 cols with a `TOTAL` label and a spurious `Count` header (count under the wrong header) | MTD → 6 cols, count under `Sessions`, no `TOTAL` |
| 8 | Daily labels used local time (`datetime.fromtimestamp`) over a UTC bucket → off-by-one / merged days | group by the SGT day string and use it directly as the label |
| 9 | `render_tables.render_table` silently misaligned a header/row width mismatch (tabulate pads/shifts → wrong-but-plausible image) | raises `ValueError` on `len(row) != len(headers)` |
| 10 | Empty data → `render_table` returns `None` → `curl file=@None` + cleanup `TypeError` | skip `None` image paths in the send and cleanup loops |

Bugs 3, 4, 6, 8 existed only in the (non-executed) skill-dir copy; consolidating
to the canonical script removed them. Bugs 5, 7, 10 were fixed in the canonical
script. Bug 9 is in the shared renderer and affects **all** its callers
(token-usage, task-tracker, model-eval-suite, sg-commute).

Also folded in: NULL models filtered (`WHERE model IS NOT NULL`), SGT-consistent
MTD window via a Python timestamp (not SQL `date()`), correct `~/.hermes/.env`
token path, and the documented `## title` / `### section` Discord format.

---

## 2. Performance review (measured, June 2026)

Ground-truth from read-only probes against the live `state.db`:

- `state.db`: **439.6 MB**, **not bloated** (freelist 0.4% → VACUUM reclaims only
  ~1.6 MB; not worth it).
- Rows: `sessions` 1,150; `messages` 24,549.
- Big columns: `messages.content` 55 MB, `sessions.system_prompt` 18 MB,
  `messages.tool_calls` 9 MB.

### Cold vs warm cache is the dominant factor

On the Pi's storage the **first** touch of `messages` is disk-bound. The same
introspection scan measured **~10.5 s cold** vs **~0.85 s warm** (pages cached in
RAM). "Performance" here = how much of the 439 MB each query is forced to read
cold, not CPU.

### The report pipeline is already fast — no action

| Query | Plan | Time (warm) |
|---|---|---|
| daily | `SEARCH … USING idx_sessions_started` | 5.3 ms |
| MTD | `SEARCH … USING idx_sessions_started` | 0.06 ms |
| all-time | `SCAN sessions` + temp b-trees | 56 ms |

Total report compute ≈ 0.5 s (61 ms queries + 138 ms render import + ~360 ms for
3 PNGs); wall-clock is dominated by the deliberate `4 × sleep(0.5) = 2 s` Discord
rate-limit. `render_tables.py` is already the optimized retina path (don't
re-add the old 6000×2000 + LANCZOS upscale). `no_agent` mode burns zero LLM
tokens. Nothing to optimize for a weekly job.

### Finding #1 — APPLIED: prefilter the skill-introspection scan in SQL

The introspection patterns (`SKILL.md` Query 1, `state-db-skill-introspection.md`
Source 1) fetched **every** non-null `tool_calls` blob and `json.loads`-ed each
in Python. Pushing a coarse substring filter into SQL is exact for the count and
far cheaper:

| Approach | Warm | Rows into Python |
|---|---|---|
| `WHERE tool_calls IS NOT NULL`, parse all | 854 ms | 9,766 |
| `WHERE tool_calls LIKE '%skill_view%'`, parse matches | **60 ms** (~14×) | 352 |

Same result (372 `skill_view` calls). False positives (substring in an arg value)
are gated by the inner `name == 'skill_view'` check; false negatives are
impossible. **This is now the documented query.**

### Finding #2 — documented, NOT applied: all-time covering index

The all-time query is the only report query that does a full `SCAN sessions`
(56 ms warm). Because each `sessions` row carries the 18 MB of `system_prompt`, a
*cold* scan drags overflow pages. A covering index on
`sessions(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd)`
would make the aggregate index-only. **Not applied** — at 1,150 rows on a weekly
job it is not worth the per-upsert write cost on the shared DB. Revisit if
`sessions` grows large or this query starts running interactively.

---

## 3. Verification performed

- Read-only DB probes (`file:…?mode=ro`) for all timings and query plans.
- Read-only harness confirming the daily table now has 7 day-rows (was 1), MTD is
  6 cols = 6 headers, and the #9 guard raises on mismatch / renders on match.
- Integration dry-run: ran the real canonical script under the gateway venv with
  a stub `curl` on `PATH` → exit 0, stdout exactly `[SILENT]`, no traceback, 4
  sends attempted, temp PNGs cleaned up.
- `py_compile` of both script copies and `render_tables.py` under the venv.
- A **live scheduled/triggered send was deliberately NOT run** (would post to the
  production work channel). Next scheduled fire: Sundays 11:00 SGT.

## 4. Backups

Originals from this session: `~/.hermes/_backups_review_20260605_234654/`
(`EXEC__…`, `SKILL__…`, `render_tables.py`, `jobs.json`, `SKILL.md`,
`state-db-skill-introspection.md`).
