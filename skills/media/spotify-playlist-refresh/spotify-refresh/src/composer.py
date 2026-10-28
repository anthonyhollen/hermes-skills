"""
composer.py — Playlist composition: core favorites + similar discoveries + wildcards.
Section counts (core/discovery/wildcard, v2.19 llm) and target size are read live from config.yaml → playlist.
v2.19: with llm_discovery.enabled the new-music slots come from stored DeepSeek
picks + similar artists' new releases (llm_discovery.py) instead of the artist graph.
Guardrails (filters, dedupe, per-section + combined per-artist caps) are applied
while picking (see _Picker), then a fallback fill tops the playlist up to size.
"""

import math, random, re, os, sys
from datetime import datetime, timezone
import db, client, scorer, exclusions, skip_signals

# ── composition params — read live from config.yaml (playlist block) ───────────
# (defaults below are the current production values; edit config.yaml to tune)
_cfg = {}
_llm_cfg = {}
_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
if os.path.exists(_cfg_path):
    import yaml
    with open(_cfg_path) as _f:
        _full = yaml.safe_load(_f) or {}
    _cfg = _full.get("playlist", {}) or {}
    _llm_cfg = _full.get("llm_discovery", {}) or {}

_comp = _cfg.get("composition", {}) or {}
_cons = _cfg.get("constraints", {}) or {}
CORE_COUNT      = _comp.get("core", 12)
DISCOVERY_COUNT = _comp.get("discovery", 6)
WILDCARD_COUNT  = _comp.get("wildcard", 2)
LLM_COUNT       = _comp.get("llm", 0)           # v2.19 DeepSeek picks (llm_discovery.py)
TARGET_SIZE     = _cfg.get("target_size", CORE_COUNT + DISCOVERY_COUNT + WILDCARD_COUNT + LLM_COUNT)
MAX_PER_ARTIST          = _cons.get("max_tracks_per_artist", 3)            # per-section cap (core / discovery / wildcard independently)
MAX_PER_ARTIST_COMBINED = _cons.get("max_tracks_per_artist_combined", 4)   # combined-pool cap applied after sections are merged
WILDCARD_WINDOW_DAYS    = _cons.get("wildcard_release_window_days", 30)     # "new release" window for wildcards
TOP_ARTISTS_N   = 10   # how many liked artists to iterate for discovery/wildcard

# v2.19: llm_discovery.enabled switches the new-music sections to stored DeepSeek
# picks + similar artists' new releases (no artist-graph calls at refresh time).
# Off = exact v2.18 behaviour: the llm slots become artist-graph discovery,
# wildcards come from the top-10 artists, block order.
_llm_flag = _llm_cfg.get("enabled", False)
LLM_ENABLED = _llm_flag if isinstance(_llm_flag, bool) else str(_llm_flag).strip().lower() in ("true", "yes", "on", "1")
NEW_MUSIC_LAYOUT = _cfg.get("new_music_layout", "block")   # mix | block
MIX_LEAD_CORE    = 3      # mix layout: favourites before the first new song
LAST_INFO: dict = {}      # the last compose_playlist's new-music counts / batch status (refresher logs it)

# ── Live version filter ──────────────────────────────────────────────────────
# Matches standard live-performance markers in track names:
#   (Live), (Live at Madison Square Garden), [Live], - Live, – Live
#   "Live at/from/@ ..." (no dash needed), a trailing "Live" right after a
#   closing quote, and "... (World) Tour ... Live" titles that end in the marker
#   (e.g. XG 'NEW DANCE - XG 1st WORLD TOUR "The first HOWL" Live').
# Does NOT match: "Live and Let Die", "Stayin' Alive", "Long Live", etc. —
# a bare trailing "Live" with no quote/tour/at-from context is kept.
_LIVE_VERSION_RE = re.compile(
    r'[\(\[]\s*live\b'       # (Live...) or [Live...] — parenthesized marker
    r'|\s*[\-\u2013\u2014]\s*live\b'              # - Live / – Live after dash separator
    r'|\blive\s+(?:at|from|@)\b'      # "Live at/from/@ ..." — strong live-album phrasing, no delimiter needed
    r'|["“”]\s*live\s*$'          # trailing "Live" right after a closing quote (tour-recording titles)
    r'|\b(?:world\s+)?tour\b.*\blive\s*$',  # "... (World) Tour ... Live" ending in the live marker
    re.IGNORECASE
)

def _is_live_version(name: str) -> bool:
    """Return True if track name indicates a live performance version."""
    return bool(_LIVE_VERSION_RE.search(name or ""))


# ── Instrumental filter ──────────────────────────────────────────────────────
# Matches instrumental / karaoke markers used by Spotify catalogue:
#   (Instrumental), [Instrumental], - Instrumental, – Instrumental
#   (Inst.), [Inst.], - Inst., – Inst.   ← the "Inst." abbreviation (e.g. izna "Psycho - Inst.")
#   "Instrumental Version" (Spotify-owned instrumental catalogue entries)
#   "Karaoke Version"
# Does NOT match prose where "Instrumental" is the song/band title itself —
# the parens/bracket/dash anchor + case-insensitive word boundary mirrors
# the live-version regex. The abbreviation branch requires the trailing period
# ("inst.") so it can't fire on words like "Instant"/"Instinct"/"Install".
# "Instrumental Version" / "Karaoke Version" are matched as terminator phrases
# after any leading separator the catalog uses.
_INSTRUMENTAL_RE = re.compile(
    r'[\(\[]\s*(?:instrumental\b|inst\.)'                  # (Instrumental...) or [Instrumental...] — parenthesized marker
    r'|\s*[\-\u2013\u2014]\s*(?:instrumental\b|inst\.)'          # - Instrumental... or – Instrumental... after dash separator
    r'|\binstrumental\s+version\b'              # "Instrumental Version" (Spotify-owned catalogue)
    r'|\bkaraoke\s+version\b',                  # "Karaoke Version"
    re.IGNORECASE
)

def _is_instrumental_version(name: str) -> bool:
    """Return True if track name indicates an instrumental or karaoke version."""
    return bool(_INSTRUMENTAL_RE.search(name or ""))


# ── Remix filter ──────────────────────────────────────────────────────────────
# Matches remix-version markers in track names (e.g. izna "SIGN (Remix)"):
#   (Remix), [Remix], (Club Remix), (Tiësto Remix), (Remixes), - Remix,
#   – Artist Remix, and a trailing "Remix"/"Remixes"/"Remixed" at end of title.
# "remix" is word-bounded so it can't fire mid-word ("Remixing", "premix" are
# kept), and IGNORECASE so case doesn't matter. Accepted tradeoff (mirrors the
# baby-sleep filter): a song titled exactly "Remix" would also be caught — remix
# titles are reliably version markers, so this errs toward excluding them.
_REMIX_RE = re.compile(
    r'[\(\[][^)\]]*\bremix(?:e[sd])?\b'          # (Remix) / (Club Remix) / [Remix] / (Remixes) — remix inside parens/brackets
    r'|[\-\u2013\u2014]\s.*\bremix(?:e[sd])?\b'        # - Remix / – Artist Remix after a dash separator
    r'|\bremix(?:e[sd])?\b\s*$',                 # trailing "Remix"/"Remixes"/"Remixed" at end of title
    re.IGNORECASE
)

def _is_remix_version(name: str) -> bool:
    """Return True if track name indicates a remix version."""
    return bool(_REMIX_RE.search(name or ""))


# ── Unplayable / region-locked filter ────────────────────────────────────────
# Spotify keeps tracks in the catalogue that are not licensed in the user's
# market. They appear greyed out in-app and error with "this content is not
# available" on play (reported: izna "Animal Fries", "PROUD CITIZEN").
#
# Unlike the name-based filters above, this is a DATA check, not a regex — the
# signal comes from the API response, so it can only be applied where a raw
# track object is in hand (discovery + wildcard live fetches). Note:
#   - `is_playable` is present ONLY when the request passed `market=` — every
#     composer fetch does, but a caller that forgets would silently see None.
#   - `restrictions` is returned with or without `market`, so it is checked too
#     rather than trusting `is_playable` alone.
# Absent BOTH fields the track is treated as playable (fail-open) — the vast
# majority of tracks carry neither, and failing closed would empty the pool.
def _is_unplayable(track: dict) -> bool:
    """
    Return True if a raw Spotify track object is known to be unplayable in the
    user's market (greyed out in-app).

    Fail-open by design: only an explicit `is_playable: false` or a present
    `restrictions.reason` marks a track unplayable. A track dict with neither
    field (e.g. one rebuilt from track_cache, which stores no playability
    column) is treated as playable.
    """
    if not isinstance(track, dict):
        return False
    if track.get("is_playable") is False:
        return True
    restrictions = track.get("restrictions") or {}
    return bool(isinstance(restrictions, dict) and restrictions.get("reason"))


# ── Same-song / same-artist duplicate detection ─────────────────────────────
# Track-ID dedupe alone cannot catch the same song released under DIFFERENT
# IDs — e.g. 'Shapeshifter' by Baby DONT Cry exists both as the standalone
# single (0kkqkpgbkLFpopZqPAH1CO) and on 'The 1st Mini Album [AFTER CRY]'
# (6ONjO8j2uE9Y9wPgbYR3fw). Discovery can surface the album copy while the
# liked single sits in core, and both pass the track-ID check. These helpers
# build a (normalized name, artist set) signature so the same song from the
# same artist(s) is kept once, first occurrence wins (core favorites precede
# discovery/wildcard in the combined pool, so the liked/heard release is kept).
def _normalize_track_name(name: str) -> str:
    """Lowercase + collapse whitespace; enough to catch case/space variants
    without merging distinct versions ('Song' vs 'Song (Sped Up)')."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _track_sig(track: dict):
    """
    Signature identifying 'the same song by the same artist(s)'.
    Two tracks sharing a sig are duplicates even with different track IDs.
    Returns None when the track has no usable name or no artist info —
    such tracks (local files / regional entries) are never sig-deduped.
    """
    name = _normalize_track_name(track.get("name", ""))
    if not name:
        return None
    artist_ids = [a for a in (track.get("artist_ids") or []) if a]
    if artist_ids:
        return (name, frozenset(artist_ids))
    artist_names = frozenset(_normalize_track_name(a) for a in (track.get("artist_names") or []) if a)
    if not artist_names:
        return None
    return (name, artist_names)


def _dedupe_tracks(pool: list[dict]) -> list[dict]:
    """Dedupe by track ID, then by same-song/same-artist signature."""
    seen_ids: set[str] = set()
    seen_sigs: set = set()
    out = []
    for t in pool:
        tid = t.get("track_id") or ""
        if tid and tid in seen_ids:
            continue
        sig = _track_sig(t)
        if sig is not None and sig in seen_sigs:
            continue
        if tid:
            seen_ids.add(tid)
        if sig is not None:
            seen_sigs.add(sig)
        out.append(t)
    return out



def _in_cooldown(t: dict, cooldown) -> bool:
    """cooldown = (track_ids, same-song signatures) from skip signals, or None."""
    if not cooldown:
        return False
    ids, sigs = cooldown
    if t.get("track_id") in ids:
        return True
    sig = _track_sig(t)
    return sig is not None and sig in sigs


def _cooldown_sets(track_ids: set[str]):
    """(ids, sigs) — the signature catches another release of the same song."""
    sigs = {s for s in (_track_sig(c) for c in db.get_cached_tracks(list(track_ids)).values())
            if s is not None}
    return set(track_ids), sigs


def _passes_output_filters(t: dict, blocked: set[str], cooldown=None) -> bool:
    """Every output guardrail except dedupe/caps: blocklist, live / instrumental /
    remix version filters, unplayable, category + album exclusions, and (skip
    signals on) the skip cooldown / demotion."""
    name = t.get("name") or ""
    return (t.get("track_id") not in blocked
            and not _in_cooldown(t, cooldown)
            and not _is_live_version(name)
            and not _is_instrumental_version(name)
            and not _is_remix_version(name)
            and not _is_unplayable(t)
            and not exclusions.is_output_excluded_track(
                name, t.get("artist_ids"), t.get("track_id", ""), t.get("album_name", "")))


def _primary_artist(t: dict) -> str:
    return t["artist_ids"][0] if t.get("artist_ids") else ""


class _Picker:
    """
    Shared composition state. Every accepted track has passed the output filters,
    is not a duplicate (track ID or same-song signature) of an accepted track, and
    keeps its primary artist within MAX_PER_ARTIST_COMBINED across the whole
    playlist (and within the per-section cap while filling a section).

    Guardrails are applied WHILE picking, not after sampling: a filtered/capped
    candidate is skipped and the next one tried, so a section is only short when
    its candidate supply is genuinely exhausted. (Sampling first and filtering
    after left discovery at ~3–8 of 12 — live/Inst./XDM picks were dropped with
    no refill.) Tracks with no known artist ID are never bucketed under the caps.
    """

    def __init__(self, blocked: set[str], cooldown=None):
        self.blocked = blocked
        self.cooldown = cooldown          # (ids, sigs) with skip signals on, else None
        self.ids: set[str] = set()
        self.sigs: set = set()
        self.artist_counts: dict[str, int] = {}

    def try_add(self, t: dict, section_counts: dict | None = None, section_cap: int = 0) -> bool:
        tid = t.get("track_id") or ""
        if not tid or tid in self.ids:
            return False
        sig = _track_sig(t)
        if sig is not None and sig in self.sigs:
            return False  # same song from same artist(s) already picked (different release ID)
        primary = _primary_artist(t)
        if primary:
            if self.artist_counts.get(primary, 0) >= MAX_PER_ARTIST_COMBINED:
                return False
            if section_counts is not None and section_counts.get(primary, 0) >= section_cap:
                return False
        if not _passes_output_filters(t, self.blocked, self.cooldown):
            return False
        self.ids.add(tid)
        if sig is not None:
            self.sigs.add(sig)
        if primary:
            self.artist_counts[primary] = self.artist_counts.get(primary, 0) + 1
            if section_counts is not None:
                section_counts[primary] = section_counts.get(primary, 0) + 1
        return True

    def fill(self, ordered: list[dict], n: int, section_cap: int | None = MAX_PER_ARTIST,
             source: str | None = None, counts: dict | None = None) -> list[dict]:
        """Take up to n tracks from `ordered` (best first) that pass every guardrail.
        `counts` shares one per-artist section count across several fills (v2.19:
        one song per new artist across the llm + wildcard sections)."""
        if section_cap is None:
            counts = None
        elif counts is None:
            counts = {}
        out = []
        for t in ordered:
            if len(out) >= n:
                break
            if self.try_add(t, counts, section_cap or 0):
                if source:
                    t["source"] = source
                out.append(t)
        return out


def _weighted_order(items: list, weights: list[float]) -> list:
    """
    Weighted-random permutation WITHOUT replacement (Efraimidis–Spirakis: sort by
    u^(1/w)). Taking the first n is an exact weighted sample of n; the rest is the
    ordered remainder a picker falls through to when a pick is filtered out.
    Non-positive weights go last, in random order.
    Keys are (log(u)/w, 0) for w > 0 and (-inf, u) otherwise, sorted descending.
    """
    keyed = []
    for item, w in zip(items, weights):
        u = 1.0 - random.random()          # (0, 1] — log() is safe
        # log(u)/w ranks identically to u**(1/w) but can't underflow to 0.0 for
        # tiny w (which would tie keys and fall back to input order).
        keyed.append(((math.log(u) / w, 0) if w > 0 else (float("-inf"), u), item))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in keyed]


def _spread(base: list, extra: list, lead_extra: bool = False) -> list:
    """Interleave `extra` evenly through `base`, keeping both orders (ties: base
    first). _spread(21 core, 16 new) ≈ every other song; lead_extra=True starts
    with an extra item (the mix layout's position 4)."""
    if not extra or not base:
        return list(base) + list(extra)
    keyed = [((i + 0.5) / len(base), 0, i, t) for i, t in enumerate(base)]
    keyed += [((i / len(extra)) if lead_extra else (i + 0.5) / len(extra), 1, i, t) for i, t in enumerate(extra)]
    keyed.sort(key=lambda x: x[:3])
    return [t for *_, t in keyed]


def _round_robin(items: list[dict], key) -> list[dict]:
    """Reorder so consecutive items rotate across key(item) groups (group order =
    first appearance) — one EP can't crowd the front of the wildcard list."""
    groups: dict = {}
    for t in items:
        groups.setdefault(key(t), []).append(t)
    out, queues = [], list(groups.values())
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            out.append(q.pop(0))
    return out


def _llm_candidates(heard_ids: set[str], heard_sigs: set, skipped: set[str] | None,
                    liked_ids: set[str] | None = None) -> dict:
    """{picks, releases, encores, encore_slots, status} from the stored
    llm_discovery batches — DB only, no API calls. Re-checks what may have changed
    since the job ran: heard (ID + same-song signature), sampler-skipped, artists
    the listener now knows (unless their DeepSeek songs were finished), J-pop /
    version guards and the release window. Encores (v2.20) are finished,
    never-skipped DeepSeek songs offered again until they become favourites.
    Output filters, cooldown, blocklist and caps are applied by the picker. Never
    raises: any failure → empty lists, so a broken store can only cost the
    new-music slots, never the refresh."""
    empty = {"picks": [], "releases": [], "encores": [], "encore_slots": 0, "more_from_slots": 0}
    try:
        import llm_discovery as ld
        cfg = ld.load_config()
        status = ld.supply_status()
        known, positive = ld.known_artist_ids(), ld.positive_artist_ids()
        blocked_names = ld._blocked_names(cfg)
        cutoff = datetime.now(timezone.utc).timestamp() * 1000 - WILDCARD_WINDOW_DAYS * 86400 * 1000

        heard_base = ld.heard_base_sigs(heard_ids)
        rctx = {"known": known, "positive": positive, "cfg": cfg, "last_heard": ld.artist_last_heard(),
                "now": int(datetime.now(timezone.utc).timestamp() * 1000)}

        def usable(c: dict, release: bool = False) -> bool:
            # a release by a known artist is OK once they have been unheard for the cool-off
            blocked_artist = (lambda a: ld.release_blocked_artist(a, rctx)) if release else \
                             (lambda a: a in known and a not in positive)
            return not (_is_heard(c, heard_ids, heard_sigs) or ld.base_sig(c) in heard_base
                        or (skipped and c["track_id"] in skipped)
                        or any(blocked_artist(a) for a in (c.get("artist_ids") or []))
                        or ld.is_japanese(c.get("name", ""), c.get("artist_names") or [],
                                          c.get("album_name", ""), blocked_names)
                        or ld.bad_version(c.get("name", ""), c.get("duration_ms"), cfg["min_duration_s"]))

        picks = _dedupe_tracks([c for c in ld.load_candidates("pick", cfg["max_pick_age_days"]) if usable(c)])
        # Newest batch first; within a batch "close" and "adjacent" picks are
        # interleaved and "major"-tier picks go last.
        by_batch: dict[int, list[dict]] = {}
        for c in picks:
            by_batch.setdefault(c.get("_batch_id") or 0, []).append(c)
        ordered = []
        for bid in sorted(by_batch, reverse=True):
            group = by_batch[bid]
            close = [c for c in group if c.get("fit") != "adjacent" and c.get("tier") != "major"]
            adjacent = [c for c in group if c.get("fit") == "adjacent" and c.get("tier") != "major"]
            ordered += _spread(close, adjacent) + [c for c in group if c.get("tier") == "major"]
        releases = [c for c in ld.load_candidates("new_release", cfg["new_release_max_age_days"])
                    if usable(c, release=True) and _parse_release_ms(c.get("release_date", "")) >= cutoff
                    and (not c.get("_artist_id") or _primary_artist(c) == c["_artist_id"])]   # not a feature
        releases = _round_robin(_dedupe_tracks(releases), _primary_artist)
        try:
            # real skips are handled there (replay skips excused); the skip cooldown /
            # demotion still applies through the picker
            encores = ld.encore_candidates(liked_ids or set(), cfg=cfg)
        except Exception as e:      # encores are a bonus — never cost the picks
            print(f"⚠️  DeepSeek encores unavailable ({type(e).__name__}: {e})", file=sys.stderr)
            encores = []
        for c in ordered:      # more songs by artists whose DeepSeek songs were finished
            c["_more_from"] = any(a in positive for a in (c.get("artist_ids") or []))
        return {"picks": ordered, "releases": releases, "encores": encores,
                "encore_slots": min(int(cfg["encore_slots"]), LLM_COUNT),
                "more_from_slots": int(cfg["more_from_max_slots"]), "status": status}
    except Exception as e:
        print(f"⚠️  DeepSeek picks unavailable ({type(e).__name__}: {e}); "
              f"new-music slots fall back to favourites", file=sys.stderr)
        return dict(empty, status={"status": "error", "age_h": None, "error": f"{type(e).__name__}: {e}"})


def _llm_note(n_llm: int, n_wild: int, status: dict) -> str | None:
    """One honest banner line when the new-music slots are short BECAUSE the
    DeepSeek supply failed or went stale (never any model text)."""
    if n_llm >= LLM_COUNT and n_wild >= WILDCARD_COUNT:
        return None
    st, age = status.get("status"), status.get("age_h")
    if st == "ok" and age is not None and age <= 30:
        return None
    reason = {"none": "no DeepSeek run yet", "error": "the last DeepSeek run crashed",
              "killed": "the last DeepSeek run was cut off",
              "partial": ("the last DeepSeek run failed" if status.get("llm_failed")
                          else "Spotify lookups failed in the last DeepSeek run")
              }.get(st, "the DeepSeek picks could not be read")
    if st == "ok":
        reason = f"the last DeepSeek run is {age:.0f}h old"
    return (f"DeepSeek picks {n_llm}/{LLM_COUNT}, new releases {n_wild}/{WILDCARD_COUNT} today "
            f"({reason}); topped up with favourites")


def compose_playlist(affinity_scores: dict[str, float], dry_run: bool = False) -> list[dict]:
    """
    Main entry point. Takes affinity scores dict {track_id: score}, returns up to
    TARGET_SIZE track dicts (track_id, name, artist_ids, uri, source, ...).
    dry_run=True makes the same read-only API calls but writes nothing to the DB
    (no artist_cache upserts); the playlist is never touched here either way.
    """
    liked    = scorer.get_liked_tracks(affinity_scores)
    neutral  = scorer.get_neutral_tracks(affinity_scores)
    blocked  = set(db.get_blocklist())
    token    = client.get_token()

    # Skip signals (config.yaml → skip_signals.enabled; off = pre-v2.18 behaviour):
    # cooldown + demotion keep recently skipped tracks out; every observed
    # exposure counts as heard; skipped tracks never return as "heard" wildcards.
    use_signals = skip_signals.enabled()
    cool = skip_signals.cooldown_state() if use_signals else None
    cooldown = _cooldown_sets(set(cool["cooldown"]) | set(cool["demoted"])) if cool else None
    skipped = skip_signals.skipped_ids() if use_signals else None
    picker   = _Picker(blocked, cooldown)

    heard_ids = _get_heard_track_ids()
    if use_signals:
        heard_ids |= skip_signals.heard_ids()
    heard_sigs = {s for s in (_track_sig(c) for c in db.get_cached_tracks(list(heard_ids)).values())
                  if s is not None}
    artist_affinity = _aggregate_artist_affinity(liked)
    top_artists = sorted(artist_affinity.items(), key=lambda x: x[1], reverse=True)[:TOP_ARTISTS_N]
    artist_data: dict[str, dict] = {}   # per-run memo shared by discovery + wildcards

    # v2.19: DeepSeek picks + similar artists' new releases replace the artist
    # graph (stored by the llm_discovery job — no API calls here). Disabled → the
    # llm slots go to artist-graph discovery, exactly as in v2.18.
    global LAST_INFO
    LAST_INFO = {}
    use_llm = LLM_ENABLED and LLM_COUNT > 0
    disc_n = DISCOVERY_COUNT + (0 if use_llm else LLM_COUNT)

    # ── 1. Core favorites (CORE_COUNT) — weighted by affinity ───────────────
    core = picker.fill(_pick_core_favorites(liked), CORE_COUNT, source="core")

    # ── 2. Candidate pools (filtered at ingestion) ──────────────────────────
    disc_cands = _pick_discoveries(top_artists, disc_n, blocked, token,
                                   heard_ids, heard_sigs, artist_data, dry_run) if liked and disc_n > 0 else []
    llm_cands, enc_cands, enc_slots, mf_slots, llm_status = [], [], 0, 0, {}
    if use_llm:
        sup = _llm_candidates(heard_ids, heard_sigs, skipped, {tid for tid, _ in liked})
        llm_cands, wild_cands, enc_cands = sup["picks"], sup["releases"], sup["encores"]
        enc_slots, mf_slots, llm_status = sup["encore_slots"], sup["more_from_slots"], sup["status"]
    else:
        wild_cands = _pick_wildcards(top_artists, token, heard_ids, heard_sigs,
                                     artist_data, dry_run, skipped) if liked else []

    # ── 3. Wildcards before discovery in cap accounting (they are scarce —
    #       appended last they always lost the combined per-artist cap) ──────
    if use_llm:
        new_artists: dict = {}   # one song per new artist across wildcard + llm
        wildcard = picker.fill(wild_cands, WILDCARD_COUNT, section_cap=1, source="wildcard", counts=new_artists)
        # Encores (finished DeepSeek songs coming back) take up to encore_slots of
        # the llm slots; their artist may also get a new pick (a liked new artist).
        enc_counts: dict = {}
        encore = picker.fill(enc_cands, enc_slots, section_cap=1, source="encore", counts=enc_counts)
        # At most more_from_max_slots go to more songs by artists already finished;
        # the rest are artists the listener has never heard (interleaved for the layout).
        n_slots = LLM_COUNT - len(encore)
        llm_mf = picker.fill([c for c in llm_cands if c.get("_more_from")], min(mf_slots, n_slots),
                             section_cap=1, source="llm", counts=new_artists)
        llm_new = picker.fill([c for c in llm_cands if not c.get("_more_from")], n_slots - len(llm_mf),
                              section_cap=1, source="llm", counts=new_artists)
        llm = _spread(llm_new, llm_mf)
        if len(encore) + len(llm) < LLM_COUNT:      # picks ran short: more encores before favourites
            encore += picker.fill(enc_cands, LLM_COUNT - len(encore) - len(llm), section_cap=1,
                                  source="encore", counts=enc_counts)
    else:
        wildcard = picker.fill(wild_cands, WILDCARD_COUNT, source="wildcard")
        encore, llm = [], []
    discovery = picker.fill(disc_cands, disc_n, source="discovery")

    pool = core + discovery + wildcard + encore + llm

    # ── 4. Fallback fill: liked → neutral → leftover discovery/wildcard ─────
    if len(pool) < TARGET_SIZE:
        need = TARGET_SIZE - len(pool)
        liked_meta = _fetch_track_metadata([tid for tid, _ in liked if tid not in picker.ids])
        pool += picker.fill(liked_meta, need, section_cap=None, source="fallback")
    if len(pool) < TARGET_SIZE:
        need = TARGET_SIZE - len(pool)
        neutral_meta = _fetch_track_metadata([tid for tid, _ in neutral if tid not in picker.ids])
        pool += picker.fill(neutral_meta, need, section_cap=None, source="fallback")
    if len(pool) < TARGET_SIZE:
        need = TARGET_SIZE - len(pool)
        pool += picker.fill(disc_cands + wild_cands + llm_cands + enc_cands, need, section_cap=None,
                            source="fallback")

    # ── 5. Still short with a cooldown active: let cooled-down tracks back,
    #       oldest skip first. Demoted tracks and anything the sampler saw
    #       skipped in the last 24h stay out — a short playlist beats those. ──
    if len(pool) < TARGET_SIZE and cool and skip_signals.load_config()["cooldown"].get("relax_on_underfill"):
        blocked_ids = set(cool["cooldown"]) | set(cool["demoted"])
        strict = set(cool["demoted"]) | set(cool["recent24h"])
        relaxable = [tid for _, tid in sorted((until, tid) for tid, until in cool["cooldown"].items()
                                              if tid not in strict)]
        for tid in relaxable:
            if len(pool) >= TARGET_SIZE:
                break
            blocked_ids.discard(tid)
            picker.cooldown = _cooldown_sets(blocked_ids)
            for cands in (_fetch_track_metadata([t for t, _ in liked if t not in picker.ids]),
                          _fetch_track_metadata([t for t, _ in neutral if t not in picker.ids]),
                          disc_cands + wild_cands + llm_cands):
                if len(pool) >= TARGET_SIZE:
                    break
                pool += picker.fill(cands, TARGET_SIZE - len(pool), section_cap=None, source="fallback")

    if len(pool) < TARGET_SIZE:
        # stderr, not stdout: the refresh wrapper's stdout is the Discord message.
        print(f"⚠️  Only {len(pool)} tracks composed (target: {TARGET_SIZE})", file=sys.stderr)

    pool = pool[:TARGET_SIZE]
    if use_llm:
        LAST_INFO = {"llm": len(llm), "more_from": sum(1 for t in llm if t.get("_more_from")),
                     "encore": len(encore), "llm_target": LLM_COUNT, "wildcard": len(wildcard),
                     "wildcard_target": WILDCARD_COUNT, "llm_pool": len(llm_cands), "encore_pool": len(enc_cands),
                     "release_pool": len(wild_cands), "batch_status": llm_status.get("status"),
                     "batch_age_h": llm_status.get("age_h"), "layout": NEW_MUSIC_LAYOUT,
                     "note": _llm_note(len(llm) + len(encore), len(wildcard), llm_status)}
        new_src = ("encore", "llm", "wildcard")
        favs = [t for t in pool if t.get("source") == "core"]
        rest = [t for t in pool if t.get("source") not in ("core",) + new_src]
        if NEW_MUSIC_LAYOUT != "mix":
            pool = (favs + [t for t in pool if t.get("source") in ("encore", "llm")]
                    + [t for t in pool if t.get("source") == "wildcard"] + rest)
        else:
            # Positions 1–MIX_LEAD_CORE favourites, then every new song (DeepSeek
            # picks with the new releases spread through them, then any fallback
            # fill) spread evenly through the remaining favourites. A pure reorder:
            # playlist_version stores the final order, so skip inference holds.
            new = _spread([t for t in pool if t.get("source") in ("encore", "llm")],   # encores first
                          [t for t in pool if t.get("source") == "wildcard"])
            pool = favs[:MIX_LEAD_CORE] + _spread(favs[MIX_LEAD_CORE:], new + rest, lead_extra=True)
    return pool


def _pick_core_favorites(liked: list[tuple[str, float]]) -> list[dict]:
    """
    All liked tracks (with metadata) in weighted-random order, weight = affinity.
    Re-sampled each run (spec §6: "actually changes day-to-day"); the picker takes
    the first CORE_COUNT that pass the guardrails. (Replaces a cumulative-weight
    loop that, on hitting an already-picked track, fell through to the NEXT index
    instead of redrawing — over-picking whatever sat after a heavy track.)
    """
    if not liked:
        return []
    ordered = _weighted_order([tid for tid, _ in liked], [s for _, s in liked])
    return _fetch_track_metadata(ordered)


def _fetch_track_metadata(track_ids: list[str]) -> list[dict]:
    """Rebuild track dicts (name/artists/duration/album) for track IDs from track_cache, in order."""
    if not track_ids:
        return []
    cached = db.get_cached_tracks(track_ids)
    result = []
    for tid in track_ids:
        c = cached.get(tid)
        if not c:
            continue
        result.append({
            "track_id":     tid,
            "name":         c.get("name") or "",
            "artist_names": [],   # track_cache stores no artist names
            "artist_ids":   c.get("artist_ids", []),
            "duration_ms":  c.get("duration_ms", 0),
            "album_name":   c.get("album_name") or "",
            "uri":          f"spotify:track:{tid}",
            "source":       "core",
        })
    return result


def _candidate(track: dict, album_name: str, source: str, fallback_artist: str = "") -> dict | None:
    """Normalize a (slim) API track into a composer track dict; None if it has no ID."""
    uri = track.get("uri", "") or ""
    tid = track.get("id") or (uri.split(":")[-1] if uri else "")
    if not tid:
        return None
    artists = track.get("artists") or []
    artist_ids = [a.get("id") for a in artists if a.get("id")] or ([fallback_artist] if fallback_artist else [])
    return {
        "track_id":     tid,
        "name":         track.get("name") or "",
        "artist_ids":   artist_ids,
        "artist_names": [a.get("name", "") for a in artists],
        "duration_ms":  track.get("duration_ms", 0),
        "album_name":   album_name or "",
        "uri":          uri or f"spotify:track:{tid}",
        "is_playable":  track.get("is_playable"),
        "restrictions": track.get("restrictions"),
        "source":       source,
    }


def _is_heard(t: dict, heard_ids: set[str], heard_sigs: set) -> bool:
    """Heard by track ID, or the same song (name + artists) under another release ID."""
    if t["track_id"] in heard_ids:
        return True
    sig = _track_sig(t)
    return sig is not None and sig in heard_sigs


def _pick_discoveries(
    top_artists: list[tuple[str, float]],
    count: int,
    blocked: set[str],
    token: str,
    heard_ids: set[str],
    heard_sigs: set,
    artist_data: dict,
    dry_run: bool = False,
) -> list[dict]:
    """
    Artist-graph discovery candidates (spec §6.2; replaces /v1/recommendations),
    returned in weighted-random order (weight = artist affinity) for the picker.

    For each top artist (by aggregated liked-track affinity):
      a. _fetch_artist_top_tracks() — up to 10 tracks from the 5 newest releases
      b. tracks of the artist's first 5 cached albums (artist_cache albums[].tracks,
         live fetch only for rows cached before tracks were stored)
    Filtered at ingestion: already-heard tracks (ID or same-song signature),
    blocklist, unplayable, and every output guardrail — so the sample is drawn
    only from tracks that can actually ship. Stops once count*5 candidates exist.
    """
    market = client.get_user_market(token)
    candidates: list[dict] = []
    weights: list[float] = []
    seen: set[str] = set()

    def _consider(c: dict | None, weight: float):
        if not c or c["track_id"] in seen:
            return
        seen.add(c["track_id"])
        if _is_heard(c, heard_ids, heard_sigs) or not _passes_output_filters(c, blocked):
            return
        candidates.append(c)
        weights.append(weight)

    for artist_id, artist_score in top_artists:
        data = _get_artist_data(artist_id, market, token, artist_data, dry_run)
        for track in (data.get("top_tracks") or [])[:10]:
            _consider(_candidate(track, track.get("album_name", ""), "discovery", artist_id), artist_score)
        for album in (data.get("albums") or [])[:5]:
            album_id = album.get("album_id")
            if not album_id:
                continue
            tracks = _album_tracks(album, market, token)
            for track in tracks:
                _consider(_candidate(track, album.get("name", ""), "discovery", artist_id), artist_score)
        if len(candidates) >= count * 5:
            break  # enough candidates

    return _weighted_order(candidates, weights)


def _aggregate_artist_affinity(liked: list[tuple[str, float]]) -> dict[str, float]:
    """
    Aggregate track affinity scores by artist.
    For each liked track, distribute its affinity equally across its artists.
    Returns {artist_id: aggregated_affinity}.
    """
    artist_scores: dict[str, float] = {}
    cached = db.get_cached_tracks([tid for tid, _ in liked])
    for tid, score in liked:
        c = cached.get(tid, {})
        artist_ids = c.get("artist_ids", [])
        if not artist_ids:
            continue
        share = score / len(artist_ids)
        for aid in artist_ids:
            artist_scores[aid] = artist_scores.get(aid, 0.0) + share
    return artist_scores


def _get_heard_track_ids() -> set[str]:
    """Return all track_ids that exist in play_events (user has heard them)."""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT track_id FROM play_events").fetchall()
    return {r[0] for r in rows}


def _parse_release_ms(release_date: str) -> float:
    """Parse a Spotify release_date string ('YYYY', 'YYYY-MM', 'YYYY-MM-DD') to unix ms."""
    try:
        if len(release_date) == 4:
            return datetime(int(release_date[:4]), 1, 1).timestamp() * 1000
        elif len(release_date) == 7:
            y, m = release_date.split("-")
            return datetime(int(y), int(m), 1).timestamp() * 1000
        else:
            return datetime.fromisoformat(release_date).timestamp() * 1000
    except Exception:
        return 0.0


def _slim_track(t: dict) -> dict:
    """Keep only the fields the composer reads — album-track payloads are cached
    in artist_cache, so full API objects would bloat the DB."""
    return {
        "id":           t.get("id"),
        "uri":          t.get("uri"),
        "name":         t.get("name"),
        "duration_ms":  t.get("duration_ms"),
        "artists":      [{"id": a.get("id"), "name": a.get("name")} for a in (t.get("artists") or [])],
        "is_playable":  t.get("is_playable"),
        "restrictions": t.get("restrictions"),
    }


def _get_artist_data(artist_id: str, market: str, token: str, memo: dict, dry_run: bool) -> dict:
    """
    Artist graph data (top_tracks + albums) for one artist: per-run memo →
    fresh artist_cache row → live fetch. A live fetch is cached only when it was
    complete (albums call + every top-tracks album fetch succeeded); otherwise
    (rate limit / deadline) the last good row is reused even if stale and nothing
    is overwritten — a failed refetch used to replace good data with an empty /
    partial row for the next TTL. With no stale row, the partial data is used
    for this run only.
    """
    if artist_id in memo:
        return memo[artist_id]
    fresh = db.get_cached_artists([artist_id]).get(artist_id)
    if fresh and (fresh.get("top_tracks") or fresh.get("albums")):
        memo[artist_id] = fresh
        return fresh
    top_tracks, albums, name, ok = _fetch_artist_graph(artist_id, market, token)
    data = {
        "artist_id": artist_id, "name": name, "top_tracks": top_tracks, "albums": albums,
        "cached_at": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    if ok and (top_tracks or albums):
        if not dry_run:
            db.upsert_artist_cache([data])
    else:
        stale = db.get_cached_artists([artist_id], include_stale=True).get(artist_id)
        if stale and (stale.get("top_tracks") or stale.get("albums")):
            data = stale
    memo[artist_id] = data
    return data


def _fetch_artist_top_tracks(artist_id: str, market: str, token: str) -> tuple[list[dict], bool]:
    """
    Fetch up to 10 representative tracks for an artist using the Feb 2026 approach.
    Replaces the removed GET /v1/artists/{id}/top-tracks endpoint.

    Algorithm:
      1. GET /v1/artists/{id}/albums?include_groups=album,single&market={market}&limit=10
         → sort by release_date desc (newest first), pick top 5
      2. For each album: GET /v1/albums/{id}/tracks?market={market}&limit=50
         → filter to tracks where artist_id appears in track.artists[]
      3. Dedupe by track_id across albums (keep first occurrence)
      4. Filter out tracks with duration_ms < 60000 (interludes/intros)
      5. Return (first 10 tracks as slim track dicts (+ album_name), complete) —
         complete=False if the albums call or any album-tracks call failed, so the
         caller doesn't cache a partial/empty list over a good row
    Both GETs are memoized per run in client.api, so the albums list and album
    tracks shared with _fetch_artist_graph are fetched once.
    """
    resp = client.get(
        f"artists/{artist_id}/albums"
        f"?include_groups=album,single"
        f"&market={market}"
        f"&limit=10",
        token=token
    )
    if "error" in resp:
        return [], False
    items = resp.get("items", []) or []

    sorted_albums = sorted(items, key=lambda a: _parse_release_ms(a.get("release_date", "")), reverse=True)
    top_5_albums = sorted_albums[:5]

    seen_ids: set[str] = set()
    result: list[dict] = []
    complete = True

    for album in top_5_albums:
        album_id = album.get("id")
        if not album_id:
            continue
        tracks = _fetch_album_tracks(album_id, market, token)
        if tracks is None:
            complete = False
            continue
        for track in tracks:
            # Filter: artist must appear in track.artists[]
            if not any(a.get("id") == artist_id for a in track.get("artists", [])):
                continue
            uri = track.get("uri", "") or ""
            tid = uri.split(":")[-1] if uri else ""
            if not tid or tid in seen_ids:
                continue
            # Filter: skip interludes/intros < 60s
            duration_ms = track.get("duration_ms") or 0
            if 0 < duration_ms < 60000:
                continue
            seen_ids.add(tid)
            # Raw /albums/{id}/tracks items carry no album field — stamp the name
            # so the output-only album-name exclusion rule can see it.
            result.append({**track, "album_name": album.get("name", "")})
            if len(result) >= 10:
                return result, complete

    return result, complete


def _fetch_artist_graph(artist_id: str, market: str, token: str) -> tuple[list[dict], list[dict], str, bool]:
    """
    Fetch an artist's representative tracks and albums.
    Returns (top_track_dicts, albums_list, artist_name, albums_ok).
    - top_track_dicts: output of _fetch_artist_top_tracks (up to 10 slim track dicts)
    - albums_list: [{album_id, name, release_date, tracks?}] — `tracks` (slim) is
      pre-fetched for the first 5 albums (discovery source b) and every album in
      the wildcard release window, so warm runs make no album-track calls
    - artist_name: first album's primary artist name ("" if the albums call failed)
    - ok: False if the albums call or any top-tracks album fetch failed (caller
      must not cache the result — it would overwrite a good row with partial data)
    """
    artist_name = ""
    albums_list: list[dict] = []

    albums_resp = client.get(
        f"artists/{artist_id}/albums"
        f"?include_groups=album,single"
        f"&market={market}"
        f"&limit=10",
        token=token
    )
    albums_ok = "error" not in albums_resp
    if albums_ok:
        for album in (albums_resp.get("items", []) or []):
            if album.get("artists") and not artist_name:
                artist_name = album["artists"][0].get("name", "")
            aid = album.get("id")
            if aid:
                albums_list.append({
                    "album_id": aid,
                    "name": album.get("name", ""),
                    "release_date": album.get("release_date", ""),
                })

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        cutoff_ms = now_ms - WILDCARD_WINDOW_DAYS * 86400 * 1000
        for i, album_dict in enumerate(albums_list):
            if i < 5 or _parse_release_ms(album_dict.get("release_date", "")) >= cutoff_ms:
                tracks = _fetch_album_tracks(album_dict["album_id"], market, token)
                if tracks is not None:
                    album_dict["tracks"] = tracks

    top_track_dicts, top_ok = _fetch_artist_top_tracks(artist_id, market, token)

    return top_track_dicts, albums_list, artist_name, albums_ok and top_ok


def _fetch_album_tracks(album_id: str, market: str, token: str) -> list[dict] | None:
    """Tracks of one album as slim dicts, or None on error (so callers can tell
    "fetched, empty" from "failed" and only cache a real result). Retries/backoff
    and the per-run memo live in client.api — no second retry loop here (it used
    to multiply a timeout into 18 attempts per album)."""
    resp = client.get(f"albums/{album_id}/tracks?market={market}&limit=50", token=token)
    if "error" in resp:
        return None
    return [_slim_track(t) for t in (resp.get("items", []) or [])[:50]]


def _album_tracks(album: dict, market: str, token: str) -> list[dict]:
    """An artist_cache album's tracks: the cached list if non-empty, else a live
    fetch (rows cached before tracks were stored, a failed prefetch, or an empty
    list written by pre-2.17 code on error). Memoized per run in client.api."""
    if album.get("tracks"):
        return album["tracks"]
    return _fetch_album_tracks(album["album_id"], market, token) or []


def _pick_wildcards(
    top_artists: list[tuple[str, float]],
    token: str,
    heard_ids: set[str],
    heard_sigs: set,
    artist_data: dict,
    dry_run: bool = False,
    skipped: set[str] | None = None,
) -> list[dict]:
    """
    Wildcard candidates: tracks from releases in the last WILDCARD_WINDOW_DAYS by
    the top TOP_ARTISTS_N liked artists. Every track of each new release is a
    candidate (not just the first 2); unheard tracks come first (shuffled), then
    heard ones — so a wildcard is a genuinely new track whenever one exists.
    The picker applies guardrails/caps and takes WILDCARD_COUNT.
    """
    market = client.get_user_market(token)
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - WILDCARD_WINDOW_DAYS * 86400 * 1000

    unheard: list[dict] = []
    heard: list[dict] = []
    seen: set[str] = set()
    for artist_id, _ in top_artists:
        data = _get_artist_data(artist_id, market, token, artist_data, dry_run)
        for album in data.get("albums") or []:
            if _parse_release_ms(album.get("release_date", "")) < cutoff_ms:
                continue  # outside the new-release window (or unparseable date)
            album_id = album.get("album_id")
            if not album_id:
                continue
            tracks = _album_tracks(album, market, token)
            for track in tracks:
                c = _candidate(track, album.get("name", ""), "wildcard", artist_id)
                if not c or c["track_id"] in seen or _is_unplayable(c):
                    continue
                if skipped and c["track_id"] in skipped:
                    continue     # skip signals on: a skipped new release is not re-offered
                seen.add(c["track_id"])
                (heard if _is_heard(c, heard_ids, heard_sigs) else unheard).append(c)

    random.shuffle(unheard)
    random.shuffle(heard)
    return unheard + heard


if __name__ == "__main__":
    import scorer as sc
    scores, _ = sc.compute_affinity()
    liked  = sc.get_liked_tracks(scores)
    print(f"Liked tracks: {len(liked)}")
    print(f"Composing playlist...")
    playlist = compose_playlist(scores, dry_run=True)
    for i, t in enumerate(playlist, 1):
        print(f"  {i:2}. [{t['source']:8}] {t['name'][:40]} — {', '.join(t['artist_names'][:2])}")