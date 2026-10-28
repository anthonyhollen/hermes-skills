"""
scorer.py — Affinity scoring + blocklist management.
Implements the spec's scoring model:
  - completion = ms_played / duration_ms
  - weight by completion bracket
  - recency decay: 0.5 ^ (age_days / half_life_days)
  - per-track affinity = Σ event_score for events in window
  - Like (≥ +1.0) / Dislike (≤ −1.0) / Neutral buckets
"""

import math, os
from datetime import datetime, timezone
import db, exclusions, skip_signals

# ── Scoring params — read from config.yaml; defaults below are the live values ──
# (Previously these were hardcoded and config.yaml was ignored — they drifted.
#  config.yaml is now the single source of truth; edit it to tune scoring.)
_cfg = {}
_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
if os.path.exists(_cfg_path):
    import yaml
    with open(_cfg_path) as _f:
        _cfg = (yaml.safe_load(_f) or {}).get("scoring", {}) or {}

_w = _cfg.get("weights", {}) or {}
WEIGHT_FULL_LISTEN    = _w.get("full_listen",    1.0)   # completion >= 0.90
WEIGHT_MOSTLY_LISTEN  = _w.get("mostly_listen",  0.5)   # 0.50 <= completion < 0.90
WEIGHT_PARTIAL_LISTEN = _w.get("partial_listen", 0.1)   # 0.30 <= completion < 0.50
WEIGHT_SKIP           = _w.get("skip",          -1.0)   # 0.10 <= completion < 0.30
WEIGHT_HARD_SKIP      = _w.get("hard_skip",     -1.5)   # completion < 0.10

RECENCY_HALF_LIFE_DAYS = _cfg.get("half_life_days", 7)   # shortened from 14 for faster preference feedback
WINDOW_DAYS            = _cfg.get("window_days",    7)   # only consider events in last N days
LIKE_THRESHOLD         = _cfg.get("like_threshold",    1.0)
DISLIKE_THRESHOLD      = _cfg.get("dislike_threshold", -1.0)

# Max track duration to consider (skip podcasts / mixes > 10 min)
MAX_DURATION_MS = 10 * 60 * 1000


def compute_affinity(window_days: int = WINDOW_DAYS, now_ms: int | None = None,
                     use_signals: bool | None = None) -> tuple[dict[str, float], int]:
    """
    Compute per-track affinity scores from all events in the DB.
    Only events within `window_days` are considered.
    Returns (scores_dict, computed_at_ms) where computed_at_ms is the
    unix-timestamp when scoring was done (used for snapshot write).

    use_signals (default: config.yaml → skip_signals.enabled) folds in the v2.18
    skip signals (skip_signals.scoring_inputs): sampler-observed listen time
    replaces the gap inference for matched plays, sampler-observed plays Spotify
    never recorded are added, and uncovered plays after an unexplained pause are
    not scored. With it off this function is exactly the pre-v2.18 scorer.
    """
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cutoff_ms = now_ms - window_days * 86400 * 1000
    if use_signals is None:
        use_signals = skip_signals.enabled()

    # Load events in window
    events = db.get_events_since(cutoff_ms)
    if use_signals:
        overrides, derived = skip_signals.scoring_inputs(cutoff_ms)
        for ev in events:
            key = (ev["track_id"], ev["played_at"])
            if key in overrides:
                ev["ms_played"] = overrides[key]
        events = events + [d for d in derived if d["played_at"] > cutoff_ms]
    if not events:
        return {}, now_ms

    # Load blocklist — skip blocklisted tracks
    blocked = set(db.get_blocklist())

    # Excluded artists (config.yaml → exclude.artist_ids); fetched once for the loop.
    excluded_artists = exclusions.EXCLUDED_ARTIST_IDS

    # Load cached duration for each track
    track_ids = list({e["track_id"] for e in events})
    cached = db.get_cached_tracks(track_ids)

    # Accumulate affinity per track
    scores: dict[str, float] = {}

    for ev in events:
        tid = ev["track_id"]
        if tid in blocked:
            continue

        ctrack = cached.get(tid, {})

        # Category exclusion (baby-sleep music etc.): drop BEFORE scoring so the
        # track never earns affinity and its artists never become discovery/
        # wildcard "top artists". See exclusions.py.
        if exclusions.is_excluded_track(ctrack.get("name", ""), ctrack.get("artist_ids", []), excluded_artists):
            continue

        duration_ms = ev.get("duration_ms") or ctrack.get("duration_ms") or 0
        if duration_ms == 0 or duration_ms > MAX_DURATION_MS:
            continue

        ms_played = ev.get("ms_played")
        if ms_played is None:
            continue

        completion = ms_played / duration_ms

        # Weight from spec table
        if completion >= 0.90:
            weight = WEIGHT_FULL_LISTEN
        elif completion >= 0.50:
            weight = WEIGHT_MOSTLY_LISTEN
        elif completion >= 0.30:
            weight = WEIGHT_PARTIAL_LISTEN
        elif completion >= 0.10:
            weight = WEIGHT_SKIP
        else:
            weight = WEIGHT_HARD_SKIP

        # Recency factor: 0.5 ^ (age_days / half_life)
        age_ms = now_ms - ev["played_at"]
        age_days = age_ms / (86400 * 1000)
        recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)

        event_score = weight * recency

        scores[tid] = scores.get(tid, 0.0) + event_score

    return scores, now_ms


def update_blocklist(affinity_scores: dict[str, float], use_signals: bool | None = None):
    """
    Apply blocklist rules: tracks with affinity ≤ DISLIKE_THRESHOLD are permanently banned.
    Once in blocklist, never removed (spec §5.5).
    Returns list of newly blocked track_ids.

    With skip signals on, a ban that the recorded plays alone would NOT cause
    (i.e. one driven by sampler-observed skips) additionally needs negative
    evidence on skip_signals.ban.min_negative_days separate days — one
    mis-detected skip must never become permanent.
    """
    newly_blocked = []
    existing_blocked = set(db.get_blocklist())
    if use_signals is None:
        use_signals = skip_signals.enabled()
    baseline = None

    for tid, score in affinity_scores.items():
        if score <= DISLIKE_THRESHOLD and tid not in existing_blocked:
            reason = f"affinity={score:.3f} <= {DISLIKE_THRESHOLD}"
            if use_signals:
                if baseline is None:
                    baseline, _ = compute_affinity(use_signals=False)
                if baseline.get(tid, 0.0) > DISLIKE_THRESHOLD:
                    since = int(datetime.now(timezone.utc).timestamp() * 1000) - WINDOW_DAYS * 86400 * 1000
                    if not skip_signals.ban_evidence_ok(tid, since):
                        continue
                    reason += " (sampler skips on multiple days)"
            db.add_to_blocklist(tid, reason)
            newly_blocked.append(tid)

    return newly_blocked


def get_liked_tracks(affinity_scores: dict[str, float]) -> list[tuple[str, float]]:
    """
    Return tracks with affinity ≥ LIKE_THRESHOLD, sorted descending.
    Each entry: (track_id, affinity_score)
    """
    liked = [(tid, score) for tid, score in affinity_scores.items() if score >= LIKE_THRESHOLD]
    liked.sort(key=lambda x: x[1], reverse=True)
    return liked


def get_neutral_tracks(affinity_scores: dict[str, float]) -> list[tuple[str, float]]:
    """
    Return tracks with LIKE_THRESHOLD > affinity > DISLIKE_THRESHOLD.
    """
    neutral = [
        (tid, score) for tid, score in affinity_scores.items()
        if DISLIKE_THRESHOLD < score < LIKE_THRESHOLD
    ]
    neutral.sort(key=lambda x: x[1], reverse=True)
    return neutral


def ranked_tracks(affinity_scores: dict[str, float]) -> list[tuple[str, float]]:
    """All tracks with non-zero affinity, sorted descending."""
    ranked = [(tid, score) for tid, score in affinity_scores.items()]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


if __name__ == "__main__":
    scores, _ = compute_affinity()
    liked  = get_liked_tracks(scores)
    print(f"Tracks scored: {len(scores)}")
    print(f"Liked (≥{LIKE_THRESHOLD}): {len(liked)}")
    if liked:
        print(f"  Top 5: {[(tid[:8], f'{s:.2f}') for tid, s in liked[:5]]}")