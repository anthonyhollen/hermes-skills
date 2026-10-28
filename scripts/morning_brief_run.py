#!/usr/bin/env python3
"""Morning Brief (job 4879) — no_agent wrapper.

Runs the morning-brief pipeline (fetch_rss → summarize → send_brief) directly,
removing the outer LLM agent. The LLM summarization still happens inside
summarize.py via Hermes' configured (cheap) provider; send_brief.py posts to
Discord itself, so the cron job keeps deliver=local (no double delivery).

Pass --dry-run to run fetch+summarize and preview send_brief without posting. It still
fetches, calls the LLM and rewrites articles*.json, but leaves feed_health.json and the
seen ledger untouched (fetch_rss/send_brief get --dry-run).

Only one run at a time (a flock on <data dir>/.run.lock), so an overlapping run can't
post a second brief or race the first on the same .tmp files. A real run WAITS for the
lock (LOCK_WAIT_SECONDS; a manual dry run normally takes 15-60 s) and, if it is still
held, fails through fail() so the missed brief is ANNOUNCED — exiting silently would let
a manual --dry-run started at 07:29 cost the day's brief with no alert. The wait counts
against the same 600 s cron watchdog as the steps, so each step's timeout is also capped
by the budget left (minus a reserve for fail()'s notify). A --dry-run gives up
immediately instead. The lock fd
is passed to every step, so it stays held until the last child exits even if this
wrapper is killed first.

Reliability (deliver:local self-notify): the cron scheduler pushes NO failure alert
for a deliver:local job — a crashed/errored/hung run is otherwise invisible (only a
red `hermes cron list` status). So each step runs under a per-step subprocess timeout
(kept well under the global cron watchdog), and any hard failure (non-zero exit or
timeout) is announced to the brief channel via `fail()` before exiting non-zero.
"""
import fcntl, os, subprocess, sys, json, time, urllib.request

# Line-buffer our own stdout. The scheduler attaches a PIPE (not a tty) and sets no
# PYTHONUNBUFFERED, so bare print() was 8KB block-buffered and flushed only at exit —
# while the three child processes inherit fd 1 and flush at *their* exit. Every archived
# run therefore showed all three "→ step" markers AFTER all of the pipeline output,
# making them useless for attributing a line to a step. Worse, a watchdog SIGKILL
# discarded the parent's buffer entirely, so a killed run left an empty archive.
sys.stdout.reconfigure(line_buffering=True)

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SCRIPTS = os.path.join(HERMES_HOME, "skills", "productivity", "morning-brief", "scripts")
DRY = "--dry-run" in sys.argv

# The morning-brief pipeline (feedparser etc.) targets the SYSTEM python3, not
# the gateway venv that runs this wrapper as a no_agent cron script — the venv
# has no feedparser. Use /usr/bin/python3 when present, else fall back.
INTERP = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

# summarize.py imports Hermes' own config + runtime_provider from the hermes-agent tree
# to use the configured model. Since Hermes v0.22.0 (2026-10-14) that code needs Hermes'
# dependencies (ruamel.yaml, …), which the system python3 lacks: the 10-15 brief failed
# at summarize.py ("No module named 'ruamel'"). So summarize runs under the first
# interpreter that can import them — this wrapper's own (the scheduler runs no_agent
# scripts on Hermes' dependency venv), the install's current dependency venv
# (installs/*/facts.json; its path changes when Hermes rebuilds it), the gateway venv —
# with INTERP last (summarize then reports the import error itself, through fail()).
HERMES_AGENT_DIR = os.path.join(HERMES_HOME, "hermes-agent")
HERMES_PROBE_SECONDS = 15


def _hermes_interpreters():
    cands = [sys.executable]
    try:
        import glob
        for facts in sorted(glob.glob(os.path.join(HERMES_HOME, "installs", "*", "facts.json"))):
            with open(facts) as f:
                cands.append(os.path.join(json.load(f)["packages"]["venv"]["environment"], "bin", "python"))
    except Exception:
        pass
    cands += [os.path.join(HERMES_AGENT_DIR, "venv", "bin", "python"), INTERP]
    out = []
    for c in cands:
        if c and c not in out and os.path.exists(c):
            out.append(c)
    return out


def _summarize_interp():
    """The first interpreter that imports requests + Hermes' config and runtime
    provider; INTERP if none does."""
    probe = ("import sys; sys.path.insert(0, %r); "
             "import requests, hermes_cli.config, hermes_cli.runtime_provider" % HERMES_AGENT_DIR)
    for p in _hermes_interpreters():
        try:
            if subprocess.run([p, "-c", probe], cwd=SCRIPTS, timeout=HERMES_PROBE_SECONDS,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return p
        except (OSError, subprocess.TimeoutExpired):
            continue
    return INTERP

# Per-step wall-clock caps (backstop, not an SLA). Typical runtime is ~20s fetch +
# ~4s summarize + ~1s send; these bound a hung/pathologically-slow step so it fails
# WITH a notify here, before the scheduler's silent SIGKILL. The sum (560s) stays
# under the global cron.script_timeout_seconds (600s). send_brief gets the largest
# budget because a 429-throttled delivery can legitimately run long (Retry-After
# backoff across ~10 POSTs) and a killed send costs a duplicate brief next run.
STEP_TIMEOUT = {"fetch_rss.py": 120, "summarize.py": 200, "send_brief.py": 240}
DEFAULT_ENV = os.path.join(HERMES_HOME, ".env")
LOCK_WAIT_SECONDS = 30
# The cron watchdog (cron.script_timeout_seconds) SIGKILLs this whole process at 600 s.
# Each step's timeout is capped by what is left of that budget minus NOTIFY_RESERVE, so
# fail() always has time to post its alert — fixed per-step caps alone didn't guarantee
# it once a lock wait could precede them (30 + 560 + a 15 s notify > 600).
WATCHDOG_SECONDS = 600
NOTIFY_RESERVE_SECONDS = 20
_T0 = time.monotonic()
_LOCK_FD = []                   # the held lock's fd, passed to each step


def _load_env(name, default=""):
    """Read a KEY=value from ~/.hermes/.env (the scheduler doesn't export these to
    no_agent scripts). Returns default if absent/unreadable."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(DEFAULT_ENV) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return default


def fail(step_name, reason):
    """Announce a hard failure to the brief channel, then exit non-zero.

    deliver:local means the scheduler won't alert on failure, so we self-notify.
    Best-effort: a failed notify still exits non-zero (sets last_error, red in cron
    list). Suppressed under --dry-run (manual testing must not post). Uses stdlib
    urllib with an explicit User-Agent (Cloudflare 1010-blocks the bare urllib UA).
    """
    msg = "⚠️ Morning brief failed at %s: %s" % (step_name, reason)
    print(msg, file=sys.stderr)
    if not DRY:
        token   = _load_env("DISCORD_BOT_TOKEN")
        channel = _load_env("DISCORD_BRIEF_CHANNEL_ID") or _load_env(
            "DISCORD_HOME_CHANNEL", "1499972245437550602")
        if token:
            try:
                body = json.dumps({"content": msg[:2000],
                                   "allowed_mentions": {"parse": []}}).encode()
                req = urllib.request.Request(
                    "https://discord.com/api/v10/channels/%s/messages" % channel,
                    data=body, method="POST",
                    headers={"Authorization": "Bot %s" % token,
                             "Content-Type": "application/json",
                             "User-Agent": "DiscordBot (hermes-agent/1.0)"})
                urllib.request.urlopen(req, timeout=15)
            except Exception as e:
                print("(failure notify could not be posted: %s)" % e, file=sys.stderr)
    sys.exit(1)


def step(name, extra=None):
    interp = _summarize_interp() if name == "summarize.py" else INTERP
    remaining = WATCHDOG_SECONDS - NOTIFY_RESERVE_SECONDS - (time.monotonic() - _T0)
    limit = min(STEP_TIMEOUT.get(name, remaining), remaining)
    if limit < 5:
        fail(name, "no time left in the %ds cron budget" % WATCHDOG_SECONDS)
    cmd = [interp, os.path.join(SCRIPTS, name)] + (extra or [])
    print("→ %s %s" % (name, " ".join(extra or [])) + ("  [%s]" % interp if interp != INTERP else ""))
    # PYTHONUNBUFFERED keeps each child's stdout unbuffered too. Without it the child's
    # stderr (always unbuffered) lands in the merged stream ahead of its own stdout,
    # so a "[DOWN] <feed>" line appears before the fetch output it belongs to.
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        # stderr=STDOUT merges each step's stderr into our stdout. The cron scheduler
        # captures both pipes but RETURNS ONLY STDOUT on a successful run, so every
        # diagnostic this pipeline emits — [DOWN] <feed>, "N feed(s) down", [DEDUP]
        # drops, [WARN] summary failures — was discarded on the happy path. 0 of 50
        # archived runs contained a single stderr line, including a day when 4 feeds
        # were down and the brief posted a dead-feed notice. Merging makes the archive
        # the forensic record it was meant to be.
        r = subprocess.run(cmd, cwd=SCRIPTS, timeout=limit,
                           stderr=subprocess.STDOUT, env=env, pass_fds=tuple(_LOCK_FD))
    except subprocess.TimeoutExpired:
        fail(name, "timed out after %.0fs" % limit)
    except OSError as e:
        # Couldn't even launch the step (missing script, bad cwd, exec error) — route
        # through fail() so it self-notifies instead of crashing with a bare traceback.
        fail(name, "could not launch: %s" % e)
    if r.returncode != 0:
        fail(name, "exit %d" % r.returncode)


def _acquire_lock():
    """Take the single-run lock. Returns the open handle (keep it alive), or None if the
    lock file can't be used at all — then run unlocked rather than lose the brief."""
    data_dir = os.path.join(HERMES_HOME, "morning-brief")
    try:
        os.makedirs(data_dir, exist_ok=True)
        fh = open(os.path.join(data_dir, ".run.lock"), "a")
    except OSError as e:
        print("[WARN] run lock unavailable (%s) — continuing without it" % e, file=sys.stderr)
        return None
    deadline = time.monotonic() + (0 if DRY else LOCK_WAIT_SECONDS)
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _LOCK_FD[:] = [fh.fileno()]
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                if DRY:
                    # Not via fail(): nothing is wrong, a real run is in progress.
                    print("Another morning-brief run is in progress — exiting.",
                          file=sys.stderr)
                    sys.exit(1)
                fail("lock", "another run still held the lock after %ds" % LOCK_WAIT_SECONDS)
            time.sleep(2)
        except OSError as e:
            print("[WARN] run lock failed (%s) — continuing without it" % e, file=sys.stderr)
            fh.close()
            return None


def main():
    _lock = _acquire_lock()         # noqa: F841 — held until exit
    step("fetch_rss.py", ["--dry-run"] if DRY else None)
    step("summarize.py")
    step("send_brief.py", ["--dry-run"] if DRY else None)
    print("Morning brief pipeline complete%s." % (" (dry-run)" if DRY else ""))


if __name__ == "__main__":
    main()
