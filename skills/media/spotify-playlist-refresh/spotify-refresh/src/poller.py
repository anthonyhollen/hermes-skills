"""
poller.py — Entry point: poll_listening_events()
Fetches recently-played, infers ms_played from the gap back to the previous
play (played_at = play END), caches track metadata, persists to DB.
"""

import sys
from datetime import datetime, timezone
import db, client

MAX_EVENTS_PER_POLL = 50
MAX_MS_PLAYED_INFERRED = 6 * 3600 * 1000  # 6h cap — above this = user stopped listening


class PollError(RuntimeError):
    """recently-played fetch failed; `status` is the HTTP/API status (0 = network)."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def poll_listening_events():
    """
    Fetch the last MAX_EVENTS_PER_POLL plays from Spotify, infer ms_played
    using inter-event timestamp gaps, cache track metadata, store events to DB.
    Returns a summary dict. Any failure writes an 'error' poll run_log row
    before re-raising.
    """
    started_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        return _poll(started_at)
    except Exception as e:
        try:
            db.log_run("poll", started_at, int(datetime.now(timezone.utc).timestamp() * 1000),
                       "error", {"detail": f"{type(e).__name__}: {e}"})
        except Exception:
            pass  # never mask the original failure
        raise


def _poll(started_at: int) -> dict:
    token = client.get_token()

    # ── 1. Fetch recently-played ────────────────────────────────────────────
    resp = client.get(f"me/player/recently-played?limit={MAX_EVENTS_PER_POLL}", token=token)
    if "error" in resp:
        err = resp["error"] if isinstance(resp.get("error"), dict) else {}
        raise PollError(err.get("status") or 0, f"recently-played failed: {resp['error']}")

    raw_events = resp.get("items", [])
    if not raw_events:
        db.log_run("poll", started_at, int(datetime.now(timezone.utc).timestamp() * 1000), "ok",
                   {"events_fetched": 0, "events_stored": 0})
        return {"events_fetched": 0, "events_stored": 0}

    # ── 2. Parse + sort by played_at ascending ─────────────────────────────
    parsed = []
    for item in raw_events:
        track = item.get("track", {})
        if not track or not track.get("id"):
            continue
        parsed.append({
            "track_id":   track["id"],
            "name":       track.get("name"),
            "artist_ids": [a["id"] for a in track.get("artists", []) if a.get("id")],
            "artist_names": [a["name"] for a in track.get("artists", [])],
            "duration_ms": track.get("duration_ms", 0),
            "album_name":  (track.get("album") or {}).get("name"),
            "popularity":  track.get("popularity"),
            "played_at":   _parse_ts(item.get("played_at")),
            "context_uri": (item.get("context") or {}).get("uri"),
        })

    parsed.sort(key=lambda x: (x["played_at"], x["track_id"]))  # same tie order as the migration

    # ── 3. Infer ms_played for all events using inter-event gaps ────────────
    # `played_at` is when a play ENDED (verified on 1,276 production gaps: the
    # gap to the previous event matched THIS track's duration 911×, the next
    # track's 11×). So each event's listen time is the gap back to the previous
    # event. The oldest event in this response takes its predecessor from the
    # DB (the newest stored event before it) — normally the previous poll's
    # last event, so values are identical across overlapping polls.
    prev_played_at = db.get_latest_played_at_before(parsed[0]["played_at"]) if parsed else None
    inferred = _infer_ms_played(parsed, prev_played_at)

    # ── 4. Cache new tracks / backfill album_name ─────────────────────────────
    # cache_tracks_from_recently_played caches unseen tracks and re-upserts rows
    # missing album_name (cached before the column existed), so the core/fallback
    # paths can apply the album-name exclusion rule without a bulk backfill job.
    cache_tracks_from_recently_played(parsed, token)

    # ── 5. Persist to DB ────────────────────────────────────────────────────
    # events_stored = rows actually NEW this poll (was len(inferred), which is
    # always the fetch size). If every fetched event is new, the 50-event window
    # did not reach back to the previous poll → plays may have been lost.
    stored = db.upsert_play_events(inferred)
    detail = {"events_fetched": len(parsed), "events_stored": stored}
    if parsed and stored == len(parsed) and len(raw_events) >= MAX_EVENTS_PER_POLL \
            and prev_played_at is not None:
        detail["possible_truncation"] = True
        print(f"⚠️  poll: all {stored} fetched events were new — recently-played window "
              f"may not reach the previous poll (plays lost)", file=sys.stderr)

    # ── 6. Rebuild derived skip signals (best-effort, never fails the poll) ─
    # Late-arriving plays change the fallback inference, so the derived table is
    # rebuilt from scratch on every poll; see skip_signals.py.
    try:
        import skip_signals
        detail["signals"] = skip_signals.rebuild_signals().get("signals")
    except Exception as e:
        detail["signals_error"] = f"{type(e).__name__}: {e}"
        print(f"⚠️  poll: skip-signal rebuild failed: {e}", file=sys.stderr)

    finished_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    db.log_run("poll", started_at, finished_at, "ok", detail)

    return detail


def _parse_ts(ts: str) -> int:
    """Parse ISO8601 timestamp → unix milliseconds."""
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _infer_ms_played(events: list[dict], prev_played_at: int | None = None) -> list[dict]:
    """
    `played_at` marks the END of a play, so for event i (events sorted ascending):
      ms_played_i = min(duration_ms_i, played_at_i − played_at_{i−1})
    The predecessor of events[0] is `prev_played_at` (the newest stored event
    before it); with none (first-ever poll) events[0] stays None (unscored).
    Special cases: gap < 0 (clock skew) or gap > 6h (listening resumed after a
    break — the gap says nothing about this play) → treat as a full play;
    gap == 0 (two plays stamped the same ms — not a real listen time) → None.
    """
    result = []
    prev = prev_played_at
    for ev in events:
        if prev is None:
            ms_played = None
        else:
            gap_ms = ev["played_at"] - prev
            if gap_ms < 0 or gap_ms > MAX_MS_PLAYED_INFERRED:
                gap_ms = ev["duration_ms"]
            ms_played = min(ev["duration_ms"], gap_ms) if gap_ms != 0 else None
        prev = ev["played_at"]
        result.append({
            "track_id":   ev["track_id"],
            "played_at":   ev["played_at"],
            "duration_ms": ev["duration_ms"],
            "ms_played":   ms_played,
            "context_uri": ev.get("context_uri"),
        })
    return result


def cache_tracks_from_recently_played(events: list[dict], token: str):
    """
    Cache track metadata using data already in the recently-played response.
    No extra API calls needed — we have name, artist_ids, duration_ms from the response.
    Audio features and popularity are no longer cached (endpoints deprecated Nov 2024).
    """
    track_map: dict[str, dict] = {}
    for ev in events:
        tid = ev.get("track_id")
        if tid and tid not in track_map:
            track_map[tid] = ev

    existing = db.get_cached_tracks(list(track_map.keys()))
    # New tracks → cache them. Tracks already cached but missing album_name
    # (cached before the album_name column existed) → re-upsert with the value
    # from this response; this self-heals the core/fallback paths without a
    # bulk backfill job.
    to_cache = [
        tid for tid in track_map
        if tid not in existing or not (existing[tid].get("album_name") or "")
    ]
    if not to_cache:
        return

    records = []
    for tid in to_cache:
        ev = track_map[tid]
        records.append({
            "track_id":    tid,
            "name":        ev.get("name"),
            "artist_ids":  ev.get("artist_ids", []),
            "duration_ms": ev.get("duration_ms"),
            "album_name":  ev.get("album_name"),
        })

    if records:
        db.upsert_track_cache(records)


if __name__ == "__main__":
    result = poll_listening_events()
    print(f"poll_listening_events → {result}")