> **Historical (2026-09-29):** written for the agent-mode crons (May 2026). Both jobs are `no_agent` since 2026-05-31: check `~/.hermes/cron/output/<job_id>/` and the gateway log for a `Sending file attachment route_<dest>.png` line; `send_message` re-delivery advice below is obsolete — re-run the script by hand to see the report, and ask before re-posting.

# Silent Discord Delivery Failure — Recurring Bug Pattern

> ⚠️ **Last updated:** May 26, 2026. Two distinct patterns — read "Current Pattern" first.

---

## Two Distinct Failure Patterns

| Era | Root Cause | Deliver Setting | How to Fix |
|-----|-----------|-----------------|------------|
| **May 5–13, 2026** (legacy) | `deliver: "local"` — cron saved file only, agent never called Discord API | `deliver: "local"` | Change to `deliver: "discord:channel"` |
| **May 14–present** (current) | Scheduler claims "delivered via live adapter" but gateway has no send event; thread_id mismatch | `deliver: "discord:channel"` (correct) | Manual re-delivery + see below |

---

## Current Pattern (May 14+)

### Symptom

The cron fires, agent runs to completion, scheduler logs `delivered to discord:1499972245437550602 via live adapter`, but **nothing appears in Discord**. Output file has correct data. No errors.

**Key diagnostic signals (check in order):**
1. `agent.log` contains: `delivered to discord:1499972245437550602 via live adapter`
2. `gateway.log` has **zero** "Sending response" entries for channel `1499972245437550602` in the delivery window
3. `errors.log` contains: `origin has thread_id=<X> but delivery target lost it`

### Root Cause — Two-Part Failure

1. **Live adapter silently fails without a gateway send.** The scheduler logs success but the live adapter path does not generate a corresponding gateway event. The message is never posted.

2. **Thread_id mismatch causes context loss.** The delivery target loses the origin thread_id. When `deliver=discord:1499972245437550602` (channel only), the message goes to top-level. Combined with the live adapter failure, nothing reaches Discord.

### How to Diagnose

```bash
# 1. Check if cron fired
grep "Job 'b617d89da15b'" ~/.hermes/logs/agent.log | tail -3

# 2. Check scheduler delivery claim
grep "delivered to discord:1499972245437550602" ~/.hermes/logs/agent.log | tail -3

# 3. Check gateway (absence = silent failure)
grep "1499972245437550602" ~/.hermes/logs/gateway.log | grep "Sending response"

# 4. Check thread_id mismatch
grep "delivery target lost it" ~/.hermes/logs/errors.log | tail -3
```

**If agent.log has "delivered via live adapter" but gateway.log has no "Sending response" → live adapter silent failure. Manual re-delivery required.**

### Immediate Fix

Use `send_message` tool to post the report text + MEDIA path to `discord:1499972245437550602`. Do NOT use `execute_code` + `requests` — bot token is not accessible in the sandbox.

### Verification

After any cron run, check **both**:
- `agent.log` for "delivered via live adapter" — scheduler finished
- `gateway.log` for "Sending response" to `1499972245437550602` — Discord actually received it

Discrepancy = silent failure → manual re-delivery.

---

## Legacy Pattern (May 5–13, 2026) — DEPRECATED

> Deprecated. `deliver: "local"` is no longer used.

`deliver: "local"` — cron saved to file but did NOT auto-post. Agent was supposed to call `execute_code` to POST to Discord but never did.

**Session structure when skipped:**
```
[tool: execute_code] "Image size: 179734 bytes..."     ← last call
[assistant] **☀️ Morning Commute Update**...           ← terminates, no Discord call
```

**Fix:** Changed `deliver: "local"` → `deliver: "discord:1499972245437550602"`.

---

## Health-Check Script

```bash
python3 -c "
import requests, os
with open(os.path.expanduser('~/.hermes/.env')) as f:
    for line in f:
        if '=' in line and not line.startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ[k] = v
BOT = os.environ['DISCORD_BOT_TOKEN']
CH = '1499972245437550602'
r = requests.post(f'https://discord.com/api/v10/channels/{CH}/messages',
    headers={'Authorization': f'Bot {BOT}', 'Content-Type': 'application/json',
              'User-Agent': 'DiscordBot (hermes-agent, 1.0)'},
    json={'content': '🧪 health check', 'embeds': []}, timeout=10)
print(f'Status: {r.status_code}')
"
```

## Key Lessons

1. **"delivered via live adapter" ≠ gateway send.** Live adapter logs success; gateway may have no send event. Verify with gateway.log.

2. **Always check both logs.** agent.log tells you scheduler finished. gateway.log tells you Discord received it. Discrepancy = silent failure.

3. **Thread_id mismatch is a known cron delivery issue.** `deliver=discord:channel` loses thread context. For channel-only crons this is expected — but if combined with live adapter failure, nothing appears.

4. **`cronjob action='run'` is a no-op for verification.** Returns `{"success": true}` but does NOT spawn an agent or produce output.

5. **Manual re-delivery:** Use `send_message` tool (not `execute_code` + `requests`) for ad-hoc Discord delivery.
