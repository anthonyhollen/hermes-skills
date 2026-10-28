# Cron Schedule, Wrapper Scripts & Token Handling

Rewritten 2026-09-23 (v2.17.0) against the live wrappers in `~/.hermes/scripts/`;
schedule / token / re-auth detail relocated here from SKILL.md.

## Schedule (spec §2)

| Job | Schedule (SGT, daily) | Entry | Delivery |
|---|---|---|---|
| Poll (daytime + late night) | 04,07,10,13,16,17,19,22,23:00 | `spotify_poll_wrapper.py` → `poll_listening_events()` | local |
| Poll (overnight) | 02:00 | `spotify_poll_wrapper.py` → `poll_listening_events()` | local |
| **Refresh** | 03:00 | `spotify_refresh_wrapper.py` → `refresh_playlist()` | Discord channel `1499972245437550602` |
| DeepSeek discovery (v2.19) | 01:20 + 02:20 retry | `spotify_llm_discover_wrapper.py` → `llm_discovery.run(token)` | local |
| DeepSeek skip report (v2.21) | 23:30 | `spotify_deepseek_report_wrapper.py` → `rebuild_signals()` + `llm_report.build_report()` | Discord playground `1501742338639401071` |
| ~~Skip digest~~ — **paused 2026-10-12** | 23:30 | `spotify_skip_digest_wrapper.py` | playground |

DeepSeek discovery job ID: `56b1d25e4c69` (`20 1,2 * * *`, no_agent, deliver local; created 2026-10-06).

DeepSeek skip report (v2.21, created 2026-10-12): no_agent job "Spotify DeepSeek
skip report" (`ebe12186f5fd`), `30 23 * * *`, delivered to the
playground. It replaced the skip digest (`bdbb183faf82`, **paused**
2026-10-12 at the user's request — they trust the skip logic; `hermes cron
resume bdbb183faf82` brings it back). The wrapper rebuilds the skip signals
(as the digest did, so the 01:20 batch learns from the evening's listening),
then prints `llm_report.build_report()`. Empty stdout (no DeepSeek song
finished or skipped today) means no message; a crash prints one failure line
and exits 1. DB only — no Spotify or DeepSeek calls.

The discovery wrapper force-refreshes the Spotify token via Hermes and passes it
straight to `run()` (no auth.json rewrite — Hermes' resolver saved it). It reads
`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` from `~/.hermes/.env` itself (the
scheduler does not export the gateway env to no_agent scripts). stdout: one
summary line on a finished batch, nothing on a skip (the 02:20 run after an ok
01:20 batch), one failure line + exit 1 on a crash. The refresh banner carries
a one-line note when DeepSeek picks were short because the batch failed / is stale.

Job IDs (confirmed live 2026-09-23 against cron/jobs.json): `f5e75496918a` (refresh, `0 3 * * *`,
deliver=discord), `afdb0b06e11a` (poll, `0 4,7,10,13,16,17,19,22,23 * * *`,
deliver=local), `dfadaf5e1833` (poll, `0 2 * * *`, deliver=local) — all
`no_agent`, `profile: None`. Old weekly cron `02979b83960d` (Saturdays 9:15 AM)
— **paused**.

> **May 2026 schedule updates**:
> - Added 17:00 SGT poll to capture evening commute peak (16:00–19:00 was overflowing at 47.6% of daily listening)
> - Added 23:00 SGT poll to capture late-night 23:00 listening cluster
> - Shifted overnight from 1am → 2am SGT
> - See `references/polling-data-loss.md` for the full corrected data analysis

Test outputs go to the Discord **playground** channel `1501742338639401071` only
(never the real channel `1499972245437550602` or a user thread). Real refresh
runs deliver to `1499972245437550602`.
The watchdog is the gateway's global `cron.script_timeout_seconds` (600s); the
refresher's stage budgets are sized under it (see
`references/scoring-and-composition.md` §4).

## The problem the wrappers solve

Spotify tokens from `hermes auth spotify` expire ~1 hour after issuance. A
`no_agent` cron job at 03:00 would otherwise use whatever token is in
`auth.json` — usually expired hours earlier.

Refreshing via `auth.py`'s `_do_refresh()` does not work: it sends
`grant_type=refresh_token` without a `client_id` (it has no `client_secret` to
send — `hermes auth spotify` stores none), and Spotify's token endpoint returns
`400 invalid_client` for PKCE tokens refreshed that way. **Result:**
`RuntimeError: The access token expired` and the whole run aborts.

## What the wrappers do (live versions, 2026-09-23)

Both `~/.hermes/scripts/spotify_refresh_wrapper.py` and
`~/.hermes/scripts/spotify_poll_wrapper.py`:

1. `sys.path.insert(0, "/home/amh/.hermes/hermes-agent")` and call
   `hermes_cli.auth.resolve_spotify_runtime_credentials(force_refresh=True)` —
   Hermes Agent's `_refresh_spotify_oauth_state()` performs the PKCE refresh
   correctly (sends `client_id`).
2. Write the fresh token into `~/.hermes/auth.json` → `providers.spotify`
   (`access_token`, `expires_at` = now + 1h ISO-8601) **atomically**: temp file +
   `fsync` + `os.replace`. If `auth.json` is unreadable they **abort** rather than
   rewrite it (falling back to `{}` would wipe every other provider's credentials).
3. Import the skill from
   `/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src`,
   set `db.DB_PATH` to the production DB, `db.init_db()` (applies schema
   migrations), then call the entrypoint.
4. Output contract (stdout = the delivered message):
   - **poll**: prints the poll summary dict; on any exception prints the traceback
     to stderr and one clean line `spotify poll failed: <Type>: <msg>`, exit 1.
   - **refresh**: on `status == "ok"` prints the banner
     `## 🎵 DJ Hermes - Hot Hits Updated!` + playlist link, appending
     `⚠️ n/40 tracks — not enough eligible tracks to fill the playlist` when
     short; on a handled error (`composer_empty`, `no_uris`) prints
     `## ⚠️ DJ Hermes - playlist refresh did not complete (reason: …)` (exit 0 so
     it is delivered); on a crash prints the traceback to stderr and
     `## ⚠️ DJ Hermes - playlist refresh failed: <Type>: <msg>`, exit 1. Never a
     raw traceback or a false success in the message body.

**Rule:** never call `auth._do_refresh()` directly in a cron context. Use
`resolve_spotify_runtime_credentials()` from the hermes-agent library.

## Token source (`auth.get_access_token()`)

Since v2.17.0: **`~/.hermes/auth.json` → `providers.spotify.access_token` first**
(what the wrappers just wrote), then the legacy SQLite `oauth_token` row (used
as-is if it expires in > 60s, else a `_do_refresh()` attempt, which fails for PKCE
tokens), else `RuntimeError("No Spotify token found. Run: hermes auth spotify")`.
Before v2.17.0 the DB row won, so a stale `oauth_token` row shadowed the fresh
wrapper token and triggered a doomed client_id-less refresh per call. The skill
itself never refreshes the auth.json token — only the wrappers (or `hermes auth
spotify`) do. Manual runs outside a wrapper therefore need a token < 1h old in
auth.json.

**Symptoms of an expired token (no wrapper / wrapper misconfigured):**
- `RuntimeError: The access token expired` / `401 Invalid access token`
- Polls stop capturing events; playlist stops refreshing
- In `refresh_playlist()` a 401/403 on the poll aborts the run (logged as an
  `'error'` refresh row); other poll failures are best-effort.

If you see `401` in a cron run output, check the job uses the wrapper
(`spotify_refresh_wrapper.py` / `spotify_poll_wrapper.py`); if it was configured
with inline bash/Python (old pattern), update it.

**Re-authenticate (manual, when the refresh token itself is dead):**
```bash
cd /home/amh/.hermes/hermes-agent && python3 -m hermes_cli.auth spotify
# OR equivalently:
hermes auth spotify
```
Browser opens → authorize → copy the callback URL from the browser address bar →
paste it back into the terminal. `hermes auth spotify login --no-browser` times
out — the browser flow is required for PKCE.

Granted scopes (`providers.spotify.granted_scope`, checked 2026-09-30):
`playlist-modify-private`, `playlist-modify-public`, `playlist-read-private`,
`playlist-read-collaborative`, `user-top-read`, `user-read-recently-played`,
`user-library-read`, `user-library-modify`, `user-read-private`,
`user-read-email`, **`user-read-playback-state`**, **`user-read-currently-playing`**
(the playback sampler needs these two), `user-modify-playback-state`. The
separate `scope` field lists only the 10 originally requested — read `granted_scope`.

## v2.18 additions (2026-09-30)

- **Skip digest** — no_agent job `Spotify skip digest (shadow)` (`bdbb183faf82`),
  `30 23 * * *`, script `spotify_skip_digest_wrapper.py`, delivered to the
  Discord playground `1501742338639401071`. It rebuilds the signals and prints
  the digest; empty stdout (no listening) means no message. **Paused
  2026-10-12** — replaced by the DeepSeek skip report (above).
- **Playback sampler** — **not cron**. It is the systemd user unit
  `~/.config/systemd/user/spotify-sampler.service`, `Restart=always`, which runs
  `~/.hermes/scripts/spotify_sampler.py` with the gateway venv python. The cron
  scheduler can't do it: 1-min granularity, a 600s watchdog, and an output file
  per run. Its token comes from `resolve_spotify_runtime_credentials(
  force_refresh=False, refresh_skew_seconds=300)`, which refreshes under Hermes'
  cross-process auth lock. The sampler never writes `auth.json` itself.
- **Wrapper change:** `_update_auth_token` in both the poll and refresh wrappers
  now does its read-modify-write of `auth.json` inside Hermes'
  `_auth_store_lock()`. Before, it wrote without the lock, so it could have
  reverted a refresh token the sampler had just rotated.
  - If this Hermes build lacks `_auth_store_lock`, the lock is a no-op.
  - If the lock can't be acquired (timeout), the rewrite is **skipped** with a
    stderr note. Hermes' resolver has already saved the token, so the
    poll/refresh continues.

## Cron job configuration

For `no_agent: true` jobs set `script` to the wrapper **filename** — the scheduler
resolves it in `~/.hermes/scripts/`, NOT as inline code (inline bash/Python causes
"Script not found"):

```python
cronjob(action="update", job_id="f5e75496918a", script="spotify_refresh_wrapper.py")
```
