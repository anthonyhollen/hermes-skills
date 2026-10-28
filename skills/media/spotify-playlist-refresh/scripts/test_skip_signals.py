#!/usr/bin/env python3
"""
Skip-detection regression suite (v2.18) — run BEFORE trusting any change to
spotify-refresh/src/{skipsig,skip_signals,sampler}.py or the skip_signals
config block.

  1. Sampler scenarios — a ground-truth playback simulator produces the
     GET /me/player responses the sampler would see (adaptive cadence, jitter,
     round-trip time) and asserts what skipsig.Segmenter + classify_segment make
     of each track: bursts, pass-throughs, pause-then-skip, session-start
     "start fresh" taps, taps, back/restart, repeat-one, crossfade, device
     switch, offline (204) windows, 429 gaps, restarts, context switches, ads…
  2. Fallback replay — the real 2026-09-30 data (scripts/fixtures/
     skip_2026_09_30.json: 50 recorded plays with context, the 40-track order
     written at 03:00, the user's labels) through skipsig.infer_uncovered;
     asserts the per-position tiers and precision / recall against the labels.
  3. Policy integration — a throwaway SQLite DB: rebuild_signals, cooldown /
     demotion / heard / scoring inputs / ban gate, flag off = no effect.

Usage:  python3 scripts/test_skip_signals.py        Exit 0 on success, 1 on failure.
Needs only the stdlib + PyYAML (the skill's own deps); no network, no live DB.
"""
import json, os, random, sys, tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SRC = SKILL / "spotify-refresh" / "src"
sys.path.insert(0, str(SRC))
import skipsig  # noqa: E402

FIXTURE = SKILL / "scripts" / "fixtures" / "skip_2026_09_30.json"
PL = "spotify:playlist:4y4Fd7Pg52Ubi0zqJBNJnm"
OTHER = "spotify:album:someAlbum"
S = 1000

_failures: list[str] = []
_passes = 0


def check(cond, msg):
    global _passes
    if cond:
        _passes += 1
    else:
        _failures.append(msg)
        print(f"  FAIL: {msg}")


# ── 1. playback simulator ────────────────────────────────────────────────────

class Timeline:
    """Ground-truth player state as intervals [t0, t1) of
    (track, duration, progress_at_t0, playing, context, device_type, kind)."""

    def __init__(self, t0=1_790_724_600_000):      # 2026-09-30 07:30 SGT (daytime cadence)
        self.t = t0
        self.iv = []

    def play(self, track, dur_s, secs, start_s=0.0, ctx=PL, device="Smartphone"):
        self.iv.append((self.t, self.t + int(secs * S), track, int(dur_s * S), int(start_s * S), True, ctx, device, "track"))
        self.t += int(secs * S)
        return self

    def pause(self, track, dur_s, at_s, secs, ctx=PL, as_204=False):
        kind = "idle" if as_204 else "track"
        self.iv.append((self.t, self.t + int(secs * S), track, int(dur_s * S), int(at_s * S), False, ctx, "Smartphone", kind))
        self.t += int(secs * S)
        return self

    def idle(self, secs):
        self.iv.append((self.t, self.t + int(secs * S), None, 0, 0, False, None, None, "idle"))
        self.t += int(secs * S)
        return self

    def ad(self, secs):
        self.iv.append((self.t, self.t + int(secs * S), None, 0, 0, True, None, "Smartphone", "ad"))
        self.t += int(secs * S)
        return self

    def outage(self, secs):
        self.iv.append((self.t, self.t + int(secs * S), None, 0, 0, False, None, None, "outage"))
        self.t += int(secs * S)
        return self

    def private(self, track, dur_s, secs):
        self.iv.append((self.t, self.t + int(secs * S), track, int(dur_s * S), 0, True, PL, "Smartphone", "private"))
        self.t += int(secs * S)
        return self

    def response(self, t):
        """(resp, http) of GET /me/player at time t."""
        for (a, b, track, dur, p0, playing, ctx, device, kind) in self.iv:
            if a <= t < b:
                break
        else:
            return {"error": {"status": 204, "message": "parse_error: "}}, 204
        if kind == "outage":
            return {"error": {"status": 429, "message": "rate limited"}}, 429
        if kind == "idle":
            return {"error": {"status": 204, "message": "parse_error: "}}, 204
        base = {"shuffle_state": False, "smart_shuffle": False, "repeat_state": "off", "timestamp": a,
                "device": {"id": "d1", "type": device, "is_private_session": kind == "private"}}
        if kind == "ad":
            return {**base, "is_playing": True, "item": None, "currently_playing_type": "ad", "context": None}, 200
        prog = p0 + (t - a if playing else 0)
        return {**base, "is_playing": playing, "progress_ms": min(prog, dur), "currently_playing_type": "track",
                "context": {"uri": ctx},
                "item": {"id": track, "type": "track", "duration_ms": dur, "name": track,
                         "artists": [{"id": f"art_{track}", "name": "Artist"}]}}, 200


def sample_run(tl: Timeline, cfg=None, start=None, end=None, seg=None, seed=7, state_split_at=None):
    """Drive the Segmenter exactly like sampler.Sampler would (adaptive cadence,
    ±200ms jitter, 60–250ms RTT). Returns (closed segments, segmenter)."""
    import sampler as smp
    rnd = random.Random(seed)
    c = skipsig.merge_config(cfg)
    seg = seg or skipsig.Segmenter(c)
    policy = smp.IntervalPolicy(c)
    t = start if start is not None else tl.iv[0][0]
    end = end if end is not None else tl.t + 180 * S
    closed, last_status = [], None
    split_done = False
    while t < end:
        if state_split_at is not None and not split_done and t >= state_split_at:
            seg = skipsig.Segmenter(c, json.loads(json.dumps(seg.state())))   # restart: persisted state
            split_done = True
        rtt = rnd.randint(60, 250)
        resp, http = tl.response(t + rtt // 2)
        smp_ = skipsig.parse_player(resp, http, t + rtt // 2, rtt)
        if smp_["status"] != "error":
            closed += seg.feed(smp_)
            last_status = smp_["status"] if smp_["status"] in ("playing", "paused") else "idle"
        step = policy.next_s(seg, last_status, t) if http != 429 else 60
        t += int(step * S) + rtt + rnd.randint(-200, 200)
    return closed, seg


def kinds(closed, versions=None, cfg=None):
    out = []
    for s in closed:
        c = skipsig.classify_segment(s, cfg, versions)
        out.append((c["track_id"], c["kind"], c.get("end_reason"), c))
    return out


def kind_of(ks, track):
    return [k for t, k, _, _ in ks if t == track]


def test_scenarios():
    print("1. sampler scenarios")
    # A. sequential burst: #15 4s, #16 3.5s, #17 0.6s (pass-through), #18 4s, lone skip at 12s
    tl = (Timeline().play("t14", 180, 180).play("t15", 200, 4).play("t16", 190, 3.5)
          .play("t17", 210, 0.6).play("t18", 200, 4).play("t19", 170, 170)
          .play("t20", 200, 12).play("t21", 180, 180))
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "t14") == ["completed"], f"burst: t14 completed, got {kind_of(ks, 't14')}")
    for t in ("t15", "t16", "t18"):
        check(kind_of(ks, t) == ["skim"], f"burst: {t} (3.5–4s) = skim (cooldown only), got {kind_of(ks, t)}")
    check(kind_of(ks, "t17") in ([], ["pass"]), f"burst: t17 (0.6s) pass or unobserved, got {kind_of(ks, 't17')}")
    check(kind_of(ks, "t19") == ["completed"], f"burst: t19 completed, got {kind_of(ks, 't19')}")
    check(kind_of(ks, "t20") == ["skip"], f"lone 12s exit = skip, got {kind_of(ks, 't20')}")
    t20 = [c for t, _, _, c in ks if t == "t20"][0]
    check(10 * S <= t20["listened_lo"] <= 12.5 * S and t20["listened_hi"] <= 13.5 * S,
          f"lone skip exposure bounds ≈12s, got {t20['listened_lo']}–{t20['listened_hi']}")

    # B. pause-then-skip (#37): 20s, paused 34 min, resume 5s, next → skip ~25s of 156s
    for as_204 in (False, True):
        tl = (Timeline().play("a", 180, 180).play("izna", 156, 20)
              .pause("izna", 156, 20, 34 * 60, as_204=as_204).play("izna", 156, 5, start_s=20).play("b", 180, 180))
        ks = kinds(sample_run(tl)[0])
        izna = [c for t, _, _, c in ks if t == "izna"]
        check([c["kind"] for c in izna] == ["skip"],
              f"pause-then-skip ({'204' if as_204 else 'paused'} window) = one skip, got {[c['kind'] for c in izna]}")
        if izna:
            check(19 * S <= izna[0]["listened_lo"] <= 26 * S and izna[0]["listened_hi"] <= 32 * S,
                  f"pause-then-skip listened ≈25s (pause excluded), got {izna[0]['listened_lo']}–{izna[0]['listened_hi']}")

    # C. MOON: session start resumes yesterday's track mid-way, user taps #1 after 18s
    #    (a first sighting up to idle_s late must never turn this into a skip)
    tl = Timeline(1_790_724_600_000 - 12 * 3600 * S).idle(12 * 3600).play("moon", 166, 18, start_s=90) \
        .play("t1", 165, 165).play("t2", 170, 170)
    ks = kinds(sample_run(tl, start=tl.iv[0][0] + 12 * 3600 * S - 40 * S)[0])
    check(kind_of(ks, "moon") in (["neutral"], []), f"session-start resume + tap = neutral/unseen, got {kind_of(ks, 'moon')}")
    moon = [c for t, _, _, c in ks if t == "moon"]
    check(not moon or moon[0]["end_reason"] == "session_start", f"moon end_reason session_start, got {moon and moon[0]['end_reason']}")
    #    a fresh first track left within the grace ("start fresh" by picking another) is neutral too
    tl = Timeline(1_790_724_600_000 - 12 * 3600 * S).idle(12 * 3600).play("f1", 170, 12).play("f2", 170, 170).play("f3", 170, 170)
    ks = kinds(sample_run(tl, start=tl.iv[0][0] + 12 * 3600 * S - 40 * S)[0])
    check(kind_of(ks, "f1") in (["neutral"], []), f"first track left within grace = neutral, got {kind_of(ks, 'f1')}")
    #    …but a skip later in the same session still counts
    tl = Timeline(1_790_724_600_000 - 12 * 3600 * S).idle(12 * 3600).play("s1", 170, 170).play("s2", 180, 9).play("s3", 170, 170)
    ks = kinds(sample_run(tl, start=tl.iv[0][0] + 12 * 3600 * S - 40 * S)[0])
    check(kind_of(ks, "s2") == ["skip"], f"skip after a completed first track = skip, got {kind_of(ks, 's2')}")

    # D. versions for next / jump / back
    ver = skipsig.VersionIndex([{"version_id": 1, "applied_at": 0, "tracks": [f"p{i}" for i in range(1, 41)]}])
    tl = Timeline().play("p5", 180, 60).play("p12", 180, 180)       # tap forward at 33%
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p5") == ["skip"], f"tap away at 33% = skip (user rule: < 90%), got {kind_of(ks, 'p5')}")
    tl = Timeline().play("p5", 180, 126).play("p6", 180, 180)       # next at 70%
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p5") == ["skip"], f"next at 70% = skip, got {kind_of(ks, 'p5')}")
    tl = Timeline().play("p5", 200, 186).play("p6", 180, 180)       # next at 93% (14s before the end)
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p5")[0] in ("completed", "early_exit"), f"next at 93% = a listen, got {kind_of(ks, 'p5')}")
    tl = Timeline().play("p4", 180, 180).play("p5", 180, 10).play("p12", 180, 180)   # tap forward at 10s
    ks = kinds(sample_run(tl)[0], ver)
    p5 = [c for t, _, _, c in ks if t == "p5"]
    check(bool(p5) and p5[0]["kind"] == "skip" and p5[0]["end_reason"] == "jump",
          f"tap away at 10s = skip via jump, got {p5 and (p5[0]['kind'], p5[0]['end_reason'])}")
    tl = Timeline().play("p7", 180, 180).play("p8", 180, 10).play("p7", 180, 180)    # previous → back
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p8") == ["neutral"], f"'previous' back to p7 = neutral, got {kind_of(ks, 'p8')}")
    tl = Timeline().play("p7", 180, 180).play("p8", 180, 20).play("p8", 180, 180).play("p9", 180, 180)  # restart
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p8") == ["neutral", "completed"], f"restart = neutral then completed, got {kind_of(ks, 'p8')}")

    # E. repeat-one, crossfade, device switch, offline window, ad, private, context switch, stopped
    tl = Timeline().play("r", 150, 150).play("r", 150, 150).play("z", 180, 180)
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "r") == ["completed", "completed"], f"repeat-one = 2 completions, got {kind_of(ks, 'r')}")
    tl = Timeline().play("x", 200, 192).play("y", 180, 180)          # 8s crossfade
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "x") == ["completed"], f"crossfade exit 8s early = completed, got {kind_of(ks, 'x')}")
    tl = (Timeline().play("d", 200, 60).play("d", 200, 140, start_s=60, device="Automobile")
          .play("e", 180, 180))
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "d") == ["completed"], f"device switch mid-track = one completed segment, got {kind_of(ks, 'd')}")
    tl = Timeline().play("o", 200, 50).pause("o", 200, 50, 120, as_204=True).play("o", 200, 30, start_s=170).play("f", 180, 180)
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "o") == ["completed"], f"204 window then resume near end = completed, got {kind_of(ks, 'o')}")
    tl = Timeline().play("g", 180, 180).ad(30).play("h", 180, 180).play("h2", 180, 180)
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "g") == ["completed"] and kind_of(ks, "h") == ["completed"],
          f"ad between tracks, got g={kind_of(ks, 'g')} h={kind_of(ks, 'h')}")
    tl = Timeline().play("i", 180, 180).private("j", 180, 10).play("k", 180, 180)
    ks = kinds(sample_run(tl)[0])
    check("skip" not in kind_of(ks, "j") and "skim" not in kind_of(ks, "j"), f"private session never a skip, got {kind_of(ks, 'j')}")
    tl = Timeline().play("m", 180, 180).play("n", 200, 30).play("q", 180, 180, ctx=OTHER)
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "n") == ["neutral"], f"switch to another context = neutral, got {kind_of(ks, 'n')}")
    tl = Timeline().play("bans", 215, 189).pause("bans", 215, 189, 60).idle(3 * 3600)
    closed, seg = sample_run(tl, end=tl.t)
    ks = kinds(closed)
    check(kind_of(ks, "bans") == ["stopped"], f"session ends paused at 88% = stopped, got {kind_of(ks, 'bans')}")

    # F. 429 / outage covering an exit: the exit is unobserved → neutral 'lost'
    tl2 = Timeline().play("u", 200, 30)
    t_out = tl2.t
    tl2.play("v", 180, 25).play("w", 180, 180)
    closed = []
    seg = skipsig.Segmenter()
    for t in range(tl2.iv[0][0], tl2.t + 60 * S, 3 * S):
        if t_out - 2 * S < t < t_out + 40 * S:
            continue                                   # 42s with no samples
        resp, http = tl2.response(t)
        closed += seg.feed(skipsig.parse_player(resp, http, t))
    ks = kinds(closed)
    check(kind_of(ks, "u") == ["neutral"], f"exit hidden by a 42s gap = neutral (lost), got {kind_of(ks, 'u')}")

    # G. restart mid-segment: persisted state gives the same result
    tl = Timeline().play("a", 180, 180).play("sk", 200, 12).play("c", 180, 180)
    base = kinds(sample_run(tl)[0])
    split = kinds(sample_run(tl, state_split_at=tl.iv[1][0] + 5 * S)[0])
    check([k[:2] for k in base] == [k[:2] for k in split], f"restart via persisted state: {base} vs {split}")


# ── 2. fallback replay on real data ──────────────────────────────────────────

def test_fixture():
    print("2. fallback replay (2026-09-30 real data)")
    fx = json.loads(FIXTURE.read_text())
    ver = skipsig.VersionIndex([fx["version"]])
    events = [{k: e[k] for k in ("track_id", "played_at", "duration_ms", "context_uri")} for e in fx["events"]]
    sig, unknown = skipsig.infer_uncovered(events, ver, [], [], None, fx["playlist_uri"])
    by_pos = {s["position"]: s["tier"] for s in sig}
    expect = {10: "T1", 15: "T1-dense", 16: "T1-dense", 17: "T1-dense", 18: "T1-dense",
              25: "T2", 26: "T2", 27: "T2", 28: "T2", 30: "T1",
              32: "T1-dense", 33: "T1-dense", 34: "T1-dense", 35: "T1-dense", 38: "T1", 40: "T3"}
    check(by_pos == expect, f"fallback tiers per position\n    got      {sorted(by_pos.items())}\n    expected {sorted(expect.items())}")
    lab = fx["labels"]
    pred = set(by_pos)
    truth = set(lab["skip"])
    scored = set(range(1, 41)) - set(lab["maybe"])
    tp, fp, fn = len(pred & truth & scored), len((pred - truth) & scored), len((truth - pred) & scored)
    print(f"   fallback vs labels: TP={tp} FP={fp} FN={fn}  precision {tp}/{tp + fp}  recall {tp}/{tp + fn}")
    check((tp, fp, fn) == (14, 1, 1), f"fallback precision/recall 14/15 each (FP #17, FN #37), got TP={tp} FP={fp} FN={fn}")
    names = {tid: n for tid, n in zip(fx["version"]["tracks"], fx["version"]["names"])}
    names.update({e["track_id"]: e["name"] for e in fx["events"]})
    today = {u["track_id"] for u in unknown if u["played_at"] >= fx["day_start_ms"]}
    today_names = sorted(names.get(t, t) for t in today)
    print(f"   unknown-listen plays today ({len(today)}): {', '.join(today_names)}")
    izna = fx["version"]["tracks"][36]
    check(izna in today, "#37 IZNA (after a 34-min pause) is unknown, no longer a +1.0 full play")
    for pos in (11, 19, 36, 39):     # full listens right after a skip burst stay scored
        check(fx["version"]["tracks"][pos - 1] not in today, f"#{pos} after inferred pass-overs keeps its score")
    # coverage suppresses the fallback entirely
    sig2, unk2 = skipsig.infer_uncovered(events, ver, [(events[0]["played_at"] - 1, events[-1]["played_at"] + 1)],
                                         [], None, fx["playlist_uri"])
    check(sig2 == [] and unk2 == [], "fallback emits nothing for sampler-covered time")


# ── 3. policy integration on a throwaway DB ──────────────────────────────────

def test_policy():
    print("3. policy integration (temp DB)")
    import db
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "t.db")
    db.init_db()
    db.init_db()                                   # idempotent (migration race-safe)
    import skip_signals, scorer, refresher
    check(skip_signals.PLAYLIST_ID == refresher.PLAYLIST_ID, "skip_signals.PLAYLIST_ID matches refresher")
    live_cfg = skip_signals.load_config
    now = 1_790_800_000_000
    DAY = 86400 * S
    tracks = [f"p{i}" for i in range(1, 11)]
    db.upsert_track_cache([{"track_id": t, "name": f"Song {t}", "artist_ids": [f"a{t}"], "duration_ms": 200 * S}
                           for t in tracks])
    vid = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - 2 * DAY)
    # recorded plays: p1 full ×3 on day −1 (liked), p2 full ×2
    evs = [{"track_id": "p1", "played_at": now - DAY + i * 300 * S, "ms_played": 200 * S, "duration_ms": 200 * S,
            "context_uri": PL} for i in range(3)]
    evs += [{"track_id": "p2", "played_at": now - DAY + (10 + i) * 300 * S, "ms_played": 200 * S,
             "duration_ms": 200 * S, "context_uri": PL} for i in range(2)]
    db.upsert_play_events(evs)
    # sampler: p2 skipped (8s) twice on two days, p3 skim 3s, p4 completed, p5 pass 0.7s
    def seg(tid, first, listened, reason="early", status="playing", prog=None):
        prog = listened if prog is None else prog
        return {"track_id": tid, "linked_from": None, "name": f"Song {tid}", "duration_ms": 200 * S,
                "context_uri": PL, "first_seen": first, "last_seen": first + listened, "exit_lo": first + listened,
                "exit_hi": first + listened + 300, "listened": listened, "listened_lo": listened,
                "listened_hi": listened + 300, "end_progress_hi": prog + 300, "last_progress": prog,
                "remaining_ms": 200 * S - prog, "last_status": status, "end_reason": reason,
                "session_start": False, "resumed": False, "shuffle": False, "n_samples": 5}
    db.insert_segments([seg("p2", now - 2 * DAY + 3600 * S, 8 * S), seg("p2", now - 3600 * S, 8 * S),
                        seg("p3", now - 1800 * S, 3 * S), seg("p4", now - 1700 * S, 200 * S, "completed"),
                        seg("p5", now - 1600 * S, 700)])
    db.extend_coverage(now - 3 * DAY, now)
    info = skip_signals.rebuild_signals(now)
    check(info["segments"] == 5, f"rebuild saw 5 segments, got {info}")
    rows = db.get_skip_signals_since(0)
    got = sorted((r["track_id"], r["kind"]) for r in rows if r["source"] == "sampler" and r["kind"] != "coverage_miss")
    check(got == [("p2", "skip"), ("p2", "skip"), ("p3", "skim"), ("p4", "completed"), ("p5", "pass")],
          f"sampler signal kinds, got {got}")
    cool = skip_signals.cooldown_state(now)
    check(set(cool["cooldown"]) == {"p2", "p3"}, f"cooldown = skip + skim tracks, got {sorted(cool['cooldown'])}")
    check(set(cool["demoted"]) == {"p2"}, f"demoted = 2 skips within 14 days, got {sorted(cool['demoted'])}")
    check({"p2", "p3", "p4", "p5"} <= skip_signals.heard_ids(now), "every observed exposure counts as heard")
    check(skip_signals.skipped_ids(now) == {"p2", "p3"}, "skipped ids = skip + skim")
    ov, derived = skip_signals.scoring_inputs(now - 7 * DAY)
    check(sorted((d["track_id"], d["ms_played"]) for d in derived)
          == [("p2", 8300), ("p2", 8300), ("p3", 3300), ("p4", 200 * S)],
          f"derived scoring events (unrecorded skips + completion), got {sorted((d['track_id'], d['ms_played']) for d in derived)}")
    # a completed play after the demotion trigger clears it
    db.insert_segments([seg("p2", now - 600 * S, 200 * S, "completed")])
    skip_signals.rebuild_signals(now)
    check("p2" not in skip_signals.cooldown_state(now)["demoted"], "a later completed play lifts the demotion")

    # flag off: scorer identical with and without signals present (independent of the live config)
    off, _ = scorer.compute_affinity(now_ms=now, use_signals=False)
    _live = skip_signals.load_config
    try:
        skip_signals.load_config = lambda: {**_live(), "enabled": False}
        dflt, _ = scorer.compute_affinity(now_ms=now)
    finally:
        skip_signals.load_config = _live
    check(off == dflt, "flag off = pre-v2.18 scoring")
    on, _ = scorer.compute_affinity(now_ms=now, use_signals=True)
    check(on.get("p2", 0) < off.get("p2", 0), f"flag on: sampler skips lower p2 ({off.get('p2')} → {on.get('p2')})")

    # ban gate: a sampler-only skip on ONE day never bans; on two days it may.
    # update_blocklist() windows on the real wall clock; pin it to the test's
    # `now` or these checks rot once the fixed dates slide out of 7 days.
    db.insert_segments([seg("p6", now - 3000 * S, 6 * S)])
    skip_signals.rebuild_signals(now)
    scores = {"p6": -1.5}
    real_dt = scorer.datetime

    class _FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt.fromtimestamp(now / 1000, tz)

    try:
        skip_signals.load_config = lambda: {**live_cfg(), "enabled": True}
        scorer.datetime = _FixedDT
        check(scorer.update_blocklist(scores, use_signals=True) == [], "one-day sampler skip does not ban")
        db.insert_segments([seg("p6", now - 2 * DAY, 6 * S)])
        skip_signals.rebuild_signals(now)
        check(scorer.update_blocklist(scores, use_signals=True) == ["p6"], "sampler skips on 2 days may ban")
    finally:
        scorer.datetime = real_dt
        skip_signals.load_config = live_cfg

    # labels + digest render without error
    day = skip_signals._day(now - 2 * DAY + 3600 * S)
    msg = skip_signals.label(day, "3 not, 2 skip")
    check("2 correction" in msg, f"label parses free text, got {msg!r}")
    acc = skip_signals.accuracy()
    check(acc["days"] == 1, f"accuracy counts the reviewed version, got {acc}")
    check(len(skip_signals.build_digest(day, now)) < 1900, "digest fits one Discord message")


def test_review_regressions():
    """One check per finding of the 2026-09-30 independent reviews."""
    print("4. review regressions")
    import db, skip_signals, sampler as smp
    ver = skipsig.VersionIndex([{"version_id": 1, "applied_at": 0, "tracks": [f"p{i}" for i in range(1, 41)]}])
    # R1 overnight pause reported as 'paused', resumed next morning, tapped away after 18s
    tl = (Timeline(1_790_724_600_000 - 13 * 3600 * S).play("moon", 166, 20).pause("moon", 166, 20, 12.7 * 3600)
          .play("moon", 166, 18, start_s=20).play("p1", 165, 165).play("p2", 170, 170))
    ks = kinds(sample_run(tl)[0], ver)
    check("skip" not in kind_of(ks, "moon") and "skim" not in kind_of(ks, "moon"),
          f"R1 overnight pause + morning tap is never a skip, got {kind_of(ks, 'moon')}")
    # R2 tunnel: playing at 10s, 3 min of 204 while the phone plays offline, reconnects on the next track
    tl = Timeline().play("p4", 180, 180).play("p5", 200, 10).pause("p5", 200, 10, 180, as_204=True) \
        .play("p6", 180, 170, start_s=10).play("p7", 180, 180)
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p5") == ["neutral"], f"R2 exit bridged by a 204 (tunnel) = neutral/lost, got {kind_of(ks, 'p5')}")
    # R5 previous twice (c → b → a): b must be neutral
    tl = Timeline().play("a", 180, 180).play("b", 180, 180).play("c", 180, 8).play("b", 180, 4).play("a", 180, 180).play("z", 180, 180)
    ks = kinds(sample_run(tl)[0])
    check(kind_of(ks, "b")[-1:] == ["neutral"], f"R5 'previous' twice: the middle track is neutral, got {kind_of(ks, 'b')}")
    # R6 backward tap mid-session (p12 → p5) = neutral
    tl = Timeline().play("p11", 180, 180).play("p12", 180, 15).play("p5", 180, 180).play("p6", 180, 180)
    ks = kinds(sample_run(tl)[0], ver)
    check(kind_of(ks, "p12") == ["neutral"], f"R6 tapping an EARLIER track is neutral, got {kind_of(ks, 'p12')}")
    # R8 flush(hint='lost') is honoured
    seg = skipsig.Segmenter()
    resp, http = Timeline().play("x", 200, 60).response(1_790_724_600_000 + 30 * S)
    seg.feed(skipsig.parse_player(resp, http, 1_790_724_600_000 + 30 * S))
    check(seg.flush(1_790_724_600_000 + 32 * S)[0]["end_reason"] == "lost", "R8 flush(lost) closes as lost")
    # F1 shuffled order (same plays, positions permuted) → no positional inference at all
    fx = json.loads(FIXTURE.read_text())
    events = [{k: e[k] for k in ("track_id", "played_at", "duration_ms", "context_uri")} for e in fx["events"]]
    shuffled = list(fx["version"]["tracks"])
    random.Random(3).shuffle(shuffled)
    sig, _ = skipsig.infer_uncovered(events, skipsig.VersionIndex([{**fx["version"], "tracks": shuffled}]),
                                     [], [], None, fx["playlist_uri"])
    check(sig == [], f"F1 a shuffled session yields no inference, got {len(sig)} signals")
    # F2 forward tap over 13 positions (p6 heard 25s, unrecorded) → nothing; F3 T3 far from the end → nothing
    t0 = 1_790_724_600_000
    ev = lambda tid, t, ctx=PL: {"track_id": tid, "played_at": t, "duration_ms": 180 * S, "context_uri": ctx}
    seq = [ev("p3", t0), ev("p4", t0 + 180 * S), ev("p5", t0 + 360 * S), ev("p20", t0 + 565 * S), ev("p21", t0 + 745 * S)]
    sig, _ = skipsig.infer_uncovered(seq, ver, [], [], None, PL)
    check(sig == [], f"F2 a 14-position jump is a tap, not skips — got {[x['position'] for x in sig]}")
    seq = [ev("p3", t0), ev("p4", t0 + 180 * S), ev("p5", t0 + 360 * S), ev("Q", t0 + 600 * S), ev("p6", t0 + 780 * S)]
    sig, _ = skipsig.infer_uncovered(seq, ver, [], [], None, PL)
    check(sig == [], f"F3 a queued track mid-playlist doesn't fire T3 — got {[x['position'] for x in sig]}")
    # P4 / P5 config safety
    check(skipsig.merge_config({"cooldown": None, "judge": {"t_pass_ms": None}})["cooldown"]["first_days"] == 3
          and skipsig.merge_config({"judge": {"t_pass_ms": None}})["judge"]["t_pass_ms"] == 1500,
          "P5 null config blocks/keys keep the defaults")
    live = skip_signals.load_config
    try:
        skip_signals.load_config = lambda: {**live(), "enabled": "false"}
        check(skip_signals.enabled() is False, "P4 a quoted \"false\" never enables skip signals")
    finally:
        skip_signals.load_config = live
    # P2 label parsing
    pl = skip_signals._parse_labels
    check(pl("17, 30 not")[0] == [(17, "not"), (30, "not")], f"P2 '17, 30 not' → both, got {pl('17, 30 not')}")
    check(pl("25–28 skip")[0] == [(25, "skip"), (26, "skip"), (27, "skip"), (28, "skip")], "P2 range 25–28")
    check(pl("#17: no, 30 skipped, 12 maybe")[0] == [(17, "not"), (30, "skip"), (12, "maybe")], "P2 mixed forms")
    check(pl("ok")[1] is True and pl("ok")[0] == [], "P2 ok")
    try:
        pl("17 30")
        check(False, "P2 numbers without a label must be rejected")
    except SystemExit:
        check(True, "")

    neg_text = "17 isn't a skip, 30 wasn't skipped, 12 skip"
    check(pl(neg_text)[0] == [(17, "not"), (30, "not"), (12, "skip")], f"P2 negation, got {pl(neg_text)[0]}")
    check(skipsig.merge_config(True)["enabled"] is False, "P5 a non-mapping skip_signals block = defaults (off)")
    # R9 a long 'paused' state spawns no junk segments
    tl = (Timeline(1_790_724_600_000 - 13 * 3600 * S).play("m9", 166, 20).pause("m9", 166, 20, 12.7 * 3600)
          .play("m9", 166, 18, start_s=20).play("p1", 165, 165).play("p2", 170, 170))
    ks = kinds(sample_run(tl)[0], ver)
    check(len(kind_of(ks, "m9")) <= 2, f"R9 overnight pause yields ≤2 segments, got {kind_of(ks, 'm9')}")

    # DB-backed checks
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "r.db")
    db.init_db()
    now = 1_790_900_000_000
    DAY = 86400 * S
    tracks = [f"p{i}" for i in range(1, 11)]
    db.upsert_track_cache([{"track_id": t, "name": f"Song {t}", "artist_ids": [f"a{t}"], "duration_ms": 200 * S}
                           for t in tracks])
    db.save_playlist_version([(t, "core", f"Song {t}") for t in tracks], applied_at=now - 20 * 3600 * S)

    def seg(tid, first, listened, reason="early", prog=None, **kw):
        prog = listened if prog is None else prog
        d = {"track_id": tid, "linked_from": None, "name": f"Song {tid}", "duration_ms": 200 * S,
             "context_uri": PL, "first_seen": first, "last_seen": first + listened, "exit_lo": first + listened,
             "exit_hi": first + listened + 300, "listened": listened, "listened_lo": listened,
             "listened_hi": listened + 300, "end_progress_hi": prog + 300, "last_progress": prog,
             "remaining_ms": 200 * S - prog, "last_status": "playing", "end_reason": reason,
             "session_start": False, "resumed": False, "shuffle": False, "n_samples": 5}
        d.update(kw)
        return d
    base = now - 10 * 3600 * S
    # R4 server-extrapolated "completion": recorded end at 40s → truncated → skip
    db.insert_segments([seg("p1", base, 199 * S, "completed", prog=199 * S)])
    db.upsert_play_events([{"track_id": "p1", "played_at": base + 40 * S, "ms_played": 40 * S,
                            "duration_ms": 200 * S, "context_uri": PL}])
    # R7 67s of 170-ish (≈33%) early exit → cooldown; P8 recorded end 25s after exit_hi → matched once
    db.insert_segments([seg("p2", base + 600 * S, 67 * S), seg("p3", base + 900 * S, 200 * S - 12 * S, "completed",
                                                                 prog=200 * S - 12 * S)])
    db.upsert_play_events([{"track_id": "p3", "played_at": base + 900 * S + 188 * S + 300 + 25 * S,
                            "ms_played": 200 * S, "duration_ms": 200 * S, "context_uri": PL}])
    db.extend_coverage(base - 60 * S, base + 1300 * S)
    skip_signals.rebuild_signals(now)
    rows = {(r["track_id"], r["kind"]) for r in db.get_skip_signals_since(0)}
    check(("p1", "skip") in rows, f"R4 recorded end inside the segment truncates it → skip, got {sorted(rows)}")
    cool = skip_signals.cooldown_state(now)
    check("p2" in cool["cooldown"], "R7 an early exit at ~33% feeds the cooldown")
    ov, derived = skip_signals.scoring_inputs(now - 7 * DAY)
    check(not any(d["track_id"] == "p3" for d in derived) and ("p3", base + 900 * S + 188 * S + 300 + 25 * S) in ov,
          "P8 a recorded end 25s after the exit still matches (no double count)")
    # R10 resumed tracks are judged on where they were LEFT: resumed at 70% and left
    # at 95% (50s heard) is a listen; left at 85% is a skip (user rule)
    db.insert_segments([seg("p4", base + 1100 * S, 50 * S, prog=190 * S, resumed=True, session_start=True),
                        seg("p5", base + 1150 * S, 30 * S, prog=170 * S, resumed=True, session_start=True)])
    skip_signals.rebuild_signals(now)
    cs = skip_signals.cooldown_state(now)["cooldown"]
    check("p4" not in cs and "p5" in cs, f"R10 resumed: left at 95% = listen, at 85% = skip (got {sorted(cs)})")
    # N1 a skip late in the song is still scored as a skip (capped inside the skip bracket)
    db.insert_segments([seg("p6", base + 1180 * S, 140 * S)])        # next at 70%
    skip_signals.rebuild_signals(now)
    r6 = [r for r in db.get_skip_signals_since(0) if r["track_id"] == "p6" and r["source"] == "sampler"]
    check(r6 and r6[0]["kind"] == "skip" and r6[0]["score_ms"] <= 0.29 * 200 * S,
          f"N1 a 70% skip scores inside the skip bracket, got {r6 and (r6[0]['kind'], r6[0]['score_ms'])}")
    # P1 coverage_miss is not 'seen by the sampler'
    db.upsert_play_events([{"track_id": "p9", "played_at": base + 1200 * S, "ms_played": 200 * S,
                            "duration_ms": 200 * S, "context_uri": PL}])
    skip_signals.rebuild_signals(now)
    v = skip_signals.version_for_day(skip_signals._day(base))
    dd = skip_signals.day_outcomes(v, now)
    check(dd["matched"] == 2, f"P1 digest coverage counts only sampler-matched plays (2), got {dd['matched']}")
    # P3 accuracy only judges listening up to the review
    skip_signals.label(skip_signals._day(base), "ok", now_ms=base + 1300 * S)
    db.insert_segments([seg("p8", base + 2000 * S, 8 * S)])     # skipped AFTER the review
    skip_signals.rebuild_signals(now)
    acc = skip_signals.accuracy()
    check(acc["fp"] == 0 and acc["fn"] == 0, f"P3 post-review listening is not scored, got {acc}")
    # P6 demotion expires after demote_max_days
    db.insert_segments([seg("p7", now - 40 * DAY, 8 * S), seg("p7", now - 39 * DAY, 8 * S)])
    skip_signals.rebuild_signals(now)
    check("p7" not in skip_signals.cooldown_state(now)["demoted"], "P6 a demotion older than 30 days has expired")
    # P9 a session across midnight is one listening day
    mid = int(1_790_870_400_000)                                   # some SGT midnight-ish instant
    db.insert_segments([seg("p10", mid - 120 * S, 8 * S), seg("p10", mid + 120 * S, 8 * S)])
    skip_signals.rebuild_signals(now)
    check(skip_signals.negative_days("p10", mid - DAY) == 1, "P9 23:58 + 00:02 skips = one listening day")
    # P7 digest budget with 40 long CJK / emoji names keeps the reply line
    long = [(f"x{i}", "core", "晩餐歌 🎵🎶🎵 " * 6) for i in range(40)]
    db.save_playlist_version(long, applied_at=now - 3600 * S)
    db.insert_segments([seg(f"x{i}", now - 3000 * S + i * 20 * S, 8 * S) for i in range(40)])
    skip_signals.rebuild_signals(now)
    dg = skip_signals.build_digest(skip_signals._day(now - 3600 * S), now)
    check(skip_signals._discord_len(dg) <= 1900 and "Reply only if something is wrong" in dg,
          f"P7 worst-case digest fits Discord and keeps the reply line ({skip_signals._discord_len(dg)} units)")
    # D1 a late second refresh doesn't blank the digest — the reviewed version is
    # the one that saw the day's listening (2026-10-06: a 23:27 refresh made the
    # digest pick its 3-minute-old successor; the whole day came out silent)
    day_review = skip_signals._day(now - 3600 * S)
    before = skip_signals.version_for_day(day_review)
    late_vid = db.save_playlist_version([(f"y{i}", "core", f"Song y{i}") for i in range(3)],
                                        applied_at=now - 120 * S)
    skip_signals.rebuild_signals(now)
    v_after = skip_signals.version_for_day(day_review)
    check(v_after["version_id"] == before["version_id"],
          f"D1 a late refresh doesn't take the day ({before['version_id']} -> {v_after['version_id']}, late={late_vid})")
    dg3 = skip_signals.build_digest(day_review, now)
    check("Reply only if something is wrong" in dg3 and "refreshed again at" in dg3
          and skip_signals._discord_len(dg3) < 1900,
          "D1 the day's digest still renders and notes the mid-day refresh")
    # U1 digest shows "Song — Artist", plain-language sections, no internal tier codes
    fx2 = json.loads(FIXTURE.read_text())
    v2 = fx2["version"]
    vid2 = db.save_playlist_version([(t, None, n, "Artist " + t[:4]) for t, n in zip(v2["tracks"], v2["names"])],
                                    applied_at=v2["applied_at"])
    db.upsert_play_events([{k: e[k] for k in ("track_id", "played_at", "duration_ms", "context_uri")}
                           | {"ms_played": e["ms_played_db"]} for e in fx2["events"]])
    t_end = fx2["events"][-1]["played_at"] + 3600 * S
    skip_signals.rebuild_signals(t_end)
    dg2 = skip_signals.build_digest("2026-09-30", t_end)
    check(f"UNNATURAL — Artist {v2['tracks'][14][:4]}" in dg2, "U1 digest lines show the artist")
    check("Probably skipped" in dg2 and "quick run" in dg2 and "autoplay" in dg2,
          "U1 digest explains inferred skips in plain words")
    check(not any(code in dg2 for code in ("T1", "T2", "T3", "dense")), "U1 no internal tier codes in the digest")
    check(skip_signals._discord_len(dg2) <= 1900 and "Played through" in dg2, "U1 fixture digest fits and keeps the summary")

    # S1 sampler coverage: a 204 breaks the run, playing extends it
    sm = smp.Sampler(lambda force=False: "tok", log=lambda m: None)
    t = now
    for i, st in enumerate(["playing"] * 5 + ["idle"] * 3 + ["playing"] * 5):
        sm._observe({"t": t + i * 3000, "status": st, "track_id": "p1" if st == "playing" else None,
                     "progress_ms": i * 3000, "duration_ms": 200 * S, "context_uri": PL})
    sm._break_coverage()
    sm.flush(force_state=True)
    runs = [r for r in db.get_coverage_since(now - 1) if r[0] >= now]
    check(len(runs) == 2, f"S1 a 204 splits sampler coverage into two runs, got {runs}")


def test_silence_is_correct():
    """v2.20.4 user rule: no reply = the skips were right."""
    print("5. silence = correct (temp DB)")
    import db
    tmp = tempfile.mkdtemp()
    db.DB_PATH = os.path.join(tmp, "s.db")
    db.init_db()
    import skip_signals
    now = 1_790_900_000_000
    DAY, H = 86400 * S, 3600 * S
    tracks = [f"q{i}" for i in range(1, 6)]
    db.upsert_track_cache([{"track_id": t, "name": f"Song {t}", "artist_ids": [f"a{t}"], "duration_ms": 200 * S}
                           for t in tracks])
    va = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - 3 * DAY)
    vb = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - 2 * DAY)
    vq = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - DAY - 2 * H)   # no listening
    vc = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - 10 * H)        # ended < 20 h ago
    vd = db.save_playlist_version([(t, "core") for t in tracks], applied_at=now - H)             # live

    def seg(tid, first, listened, reason="early"):
        return {"track_id": tid, "linked_from": None, "name": f"Song {tid}", "duration_ms": 200 * S,
                "context_uri": PL, "first_seen": first, "last_seen": first + listened, "exit_lo": first + listened,
                "exit_hi": first + listened + 300, "listened": listened, "listened_lo": listened,
                "listened_hi": listened + 300, "end_progress_hi": listened + 300, "last_progress": listened,
                "remaining_ms": 200 * S - listened, "last_status": "playing", "end_reason": reason,
                "session_start": False, "resumed": False, "shuffle": False, "n_samples": 5}
    db.insert_segments([seg("q1", now - 3 * DAY + H, 200 * S, "completed"), seg("q2", now - 3 * DAY + 2 * H, 8 * S),
                        seg("q1", now - 2 * DAY + H, 200 * S, "completed"), seg("q3", now - 2 * DAY + 2 * H, 9 * S),
                        seg("q4", now - 10 * H + H, 7 * S)])
    db.extend_coverage(now - 4 * DAY, now)
    skip_signals.rebuild_signals(now)
    got = skip_signals.auto_accept(now)
    check(got == [va, vb], f"silent days accepted: ended ≥ 20 h ago with listening (not {vq} empty, {vc} recent, {vd} live), got {got}")
    labs = db.get_labels()
    check(labs[va]["silent"] and not labs[va]["explicit"], "silent labels are marked as such")
    check(skip_signals.auto_accept(now) == [], "auto-accept is idempotent")
    acc = skip_signals.accuracy()
    check(acc["days"] == 2 and acc["corrections"] == 0 and acc["precision"] == "100%" and acc["last_correction"] is None,
          f"silence counts as correct, got {acc}")
    # a late reply overrides the silent acceptance and lowers the score
    msg = skip_signals.label(skip_signals._day(now - 2 * DAY + H), "3 not", now_ms=now)
    check("1 correction" in msg, f"a correction replaces the silent labels, got {msg!r}")
    acc2 = skip_signals.accuracy()
    check(acc2["corrections"] == 1 and acc2["precision"] != "100%" and acc2["last_correction"],
          f"a correction lowers precision and is reported, got {acc2}")
    dg = skip_signals.build_digest(skip_signals._day(now - 10 * H + H), now)
    check("no reply = correct" in dg and "1 correction from you" in dg and "Reply only if something is wrong" in dg,
          f"digest wording, got {dg[-300:]!r}")


def main():
    test_scenarios()
    test_fixture()
    test_policy()
    test_review_regressions()
    test_silence_is_correct()
    print(f"\n{_passes} passed, {len(_failures)} failed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
