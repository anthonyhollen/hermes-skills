> **Historical (2026-09-29):** both sg-commute crons have been `no_agent` scripts since 2026-05-31 — the scheduler never reads their `prompt`, so the prompt-design advice below no longer applies to them. Report text/format is changed in `~/.hermes/scripts/morning_commute.py` / `evening_weather.py`.

# Cron Prompt Delivery Anti-Patterns

## The Core Problem

When a cron job has `deliver: "discord:channel"` (system auto-delivers), AND the cron prompt tells the agent to call `send_message` explicitly, the agent's internal reasoning text ("Now I have all the data...") gets included in the final output before the actual report.

**Root cause:** The agent's reasoning output is part of its response. If the cron system auto-delivers everything the agent says, reasoning text that precedes the actual report gets sent to Discord.

## The Anti-Pattern (WRONG)

Cron job with `deliver: "discord:channel"` AND prompt that says:
```
Key reminders:
1. send_message must be called explicitly — model output is NOT auto-delivered to Discord
2. Discord image upload requires TWO calls: (a) text message with embeds:[], (b) image as multipart file attachment
```

Result: Agent's internal reasoning leaks into output, double-delivery risk.

## The Correct Pattern (RIGHT)

For `deliver: "discord:channel"` workflows (system auto-delivers):
```
You are [workflow]. [context].

IMPORTANT: The cron has `deliver: "discord:1499972245437550602"` set. Your final output IS the report — just produce it and end the session. Do NOT call send_message, do NOT use execute_code for Discord API calls, do NOT add HTTP status text. The system delivers your output automatically.
```

## Two Distinct Delivery Patterns

| Workflow | `deliver` setting | Agent action |
|----------|------------------|--------------|
| Morning Commute Report | `deliver: "discord:1499972245437550602"` | Final output IS the report — no Discord API calls |
| Evening Weather Forecast | `deliver: "discord:1499972245437550602"` | Final output IS the report — cron system auto-delivers |

**⚠️ `send_message` is NOT available in the Discord platform toolset.** Only `search_members`, `fetch_messages`, `create_thread` exist. Any cron that instructs the agent to call `send_message` will fail silently with `Unknown action: send_message`. Always use `deliver: "discord:channel"` for auto-delivery — never rely on agent-side `send_message` calls in Discord crons.

## Cron Prompt Anti-Pattern Checklist

For any cron job with `deliver: "discord"`:
- ❌ Do NOT tell the agent to call `send_message`
- ❌ Do NOT use `execute_code` for Discord API calls
- ❌ Do NOT output HTTP status text (e.g. "Sent to Discord (HTTP 200)")
- ❌ Do NOT output "Now I have all the data..." or similar reasoning text
- ✅ Keep the prompt focused on producing the report content only
- ✅ Let the cron system handle delivery automatically

## ⚠️ Editing Cron Prompts in jobs.json — Pitfalls

**The `patch` tool CANNOT safely edit JSON strings containing newlines or quotes.** When a cron prompt stored in `jobs.json` contains literal newline characters, the `patch` tool inserts raw bytes (0x0a) instead of escaped `\n` sequences (0x5c 0x6e), and does not escape inner double-quotes — silently corrupting the JSON.

**Symptoms of corruption:**
- `cronjob(action='list')` returns the job but with truncated/corrupted `prompt_preview`
- The JSON may parse fine but the prompt string is broken mid-way
- Agent output includes raw `\n` characters or broken text

**Safe editing methods for `jobs.json`:**

1. **execute_code byte-level surgery** (recommended for prompts with multi-line content):
   ```python
   import json
   with open('/home/amh/.hermes/cron/jobs.json', 'rb') as f:
       raw = f.read()
   jobs = json.loads(raw)
   # edit the prompt in memory, then:
   with open('/home/amh/.hermes/cron/jobs.json', 'w') as f:
       json.dump(jobs, f, indent=2)
   ```

2. **Rebuild the entire jobs.json** if corruption is extensive — dump all 20 jobs, fix the broken one, rewrite.

3. **Never use `patch` on `jobs.json` for multi-line prompts** — the tool is safe for code files (.py, .sh, .md) but NOT for JSON strings with newlines.

**Why `patch` fails here:** The patch tool operates on text-level search/replace. JSON requires `\n` escapes and `\"` for quotes inside strings. The tool doesn't know it's inside a JSON string and doesn't apply the necessary escaping.

## File-Mutation Verifier Warning

When `patch` fails to find `old_string` in a target file, the **file-mutation verifier** (a post-patch safety check) emits a warning:
```
⚠️ File-mutation verifier: 1 file(s) were NOT modified this turn
  • /path/to/file.md — [patch] Could not find a match for old_string
```

**This means the patch was a no-op** — the file was not modified. The file is intact; the patch simply didn't match.

**Common cause:** Searching for text that exists in a **cron run output log** but never existed in the skill/reference file. For example, `"Now let me compile"` appeared in cron output logs but was never in `commute-report.md` — a patch targeting it fails with this warning.

**What to do:**
1. Do NOT panic — check whether the target text actually exists in the file via `read_file` with grep
2. If the text is in a cron output log, the fix belongs in the **cron prompt** (in `jobs.json`), not in a skill file
3. If the fix belongs in `jobs.json`, use Python `json.load()` → edit → `json.dump()` — NOT `patch`

**Note:** A successful `patch` produces `"files_modified"` in its return. No such field = patch didn't apply.

## Verification

After fixing a cron prompt, check `~/.hermes/cron/output/<job_id>/` for the latest output file:
- Clean output: report starts immediately with the header
- Bad output: reasoning text appears before the report header

## Known Cron Jobs Using Each Pattern

| Job ID | Name | Pattern |
|--------|------|---------|
| `b617d89da15b` | Morning Commute Report | `deliver: "discord:1499972245437550602"` — agent outputs report, system delivers |
| `c967008ca121` | Evening Weather Forecast | `deliver: "discord:1499972245437550602"` — agent outputs report, system delivers (NOT `send_message`) |
| `cbb893c8a65d` | Weekly Token Usage Report | `deliver: "discord:1499972245437550602"` — agent outputs report, system delivers |