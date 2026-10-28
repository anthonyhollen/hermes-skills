# Skill Usage Introspection from `state.db` + `jobs.json`

**When to use:** the user asks for a skill usage leaderboard, "what skills do I use most", "what's installed but never used", "what's costing me tokens in the system prompt", or any self-introspection of their skill library.

**Added:** June 2026, after ham asked for skill-usage analytics as a one-off leaderboard.

## The two data sources

Hermes stores skill usage in **two places that must be combined**:

| Source | File | Records |
|--------|------|---------|
| Interactive `skill_view` tool calls | `~/.hermes/state.db`, table `messages`, column `tool_calls` | One row per LLM-driven `skill_view(name="...")` call inside a session |
| Cron job completions | `~/.hermes/cron/jobs.json`, each job's `skills[]` + `repeat.completed` | One count per completed run of a cron that has the skill in its `skills` array |

Interactive alone misses cron loads. Cron alone misses on-demand usage. **Always combine.**

## Full introspection pipeline (worked example)

The script below was used in June 2026 to answer ham's three-question sequence:
1. Top skills by usage
2. Same but include cron-driven
3. What skills are enabled but never used

```python
#!/usr/bin/env python3
"""Skill usage introspection — runs as a one-off via execute_code or terminal."""
import sqlite3, json, os, glob, re
from collections import Counter, defaultdict

HERMES_HOME = '/home/amh/.hermes'  # or $HERMES_HOME env var

# ── Source 1: interactive skill_view calls ──
con = sqlite3.connect(f'{HERMES_HOME}/state.db')
cur = con.cursor()
interactive = Counter()
# Prefilter in SQL — only blobs mentioning skill_view can hold a skill_view
# call, so this is exact while skipping the ~95% of rows that don't. Measured
# on the 24.5k-row messages table (warm cache, June 2026): the old
# `WHERE tool_calls IS NOT NULL` form parsed 9,766 rows in ~854 ms; this LIKE
# form parses 352 rows in ~60 ms (~14x), and avoids the ~10 s cold-cache scan.
for row in cur.execute("SELECT tool_calls FROM messages WHERE tool_calls LIKE '%skill_view%'"):
    try:
        tcs = json.loads(row[0])
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    fn = tc.get('function', {}) or {}
                    if fn.get('name') == 'skill_view':
                        try:
                            args = json.loads(fn.get('arguments', ''))
                            interactive[args.get('name', '?')] += 1
                        except Exception:
                            pass
    except Exception:
        pass

# ── Source 2: cron-driven skill loads ──
with open(f'{HERMES_HOME}/cron/jobs.json') as f:
    jobs = json.load(f)['jobs']

cron_skill_runs = Counter()
cron_skill_jobs = defaultdict(list)
for j in jobs:
    name = j.get('name', '?')
    completed = (j.get('repeat') or {}).get('completed', 0) or 0
    no_agent = j.get('no_agent', False)
    for s in (j.get('skills') or []):
        cron_skill_runs[s] += completed
        cron_skill_jobs[s].append(
            f"{name} (×{completed}, {'script' if no_agent else 'agent'})"
        )

# ── Combined leaderboard ──
all_skills = set(interactive) | set(cron_skill_runs)
combined = sorted(
    [(s, interactive.get(s, 0), cron_skill_runs.get(s, 0)) for s in all_skills],
    key=lambda x: -(x[1] + x[2])
)
print(f"{'RANK':<5}{'SKILL':<35}{'INTERACTIVE':<13}{'CRON':<8}{'TOTAL':<8}")
print("-" * 70)
for i, (s, inter, cron) in enumerate(combined, 1):
    print(f"{i:<5}{s:<35}{inter:<13}{cron:<8}{inter+cron:<8}")

# ── "Installed but never used" ──
installed = {p.split('/')[1] for p in glob.glob(f'{HERMES_HOME}/skills/*/SKILL.md')}
sub_skills = defaultdict(list)
for path in glob.glob(f'{HERMES_HOME}/skills/*/*/SKILL.md'):
    cat, sub = path.split('/')[-3], path.split('/')[-2]
    sub_skills[cat].append(sub)

used = set(interactive) | set(cron_skill_runs)
print("\n=== Installed sub-skills never loaded ===")
for cat in sorted(sub_skills):
    for sub in sorted(sub_skills[cat]):
        if sub not in used:
            print(f"  {cat}/{sub}")

# ── System-prompt token share ──
sp = cur.execute(
    "SELECT system_prompt FROM sessions WHERE system_prompt IS NOT NULL "
    "ORDER BY started_at DESC LIMIT 1"
).fetchone()[0]
m = re.search(r'<available_skills>(.*?)</available_skills>', sp, re.DOTALL)
if m:
    block = m.group(0)
    print(f"\nSkills block: {len(block):,} chars (~{len(block)//4:,} tokens)")
    print(f"Share of system_prompt: {len(block)/len(sp)*100:.1f}%")
```

## What each output section tells you

### Combined leaderboard
- Skills at the top with `CRON` column dominating are **automation workhorses** (e.g. `google-workspace` for Gmail triage). High cron + low interactive = "I scheduled this and forgot about it".
- Skills with high `INTERACTIVE` and zero `CRON` are **on-demand tools** (e.g. `cc-bill-review`, `task-tracker`). User reaches for them when needed.
- Skills with both zero columns are **never used** — see "Installed but never used" section.

### "Installed but never used" cross-reference
- Three layers of "enabled": **installed on disk** (Skills/ folder), **listed in `<available_skills>`** (category umbrellas), **used** (skill_view or cron).
- `<available_skills>` lists **categories** not sub-skills. To find unused sub-skills, you must glob the directory tree.
- Typical finding: 60-70% of installed sub-skills are never loaded in interactive sessions (verified June 2026 across 92 installed skills for the active profile).

### System-prompt token share
- The skills block is typically 30-50% of the system prompt (49% verified June 2026 for the active profile).
- That fraction applies to both `cache_write_tokens` (system prompt priming per session) AND `cache_read_tokens` (cache hits on subsequent turns within a session).
- **Pruning unused skills shrinks the entire prefix** — a 1,500-token reduction in the skills block saves 1,500 tokens of cache_write per session AND 1,500 tokens of cache_read per turn.

## Pitfalls

1. **`tool_calls` JSON format is variable.** The column may be a list of dicts, a JSON string of a list, a single dict, or `None`. Always parse with `isinstance` checks and `try/except`.

2. **Categories vs sub-skills.** `<available_skills>` lists 19 category umbrellas (e.g. `productivity`, `github`), but each category has 1-20 sub-skills. The user thinks in sub-skill names. Always present results in sub-skill granularity.

3. **Script-only crons inflate counts.** `no_agent: true` crons load skills for the script's import context but never invoke the LLM. Counting cron skill loads as "skill usage" is fair for code execution frequency but OVERSTATES token spend. For actual cost, query `cache_write_tokens` and `input_tokens` from `sessions` table.

4. **`jobs.json` schema changes.** Cron job config is moving target — the `skills` field, `repeat.completed`, and `no_agent` flags have all been added in different versions. Always check for field presence (`j.get('skills') or []`) rather than direct access.

5. **No "last used" timestamp.** `state.db` doesn't record per-skill last-use time — only that a skill was loaded in a given message. To compute "skill last used N days ago" you'd need to join `messages.session_id` → `sessions.started_at` for each `skill_view` row. Future improvement.

## Output format recommendation

When presenting results to the user:
- **Top skills**: ranked table with both interactive and cron columns
- **Never used**: grouped by category, with installed count vs used count
- **Token cost share**: 1-line summary of skills block % + estimated $ saved by pruning

Pair with the `discord-table-format` skill when delivering to Discord — image rendering is the default.
