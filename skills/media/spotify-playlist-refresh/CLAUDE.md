# CLAUDE.md — spotify-playlist-refresh

Attached to every agent command run in this directory, so it stays short
(≤ 4,000 characters; `scripts/test_llm_discovery.py` checks it). Read `SKILL.md`
first for the summary, cron jobs and operations; the detail is in `references/`
(`SKILL.md` → References table). History is in `CHANGELOG.md`.

## Where things are
- Live code: `~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/`.
  - `config.yaml` is read live (except its `spotify` / `schedule` / `paths` blocks).
  - `spotify.db` (SQLite WAL) is the only store.
  - `src/` modules run from `src/` with `sys.path.insert(0, '.')`.
  - Set `db.DB_PATH` before `db.init_db()` unless a wrapper does it.
- Cron wrappers live in `~/.hermes/scripts/`, not here: refresh, poll, DeepSeek
  discovery, DeepSeek skip report. A cron `script` field is a filename, never
  inline code.

## Invariants
- **Spotify calls go through `client.py` (curl).**
  - Never use the Hermes `spotify_playlists` tool for writes: it hits deprecated
    endpoints (403).
  - The Feb-2026 removals return 403: top-tracks, recommendations,
    audio-features / audio-analysis, batch tracks / albums / artists. See
    `references/feb-2026-api-audit.md`.
- **Catalogue calls are guarded** (`api_guard`: spacing, caps, remembered bans).
  Never bypass the guard. Never run DeepSeek batches or catalogue bursts in the
  daytime (2026-10-07: a 4.8 h ban).
- **Tokens:** never call `auth._do_refresh()` for PKCE tokens (400
  invalid_client); the wrappers refresh through Hermes. To re-authenticate:
  `cd ~/.hermes/hermes-agent && python3 -m hermes_cli.auth spotify` (browser).
- **DeepSeek:**
  - `max_tokens` is a fixed ceiling — never raise it. If the nightly report
    warns about reasoning, shrink the request (`picks_requested`).
  - Prompt input is bounded by `input_max_days` (14) and `prompt_chars_max`.
  - Every call shares a daily budget (`budget_tokens_day`, `llm_usage`). Ask the
    user before any DeepSeek test / probe calls — they bill (10-12: 77¢).
  - Taste rules: upbeat (`min_bpm`) and female-led (`vocal_filter`) only —
    the user drives while listening.
  - `llm_discovery.enabled: false` rolls back to the v2.18 artist graph.
- **Skip signals** live only in derived tables (`skip_signal` is rebuilt).
  - Never write synthetic `play_events`.
  - Only the user flips `skip_signals.enabled`.
- **The sampler** is the systemd user unit `spotify-sampler.service`.
  - Restart it after changing a module it imports (`db`, `client`, `skipsig`,
    `skip_signals`, `sampler`, `auth`).
  - Over SSH, run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` first.
- Test posts go to the Discord playground `1501742338639401071` only.

## Before deploying
- Run `scripts/test_llm_discovery.py`, `scripts/test_skip_signals.py` and
  `scripts/test_track_name_filters.py` with the venv python.
- Drift-guard the live files (sha256) first: other sessions and the
  post-session curator edit skill files.
- Bump the `version` in `SKILL.md` and add a `CHANGELOG.md` entry.
- Keep `SKILL.md` and this file to the current state. Release history belongs
  in `CHANGELOG.md`.
