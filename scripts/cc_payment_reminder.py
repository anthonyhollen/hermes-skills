#!/usr/bin/env python3
"""
No-agent cron runner for the cc-bill-review payment reminder.

The cron job runs this script directly (Mode: no-agent, Deliver: local) and the
script posts to Discord itself via the bot API. No LLM is in the loop, so the
reminder fires deterministically.

Schedule: daily at 09:00 (`0 9 * * *`). The skill's reminder.py decides per
card whether today is a reminder day (3 days before, the due date, or a late /
overdue catch-up for one that didn't go out) and is idempotent per cycle, so
most days this script sends nothing and exits silently.

`deliver: local` means the scheduler pushes NOTHING on failure, so every
failure path here self-alerts (best effort, @-mentioning the account holder),
prints the unsent reminder text to stdout (saved in the job output) and exits
non-zero (turns the job red — which the skill's cron-health check reports).

    --dry-run   print what would be sent (and whether the token is readable);
                post nothing, mark nothing
"""
import json
import os
import site
import sys
import traceback
import urllib.request

# Ensure user site-packages are importable (cron runner may strip them per PEP 668).
_USER_SITE = os.path.expanduser("~/.local/lib/python3.11/site-packages")
if _USER_SITE not in sys.path:
    sys.path.insert(0, _USER_SITE)

SKILL_SCRIPTS = "/home/amh/.hermes/skills/productivity/cc-bill-review/scripts"
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
CHANNEL_ID = os.environ.get("CC_REVIEW_CHANNEL", "1499972245437550602")
# The one user allowed to be @-mentioned by a reminder (must match reminder.py's
# NOTIFY_USER_ID). Empty = allow no mentions. We allow-list this exact id rather
# than {"parse": ["users"]} so a stray <@id> in content can never ping anyone else.
NOTIFY_USER_ID = os.environ.get("CC_REVIEW_NOTIFY_USER", "470611394698412052")
ALLOWED = {"users": [NOTIFY_USER_ID]} if NOTIFY_USER_ID else {"parse": []}
MENTION = f"<@{NOTIFY_USER_ID}> " if NOTIFY_USER_ID else ""


def _bare_alert(text: str) -> None:
    """Last-resort alert when the skill itself can't be imported: stdlib only,
    single attempt, never raises."""
    try:
        token = None   # same parsing rules as scripts/discord_post.discord_token()
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.lstrip("\ufeff").strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, v = line.partition("=")
                if sep and key.strip() == "DISCORD_BOT_TOKEN":
                    token = v.strip().strip('"').strip("'") or None
        if not token:
            return
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
            data=json.dumps({"content": text[:1900], "allowed_mentions": ALLOWED}).encode(),
            method="POST",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception:  # noqa: BLE001 — best effort
        pass


try:
    sys.path.insert(0, os.path.dirname(SKILL_SCRIPTS))  # .../cc-bill-review
    from scripts.reminder import due_messages, mark_fired  # noqa: E402
    from scripts.discord_post import discord_token, make_nonce, send_discord  # noqa: E402
except Exception as _exc:  # noqa: BLE001 — a broken skill must not fail silently
    _msg = (f"{MENTION}⚠️ CC payment-reminder cron is BROKEN — the skill failed to "
            f"import ({type(_exc).__name__}: {_exc}). Payment reminders will NOT be "
            f"sent until this is fixed.")
    if "--dry-run" not in sys.argv:
        _bare_alert(_msg)
    print(_msg)
    traceback.print_exc()
    sys.exit(1)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    items = due_messages()
    if dry_run:
        print(f"[dry-run] bot token readable: {'yes' if discord_token() else 'NO'}")
        if not items:
            print("[dry-run] nothing due today")
        for it in items:
            print(f"[dry-run] would post to {CHANNEL_ID}:\n{it['text']}")
        return 0
    if not items:
        return 0  # silent on no-op days

    failed = []
    for it in items:
        try:
            nonce = make_nonce("cc-remind", it["card"], it["kind"], it["year_month"])
            ok, detail, mid, _may = send_discord(CHANNEL_ID, it["text"], nonce=nonce,
                                                 allowed_mentions=ALLOWED)
        except Exception as exc:  # noqa: BLE001 — never let one item kill the loop
            ok, detail, mid = False, f"{type(exc).__name__}: {exc}", None
        if ok:
            # Mark THIS item fired immediately after its own successful send, so
            # a later item's failure can't un-record it (both cards routinely
            # share a due date).
            try:
                mark_fired([it])
                print(f"sent {it['card']}/{it['kind']} (message id {mid})")
            except Exception as exc:  # noqa: BLE001
                # Delivered but not recorded: the nonce dedupes a re-send within
                # minutes only, so say so loudly rather than abort the loop.
                print(f"sent {it['card']}/{it['kind']} (message id {mid}) but FAILED to "
                      f"record it ({type(exc).__name__}: {exc}) — it may repeat tomorrow",
                      file=sys.stderr)
                failed.append((it, f"sent but not recorded: {exc}", True))
        else:
            print(f"FAILED {it['card']}/{it['kind']}: {detail}", file=sys.stderr)
            failed.append((it, detail, False))

    if not failed:
        return 0
    unsent = [(it, d) for it, d, sent in failed if not sent]
    summary = ("⚠️ CC payment reminder problem: "
               + ", ".join(f"{it['card']}/{it['kind']} ({d})" for it, d, _s in failed))
    alert = summary
    if unsent:
        alert += "\nThe reminder(s) that could not be sent:\n" + "\n".join(
            it["text"].replace(MENTION, "", 1) for it, _d in unsent)
    try:
        send_discord(CHANNEL_ID, MENTION + alert[:1800], allowed_mentions=ALLOWED)
    except Exception:  # noqa: BLE001 — best effort
        pass
    print(summary)
    for it, _d in unsent:
        print(f"UNSENT {it['card']}/{it['kind']}: {it['text']}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _exc:  # noqa: BLE001 — e.g. the DB is unreadable
        _msg = (f"{MENTION}⚠️ CC payment-reminder cron crashed "
                f"({type(_exc).__name__}: {_exc}). Today's reminders may not have been sent.")
        if "--dry-run" not in sys.argv:
            _bare_alert(_msg)
        print(_msg)
        traceback.print_exc()
        sys.exit(1)
