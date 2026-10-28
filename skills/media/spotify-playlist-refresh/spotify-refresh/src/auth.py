"""
auth.py — OAuth bootstrap + headless token refresh for Spotify.
Primary token source: ~/.hermes/auth.json (Hermes Agent token, refreshed by the
cron wrappers each run). Fallback: SQLite oauth_token table (bootstrap_interactive).
"""

import json, os, subprocess, time
from datetime import datetime, timezone
import db

CLIENT_ID    = os.environ.get("SPOTIFY_CLIENT_ID", "c3616d872c07443980727a2beef2e936")
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
REDIRECT_URI  = "http://127.0.0.1:8888/callback"
TOKEN_URL     = "https://accounts.spotify.com/api/token"
EXPIRY_SOFT_SECS = 60   # refresh if expires within 60s

# ── helpers ────────────────────────────────────────────────────────────────────

def _curl_post(data: dict) -> dict:
    cmd = [
        "curl", "-s", "--max-time", "30", "--connect-timeout", "10", "-X", "POST",
        "-H", "Content-Type: application/x-www-form-urlencoded",
    ]
    if CLIENT_SECRET:
        cmd += ["-u", f"{CLIENT_ID}:{CLIENT_SECRET}"]
    body = "&".join(f"{k}={v}" for k, v in data.items())
    try:
        result = subprocess.run(cmd + [TOKEN_URL, "-d", body],
                                capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "raw": "token endpoint timed out"}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"error": "parse_failed", "raw": result.stdout[:200]}


def _load_hermes_token() -> str | None:
    """Read access token from Hermes Agent's auth.json (fallback for initial use)."""
    try:
        with open(os.path.expanduser("~/.hermes/auth.json")) as f:
            data = json.load(f)
        providers = data.get("providers", {}) or {}
        sp = providers.get("spotify", {}) or data.get("spotify", {})
        return sp.get("access_token")
    except Exception:
        return None


def _load_hermes_refresh_token() -> str | None:
    """Read refresh token from Hermes Agent's auth.json."""
    try:
        with open(os.path.expanduser("~/.hermes/auth.json")) as f:
            data = json.load(f)
        providers = data.get("providers", {}) or {}
        sp = providers.get("spotify", {}) or data.get("spotify", {})
        return sp.get("refresh_token")
    except Exception:
        return None


def _hermes_scopes() -> list[str]:
    """Scopes available in the Hermes token."""
    try:
        with open(os.path.expanduser("~/.hermes/auth.json")) as f:
            data = json.load(f)
        providers = data.get("providers", {}) or {}
        sp = providers.get("spotify", {}) or data.get("spotify", {})
        scope_str = sp.get("granted_scope", "")
        return scope_str.split() if scope_str else []
    except Exception:
        return []


# ── public API ────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    """
    Returns an access token.
    Priority: Hermes auth.json (the cron wrappers write a freshly refreshed token
    there at the start of every run) > SQLite oauth_token (legacy bootstrap path).
    The DB path used to win, so any stale oauth_token row would shadow the fresh
    wrapper token and trigger a doomed client_id-less PKCE refresh per call.
    """
    # 1. Hermes auth.json
    token = _load_hermes_token()
    if token:
        return token

    # 2. Fallback: DB (bootstrap_interactive flow)
    token_data = db.get_oauth_token()
    if token_data:
        expires_at_ms = token_data["expires_at"]
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if expires_at_ms - now_ms > EXPIRY_SOFT_SECS * 1000:
            return token_data["access_token"]
        new_token = _do_refresh(token_data["refresh_token"])
        if new_token:
            return new_token

    raise RuntimeError("No Spotify token found. Run: hermes auth spotify")


def _do_refresh(refresh_token: str) -> str | None:
    """Attempt token refresh. Returns new access token on success, None on failure."""
    resp = _curl_post({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if "error" in resp:
        return None
    access_token  = resp.get("access_token")
    if not access_token:
        return None  # malformed 200 without an error envelope — treat as failure
    new_refresh   = resp.get("refresh_token", refresh_token)  # Spotify may rotate this
    expires_in_s  = resp.get("expires_in", 3600)
    expires_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + expires_in_s * 1000
    db.save_oauth_token(access_token, new_refresh, expires_at_ms)
    return access_token


def ensure_valid_token() -> str:
    """Alias for get_access_token — guarantees a valid token or raises."""
    return get_access_token()


def bootstrap_interactive() -> str:
    """
    One-time interactive OAuth authorization code flow.
    Prints URL, waits for user to visit and copy the code from redirect.
    Saves token to DB on success.
    """
    import urllib.parse
    scopes = [
        "user-read-recently-played",
        "user-top-read",
        "user-library-read",
        "user-library-modify",
        "playlist-modify-private",
        "playlist-modify-public",
        "playlist-read-private",
        "user-read-private",
        "user-read-email",
    ]
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "show_dialog": "true",
    }
    url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
    print(f"\n🔑 Open this URL in your browser:\n\n  {url}\n")
    code = input("Paste the code from the redirect URL: ").strip()
    resp = _curl_post({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    if "error" in resp:
        raise RuntimeError(f"Auth failed: {resp}")
    access_token  = resp["access_token"]
    refresh_token = resp["refresh_token"]
    expires_in_s  = resp.get("expires_in", 3600)
    expires_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000) + expires_in_s * 1000
    db.save_oauth_token(access_token, refresh_token, expires_at_ms)
    return access_token