#!/usr/bin/env python3
"""
Spotify DeepSeek discovery wrapper — runs in no_agent cron context (v2.19).

Schedule 01:20 SGT with a 02:20 retry (the retry is a no-op once an 'ok' batch
exists). Gets a fresh Spotify token from Hermes Agent's auth layer and passes it
straight to llm_discovery.run() (no auth.json rewrite — Hermes' resolver already
saved the token), which asks DeepSeek for picks, verifies them with Spotify
search and stores llm_batch / llm_pick rows for the 03:00 refresh.

stdout is the delivered message (deliver=local, so nobody sees it unless
delivery changes): one plain summary line on a finished batch, NOTHING on a
skip, one clean failure line (exit 1) on a crash — the traceback goes to stderr.
No model text is ever printed.
"""
import sys, traceback
import os

HERMES_AGENT = "/home/amh/.hermes/hermes-agent"
SKILL_SRC    = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src"
DB_PATH      = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"


def main():
    sys.path.insert(0, HERMES_AGENT)
    # PyYAML from the scripts venv (pure-python; importable on any interpreter).
    _YAML_SP = os.path.expanduser("~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages")
    if _YAML_SP not in sys.path:
        sys.path.append(_YAML_SP)
    try:
        from hermes_cli.auth import resolve_spotify_runtime_credentials
    except ImportError:  # v0.22.0+: moved to the spotify plugin
        sys.path.insert(0, os.path.expanduser('~/.hermes/plugins/spotify'))
        from spotify_auth import resolve_runtime_credentials as resolve_spotify_runtime_credentials
    creds = resolve_spotify_runtime_credentials(force_refresh=True)

    sys.path.insert(0, SKILL_SRC)
    import db, llm_discovery
    db.DB_PATH = DB_PATH
    db.init_db()
    result = llm_discovery.run(creds["access_token"], force="--force" in sys.argv[1:])
    if result.get("status") in ("ok", "partial"):
        print(result.get("summary", ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        print(f"Spotify DeepSeek discovery failed: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)