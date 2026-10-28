#!/usr/bin/env python3
"""
Gmail Triage Report Script — posts to Discord channel 1499972245437550602
Run at 7:10am, 1:10pm, 7:10pm SGT daily.
Reports on emails triaged since last report.

v2: Uses Gmail API directly (no subprocess spawning) for ~10x faster execution.
v3: triaged_at watermark. 2026-09-28: 2000-unit Discord cap, send retry, exit 1 on
failure (deliver: local jobs get no scheduler alert), atomic/guarded file I/O.
"""

import json, os, sys, time, socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

EPOCH = "1970/01/01 00:00:00"  # sortable sentinel: older than any real stamp

# ─── Config ───────────────────────────────────────────────────────────────
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
TOKEN_PATH = Path(f"{HERMES_HOME}/google_token.json")
STATE_FILE = Path(f"{HERMES_HOME}/scripts/gmail_triage_state.json")
REPORT_LOG_FILE = Path(f"{HERMES_HOME}/scripts/gmail_triage_report_log.json")
DISCORD_CHANNEL = "1499972245437550602"
DISCORD_MAX = 2000   # Discord bot message content limit, in UTF-16 code units

SGT = timezone(timedelta(hours=8))

# ─── Gmail API (in-process, no subprocess) ────────────────────────────────

def _build_gmail_service():
    """Build Gmail API service using stored OAuth2 credentials."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not TOKEN_PATH.exists():
        print("ERROR: No Google token found at", TOKEN_PATH, file=sys.stderr)
        sys.exit(1)

    creds_data = json.loads(TOKEN_PATH.read_text())
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=creds_data.get("scopes"),
    )
    # Bound every in-process Google HTTP call (build()/execute() take no timeout).
    socket.setdefaulttimeout(30)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist refreshed token — atomically: google_api.py (triage) reads the
        # same file, and a torn write would break every Gmail call.
        creds_data["token"] = creds.token
        _write_atomic(TOKEN_PATH, json.dumps(creds_data))

    return build("gmail", "v1", credentials=creds)

# Singleton service — built once, reused across all fetches
_service = None

def gmail_service():
    global _service
    if _service is None:
        _service = _build_gmail_service()
    return _service

def get_labels_map() -> dict:
    """Fetch all Gmail labels → {id: name} mapping."""
    svc = gmail_service()
    results = svc.users().labels().list(userId="me").execute()
    return {l["id"]: l["name"] for l in results.get("labels", [])}

def get_messages_metadata(msg_ids: list[str]) -> list[dict]:
    """Fetch metadata for a batch of message IDs in-process.

    Uses format='metadata' with only From/Subject headers — much lighter
    than format='full'. Returns list of {id, labels, from, subject, snippet}.
    """
    svc = gmail_service()
    results = []
    for msg_id in msg_ids:
        try:
            msg = svc.users().messages().get(
                userId="me", id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            results.append({
                "id": msg["id"],
                "labels": msg.get("labelIds", []),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": msg.get("snippet", ""),
            })
        except Exception as e:
            print(f"  [warn] Could not fetch {msg_id}: {e}", file=sys.stderr)
    return results

# ─── Helpers ──────────────────────────────────────────────────────────────

def _write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def discord_len(s: str) -> int:
    """Length as Discord counts it: UTF-16 code units (astral emoji = 2)."""
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)

def extract_email(addr: str) -> str:
    """Same parsing as gmail_triage.extract_email (kept in sync)."""
    from email.utils import parseaddr
    addr = addr or ""
    if "<" in addr:
        return addr.split("<")[-1].split(">")[0].strip().lower()
    parsed = parseaddr(addr)[1]
    return (parsed or addr).strip().lower()

def sanitize_discord(text: str) -> str:
    """Neutralize untrusted email content (subjects) before posting to Discord:
    strip newlines, escape backticks, and defang @mentions so a crafted subject
    cannot ping the channel or inject/break digest formatting. Paired with
    allowed_mentions={"parse": []} on the send."""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("`", "'")
    text = text.replace("](", "]\u200b(")   # break [label](url) masked links
    text = text.replace("://", ":\u200b//")  # break bare http(s):// autolinks
    text = text.replace("@", "@\u200b")
    return text.strip()

def load_state() -> dict:
    """Triage writes the state atomically (temp + os.replace), so a reader sees a
    complete old-or-new file. An unreadable file is a hard failure: exit 1 so the
    run shows as failed rather than posting an empty digest."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (ValueError, OSError) as e:
            print(f"ERROR: triage state unreadable ({e}): {STATE_FILE}", file=sys.stderr)
            sys.exit(1)
    if REPORT_LOG_FILE.exists():
        # A watermark exists, so triage has run before: a missing state file is a
        # breakage, not "no new mail" (it would otherwise post empty digests forever).
        print(f"ERROR: triage state missing: {STATE_FILE}", file=sys.stderr)
        sys.exit(1)
    return {"processed_ids": [], "last_run": None}

def load_report_log() -> dict:
    """Stores the last report timestamp (the WATERMARK) and which IDs were included.

    If the log is unreadable, do NOT fall back to EPOCH (that would re-report every
    stored email). Fall back to the file's mtime — when the last successful digest
    wrote it — so the next digest covers roughly "since the last digest"."""
    if REPORT_LOG_FILE.exists():
        try:
            with open(REPORT_LOG_FILE) as f:
                log = json.load(f)
            if isinstance(log, dict):
                return log
            raise ValueError("not a JSON object")
        except (ValueError, OSError) as e:
            try:
                ts = datetime.fromtimestamp(REPORT_LOG_FILE.stat().st_mtime, SGT)
                wm = ts.strftime("%Y/%m/%d %H:%M:%S")
            except OSError:
                wm = datetime.now(SGT).strftime("%Y/%m/%d %H:%M:%S")
            print(f"  [warn] report log unreadable ({e}); using its mtime {wm} as the watermark.",
                  file=sys.stderr)
            return {"last_report_ts": wm, "last_reported_ids": []}
    return {"last_report_ts": None, "last_reported_ids": []}

def save_report_log(log: dict):
    _write_atomic(REPORT_LOG_FILE, json.dumps(log, indent=2, default=str))

def load_env() -> dict:
    env_path = Path(f"{HERMES_HOME}/.env")
    vars_ = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vars_[k.strip()] = v.strip().strip("'\"").strip()
    return vars_

# ─── Report builder ───────────────────────────────────────────────────────

def heal_metadata(msg_ids: list) -> dict:
    """Last-resort, READ-ONLY fetch of from/subject for selected entries that
    are missing them. Triage normally persists these at triage time, so this
    should almost never run — when it does, it is logged loudly so a gap is
    visible on the very next digest rather than discovered weeks later. Never
    writes state (the triage script is the only writer of the state file)."""
    out = {}
    if not msg_ids:
        return out
    try:
        for m in get_messages_metadata(msg_ids):
            out[m["id"]] = {"from": m.get("from", ""), "subject": m.get("subject", "")}
        # OAuth token-refresh race can strip payload.headers on large batches;
        # rebuild the service once and retry the empties.
        empty = [i for i in msg_ids if not out.get(i, {}).get("from")]
        if empty:
            global _service
            _service = None
            for m in get_messages_metadata(empty):
                if m.get("from"):
                    out[m["id"]] = {"from": m.get("from", ""), "subject": m.get("subject", "")}
    except Exception as e:
        print(f"  [heal] FAILED: {e} — affected rows render a placeholder", file=sys.stderr)
    return out


def _render_section(emoji: str, title: str, msgs: list, empty_text: str) -> str:
    if not msgs:
        return f"### {emoji} {title} (0)\n{empty_text}"
    lines = [f"### {emoji} {title} ({len(msgs)})"]
    for i, m in enumerate(msgs, 1):
        sender = sanitize_discord(extract_email(m["from"])) or "(unknown sender)"
        subject = sanitize_discord(m["subject"]) or "(no subject)"
        lines.append(f"{i}. `{sender}`: {subject}")
    return "\n".join(lines)


def build_report(report_slot_label: str, state: dict, report_log: dict):
    """Build a digest for emails triaged since the last report.

    Selection is a TIMESTAMP WATERMARK, not a set-difference of two capped
    lists: an email is "new" iff its triaged_at is strictly after the last
    report's watermark. Once the watermark advances past an email it can never
    be selected again — and an email that ages out of the 5000-cap state is
    simply gone. There is no second list to drift out of sync, which is what
    used to re-report old emails (state cap 5000 vs report-log cap 500).

    Returns (report_text, selected_entries, new_watermark, total).
    """
    last_report_ts = report_log.get("last_report_ts") or EPOCH

    entries = [e for e in state.get("processed_ids", []) if isinstance(e, dict)]
    # Fixed-width "%Y/%m/%d %H:%M:%S" stamps sort lexically == chronologically,
    # so a plain string compare is correct (and locale/DST-proof; SGT is fixed).
    # Tie-break AT the watermark: triage persists after every email, so a digest
    # can read a run half-way; an email filed later in the same second carries
    # the watermark's own stamp. It is still new iff it isn't one of the ids the
    # digest(s) already reported at exactly that stamp. Same single watermark
    # mechanism (invariant #8) — this only resolves equal stamps.
    # A log written before this key existed falls back to last_reported_ids (the
    # previous digest's ids — a superset of those at its watermark); with
    # neither key, ties are excluded (the original strict ">" behaviour).
    if "reported_at_watermark" in report_log:
        at_wm, tie_ok = set(report_log.get("reported_at_watermark") or []), True
    elif report_log.get("last_reported_ids"):
        at_wm, tie_ok = set(report_log["last_reported_ids"]), True
    else:
        at_wm, tie_ok = set(), False
    new_entries = [e for e in entries
                   if (e.get("triaged_at") or "") > last_report_ts
                   or (tie_ok and last_report_ts != EPOCH
                       and (e.get("triaged_at") or "") == last_report_ts
                       and e.get("id") not in at_wm)]

    header = f"## 📬 Gmail Triage Digest — {report_slot_label}\n"
    if not new_entries:
        return header + "\nNo new emails since last digest.", [], last_report_ts, 0

    # Heal any selected entry missing sender/subject (rare header-strip race at
    # triage time). Read-only; loud. Merge healed values onto a working copy.
    missing = [e["id"] for e in new_entries if not e.get("from") or not e.get("subject")]
    if missing:
        print(f"  [heal] {len(missing)} selected entr(ies) missing from/subject — "
              f"fetching from Gmail API (triage should have stored these)...", file=sys.stderr)
    healed = heal_metadata(missing)

    action_msgs, readlater_msgs, autofiled_msgs, unsure_count = [], [], [], 0
    for e in new_entries:
        h = healed.get(e["id"], {})
        row = {"id": e["id"],
               "from": e.get("from") or h.get("from", ""),
               "subject": e.get("subject") or h.get("subject", "")}
        cat = e.get("category", "")
        if cat == "Action":
            action_msgs.append(row)
        elif cat == "ReadLater":
            readlater_msgs.append(row)
        elif cat == "Auto-filed":
            autofiled_msgs.append(row)
        elif cat == "VIP":
            # VIP is a flag, never a standalone category, but a re-encountered
            # already-labeled VIP email can carry it — surface under Action.
            action_msgs.append(row)
        elif cat == "Unsure":
            unsure_count += 1   # asked in Discord separately; not a digest row
        # empty category (legacy/orphan) is intentionally not rendered
        if cat not in ("Unsure", "") and not row["from"] and not row["subject"]:
            print(f"  [warn] blank row for {e['id']} (cat={cat!r}) after heal — "
                  f"rendering placeholder.", file=sys.stderr)

    # Footer total now equals the sum of the rendered sections (Action +
    # Read Later + Auto-filed). Unsure is surfaced as its own count so the math
    # always reconciles (old code set total = len(new) which silently included
    # Unsure/empty and never matched the section counts).
    total = len(action_msgs) + len(readlater_msgs) + len(autofiled_msgs)

    action_block = _render_section("🔴", "Action — Needs Attention Today",
                                   action_msgs, "No urgent emails.")
    readlater_block = _render_section("🟡", "Read Later", readlater_msgs,
                                      "No important emails.")
    autofiled_block = _render_section("⚪", "Auto-filed", autofiled_msgs,
                                      "No junk emails.")

    total_line = (f"Total triaged since last digest: {total} | "
                  f"🔴 Action: {len(action_msgs)} | "
                  f"🟡 Read Later: {len(readlater_msgs)} | "
                  f"⚪ Auto-filed: {len(autofiled_msgs)}")
    if unsure_count:
        total_line += f" | ❓ Unsure: {unsure_count}"

    report = (header + "\n" + action_block + "\n\n" + readlater_block
              + "\n\n" + autofiled_block + "\n\n" + total_line)

    # Discord's bot limit is 2000 UTF-16 units (it was checked against 4000, so a
    # 2001–4000 digest was rejected, the watermark never advanced, and every later
    # digest was bigger still). Over the limit → compact summary, itself capped.
    if discord_len(report) > DISCORD_MAX:
        summary_lines = [header, "", total_line, ""]
        if action_msgs:
            summary_lines.append(f"### 🔴 Action — Needs Attention Today ({len(action_msgs)})")
            for i, m in enumerate(action_msgs[:5], 1):
                summary_lines.append(f"{i}. `{sanitize_discord(extract_email(m['from']))}`: {sanitize_discord(m['subject'])}")
            if len(action_msgs) > 5:
                summary_lines.append(f"…and {len(action_msgs) - 5} more Action emails")
        if readlater_msgs:
            summary_lines.append("")
            summary_lines.append(f"### 🟡 Read Later ({len(readlater_msgs)})")
            for i, m in enumerate(readlater_msgs[:3], 1):
                summary_lines.append(f"{i}. `{sanitize_discord(extract_email(m['from']))}`: {sanitize_discord(m['subject'])}")
            if len(readlater_msgs) > 3:
                summary_lines.append(f"…and {len(readlater_msgs) - 3} more ReadLater emails")
        if autofiled_msgs:
            summary_lines.append("")
            summary_lines.append(f"### ⚪ Auto-filed ({len(autofiled_msgs)})")
            senders = {}
            for m in autofiled_msgs:
                s = sanitize_discord(extract_email(m["from"]))
                senders[s] = senders.get(s, 0) + 1
            for sender, count in sorted(senders.items(), key=lambda x: -x[1])[:5]:
                suffix = f" ×{count}" if count > 1 else ""
                summary_lines.append(f"• `{sender}`{suffix}")
            if len(senders) > 5:
                summary_lines.append(f"…and {len(senders) - 5} more senders")
        report = "\n".join(summary_lines)
        # Long subjects can still push the summary over — drop lines from the
        # bottom (keeping header + footer totals) until it fits.
        while discord_len(report) > DISCORD_MAX and len(summary_lines) > 4:
            summary_lines.pop()
            report = "\n".join(summary_lines + ["…(truncated — see Gmail labels)"])
        if discord_len(report) > DISCORD_MAX:
            report = report[:DISCORD_MAX // 2]

    # New watermark = the max stamp actually included, so a triage run racing
    # this digest (a later stamp not yet read) is picked up next time, never
    # skipped. Strictly > selection means this batch is never re-shown.
    new_watermark = max(e.get("triaged_at") or EPOCH for e in new_entries)
    return report, new_entries, new_watermark, total

def advance_watermark(report_log: dict, new_entries: list, new_watermark: str) -> dict:
    """The post-send watermark update (the only writer of the selection state).
    last_report_ts = newest stamp shown; reported_at_watermark = ids already shown
    AT that stamp (the tie-break build_report honors), accumulated while the
    watermark stays on the same second."""
    prev_ts = report_log.get("last_report_ts") or EPOCH
    wm = max(prev_ts, new_watermark)
    at_wm = [e["id"] for e in new_entries if e.get("triaged_at") == wm]
    out = dict(report_log)
    out["last_report_ts"] = wm
    if wm == prev_ts:
        # Same second as before: carry the prior ties forward. They come from the
        # key, else (legacy log) from last_reported_ids — the SAME fallback
        # build_report uses. With neither, the set of ids already shown at wm is
        # unknown, so stay strict and do not start tracking (gate lap 2: seeding
        # from [] re-reported every tie on the next digest).
        if "reported_at_watermark" in report_log:
            prior = report_log.get("reported_at_watermark") or []
        elif report_log.get("last_reported_ids"):
            prior = report_log["last_reported_ids"]
        else:
            prior = None
        if prior is None:
            out.pop("reported_at_watermark", None)
        else:
            out["reported_at_watermark"] = list(dict.fromkeys(list(prior) + at_wm))[-1000:]
    else:
        out["reported_at_watermark"] = at_wm[-1000:]   # wm advanced: fully known
    # Kept for debugging/inspection only — NOT used for selection.
    out["last_reported_ids"] = [e["id"] for e in new_entries][-500:]
    return out

# ─── Discord delivery ─────────────────────────────────────────────────────

def send_discord(message: str):
    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not found in .env")
        return False

    import requests
    # Retry ONCE, only where the message cannot already exist: HTTP 429 (honor
    # retry_after) and a connection error before the request was sent. No retry on
    # a read timeout / 5xx — Discord may have created it, a re-POST would duplicate.
    for attempt in range(2):
        try:
            resp = requests.post(
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages",
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "DiscordBot (hermes-agent/1.0)",
                },
                json={"content": message, "allowed_mentions": {"parse": []}},
                timeout=15
            )
        except requests.exceptions.ConnectionError as e:
            # Retry only if the request provably never reached Discord (connect
            # timeout / DNS / refused); a reset after sending could duplicate.
            never_sent = isinstance(e, requests.exceptions.ConnectTimeout) or any(
                k in repr(e) for k in ("NewConnectionError", "NameResolutionError",
                                       "Failed to resolve", "Connection refused"))
            if attempt == 0 and never_sent:
                time.sleep(2)
                continue
            print(f"  [discord] Failed: {e}")
            return False
        except Exception as e:
            print(f"  [discord] Failed: {e}")
            return False
        if resp.status_code == 429 and attempt == 0:
            try:
                wait = float(resp.json().get("retry_after", 1))
            except Exception:
                wait = 1.0
            time.sleep(min(wait, 10) + 0.25)
            continue
        if 200 <= resp.status_code < 300:
            try:
                msg_id = resp.json().get("id", "unknown")
            except Exception:
                msg_id = "unknown"
            print(f"  [discord] Report sent successfully (msg_id={msg_id}).")
            return True
        print(f"  [discord] HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    return False

# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    now_sgt = datetime.now(SGT)

    # Determine slot label
    hour = now_sgt.hour
    if hour == 7:
        slot_label = "7:10 AM SGT"
    elif hour == 13:
        slot_label = "1:10 PM SGT"
    elif hour == 19:
        slot_label = "7:10 PM SGT"
    else:
        slot_label = now_sgt.strftime("%-I:%M %p SGT")
        print(f"[{now_sgt.strftime('%Y-%m-%d %H:%M:%S SGT')}] Report script called at unexpected hour ({hour})")

    print(f"[{now_sgt.strftime('%Y-%m-%d %H:%M:%S SGT')}] Generating {slot_label} report...")

    state = load_state()
    report_log = load_report_log()
    report, new_entries, new_watermark, total = build_report(slot_label, state, report_log)

    print(f"  Report preview:\n{report[:500]}")
    print(f"  Total triaged: {total} emails")

    if total == 0 and not new_entries:
        print("  No new emails to report since last digest.")

    success = send_discord(report)

    if success:
        # Advance the watermark to the newest stamp we just reported. Because
        # selection is "triaged_at > watermark", nothing in this batch can be
        # re-selected. No second capped list to drift — the old re-report bug
        # (state cap 5000 vs report-log cap 500) is structurally impossible now.
        save_report_log(advance_watermark(load_report_log(), new_entries, new_watermark))
    else:
        # deliver: local → the scheduler pushes NO alert for this job; the full
        # report goes into the saved run output and a non-zero exit marks the run
        # failed (last_status=error). The watermark did not move, so the next
        # digest re-includes these emails.
        print("  [discord] DELIVERY FAILED — full digest follows; it will be re-included next slot.")
        print(report)
        print(f"[{datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S SGT')}] Report FAILED.")
        sys.exit(1)

    print(f"[{datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S SGT')}] Report complete.")

if __name__ == "__main__":
    main()
