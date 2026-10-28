# model-eval-suite v2 — Design Spec

> Status: **PROPOSED — awaiting approval.** No eval code written yet. This document is the
> agreed design; `scripts/eval_suite.py` will be rewritten to match it after sign-off.

## 1. Why v2

v1 measured the wrong thing. Analysis of real usage on this host:

- **Workload is agentic, not text-completion.** Across 199 full transcripts the dominant
  tool calls are `terminal` (2673), `read_file` (982), `patch` (870), `execute_code` (704),
  `search_files` (382), `cronjob` (286), `skill_view` (258), `send_message` (198). Every v1
  test is a single-turn text prompt with **no tools** — it never exercises the multi-step
  tool loop the agent actually runs.
- **Production model is `MiniMax-M2.7`** (1263 sessions). v1 treats it as one peer of four
  with no baseline role.
- **v1 tasks are stale / synthetic.** They reference skills now in `.archive/`
  (sg-carpark-availability, sg-commute-*) and a "transport mode" task that maps to no skill.
- **Scoring can't discriminate.** v1's own June results: all 4 models scored 12/14. The
  14-point regex auto-score does not separate competent models; the real signal needs manual
  review.

v2 fixes all four: usage-grounded agentic tasks, the incumbent as a pinned baseline,
pairwise judging that produces a decisive verdict, and reliability metrics that matter for an
always-on cron agent.

## 2. Goals / non-goals

**Goals**
- Evaluate candidate models on tasks drawn from *actual* top-skill workflows, weighted by
  real usage frequency.
- Exercise the real agentic loop (tool use, multi-step) via the existing Hermes runner.
- Produce a decisive, low-noise verdict: **win-rate vs the incumbent MiniMax-M2.7**.
- Prioritize **reliability** (timeouts, API errors, refusals, malformed tool calls) and
  **task quality** — per the eval's stated optimization targets.
- Pluggable judge: autonomous by default, neutral (Claude) on demand.

**Non-goals**
- Generic academic benchmarks. Not measuring MMLU-style knowledge.
- Latency/cost as primary verdict drivers (still reported as secondary metrics).
- Re-implementing a tool loop — we drive the shipped `hermes -z` runner.

## 3. Architecture

```
eval_suite.py
├── discover_models()      — read provider_models_cache.json, filter to providers with creds
├── load_tasks()           — tasks.jsonl (mined from transcripts, see §6)
├── run_task(model, task)  — invoke `hermes -z` headless with restricted toolset (§4), in a
│                            disposable workdir; capture trajectory, output, latency, tokens,
│                            errors, tool-call validity
├── verify(task, run)      — deterministic outcome verifier → objective pass/fail (§5.1)
├── reliability(run)       — timeout / error / refusal / malformed-tool-call flags (§5.2)
├── judge_pairwise(...)    — candidate vs incumbent, pluggable backend (§5.3 / §7)
└── report()               — report.json + Discord table + trajectories/ (§8)
```

### Engine: driving the real agent

Each `(model × task)` runs through the shipped headless runner:

```
HERMES_INFERENCE_MODEL=<model> hermes -z \
    --model <model> --provider <provider> \
    --toolsets "<restricted set>" \
    -- "<task prompt>"
```

`run_oneshot()` (hermes_cli/oneshot.py) auto-sets `HERMES_YOLO_MODE=1` (approvals bypassed),
returns the agent's final text to stdout, and logs the trajectory to a session file. We read
that session file for the full tool-call trajectory used by the verifier and judge.

Working directory = a **fresh disposable dir per run** (`~/agent-eval/work/<model>/<task>/`)
seeded with read-only fixtures (§4), so `terminal`/`file`/`code_execution` writes can't touch
real state.

## 4. Safety model — restricted toolset, local exec

User chose **local execution with a restricted toolset**. Safety rests on three layers:

1. **Toolset allow-list** — only enable read-only / compute toolsets; never enable
   side-effecting ones.

   | Enabled (safe) | Excluded (side-effecting) |
   |---|---|
   | terminal, code_execution, file, web, session_search, skills, vision, memory, todo, clarify | cronjob, discord, discord_admin, messaging, spotify, homeassistant, image_gen, video_gen, tts, delegation, feishu_doc, feishu_drive, kanban, browser* |

   The model literally cannot `send_message`, schedule a `cronjob`, or hit Spotify/HA — those
   toolsets are absent. *(browser excluded by default; opt in per-task if a task needs it.)*

2. **Disposable workdir + read-only fixtures.** Each run executes in a throwaway dir. Tasks
   that need real data (e.g. token-usage querying `state.db`) get a **read-only copy** of the
   fixture placed in the workdir; the original is never exposed. CWD is set so AGENTS.md /
   memory resolve to an eval-scoped sandbox profile, not the live one.

3. **Env subsetting.** The subprocess gets only the env vars needed for inference
   (`MINIMAX_API_KEY`, `XIAOMI_API_KEY`, base URLs). Discord token, Google tokens, etc. are
   stripped so even an enabled tool couldn't reach a live external account.

> Residual risk (accepted): `terminal`/`file` can still write/delete *within the disposable
> workdir*. That is the intended blast radius. Anything destructive outside it would require a
> path traversal the workdir + env subsetting are designed to prevent.

## 5. Scoring

Two independent layers. Reliability + objective are automated; quality is the pairwise judge.

### 5.1 Objective verifiers (per task, deterministic)
Each task in `tasks.jsonl` carries a `verify` spec. Examples by task type:
- **code/terminal task** — the produced script/patch runs and its output matches an expected
  value or regex; exit code 0.
- **structured-extraction task** (cc-bill-review, data extraction) — output parses as JSON and
  matches expected fields/values.
- **skill-routing task** — trajectory shows the *correct* `skill_view`/skill invocation for
  the task (e.g. a token-usage request actually loads the token-usage skill).
- **report task** (morning-brief, token-usage) — output contains the required sections /
  fields and no placeholder/error text.

Verifier returns `pass | fail | n/a` plus a short reason. This is the **task-quality
objective floor**.

### 5.2 Reliability metrics (per model, aggregated)
- `timeout_rate` — runs exceeding the per-task wall-clock budget.
- `api_error_rate` — provider 4xx/5xx or empty-content responses.
- `refusal_rate` — model declines / "I can't" on a benign task (keyword + judge check).
- `malformed_tool_call_rate` — tool calls with invalid JSON args or nonexistent tool names.
- `trajectory_length` — median tool calls to completion (efficiency; runaway loops flagged).

### 5.3 Pairwise quality (vs incumbent)
For each task, the judge sees the task and **two anonymized trajectories+answers** (A/B,
order randomized per task to kill position bias): the candidate and **MiniMax-M2.7**. It
returns `{winner: A|B|tie, confidence, rationale}` against a fixed rubric (correctness,
instruction-following, completeness, tool-use efficiency). Aggregated to a **usage-weighted
win-rate vs incumbent** per candidate.

Judge backends are interchangeable (§7) and use the identical rubric + schema, so scores are
comparable regardless of who judged.

## 6. Task corpus — usage-grounded

`tasks.jsonl`, one object per task:
```json
{
  "id": "token-usage-weekly",
  "skill": "token-usage",
  "weight": 35,
  "prompt": "<real-style user request>",
  "toolsets": ["terminal", "code_execution", "file", "skills"],
  "fixtures": ["state.db:ro"],
  "verify": {"type": "report_sections", "must_include": ["total tokens", "by model", "by day"]},
  "timeout_s": 180
}
```

**Sourcing.** Mined from the 199 JSONL transcripts + 988 real user prompts, bucketed by the
top skills and weighted by real invocation counts:

| Bucket | Real count | Sample task |
|---|---|---|
| morning-brief / sg-commute | 46 / 36 | assemble brief from live data sources |
| token-usage | 35 | query state.db → weekly token report |
| email-gmail-triage | 12 | triage an inbox sample → structured output |
| cc-bill-review | 6 | parse statement text → categorized JSON |
| food-recommender / maps | 5 / 7 | recommend near a location from order history |
| coding loop (terminal+patch+execute_code) | dominant | fix a broken script, verify it runs |

Target **15–25 tasks**. `weight` (frequency-derived) drives the weighted aggregate so the
verdict reflects the real workload mix. Prompts are paraphrased from real ones (no secrets).

## 7. Pluggable judge

A `Judge` interface with two backends, selected by `--judge`:

### `--judge minimax-m3` (DEFAULT — autonomous)
When the **live Hermes agent** invokes this skill, judging runs fully autonomously via the
MiniMax API on this host. `MiniMax-M3` is the judge; on any run where M3 is itself a
candidate, it is **held out** (its own pairings are deferred to the fallback judge
`mimo-v2.5-pro`, or marked `unjudged` if the user prefers strict neutrality).

### `--judge claude-external` (on user instruction — neutral)
No Anthropic key exists on this host, so neutral judging happens **out-of-band through
Claude**:
1. Suite runs all candidate-vs-incumbent pairs, then writes `judge_queue.jsonl` — one packet
   per pairing: `{task_id, rubric, A: {trajectory, answer}, B: {trajectory, answer}}`,
   A/B anonymized + order-randomized.
2. The suite **stops** and prints the queue path.
3. Claude (Claude Code) reads `judge_queue.jsonl`, applies the rubric, writes
   `judge_verdicts.jsonl`: `{task_id, winner, confidence, rationale}`.
4. Re-running with `--resume-judge` imports the verdicts and produces the final report.

This makes Claude a drop-in neutral judge whenever instructed, without changing scoring or
schema. A future `--judge anthropic` backend can call the API directly if a key is ever added.

## 8. Output
- `report.json` — per model: weighted win-rate vs incumbent, reliability table, secondary
  latency/$ metrics, per-task breakdown.
- `trajectories/<model>/<task>.json` — full trajectory for manual spot-checks.
- **Discord table** via the `discord-table-format` skill: a "vs incumbent" win-rate table +
  a reliability table, rendered to images and sent as header + MEDIA pairs.

## 9. Model discovery
`discover_models()` reads `~/.hermes/provider_models_cache.json`, keeps only providers whose
API key is present in env (currently `minimax`, `xiaomi`), and yields
`{name, provider, model_id}`. `MiniMax-M2.7` is always pinned as the incumbent baseline and
excluded from the candidate set (it's the comparison anchor). `--models a,b,c` subsets at
runtime. Currently discoverable: MiniMax `M2.7/M2.5/M2.1/M2`, Xiaomi
`mimo-v2.5-pro/v2.5/v2-pro/v2-omni/v2-flash`.

## 10. CLI
```
python3 scripts/eval_suite.py \
    [--models m1,m2]            # default: all discovered candidates
    [--tasks t1,t2]             # default: all in tasks.jsonl
    [--judge minimax-m3|claude-external]   # default: minimax-m3
    [--resume-judge]            # import judge_verdicts.jsonl and finish report
    [--concurrency N]           # parallel runs (default 2)
    [--out ~/agent-eval]
```

## 11. Carried-over bug fixes from v1
- MiniMax caller used `max_tokens=1024` (truncates; SKILL doc says 4096) — fixed.
- MiniMax text extraction read only the first text block — use all text blocks with a
  stringify fallback.
- Delete the dead synthetic TESTS list and regex `auto_score()` (superseded by verifiers).

## 12. Build plan (after approval)
1. `discover_models()` + restricted-toolset runner around `hermes -z`, disposable workdir,
   env subsetting. **Smoke test:** 1 model × 1 trivial task, confirm isolation + capture.
2. Mine transcripts → draft `tasks.jsonl` (15–25 tasks) with verifiers + weights. Review.
3. Reliability metrics + objective verifiers.
4. Pluggable judge (minimax-m3 backend + claude-external queue/resume).
5. Report + Discord rendering. Update `SKILL.md` to v2.
6. Full run: all candidates × all tasks, judge = minimax-m3; then a neutral re-judge pass
   via claude-external for the final verdict.

## 13. Open questions
- M3-as-candidate hold-out: fall back to `mimo-v2.5-pro` judge, or mark `unjudged`? (default:
  fallback judge.)
- Fixtures for data tasks (state.db, a sample inbox, a sample statement): synthesize scrubbed
  fixtures vs read-only copies of real data? (default: scrubbed synthetic fixtures to avoid
  any secret exposure in trajectories/judge packets.)
