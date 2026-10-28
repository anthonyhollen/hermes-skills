# Delivering table images to Discord

Written 2.0.0 (2026-09-29) from the gateway source (`tools/send_message_tool.py`, `gateway/platforms/base.py`) and
200 real Discord sessions. The default for the agent is SKILL.md's one command (`scripts/send_tables.py`).

## What goes wrong (seen in real sessions)
- **`send_message` needs an explicit target.** `target` + `message` are both required; `target="discord"` (platform
  only) posts to the **home channel #alerts** (`1499972245437550602`) — 9 table messages once landed there from a
  thread. A message id or the parent channel id as target gives 404 / the wrong place. Thread target form:
  `discord:<channel_id>:<thread_id>`.
- **Several `MEDIA:` tags in one final reply** are re-batched by `extract_media()`: all text first, then all images —
  header↔image pairing is lost (the user complained on 2026-06-25). A `---` separator doesn't help. One `MEDIA:` tag
  (a single image) in a reply is fine; a `MEDIA:` path that doesn't exist is silently dropped
  (`Skipping unsafe MEDIA directive path`), so never delete the PNG in the same turn as a `MEDIA:` reply.
- **Restating the table** as text/bullets after the images made the user think data was sent twice (2026-07-22).
- **Shell quoting**: `echo '<json>' | …` and `--one '<json>'` broke on an apostrophe ("Jln Ma'mor"); a quoted heredoc
  (`<<'JSON'`) or a file argument can't.
- **Hand-written curl uploads** sometimes `grep`-ed `.env` and printed the bot token into the session transcript —
  another reason to use the script, which reads the token itself and passes it to curl on stdin (`-K -`).

## send_tables.py (default)
Renders all sections in-process, then per section ONE `POST /channels/<id>/messages` multipart request:
`--form-string payload_json={"content": caption, "allowed_mentions": {"parse": []}, "nonce": …, "enforce_nonce": true}` +
`-F file=@<png>`.
- **Target**: the current conversation — `HERMES_SESSION_KEY` parts[4], else `HERMES_SESSION_THREAD_ID` / `CHAT_ID` (only
  for a Discord session). The terminal tool injects the current turn's session variables into every command; for
  Discord threads the key is `agent:main:discord:thread:<thread_id>:<thread_id>` (84/84 recent sessions: parts[4] == the
  thread/chat id). For group chats parts[5] is a **user** id — never used. `--to discord:<ch>[:<thread>]` (the last id
  wins) is refused (exit 2, nothing posted) if it differs from the current conversation or is the home channel
  (`DISCORD_HOME_CHANNEL` in `~/.hermes/.env`) unless `--force-target`. A missing target, a non-Discord session or an id
  that isn't 17–20 ASCII digits → exit 2. Never a fixed/home fallback.
- **Transport**: `curl -q` (ignores `~/.curlrc`); the token is read from `~/.hermes/.env`, checked against
  `[A-Za-z0-9._-]+`, and passed on stdin (`-K -` → `header = "Authorization: Bot …"`), never in argv.
- **Duplicates**: the nonce is a hash of (target, title, caption, rows); with `enforce_nonce` Discord returns the existing
  message for a same-nonce create within a few minutes, so re-sending a section is safe in that window. `--repost` salts it.
- **Retries**: a 429 with `retry_after` ≤ 10 s is retried once; longer fails fast. A timeout/5xx is **not** retried (the
  POST may have landed) and is reported with `"may_have_posted": true`; 4xx and connect/DNS failures (curl 6/7) report
  `false`; a 404 adds "not a channel/thread id (a message id?)".
- **Order and reporting**: 0.5 s between posts (5 msgs/5 s/channel); stops at the first failure so order is kept; one JSON
  line on stdout on every normal exit and on Ctrl-C or an internal error (`"aborted"`); a flushed
  `posted: <title> -> message <id>` line on stderr per message — the only record if the process is killed (SIGTERM/SIGKILL
  get their default action: three gate laps of signal handling each opened a new hole, so it was removed); each PNG
  deleted only after a 2xx. (A `nohup`'d run therefore ignores SIGHUP as expected.)
- Posts go straight to Discord, so they're not mirrored into the gateway's session transcript and bypass
  `send_message`'s relay authorization — the stdout JSON is the record. No lock: don't run two at once for one reply.

## A single image via send_message (alternative)
If the script can't be used: render with `render_tables.py`, then ONE call per section —
`send_message(target="discord:<channel_id>:<thread_id>", message="**Title**\nMEDIA:<png>")`. The gateway sends the text
as the image's caption, so header + image arrive as one message (29/29 such calls succeeded in the history).

## no_agent scripts (crons)
`send_message` doesn't exist there and `hermes` isn't on a non-login PATH — don't use `hermes send` or `MEDIA:`
(except a single trailing `MEDIA:` line on stdout for a `deliver: discord:<id>` job). Copy a proven sibling:
REST multipart with `curl`, token from `~/.hermes/.env`, `--form-string payload_json=…` (not `-F`, which truncates at
`;`), `allowed_mentions: {"parse": []}`, status check, one 429 retry — `task-tracker.py` (`discord_post`),
`skill_usage_report.py`, or `send_tables.discord_post`. General rules: `hermes-skill-tuneup/references/delivery-reliability.md`.
