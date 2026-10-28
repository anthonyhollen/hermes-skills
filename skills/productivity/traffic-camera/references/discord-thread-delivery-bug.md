# Discord Thread Delivery Bug

## Problem

When `traffic_camera_report.py` runs inside a Discord thread, it may deliver
to the **parent channel** instead of the thread.

## Root Cause

The gateway's `HERMES_SESSION_KEY` and `HERMES_SESSION_THREAD_ID` env vars
can contain the parent channel ID instead of the actual thread ID.

**Chain:**
1. Discord adapter (`_handle_message`) sets `chat_id=str(effective_channel.id)`.
   For thread messages, `effective_channel` is the thread itself, so
   `chat_id = thread_id`.
2. `build_session_key` uses `source.chat_id` + `source.thread_id` — both
   equal the thread ID.
3. BUT the session may have been established from the **first channel message**
   (before auto-threading created the thread). The gateway reuses that session,
   so `session_key` still has the channel ID.
4. `_set_session_env` reads `context.session_key` (from the session entry)
   and `context.source.chat_id`/`thread_id` (from the current source).
   When these disagree, env vars can be stale.
5. The script reads `parts[4]` from `HERMES_SESSION_KEY` → gets channel ID →
   posts to parent channel.

## Comparison: Telegram

Telegram's adapter passes `chat_id=supergroup_id` and `thread_id=topic_id`
(distinct values). Discord passes `chat_id=thread_id` and `thread_id=thread_id`
(identical), losing the parent channel distinction.

## Workaround

Always pass `--thread-id` explicitly:

```bash
python3 ~/.hermes/scripts/traffic_camera_report.py --start "Home" --end "670238" \
    --thread-id <THREAD_ID_FROM_SESSION_CONTEXT>
```

The agent reads the thread ID from the session context header:
`Source: Discord (<channel_id>/<thread_id>)`

## Fix (upstream)

The proper fix is in the gateway: `HERMES_SESSION_THREAD_ID` should always
reflect the actual thread/topic ID, regardless of which session entry is used.
This requires changes to how `_set_session_env` populates the variable, or
how the Discord adapter builds the SessionSource for thread messages.
