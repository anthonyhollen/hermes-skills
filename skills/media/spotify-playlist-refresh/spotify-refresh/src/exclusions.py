"""
exclusions.py — Category exclusions applied BEFORE scoring and during composition.

Unlike the live / instrumental filters in composer.py — which only shape the
playlist *output* — these exclusions also run inside scorer.compute_affinity(),
so an excluded track never earns affinity, never lands in the Like bucket, and
never contaminates artist-graph discovery / wildcard selection. (Baby-sleep
tracks are played as repeated full listens to settle a baby, so left unchecked
they would climb into the core favorites AND turn their artists into "top
artists" that drive discovery + wildcards toward more sleep music.)

Two mechanisms, OR'd together — a track is excluded if EITHER matches:

  1. Excluded artist IDs — config.yaml → exclude.artist_ids. A track is excluded
     if ANY of its artist_ids is listed. Catches every track from a dedicated
     sleep-music artist/label regardless of the track name. Add IDs as you find
     more artists (see the Spotify open.spotify.com/artist/<ID> URL).

  2. Baby-sleep name filter — matches sleep-music / white-noise / lullaby markers
     in the track name, so a sleep track from a not-yet-listed artist is still
     caught. Anchored to sleep-MUSIC phrases (not the bare word "sleep"/"rain")
     so it does NOT trip on ordinary songs — verified in
     scripts/test_track_name_filters.py.

A THIRD mechanism is output-only and deliberately NOT applied pre-scoring:

  3. Excluded albums — config.yaml → exclude.albums[].track_ids. Dropped from
     the playlist by the composer guardrails, but still allowed to earn affinity.
     Used when the artist is genuinely liked and their plays are real taste
     signal, but one particular album's versions shouldn't surface (e.g. an
     alternate-mix album). Use is_output_excluded_track() for this — the
     composer calls it; scorer.compute_affinity() must NOT.

  4. Excluded album names — config.yaml → exclude.album_name_contains. Any
     track whose ALBUM NAME contains one of the substrings (case-insensitive)
     is dropped from the playlist output. Same output-only semantics as (3):
     an album-name-excluded track still earns affinity, so the liked artists
     behind it keep driving discovery. This is the general rule that the
     specific ID-based entry for the CTLBT soundtrack album was merged into —
     that album's name contains the substring, so the general rule subsumes
     the track-ID list exactly. Add substrings here (NOT per-album IDs) when
     a whole *category* of albums (e.g. every Netflix-series soundtrack)
     should stay out of the playlist.

Config is read at import — cron spawns a fresh process per run, so this is
"live" per run, matching how scorer.py / composer.py read their config blocks.
"""

import os, re

_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def _load_excluded_artist_ids() -> set[str]:
    """Read exclude.artist_ids from config.yaml (best-effort; empty on any error)."""
    try:
        import yaml
        with open(_cfg_path) as f:
            cfg = (yaml.safe_load(f) or {}).get("exclude", {}) or {}
        return {str(a) for a in (cfg.get("artist_ids") or []) if a}
    except Exception:
        return set()


def _load_excluded_album_track_ids() -> set[str]:
    """
    Read exclude.albums[].track_ids from config.yaml and flatten to one set
    (best-effort; empty on any error).

    Album membership is pinned by track ID. (This list predates album names in
    the pipeline — since 2026-08-28 track_cache has an album_name column and the
    composer stamps album_name on every track dict, which powers the general
    exclude.album_name_contains rule; the ID list remains for albums with no
    shared naming pattern.) Track IDs are stable, so this is exact — but it means
    a re-release under a new album ID needs its own entry.
    """
    try:
        import yaml
        with open(_cfg_path) as f:
            cfg = (yaml.safe_load(f) or {}).get("exclude", {}) or {}
        ids: set[str] = set()
        for album in (cfg.get("albums") or []):
            for tid in ((album or {}).get("track_ids") or []):
                if tid:
                    ids.add(str(tid))
        return ids
    except Exception:
        return set()


def _load_album_name_filters() -> tuple[str, ...]:
    """
    Read exclude.album_name_contains from config.yaml and return the substrings
    lowercased (best-effort; empty on any error). Matching is substring-based
    ("name CONTAINS X") and case-insensitive, so the search is performed
    against a lowercased album name.
    """
    try:
        import yaml
        with open(_cfg_path) as f:
            cfg = (yaml.safe_load(f) or {}).get("exclude", {}) or {}
        return tuple(str(s).lower() for s in (cfg.get("album_name_contains") or []) if s)
    except Exception:
        return ()


EXCLUDED_ARTIST_IDS: set[str] = _load_excluded_artist_ids()
EXCLUDED_ALBUM_TRACK_IDS: set[str] = _load_excluded_album_track_ids()
ALBUM_NAME_FILTERS: tuple[str, ...] = _load_album_name_filters()


# ── Baby-sleep music name filter ──────────────────────────────────────────────
# Matches sleep-music / white-noise / lullaby markers common to baby-sleep
# catalogues. Every branch requires sleep-MUSIC *context* (a multi-word phrase,
# a noise colour, or a strongly genre-specific word like "lullaby") rather than
# the bare word "sleep"/"rain"/"ocean"/"music", so ordinary songs whose titles
# merely contain those words are NOT excluded (e.g. "Sleep", "Ocean", "Rain").
#
# NOTE (accepted tradeoff — user chose "artist + name keywords"): standalone
# titles like "Lullaby" (The Cure) or "Nursery Rhyme" WILL be caught. The
# artist-id list is the precise mechanism; this filter is the catch-all safety
# net for new sleep artists. Keep the two anchors in mind when editing:
#   - use \b word boundaries so a marker can't fire mid-word
#   - keep genre-specific phrasing, never a bare common word
_BABY_SLEEP_RE = re.compile(
    r'\bbaby\s+sleep\b'                       # "Relaxing Baby Sleep Music"
    r'|\bsleep\s+music\b'                     # "Deep Sleep Music"
    r'|\bsleep\s+sounds?\b'                   # "Sleep Sound(s) for Babies"
    r'|\bsleep\s+lullab'                      # "Sleep Lullaby / Lullabies"
    r'|\blullab(?:y|ies)\b'                   # "Lullaby" / "Lullabies"
    r'|\bwhite\s+noise\b'                     # "White Noise"
    r'|\bpink\s+noise\b'                      # "Pink Noise"
    r'|\bbrown\s+noise\b'                     # "Brown Noise"
    r'|\bwomb\s+sounds?\b'                    # "Womb Sound(s)"
    r'|\bnursery\s+rhyme'                     # "Nursery Rhyme(s)"
    r'|\bcalming\s+(?:music|sounds?)\b'       # "Calming Music / Sounds"
    r'|\bsoothing\s+(?:music|sounds?)\b'      # "Soothing Music / Sounds"
    r'|\brain\s+sounds?\b'                    # "Rain Sound(s)"
    r'|\bnature\s+sounds?\b',                 # "Nature Sounds"
    re.IGNORECASE,
)


def is_baby_sleep_name(name: str) -> bool:
    """True if the track name carries a baby-sleep / white-noise / lullaby marker."""
    return bool(_BABY_SLEEP_RE.search(name or ""))


def is_excluded_track(name: str, artist_ids=None, excluded_artist_ids: set = None) -> bool:
    """
    True if a track should be excluded from scoring AND playlist selection.

    Excluded if EITHER the name matches the baby-sleep filter OR any of its
    artist_ids is in the excluded-artist set. Pass a pre-fetched
    `excluded_artist_ids` set to avoid a lookup per call in tight loops
    (defaults to the module-level EXCLUDED_ARTIST_IDS).

    Deliberately does NOT consider the excluded-album list — album exclusions are
    output-only. Use is_output_excluded_track() from the composer instead.
    """
    if is_baby_sleep_name(name):
        return True
    ex = EXCLUDED_ARTIST_IDS if excluded_artist_ids is None else excluded_artist_ids
    return bool(ex) and any(a in ex for a in (artist_ids or []))


def is_album_excluded_track(track_id: str, excluded_album_track_ids: set = None) -> bool:
    """True if the track belongs to an album listed in config.yaml → exclude.albums."""
    ex = EXCLUDED_ALBUM_TRACK_IDS if excluded_album_track_ids is None else excluded_album_track_ids
    return bool(ex) and (track_id or "") in ex


def is_album_name_excluded(album_name: str, album_name_filters: tuple = None) -> bool:
    """
    True if the album name contains any substring from config.yaml →
    exclude.album_name_contains (case-insensitive substring match).

    Output-only: callers are the composer guardrails (via is_output_excluded_track);
    scorer.compute_affinity() must NOT call this so album-excluded tracks
    (e.g. Netflix soundtracks played by/liked artists) still earn affinity.
    """
    filters = ALBUM_NAME_FILTERS if album_name_filters is None else album_name_filters
    if not filters or not album_name:
        return False
    lowered = album_name.lower()
    return any(f in lowered for f in filters)


def is_output_excluded_track(name: str, artist_ids=None, track_id: str = "", album_name: str = "") -> bool:
    """
    True if a track should be kept out of the playlist OUTPUT.

    Superset of is_excluded_track(): the scoring-level exclusions PLUS the
    output-only album exclusions (ID-based exclude.albums and the general
    album-name rule exclude.album_name_contains). This is the check the
    composer guardrails use; scorer.compute_affinity() must keep using
    is_excluded_track() so an album-excluded track still contributes its
    artist's real affinity.
    """
    return (is_excluded_track(name, artist_ids)
            or is_album_excluded_track(track_id)
            or is_album_name_excluded(album_name))
