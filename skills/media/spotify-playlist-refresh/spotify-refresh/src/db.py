"""
db.py — SQLite schema init + CRUD helpers for Spotify playlist refresh.
Schema: play_events, track_cache, blocklist, oauth_token, run_log, affinity_snapshot,
plus the v2.18 skip-signal tables (playlist_version*, player_sample, play_segment,
sampler_coverage, sampler_state, skip_signal, skip_label) and the v2.19 DeepSeek
discovery tables (llm_batch, llm_pick, artist_name, release_scan, album_tracks_cache) and
the Spotify catalogue guard (api_call_log, api_guard) — see references/schema.md.
WAL mode enabled on every connection. affinity_snapshot is write-only for observability.
"""

import sqlite3, json, os
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.path.expanduser("~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS play_events (
    track_id    TEXT    NOT NULL,
    played_at   INTEGER NOT NULL,   -- unix ms
    ms_played   INTEGER,            -- inferred from the gap to the previous play (played_at = play END); NULL if unknown
    duration_ms INTEGER NOT NULL,
    context_uri TEXT,               -- recently-played context (e.g. spotify:playlist:…); NULL before v2.18
    PRIMARY KEY (track_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_events_track  ON play_events(track_id);
CREATE INDEX IF NOT EXISTS idx_events_time   ON play_events(played_at);

CREATE TABLE IF NOT EXISTS track_cache (
    track_id       TEXT PRIMARY KEY,
    name           TEXT,
    artist_ids     TEXT,             -- JSON array of artist ID strings
    duration_ms    INTEGER,
    cached_at      INTEGER,          -- unix ms
    album_name     TEXT              -- album title at last poll; drives the
                                     -- output-only album-name exclusion rule
);

CREATE TABLE IF NOT EXISTS artist_cache (
    artist_id      TEXT PRIMARY KEY,
    name           TEXT,
    top_tracks     TEXT,             -- JSON array of slim track dicts (id, name, uri, duration_ms, artists, album_name) from _fetch_artist_top_tracks
    albums         TEXT,             -- JSON array of {album_id, name, release_date, tracks?} from /artists/{id}/albums
    cached_at      INTEGER           -- unix ms; stale after 36h (ARTIST_CACHE_TTL_MS)
);

CREATE TABLE IF NOT EXISTS blocklist (
    track_id  TEXT PRIMARY KEY,
    reason    TEXT,
    banned_at INTEGER               -- unix ms
);

CREATE TABLE IF NOT EXISTS oauth_token (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT,
    refresh_token TEXT,
    expires_at    INTEGER            -- unix ms
);

CREATE TABLE IF NOT EXISTS run_log (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type    TEXT  NOT NULL,      -- 'poll' | 'refresh'
    started_at  INTEGER NOT NULL,    -- unix ms
    finished_at INTEGER,              -- unix ms
    status      TEXT  NOT NULL,      -- 'ok' | 'error'
    detail      TEXT                  -- JSON
);

CREATE TABLE IF NOT EXISTS affinity_snapshot (
    run_id      INTEGER NOT NULL,    -- FK to run_log.run_id
    track_id    TEXT    NOT NULL,
    affinity    REAL    NOT NULL,
    computed_at INTEGER NOT NULL,     -- unix ms
    PRIMARY KEY (run_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_time  ON affinity_snapshot(computed_at);
CREATE INDEX IF NOT EXISTS idx_snapshot_track ON affinity_snapshot(track_id);

-- ── v2.18 skip signals ──────────────────────────────────────────────────────
-- Every composition actually written to the playlist (positions 1..N).
CREATE TABLE IF NOT EXISTS playlist_version (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at  INTEGER NOT NULL,    -- unix ms the PUT succeeded (bootstrap: that refresh's finish)
    snapshot_id TEXT,
    run_id      INTEGER,
    origin      TEXT                 -- 'refresh' | 'bootstrap'
);
CREATE TABLE IF NOT EXISTS playlist_version_track (
    version_id INTEGER NOT NULL,
    position   INTEGER NOT NULL,     -- 1-based
    track_id   TEXT    NOT NULL,
    source     TEXT,                 -- core / discovery / wildcard / fallback
    name       TEXT,                 -- display name (discovery tracks are not in track_cache)
    artists    TEXT,                 -- display artists "A, B" (track_cache stores only artist ids)
    PRIMARY KEY (version_id, position)
);
-- Raw GET /me/player samples (non-idle only), kept 14 days for calibration.
CREATE TABLE IF NOT EXISTS player_sample (
    t_ms INTEGER NOT NULL, status TEXT NOT NULL, track_id TEXT, progress_ms INTEGER,
    duration_ms INTEGER, context_uri TEXT, shuffle INTEGER, smart_shuffle INTEGER,
    repeat_state TEXT, device_type TEXT, spotify_ts INTEGER, rtt_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sample_time ON player_sample(t_ms);
-- One row per observed track exposure (skipsig.Segmenter), kept 180 days.
CREATE TABLE IF NOT EXISTS play_segment (
    segment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id     TEXT NOT NULL,
    data         TEXT NOT NULL,      -- full segment JSON (bounds, listened, flags, …)
    first_seen   INTEGER NOT NULL,
    exit_hi      INTEGER NOT NULL,
    end_reason   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_segment_key ON play_segment(track_id, first_seen);
CREATE INDEX IF NOT EXISTS idx_segment_exit ON play_segment(exit_hi);
-- Time ranges the sampler was actively observing (≤ a few poll intervals apart).
CREATE TABLE IF NOT EXISTS sampler_coverage (
    from_ms INTEGER PRIMARY KEY,
    to_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sampler_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER
);
-- Derived — fully rebuilt by signals.rebuild_signals(); never hand-edit.
CREATE TABLE IF NOT EXISTS skip_signal (
    track_id       TEXT    NOT NULL,
    at_ms          INTEGER NOT NULL,
    kind           TEXT    NOT NULL,  -- completed/skip/skim/pass/early_exit/stopped/neutral/inferred_skip/unknown_play/coverage_miss
    source         TEXT    NOT NULL,  -- 'sampler' | 'fallback'
    tier           TEXT,
    exposure_ms    INTEGER,
    exposure_hi_ms INTEGER,
    duration_ms    INTEGER,
    end_reason     TEXT,
    version_id     INTEGER,
    position       INTEGER,
    context_uri    TEXT,
    segment_id     INTEGER,
    played_at      INTEGER,           -- matched recorded play (play_events key), if any
    score_ms       INTEGER,           -- listened ms the scorer uses (flag on); NULL = unscored
    alt_track_id   TEXT,              -- the other id of a relinked track (player id vs recorded id)
    PRIMARY KEY (track_id, at_ms, kind, source)
);
CREATE INDEX IF NOT EXISTS idx_signal_time ON skip_signal(at_ms);
-- Ground truth from the user's reply to a digest: every position of the version,
-- 'explicit' = the user's correction, 'accepted' = the digest's call left
-- uncorrected. Position 0 holds the review marker ('ok') and its labeled_at.
CREATE TABLE IF NOT EXISTS skip_label (
    version_id INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    label      TEXT    NOT NULL,      -- skip | not | maybe | ok
    track_id   TEXT,
    labeled_at INTEGER,
    origin     TEXT,                  -- explicit | accepted
    PRIMARY KEY (version_id, position)
);

-- ── v2.19 DeepSeek discovery (llm_discovery.py) ─────────────────────────────
-- One row per discovery job run (01:20 / 02:20 SGT); the composer reads the
-- resolved llm_pick rows of 'ok' / 'partial' batches.
CREATE TABLE IF NOT EXISTS llm_batch (
    batch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL,    -- unix ms
    finished_at INTEGER,
    model       TEXT,
    thinking    INTEGER,             -- 1 = the picks came from the thinking call
    angle       TEXT,
    status      TEXT    NOT NULL,    -- running | ok | partial | error | killed
    detail      TEXT                 -- JSON: usage, cost_usd, timings, funnel, errors
);
CREATE TABLE IF NOT EXISTS llm_pick (
    batch_id   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,     -- pick | similar | new_release
    rank       INTEGER NOT NULL,     -- the model's order (new_release: scan order)
    artist     TEXT,                 -- as the model wrote it (new_release: Spotify's)
    title      TEXT,                 -- similar: the song the model named to identify the artist
    fit        TEXT,                 -- close | adjacent
    tier       TEXT,                 -- rookie | midsize | major
    why        TEXT,
    norm_key   TEXT,                 -- compact(artist)|compact(title): resolution cache key
    status     TEXT    NOT NULL,     -- resolved | unresolved | filtered
    reason     TEXT,                 -- why not resolved (no_match, near_miss, known_artist, heard, jpop, …)
    track_id   TEXT,
    artist_id  TEXT,                 -- primary artist (similar: the resolved artist)
    track      TEXT,                 -- JSON composer candidate (+ release_date, album_type)
    created_at INTEGER NOT NULL,
    PRIMARY KEY (batch_id, kind, rank)
);
CREATE INDEX IF NOT EXISTS idx_llm_pick_track ON llm_pick(track_id);
CREATE INDEX IF NOT EXISTS idx_llm_pick_key   ON llm_pick(norm_key);
-- Release-scan cache (v2.20.1): an artist's album list is re-fetched at most every
-- release_rescan_hours; album track lists never change, so they are kept.
CREATE TABLE IF NOT EXISTS release_scan (
    artist_id  TEXT PRIMARY KEY,
    scanned_at INTEGER NOT NULL,     -- unix ms
    albums     TEXT                  -- JSON [{id, name, release_date, album_type}] as listed
);
CREATE TABLE IF NOT EXISTS album_tracks_cache (
    album_id  TEXT PRIMARY KEY,
    tracks    TEXT NOT NULL,         -- JSON slim tracks (composer._slim_track)
    cached_at INTEGER NOT NULL
);
-- Spotify catalogue guard (v2.20.2, client.py): every search / artists / albums /
-- tracks GET from any process is reserved here first (cross-process spacing +
-- rolling caps), and a catalogue 429's Retry-After is remembered in api_guard so
-- no process calls into a ban.
CREATE TABLE IF NOT EXISTS api_call_log (
    t_ms     INTEGER NOT NULL,       -- unix ms the call was reserved
    endpoint TEXT,                   -- path without the query string
    status   INTEGER                 -- HTTP status (NULL = not finished)
);
CREATE INDEX IF NOT EXISTS idx_api_call_time ON api_call_log(t_ms);
CREATE TABLE IF NOT EXISTS api_guard (
    key        TEXT PRIMARY KEY,     -- 'catalogue_blocked_until'
    value      TEXT,
    updated_at INTEGER
);
-- v2.23 DeepSeek budget: every chat call through llm_discovery._post_json (any process)
-- reserves its worst case here first; a rolling 24 h sum over budget_tokens_24h is refused.
-- used NULL = in flight / outcome unknown (the reservation counts).
CREATE TABLE IF NOT EXISTS llm_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ms      INTEGER NOT NULL,
    caller    TEXT,
    reserved  INTEGER NOT NULL,
    used      INTEGER,
    status    TEXT NOT NULL          -- reserved | done | failed | timeout | refused
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_time ON llm_usage(t_ms);
-- Artist display names (track_cache stores only ids): the taste profile sent to the model.
CREATE TABLE IF NOT EXISTS artist_name (
    artist_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT,                 -- segment | artist_cache | search | api
    updated_at INTEGER
);
"""


def _open(timeout: float = 30):
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")  # must be set before any tables exist
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn(timeout: float = 30):
    """timeout = SQLite busy wait; the resident sampler passes a short one so a
    long cron transaction can't stall its poll loop (it retries later)."""
    conn = _open(timeout)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """
    In-place schema migrations for pre-existing DBs. CREATE TABLE IF NOT EXISTS
    never alters an existing table, so new columns on old tables need ALTER.
    Each step is idempotent (guarded by a column check).
    """
    # 2026-08-28: album_name column on track_cache (album-name exclusion rule).
    # Appended last so positional indices used by _row_to_cache (SELECT *) stay
    # stable for pre-migration rows.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(track_cache)").fetchall()}
    if "album_name" not in cols:
        _add_column(conn, "ALTER TABLE track_cache ADD COLUMN album_name TEXT")
    # 2026-09-30 (v2.18): recently-played context on play_events (skip inference).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(play_events)").fetchall()}
    if "context_uri" not in cols:
        _add_column(conn, "ALTER TABLE play_events ADD COLUMN context_uri TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(playlist_version_track)").fetchall()}
    if "name" not in cols:
        _add_column(conn, "ALTER TABLE playlist_version_track ADD COLUMN name TEXT")
    if "artists" not in cols:
        _add_column(conn, "ALTER TABLE playlist_version_track ADD COLUMN artists TEXT")


def _add_column(conn, sql: str):
    """ALTER TABLE … ADD COLUMN that tolerates losing a race: the sampler service
    and a cron wrapper can both run init_db() on an un-migrated DB at once."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


# ── play_events ───────────────────────────────────────────────────────────────

def upsert_play_events(events: list[dict]) -> int:
    """
    events: list of dicts with keys track_id, played_at (unix ms),
    duration_ms, ms_played (can be None).
    Returns the number of rows that were NEW (not already stored).

    On a duplicate (track_id, played_at), a real (non-NULL) ms_played overwrites
    the stored value; NULL never overwrites. With played_at = play END an
    event's value is fixed by its predecessor, so overlapping polls recompute
    the same value. (The old "only ever lower" rule assumed start-time
    semantics and would block legitimate upward corrections.)
    """
    rows = [(e["track_id"], e["played_at"], e.get("ms_played"), e["duration_ms"], e.get("context_uri"))
            for e in events]
    if not rows:
        return 0
    with get_conn() as conn:
        placeholders = ",".join("(?,?)" for _ in rows)
        keys = [v for r in rows for v in (r[0], r[1])]
        existing = conn.execute(
            f"SELECT COUNT(*) FROM play_events WHERE (track_id, played_at) IN (VALUES {placeholders})",
            keys,
        ).fetchone()[0]
        conn.executemany(
            """INSERT INTO play_events (track_id, played_at, ms_played, duration_ms, context_uri)
               VALUES (?,?,?,?,?)
               ON CONFLICT(track_id, played_at) DO UPDATE SET
                   ms_played   = COALESCE(excluded.ms_played, play_events.ms_played),
                   context_uri = COALESCE(excluded.context_uri, play_events.context_uri)""",
            rows
        )
    return len({(r[0], r[1]) for r in rows}) - existing

def get_latest_played_at_before(played_at: int) -> int | None:
    """played_at of the newest stored event strictly before `played_at` (the
    predecessor used to infer the oldest event's ms_played in a poll)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(played_at) FROM play_events WHERE played_at < ?", (played_at,)
        ).fetchone()
    return row[0] if row and row[0] is not None else None

def get_last_event_for_track(track_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM play_events WHERE track_id = ? ORDER BY played_at DESC LIMIT 1",
            (track_id,)
        ).fetchone()
    if not row:
        return None
    return {"track_id": row[0], "played_at": row[1], "ms_played": row[2], "duration_ms": row[3]}

def update_ms_played(track_id: str, played_at: int, ms_played: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE play_events SET ms_played = ? WHERE track_id = ? AND played_at = ?",
            (ms_played, track_id, played_at)
        )

def get_events_since(after_ms: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM play_events WHERE played_at > ? ORDER BY played_at ASC",
            (after_ms,)
        ).fetchall()
    return [_row_to_event(r) for r in rows]

def get_recent_events(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM play_events ORDER BY played_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [_row_to_event(r) for r in rows]

def _row_to_event(row) -> dict:
    return {"track_id": row[0], "played_at": row[1], "ms_played": row[2], "duration_ms": row[3],
            "context_uri": row[4] if len(row) > 4 else None}

# ── track_cache ───────────────────────────────────────────────────────────────

def upsert_track_cache(tracks: list[dict]):
    """
    tracks: list of dicts with track_id, name, artist_ids (list), duration_ms,
    album_name (optional). Popularity / audio_features are no longer cached
    (endpoints deprecated Nov 2024). album_name comes from the recently-played
    response (or later backfill) and powers the output-only album-name
    exclusion rule only — it is not used for scoring.
    """
    rows = [
        (
            t["track_id"],
            t.get("name"),
            json.dumps(t.get("artist_ids", [])),
            t.get("duration_ms"),
            int(datetime.now(timezone.utc).timestamp() * 1000),
            t.get("album_name"),
        )
        for t in tracks
    ]
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO track_cache
               (track_id, name, artist_ids, duration_ms, cached_at, album_name)
               VALUES (?,?,?,?,?,?)""",
            rows
        )

_IN_CHUNK = 500     # SQLite caps bound variables (32,766); "every heard track" grows without bound


def get_cached_tracks(track_ids: list[str]) -> dict[str, dict]:
    if not track_ids:
        return {}
    ids, rows = list(track_ids), []
    with get_conn() as conn:
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i:i + _IN_CHUNK]
            rows += conn.execute(
                f"SELECT * FROM track_cache WHERE track_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    result = {}
    for row in rows:
        result[row[0]] = _row_to_cache(row)
    return result

def _row_to_cache(row) -> dict:
    return {
        "track_id":   row[0],
        "name":       row[1],
        "artist_ids": json.loads(row[2]) if row[2] else [],
        "duration_ms": row[3],
        "cached_at":  row[4],
        # album_name only exists post-migration (column 5); len guard keeps
        # reads of pre-migration rows working.
        "album_name": row[5] if len(row) > 5 else None,
    }

# ── artist_cache ─────────────────────────────────────────────────────────────

# 36h, not 24h: the refresh runs every 24h, so a 24h TTL expired a few seconds
# before every 03:00 run and each refresh fetched the whole artist graph cold.
ARTIST_CACHE_TTL_MS = 36 * 3600 * 1000

def upsert_artist_cache(artists: list[dict]):
    """
    artists: list of dicts with artist_id, name, top_tracks (list of slim track
    dicts), albums (list of {album_id, name, release_date[, tracks]}), cached_at.
    """
    rows = [
        (
            a["artist_id"],
            a.get("name"),
            json.dumps(a.get("top_tracks", [])),
            json.dumps(a.get("albums", [])),
            int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        for a in artists
    ]
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO artist_cache
               (artist_id, name, top_tracks, albums, cached_at)
               VALUES (?,?,?,?,?)""",
            rows
        )

def get_cached_artists(artist_ids: list[str], include_stale: bool = False) -> dict[str, dict]:
    """Cached artist rows younger than ARTIST_CACHE_TTL_MS (or all rows with
    include_stale=True — the fallback when a refetch fails)."""
    if not artist_ids:
        return {}
    ids, rows = list(artist_ids), []
    with get_conn() as conn:
        for i in range(0, len(ids), _IN_CHUNK):
            chunk = ids[i:i + _IN_CHUNK]
            rows += conn.execute(
                f"SELECT * FROM artist_cache WHERE artist_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    result = {row[0]: _row_to_artist(row) for row in rows}
    if include_stale:
        return result
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        aid: v for aid, v in result.items()
        if (now_ms - (v.get("cached_at") or 0)) < ARTIST_CACHE_TTL_MS
    }

def _row_to_artist(row) -> dict:
    return {
        "artist_id":  row[0],
        "name":       row[1],
        "top_tracks": json.loads(row[2]) if row[2] else [],
        "albums":     json.loads(row[3]) if row[3] else [],
        "cached_at":  row[4],
    }

# ── blocklist ─────────────────────────────────────────────────────────────────

def add_to_blocklist(track_id: str, reason: str):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocklist (track_id, reason, banned_at) VALUES (?,?,?)",
            (track_id, reason, now)
        )

def is_blocked(track_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocklist WHERE track_id = ?", (track_id,)
        ).fetchone()
    return row is not None

def get_blocklist() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT track_id FROM blocklist").fetchall()
    return [r[0] for r in rows]

# ── oauth_token ────────────────────────────────────────────────────────────────

def save_oauth_token(access_token: str, refresh_token: str, expires_at_ms: int):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO oauth_token (id, access_token, refresh_token, expires_at)
               VALUES (1, ?,?,?)""",
            (access_token, refresh_token, expires_at_ms)
        )

def get_oauth_token() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM oauth_token WHERE id = 1").fetchone()
    if not row:
        return None
    return {"access_token": row[1], "refresh_token": row[2], "expires_at": row[3]}

# ── run_log ────────────────────────────────────────────────────────────────────

def log_run(run_type: str, started_at_ms: int, finished_at_ms: int, status: str, detail: dict) -> int:
    """Insert a run_log row and return its autoincrement run_id (use this id
    directly rather than re-reading via get_last_runs, which orders by
    started_at and can return the wrong run on a tie / out-of-order insert)."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO run_log (run_type, started_at, finished_at, status, detail)
               VALUES (?,?,?,?,?)""",
            (run_type, started_at_ms, finished_at_ms, status, json.dumps(detail))
        )
        return cur.lastrowid

def get_last_runs(run_type: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM run_log WHERE run_type = ?
               ORDER BY started_at DESC LIMIT ?""",
            (run_type, limit)
        ).fetchall()
    cols = ["run_id", "run_type", "started_at", "finished_at", "status", "detail"]
    return [dict(zip(cols, r)) for r in rows]


# ── affinity_snapshot ────────────────────────────────────────────────────────

def write_snapshot(run_id: int, scores: dict[str, float], computed_at_ms: int):
    """
    Write per-track affinity scores as observability records for a given run.
    Only non-zero tracks are written. Independent of prune_snapshot: the two
    touch disjoint row sets (this INSERTs the current run; prune DELETEs rows
    older than the retention window), so they need not share a transaction.
    """
    rows = [(run_id, tid, score, computed_at_ms) for tid, score in scores.items() if score != 0]
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO affinity_snapshot (run_id, track_id, affinity, computed_at) VALUES (?,?,?,?)",
            rows
        )


def prune_snapshot(retention_days: int = 90):
    """
    Delete affinity_snapshot rows older than retention_days.
    Returns the count of deleted rows.
    """
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - retention_days * 86400 * 1000
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM affinity_snapshot WHERE computed_at < ?", (cutoff_ms,))
    return cur.rowcount


def get_snapshot_trend(track_id: str, limit: int = 90) -> list[dict]:
    """Return affinity history for one track, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT computed_at, affinity FROM affinity_snapshot
               WHERE track_id = ? ORDER BY computed_at DESC LIMIT ?""",
            (track_id, limit)
        ).fetchall()
    return [{"computed_at": r[0], "affinity": r[1]} for r in rows]


def get_latest_snapshot(limit: int = 50) -> list[dict]:
    """Return top N tracks by affinity from the most recent refresh run."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(run_id) FROM run_log WHERE run_type='refresh'"
        ).fetchone()
    if not row or not row[0]:
        return []
    last_run_id = row[0]
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.track_id, s.affinity, t.name
               FROM affinity_snapshot s
               LEFT JOIN track_cache t USING(track_id)
               WHERE s.run_id = ?
               ORDER BY s.affinity DESC LIMIT ?""",
            (last_run_id, limit)
        ).fetchall()
    # track_cache does not store artist_names (artist endpoints deprecated Nov 2024),
    # so it is reported as empty rather than joined.
    return [
        {"track_id": r[0], "affinity": r[1], "name": r[2], "artist_names": []}
        for r in rows
    ]

# ── stats helpers ──────────────────────────────────────────────────────────────

def get_event_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM play_events").fetchone()
    return row[0] if row else 0

def get_track_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM track_cache").fetchone()
    return row[0] if row else 0


# ── v2.18 skip signals ──────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def save_playlist_version(tracks: list[tuple], applied_at: int,
                          snapshot_id: str | None = None, run_id: int | None = None,
                          origin: str = "refresh") -> int:
    """Record one composition as written to the playlist: tracks =
    [(track_id, source[, name[, artists]])] in playlist order. Returns the new version_id."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO playlist_version (applied_at, snapshot_id, run_id, origin) VALUES (?,?,?,?)",
            (applied_at, snapshot_id, run_id, origin))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO playlist_version_track (version_id, position, track_id, source, name, artists) "
            "VALUES (?,?,?,?,?,?)",
            [(vid, i + 1, t[0], t[1] if len(t) > 1 else None, t[2] if len(t) > 2 else None,
              (t[3] or None) if len(t) > 3 else None)
             for i, t in enumerate(tracks) if t and t[0]])
    return vid


def set_version_track_meta(version_id: int, meta: dict[str, tuple]):
    """Fill missing display name / artists of a version's rows: meta = {track_id: (name, artists)}."""
    if not meta:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """UPDATE playlist_version_track SET name = COALESCE(name, ?), artists = COALESCE(artists, ?)
               WHERE version_id = ? AND track_id = ?""",
            [(n or None, a or None, version_id, tid) for tid, (n, a) in meta.items()])
    return len(meta)


def get_playlist_versions(since_ms: int = 0, timeout: float = 30) -> list[dict]:
    """[{version_id, applied_at, snapshot_id, origin, tracks:[track_id…], sources:[…], names:[…],
    artists:[…]}], oldest first; includes the last version applied before since_ms."""
    with get_conn(timeout) as conn:
        heads = conn.execute(
            """SELECT version_id, applied_at, snapshot_id, origin FROM playlist_version
               WHERE applied_at >= COALESCE((SELECT MAX(applied_at) FROM playlist_version
                                             WHERE applied_at < ?), 0)
               ORDER BY applied_at, version_id""", (since_ms,)).fetchall()
        out = []
        for vid, applied_at, snap, origin in heads:
            rows = conn.execute(
                "SELECT track_id, source, name, artists FROM playlist_version_track "
                "WHERE version_id=? ORDER BY position", (vid,)).fetchall()
            out.append({"version_id": vid, "applied_at": applied_at, "snapshot_id": snap,
                        "origin": origin, "tracks": [r[0] for r in rows], "sources": [r[1] for r in rows],
                        "names": [r[2] for r in rows], "artists": [r[3] for r in rows]})
    return out


def insert_samples(samples: list[dict], timeout: float = 30):
    rows = [_sample_row(s) for s in samples]
    if not rows:
        return
    with get_conn(timeout) as conn:
        conn.executemany(
            """INSERT INTO player_sample (t_ms, status, track_id, progress_ms, duration_ms, context_uri,
                   shuffle, smart_shuffle, repeat_state, device_type, spotify_ts, rtt_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def insert_segments(segments: list[dict], timeout: float = 30):
    """Closed segments (skipsig.Segmenter output). Idempotent on (track_id, first_seen)."""
    if not segments:
        return
    with get_conn(timeout) as conn:
        _insert_segments(conn, segments)


def _insert_segments(conn, segments: list[dict]):
    rows = [(s["track_id"], json.dumps(s, separators=(",", ":")), s["first_seen"], s["exit_hi"],
             s.get("end_reason")) for s in segments]
    if rows:
        conn.executemany(
            """INSERT OR IGNORE INTO play_segment (track_id, data, first_seen, exit_hi, end_reason)
               VALUES (?,?,?,?,?)""", rows)   # first close wins (a crash-resumed stale copy can't overwrite)


def get_segments_since(since_ms: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT segment_id, data FROM play_segment WHERE exit_hi >= ? ORDER BY first_seen",
            (since_ms,)).fetchall()
    out = []
    for sid, data in rows:
        d = json.loads(data)
        d["segment_id"] = sid
        out.append(d)
    return out


def extend_coverage(from_ms: int, to_ms: int, timeout: float = 30):
    """Upsert one sampler coverage run keyed by its start."""
    with get_conn(timeout) as conn:
        conn.execute(
            """INSERT INTO sampler_coverage (from_ms, to_ms) VALUES (?,?)
               ON CONFLICT(from_ms) DO UPDATE SET to_ms = MAX(to_ms, excluded.to_ms)""",
            (from_ms, to_ms))


def get_coverage_since(since_ms: int) -> list[tuple[int, int]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT from_ms, to_ms FROM sampler_coverage WHERE to_ms >= ? ORDER BY from_ms",
            (since_ms,)).fetchall()
    return [(a, b) for a, b in rows]


def prune_sampler_data(sample_days: int = 14, segment_days: int = 180, timeout: float = 30):
    now = _now_ms()
    with get_conn(timeout) as conn:
        conn.execute("DELETE FROM player_sample WHERE t_ms < ?", (now - sample_days * 86400000,))
        conn.execute("DELETE FROM play_segment WHERE exit_hi < ?", (now - segment_days * 86400000,))
        conn.execute("DELETE FROM sampler_coverage WHERE to_ms < ?", (now - segment_days * 86400000,))


def get_sampler_state(key: str, timeout: float = 30):
    with get_conn(timeout) as conn:
        row = conn.execute("SELECT value FROM sampler_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def set_sampler_state(key: str, value, timeout: float = 30):
    with get_conn(timeout) as conn:
        conn.execute(
            """INSERT INTO sampler_state (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, json.dumps(value, separators=(",", ":")), _now_ms()))


_SIGNAL_COLS = ("track_id", "at_ms", "kind", "source", "tier", "exposure_ms", "exposure_hi_ms",
                "duration_ms", "end_reason", "version_id", "position", "context_uri",
                "segment_id", "played_at", "score_ms", "alt_track_id")


def replace_skip_signals(rows: list[dict]):
    """Atomically replace the whole derived skip_signal table."""
    with get_conn() as conn:
        conn.execute("DELETE FROM skip_signal")
        conn.executemany(
            f"INSERT OR REPLACE INTO skip_signal ({','.join(_SIGNAL_COLS)}) "
            f"VALUES ({','.join('?' * len(_SIGNAL_COLS))})",
            [tuple(r.get(c) for c in _SIGNAL_COLS) for r in rows])


def get_skip_signals_since(since_ms: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {','.join(_SIGNAL_COLS)} FROM skip_signal WHERE at_ms >= ? ORDER BY at_ms",
            (since_ms,)).fetchall()
    return [dict(zip(_SIGNAL_COLS, r)) for r in rows]


def set_labels(version_id: int, labels: list[tuple], labeled_at: int | None = None):
    """labels = [(position, label, track_id, origin)] — the full truth set for a
    version (position 0 = the 'ok' review marker). Replaces earlier labels for it."""
    now = labeled_at or _now_ms()
    with get_conn() as conn:
        conn.execute("DELETE FROM skip_label WHERE version_id=?", (version_id,))
        conn.executemany(
            "INSERT INTO skip_label (version_id, position, label, track_id, labeled_at, origin) VALUES (?,?,?,?,?,?)",
            [(version_id, pos, lab, tid, now, origin) for pos, lab, tid, origin in labels])


def get_labels() -> dict[int, dict]:
    """{version_id: {"labeled_at": ms, "labels": {position: label}, "explicit": {positions},
    "silent": bool}} — silent = no reply, accepted as correct (v2.20.4 user rule)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT version_id, position, label, labeled_at, origin FROM skip_label").fetchall()
    out: dict[int, dict] = {}
    for vid, pos, lab, at, origin in rows:
        d = out.setdefault(vid, {"labeled_at": at, "labels": {}, "explicit": set(), "silent": False})
        d["labeled_at"] = max(d["labeled_at"] or 0, at or 0)
        if pos:
            d["labels"][pos] = lab
            if origin == "explicit":
                d["explicit"].add(pos)
        elif origin == "silent":
            d["silent"] = True
    return out


def sampler_flush(samples: list[dict], segments: list[dict], coverage: list[tuple[int, int]],
                  state: dict | None, timeout: float = 2):
    """Everything the sampler persists, in ONE transaction (one WAL commit per flush)."""
    now = _now_ms()
    with get_conn(timeout) as conn:
        _insert_segments(conn, segments)
        if samples:
            conn.executemany(
                """INSERT INTO player_sample (t_ms, status, track_id, progress_ms, duration_ms, context_uri,
                       shuffle, smart_shuffle, repeat_state, device_type, spotify_ts, rtt_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", [_sample_row(s) for s in samples])
        for a, b in coverage:
            conn.execute(
                """INSERT INTO sampler_coverage (from_ms, to_ms) VALUES (?,?)
                   ON CONFLICT(from_ms) DO UPDATE SET to_ms = MAX(to_ms, excluded.to_ms)""", (a, b))
        for key, value in (state or {}).items():
            conn.execute(
                """INSERT INTO sampler_state (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, json.dumps(value, separators=(",", ":")), now))


def _sample_row(s: dict) -> tuple:
    return (s["t"], s["status"], s.get("track_id"), s.get("progress_ms"), s.get("duration_ms"),
            s.get("context_uri"), None if s.get("shuffle") is None else int(bool(s["shuffle"])),
            None if s.get("smart_shuffle") is None else int(bool(s["smart_shuffle"])),
            s.get("repeat"), s.get("device_type"), s.get("spotify_ts"), s.get("rtt"))


# ── artist_name (v2.19) ──────────────────────────────────────────────────────

def upsert_artist_names(pairs: list[tuple], timeout: float = 30) -> int:
    """pairs = [(artist_id, name, source)]; blank ids / names are ignored.
    Returns the number of rows written."""
    now = _now_ms()
    rows = [(aid, name, src, now) for aid, name, src in pairs if aid and name]
    if not rows:
        return 0
    with get_conn(timeout) as conn:
        conn.executemany(
            """INSERT INTO artist_name (artist_id, name, source, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(artist_id) DO UPDATE SET name=excluded.name, source=excluded.source,
                   updated_at=excluded.updated_at""", rows)
    return len(rows)


def get_artist_names(artist_ids: list[str] | None = None) -> dict[str, str]:
    """{artist_id: name} from artist_name (all rows when artist_ids is None)."""
    with get_conn() as conn:
        if artist_ids is None:
            rows = conn.execute("SELECT artist_id, name FROM artist_name").fetchall()
        else:
            ids = list(artist_ids)
            rows = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                rows += conn.execute(
                    f"SELECT artist_id, name FROM artist_name WHERE artist_id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()
    return {aid: name for aid, name in rows}
