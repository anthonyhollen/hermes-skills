#!/usr/bin/env python3
"""
Nightly skip-detection digest — no_agent cron job (23:30 SGT), delivered to the
Discord playground channel while skip signals run in shadow mode.

Rebuilds the derived skip signals, then prints the digest for the current
playlist version (what the sampler / fallback saw at each position, as
"Song — Artist"). Prints NOTHING when there was no listening (empty stdout =
no message). Makes at most ONE read-only Spotify call (the live playlist, to
fill in artist names the local caches lack). The user's reply is recorded with
  python3 skip_signals.py label YYYY-MM-DD "17 not, 30 skip"   (from src/)
"""
import sys, traceback
import os

HERMES_AGENT = "/home/amh/.hermes/hermes-agent"
SKILL_SRC = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src"
DB_PATH   = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"


# PyYAML from the scripts venv (pure-python; importable on any interpreter).
_YAML_SP = os.path.expanduser("~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages")
if _YAML_SP not in sys.path:
    sys.path.append(_YAML_SP)

def _fill_artists(skip_signals):
    """Best-effort: a failure only means the digest shows fewer artist names."""
    try:
        sys.path.insert(1, HERMES_AGENT)
        try:
            from hermes_cli.auth import resolve_spotify_runtime_credentials
        except ImportError:  # v0.22.0+: moved to the spotify plugin
            sys.path.insert(0, os.path.expanduser('~/.hermes/plugins/spotify'))
            from spotify_auth import resolve_runtime_credentials as resolve_spotify_runtime_credentials
        token = resolve_spotify_runtime_credentials(force_refresh=False)["access_token"]
        version = skip_signals.version_for_day(skip_signals._day(skip_signals._now_ms()))
        if version:
            skip_signals.fill_version_meta(version, token)
    except Exception as e:
        print(f"artist lookup skipped: {type(e).__name__}: {e}", file=sys.stderr)


def main():
    sys.path.insert(0, SKILL_SRC)
    import db, skip_signals
    db.DB_PATH = DB_PATH
    db.init_db()
    skip_signals.rebuild_signals()
    try:
        skip_signals.auto_accept()      # v2.20.4: an unanswered digest = the skips were right
    except Exception as e:
        print(f"auto-accept failed: {type(e).__name__}: {e}", file=sys.stderr)
    _fill_artists(skip_signals)
    out = skip_signals.build_digest()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        print(f"⚠️ DJ Hermes skip digest failed: {type(e).__name__}: {e}")
        sys.exit(1)