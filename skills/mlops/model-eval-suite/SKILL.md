---
name: model-eval-suite
description: "Evaluate and compare LLM models for the Hermes Agent on AGENTIC tasks drawn from real top-skill usage. Runs the actual `hermes -z` loop in isolation, scores reliability + objective verifiers, judges each candidate PAIRWISE vs the incumbent (MiniMax-M2.7), and adds a consistency mode (repeat N x -> pass^k + latency variance) plus a hard / error-recovery task pack. Pluggable judge: MiniMax-M3 by default, Claude as a neutral judge on request. Can render results to Discord tables."
version: 2.1.0
author: ham
metadata:
  hermes:
    tags: [mlops, evaluation, benchmark, model-comparison, agent-eval]
---

# Model Evaluation Suite for Hermes Agent (v2.1)

Evaluates models the way the agent is **actually used**: multi-step tool loops (terminal /
code / file / skills), not single-turn text. Each candidate runs through the real `hermes -z`
agent loop in an isolated sandbox, scored on reliability + deterministic verifiers, and judged
**pairwise against the incumbent `MiniMax-M2.7`**. A **consistency mode** repeats each task N×
to expose flakiness and latency variance, and a **hard / error-recovery task pack** raises the
ceiling when easy tasks tie. See `DESIGN.md` for the rationale and `CLAUDE.md` for internals.

## Quick Start

```bash
cd ~/agent-eval/suite          # working copy (mirrored in this skill's scripts/)

# A) Quality eval — pairwise vs incumbent, autonomous MiniMax-M3 judge:
python3 eval_suite.py

# B) Neutral judging by Claude (two steps):
python3 eval_suite.py --judge claude-external          # runs, writes judge_queue.jsonl, stops
#   ... Claude reads judge_queue.jsonl, writes judge_verdicts.jsonl ...
python3 eval_suite.py --judge claude-external --resume-judge   # scores -> report.json

# C) Consistency / hard-pack eval (repeat N x, pass^k + latency variance, no judge):
python3 eval_suite.py --tasks-file tasks_hard.json --repeat 3

# Pick models / tasks explicitly (models need not be in the discovery cache):
python3 eval_suite.py --models MiniMax-M3,deepseek-v4-pro --tasks code-fix-script,sg-knowledge
```

Outputs in `~/agent-eval/`: `report.json` (pairwise + objective), `consistency.json`,
`judge_queue.jsonl` / `judge_verdicts.jsonl` / `judge_key.json`, and
`trajectories/<model>/<task>.json`.

## Architecture

```
scripts/
├── eval_suite.py     — orchestrator: discover → run → verify → (judge | consistency) → report
├── runner.py         — drives `hermes -z` in an isolated sandbox HERMES_HOME (see Safety)
├── verifiers.py      — deterministic objective verifiers + reliability metrics
├── judge.py          — pluggable pairwise judge (minimax-m3 | claude-external)
├── tasks.json        — 15 usage-grounded tasks (the quality eval)
├── tasks_hard.json   — 8 hard / error-recovery tasks (the discrimination eval)
├── render_discord.py — report.json → Discord table images via discord-table-format
└── send_report.py    — render all tables + post a full report to Discord #alerts
```

### How a run works
For each `(model × task)`: build a throwaway sandbox `HERMES_HOME`, write the task's fixtures
into a disposable workdir, then invoke:

```
HERMES_HOME=<sandbox> HERMES_SESSION_ID=<id> HERMES_MAX_ITERATIONS=<n> \
  hermes -z "<task prompt>" -m <model> --provider <p> -t "<restricted toolset>"
```

The final answer is read from stdout; the full tool-call trajectory is read back from the
sandbox `state.db`. Sandbox + workdir are **auto-deleted** after capture (set
`EVAL_KEEP_ARTIFACTS=1` to retain for debugging).

## Two evaluation modes

**1. Quality (pairwise).** `tasks.json`, run once each, judged head-to-head vs `MiniMax-M2.7`,
A/B anonymized + order-randomized → usage-weighted **win-rate** (`win + 0.5·tie`).

**2. Consistency (`--repeat N`).** Any task file, run N× each. Fully automated (no judge).
Reports **`pass^k`** (passed *all* N reps, usage-weighted), mean pass-rate, **latency p50 + σ**,
and flags **flaky** tasks (passed some-but-not-all reps). This is what separates models when the
quality eval ties — use it with `tasks_hard.json`.

## Scoring

- **Objective verifiers** (`verifiers.py`): `numeric`, `contains` (comma-normalized),
  `ordered_values`, `regex`, `word_limit`, `markdown_table`, `json_array_check`, `json_fields`,
  `skill_loaded`. Return pass/fail per task.
- **Reliability:** timeout / api_error / refusal / malformed-tool-call rates; trajectory length;
  latency p50/p95/σ.
- **Pairwise quality:** judge verdict win/tie/loss vs incumbent (quality mode only).

## Pluggable judge

- **`--judge minimax-m3` (default, autonomous):** `MiniMax-M3` judges via API on-host. If a
  candidate equals the judge model, that pairing falls back to `mimo-v2.5-pro`.
- **`--judge claude-external` (neutral, on instruction):** writes `judge_queue.jsonl` (A/B
  packets, no mapping) + `judge_key.json` and stops. **Claude** reads the queue and writes
  `judge_verdicts.jsonl` (`{pair_id, winner:A|B|tie, confidence, rationale}`); rerun with
  `--resume-judge` to score. Same rubric/schema either way.

## Models

`discover_models()` reads `~/.hermes/provider_models_cache.json` and includes any model whose
provider has an API key in `.env`. Providers currently wired: **minimax, xiaomi, deepseek**.
`--models` accepts ids **not in the cache** (e.g. `MiniMax-M3`, `deepseek-v4-flash/pro`) —
they're resolved by provider name-prefix (`runner.resolve_model`). `MiniMax-M2.7` is the pinned
incumbent baseline.

### Add a model
Drop the provider key in `~/.hermes/.env`; it's auto-discovered. Out-of-cache ids work via
`--models` if the name prefix maps to a known provider.

### Add a task
Append to `tasks.json` (quality) or `tasks_hard.json` (discrimination):
```json
{"id":"my-task","skill":"<bucket>","weight":10,"prompt":"...","toolsets":["terminal","code_execution","file"],
 "fixtures":{"data.csv":"..."},"verify":{"type":"numeric","expected":42,"tol":0},"timeout_s":180,"max_iter":12}
```

## Safety (local, restricted)
1. **Restricted toolset** — only read-only/compute toolsets per task (`terminal, code_execution,
   file, web, session_search, skills, vision, memory, todo, clarify`). Side-effecting toolsets
   (`cronjob, discord, messaging, spotify, homeassistant, image_gen, …`) are never enabled.
2. **Isolated sandbox `HERMES_HOME`** — copied config, trimmed `.env` (inference keys only),
   symlinked skills, fresh `state.db`. The live `~/.hermes/state.db` is never touched.
3. **Disposable workdir**, auto-deleted; env subset to inference creds.

## Reporting to Discord
```bash
python3 render_discord.py ~/agent-eval/report.json   # just render tables to PNGs
python3 send_report.py                               # render + POST full report to #alerts
```
`send_report.py` renders the pairwise / objective / consistency tables via the
`discord-table-format` skill and posts header+image pairs to channel `1499972245437550602`
(#alerts) using the Discord API + `DISCORD_BOT_TOKEN`.

## Reference: latest results (June 2026)
Incumbent `MiniMax-M2.7` vs MiniMax-M3, mimo-v2.5, mimo-v2.5-pro, deepseek-v4-flash/pro:
- No candidate decisively beat the incumbent on quality; **deepseek-v4-pro** reached parity (0.50).
- Hard-pack consistency: all `pass^k = 1.00` except **mimo-v2.5** (flaked `multi-file-join`, 0.88).
- Latency tails: **deepseek-v4-flash** fastest/steadiest; **deepseek-v4-pro** (197s) and
  **MiniMax-M3** (147s) have bad tails; incumbent slowest typical but bounded.
- All models: zero timeouts / API errors / refusals / malformed tool calls.

## File Layout
```
~/agent-eval/
├── suite/              # working copy of scripts/
├── report.json         # pairwise + objective (quality eval)
├── consistency.json    # pass^k + latency variance (consistency eval)
├── judge_queue.jsonl / judge_verdicts.jsonl / judge_key.json
├── trajectories/<model>/<task>.json
└── homes/, work/       # disposable per-run scratch (auto-deleted)
```
