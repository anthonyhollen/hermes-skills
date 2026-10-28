"""
sampler.py — resident playback sampler (systemd user unit spotify-sampler.service).

Polls GET /me/player adaptively, folds samples into per-track segments
(skipsig.Segmenter) and persists them for skip_signals.rebuild_signals():

  playing                   every sampler.playing_s   (3s)
  first seconds of a track  every sampler.burst_s     (1s) for burst_window_s
  paused                    every sampler.paused_s    (10s)
  stopped < 45 min ago      every sampler.idle_recent_s (5s)  — resume-then-skip
  idle 06:00–24:00 / night  every sampler.idle_s / night_idle_s (15s / 60s)
  sampler.quiet window      ≥30s apart (the 03:00 refresh owns the rate budget)

A token bucket caps requests at sampler.max_req_per_30s; a 429 waits 2× its
Retry-After. Requests go through client._do_request (no retry loop, 8s curl
timeout) and are stamped at the request midpoint. Everything is persisted in
ONE transaction per flush (db.sampler_flush, 2s busy timeout, retried on the
next flush), so a long cron transaction can't stall the loop. The open segment
is persisted in sampler_state, so a restart resumes it. config.yaml is re-read
(and validated) when its mtime changes.

Entry: run_forever(token_fn) — token_fn(force) -> access token, where force is
False (cached), None (drop the cache, re-read) or True (force a refresh); the
wrapper ~/.hermes/scripts/spotify_sampler.py supplies Hermes' resolver.
"""

import os, sys, time, signal
from datetime import datetime, timezone, timedelta
import db, client, skipsig, skip_signals

SGT = timezone(timedelta(hours=8))
PLAYER_ENDPOINT = "me/player?additional_types=episode"
FLUSH_EVERY_S = 30
STATE_EVERY_S = 30
PRUNE_EVERY_S = 6 * 3600
DB_TIMEOUT_S = 2
TIMESYNC_FLAG = "/run/systemd/timesync/synchronized"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hhmm(s: str) -> int:
    h, _, m = s.partition(":")
    return int(h) * 60 + int(m or 0)


class TokenBucket:
    """Sliding-window request cap (time.monotonic seconds)."""

    def __init__(self, capacity: int, per_s: float = 30.0):
        self.capacity, self.per_s = max(1, capacity), per_s
        self.stamps: list[float] = []

    def wait_s(self, now: float) -> float:
        """Seconds until a request is allowed (0 = go now)."""
        self.stamps = [t for t in self.stamps if now - t < self.per_s]
        if len(self.stamps) < self.capacity:
            return 0.0
        return self.per_s - (now - self.stamps[0]) + 0.05

    def take(self, now: float):
        self.stamps.append(now)


class IntervalPolicy:
    def __init__(self, cfg: dict):
        self.c = cfg["sampler"]

    def next_s(self, seg: skipsig.Segmenter, last_status: str | None, now_ms: int) -> float:
        local = datetime.fromtimestamp(now_ms / 1000, SGT)
        minute = local.hour * 60 + local.minute
        q0, q1 = (_hhmm(x) for x in self.c["quiet"])
        if q0 <= minute < q1:
            return 30.0
        o = seg.open
        if last_status == "playing" and o is not None:
            since_start = now_ms - o["first_seen"]
            since_exit = now_ms - (seg.last_exit_t or 0)
            burst_ms = self.c["burst_window_s"] * 1000
            if since_start < burst_ms or since_exit < burst_ms:
                return float(self.c["burst_s"])
            return float(self.c["playing_s"])
        if last_status == "paused":
            return float(self.c["paused_s"])
        # Just stopped (car off, app backgrounded): a resume-then-skip can follow,
        # so stay quick for a while — the bound on unseen listening is this interval.
        if seg.last_playing_t and now_ms - seg.last_playing_t < self.c["idle_recent_window_s"] * 1000:
            return float(self.c["idle_recent_s"])
        return float(self.c["idle_s"] if 6 * 60 <= minute else self.c["night_idle_s"])


def _validated(cfg: dict) -> dict:
    """Raise on a config that would break the loop (e.g. quiet: "02:55-03:20")."""
    pol = IntervalPolicy(cfg)
    probe = skipsig.Segmenter(cfg)
    for status in ("playing", "paused", "idle", None):
        float(pol.next_s(probe, status, _now_ms()))
    int(cfg["sampler"]["max_req_per_30s"])
    return cfg


class Sampler:
    """
    Coverage (sampler_coverage) means "a device was being OBSERVED": runs are
    extended only by samples that show a device state (playing / paused / ad /
    private) at most judge.lost_gap_ms apart. A 204 or stale state (the phone may
    be playing offline), an error, a 429 or any backoff breaks the run, so the
    positional fallback still speaks for that time.
    """

    def __init__(self, token_fn, log=print):
        self.token_fn = token_fn
        self.log = log
        self.stop = False
        self.cfg_mtime = None
        try:
            self.cfg = _validated(self._load_cfg())
        except Exception as e:
            self.log(f"sampler: bad skip_signals config ({e}) — using built-in defaults")
            self.cfg = skipsig.merge_config(None)
        state = None
        try:
            state = db.get_sampler_state("segmenter", timeout=DB_TIMEOUT_S)
        except Exception as e:
            self.log(f"sampler: could not load segmenter state: {e}")
        self.seg = skipsig.Segmenter(self.cfg, state)
        self.policy = IntervalPolicy(self.cfg)
        self.bucket = TokenBucket(self.cfg["sampler"]["max_req_per_30s"])
        self.pending_samples: list[dict] = []
        self.pending_segments: list[dict] = []
        self.pending_coverage: list[tuple[int, int]] = []
        self.cov_from = self.cov_to = None
        self.last_status = None
        self.last_ok_ms = None
        self.backoff_until = 0.0          # time.monotonic()
        self.err_streak = 0
        self.exc_streak = 0
        self.unauth_streak = 0
        self.last_flush = self.last_state = self.last_prune = time.monotonic()
        self.counters = {"requests": 0, "errors": 0, "rate_limited": 0, "segments": 0}

    # -- config -------------------------------------------------------------
    def _load_cfg(self) -> dict:
        try:
            self.cfg_mtime = os.path.getmtime(skip_signals._CFG_PATH)
        except OSError:
            self.cfg_mtime = None
        return skip_signals.load_config()

    def _maybe_reload_cfg(self):
        try:
            m = os.path.getmtime(skip_signals._CFG_PATH)
        except OSError:
            return
        if m == self.cfg_mtime:
            return
        try:
            cfg = _validated(self._load_cfg())
        except Exception as e:
            self.log(f"sampler: config reload rejected, keeping the old one: {e}")
            return
        self.cfg = cfg
        self.seg.cfg = cfg
        self.policy = IntervalPolicy(cfg)
        self.bucket.capacity = max(1, int(cfg["sampler"]["max_req_per_30s"]))
        self.log("sampler: config reloaded")

    # -- coverage -------------------------------------------------------------
    def _break_coverage(self):
        if self.cov_from is not None and self.cov_to is not None and self.cov_to > self.cov_from:
            self.pending_coverage.append((self.cov_from, self.cov_to))
        self.cov_from = self.cov_to = None

    def _extend_coverage(self, t: int):
        if self.cov_to is None or t - self.cov_to > self.cfg["judge"]["lost_gap_ms"]:
            self._break_coverage()
            self.cov_from = t
        self.cov_to = t

    # -- one poll -----------------------------------------------------------
    def poll_once(self) -> dict:
        tok = self.token_fn(False)
        t0 = _now_ms()
        resp, http, retry_after = client._do_request("GET", PLAYER_ENDPOINT, tok, max_time=8)
        t1 = _now_ms()
        self.counters["requests"] += 1
        if http == 429:
            self.counters["rate_limited"] += 1
            wait = 2 * (retry_after if retry_after is not None else 30)
            self.backoff_until = time.monotonic() + wait
            self.log(f"sampler: 429, backing off {wait:.0f}s")
        elif http == 401:
            self.unauth_streak += 1
            # 1st 401: drop the cached token and re-read (a wrapper may have
            # refreshed); 2nd: force a refresh; then back off (revoked grant).
            if self.unauth_streak == 1:
                self.token_fn(None)
            elif self.unauth_streak == 2:
                self.token_fn(True)
            else:
                self.backoff_until = time.monotonic() + min(600, 30 * self.unauth_streak)
                self.log("sampler: still 401 after a forced refresh — backing off "
                         "(re-auth with `hermes auth spotify` if this persists)")
        else:
            self.unauth_streak = 0
        sample = skipsig.parse_player(resp, http, (t0 + t1) // 2, t1 - t0)
        if sample["status"] == "error":
            self.err_streak += 1
            self.counters["errors"] += 1
            self._break_coverage()
            if http not in (401, 429):
                self.backoff_until = max(self.backoff_until,
                                         time.monotonic() + min(60, 2 ** min(self.err_streak, 6)))
        else:
            self.err_streak = 0
            self._observe(sample)
        return sample

    def _observe(self, s: dict):
        try:
            closed = self.seg.feed(s)
        except Exception as e:
            # A Segmenter bug must not kill the service or lose earlier data:
            # drop only the (possibly inconsistent) open segment.
            self.log(f"sampler: segmenter error on {s.get('status')} sample: {type(e).__name__}: {e}")
            self.seg.open = None
            closed = []
        if closed:
            self.pending_segments.extend(closed)
            self.counters["segments"] += len(closed)
        if s["status"] != "idle":
            self.pending_samples.append(s)
        self.last_status = s["status"] if s["status"] in ("playing", "paused") else "idle"
        if s["status"] in ("playing", "paused", "non_track", "private"):
            self._extend_coverage(s["t"])
        else:
            self._break_coverage()         # 204 / stale: a phone may be playing offline
        self.last_ok_ms = s["t"]
        if closed:
            self.flush(force_state=True)

    # -- persistence --------------------------------------------------------
    def flush(self, force_state: bool = False):
        now = time.monotonic()
        cov = list(self.pending_coverage)
        if self.cov_from is not None and self.cov_to is not None and self.cov_to > self.cov_from:
            cov.append((self.cov_from, self.cov_to))    # the open run so far (upserted by start)
        state = None
        if force_state or now - self.last_state >= STATE_EVERY_S:
            state = {"segmenter": self.seg.state(),
                     "heartbeat": {"t": _now_ms(), "last_ok": self.last_ok_ms, "status": self.last_status,
                                   "open_track": (self.seg.open or {}).get("track_id"), **self.counters}}
        try:
            db.sampler_flush(self.pending_samples, self.pending_segments, cov, state, timeout=DB_TIMEOUT_S)
            self.pending_samples, self.pending_segments, self.pending_coverage = [], [], []
            if state is not None:
                self.last_state = now
            self.last_flush = now
        except Exception as e:
            self.log(f"sampler: DB write deferred: {e}")   # everything stays buffered
            self.last_flush = now
        if len(self.pending_samples) > 5000:
            self.pending_samples = self.pending_samples[-5000:]   # never grow without bound

    def maintenance(self):
        now = time.monotonic()
        if now - self.last_prune >= PRUNE_EVERY_S:
            try:
                db.prune_sampler_data(timeout=DB_TIMEOUT_S)
                self.last_prune = now
            except Exception as e:
                self.log(f"sampler: prune deferred: {e}")

    # -- loop ---------------------------------------------------------------
    def _next_interval(self) -> float:
        try:
            return float(self.policy.next_s(self.seg, self.last_status, _now_ms()))
        except Exception as e:
            self.log(f"sampler: interval policy failed ({e}); using 5s")
            return 5.0

    def run(self):
        self._wait_for_clock()
        while not self.stop:
            self._maybe_reload_cfg()
            now = time.monotonic()
            if now < self.backoff_until:
                self._break_coverage()
                self._sleep(min(self.backoff_until - now, 5))
                continue
            wait = self.bucket.wait_s(now)
            if wait > 0:
                self._sleep(wait)
                continue
            self.bucket.take(time.monotonic())
            try:
                self.poll_once()
                self.exc_streak = 0
            except Exception as e:
                # e.g. the token resolver failing (network down, grant revoked):
                # exponential backoff up to 10 min so nothing is hammered.
                self.exc_streak += 1
                self.counters["errors"] += 1
                self._break_coverage()
                wait = min(600, 15 * 2 ** min(self.exc_streak, 6))
                self.log(f"sampler: poll failed ({type(e).__name__}: {e}); retry in {wait}s")
                self.backoff_until = time.monotonic() + wait
            if time.monotonic() - self.last_flush >= FLUSH_EVERY_S:
                self.flush()
                self.maintenance()
            self._sleep(self._next_interval())
        self._break_coverage()
        self.flush(force_state=True)

    def _sleep(self, s: float):
        end = time.monotonic() + max(0.0, s)
        while not self.stop and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    def _wait_for_clock(self, max_wait_s: int = 300):
        """Timestamps must be right before segments are written (NTP after boot)."""
        if not os.path.isdir(os.path.dirname(TIMESYNC_FLAG)):
            return
        waited = 0
        while not self.stop and not os.path.exists(TIMESYNC_FLAG) and waited < max_wait_s:
            if waited == 0:
                self.log("sampler: waiting for NTP sync")
            time.sleep(5)
            waited += 5


def run_forever(token_fn, log=None):
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    s = Sampler(token_fn, log)

    def _stop(signum, frame):
        s.stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log("sampler: started")
    s.run()
    log("sampler: stopped")
