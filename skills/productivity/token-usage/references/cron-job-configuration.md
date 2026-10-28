# Token Usage Cron Job — Configuration Reference

**Job ID:** `cbb893c8a65d`  
**Job name:** Weekly Token Usage Report  
**Script:** `token_report_no_agent.py`  
**Required mode:** `no_agent: true`

---

## Why `no_agent: true` is mandatory

The scheduler's LLM path wraps **ALL** output in `# Cron Job: ...` header/footer — this cannot be suppressed. The skill's documentation contains a "Cron Prompt Section" pattern designed for LLM-mode cron jobs, but it produces unwanted headers/footers for this job.

**`[SILENT]` suppression only works in `no_agent: true` mode.** The scheduler checks for `SILENT_MARKER = "[SILENT]"` at the START of script output to suppress delivery. In LLM mode, the scheduler wraps output before delivery — suppression is impossible.

**Correct architecture — fully self-contained no_agent script:**
- `no_agent: true, script: token_report_no_agent.py`
- Script does: DB query → generate table images → read bot token from `.env` → send via curl multipart → cleanup → `os._exit(0)`
- No LLM involvement → no double-wrapping

---

## Required job fields

```json
{
  "job_id": "cbb893c8a65d",
  "name": "Weekly Token Usage Report",
  "no_agent": true,
  "script": "token_report_no_agent.py",
  "schedule": "0 11 * * 0",
  "deliver": "discord:1499972245437550602"
}
```

**🚫 Do NOT set a `profile` on this job.** For `no_agent` jobs the scheduler resolves `script` under `<profile-home>/scripts/`. A stray `profile: "pro"` made it look for `~/.hermes/profiles/pro/scripts/token_report_no_agent.py` (which doesn't exist) and the job failed with `Script not found` on 2026-05-31 (the error went to the job's `deliver` target because the crash happened before the script could print `[SILENT]`). Fixed by clearing the profile. The script hardcodes `~/.hermes/state.db` and `~/.hermes/.env`, so it must run under the default (main) profile anyway — leave `profile` unset/null.

**⚠️ Skills list is retained for accessibility** — the `skills` field on the job still references `token-usage` and `discord-table-format` so skill files are accessible if the script ever needs to reference them. Skills are loaded for LLM-mode jobs; for `no_agent` jobs they serve as a file accessibility hint only.

---

## Critical: Bot token is NOT passed to no_agent scripts

**The scheduler does NOT pass `DISCORD_BOT_TOKEN` via `os.environ` to `no_agent: true` scripts.** This was the hidden reason the old `token_report_dispatch.py` failed silently.

**Fix:** Read the token directly from the `.env` file — use `os.path.expanduser("~/.hermes/.env")`, NOT `hermes-agent/.env` (that file has a stale token that causes 401 Unauthorized):

```python
ENV_FILE = os.path.expanduser("~/.hermes/.env")
TOKEN = None
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break
```

---

## Delivery target

The script reads the `TOKEN_REPORT_TARGET` environment variable to determine where to send the report. Format: `"channel_id"` (regular message) or `"channel_id:thread_id"` (threaded reply). When unset, defaults to the production home channel.

| Target format | Example | Behavior |
|---|---|---|
| *(unset / empty)* | — | Posts to home channel `1499972245437550602` as a regular message |
| `channel_id` | `1501742338639401071` | Posts to that channel as a regular message |
| `channel_id:thread_id` | `1499972245437550602:1512631264006504569` | Posts as a threaded reply (uses `channels/{thread_id}/messages` endpoint) |

**Setting the target for cron runs:** The scheduler does not pass env vars to `no_agent` scripts by default. To override the target for a specific cron run, set `TOKEN_REPORT_TARGET` in `~/.hermes/.env` (global) or use the cron job's env config if available. For manual one-off runs: `TOKEN_REPORT_TARGET=channel:thread python3 ~/.hermes/scripts/token_report_no_agent.py`.

**Known targets:**
| Context | Channel | Thread | `TOKEN_REPORT_TARGET` |
|---|---|---|---|
| Test / debugging | `1501742338639401071` | — | `1501742338639401071` |
| Production (work home) | `1499972245437550602` | — | *(default, unset)* |
| Threaded (example) | `1499972245437550602` | `1512631264006504569` | `1499972245437550602:1512631264006504569` |

The job's `deliver` field is set to `discord:1499972245437550602` but since the script sends directly via curl, `deliver` here is a housekeeping reference rather than the actual delivery mechanism.

---

## The LLM-mode trap (for reference — now resolved)

The skill previously contained BOTH:
1. A "Cron Prompt Section" for LLM-mode self-contained report generation
2. A `token_report_dispatch.py` delivery script pattern for script-mode delivery

The job was running in LLM mode but the skill's pattern told it to use the dispatch script — contradictory. The scheduler's output wrapping was producing double headers.

**Resolution (May 25, 2026):** Replaced with `token_report_no_agent.py` — a single self-contained script that eliminates the LLM entirely. The `Cron Prompt Section` pattern is now deprecated for this job.