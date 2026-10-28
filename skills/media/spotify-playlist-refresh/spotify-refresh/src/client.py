"""
client.py — Shared HTTP client for Spotify API.
Handles: auth header, JSON, retry with exponential backoff, 429 rate-limit handling.
Market code for artist-graph calls (cached one-time via GET /v1/me).
"""

import time, json, subprocess, os
import auth

BASE_URL = "https://api.spotify.com/v1"
MAX_RETRIES = 6
BACKOFF_BASE = 3.0  # seconds

# ── user market (one-time cache) ─────────────────────────────────────────────

_USER_MARKET: str | None = None

def get_user_market(token: str | None = None) -> str:
    """
    Returns the user's country/market code (e.g. 'SG', 'US') from GET /v1/me.
    Cached after first call — pass market to all artist-graph endpoints.
    """
    global _USER_MARKET
    if _USER_MARKET:
        return _USER_MARKET
    if token is None:
        token = auth.get_access_token()
    if _time_left() < _ATTEMPT_BUDGET_S:
        _USER_MARKET = "SG"  # no budget for the lookup — use the fallback
        return _USER_MARKET
    resp, _, _ = _do_request("GET", "me", token)
    # Cache the fallback too, so a failure costs one request per run, not one per caller.
    _USER_MARKET = "SG" if "error" in resp else resp.get("country", "SG")
    return _USER_MARKET

# ── low-level curl wrapper ────────────────────────────────────────────────────

def _do_request(method: str, endpoint: str, token: str, body: dict | None = None, max_time: int = 30):
    """
    Makes one HTTP request via curl. Returns (parsed_json_or_error_dict, http_status, retry_after):
      - http_status: the real HTTP code (0 on timeout / connection failure)
      - retry_after: the Retry-After header in seconds if present, else None
    Does NOT retry — use api() below for automatic retry. max_time = curl
    --max-time (the playback sampler uses a short one).
    """
    url = f"{BASE_URL}/{endpoint}"
    cmd = [
        "curl", "-s", "--max-time", str(max_time), "--connect-timeout", str(min(10, max_time)),
        "-w", "\n__META__%{http_code}|%header{retry-after}",
        "-X", method.upper(),
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
    ]
    if body:
        cmd += ["-d", json.dumps(body)]

    try:
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=max_time + 5)
    except subprocess.TimeoutExpired:
        return {"error": {"status": 0, "message": "request timed out"}}, 0, None

    out = result.stdout or ""
    http_status, retry_after, body_text = 0, None, out
    idx = out.rfind("\n__META__")
    if idx != -1:
        body_text = out[:idx]
        code, _, ra = out[idx + len("\n__META__"):].partition("|")
        try:
            http_status = int(code)
        except ValueError:
            http_status = 0
        ra = ra.strip()
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = None
    try:
        parsed = json.loads(body_text)
    except Exception:
        parsed = {"error": {"status": http_status or 0, "message": f"parse_error: {body_text[:200]}"}}
    return parsed, http_status, retry_after


def _do_request_raw(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """Low-level request to an arbitrary URL (used for non-Spotify endpoints like token refresh)."""
    cmd = [
        "curl", "-s", "--max-time", "30", "--connect-timeout", "10",
        "-X", method.upper(),
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
    ]
    if body:
        cmd += ["-d", json.dumps(body)]
    try:
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=35)
    except subprocess.TimeoutExpired:
        return {}
    try:
        return json.loads(result.stdout)
    except Exception:
        return {}


# ── run budget + in-run GET memo ─────────────────────────────────────────────
# A cron run has a hard 600s watchdog. _DEADLINE (time.monotonic() value, or
# None = unbounded) is set by the refresher per stage; api() never starts an
# attempt or a backoff sleep it can't finish before it, and returns an error
# instead — so a rate-limited artist graph degrades to fewer candidates rather
# than getting the run SIGKILLed before the playlist write.
_DEADLINE: float | None = None
_ATTEMPT_BUDGET_S = 36.0     # curl --max-time 30 + subprocess timeout slack
MAX_BACKOFF_S = 30.0         # cap for computed backoff (429 without Retry-After, 5xx)
# A Retry-After is honored in full when it fits the stage deadline; if it doesn't
# (or with no deadline set — e.g. the standalone poll cron — if it exceeds
# MAX_RETRY_AFTER_S, keeping the old ≤30s-per-sleep worst case) the call fails fast,
# and GETs skip the network until the ban lifts (_RATE_LIMITED_UNTIL) instead of
# each re-poking a banned endpoint. Non-GETs (the playlist PUT) always try.
MAX_RETRY_AFTER_S = 30.0
_RATE_LIMITED_UNTIL = 0.0

# Successful GETs of artist/album catalogue endpoints are memoized for the life
# of the process: the discovery / top-tracks / wildcard paths ask for the same
# artists/{id}/albums and albums/{id}/tracks URLs several times per run.
_MEMO_PREFIXES = ("artists/", "albums/")
_GET_MEMO: dict[str, dict] = {}

# Catalogue GETs (the same artists/ + albums/ endpoints) are paced at least
# CATALOGUE_MIN_INTERVAL_S apart: a cold artist graph is ~60 requests, which
# fired in a 20–40s burst is over the ~50 req/min "Other" limit. Paced, a cold
# run costs ~75s of the compose budget; warm runs make almost none.
CATALOGUE_MIN_INTERVAL_S = 1.2
_last_catalogue_at = float("-inf")


# ── catalogue guard (v2.20.2) ────────────────────────────────────────────────
# 2026-10-07: a daytime dry-run burst (~85 search / artists / albums calls at
# ~50/min, 18 h after a ~230-call night burst) drew a 4.8 h Retry-After on the
# catalogue endpoints. Spotify documents only a rolling 30 s window, not the
# escalation, so every catalogue GET from ANY process now passes a persistent
# guard in SQLite (db.api_call_log / db.api_guard):
#   - a remembered ban: a catalogue 429 with Retry-After blocks catalogue calls
#     in every process until it expires (+ ban_margin_s) — never call into a ban
#   - cross-process spacing: ≥ min_gap_s between catalogue calls
#   - rolling caps: per_hour / per_day
# A refusal is returned as a 429-shaped error, which every caller already treats
# as "no data" (fewer candidates / a partial batch), never as a crash. Player,
# playlist and /me endpoints are not guarded. Config: config.yaml → api_guard.
CATALOGUE_PREFIXES = ("search", "artists", "albums", "tracks", "recommendations", "browse/",
                      "audio-features", "audio-analysis")      # incl. batch ?ids= forms
_GUARD_DEFAULTS = {"enabled": True, "min_gap_s": 2.5, "per_hour": 120, "per_day": 200,
                   "ban_margin_s": 600, "max_wait_s": 30, "log_days": 3}
GUARD = dict(_GUARD_DEFAULTS)


def _load_guard_config():
    """config.yaml → api_guard over the defaults; each value is coerced to the
    default's type, and one that won't parse keeps that key's default."""
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
        if not os.path.exists(path):
            return
        import yaml
        with open(path) as f:
            g = (yaml.safe_load(f) or {}).get("api_guard") or {}
    except Exception:
        return                 # a bad config block keeps the safe defaults
    if not isinstance(g, dict):
        return
    for k, default in _GUARD_DEFAULTS.items():
        v = g.get(k)
        if v is None:
            continue
        try:
            if isinstance(default, bool):
                GUARD[k] = v if isinstance(v, bool) else str(v).strip().lower() not in ("false", "no", "off", "0")
            else:
                GUARD[k] = type(default)(float(v)) if isinstance(default, int) else float(v)
        except (TypeError, ValueError):
            pass


_load_guard_config()


def _is_catalogue(endpoint: str) -> bool:
    return endpoint.startswith(CATALOGUE_PREFIXES)


def guard_state(now_ms: float | None = None) -> dict:
    """{blocked_until, last_hour, last_day, last_ms} for the catalogue guard."""
    import db
    now = now_ms or time.time() * 1000
    with db.get_conn(timeout=5) as conn:
        b = conn.execute("SELECT value FROM api_guard WHERE key='catalogue_blocked_until'").fetchone()
        hour = conn.execute("SELECT COUNT(*) FROM api_call_log WHERE t_ms > ?", (now - 3600e3,)).fetchone()[0]
        day = conn.execute("SELECT COUNT(*) FROM api_call_log WHERE t_ms > ?", (now - 86400e3,)).fetchone()[0]
        last = conn.execute("SELECT MAX(t_ms) FROM api_call_log").fetchone()[0]
    return {"blocked_until": float(b[0]) if b and b[0] else 0.0, "last_hour": hour, "last_day": day,
            "last_ms": float(last or 0)}


def _guard_acquire(endpoint: str) -> tuple[str | None, int | None]:
    """(refusal reason, None) or (None, reservation rowid). Sleeps for the
    cross-process spacing outside any transaction (the sampler's 2 s busy timeout)."""
    if not GUARD.get("enabled", True):
        return None, None
    import db
    path = endpoint.split("?", 1)[0]
    gap = float(GUARD["min_gap_s"])
    for _ in range(20):
        st = guard_state()
        now = time.time() * 1000
        if st["blocked_until"] > now:
            return (f"Spotify catalogue ban remembered until "
                    f"{time.strftime('%H:%M', time.localtime(st['blocked_until'] / 1000))}"), None
        if st["last_hour"] >= int(GUARD["per_hour"]):
            return f"hourly cap ({GUARD['per_hour']} catalogue calls)", None
        if st["last_day"] >= int(GUARD["per_day"]):
            return f"daily cap ({GUARD['per_day']} catalogue calls)", None
        delta = st["last_ms"] + gap * 1000 - now
        wait = delta / 1000 if 0 < delta <= gap * 2000 else 0   # far-future rows = the clock stepped back: ignore
        if wait > 0:
            if wait > min(float(GUARD["max_wait_s"]), _time_left() - _ATTEMPT_BUDGET_S):
                return "spacing wait would overrun the deadline", None
            time.sleep(wait)
            continue
        with db.get_conn(timeout=5) as conn:
            conn.execute("BEGIN IMMEDIATE")
            last = conn.execute("SELECT MAX(t_ms) FROM api_call_log").fetchone()[0] or 0
            now = time.time() * 1000
            if now < last + gap * 1000 < now + gap * 2000:
                continue                    # another process took the slot — wait again
                                            # (a row far in the future = clock skew: ignored)
            cur = conn.execute("INSERT INTO api_call_log (t_ms, endpoint) VALUES (?,?)", (now, path))
            conn.execute("DELETE FROM api_call_log WHERE t_ms < ?", (now - float(GUARD["log_days"]) * 86400e3,))
            return None, cur.lastrowid
    return "guard busy", None


def _guard_finish(rowid: int | None, status: int, retry_after: float | None):
    """Record the outcome; remember a catalogue ban for every process."""
    if rowid is None:
        return
    import db
    hold = None
    if status == 429:
        try:
            ra = float(retry_after or 0)
        except (TypeError, ValueError):
            ra = 0.0
        hold = max(ra, 60.0) + float(GUARD["ban_margin_s"])
    for attempt in range(3 if hold else 1):     # a ban must be saved: retry a busy DB
        try:
            with db.get_conn(timeout=15 if hold else 5) as conn:
                conn.execute("UPDATE api_call_log SET status=? WHERE rowid=?", (status, rowid))
                if hold:
                    until = time.time() * 1000 + hold * 1000
                    # CAST both sides: value is TEXT, and MAX(real, text) is the text
                    conn.execute(
                        """INSERT INTO api_guard (key, value, updated_at) VALUES ('catalogue_blocked_until', ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             value = MAX(CAST(api_guard.value AS REAL), CAST(excluded.value AS REAL)),
                             updated_at = excluded.updated_at""", (until, time.time() * 1000))
            return
        except Exception:
            time.sleep(0.5)        # bookkeeping must never fail the call


def set_deadline(seconds_from_now: float | None):
    """Bound all subsequent api() calls to finish within `seconds_from_now` (None = unbounded)."""
    global _DEADLINE
    _DEADLINE = None if seconds_from_now is None else time.monotonic() + seconds_from_now


def _time_left() -> float:
    return float("inf") if _DEADLINE is None else _DEADLINE - time.monotonic()


# ── public API with retry + backoff ─────────────────────────────────────────

def api(method: str, endpoint: str, token: str | None = None, body: dict | None = None) -> dict:
    """
    Spotify API call with automatic retry on 429 / 5xx / transient network failures.
    - 429: honor Retry-After if it fits the deadline (≤ MAX_RETRY_AFTER_S when
      unbounded), else fail fast and trip the GET rate-limit breaker
    - 5xx and transient failures (timeout / connection error / unparseable body): backoff + retry
    - Other errors (400/401/403/404): pass through immediately
    - Never sleeps after the final attempt, and never starts an attempt or sleep
      that would overrun the stage deadline (set_deadline) — returns an error instead.
    """
    if token is None:
        token = auth.ensure_valid_token()

    global _RATE_LIMITED_UNTIL
    is_get = method.upper() == "GET"
    memo_key = endpoint if is_get and endpoint.startswith(_MEMO_PREFIXES) else None
    if memo_key and memo_key in _GET_MEMO:
        return _GET_MEMO[memo_key]
    if is_get and time.monotonic() < _RATE_LIMITED_UNTIL:
        return {"error": {"status": 429, "message": "rate limited (breaker open)"}}

    global _last_catalogue_at
    status = 0
    guarded = is_get and _is_catalogue(endpoint)
    for attempt in range(MAX_RETRIES):
        if _time_left() < _ATTEMPT_BUDGET_S:
            return {"error": {"status": status or 0, "message": "run deadline reached"}}
        if memo_key and CATALOGUE_MIN_INTERVAL_S > 0:
            gap = _last_catalogue_at + CATALOGUE_MIN_INTERVAL_S - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            _last_catalogue_at = time.monotonic()
        rowid = None
        if guarded:
            try:
                refusal, rowid = _guard_acquire(endpoint)
            except Exception as e:          # an unusable guard DB must not open the floodgates
                refusal = f"guard unavailable ({type(e).__name__})"
            if refusal:
                return {"error": {"status": 429, "message": f"local guard: {refusal}"}}
        resp, http_status, retry_after = _do_request(method, endpoint, token, body)
        if guarded:
            body_err = resp.get("error") if isinstance(resp, dict) else None
            g_status = (body_err.get("status") if isinstance(body_err, dict) else None) or http_status
            _guard_finish(rowid, g_status, retry_after)
            if g_status == 429:
                # remembered for every process (longer than Retry-After) — don't sleep it out here
                _RATE_LIMITED_UNTIL = time.monotonic() + max(float(retry_after or 0), 60.0)
                return {"error": {"status": 429, "message": f"rate limited (catalogue ban remembered; "
                                                            f"Retry-After {retry_after})"}}

        if not resp:
            return {"error": {"status": 0, "message": "empty response"}}

        error = resp.get("error", {})
        if not error:
            if memo_key:
                _GET_MEMO[memo_key] = resp
            return resp  # success

        # Authoritative status: the body's error.status if present, else the real HTTP code.
        body_status = error.get("status") if isinstance(error, dict) else None
        status = body_status or http_status or 0

        if status == 429:
            if retry_after is not None:
                wait = max(1.0, float(retry_after))  # 0/negative → still pause
                limit = MAX_RETRY_AFTER_S if _DEADLINE is None else _time_left() - _ATTEMPT_BUDGET_S
                if wait > limit:
                    _RATE_LIMITED_UNTIL = time.monotonic() + wait
                    return {"error": {"status": 429, "message": f"rate limited (Retry-After {wait:.0f}s)"}}
            else:
                wait = min(BACKOFF_BASE * (2 ** attempt), MAX_BACKOFF_S)
        elif (500 <= status < 600) or status == 0:
            # 5xx, or a transient failure that yielded no usable status (timeout /
            # connection error / non-JSON body) — retry with bounded exponential backoff.
            wait = min(BACKOFF_BASE * (2 ** attempt), 20.0)
        else:
            # Other error (400, 401, 403, 404) — fail immediately.
            return resp

        if attempt == MAX_RETRIES - 1:
            break  # no attempt left — don't sleep for nothing
        if _time_left() < float(wait) + _ATTEMPT_BUDGET_S:
            return {"error": {"status": status or 0, "message": "run deadline reached"}}
        time.sleep(float(wait))

    return {"error": {"status": status or 0, "message": "max retries exceeded"}}


def get(endpoint: str, token: str | None = None) -> dict:
    return api("GET", endpoint, token)

def post(endpoint: str, body: dict | None = None, token: str | None = None) -> dict:
    return api("POST", endpoint, token, body)

def put(endpoint: str, body: dict | None = None, token: str | None = None) -> dict:
    return api("PUT", endpoint, token, body)

def delete(endpoint: str, body: dict | None = None, token: str | None = None) -> dict:
    return api("DELETE", endpoint, token, body)

def get_token():
    return auth.get_access_token()