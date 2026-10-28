#!/usr/bin/env python3
"""
Food Expense Payment Reminder (multi-provider: FoodPanda + Grab)

Monthly payment reminder: fires on the 1st of each month (with a small catch-up
window on days 1–5 if an earlier run failed) and asks the account holder to make
payment for the PREVIOUS month's food expenses, showing that month's total from
the tracker sheet.

Modelled on the cc-bill-review payment reminder:
- self-posts to Discord via the REST API (no LLM in the loop, no `hermes` CLI) —
  the no_agent cron runs this script directly with deliver: local;
- @-mentions the account holder so it pings (allowed_mentions allow-lists exactly
  that one id, so a stray <@id> could never ping anyone else);
- idempotent per month via a state file; a timed-out POST is not re-sent within the run
  (and Discord `nonce` + `enforce_nonce` dedupes a re-send within minutes). Residual: if a
  POST times out AFTER Discord created it, the run reports a failure and the next day in the
  window posts it again — one possible duplicate, never a missed reminder;
- if the nightly sync on the month's last day (or any later one) didn't finish OK, the
  reminder says its total may be missing orders.

Testing flags:
    --dry-run              print what would be sent; no post, no state write
    --simulate-date DATE   pretend today is DATE (YYYY-MM-DD), e.g. 2026-10-01. IMPLIES
                           --dry-run (a simulated date must never post or mark a cycle)
                           unless --force is also given
    --force                post even if the window/state say no; DOES mark state
"""

import json, os, re, socket, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SKILLS_DIR  = os.path.join(HERMES_HOME, "skills", "productivity", "google-workspace", "scripts")

SHEET_ID = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"

# Where the reminder posts (override for tests, e.g. the playground channel).
DISCORD_CHANNEL_ID = os.environ.get("FOODPANDA_REMINDER_CHANNEL", "1499972245437550602")
# The one user allowed to be @-mentioned (so the reminder pings). Empty = no mention.
NOTIFY_USER_ID = os.environ.get("FOODPANDA_REMINDER_NOTIFY_USER", "470611394698412052")
# Test knob: keep the mention text but never actually ping (blocked via allowed_mentions).
NO_PING = os.environ.get("FOODPANDA_REMINDER_NO_PING") == "1"

SGT = timezone(timedelta(hours=8))

# Fire on day 1 of each month; days 2–5 are a catch-up window if an earlier run failed.
FIRE_DAY_MIN, FIRE_DAY_MAX = 1, 5

# Per-month idempotency: {"last_fired_cycle": "Aug 2026"} — the label is the month the
# reminder was ABOUT (the previous calendar month), so the same cycle can't re-fire.
STATE_FILE = os.path.join(HERMES_HOME, "foodpanda_reminder_state.json")

# The scheduler's own job records — read to check the sync ran (OK) today before the reminder.
JOBS_FILE   = os.path.join(HERMES_HOME, "cron", "jobs.json")
SYNC_SCRIPT = "foodpanda_sync.py"

# Timeout budget — the cron scheduler SIGKILLs a no_agent script at the global
# `cron.script_timeout_seconds` (600s here) with no Discord alert, so bound every call and cap
# cumulative retries. Discord posts aren't deadline-gated. (Mirror of foodpanda_weekly_report.py.)
CALL_TIMEOUT      = 20
RUN_DEADLINE_SECS = 300   # RUN_DEADLINE + CALL_TIMEOUT + the Discord posts (≤ ~51s each) < 600s
socket.setdefaulttimeout(CALL_TIMEOUT)   # bounds in-process sockets (subprocess calls pass timeout=)
_run_deadline = None

# Locale-independent month abbreviations — must match the other scripts' tab naming.
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Load Discord token from .env file (not exported, so direct read required)
DISCORD_BOT_TOKEN = ""
ENV_FILE = os.path.join(HERMES_HOME, ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN=") and not line.startswith("DISCORD_BOT_TOKEN=#"):
                DISCORD_BOT_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")

def retry(fn, max_attempts=3, backoff=5):
    """Retry a no-argument function on transient HTTP/API errors with exponential backoff.
    A run-global deadline bounds cumulative time: no new attempt starts and no backoff sleep
    crosses `_run_deadline`, keeping the worst case under the cron SIGKILL watchdog."""
    for attempt in range(max_attempts):
        if _run_deadline is not None and time.monotonic() >= _run_deadline:
            raise TimeoutError("run deadline exceeded — aborting to stay under the cron watchdog")
        try:
            return fn()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            err_str = str(e).lower()
            if any(k in err_str for k in ["429", "503", "ratelimit", "timeout", "transient", "timed out"]):
                if _run_deadline is not None and time.monotonic() + backoff >= _run_deadline:
                    raise
                print(f"Transient error, retrying in {backoff}s: {e}")
                time.sleep(backoff)
                backoff *= 2
            else:
                raise

def run_gapi(args):
    """Run google_api.py and return parsed JSON. The returncode check lives INSIDE the
    retried call so transient API failures (429/503) are retried; the subprocess is bounded
    by CALL_TIMEOUT so a hung google_api.py raises TimeoutExpired instead of blocking to the
    cron SIGKILL."""
    # sys.executable (the gateway venv under cron), never a bare "python" that resolves via PATH.
    cmd = [sys.executable, f"{SKILLS_DIR}/google_api.py"] + args
    def call():
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"gapi failed: {result.stderr}")
        return json.loads(result.stdout)
    return retry(call)

def sgt_now():
    return datetime.now(SGT)

def month_label(d):
    """'May 2026' for a date/datetime (locale-independent)."""
    return f"{MONTHS[d.month - 1]} {d.year}"

def reminder_cycle(today):
    """The month the reminder is about: the calendar month BEFORE `today` (an SGT date).
    e.g. 2026-10-01 → 'Sep 2026'."""
    prev = today.replace(day=1) - timedelta(days=1)
    return month_label(prev)

def parse_money(x):
    """Lenient numeric parse tolerant of currency formatting ('$30.32', 'S$ 1,037.17')."""
    s = re.sub(r"[^\d.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None

def _is_missing_sheet_error(e):
    """A read against a non-existent tab returns HTTP 400 'Unable to parse range' from the
    Sheets API. Treat that as 'sheet not created yet' (benign), not a hard error."""
    return "Unable to parse range" in str(e)

def read_month_total(month_name):
    """Read (total, count, ok) from a month tab (sum of col C, skipping the header).
    A missing tab → (0.0, 0, True): benign 'no expenses'. A genuine read failure →
    (None, 0, False) so the caller can alert and retry on the next run."""
    try:
        data = run_gapi(["sheets", "get", SHEET_ID, f"'{month_name}'!A:C"])
    except Exception as e:
        if _is_missing_sheet_error(e):
            return 0.0, 0, True
        print(f"Could not read sheet '{month_name}': {e}")
        return None, 0, False
    total, count = 0.0, 0
    for row in data[1:]:  # skip header
        if len(row) >= 3 and row[0] and row[1] and row[2]:
            amount = parse_money(row[2])
            if amount is None:
                continue
            total += amount
            count += 1
    return round(total, 2), count, True

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Could not read state file: {e}")
    return {}

def save_state(state):
    """Atomic write: temp file + rename (mirror of foodpanda_sync.py)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.rename(tmp, STATE_FILE)

def sync_covered_month_end(today):
    """Has the foodpanda_sync.py cron job finished OK on or after the previous month's LAST day
    (SGT)? — i.e. did the 23:30 sync on the month's last night (or a later one, on days 2–5)
    succeed, so the month's orders are in the sheet. `last_run_at` is the run's END time
    (+08:00 ISO; naive = SGT). True = yes; False = not confirmed (it failed, or hasn't run since,
    or last_run_at is missing/unreadable); None = can't tell (jobs.json unreadable / no sync
    job) — treated as no news, not a warning."""
    try:
        with open(JOBS_FILE) as f:
            jobs = json.load(f)
        jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
        syncs = [j for j in jobs if os.path.basename(str(j.get("script") or "")) == SYNC_SCRIPT
                 and j.get("enabled", True)]
    except Exception:
        return None
    if not syncs:
        return None
    for j in syncs:
        try:
            ran = datetime.fromisoformat(j.get("last_run_at") or "")
        except (TypeError, ValueError):
            continue
        if ran.tzinfo is None:
            ran = ran.replace(tzinfo=SGT)    # the scheduler writes SGT wall-clock times
        month_end = today.replace(day=1) - timedelta(days=1)
        if ran.astimezone(SGT).date() >= month_end and j.get("last_status") == "ok":
            return True
    return False

def build_message(cycle, total, sync_ok=None):
    """The reminder text — plain content (not an embed), like the cc-bill-review reminder."""
    mention = f"<@{NOTIFY_USER_ID}> " if NOTIFY_USER_ID else ""
    text = (f"{mention}🍔 Reminder: make payment for **{cycle}** food expenses. "
            f"Total **S${total:,.2f}**.")
    if sync_ok is False:
        text += ("\n_Heads-up: the receipt sync didn't complete on the month's last night, so "
                 "some orders may be missing from this total._")
    return text

def _past_deadline():
    return _run_deadline is not None and time.monotonic() >= _run_deadline

def discord_send(channel_id, payload, timeout=15, attempts=3, retry_ambiguous=False):
    """POST one Discord message. Retries ONLY when Discord definitely did not create it — HTTP
    429 (honours retry_after, capped at 10s) / 503, or a URLError raised while connecting/sending.
    A read TIMEOUT after the request went out is NOT retried (the message may already exist and
    a re-send would duplicate it), nor is a 500/502/504 — unless `retry_ambiguous` (only for a
    payload carrying a Discord `nonce` + `enforce_nonce`, which dedupes the re-send). Posts still go out after the run
    deadline — so a slow run can still alert — but then single-shot (≤ `timeout` each), which
    keeps the post-deadline tail bounded; before it, worst case ~51s per post."""
    data = json.dumps(payload).encode()
    delay = 2
    for attempt in range(attempts):
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=data,
            headers={
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (hermes-agent, 1.0)"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:   # (subclass of URLError — must come first)
            # 429/503 mean "not processed"; a 500/502/504 may still have created the message.
            retryable = e.code in (429, 503) or (retry_ambiguous and 500 <= e.code < 600)
            if attempt == attempts - 1 or not retryable or _past_deadline():
                raise
            wait = delay
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", delay))
                except Exception:
                    pass
            print(f"Discord HTTP {e.code}, retrying in {min(wait, 10):.1f}s")
            time.sleep(min(wait, 10) + 0.25)
            delay *= 2
        except urllib.error.URLError as e:     # connect/send failed — nothing was created
            if attempt == attempts - 1 or _past_deadline():
                raise
            print(f"Discord connection error, retrying in {delay}s: {e}")
            time.sleep(delay)
            delay *= 2

def discord_post(content, nonce=None):
    """POST one plain-content message. Returns (ok, detail) — never raises. `nonce` +
    enforce_nonce makes Discord return the existing message instead of creating a duplicate
    if the same reminder is re-sent within a few minutes."""
    if not DISCORD_BOT_TOKEN:
        return False, "DISCORD_BOT_TOKEN not found in .env"
    if NO_PING:
        allowed = {"parse": []}
    else:
        allowed = {"users": [NOTIFY_USER_ID]} if NOTIFY_USER_ID else {"parse": []}
    payload = {"content": content[:2000], "allowed_mentions": allowed}
    if nonce:
        payload.update({"nonce": nonce[:25], "enforce_nonce": True})
    try:
        body = discord_send(DISCORD_CHANNEL_ID, payload, retry_ambiguous=bool(nonce))
        return True, str((body or {}).get("id"))
    except Exception as e:
        return False, str(e)[:300]

def discord_alert(title, msg):
    """Best-effort Discord error notification — never raises."""
    try:
        if not DISCORD_BOT_TOKEN:
            return
        data = json.dumps({"content": "",
                           "embeds": [{"title": title, "description": msg, "color": 0xD70F64}]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            data=data,
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                     "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (hermes-agent, 1.0)"},
            method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass

def quick_alert(title, msg):
    """Single bounded best-effort Discord post for the failure/crash paths — NO retry and a
    short timeout, so the sole failure signal always fits under the cron SIGKILL watchdog even
    when the failure happens late in a slow/degraded run (worth more than a retry that dies)."""
    if not DISCORD_BOT_TOKEN:
        return
    try:
        data = json.dumps({"content": "",
                           "embeds": [{"title": title, "description": msg, "color": 0xD70F64}]}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            data=data,
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                     "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (hermes-agent, 1.0)"},
            method="POST")
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass

def friendly_error(e):
    """Short, plain-English summary of an exception for the Discord crash alert (the full
    traceback still goes to stdout / the cron log)."""
    first = (str(e).splitlines() or [""])[0]
    low = first.lower()
    if "run deadline exceeded" in low:
        return "The Google Sheets calls were too slow overall this run, so it stopped early to stay under the cron time limit."
    if isinstance(e, subprocess.TimeoutExpired) or "timed out" in low or "timeout" in low:
        return "A Google Sheets request was too slow and timed out — likely a slow or unreachable network."
    if "429" in low or "rate" in low:
        return "Google or Discord rate-limited the requests (HTTP 429)."
    if "503" in low or "unavailable" in low:
        return "A Google API was temporarily unavailable (HTTP 503)."
    if any(k in low for k in ("credential", "token", "unauthor", "invalid_grant", "refresh")):
        return "Google sign-in failed — the OAuth token may need re-authorising (see the google-workspace skill)."
    return f"{type(e).__name__}: {first[:200]}" if first else type(e).__name__

def main(argv):
    global _run_deadline
    _run_deadline = time.monotonic() + RUN_DEADLINE_SECS

    dry_run = "--dry-run" in argv
    force = "--force" in argv
    sim_date = None
    if "--simulate-date" in argv:
        i = argv.index("--simulate-date")
        if i + 1 >= len(argv):
            raise ValueError("--simulate-date needs a YYYY-MM-DD value")
        sim_date = datetime.strptime(argv[i + 1], "%Y-%m-%d").date()
        if not force:
            dry_run = True   # a simulated date never posts or marks a cycle (the real fire
                             # for that cycle would then be skipped) unless --force says so

    today = sim_date or sgt_now().date()
    cycle = reminder_cycle(today)          # e.g. on 1 Oct 2026 → "Sep 2026"
    state = load_state()

    # Fire guard: days 1–5 only, once per cycle. --force bypasses both (tests only).
    if not force:
        if not (FIRE_DAY_MIN <= today.day <= FIRE_DAY_MAX):
            print(f"[no reminder] {today.isoformat()} — outside the {FIRE_DAY_MIN}–{FIRE_DAY_MAX} window.")
            return 0
        if state.get("last_fired_cycle") == cycle:
            print(f"[no reminder] {cycle} reminder already fired.")
            return 0

    total, count, ok = read_month_total(cycle)
    if not ok:
        discord_alert("⚠️ Food Payment Reminder Error",
                      f"Could not read sheet '{cycle}' — the reminder could not be composed.")
        print(f"FAILED: could not read '{cycle}' — will retry on the next run.")
        return 1

    if count == 0:
        # No expenses recorded for that month: nothing to pay. Mark fired so the window
        # goes quiet (a completed month with receipts always has its tab; a missing tab
        # or empty tab both mean genuinely nothing to settle).
        if not dry_run:
            state["last_fired_cycle"] = cycle
            save_state(state)
        print(f"[no reminder] No food expenses recorded for {cycle} — nothing to pay.")
        return 0

    # (A simulated date has no real sync run to check, so no caveat in previews.)
    text = build_message(cycle, total, None if sim_date else sync_covered_month_end(today))

    if dry_run:
        print(f"[dry-run] would post to {DISCORD_CHANNEL_ID}:\n{text}")
        return 0

    ok, detail = discord_post(text, nonce=f"fp-remind-{cycle.replace(' ', '')}")
    if not ok:
        # deliver:local pushes NOTHING to Discord on failure, so a failed send would be
        # invisible: self-notify (best-effort), dump the unsent text to stdout (saved to the
        # cron job-output file), and exit non-zero (turns the job red). Do NOT mark fired —
        # the days 2–5 window is what retries it.
        quick_alert("⚠️ Food Payment Reminder Failed",
                    f"Couldn't post the {cycle} payment reminder to Discord ({detail}). "
                    f"It will retry on the next run (days {FIRE_DAY_MIN}–{FIRE_DAY_MAX}).")
        print(f"⚠️ Food payment reminder for {cycle} failed to send: {detail}")
        print(f"UNSENT: {text}")
        return 1

    # Mark THIS cycle fired immediately after its own successful send.
    state["last_fired_cycle"] = cycle
    try:
        save_state(state)
    except Exception as e:
        # The reminder DID post; say so plainly (the generic crash alert would claim it may not
        # have) — a later day in the window may re-send it.
        quick_alert("⚠️ Food Payment Reminder — state not saved",
                    f"The {cycle} reminder WAS posted, but recording that failed ({type(e).__name__}: "
                    f"{str(e)[:150]}). It may be posted again on a later day of the window.")
        print(f"Reminder sent for {cycle} but state write failed: {e}")
        return 1
    print(f"Food payment reminder sent for {cycle}: S${total:,.2f} ({count} orders) — message id {detail}")
    return 0

if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as e:
        import traceback
        print(traceback.format_exc())   # full traceback → stdout / cron log for debugging
        quick_alert("❌ Food payment reminder couldn't finish",
                    f"{friendly_error(e)}\n\nThe reminder may not have been posted — it will "
                    f"retry on the next run while the days {FIRE_DAY_MIN}–{FIRE_DAY_MAX} window is open. "
                    f"Full details are in the cron log.")
        raise
    sys.exit(code)
