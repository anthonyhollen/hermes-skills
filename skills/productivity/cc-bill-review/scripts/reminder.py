#!/usr/bin/env python3
"""
Payment reminder logic for cc-bill-review.

Fires REMINDER_DAYS_BEFORE days before each card's due date, plus on the due
date itself — and CATCHES UP: a 3-day reminder that didn't go out on due−3
(Pi down, Discord error) goes out late on due−2 / due−1, and a due-date
reminder that didn't go out goes out as "OVERDUE" for up to OVERDUE_DAYS days
after the due date. Each still fires at most once per cycle. Per-card
idempotency lives in `config`:
    reminded_3day_{card}    = year_month of last 3-day reminder fired
    reminders_fired_{card}  = year_month of last due-date reminder fired

DELIVERY is handled by the no-agent cron wrapper `~/.hermes/scripts/
cc_payment_reminder.py`, which calls `due_messages()` / `mark_fired()` here and
posts to Discord directly. The cron now fires DAILY (`0 9 * * *`) so it can
never miss a due day — the old `0 9 20-22 * *` window structurally skipped
every 23rd/24th due date. There is no longer any cron pause/resume: a daily
no-op run is free and removes a whole class of "reminders silently stopped"
failures.

This module is import-safe (no side effects at import) and also runnable as a
CLI for manual checks:
    reminder.py                # print what would fire today (changes nothing)
    reminder.py --mark-fired   # ...and record it as sent WITHOUT sending it
"""
import os
import sys
from datetime import date

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from scripts.parse_ocbc import get_config, get_db, set_config

REMINDER_DAYS_BEFORE = 3
FIRE_ON_DUE_DATE = True
OVERDUE_DAYS = 2   # a missed due-date reminder still goes out up to this many days late

# Discord user id to @-mention on each payment reminder so it pings (env-
# overridable; empty string = no mention). The cron wrapper allow-lists exactly
# this id in allowed_mentions, so only this user is ever pinged.
NOTIFY_USER_ID = os.environ.get("CC_REVIEW_NOTIFY_USER", "470611394698412052")
_MENTION = f"<@{NOTIFY_USER_ID}> " if NOTIFY_USER_ID else ""


def _parse_due(due_date_str: str) -> date:
    """OCBC due_date format is DD-MM-YYYY."""
    d, m, y = due_date_str.split("-")
    return date(int(y), int(m), int(d))


def _latest_per_card(cur):
    cur.execute(
        """
        SELECT s.card_suffix, s.year_month, s.due_date, s.total_due
        FROM statements s
        JOIN (
            SELECT card_suffix, MAX(year_month) AS ym
            FROM statements GROUP BY card_suffix
        ) latest
          ON s.card_suffix = latest.card_suffix AND s.year_month = latest.ym
        ORDER BY s.card_suffix
        """
    )
    return [
        {"card_suffix": c, "year_month": ym, "due_date_str": dd,
         "due": _parse_due(dd), "total_due": tot}
        for c, ym, dd, tot in cur.fetchall()
    ]


def due_messages(today: date | None = None) -> list[dict]:
    """Compute reminders to fire today, WITHOUT side effects.

    Returns a list of dicts: {card, kind, year_month, marker_key, text}.
    Honors the manual `reminders_disabled` override and per-card idempotency
    markers (an already-fired reminder for the current cycle is skipped).
    """
    today = today or date.today()
    if get_config("reminders_disabled") == "1":
        return []

    conn = get_db()
    try:
        cards = _latest_per_card(conn.cursor())
    finally:
        conn.close()

    items: list[dict] = []
    for card in cards:
        days_before = (card["due"] - today).days
        cs, ym = card["card_suffix"], card["year_month"]
        if (card["total_due"] or 0) <= 0:
            continue   # nothing to pay — no ping for a S$0.00 / credit statement
        amount = f"**S${card['total_due']:,.2f}**"

        if 0 < days_before <= REMINDER_DAYS_BEFORE:
            key = f"reminded_3day_{cs}"
            if get_config(key) != ym:
                when = (f"in {days_before} days" if days_before > 1 else "TOMORROW")
                items.append({
                    "card": cs, "kind": "3day", "year_month": ym, "marker_key": key,
                    "text": (f"{_MENTION}💳 Reminder: OCBC **{cs}** payment due {when} "
                             f"({card['due_date_str']}). Total {amount}. "
                             f"Please ensure sufficient funds."),
                })
        elif FIRE_ON_DUE_DATE and -OVERDUE_DAYS <= days_before <= 0:
            key = f"reminders_fired_{cs}"
            if get_config(key) != ym:
                if days_before == 0:
                    text = (f"{_MENTION}🔴 **Payment due today!** OCBC **{cs}** — "
                            f"{amount} is due by {card['due_date_str']}. Please pay immediately.")
                else:
                    late = -days_before
                    text = (f"{_MENTION}🚨 **OVERDUE:** OCBC **{cs}** — {amount} was due "
                            f"{card['due_date_str']} ({late} day{'s' if late > 1 else ''} ago — no "
                            f"reminder went out on the due date). Pay now if you haven't "
                            f"already.")
                items.append({"card": cs, "kind": "due", "year_month": ym,
                              "marker_key": key, "text": text})
    return items


CRON_HOUR = 9   # the daily reminder cron fires at 09:00


def upcoming(now=None) -> list[dict]:
    """What the daily cron WILL send, per card and kind: [{card, kind, fire_date}].

    Mirrors due_messages() exactly (same windows, S$0 skip, markers): the first
    cron run still ahead (today if before 09:00, else tomorrow), clipped to each
    reminder's catch-up window."""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    # the 09:00 run dispatches within ~1 min; before 09:02 it may still be ahead
    ahead = now.hour * 60 + now.minute < CRON_HOUR * 60 + 2
    first_run = now.date() if ahead else now.date() + timedelta(days=1)
    if get_config("reminders_disabled") == "1":
        return []
    conn = get_db()
    try:
        cards = _latest_per_card(conn.cursor())
    finally:
        conn.close()
    out = []
    for card in cards:
        if (card["total_due"] or 0) <= 0:
            continue
        due, cs, ym = card["due"], card["card_suffix"], card["year_month"]
        for kind, key, start, end in (
                ("3day", f"reminded_3day_{cs}", due - timedelta(days=REMINDER_DAYS_BEFORE),
                 due - timedelta(days=1)),
                ("due", f"reminders_fired_{cs}", due, due + timedelta(days=OVERDUE_DAYS))):
            if get_config(key) == ym:
                continue
            fire = max(start, first_run)
            if fire <= end:
                out.append({"card": cs, "kind": kind, "fire_date": fire,
                            "late": fire > start})
    return out


def mark_fired(items: list[dict]) -> None:
    """Record that the given reminders fired (per-card idempotency markers)."""
    for it in items:
        set_config(it["marker_key"], it["year_month"])


def reminder_confirmation() -> str:
    """A clear, human confirmation of the reminder state — used by the upload
    and approval flows so the user always sees that reminders are in place.
    """
    from collections import defaultdict
    from scripts.parse_ocbc import verify_cron_health

    if get_config("reminders_disabled") == "1":
        return "⏸️ Payment reminders are PAUSED (manual override is on)."

    cron = verify_cron_health()
    lines = []
    if cron.get("healthy"):
        lines.append("✅ Payment reminders are SET — automatic daily check, "
                     "nothing for you to do.")
    elif cron.get("determinable") is False:
        # We could not read the cron state at all (jobs.json + hermes CLI both
        # unavailable from here). The daily cron runs independently of this
        # check, so DON'T cry wolf — note it softly instead of a false alarm.
        lines.append("✅ Payment reminders are SET (daily automatic check). "
                     "_(couldn't double-check the cron entry from here.)_")
    else:
        notes = "; ".join(cron.get("notes") or ["cron not active"])
        lines.append(f"🚨 Payment reminders may NOT fire — {notes}.")

    grp: dict = defaultdict(list)
    for ev in upcoming():
        grp[(ev["kind"], ev["fire_date"], ev["late"])].append(ev["card"])
    if grp:
        for (kind, fd, late), cards in sorted(grp.items(), key=lambda x: (x[0][1], x[0][0])):
            if kind == "3day":
                label = "payment reminder (late — its 3-day date has passed)" if late \
                    else "3 days before due"
            else:
                label = "OVERDUE reminder (the due date has passed)" if late \
                    else "on the due date"
            cards_s = " & ".join(sorted(set(cards)))
            lines.append(f"   • {fd.strftime('%d %b %Y')} 09:00 — {label} (card {cards_s})")
    elif cron.get("healthy") or cron.get("determinable") is False:
        # Reminders are believed active (or state unverifiable but assumed set)
        # and nothing is pending this cycle. (Don't append this reassurance when
        # the cron is genuinely unhealthy — the 🚨 line above already covers it.)
        lines.append("   • Nothing left to fire this cycle.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # Default is read-only: the daily cron wrapper does the sending + marking. A
    # manual check must never consume a reminder (marking without sending used to
    # be the default here). `--dry-run` is accepted for backwards compatibility.
    mark = "--mark-fired" in argv
    items = due_messages()
    if not items:
        print("[no reminders due today]")
        return 0
    for it in items:
        print(it["text"])
    if mark:
        mark_fired(items)
        print("[marked as sent — NOT posted to Discord]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
