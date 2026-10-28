#!/usr/bin/env python3
"""
Spotify playback sampler launcher — run by the systemd USER unit
~/.config/systemd/user/spotify-sampler.service (not a cron job: it is resident).

Supplies sampler.run_forever() with a token function backed by Hermes' Spotify
auth: the cached token is reused until < 5 min remain, then
resolve_spotify_runtime_credentials() refreshes it under Hermes' cross-process
auth lock (this script never writes auth.json itself). Logs go to the journal:
  journalctl --user -u spotify-sampler -n 50
"""
import sys, time
import os
from datetime import datetime

HERMES_AGENT = "/home/amh/.hermes/hermes-agent"
SKILL_SRC    = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src"
DB_PATH      = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"
REFRESH_SKEW_S = 300


def _expiry(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class TokenSource:
    """force=False: cached token (re-resolved when < 5 min remain);
    force=None: drop the cache and re-read Hermes' store; force=True: force a refresh."""

    def __init__(self, resolve):
        self.resolve = resolve
        self.token, self.expires = None, 0.0

    def __call__(self, force: bool | None = False) -> str:
        if force is None:
            self.token = None
        if force or self.token is None or self.expires - time.time() < REFRESH_SKEW_S:
            try:
                creds = self.resolve(force_refresh=bool(force), refresh_skew_seconds=REFRESH_SKEW_S)
            except TypeError:        # an older Hermes without refresh_skew_seconds
                creds = self.resolve(force_refresh=bool(force))
            self.token = creds["access_token"]
            self.expires = _expiry(creds.get("expires_at")) or time.time() + 600
        return self.token


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
    sys.path.insert(0, SKILL_SRC)
    import db, sampler
    db.DB_PATH = DB_PATH
    db.init_db()
    sampler.run_forever(TokenSource(resolve_spotify_runtime_credentials))
    return 0


if __name__ == "__main__":
    sys.exit(main())