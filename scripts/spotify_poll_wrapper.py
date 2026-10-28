#!/usr/bin/env python3
"""
Spotify polling wrapper — polls listening events and stores them in SQLite.

Gets a fresh access token from Hermes Agent's auth layer, writes it into
~/.hermes/auth.json *atomically*, then runs poller.poll_listening_events().
On any error it logs the detail to stderr and prints a single clean line to
stdout instead of letting a raw traceback become the delivered message body.
"""
import sys, os, json, traceback
from datetime import datetime, timezone, timedelta

HERMES_AGENT = "/home/amh/.hermes/hermes-agent"
SKILL_SRC    = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src"
DB_PATH      = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"
AUTH_PATH    = os.path.expanduser("~/.hermes/auth.json")


def _auth_lock():
    """Hermes' cross-process auth.json lock (a no-op if this build lacks it)."""
    try:
        from hermes_cli.auth import _auth_store_lock
        return _auth_store_lock()
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _update_auth_token(access_token):
    """Atomically write the fresh Spotify token into auth.json (temp + fsync +
    os.replace) so a crash mid-write can't corrupt the shared auth.json."""
    # Under Hermes' cross-process auth lock: the resident playback sampler
    # refreshes the Spotify token under it, and an unlocked read-modify-write
    # here could revert a just-rotated refresh token. Hermes' resolver has
    # already saved this token, so if the lock can't be had the rewrite is
    # skipped rather than failing the poll / refresh.
    lock = _auth_lock()
    try:
        lock.__enter__()
    except Exception as e:
        print(f"auth.json lock unavailable ({type(e).__name__}: {e}); "
              f"token already saved by Hermes — skipping the rewrite", file=sys.stderr)
        return
    try:
        try:
            with open(AUTH_PATH) as f:
                auth_data = json.load(f)
        except Exception as e:
            # Do NOT fall back to {}: rewriting a spotify-only file would atomically
            # wipe every other provider's credentials. Abort and leave auth.json as-is.
            raise RuntimeError(f"refusing to rewrite unreadable auth.json: {e}")
        providers = auth_data.get("providers", {}) or {}
        sp = providers.get("spotify", {}) or {}
        sp["access_token"] = access_token
        sp["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        providers["spotify"] = sp
        auth_data["providers"] = providers
        tmp = AUTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(auth_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, AUTH_PATH)
    finally:
        lock.__exit__(None, None, None)


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
    _update_auth_token(creds["access_token"])

    sys.path.insert(0, SKILL_SRC)
    import db, poller
    db.DB_PATH = DB_PATH
    db.init_db()
    print(poller.poll_listening_events())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        print(f"spotify poll failed: {type(e).__name__}: {e}")
        sys.exit(1)
