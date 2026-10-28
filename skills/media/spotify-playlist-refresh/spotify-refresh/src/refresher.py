"""
refresher.py — Entry point: refresh_playlist()
Orchestrates: poll → score → compose → playlist replace.
Handles: error handling, run_log, affinity snapshot write + prune.
"""

import os, sys
from datetime import datetime, timezone, timedelta
import db, client, poller, scorer, composer

# Load config for snapshot retention_days and vacuum_weekday
_config = {}
_config_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
if os.path.exists(_config_path):
    import yaml
    with open(_config_path) as f:
        _config = yaml.safe_load(f) or {}

# `or {}` at each level so a present-but-null block in config.yaml (valid YAML →
# None) can't AttributeError at import and take down the whole refresh cron.
_snapshot_cfg = (_config.get("scoring") or {}).get("snapshot") or {}
SNAPSHOT_RETENTION_DAYS = _snapshot_cfg.get("retention_days", 90)
VACUUM_WEEKDAY = _snapshot_cfg.get("vacuum_weekday", 6)  # Sunday=6

PLAYLIST_ID = "4y4Fd7Pg52Ubi0zqJBNJnm"

# ── public API ────────────────────────────────────────────────────────────────

# Stage budgets (seconds) — the gateway SIGKILLs a no_agent script at 600s
# (cron.script_timeout_seconds). The best-effort poll gets POLL_BUDGET_S (a long
# Retry-After on recently-played can't eat the compose budget); score + compose
# must finish inside COMPOSE_BUDGET_S; the logging-only snapshot GET gets
# SNAPSHOT_BUDGET_S; the playlist PUT then gets its own WRITE_BUDGET_S — so no
# earlier stage can starve the PUT. Sum 540s leaves ~60s for the wrapper's
# token refresh, DB work and the weekly vacuum.
POLL_BUDGET_S = 60
COMPOSE_BUDGET_S = 290
SNAPSHOT_BUDGET_S = 40
WRITE_BUDGET_S = 150


def refresh_playlist():
    try:
        return _refresh_playlist()
    finally:
        client.set_deadline(None)  # don't leak the stage deadline to later callers


def _refresh_playlist():
    """
    Full daily refresh:
      1. Poll listening events (best-effort — a failed poll scores existing history)
      2. Compute affinity scores
      3. Compose the target-size playlist (config: playlist.target_size)
      4. Capture snapshot_id, PUT replacement, log result
    Every failure before the playlist write is recorded as an 'error' run_log row.
    """
    started_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        client.set_deadline(POLL_BUDGET_S)
        token = client.get_token()

        # ── 1. Poll ─────────────────────────────────────────────────────────
        # Best-effort: the DB already holds the scoring window's history, so a
        # transient recently-played failure (5xx / timeout) should cost at most
        # the last few hours of plays, not the whole day's refresh. Auth
        # failures (401/403) still abort — the playlist PUT would fail too.
        try:
            poller.poll_listening_events()
        except Exception as e:
            if getattr(e, "status", None) in (401, 403) or not isinstance(e, poller.PollError):
                raise  # logged by the outer handler
            # (the poller already wrote its own 'error' poll row)
            print(f"⚠️  poll failed, scoring existing history: {e}", file=sys.stderr)

        # ── 2. Score ─────────────────────────────────────────────────────────
        client.set_deadline(COMPOSE_BUDGET_S)
        # Skip signals must reflect the latest sampler segments even when the
        # poll failed or returned nothing (best-effort; no API calls).
        try:
            import skip_signals
            skip_signals.rebuild_signals()
        except Exception as e:
            print(f"⚠️  skip-signal rebuild failed, using the last one: {e}", file=sys.stderr)
        scores, computed_at_ms = scorer.compute_affinity()
        newly_blocked = scorer.update_blocklist(scores)

        # ── 3. Compose ───────────────────────────────────────────────────────
        tracks = composer.compose_playlist(scores, dry_run=False)
        llm_info = dict(getattr(composer, "LAST_INFO", None) or {})   # v2.19 DeepSeek slots (empty when off)
    except Exception as e:
        _log_error("refresh", started_at, f"failed before playlist write: {type(e).__name__}: {e}")
        raise

    if not tracks:
        _log_error("refresh", started_at, "composer returned 0 tracks")
        return {"status": "error", "reason": "composer_empty"}

    # ── 4. Replace playlist ──────────────────────────────────────────────────
    uris = [t["uri"] for t in tracks if t.get("uri")]
    if not uris:
        # Guard on uris, not just tracks: a composition whose tracks all lack a
        # uri would otherwise PUT {"uris": []} and wipe the playlist.
        _log_error("refresh", started_at, "no playable uris in composed tracks")
        return {"status": "error", "reason": "no_uris"}

    client.set_deadline(SNAPSHOT_BUDGET_S)
    snapshot_id = _get_snapshot_id(token)  # logging only; None on any failure
    client.set_deadline(WRITE_BUDGET_S)
    try:
        snapshot_after = _replace_tracks(token, uris)
    except Exception as e:
        _log_error("refresh", started_at, f"playlist replace failed: {e}")
        raise

    # The playlist IS updated from here on — nothing below may report failure.
    finished_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    detail = {
        "snapshot_id_before": snapshot_id,
        "snapshot_id_after": snapshot_after,
        "tracks_added": len(uris),
        "scores_computed": len(scores),
        "newly_blocked": newly_blocked,
    }
    if llm_info:
        detail["llm"] = llm_info
    run_id = None
    try:
        run_id = db.log_run("refresh", started_at, finished_at, "ok", detail)
    except Exception as e:
        print(f"⚠️  run_log write failed (playlist already updated): {e}", file=sys.stderr)

    # v2.18: remember the exact order written — skip inference maps plays to
    # playlist positions. Observability only: never fails the refresh.
    try:
        db.save_playlist_version([(t["uri"].split(":")[-1], t.get("source"), t.get("name"),
                                   ", ".join(a for a in (t.get("artist_names") or [])[:2] if a))
                                  for t in tracks if t.get("uri")],
                                 applied_at=finished_at, snapshot_id=snapshot_after, run_id=run_id)
    except Exception as e:
        print(f"⚠️  playlist_version write failed (playlist already updated): {e}", file=sys.stderr)

    # ── 5. Write affinity_snapshot + prune (observability only) ────────────
    # Isolated like the run_log write above: a SQLite "database is locked" or
    # the weekly vacuum must NOT turn a successful refresh into a reported
    # failure. Uses the run_id returned by the INSERT directly (get_last_runs
    # orders by started_at and could pick the wrong run on a tie).
    try:
        if run_id:
            db.write_snapshot(run_id, scores, computed_at_ms)
            db.prune_snapshot(SNAPSHOT_RETENTION_DAYS)

            # Conditional incremental_vacuum (weekly, on VACUUM_WEEKDAY in SGT)
            today_weekday = (datetime.now(timezone.utc) + timedelta(hours=8)).weekday()  # SGT = UTC+8, no DST
            if today_weekday == VACUUM_WEEKDAY:
                with db.get_conn() as vac_conn:
                    vac_conn.execute("PRAGMA incremental_vacuum")
    except Exception as e:
        print(f"⚠️  post-refresh snapshot/prune/vacuum failed "
              f"(playlist already updated): {e}", file=sys.stderr)

    return {"status": "ok", "tracks": len(uris), "target": composer.TARGET_SIZE,
            "scores": len(scores), "llm_note": llm_info.get("note")}


# ── internal ─────────────────────────────────────────────────────────────────

def _get_snapshot_id(token: str) -> str | None:
    resp = client.get(f"playlists/{PLAYLIST_ID}?fields=snapshot_id", token=token)
    if "error" in resp:
        return None
    return resp.get("snapshot_id")


def _replace_tracks(token: str, uris: list[str]) -> str | None:
    """
    Atomically replace the playlist's contents in one PUT (≤100 uris), so a
    failure leaves the old playlist intact instead of an empty playlist (the
    old clear-then-add sequence wiped the playlist if the add half failed).
    Overflow beyond 100 is appended with POST batches.
    Returns the snapshot_id of the last successful write (None if absent).
    """
    first, rest = uris[:100], uris[100:]
    resp = client.put(f"playlists/{PLAYLIST_ID}/items", body={"uris": first}, token=token)
    if "error" in resp:
        raise RuntimeError(f"replace failed: {resp['error']}")
    snapshot = resp.get("snapshot_id")
    for i in range(0, len(rest), 100):
        batch = rest[i:i+100]
        resp = client.post(f"playlists/{PLAYLIST_ID}/items", body={"uris": batch}, token=token)
        if "error" in resp:
            raise RuntimeError(f"add failed: {resp['error']}")
        snapshot = resp.get("snapshot_id") or snapshot
    return snapshot


def _log_error(run_type: str, started_at: int, detail: str):
    """Best-effort error row — never masks the original failure."""
    finished_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        db.log_run(run_type, started_at, finished_at, "error", {"detail": detail})
    except Exception as e:
        print(f"⚠️  run_log error-row write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    result = refresh_playlist()
    print(f"refresh_playlist → {result}")