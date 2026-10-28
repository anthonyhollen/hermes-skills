---
name: spotify-playlist-refresh
description: "Spotify playlist refresh: affinity-based scoring → 40-track daily playlist (24 favourites + 16 DeepSeek-picked new songs), with sampler-based skip detection (cooldown/demotion). Spec-compliant implementation in ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/"
version: 2.24.0
platforms: [linux]
prerequisites:
  python: "3.11+"
  files:
    - ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db  # SQLite WAL
    - ~/.hermes/auth.json           # Spotify token (from hermes auth spotify)
  note: "All Spotify calls use direct curl via subprocess — NOT Hermes Spotify tools. See 'Playlist writes'."
metadata:
  hermes:
    tags: [spotify, playlist, affinity-scoring, daily-refresh, deepseek]
    related_skills: [spotify]
---

# Spotify Playlist Refresh — Spec-Compliant Implementation

Production system at `~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/`
(replaced the old weekly JSON-history approach). Polls listening history, scores
per-track affinity, and rewrites playlist `4y4Fd7Pg52Ubi0zqJBNJnm` daily at 03:00 SGT.

## Quick Start

Run from `src/` (`python3 -c` puts the cwd on `sys.path`). `db.DB_PATH` defaults to
the production DB — assign it before `init_db()` to target a copy. Manual runs use
the token already in `~/.hermes/auth.json` (valid ~1h after a wrapper run or
`hermes auth spotify`).

```bash
cd ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src

# Poll once (no playlist change)
python3 -c "import db, poller; db.init_db(); print(poller.poll_listening_events())"

# Full refresh (poll → score → compose → REPLACE the live playlist)
python3 -c "import db, refresher; db.init_db(); print(refresher.refresh_playlist())"

# Dry-run compose: read-only API calls, writes NOTHING (no DB writes, no playlist change)
python3 -c "
import db, scorer, composer; db.init_db()
scores, _ = scorer.compute_affinity()          # returns (scores, computed_at_ms)
playlist = composer.compose_playlist(scores, dry_run=True)
by_src = {}
for t in playlist: by_src.setdefault(t.get('source', '?'), []).append(t['name'][:30])
for src, names in sorted(by_src.items()): print(f'{src}: {len(names)} (e.g. {\", \".join(names[:3])})')
print(f'Total: {len(playlist)}/40')
"
```

With a fresh token, the wrapper does the same as cron:
`~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/spotify_poll_wrapper.py`
(the refresh wrapper **replaces the live playlist** and prints the Discord banner).
Since Hermes v0.22.0 (2026-10-14) cron runs `.py` scripts on Hermes' dependency
venv (Python 3.14, no PyYAML), so every wrapper appends
`~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages` for PyYAML — keep
that block in any new wrapper (see CHANGELOG 2.23.1).

## Architecture

```
~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/
├── config.yaml     # playlist / exclude / scoring blocks are read live; spotify / schedule / paths are NOT
├── spotify.db      # SQLite WAL (+ spotify.db.bak-playedat-20260923)
├── logs/           # unused by code (cron output: ~/.hermes/cron/output/<job_id>/)
├── scripts/
│   └── migrate_2026_09_played_at_end.py   # one-shot v2.17.0 migration (already run)
└── src/
    ├── db.py          # schema init + migrations + CRUD
    ├── auth.py        # token read: auth.json first, SQLite oauth_token fallback
    ├── client.py      # curl HTTP wrapper: retry/backoff, stage deadline, 429 breaker, GET memo + pacing
    ├── poller.py      # poll_listening_events() entrypoint (+ context_uri, signal rebuild)
    ├── scorer.py      # affinity scoring + blocklist (+ skip signals when enabled)
    ├── exclusions.py  # category exclusions (pre-scoring) + output-only album exclusions
    ├── composer.py    # core + llm + wildcard (or v2.18 discovery) composition via _Picker (counts from config)
    ├── llm_discovery.py # v2.19 DeepSeek job: profile → picks → Spotify-verified candidates (llm_batch/llm_pick)
    ├── llm_report.py  # v2.21 nightly DeepSeek skip report vs the artist-graph benchmark (DB only)
    ├── refresher.py   # refresh_playlist() orchestration + stage budgets (+ playlist_version)
    ├── skipsig.py     # v2.18 pure skip logic: Segmenter, classify, match, fallback inference
    ├── skip_signals.py # v2.18 rebuild_signals + cooldown/demote/heard/bans + digest + label CLI
    └── sampler.py     # v2.18 resident GET /me/player sampler (systemd user unit)
```
Skill root: `scripts/test_track_name_filters.py` (filter suite),
`scripts/test_skip_signals.py` (+ `scripts/fixtures/`, skip-detection suite),
`references/`. Wrappers live in `~/.hermes/scripts/` (not the skill dir).

## How it works (summary — detail in references)

- **Poll** — `recently-played` (50). `played_at` is the play **END**, so
  `ms_played` comes from the gap to the previous play.
  → `references/scoring-and-composition.md`.
- **Score** — completion brackets × `0.5^(age/7)` over 7 days (config
  `scoring`). ≥ +1.0 = liked; ≤ −1.0 = permanently blocklisted. Baby-sleep /
  category exclusions are dropped before scoring (`references/filters.md`).
- **Compose (03:00, DB only — no API calls)** — 40 tracks:
  - 24 core favourites plus 16 new songs: **12 DeepSeek picks** (`llm`, incl.
    up to 3 `encore` re-plays and 3 "more from" songs) and **4 wildcards**
    (≤ 30-day releases by DeepSeek-named similar artists);
  - `mix` layout (1–3 favourites, new songs spread through the rest);
  - every guardrail applied while picking (`_Picker`).
  Short → favourites fill the gap and the banner says `⚠️ n/40`.
  `llm_discovery.enabled: false` = the v2.18 artist graph.
- **DeepSeek discovery (01:20, retry 02:20)** — one `deepseek-flash` thinking
  call on a taste profile; every pick is verified with Spotify search (exact
  title + artist, playable, artist never heard **by ID**, **no J-pop**).
  - **Bounded by design:** prompt input ≤ 14 days (`input_max_days`) and
    `prompt_chars_max`; 20 picks + 10 similar artists.
  - **`max_tokens` is a fixed ceiling** — if the report warns, shrink the
    request; never raise the limit.
  - **Spend guard:** every DeepSeek call through the skill, from any process,
    shares one budget per day (`budget_tokens_day` 120k, about $0.15). The day
    opens at 01:00 SGT so each night gets the whole allowance. Over it, calls
    are refused. **Ask the user before any test / probe calls**:
    on 10-12, 23 probe calls cost 77¢, many times the normal daily spend.
  - **Taste rules (user 2026-10-15):** upbeat (`min_bpm` 95) and female-led
    (`vocal_filter`) only, because slow songs while driving are dangerous. The
    model labels act / BPM; breaking picks are dropped before any search.
    Finished artists are excepted from the female-led rule.
  - **Learning loop:** encores, WORKED / DIDNT_WORK feedback, adaptive mix /
    angle.
  - Detail: `references/llm-discovery.md`.
- **DeepSeek skip report (23:30)** — the share of new DeepSeek songs skipped
  (today, per day, since 6 Oct) against the artist-graph benchmark (1–6 Oct:
  74% skipped / 26% finished), plus last night's token use.
- **Skip signals** — the resident sampler (`spotify-sampler.service`) measures
  real exposure. User rule: leaving before 90% is a skip. Any skip → a
  3-refresh cooldown; 2 in 14 days → out until a full play.
  → `references/skip-signals.md`.
- **Refresh** — stage budgets within the 600 s watchdog; one atomic `PUT`; a
  failed write leaves the old playlist intact.

## Spotify API guard

Every **catalogue** GET (`search`, `artists`, `albums`, `tracks`, …) from any
process is spaced ≥ 2.5 s and capped at 120/h and 200/day. Any 429 is
remembered (`api_guard`) and refused everywhere until it expires.
- **Never run DeepSeek batches or catalogue bursts in the daytime.** A daytime
  run drew a 4.8 h ban on 2026-10-07.
- Batches run only 00:00–02:45 SGT.
- Config: `api_guard`. Detail: `references/llm-discovery.md`.

## Cron (no_agent)

| Job | Schedule (SGT) | Wrapper (`~/.hermes/scripts/`) | Delivery |
|---|---|---|---|
| Poll | 04,07,10,13,16,17,19,22,23:00 + 02:00 (two jobs) | `spotify_poll_wrapper.py` | local |
| Refresh | 03:00 | `spotify_refresh_wrapper.py` | Discord `1499972245437550602` |
| DeepSeek skip report (v2.21) | 23:30 | `spotify_deepseek_report_wrapper.py` | Discord playground `1501742338639401071` |
| DeepSeek discovery | 01:20 + 02:20 retry | `spotify_llm_discover_wrapper.py` | local |
| ~~Skip digest~~ — **paused 2026-10-12** (the user trusts the skip logic; the report replaced it) | 23:30 | `spotify_skip_digest_wrapper.py` | playground |

Not cron: **`spotify-sampler.service`** (systemd user unit, launcher
`~/.hermes/scripts/spotify_sampler.py`) runs continuously — `export
XDG_RUNTIME_DIR=/run/user/$(id -u)` before `systemctl --user status|restart
spotify-sampler` over SSH; restart it after changing `src/`.

**Always use the wrapper scripts** — the `script` field is a filename; inline
code causes "Script not found". Wrappers force-refresh the token via Hermes
(`resolve_spotify_runtime_credentials(force_refresh=True)`), write it to
`auth.json` atomically, then run the entrypoint. Never use `auth._do_refresh()`
for PKCE tokens (`400 invalid_client`). Update a job:
`cronjob(action="update", job_id="<id>", script="spotify_refresh_wrapper.py")`.
Job IDs, schedule history, output contract: `references/cron-wrappers.md`.

## Token

`auth.get_access_token()`: `~/.hermes/auth.json` → `providers.spotify.access_token`
first, SQLite `oauth_token` fallback. 401 in cron output → check the job uses
the wrapper. Refresh token dead → `hermes auth spotify` (browser PKCE flow;
`--no-browser` times out). Scopes and failure modes: `references/cron-wrappers.md`.

## Playlist writes (critical)

**Do NOT use the Hermes `spotify_playlists` tool** — it maps to deprecated
endpoints that return 403. `client.py` wraps `curl` via `subprocess`. The
refresher replaces the playlist in **one** `PUT /v1/playlists/{id}/items`
(`refresher._replace_tracks`) — a failed write leaves the old playlist intact:

```python
import client, refresher
token = client.get_token()
resp = client.put(f"playlists/{refresher.PLAYLIST_ID}/items", body={"uris": uris[:100]}, token=token)
# >100 uris: POST the rest in batches of 100 to the same path. Never clear first.
```
Feb 2026 endpoint removals and app-level 403s (details, metadata, create,
library save): `references/feb-2026-api-audit.md` — read it before touching any
API call.

## Common operations

- **Add a track-name filter** — add cases to `scripts/test_track_name_filters.py`
  first, add the regex + `_is_<thing>` helper in `composer.py`, thread it into
  `_passes_output_filters` (the only call site), run the suite. Full steps +
  regex pitfalls: `references/filters.md`.
- **Exclude an album / artist** — `config.yaml` → `exclude.albums` (track IDs,
  output-only), `exclude.album_name_contains` (substring, output-only), or
  `exclude.artist_ids` (pre-scoring). See `references/filters.md`.
- **Block / unblock a track** — `references/skip-detection-limitation.md` /
  `references/troubleshooting.md` (sqlite one-liners).
- **Check recent runs** — `SELECT * FROM run_log ORDER BY run_id DESC LIMIT 10`
  (query in `references/troubleshooting.md`).
- **Answer "what did I skip this morning?"** — rebuild signals first (skip_signal
  is only refreshed on poll / the 23:30 report / refresh): from `src/`,
  `python3 -c "import db, skip_signals as ss; db.init_db(); print(ss.rebuild_signals())"`,
  then read `skip_signal` rows for the window; names/artists come from
  `play_segment` data. Use the venv Python — no `sqlite3` CLI on the Pi. A
  session not yet polled shows `0 matched` plays — expected, not a bug.
- **DeepSeek picks** — from `src/`: `python3 llm_discovery.py show` (latest
  batch, every row + why it was dropped), `stats --days 7` (finished share per
  playlist source vs the 26% discovery baseline), `python3 llm_report.py`
  (tonight's skip report, DB only), `run --dry-run` (writes nothing — night
  only), `run --force` (a batch now). Style / angles / blocked names:
  `config.yaml` → `llm_discovery`. Rollback: `llm_discovery.enabled: false`.
- **Skip-check digest** — **paused 2026-10-12** (job `bdbb183faf82`; the user
  trusts the skip logic). Resume only if asked: `hermes cron resume bdbb183faf82`.
  Labels / accuracy: `references/skip-signals.md`.

## References

| File | Read when |
|---|---|
| `references/scoring-and-composition.md` | ms_played inference, migration, scoring tables, composition/picker, caching, dry_run, stage budgets, client retry policy |
| `references/filters.md` | output filters, category + album exclusions, adding a filter, regex/patch pitfalls |
| `references/schema.md` | SQLite tables, run_log detail shapes, config.yaml (what is / isn't read) |
| `references/cron-wrappers.md` | cron jobs + IDs, wrapper behavior/output, token source, re-auth, scopes |
| `references/troubleshooting.md` | a run misbehaves: underfill, null playlist reads, unplayable tracks, 5xx, events_stored, run_log errors, backups |
| `references/feb-2026-api-audit.md` | before touching any Spotify API call |
| `references/wildcard-rate-limit.md` | wildcard fill / album-endpoint rate limits |
| `references/impl-notes.md` | May 2026 composer fixes, verification commands |
| `references/llm-discovery.md` | v2.19 DeepSeek discovery: probe evidence, flow, new-artist / no-J-pop / matching rules, run safety, ops, rollback, cost |
| `references/skip-signals.md` | v2.18 skip detection: sampler, fallback tiers, cooldown/demote/bans, digest + labels, acceptance gates, service ops |
| `references/skip-detection-limitation.md` | why recently-played misses skips (history); manual blocklist |
| `references/artist-avoidance-decay.md` | avoided artists linger; decay options |
| `references/polling-data-loss.md` | poll coverage / schedule analysis (May 2026) |
| `references/discovery-underfill-diagnosis.md` | non-standard artist IDs, v1.2 self-heal |
| `references/spec-compliance-audit.md`, `references/spec-impl-findings.md`, `references/post-full-run-findings.md` | historical audits (dated correction notes at top) |
| `CHANGELOG.md` | what changed and why, per version |

Test outputs go to the Discord **playground** channel `1501742338639401071`
(never the real channel or user threads); real refresh runs deliver to
`1499972245437550602`.
