"""
skip_signals.py — derived skip signals → policy, plus the shadow-mode digest.

rebuild_signals() regenerates the derived `skip_signal` table from scratch on
every poll (and before the nightly digest) out of:
  - play_segment      — what the resident sampler observed (skipsig.Segmenter)
  - play_events       — what Spotify recorded (recently-played)
  - playlist_version  — the exact order each refresh wrote
  - sampler_coverage  — when the sampler was actually watching
Sampler segments are authoritative where they exist; the positional fallback
(skipsig.infer_uncovered) only speaks for time the sampler did not cover.

Policy (read by scorer / composer ONLY when config.yaml → skip_signals.enabled):
  cooldown  — any skip evidence (sampler skip / skim / early exit below
              judge.cooldown_completion_max, fallback inferred_skip) keeps a
              track out of the next `first_days` refreshes (reversible)
  demote    — `demote_skips` sampler skips/skims within `demote_window_days`
              keep it out of core + fallback until a later completed play
              (at most `demote_max_days`)
  heard     — every exposure the SAMPLER observed marks a track heard
              (discovery/wildcard); fallback inferences never do
  scoring   — sampler listened time replaces the gap inference for matched
              plays; unrecorded sampler skips become derived scoring events;
              uncovered plays after an unexplained pause (fallback
              'unknown_play') are unscored
  bans      — a ban the recorded plays alone would not cause also needs
              negative evidence on `ban.min_negative_days` separate listening
              days (04:00→04:00 SGT); fallback inferences never count
With the flag off everything above is computed (for the digest) but ignored.

CLI (run from src/):
  python3 skip_signals.py rebuild
  python3 skip_signals.py digest [YYYY-MM-DD]
  python3 skip_signals.py label YYYY-MM-DD ok                  # digest was right
  python3 skip_signals.py label YYYY-MM-DD "17 not, 30 skip, 12 maybe"
  python3 skip_signals.py accuracy
  python3 skip_signals.py auto-accept          # no reply = correct (the digest wrapper runs it nightly)
"""

import os, sys
from datetime import datetime, timezone, timedelta
import db, skipsig

# = refresher.PLAYLIST_ID (not imported: refresher → poller → this module)
PLAYLIST_ID = "4y4Fd7Pg52Ubi0zqJBNJnm"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
SGT = timezone(timedelta(hours=8))
DAY_MS = 86400 * 1000
REBUILD_DAYS = 90
_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

SKIP_KINDS = ("skip", "skim")                       # sampler kinds that count as a skip
SCORED_SAMPLER_KINDS = ("completed", "skip", "early_exit", "stopped", "neutral", "pass", "skim")
DAY_SHIFT_MS = 4 * 3600 * 1000   # a "listening day" runs 04:00→04:00 SGT (a session over midnight is one day)


def load_config() -> dict:
    """config.yaml → skip_signals merged over skipsig.DEFAULTS (read on every call,
    so flipping `enabled` needs no restart of the cron jobs)."""
    live = {}
    if os.path.exists(_CFG_PATH):
        import yaml
        with open(_CFG_PATH) as f:
            live = (yaml.safe_load(f) or {}).get("skip_signals") or {}
    return skipsig.merge_config(live if isinstance(live, dict) else None)


def enabled() -> bool:
    """Strict: only a real true (or 'true'/'yes'/'on'/'1') enables — a quoted
    "false" must never switch skip signals on."""
    v = load_config().get("enabled")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "on", "1")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, SGT).strftime("%Y-%m-%d")


def _listening_day(ms: int) -> str:
    return _day(ms - DAY_SHIFT_MS)


def _ids(r: dict) -> set[str]:
    return {r["track_id"], r.get("alt_track_id")} - {None}


# ── rebuild ──────────────────────────────────────────────────────────────────

def _segment_row(s: dict, ev: dict | None) -> dict:
    """One sampler segment → skip_signal row. score_ms is the listened time the
    scorer uses when the flag is on (None = the play is not scored)."""
    kind, dur = s["kind"], s.get("duration_ms") or 0
    if ev is not None:
        listened = skipsig.listened_with_recorded_end(s, ev["played_at"])
    else:
        listened = s.get("listened_hi") or 0
    # a track resumed mid-way is judged on where it was LEFT, not on the
    # seconds heard since the resume
    heard = min(dur, s.get("end_progress_hi") or listened) if s.get("resumed") else listened
    if kind == "completed":
        score = dur
    elif kind in ("skip", "skim"):
        # A skip scores as a skip however far in it came (user rule: leaving
        # before 90% is a skip) — capped inside the skip bracket (< 30%) so it can
        # never count as a partial listen. A very early exit keeps the harder
        # < 10% "hard skip" weight.
        score = min(heard, int(0.29 * dur)) if dur else None
    elif kind == "early_exit":
        score = heard
    elif kind == "stopped":
        lo = listened if ev is not None else (s.get("listened_lo") or 0)
        score = lo if dur and lo >= 0.5 * dur else None      # session end ≠ dislike
    elif s.get("end_reason") == "lost" and ev is not None and not s.get("bridged_by_204"):
        score = listened                                     # recorded end resolves the unobserved exit
    else:
        score = None                                         # neutral / pass / 204-bridged
    # Keyed by the RECORDED id when matched (relinked tracks), the other id kept aside.
    tid = ev["track_id"] if ev is not None else s["track_id"]
    alt = ({s.get("track_id"), s.get("linked_from")} - {None, tid}) or {None}
    return {
        "track_id": tid, "alt_track_id": sorted(alt, key=str)[0], "at_ms": s["exit_hi"], "kind": kind,
        "source": "sampler", "tier": "truncated" if s.get("truncated") else None,
        "exposure_ms": s.get("listened_lo"), "exposure_hi_ms": s.get("listened_hi"),
        "duration_ms": dur, "end_reason": s.get("end_reason"), "version_id": s.get("version_id"),
        "position": s.get("position"), "context_uri": s.get("context_uri"),
        "segment_id": s.get("segment_id"), "played_at": ev["played_at"] if ev else None,
        "score_ms": score,
    }


def rebuild_signals(now_ms: int | None = None) -> dict:
    """Regenerate skip_signal from scratch (idempotent; late plays self-correct)."""
    cfg = load_config()
    now = now_ms or _now_ms()
    since = now - REBUILD_DAYS * DAY_MS
    segs = db.get_segments_since(since)
    events = db.get_events_since(since - DAY_MS)          # one extra day: predecessors
    versions = skipsig.VersionIndex(db.get_playlist_versions(since))
    coverage = db.get_coverage_since(since - DAY_MS)

    classified = [skipsig.classify_segment(s, cfg, versions) for s in segs]
    matches = skipsig.match_segments_to_events(classified, events, cfg)
    rows, matched = [], set()
    for si, s in enumerate(classified):
        ei = matches.get(si)
        ev = events[ei] if ei is not None else None
        if ev is not None:
            matched.add(ei)
            if ev["played_at"] < (s.get("exit_lo") or 0) - 3000:
                # Recorded end well before the last sighting: the tail was
                # server-extrapolated — cut there and judge the exposure again.
                s = skipsig.classify_segment(skipsig.truncate_at_recorded_end(s, ev["played_at"], cfg),
                                             cfg, versions)
        rows.append(_segment_row(s, ev))

    inferred, unknown = skipsig.infer_uncovered(events, versions, coverage, classified, cfg, PLAYLIST_URI)
    # Plays from before the first stored playlist version (pre-v2.18 history)
    # keep their original scoring — the fallback only speaks from then on.
    first_version = versions.versions[0]["applied_at"] if versions.versions else None
    unknown = [u for u in unknown if first_version is not None and u["played_at"] >= first_version]
    for f in inferred:
        rows.append({**f, "duration_ms": None, "score_ms": None})
    for u in unknown:
        rows.append({"track_id": u["track_id"], "at_ms": u["played_at"], "kind": "unknown_play",
                     "source": "fallback", "tier": u["reason"], "exposure_hi_ms": u.get("idle_ms"),
                     "played_at": u["played_at"], "score_ms": None})
    # Diagnostic: recorded plays inside sampler coverage that no segment explains.
    for ei, e in enumerate(events):
        if ei in matched or e["played_at"] < since:
            continue
        if skipsig.covered(coverage, e["played_at"] - (e.get("duration_ms") or 0), e["played_at"]):
            rows.append({"track_id": e["track_id"], "at_ms": e["played_at"], "kind": "coverage_miss",
                         "source": "sampler", "played_at": e["played_at"],
                         "duration_ms": e.get("duration_ms"), "score_ms": None})
    db.replace_skip_signals(rows)
    return {"segments": len(segs), "signals": len(rows), "inferred": len(inferred),
            "unknown": len(unknown), "matched": len(matched)}


# ── policy readers (flag on) ─────────────────────────────────────────────────

def _claimed(rows: list[dict]) -> set[tuple[str, int]]:
    """Recorded plays whose meaning a signal already decided: matched by a sampler
    segment, or marked unknown. (coverage_miss rows only point AT unexplained plays.)"""
    return {(r["track_id"], r["played_at"]) for r in rows
            if r.get("played_at") is not None and r["kind"] != "coverage_miss"}


def scoring_inputs(cutoff_ms: int) -> tuple[dict, list[dict]]:
    """
    (overrides, derived_events) for scorer.compute_affinity:
      overrides      {(track_id, played_at): ms_played or None} for recorded plays
                     the sampler observed (None = don't score) and uncovered plays
                     whose listen time is unknowable (fallback 'unknown_play')
      derived_events sampler-observed plays Spotify never recorded that carry a
                     score (skips, early exits, completions)
    """
    rows = db.get_skip_signals_since(cutoff_ms)
    overrides, derived = {}, []
    for r in rows:
        if r["source"] != "sampler" or r["kind"] not in SCORED_SAMPLER_KINDS:
            continue
        if r["played_at"] is not None:
            overrides[(r["track_id"], r["played_at"])] = r["score_ms"]
        elif r["score_ms"] is not None:
            derived.append({"track_id": r["track_id"], "played_at": r["at_ms"],
                            "duration_ms": r["duration_ms"], "ms_played": r["score_ms"]})
    for r in rows:
        if r["kind"] == "unknown_play":
            overrides.setdefault((r["track_id"], r["played_at"]), None)
    return overrides, derived


def _recorded_full_plays(since_ms: int, rows: list[dict]) -> dict[str, list[int]]:
    """Recorded full listens (≥90%) no signal claimed — for clearing demotions."""
    claimed = _claimed(rows)
    out: dict[str, list[int]] = {}
    for e in db.get_events_since(since_ms):
        dur, ms = e.get("duration_ms") or 0, e.get("ms_played")
        if (e["track_id"], e["played_at"]) in claimed or ms is None or not dur:
            continue
        if ms / dur >= 0.9:
            out.setdefault(e["track_id"], []).append(e["played_at"])
    return out


def _is_cooldown_evidence(r: dict, judge: dict) -> bool:
    """Any skip evidence: a sampler skip / skim, a sampler early exit below
    cooldown_completion_max (e.g. ~67s of a 170s track), or a fallback pass-over."""
    if r["kind"] == "inferred_skip":
        return True
    if r["source"] != "sampler":
        return False
    if r["kind"] in SKIP_KINDS:
        return True
    if r["kind"] == "early_exit" and r.get("duration_ms"):
        return (r.get("score_ms") or 0) / r["duration_ms"] < judge["cooldown_completion_max"]
    return False


def cooldown_state(now_ms: int | None = None) -> dict:
    """
    {"cooldown": {track_id: until_ms}, "demoted": {track_id: since_ms},
     "recent24h": {track_id}} — see the module docstring for the rules.
    recent24h (never re-added on underfill) holds only SAMPLER evidence.
    """
    cfg = load_config()
    c, judge = cfg["cooldown"], cfg["judge"]
    now = now_ms or _now_ms()
    since = now - REBUILD_DAYS * DAY_MS
    rows = db.get_skip_signals_since(since)
    first = int(c["first_days"] * DAY_MS)
    cooldown, recent, skips, completions = {}, set(), {}, {}
    for r in rows:
        sampler_skip = r["source"] == "sampler" and r["kind"] in SKIP_KINDS
        if _is_cooldown_evidence(r, judge):
            for tid in _ids(r):
                if r["at_ms"] + first > now:
                    cooldown[tid] = max(cooldown.get(tid, 0), r["at_ms"] + first)
                if r["source"] == "sampler" and now - r["at_ms"] < DAY_MS:
                    recent.add(tid)
        if sampler_skip:
            for tid in _ids(r):
                skips.setdefault(tid, []).append(r["at_ms"])
        if r["source"] == "sampler" and r["kind"] == "completed":
            for tid in _ids(r):
                completions.setdefault(tid, []).append(r["at_ms"])
    for tid, ts in _recorded_full_plays(since, rows).items():
        completions.setdefault(tid, []).extend(ts)

    n, window = int(c["demote_skips"]), int(c["demote_window_days"] * DAY_MS)
    max_demote = int(c["demote_max_days"] * DAY_MS)
    demoted = {}
    for tid, ts in skips.items():
        ts.sort()
        trigger = None
        for i in range(n - 1, len(ts)):
            if ts[i] - ts[i - n + 1] <= window:
                trigger = ts[i]
        if trigger is None or now - trigger > max_demote:
            continue                                   # expires: never an indefinite ban
        if not any(t > trigger for t in completions.get(tid, [])):
            demoted[tid] = trigger
    return {"cooldown": cooldown, "demoted": demoted, "recent24h": recent}


def heard_ids(now_ms: int | None = None) -> set[str]:
    """Tracks the sampler actually saw (any exposure) — discovery/wildcard treat
    them as heard. Fallback inferences are too uncertain to retire a candidate."""
    now = now_ms or _now_ms()
    out = set()
    for r in db.get_skip_signals_since(now - REBUILD_DAYS * DAY_MS):
        if r["source"] == "sampler" and r["kind"] != "coverage_miss":
            out |= _ids(r)
    return out


def skipped_ids(now_ms: int | None = None) -> set[str]:
    """Tracks the sampler saw skipped/skimmed — never re-offered as 'heard' wildcards."""
    now = now_ms or _now_ms()
    out = set()
    for r in db.get_skip_signals_since(now - REBUILD_DAYS * DAY_MS):
        if r["source"] == "sampler" and r["kind"] in SKIP_KINDS:
            out |= _ids(r)
    return out


def negative_days(track_id: str, since_ms: int) -> int:
    """Distinct listening days (04:00→04:00 SGT) with negative evidence for a track:
    sampler skips that carry a score, and recorded plays under 30% no signal claimed.
    Fallback inferences never count."""
    rows = db.get_skip_signals_since(since_ms)
    claimed = _claimed(rows)
    days = {_listening_day(r["at_ms"]) for r in rows
            if track_id in _ids(r) and r["source"] == "sampler" and r["kind"] in SKIP_KINDS}
    for e in db.get_events_since(since_ms):
        if e["track_id"] != track_id or (e["track_id"], e["played_at"]) in claimed:
            continue
        dur, ms = e.get("duration_ms") or 0, e.get("ms_played")
        if ms is not None and dur and ms / dur < 0.30:
            days.add(_listening_day(e["played_at"]))
    return len(days)


def ban_evidence_ok(track_id: str, since_ms: int) -> bool:
    return negative_days(track_id, since_ms) >= int(load_config()["ban"]["min_negative_days"])


# ── digest + labels ──────────────────────────────────────────────────────────

def version_for_day(day: str) -> dict | None:
    """The version that saw most of an SGT day's listening — normally the one
    applied that day (the 03:00 refresh). A second refresh in the same day
    (manual run) does not take the day over: candidates are ranked by the
    listening observed inside each version's span of the day (sampler signals
    + recorded plays), then wall-clock coverage, then recency. Without any
    listening it stays the last version applied before the day ended."""
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=SGT).timestamp() * 1000)
    end = start + DAY_MS
    versions = [v for v in db.get_playlist_versions(start - 2 * DAY_MS) if v["applied_at"] < end]
    if not versions:
        return None

    def rank(v: dict) -> tuple[int, int, int]:
        lo = max(start, v["applied_at"])
        hi = min(end, _version_end(v) or end)
        listened = 0
        if hi > lo:
            listened = sum(1 for r in db.get_skip_signals_since(lo)
                           if r["at_ms"] < hi and r["kind"] != "coverage_miss")
            listened += sum(1 for e in db.get_events_since(lo) if e["played_at"] < hi)
        return listened, max(0, hi - lo), v["applied_at"]

    return max(versions, key=rank)


def _version_end(version: dict) -> int | None:
    """When the next version replaced this one (None = still live)."""
    later = [v["applied_at"] for v in db.get_playlist_versions(version["applied_at"])
             if v["applied_at"] > version["applied_at"]]
    return min(later) if later else None


def day_outcomes(version: dict, until_ms: int | None = None) -> dict:
    """Per-position outcomes during one version's lifetime (up to until_ms)."""
    end = _version_end(version)
    hi = until_ms or _now_ms()
    lo, hi = version["applied_at"], (hi if end is None else min(hi, end))
    rows = [r for r in db.get_skip_signals_since(lo) if r["at_ms"] < hi]
    events = [e for e in db.get_events_since(lo) if e["played_at"] < hi]
    names = {tid: (c.get("name") or tid) for tid, c in db.get_cached_tracks(version["tracks"]).items()}
    names.update({tid: n for tid, n in zip(version["tracks"], version.get("names") or []) if n})
    pos_of = {tid: i + 1 for i, tid in enumerate(version["tracks"])}
    seg_names = {}
    for s in db.get_segments_since(lo):
        seg_names.setdefault(s["track_id"], s.get("name"))
    per_pos = {p: {"track_id": tid, "name": names.get(tid) or seg_names.get(tid) or tid,
                   "sampler": [], "fallback": [], "recorded": [], "unknown": []}
               for p, tid in enumerate(version["tracks"], 1)}
    off = []
    judge = load_config()["judge"]
    for r in rows:
        p = next((pos_of[t] for t in _ids(r) if t in pos_of), None)
        if r["source"] == "fallback" and r["kind"] == "inferred_skip":
            if r.get("version_id") == version["version_id"] and r.get("position") in per_pos:
                per_pos[r["position"]]["fallback"].append(r)
        elif r["kind"] == "unknown_play":
            if p:
                per_pos[p]["unknown"].append(r)
        elif r["source"] == "sampler" and r["kind"] != "coverage_miss":
            if p:
                per_pos[p]["sampler"].append(r)
            elif r["kind"] in SKIP_KINDS:
                off.append(r)
    for e in events:
        p = pos_of.get(e["track_id"])
        if p:
            per_pos[p]["recorded"].append(e)
    for p, o in per_pos.items():
        o["predicted_sampler"] = any(_is_cooldown_evidence(r, judge) for r in o["sampler"])
        o["predicted_fallback"] = bool(o["fallback"])
        o["predicted"] = o["predicted_sampler"] or o["predicted_fallback"]
    rec_rows = [r for r in rows if r["source"] == "sampler" and r["kind"] != "coverage_miss"]
    matched = sum(1 for r in rec_rows if r.get("played_at") is not None)
    exit_err = sorted(abs(r["played_at"] - r["at_ms"]) for r in rec_rows
                      if r.get("played_at") is not None and r.get("tier") != "truncated")
    return {"positions": per_pos, "off_playlist": off, "recorded": len(events), "matched": matched,
            "segments": len(rec_rows), "exit_err_ms": exit_err}


def _fmt_s(ms) -> str:
    return "?" if ms is None else (f"{ms / 1000:.1f}s" if ms < 10000 else f"{round(ms / 1000)}s")


def _fmt_dur(ms) -> str:
    sec = round((ms or 0) / 1000)
    return f"{sec}s" if sec < 60 else f"{sec // 60}:{sec % 60:02d}"


def fill_version_meta(version: dict, token: str) -> int:
    """Fill a version's missing display names / artists from the live playlist
    (ONE GET; only tracks still in it are updated). Returns rows filled."""
    arts = version.get("artists") or [None] * len(version["tracks"])
    missing = {tid for tid, a in zip(version["tracks"], arts) if not a}
    if not missing:
        return 0
    import client
    r = client.get(f"playlists/{PLAYLIST_ID}/items?limit=50", token=token)
    meta = {}
    for it in (r.get("items") or []) if isinstance(r, dict) else []:
        t = it.get("item") or {}
        if t.get("id") in missing:
            names = [a.get("name") for a in (t.get("artists") or []) if a.get("name")]
            meta[t["id"]] = (t.get("name"), ", ".join(names[:2]) or None)
    return db.set_version_track_meta(version["version_id"], meta)


def _artists_by_track(version: dict, since_ms: int) -> dict[str, str]:
    """Display artists per track: the version row, else what the sampler saw,
    else artist_cache names via track_cache artist ids."""
    out = {tid: a for tid, a in zip(version["tracks"], version.get("artists") or []) if a}
    for s in db.get_segments_since(since_ms):
        if s.get("artists") and s["track_id"] not in out:
            out[s["track_id"]] = ", ".join([a for a in s["artists"] if a][:2])
    missing = [t for t in version["tracks"] if t not in out]
    cached = db.get_cached_tracks(missing)
    ids = sorted({aid for c in cached.values() for aid in (c.get("artist_ids") or [])})
    names = {aid: a.get("name") for aid, a in db.get_cached_artists(ids, include_stale=True).items() if a.get("name")}
    for tid, c in cached.items():
        found = [names[a] for a in (c.get("artist_ids") or []) if a in names]
        if found:
            out[tid] = ", ".join(found[:2])
    return out


def _outcome(o: dict) -> tuple[str, str]:
    """(section, short detail) for one position — see _SECTIONS for the wording."""
    s = o["sampler"]
    pick = lambda k: [r for r in s if r["kind"] == k]
    pct = lambda r: round(100 * (r["score_ms"] or 0) / max(1, r["duration_ms"] or 1))
    if pick("skip"):
        r = pick("skip")[0]
        at = round(100 * (r["exposure_hi_ms"] or 0) / max(1, r["duration_ms"] or 1))
        return "skip", f"after {_fmt_s(r['exposure_ms'])} ({at}%)"
    if pick("skim"):
        return "skim", f"after {_fmt_s(pick('skim')[0]['exposure_hi_ms'])}"
    if pick("early_exit"):
        r = pick("early_exit")[0]
        if o["predicted_sampler"]:
            return "leave", f"left at {pct(r)}%"
        return "done", ""                       # left at ≥ 90% — a listen
    if pick("pass"):
        return "other", f"passed in {_fmt_s(pick('pass')[0]['exposure_hi_ms'])}"
    if pick("stopped"):
        return "other", "playback stopped here"
    if pick("completed"):
        return "done", ""
    if s:
        return "other", _NEUTRAL_WORDS.get(s[0].get("end_reason"), s[0].get("end_reason") or "")
    if o["fallback"]:
        return "inferred", ""
    if o["recorded"]:
        unknown_at = {r["played_at"] for r in o["unknown"]}
        known = [e for e in o["recorded"] if e["played_at"] not in unknown_at]
        if not known:
            return "unknown", ""
        e = known[0]
        dur, ms = e.get("duration_ms") or 1, e.get("ms_played")
        if ms is not None and ms / dur >= 0.9:
            return "done", ""
        return "other", "played, length unknown" if ms is None else f"played about {round(100 * ms / dur)}%"
    return "none", ""


_NEUTRAL_WORDS = {"back": "you went back to an earlier track", "restart": "restarted",
                  "context_switch": "you switched to something else", "session_start": "first track of a session",
                  "private": "private session", "lost": "the Pi lost sight of it (phone offline?)"}

# Section headers double as the legend.
_SECTIONS = [
    ("skip", "**⏭ Skipped** (the Pi saw you leave before 90% of the song)"),
    ("skim", "**👀 Skimmed** (left after only 1.5–5s; also a skip)"),
    ("leave", "**⏩ Left early** (counts toward the cooldown only)"),
    ("inferred", "**⏭? Probably skipped** (the Pi didn't see it: missing between two recorded songs, "
                 "in playlist order)"),
    ("unknown", "**? Length unknown** (right after a pause or session start, so not scored)"),
    ("other", "**· No verdict**"),
]


def _inferred_notes(rows: list[dict]) -> dict[int, str]:
    """For the fallback: one explanatory note per gap (keyed by its last position)."""
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["at_ms"], []).append(r)
    notes = {}
    for g in groups.values():
        ps = sorted(r["position"] for r in g)
        per = g[0].get("exposure_hi_ms") or 0
        tier = (g[0].get("tier") or "")
        total = per * len(ps)
        if tier.startswith("T3"):
            note = f"right before autoplay took over (~{_fmt_dur(total)})"
        elif tier == "T2":
            note = f"{len(ps)} missing over {_fmt_dur(total)}; could also be a pause"
        elif len(ps) > 1:
            note = f"{len(ps)} passed in a quick run ({_fmt_dur(total)})"
        else:
            note = f"~{_fmt_dur(total)} unaccounted for"
        notes[ps[-1]] = note
    return notes


def _ranges(ps: list[int]) -> str:
    out, start, prev = [], None, None
    for p in sorted(ps):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}" if start == prev else f"{start}–{prev}")
            start = prev = p
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(out)


def _discord_len(text: str) -> int:
    """Discord counts UTF-16 code units (emoji / CJK outside the BMP count 2)."""
    return len(text.encode("utf-16-le")) // 2


def _sampler_health(now: int) -> str | None:
    try:
        hb = db.get_sampler_state("heartbeat")
    except Exception:
        hb = None
    if not hb:
        return "⚠️ sampler: no heartbeat recorded"
    age_min = (now - (hb.get("t") or 0)) / 60000
    if age_min > 10:
        return f"⚠️ sampler: last heartbeat {age_min:.0f} min ago — is spotify-sampler.service running?"
    return None


def _song(o: dict, artists: dict[str, str]) -> str:
    a = artists.get(o["track_id"])
    name = (o["name"] or o["track_id"])[:30]
    return f"{name} — {a[:24]}" if a else name


def build_digest(day: str | None = None, now_ms: int | None = None, limit: int = 1900) -> str:
    """The nightly shadow-mode digest ('' when there was no listening)."""
    now = now_ms or _now_ms()
    day = day or _day(now)
    version = version_for_day(day)
    if version is None:
        return ""
    d = day_outcomes(version, now)
    if not d["recorded"] and not d["segments"]:
        return ""
    artists = _artists_by_track(version, version["applied_at"])
    mode = "live" if enabled() else "shadow mode"
    when = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
    head = [f"## 🎧 DJ Hermes skip check — {when} ({mode})",
            f"Pi watched {d['matched']}/{d['recorded']} recorded plays · `#` = playlist position"]
    if _day(version["applied_at"]) != day:
        head.append(f"(playlist not refreshed today — positions are from {_day(version['applied_at'])})")
    v_end = _version_end(version)
    if v_end is not None and _day(v_end) == day:
        head.append(f"(playlist refreshed again at "
                    f"{datetime.fromtimestamp(v_end / 1000, SGT).strftime('%H:%M')} — "
                    f"this check covers up to then)")
    health = _sampler_health(now)
    if health:
        head.append(health)

    by_section: dict[str, list[tuple[int, dict, str]]] = {}
    done, none = [], []
    for p, o in sorted(d["positions"].items()):
        sec, detail = _outcome(o)
        if sec == "done":
            done.append(p)
        elif sec == "none":
            none.append(p)
        else:
            by_section.setdefault(sec, []).append((p, o, detail))
    notes = _inferred_notes([r for o in d["positions"].values() for r in o["fallback"]])

    body = []
    for key, header in _SECTIONS:
        items = by_section.get(key)
        if not items:
            continue
        body.append(header)
        for p, o, detail in items:
            body.append(f"`{p:>2}` {_song(o, artists)}" + (f" · {detail}" if detail else ""))
            if key == "inferred" and p in notes:
                body.append(f"   ↳ {notes[p]}")
    if d["off_playlist"]:
        seg = {s["track_id"]: s for s in db.get_segments_since(version["applied_at"])}
        body.append("**⏭ Skipped outside the playlist** (autoplay, radio, albums…)")
        for r in sorted(d["off_playlist"], key=lambda x: x["at_ms"]):
            sg = seg.get(r["track_id"]) or {}
            who = ", ".join([a for a in (sg.get("artists") or []) if a][:2])
            name = (sg.get("name") or r["track_id"])[:30]
            body.append(f"`  ` {name}" + (f" — {who[:24]}" if who else "") + f" · after {_fmt_s(r['exposure_hi_ms'])}")
    tail = []
    if done:
        tail.append(f"✅ **Played through:** {_ranges(done)}")
    if none:
        tail.append(f"**Not reached:** {_ranges(none)}")
    acc = accuracy()
    if acc["days"]:
        n = acc.get("corrections", 0)
        since = f", last {acc['last_correction']}" if acc.get("last_correction") else ""
        tail.append(f"Accuracy ({acc['days']} days · no reply = correct · {n} correction{'' if n == 1 else 's'} "
                    f"from you{since}): precision {acc['precision']} · recall {acc['recall']}")
    tail.append(f"Reply only if something is wrong, by `#` — e.g. `17 not, 30 skip` (no reply = all correct).")
    # Fit one Discord message: header + reply line always survive; body lines are
    # dropped from the end (with a count) if needed.
    kept = []
    for i, line in enumerate(body):
        more = len(body) - i - 1
        trial = "\n".join(head + kept + [line] + ([f"… +{more} more lines"] if more else []) + tail)
        if _discord_len(trial) > limit:
            kept.append(f"… +{len(body) - i} more lines")
            break
        kept.append(line)
    out = "\n".join(head + kept + tail)
    return out if _discord_len(out) <= limit else "\n".join(head + tail)


def _parse_labels(text: str) -> tuple[list[tuple[int, str]], bool]:
    """Free-text reply → [(position, label)], ok_flag. A label word applies to
    every position number since the previous label word: '17, 30 not',
    '25–28 skip', '10 15 16 skip', '#17: no', '17 not, 30 skip'."""
    import re
    toks = re.findall(r"\d+\s*[-–]\s*\d+|\b\d+\b|\b(?:skipped|skip|not|no|maybe|ok)\b|n['’]t\b",
                      text.lower())
    norm = {"skipped": "skip", "no": "not"}
    out, pending, ok, neg = [], [], False, False
    for t in toks:
        if t in ("n't", "n’t"):
            neg = True                      # "isn't a skip", "wasn't skipped"
            continue
        if t in ("not", "no") and not pending:
            neg = True                      # "not 17"-style
            continue
        if t[0].isdigit():
            m = re.match(r"(\d+)\s*[-–]\s*(\d+)", t)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                pending += list(range(min(a, b), max(a, b) + 1))
            else:
                pending.append(int(t))
        elif t == "ok":
            ok = True
        else:
            lab = norm.get(t, t)
            if neg and lab == "skip":
                lab = "not"
            out += [(p, lab) for p in pending]
            pending, neg = [], False
    if pending:
        raise SystemExit(f"no label after position(s) {pending} in {text!r} — say skip / not / maybe")
    return out, ok


def label(day: str, text: str, now_ms: int | None = None) -> str:
    """Record the user's reply to a digest. Stores the full truth set for the
    version: the user's corrections, and every other position as the digest
    called it (a reply = the rest was right)."""
    version = version_for_day(day)
    if version is None:
        raise SystemExit(f"no playlist version for {day}")
    pairs, ok = _parse_labels(text)
    if not pairs and not ok:
        raise SystemExit(f"could not read {text!r} — use e.g. '17 not, 30 skip' or 'ok'")
    now = now_ms or _now_ms()
    d = day_outcomes(version, now)
    explicit = {p: lab for p, lab in pairs if 1 <= p <= len(version["tracks"])}
    rows = [(0, "ok", None, "explicit")]
    for p, o in d["positions"].items():
        if p in explicit:
            rows.append((p, explicit[p], o["track_id"], "explicit"))
        else:
            rows.append((p, "skip" if o["predicted"] else "not", o["track_id"], "accepted"))
    db.set_labels(version["version_id"], rows, labeled_at=now)
    return f"labelled version {version['version_id']} ({day}): {len(explicit)} correction(s)"


SILENT_GRACE_H = 20      # a version's digest went out the evening before it ended; a day to reply


def auto_accept(now_ms: int | None = None, grace_h: float = SILENT_GRACE_H) -> list[int]:
    """User rule (2026-10-08): no reply = the skips were right. Every playlist
    version that ended ≥ grace_h ago, had listening, and has no labels yet is
    stored as reviewed with the digest's own calls (origin 'silent'), judged up
    to its end. A later `label` reply replaces it. Returns the version ids."""
    now = now_ms or _now_ms()
    labelled = set(db.get_labels())
    versions = db.get_playlist_versions(0)
    done = []
    for i, v in enumerate(versions):
        end = versions[i + 1]["applied_at"] if i + 1 < len(versions) else None
        if v["version_id"] in labelled or end is None or now - end < grace_h * 3600 * 1000:
            continue
        d = day_outcomes(v, end)
        if not d["recorded"] and not d["segments"]:
            continue                                  # nothing was played — nothing to accept
        rows = [(0, "ok", None, "silent")]
        rows += [(p, "skip" if o["predicted"] else "not", o["track_id"], "silent")
                 for p, o in d["positions"].items()]
        db.set_labels(v["version_id"], rows, labeled_at=end)
        done.append(v["version_id"])
    return done


def accuracy() -> dict:
    """
    Precision / recall of the CURRENT logic against the stored truth sets, each
    judged only over what the user reviewed (version start → label time).
    Overall, per predicting source, and per day; plus sampler exit-time error.
    Truth sets include days accepted by silence (auto_accept: no reply = correct),
    so only the user's corrections can lower it — `corrections` / `last_correction`
    say how many there are and when the last one was.
    """
    labs = db.get_labels()
    if not labs:
        return {"days": 0}
    versions = {v["version_id"]: v for v in db.get_playlist_versions(0)}
    tp = fp = fn = 0
    src = {"sampler": [0, 0, 0], "fallback": [0, 0, 0]}   # tp, fp, fn per source
    per_day, errs = {}, []
    corrections, silent_days, last_corr = 0, 0, None
    for vid, lab in labs.items():
        v = versions.get(vid)
        if v is None:
            continue
        corrections += len(lab.get("explicit") or ())
        silent_days += bool(lab.get("silent"))
        if lab.get("explicit"):
            last_corr = max(last_corr or 0, lab["labeled_at"] or 0)
        d = day_outcomes(v, lab["labeled_at"])     # only what the user had reviewed
        errs += d["exit_err_ms"]
        dt = df = dn = 0
        for p, o in d["positions"].items():
            truth_label = lab["labels"].get(p)
            if truth_label in (None, "maybe"):
                continue
            truth = truth_label == "skip"
            if o["predicted"] and truth:
                dt += 1
            elif o["predicted"]:
                df += 1
            elif truth:
                dn += 1
            for key, pred in (("sampler", o["predicted_sampler"]), ("fallback", o["predicted_fallback"])):
                if pred and truth:
                    src[key][0] += 1
                elif pred:
                    src[key][1] += 1
            if truth and not o["predicted"]:
                # a missed skip is charged to the sampler if it saw that track, else the fallback
                src["sampler" if o["sampler"] else "fallback"][2] += 1
        tp, fp, fn = tp + dt, fp + df, fn + dn
        per_day[_day(v["applied_at"])] = f"P {dt}/{dt + df} R {dt}/{dt + dn}"
    pct = lambda a, b: "n/a" if a + b == 0 else f"{100 * a / (a + b):.0f}%"
    q = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))] if xs else None
    return {"days": len(per_day), "tp": tp, "fp": fp, "fn": fn,
            "corrections": corrections, "silent_days": silent_days,
            "last_correction": _day(last_corr) if last_corr else None,
            "precision": pct(tp, fp), "recall": pct(tp, fn),
            "sampler": {"precision": pct(src["sampler"][0], src["sampler"][1]),
                        "recall": pct(src["sampler"][0], src["sampler"][2])},
            "fallback": {"precision": pct(src["fallback"][0], src["fallback"][1]),
                         "recall": pct(src["fallback"][0], src["fallback"][2])},
            "per_day": per_day,
            "exit_err_ms": {"median": q(errs, 0.5), "p95": q(errs, 0.95), "n": len(errs)}}


if __name__ == "__main__":
    db.init_db()
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "digest"), sys.argv[2:]
    if cmd == "rebuild":
        print(rebuild_signals())
    elif cmd == "digest":
        rebuild_signals()
        print(build_digest(args[0] if args else None) or "(no listening)")
    elif cmd == "label":
        if len(args) < 2:
            raise SystemExit('usage: skip_signals.py label YYYY-MM-DD ok | "17 not, 30 skip"')
        print(label(args[0], " ".join(args[1:])))
    elif cmd == "auto-accept":
        db.init_db()
        print(f"accepted by silence: versions {auto_accept()}")
    elif cmd == "accuracy":
        import json
        print(json.dumps(accuracy(), indent=1))
    else:
        raise SystemExit(__doc__)
