#!/usr/bin/env python3
"""
Food Weekly Expense Report (multi-provider: FoodPanda + Grab)
- Runs Sunday morning (cron `5 9 * * 0`) and reports the PREVIOUS complete Sun–Sat week (SGT) —
  every day of it has been through its 23:30 sync by then. (Until v1.7.0 it reported the
  current Mon–Sun week at Sunday 09:05, so Sunday's orders never reached any weekly report.)
- Reads the month tab the week ended in (plus the previous month's when the week spans a month
  boundary), treating a not-yet-created tab as 'no expenses'
- Sends a combined summary to Discord: list of expenses (with provider), weekly total, MTD total
"""

import json, os, sys, subprocess, urllib.request, urllib.error, time, socket, re
from datetime import datetime, timezone, timedelta

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SKILLS_DIR  = os.path.join(HERMES_HOME, "skills", "productivity", "google-workspace", "scripts")

SHEET_ID           = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"
DISCORD_CHANNEL_ID  = "1499972245437550602"
SGT                 = timezone(timedelta(hours=8))

# Timeout budget — the cron scheduler SIGKILLs a no_agent script at the global
# `cron.script_timeout_seconds` (600s here) with no Discord alert, so bound every call and cap
# cumulative retries. Discord posts aren't deadline-gated. (Mirror of foodpanda_sync.py.)
CALL_TIMEOUT      = 20
RUN_DEADLINE_SECS = 300   # RUN_DEADLINE + CALL_TIMEOUT + the Discord posts (≤ ~51s each) < 600s
socket.setdefaulttimeout(CALL_TIMEOUT)   # bounds in-process googleapiclient/httplib2 sockets
_run_deadline = None

# Locale-independent month abbreviations — must match foodpanda_sync.py's tab naming.
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Provider names we recognise in the sheet's Provider column (col F). A value outside this
# set means an un-migrated tab (col F may still hold an old MTD value) — treat as unlabelled.
KNOWN_PROVIDERS = {"FoodPanda", "Grab"}

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
    """Run google_api.py and return parsed JSON. returncode check is inside the retried call
    so transient API failures (429/503) are retried; the subprocess is bounded by CALL_TIMEOUT
    so a hung google_api.py raises TimeoutExpired instead of blocking to the cron SIGKILL."""
    # sys.executable (the gateway venv under cron), never a bare "python" that resolves via PATH.
    cmd = [sys.executable, f"{SKILLS_DIR}/google_api.py"] + args
    def call():
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT)
        if result.returncode != 0:
            raise RuntimeError(f"gapi failed: {result.stderr}")
        return json.loads(result.stdout)
    return retry(call)

def san(s):
    """Neutralize Discord markdown in an interpolated untrusted field (merchant name from the
    provider's receipt) so a crafted name can't break formatting or inject a clickable link."""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return (s.replace("`", "'").replace("@", "@​")
             .replace("](", "]​(").replace("://", ":​//"))

def sgt_now():
    return datetime.now(SGT)

def month_label(d):
    """'May 2026' for a date/datetime (locale-independent)."""
    return f"{MONTHS[d.month - 1]} {d.year}"

def day_label(d):
    """'May 04' for a date (locale-independent)."""
    return f"{MONTHS[d.month - 1]} {d.day:02d}"

def week_range(today=None):
    """(start_sunday, end_saturday) of the most recent COMPLETE Sun–Sat week in SGT — the week
    before the one `today` is in. Run on Sunday 09:05 that is the week that ended last night,
    whose days the 23:30 syncs up to Saturday's have already written. Independent of the weekday
    it runs on (a manual mid-week run also gets the last complete Sun–Sat)."""
    today = today or sgt_now().date()
    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)   # weekday(): Mon=0 … Sun=6
    start = this_sunday - timedelta(days=7)
    return start, start + timedelta(days=6)

def parse_sheet_date(date_str):
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()

def parse_money(x):
    """Lenient numeric parse tolerant of currency formatting ('$30.32', 'S$ 1,037.17')."""
    s = re.sub(r"[^\d.\-]", "", str(x))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None

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

def discord_embed(channel_id, title, description, color=0xD70F64):
    """Post a Discord embed (description capped to the 4096-char embed limit)."""
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN not set, skipping Discord notification")
        return
    try:
        return discord_send(channel_id, {
            "content": "",
            "embeds": [{"title": title, "description": description[:4000], "color": color}]
        })
    except Exception as e:
        print(f"Discord notification failed: {e}")
        raise

def discord_alert(title, msg):
    """Best-effort Discord error notification — never raises."""
    try:
        discord_embed(DISCORD_CHANNEL_ID, title, msg)
    except Exception:
        pass

def quick_alert(title, msg):
    """Single bounded best-effort Discord post for the crash handler — no retry, short timeout,
    so the sole failure signal fits under the cron SIGKILL watchdog even late in a slow run."""
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

def _is_missing_sheet_error(e):
    """A read against a non-existent tab returns HTTP 400 'Unable to parse range' from the
    Sheets API. Treat that as 'sheet not created yet' (benign), not a hard error."""
    return "Unable to parse range" in str(e)

def read_month_rows(month_name):
    """Read (date, merchant, amount, provider) rows from a month tab, skipping the header.
    Returns (rows, ok). A missing tab → ([], True): benign 'no expenses yet'. A genuine read
    failure → ([], False) so the caller can surface it."""
    try:
        data = run_gapi(["sheets", "get", SHEET_ID, f"'{month_name}'!A:F"])
    except Exception as e:
        if _is_missing_sheet_error(e):
            return [], True
        print(f"Could not read sheet '{month_name}': {e}")
        return [], False
    rows = []
    for row in data[1:]:  # skip header
        if len(row) >= 3 and row[0] and row[1] and row[2]:
            amount = parse_money(row[2])
            if amount is None:
                continue
            provider = row[5].strip() if len(row) >= 6 and row[5] else ""
            if provider not in KNOWN_PROVIDERS:
                provider = ""  # tolerate un-migrated tabs (col F may hold an old MTD value)
            rows.append((row[0].strip(), row[1].strip(), amount, provider))
    return rows, True

def main():
    global _run_deadline
    _run_deadline = time.monotonic() + RUN_DEADLINE_SECS
    start, end = week_range()                   # Sun … Sat
    month_name = month_label(end)               # the month the week ended in — the MTD scope
    prev_month_name = month_label(start)        # the week's Sunday may fall in the prior month

    # Read the week's (end) month (MTD + week rows). A missing tab is benign (no expenses yet).
    cur_rows, ok = read_month_rows(month_name)
    if not ok:
        discord_alert("⚠️ Food Weekly Report Error",
                      f"Could not read sheet '{month_name}'.")
        return 1   # non-zero → the job shows red (the alert above is the user-facing signal)

    # If the Sun–Sat week spans a month boundary, the early-week rows live in the previous
    # month's tab — read it too so the weekly total isn't silently undercounted.
    prev_rows = []
    if prev_month_name != month_name:
        prev_rows, ok = read_month_rows(prev_month_name)
        if not ok:
            discord_alert("⚠️ Food Weekly Report Error",
                          f"Could not read sheet '{prev_month_name}'.")
            return 1

    # MTD = the week's end month, from the 1st THROUGH that Saturday — summed from the rows (the
    # tab's G2 =SUM would also count days after it once they're synced, e.g. on a re-run).
    # A row whose date doesn't parse still lives in this month's tab, so it counts (as in G2).
    mtd = 0.0
    for date_str, _m, amount, _p in cur_rows:
        try:
            if parse_sheet_date(date_str) > end:
                continue
        except ValueError:
            pass
        mtd += amount
    mtd = round(mtd, 2)
    mtd_display = f"S${mtd:.2f}"

    # Filter to the reported (previous) Sun–Sat week, drawing from both months when it spans one.
    week_rows = []
    unparseable_rows = 0
    for date_str, merchant, amount, provider in cur_rows + prev_rows:
        try:
            row_date = parse_sheet_date(date_str)
            if start <= row_date <= end:
                week_rows.append((date_str, merchant, amount, provider))
        except Exception:
            unparseable_rows += 1

    title = (f"🍔 Food Weekly Report — {month_name} "
             f"({day_label(start)} – {day_label(end)})")

    if not week_rows:
        month_count = len(cur_rows)   # the MTD month only (prev-month rows would contradict its MTD)
        if unparseable_rows > 0:
            desc = (f"*No expenses that week ({day_label(start)} – {day_label(end)}), "
                    f"but found {month_count} row(s) — {unparseable_rows} row(s) had unparseable dates.*")
        elif month_count > 0:
            desc = (f"*No expenses that week ({day_label(start)} – {day_label(end)}), "
                    f"but {month_count} expense(s) recorded earlier in {month_name}.*")
        else:
            desc = f"*No expenses recorded for the week ({day_label(start)} – {day_label(end)}).*"
        desc += f"\n**MTD Total: {mtd_display}**"
        weekly_total = 0.0
    else:
        lines = []
        weekly_total = 0.0
        for date_str, merchant, amount, provider in sorted(week_rows):
            label = f" ({provider})" if provider else ""
            lines.append(f"{date_str} · {san(merchant)}{label} · S${amount:.2f}")
            weekly_total += amount
        desc = "\n".join(lines)
        desc += f"\n\n*Weekly Total: S${weekly_total:.2f}*"
        desc += f"\n**MTD Total: {mtd_display}**"

    discord_embed(DISCORD_CHANNEL_ID, title, desc)
    print(f"Weekly report sent: {len(week_rows)} expenses, weekly total S${weekly_total:.2f}, MTD {mtd_display}")
    return 0

if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        import traceback
        print(traceback.format_exc())   # full traceback → stdout / cron log for debugging
        quick_alert("❌ FoodPanda weekly report couldn't finish",
                    f"{friendly_error(e)}\n\nThe weekly report may not have been posted. Full details are in the cron log.")
        raise
    sys.exit(code or 0)
