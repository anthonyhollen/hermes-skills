"""
skipsig.py — pure skip-signal logic (stdlib only: no DB, no network).

Spotify exposes no skip events, and recently-played only records a play's END
time (and omits most skips entirely). Two evidence sources are turned into
per-track signals here:

  1. Direct observation (primary): samples of GET /me/player taken by the
     resident sampler (sampler.py). `Segmenter.feed()` folds samples into one
     segment per track exposure — start ≈ sample time − progress_ms, listened
     time = Σ clamp(Δprogress, 0, Δwall) (pauses and forward seeks never count),
     exit bounded by [last sighting of the old track, start of the new one].
     `classify_segment()` turns a closed segment into a kind:
        completed | skip | skim | pass | early_exit | stopped | neutral
  2. Fallback inference (only for time the sampler did not cover):
     `infer_uncovered()` walks consecutive recorded plays of the managed
     playlist in its stored order; positions passed over between two ascending
     plays are 'inferred_skip' (tiers T1/T2/T3). It also marks uncovered plays
     whose listen time is unknowable ('unknown_play' — e.g. after a pause).

All timestamps are unix ms (Pi clock for samples, Spotify's clock for
played_at; both NTP-synced). Every function here is deterministic given its
inputs so scripts/test_skip_signals.py can replay scenarios exactly.
"""

import copy

# Defaults mirror config.yaml → skip_signals (signals.load_config merges the
# live block over these).
DEFAULTS = {
    "enabled": False,
    "sampler": {
        "playing_s": 3, "burst_s": 1, "burst_window_s": 15, "paused_s": 10,
        "idle_recent_s": 5, "idle_recent_window_s": 2700,
        "idle_s": 15, "night_idle_s": 60, "max_req_per_30s": 20,
        "quiet": ["02:55", "03:20"], "continuity_max_pause_s": 7200,
        "session_gap_s": 3600,
    },
    "judge": {
        "t_pass_ms": 1500, "t_judge_ms": 5000, "skip_completion_max": 0.90,
        "cooldown_completion_max": 0.90,
        "completed_tol_ms": 15000, "session_grace_ms": 20000, "lost_gap_ms": 20000,
    },
    "match_tolerance_ms": 10000,
    "fallback": {"per_track_unrecorded_max_ms": 45000, "min_per_track_ms": 1000,
                 "max_total_ms": 600000, "unknown_slack_ms": 20000,
                 "min_ascending_frac": 0.8, "min_version_pairs": 3, "max_positions": 6},
    "cooldown": {"first_days": 3, "demote_skips": 2, "demote_window_days": 14,
                 "demote_max_days": 30, "relax_on_underfill": True},
    "ban": {"min_negative_days": 2},
}

SESSION_BREAK_MS = 6 * 3600 * 1000   # same 6h cap as poller.MAX_MS_PLAYED_INFERRED
HISTORY = 5                          # recently closed tracks remembered for 'back' detection
NEUTRAL_REASONS = {"back", "restart", "context_switch", "session_start", "private", "lost"}


def merge_config(live: dict | None) -> dict:
    """Deep-merge a (possibly partial / None) live config block over DEFAULTS.
    An already-merged config is returned as-is (no per-call deep copy)."""
    if isinstance(live, dict) and live.get("_merged"):
        return live
    if not isinstance(live, dict):
        live = None                     # e.g. `skip_signals: true` — ignore, keep defaults (flag off)
    out = copy.deepcopy(DEFAULTS)
    for k, v in (live or {}).items():
        if isinstance(out.get(k), dict):
            if isinstance(v, dict):
                out[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            # a null / non-mapping block keeps the defaults (never None)
        elif v is not None:
            out[k] = v
    out["_merged"] = True
    return out


# ── samples ──────────────────────────────────────────────────────────────────

def parse_player(resp, http_status: int, t_mid: int, rtt: int = 0) -> dict:
    """
    One GET /me/player response → a sample dict. status is one of:
      playing | paused  — a track is loaded (track fields set)
      idle              — 204 / nothing active
      non_track         — ad, episode, local file (no track id)
      private           — private session (never used as evidence)
      stale             — progress past the track end (server extrapolating
                          for a phone that went offline) — no information
      error             — request failed — no information
    """
    s = {"t": int(t_mid), "rtt": int(rtt), "http": http_status, "status": "error"}
    err = resp.get("error") if isinstance(resp, dict) else None
    if http_status == 204 or (isinstance(err, dict) and err.get("status") == 204):
        s["status"] = "idle"
        return s
    if not isinstance(resp, dict) or err is not None:
        return s
    device = resp.get("device") or {}
    s.update(
        context_uri=(resp.get("context") or {}).get("uri"),
        shuffle=bool(resp.get("shuffle_state")),
        smart_shuffle=resp.get("smart_shuffle"),
        repeat=resp.get("repeat_state"),
        device_id=device.get("id"),
        device_type=device.get("type"),
        spotify_ts=resp.get("timestamp"),
    )
    if device.get("is_private_session"):
        s["status"] = "private"
        return s
    item = resp.get("item")
    if not item:
        s["status"] = "non_track" if resp.get("is_playing") else "idle"
        return s
    tid = item.get("id")
    if (item.get("type") or "track") != "track" or item.get("is_local") or not tid:
        s["status"] = "non_track"
        return s
    dur = item.get("duration_ms") or 0
    prog = resp.get("progress_ms")
    if prog is None or dur <= 0:
        return s
    s.update(
        track_id=tid,
        linked_from=(item.get("linked_from") or {}).get("id"),
        name=item.get("name"),
        artists=[a.get("name") for a in (item.get("artists") or [])],
        artist_ids=[a.get("id") for a in (item.get("artists") or []) if a.get("id")],
        duration_ms=int(dur),
        progress_ms=int(prog),
    )
    if prog > dur + 5000:
        s["status"] = "stale"
        return s
    s["status"] = "playing" if resp.get("is_playing") else "paused"
    return s


def _same_track(a: dict, b: dict) -> bool:
    ids_a = {a.get("track_id"), a.get("linked_from")} - {None}
    ids_b = {b.get("track_id"), b.get("linked_from")} - {None}
    return bool(ids_a & ids_b)


# ── segmentation ─────────────────────────────────────────────────────────────

class Segmenter:
    """
    Folds samples into per-track segments. State is JSON-serialisable
    (`state()` / Segmenter(cfg, state)) so the sampler can persist the open
    segment and resume it across a restart.
    """

    def __init__(self, cfg: dict | None = None, state: dict | None = None):
        self.cfg = merge_config(cfg)
        st = state or {}
        self.open: dict | None = st.get("open")
        self.last_playing_t: int | None = st.get("last_playing_t")
        self.prev_track_ids: list[str] = st.get("prev_track_ids") or []
        self.last_exit_t: int | None = st.get("last_exit_t")

    def state(self) -> dict:
        return {"open": self.open, "last_playing_t": self.last_playing_t,
                "prev_track_ids": self.prev_track_ids[-HISTORY:], "last_exit_t": self.last_exit_t}

    # -- public -------------------------------------------------------------
    def feed(self, s: dict) -> list[dict]:
        """Consume one sample; return the segments it closed (0, 1 or 2)."""
        st = s.get("status")
        closed: list[dict] = []
        if st in ("error", "stale"):
            return closed                       # no information
        o = self.open
        max_pause = self.cfg["sampler"]["continuity_max_pause_s"] * 1000
        if st == "idle":
            if o is not None:
                o["last_absent"] = s["t"]     # proof the track was NOT playing now
                if s["t"] - o["last_seen"] > max_pause:
                    closed.append(self._close(o, None, s["t"], hint="stopped"))
            return closed
        if st in ("private", "non_track"):
            if o is not None:
                closed.append(self._close(o, None, s["t"],
                                          hint="private" if st == "private" else "context_switch"))
            return closed
        # playing / paused with a track
        if o is not None and self._continues(o, s):
            self._update(o, s)
        else:
            if o is not None:
                if _same_track(o, s) and self._paused_too_long(o, s):
                    # Resumed (or still paused) long after it last played — e.g. the
                    # app restoring last night's track: that exposure ended when it
                    # stopped; the resume opens a new session segment.
                    closed.append(self._close(o, None, o["last_seen"], hint="stopped"))
                else:
                    closed.append(self._close(o, s, s["t"]))
            # Only a PLAYING sample opens an exposure (a track loaded but paused
            # isn't being heard; a long pause must not spawn empty segments).
            self.open = self._open(s) if st == "playing" else None
        if st == "playing":
            self.last_playing_t = s["t"]
        return closed

    def flush(self, t_now: int, hint: str = "lost") -> list[dict]:
        """Close the open segment (e.g. on shutdown when resuming is impossible)."""
        if self.open is None:
            return []
        return [self._close(self.open, None, t_now, hint=hint)]

    # -- internals ----------------------------------------------------------
    def _paused_too_long(self, o: dict, s: dict) -> bool:
        """Continuity is measured from when the track last PLAYED — paused
        samples keep refreshing last_seen, so they must not extend it."""
        ref = o.get("last_play_t") or o["first_seen"]
        return s["t"] - ref > self.cfg["sampler"]["continuity_max_pause_s"] * 1000

    def _continues(self, o: dict, s: dict) -> bool:
        if not _same_track(o, s) or s.get("context_uri") != o.get("context_uri"):
            return False
        dw = s["t"] - o["last_seen"]
        if self._paused_too_long(o, s):
            return False
        # Restart / repeat-one: progress fell back AND is consistent with having
        # started over since the last sighting (a backward seek to mid-track lands
        # further in than the elapsed time allows).
        if s["progress_ms"] < o["last_progress"] - 2000 and s["progress_ms"] <= dw + 3000:
            return False
        return True

    def _open(self, s: dict) -> dict:
        gap_ms = self.cfg["sampler"]["session_gap_s"] * 1000
        session_start = self.last_playing_t is None or s["t"] - self.last_playing_t >= gap_ms
        prog = s["progress_ms"]
        resumed = session_start and prog > 5000   # the app resumed an old track mid-way
        if resumed:
            listened = 0
        elif self.last_playing_t is not None:
            listened = max(0, min(prog, s["t"] - self.last_playing_t))
        else:
            listened = prog
        return {
            "track_id": s["track_id"], "linked_from": s.get("linked_from"),
            "name": s.get("name"), "artists": s.get("artists") or [],
            "artist_ids": s.get("artist_ids") or [],
            "duration_ms": s["duration_ms"], "context_uri": s.get("context_uri"),
            "first_seen": s["t"], "last_seen": s["t"], "started_at": s["t"] - prog,
            "first_progress": prog, "last_progress": prog, "max_progress": prog,
            "listened": listened, "last_status": s["status"], "n_samples": 1,
            "seek": False, "session_start": session_start, "resumed": resumed,
            "shuffle": s.get("shuffle"), "smart_shuffle": s.get("smart_shuffle"),
            "repeat": s.get("repeat"), "device_type": s.get("device_type"),
            "prev_track_ids": list(self.prev_track_ids[-HISTORY:]),
            "last_play_t": s["t"] if s["status"] == "playing" else None,
        }

    def _update(self, o: dict, s: dict):
        dw = s["t"] - o["last_seen"]
        dp = s["progress_ms"] - o["last_progress"]
        o["listened"] += max(0, min(dp, dw))
        if o["last_status"] == "playing" and s["status"] == "playing" and abs(dp - dw) > 2500:
            o["seek"] = True
        o["last_seen"] = s["t"]
        o["last_progress"] = s["progress_ms"]
        o["max_progress"] = max(o["max_progress"], s["progress_ms"])
        o["last_status"] = s["status"]
        if s["status"] == "playing":
            o["last_play_t"] = s["t"]
        o["n_samples"] += 1
        o["device_type"] = s.get("device_type") or o.get("device_type")
        o["shuffle"] = s.get("shuffle")
        o["smart_shuffle"] = s.get("smart_shuffle")

    def _close(self, o: dict, new: dict | None, t: int, hint: str | None = None) -> dict:
        j = self.cfg["judge"]
        dur = o["duration_ms"]
        exit_lo = o["last_seen"]
        new_start = (new["t"] - new["progress_ms"]) if new else t
        exit_hi = max(exit_lo, min(t, new_start))
        remaining = max(0, dur - o["last_progress"])
        lost_gap = j["lost_gap_ms"]
        # The exit is OBSERVED only when the old track's last sighting and the new
        # track's start are close together. A 204 in between ("nothing playing")
        # is ambiguous: a phone in a tunnel keeps playing offline, a closed app
        # does not — so an exit bridged by a 204 is unobserved ('lost', neutral).
        absent = o.get("last_absent") or 0
        bridged_by_204 = absent > exit_lo
        playing_last = o["last_status"] == "playing"
        span = max(0, exit_hi - exit_lo)
        observed = span <= lost_gap and not bridged_by_204
        tail = min(span, remaining)            # upper bound: it can't outlast the exit or its end
        seg = dict(o)
        seg.update(exit_lo=exit_lo, exit_hi=exit_hi, listened_lo=o["listened"],
                   listened_hi=o["listened"] + tail, end_progress_hi=o["last_progress"] + tail,
                   remaining_ms=remaining, bridged_by_204=bridged_by_204,
                   next_track_id=(new or {}).get("track_id"),
                   next_context_uri=(new or {}).get("context_uri"),
                   next_progress=(new or {}).get("progress_ms"))
        if remaining <= j["completed_tol_ms"]:
            reason = "completed"
        elif playing_last and observed and remaining <= span + j["completed_tol_ms"]:
            reason = "completed"
        elif hint in ("stopped", "private", "lost"):
            reason = hint
        elif not observed:
            reason = "lost"                      # the exit itself was not observed
        elif hint == "context_switch" or (new is not None and new.get("context_uri") != o.get("context_uri")):
            reason = "context_switch"
        elif new is not None and _same_track(o, new):
            reason = "restart"
        elif new is not None and new.get("track_id") in o.get("prev_track_ids", []):
            reason = "back"
        elif o["session_start"] and seg["listened_hi"] < j["session_grace_ms"]:
            reason = "session_start"
        else:
            reason = "early"                     # next / jump — decided with positions
        seg["end_reason"] = reason
        if reason == "early":
            self.last_exit_t = exit_hi
        self.prev_track_ids = (self.prev_track_ids + [o["track_id"]])[-HISTORY:]
        if self.open is o:
            self.open = None
        return seg


# ── classification ───────────────────────────────────────────────────────────

class VersionIndex:
    """Stored playlist versions: [{version_id, applied_at, tracks:[track_id,...]}]."""

    def __init__(self, versions: list[dict]):
        self.versions = sorted(versions or [], key=lambda v: v["applied_at"])
        self._pos = [{tid: i + 1 for i, tid in enumerate(v["tracks"]) if tid} for v in self.versions]

    def candidates(self, t: int) -> list[int]:
        """Indexes of the version live at t and the one before it (a client can
        keep playing yesterday's copy across the 03:00 replace)."""
        idx = None
        for i, v in enumerate(self.versions):
            if v["applied_at"] <= t:
                idx = i
        if idx is None:
            return []
        return [idx] + ([idx - 1] if idx > 0 else [])

    def position(self, vi: int, *track_ids) -> int | None:
        for tid in track_ids:
            if tid and tid in self._pos[vi]:
                return self._pos[vi][tid]
        return None

    def version_id(self, vi: int) -> int:
        return self.versions[vi]["version_id"]

    def size(self, vi: int) -> int:
        return len(self.versions[vi]["tracks"])

    def track_at(self, vi: int, pos: int) -> str | None:
        tracks = self.versions[vi]["tracks"]
        return tracks[pos - 1] if 1 <= pos <= len(tracks) else None


def locate(seg: dict, versions: VersionIndex) -> tuple[int | None, int | None]:
    """(version_id, position) of a segment's track in the version live at its exit."""
    for vi in versions.candidates(seg.get("exit_hi") or seg.get("first_seen") or 0):
        pos = versions.position(vi, seg.get("track_id"), seg.get("linked_from"))
        if pos is not None:
            return versions.version_id(vi), pos
    return None, None


def classify_segment(seg: dict, cfg: dict | None = None, versions: VersionIndex | None = None) -> dict:
    """
    Add kind (+ end_reason next/jump, version_id, position) to a closed segment.
      completed  — played to (near) the end
      stopped    — playback stopped on this track (session end; not a skip)
      neutral    — back / restart / context switch / session-start grace /
                   private / lost (exit not observed) — no evidence either way
      pass       — early exit with < t_pass listened (a pass-through)
      skim       — early exit, t_pass ≤ listened < t_judge, position < skip_completion_max
      skip       — early exit, listened ≥ t_judge, position < skip_completion_max
                   (user rule: leaving before 90% is a skip)
      early_exit — early exit at/after skip_completion_max (a listen: score brackets)
    """
    c = merge_config(cfg)
    j = c["judge"]
    out = dict(seg)
    if versions is not None:
        out["version_id"], out["position"] = locate(seg, versions)
    reason = seg.get("end_reason")
    if reason == "completed":
        out["kind"] = "completed"
        return out
    if reason == "stopped":
        out["kind"] = "stopped"
        return out
    if reason in NEUTRAL_REASONS:
        out["kind"] = "neutral"
        return out
    # early exit: next vs jump from positions of old → new; moving BACKWARDS in
    # the stored order (tapping an earlier track) is neutral like "previous".
    out["end_reason"] = "next"
    if versions is not None and out.get("position") and seg.get("next_track_id"):
        for vi in versions.candidates(seg.get("exit_hi") or 0):
            p_old = versions.position(vi, seg.get("track_id"), seg.get("linked_from"))
            p_new = versions.position(vi, seg.get("next_track_id"))
            if p_old and p_new and not seg.get("shuffle"):
                if p_new < p_old:
                    out["end_reason"], out["kind"] = "back", "neutral"
                    return out
                out["end_reason"] = "next" if p_new == p_old + 1 else "jump"
                break
    dur = max(1, seg.get("duration_ms") or 1)
    completion = max(seg.get("end_progress_hi") or 0, seg.get("listened_hi") or 0) / dur
    if seg.get("resumed"):
        completion = (seg.get("end_progress_hi") or 0) / dur   # judged on position
    if (seg.get("listened_hi") or 0) < j["t_pass_ms"]:
        out["kind"] = "pass"
    elif completion >= j["skip_completion_max"]:
        out["kind"] = "early_exit"
    elif (seg.get("listened_lo") or 0) >= j["t_judge_ms"]:
        out["kind"] = "skip"
    else:
        out["kind"] = "skim"
    return out


# ── matching sampler segments ↔ recorded plays ───────────────────────────────

def _match_anchor(s: dict) -> int:
    """Where the recorded play's END should fall: the observed exit, or — when the
    exit was not observed (stopped / lost) — the natural end if it kept playing."""
    if s.get("end_reason") in ("stopped", "lost"):
        tail = (s.get("remaining_ms") or 0) if s.get("last_status") == "playing" else 0
        return s["exit_lo"] + tail
    return s["exit_hi"]


def match_segments_to_events(segments: list[dict], events: list[dict], cfg: dict | None = None) -> dict:
    """
    One-to-one, nearest-first match of closed segments to recorded plays:
    same track (id or linked_from) and played_at within
    [first_seen + 1s, exit_hi + match_tolerance] — a recorded end well before the
    last sighting means the tail was server-extrapolated (see
    truncate_at_recorded_end). Returns {segment index: event index}.
    """
    c = merge_config(cfg)
    tol = c["match_tolerance_ms"]
    by_track: dict[str, list[int]] = {}
    for ei, e in enumerate(events):
        by_track.setdefault(e["track_id"], []).append(ei)
    pairs = []
    for si, s in enumerate(segments):
        if s.get("exit_hi") is None:
            continue
        anchor = _match_anchor(s)
        lo = (s.get("first_seen") or s["exit_lo"]) + 1000     # a recorded END inside the span
        for tid in {s.get("track_id"), s.get("linked_from")} - {None}:
            for ei in by_track.get(tid, []):
                pa = events[ei]["played_at"]
                if lo <= pa <= s["exit_hi"] + tol:
                    pairs.append((abs(pa - anchor), si, ei))
    pairs.sort()
    used_s, used_e, out = set(), set(), {}
    for _, si, ei in pairs:
        if si in used_s or ei in used_e:
            continue
        used_s.add(si)
        used_e.add(ei)
        out[si] = ei
    # Second pass, wider (60s): a recorded end stamped after a long crossfade must
    # still pair with its segment — unmatched, both would be scored (double count).
    late = []
    for si, s in enumerate(segments):
        if si in used_s or s.get("exit_hi") is None:
            continue
        for tid in {s.get("track_id"), s.get("linked_from")} - {None}:
            for ei in by_track.get(tid, []):
                pa = events[ei]["played_at"]
                if ei not in used_e and s["exit_hi"] + tol < pa <= s["exit_hi"] + 60000:
                    late.append((pa - s["exit_hi"], si, ei))
    for _, si, ei in sorted(late):
        if si in used_s or ei in used_e:
            continue
        used_s.add(si)
        used_e.add(ei)
        out[si] = ei
    return out


def truncate_at_recorded_end(s: dict, played_at: int, cfg: dict | None = None) -> dict:
    """
    Spotify recorded this play ENDING well before the sampler last saw it: the
    server kept extrapolating progress for a phone that had gone offline (or the
    user skipped just before a sampling gap). Cut the segment at the recorded
    end, assuming it played continuously up to there. Never extends a segment.
    """
    ref = s.get("last_play_t") or s.get("exit_lo") or 0     # a recorded end during a pause is consistent
    cut = ref - played_at
    if cut <= 3000:
        return s
    c = merge_config(cfg)
    out = dict(s)
    lo = max(0, (s.get("listened_lo") or 0) - cut)
    prog = max(0, (s.get("last_progress") or 0) - cut)
    out.update(listened=lo, listened_lo=lo, listened_hi=lo, end_progress_hi=prog, last_progress=prog,
               exit_lo=played_at, exit_hi=played_at, bridged_by_204=False, truncated=True,
               remaining_ms=max(0, (s.get("duration_ms") or 0) - prog))
    out["end_reason"] = "completed" if out["remaining_ms"] <= c["judge"]["completed_tol_ms"] else "early"
    return out


def listened_with_recorded_end(s: dict, played_at: int) -> int:
    """
    Listened ms of a segment refined by its matched recorded END time: Spotify's
    played_at says when the track actually stopped, which pins down an exit the
    sampler only bracketed (or never saw, for stopped / lost segments).
    """
    dur = s.get("duration_ms") or 0
    base = s.get("listened_lo") or 0
    # Across a 204 the phone may have been paused or playing offline — the
    # recorded end can't say which, so nothing is added.
    if s.get("last_status") == "playing" and not s.get("bridged_by_204"):
        base += max(0, min(played_at - s["exit_lo"], s.get("remaining_ms") or 0))
    return min(dur, base)


# ── fallback inference over uncovered time ───────────────────────────────────

def covered(coverage: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """True if one sampler coverage run spans [lo, hi]."""
    return any(a <= lo and b >= hi for a, b in coverage)


class _Coverage:
    """covered() over many runs: runs sorted by start, prefix-max of their ends."""

    def __init__(self, runs):
        import bisect
        self._bisect = bisect
        runs = sorted(runs)
        self.starts = [a for a, _ in runs]
        self.maxend, m = [], float("-inf")
        for _, b in runs:
            m = max(m, b)
            self.maxend.append(m)

    def covers(self, lo: int, hi: int) -> bool:
        i = self._bisect.bisect_right(self.starts, lo) - 1   # runs starting at or before lo
        return i >= 0 and self.maxend[i] >= hi


def infer_uncovered(events: list[dict], versions: VersionIndex, coverage: list[tuple[int, int]],
                    segments: list[dict], cfg: dict | None, playlist_uri: str) -> tuple[list[dict], list[dict]]:
    """
    Walk consecutive recorded plays (sorted by played_at, track_id) whose gap the
    sampler did NOT cover:
      - both plays from the managed playlist (context_uri), in one stored
        version, positions ascending a → b: positions a+1..b−1 were passed over.
        Unrecorded time idle = gap − duration_b (a lower bound on time spent on
        them). idle < 1s/track → tap (nothing); ≤ 45s/track → T1; longer but
        ≤ 10 min in total → T2; more → a pause (nothing).
      - T3: a in the version, b in the playlist context but NOT in the version
        (Spotify autoplay after the end): positions a+1..N passed over.
      - backwards / other contexts / ambiguous versions → no positional inference.
    Also returns uncovered plays whose own listen time is unknowable ('unknown_play'):
    the time before them not explained by inferred pass-overs exceeds the slack
    (a pause), or they follow a > 6h break. The poller scores those as full plays.
    Returns (inferred_skip signals, unknown_play marks).
    """
    c = merge_config(cfg)
    fb = c["fallback"]
    per_max, per_min = fb["per_track_unrecorded_max_ms"], fb["min_per_track_ms"]
    total_max, slack = fb["max_total_ms"], fb["unknown_slack_ms"]
    max_n = fb["max_positions"]
    evs = sorted(events, key=lambda e: (e["played_at"], e["track_id"]))
    sequential = _sequential_sessions(evs, versions, segments, playlist_uri, fb)
    spans_by_track: dict[str, list[tuple[int, int]]] = {}
    for s in segments:
        if s.get("end_reason") == "lost":
            continue                    # exit unobserved — the fallback may still speak
        for tid in {s.get("track_id"), s.get("linked_from")} - {None}:
            spans_by_track.setdefault(tid, []).append((s.get("first_seen") or 0, s.get("exit_hi") or 0))
    cov = _Coverage(coverage)
    signals, unknown = [], []

    def has_segment(tid, lo, hi):
        return any(a <= hi and b >= lo for a, b in spans_by_track.get(tid, ()))

    def in_no_version(e, t):
        return all(versions.position(v, e["track_id"]) is None for v in versions.candidates(t))

    for i, (a, b) in enumerate(zip(evs, evs[1:])):
        lo, hi = a["played_at"], b["played_at"]
        if cov.covers(lo, hi):
            continue
        gap = hi - lo
        if gap <= 0:
            continue
        if gap > SESSION_BREAK_MS:
            unknown.append({"track_id": b["track_id"], "played_at": hi, "reason": "session_break"})
            continue
        idle = gap - (b.get("duration_ms") or 0)
        explained, inferred_here = 0, []
        in_ctx = a.get("context_uri") == playlist_uri and b.get("context_uri") == playlist_uri
        chosen = None
        if in_ctx and sequential.get(id(a)):
            # The version live when `a` ended; a client that is still on yesterday's
            # copy (session spanning the 03:00 replace, or a resumed old track) is
            # only assumed when `a` is absent from the live version. Core tracks
            # recur across days, so the two versions are never cross-checked.
            cands = versions.candidates(lo)
            vi = None
            if cands and versions.position(cands[0], a["track_id"]) is not None:
                vi = cands[0]
            elif len(cands) > 1 and versions.position(cands[1], a["track_id"]) is not None:
                vi = cands[1]
            if vi is not None:
                pa = versions.position(vi, a["track_id"])
                pb = versions.position(vi, b["track_id"])
                in_any = any(versions.position(v, b["track_id"]) is not None for v in cands)
                nxt = evs[i + 2] if i + 2 < len(evs) else None
                autoplay_follows = (nxt is None or nxt["played_at"] - hi > 30 * 60 * 1000
                                    or (nxt.get("context_uri") == playlist_uri and in_no_version(nxt, hi)))
                if pb is not None and pb > pa + 1:
                    chosen = (vi, pa, pb, "T1")
                elif (pb is None and not in_any and pa < versions.size(vi)
                      and versions.size(vi) - pa <= max_n and autoplay_follows):
                    chosen = (vi, pa, versions.size(vi) + 1, "T3")   # autoplay after the end
            if chosen:
                vi, pa, pb, tier = chosen
                n = pb - pa - 1
                # > max_positions passed over is a tap / queue jump, not a run of skips
                if 0 < n <= max_n and per_min * n <= idle <= total_max:
                    if tier == "T1" and idle > per_max * n:
                        tier = "T2"
                    for pos in range(pa + 1, pb):
                        tid = versions.track_at(vi, pos)
                        if not tid or has_segment(tid, lo, hi):
                            continue
                        inferred_here.append({
                            "track_id": tid, "at_ms": lo + (hi - lo) // 2, "kind": "inferred_skip",
                            "source": "fallback", "tier": tier + ("-dense" if tier == "T1" and n > 1 else ""),
                            "exposure_ms": None, "exposure_hi_ms": int(idle / n),
                            "version_id": versions.version_id(vi), "position": pos,
                            "context_uri": playlist_uri,
                        })
                    explained = min(idle, per_max * n)
        signals.extend(inferred_here)
        if idle - explained > slack and not has_segment(b["track_id"], lo, hi):
            unknown.append({"track_id": b["track_id"], "played_at": hi, "reason": "unexplained_gap",
                            "idle_ms": int(idle - explained)})
    return signals, unknown


def _sequential_sessions(evs: list[dict], versions: VersionIndex, segments: list[dict],
                         playlist_uri: str, fb: dict) -> dict[int, bool]:
    """
    {id(event): True} for plays in listening sessions (split at gaps > 2h) that
    were clearly played IN ORDER — positional inference is meaningless under
    shuffle / Smart Shuffle, which recently-played doesn't report. A session
    qualifies when, over consecutive plays found in one stored version, at least
    min_version_pairs pairs exist, ≥ min_ascending_frac of them ascend, at least
    one is exactly adjacent, and no sampler segment in it saw shuffle on.
    """
    out: dict[int, bool] = {}
    sessions, cur = [], []
    for e in evs:
        if cur and e["played_at"] - cur[-1]["played_at"] > 2 * 3600 * 1000:
            sessions.append(cur)
            cur = []
        cur.append(e)
    if cur:
        sessions.append(cur)
    shuffled = [(s.get("first_seen") or 0, s.get("exit_hi") or 0) for s in segments
                if s.get("shuffle") or s.get("smart_shuffle")]
    for sess in sessions:
        asc = desc = adjacent = 0
        for a, b in zip(sess, sess[1:]):
            if a.get("context_uri") != playlist_uri or b.get("context_uri") != playlist_uri:
                continue
            cands = versions.candidates(a["played_at"])
            if not cands:
                continue
            pa, pb = versions.position(cands[0], a["track_id"]), versions.position(cands[0], b["track_id"])
            if pa is None or pb is None or pa == pb:
                continue
            if pb > pa:
                asc += 1
                adjacent += pb == pa + 1
            else:
                desc += 1
        lo, hi = sess[0]["played_at"], sess[-1]["played_at"]
        ok = (asc + desc >= fb["min_version_pairs"] and adjacent >= 1
              and asc >= fb["min_ascending_frac"] * (asc + desc)
              and not any(x <= hi and y >= lo for x, y in shuffled))
        for e in sess:
            out[id(e)] = ok
    return out
