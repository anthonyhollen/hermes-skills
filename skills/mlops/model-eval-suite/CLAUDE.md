# CLAUDE.md — model-eval-suite (developer guide)

Guidance for Claude Code / agents working **on** this skill's code. End-user "how to run it"
docs live in `SKILL.md`; the design rationale is in `DESIGN.md`. This file is the internals,
invariants, and gotchas.

## What this is
A benchmark that evaluates LLM models for the **Hermes Agent** by driving the real `hermes -z`
headless loop (NOT a reimplemented tool loop) once per `(model × task)`, in an isolated sandbox,
then scoring objective verifiers + reliability and judging pairwise vs the incumbent
`MiniMax-M2.7`. A consistency mode repeats each task N× for `pass^k` + latency variance.

## Where the code lives
- **Canonical:** `~/.hermes/skills/mlops/model-eval-suite/scripts/`
- **Working mirror:** `~/agent-eval/suite/` (where runs are launched from)
- When you edit a script, deploy to **both** locations (the runs import from whichever dir the
  entrypoint sits in; `eval_suite.py` does `sys.path.insert(0, <its own dir>)`).
- Run outputs go to `~/agent-eval/` (`runner.EVAL_ROOT`, override with `EVAL_ROOT`).

## Module map
- `runner.py` — isolation + execution. `discover_models`, `resolve_model`, `build_sandbox`,
  `run_task` (returns `RunResult`), `_read_trajectory`.
- `verifiers.py` — `verify(task, run) -> (bool, reason)` dispatched on `task["verify"]["type"]`;
  `reliability(run) -> dict`.
- `judge.py` — `build_pairs`, `judge_minimax`, `write_queue`/`load_verdicts`, `score`. Rubric +
  A/B anonymization live here.
- `eval_suite.py` — CLI + orchestration: `run_all` (quality), `run_consistency` (repeat mode),
  `objective_report`, `print_summary`, `main`.
- `tasks.json` (15 usage tasks) / `tasks_hard.json` (8 hard + error-recovery tasks).
- `render_discord.py` / `send_report.py` — Discord table rendering + posting.

## How execution works (the core)
`run_task` builds a fresh sandbox `HERMES_HOME`, writes fixtures into a disposable workdir, and
subprocess-runs `hermes -z "<prompt>" -m <model> --provider <p> -t "<toolsets>"` with a subset
env. `hermes -z` prints only the final answer to stdout; the **trajectory** is recovered by
reading the sandbox `state.db` `sessions`/`messages` tables (`_read_trajectory`).

## Invariants — do not break these
- **No `#` (or shell-special chars) in `task_id`.** It flows into `HERMES_SESSION_ID` and the
  sandbox path and **silently breaks trajectory capture** (empty `state.db`, `tool_calls=0`,
  answer still captured so it looks "fine"). Consistency reps use `_rep{n}`, never `#r{n}`.
- **Live state is sacrosanct.** Everything runs under a sandbox `HERMES_HOME`; never point a run
  at `~/.hermes`. `build_sandbox` trims `.env` to inference creds only (no Discord/Google tokens).
- **Incumbent is `MiniMax-M2.7`**, pinned in `runner.INCUMBENT`; excluded from candidates and
  used as the pairwise anchor.
- **Restricted toolsets only** — never enable side-effecting toolsets (`cronjob`, `discord`,
  `messaging`, `spotify`, `homeassistant`, image/video/tts). Per-task `toolsets` list enforces this.
- **Judge neutrality:** when a candidate equals the judge model (`minimax-m3` backend), fall back
  to `mimo-v2.5-pro` (`judge.judge_minimax`). The `claude-external` path keeps A/B mapping only in
  `judge_key.json`, never in `judge_queue.jsonl`.

## Gotchas learned the hard way
- **Sandbox bloat:** Hermes regenerates `models_dev_cache.json` (~2 MB) inside every sandbox
  `HERMES_HOME`. `run_task` deletes the sandbox + workdir after capture; `EVAL_KEEP_ARTIFACTS=1`
  retains them (debugging only — 60 runs ≈ 140 MB).
- **Stale discovery cache:** `provider_models_cache.json` can list models the API rejects
  (e.g. `mimo-v2-flash` → HTTP 400). The suite records this as `api_error` + a loss; it does not
  crash. `MiniMax-M3` and `deepseek-*` are *missing* from the cache but *are* served — use
  `--models` (resolved via `resolve_model` name-prefix).
- **Verifier brittleness:** `contains`/`numeric` must tolerate formatting. `contains` normalizes
  thousands separators (`4,000`→`4000`). When adding a `contains` check, never require a value
  that doesn't actually appear in the data (an earlier bug required a non-existent `3000`).
- **`run_oneshot` swallows stdout/stderr** except the final answer — debug via the trajectory in
  the sandbox `state.db`, or run with `EVAL_KEEP_ARTIFACTS=1` and inspect.
- **Per-message `token_count` is 0** in oneshot sessions; cost is treated as secondary (latency is
  the reliable efficiency signal).

## Testing changes
1. Smallest smoke: `EVAL_KEEP_ARTIFACTS=1 python3 -c` calling `runner.run_task(...)` on one task,
   then read the kept sandbox `state.db` to confirm the trajectory captured (msgs > 0).
2. Verifier change: run 5–6 mixed tasks on one fast model (`mimo-v2.5`) and eyeball pass/why.
3. Full quality run ≈ 6 models × 15 tasks; consistency run ≈ 6 × 8 × 3. Use `--concurrency 3`
   (network-bound; the Pi handles 3 comfortably).
4. Re-derive scores from saved `trajectories/` without re-running by reconstructing `RunResult`s
   and calling `objective_report` / `judge.score` (see how `report.json` was regenerated).

## Extending
- **New provider:** add `PROVIDER_KEY[<prov>] = "<ENV_KEY>"`, append its `*_API_KEY`/`*_BASE_URL`
  to `ENV_KEYS`, and add a name-prefix branch in `resolve_model`. (DeepSeek was added exactly this
  way: `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`, prefix `deepseek`.)
- **New verifier type:** add a branch in `verifiers.verify` returning `(bool, reason)`; keep it
  format-tolerant and deterministic.
- **New task:** prefer self-contained fixtures (no secrets, reproducible). Give it a `weight`
  reflecting real usage frequency so the weighted aggregates stay meaningful.

## Reporting
`send_report.py` posts a full report (pairwise + objective + consistency tables) to Discord
**#alerts** (`1499972245437550602`) via the Discord API and `DISCORD_BOT_TOKEN`, reusing the
`discord-table-format` skill's `render_tables.py`. Sending is an outward/public action — only do
it on explicit request.
