"""Format process_pdf() responses into Discord-ready confirmation messages.

Used by the bot to give the user a single visible "yes, both PDFs imported and
the reminder is set" message after every upload, instead of inferring success
from absence of errors.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable


def _fmt_date(s: str) -> str:
    """Accept 'YYYY-MM-DD' or already-formatted; return 'DD MMM YYYY'."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return s
    return d.strftime("%d %b %Y")


def _fmt_response_line(resp: dict) -> str:
    card = resp.get("card_suffix", "?")
    ym = resp.get("year_month", "?")
    txns = len(resp.get("transactions") or [])
    total = resp.get("total_due", 0.0)
    return f"✅ Card **{card}** {ym}: {txns} txns, total **S${total:,.2f}**"


def _fmt_warning_block(resp: dict) -> str | None:
    warnings = resp.get("warnings") or []
    if not warnings:
        return None
    card = resp.get("card_suffix", "?")
    bullets = "\n".join(f"  • {w}" for w in warnings)
    return f"⚠️ {card} parser warnings:\n{bullets}"


def _fmt_next_reminders(responses: list[dict]) -> str | None:
    """Aggregate next-reminder events across all responses, dedupe, sort."""
    seen: dict[tuple, dict] = {}
    for r in responses:
        for ev in r.get("next_reminders") or []:
            key = (ev["card"], ev["kind"], ev["year_month"])
            seen[key] = ev
    if not seen:
        return None
    today = date.today()
    upcoming = [ev for ev in seen.values() if not ev["already_fired"]]
    upcoming.sort(key=lambda e: (e["fire_date"], e["card"]))
    if not upcoming:
        return "📅 All reminders for this cycle have already fired."
    lines = ["📅 Upcoming reminders:"]
    for ev in upcoming:
        try:
            fd = datetime.strptime(ev["fire_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            fd = None
        kind_label = "3-day warning" if ev["kind"] == "3day" else "due-date alert"
        if fd is not None:
            days_out = (fd - today).days
            when = f"in {days_out}d" if days_out > 0 else ("today" if days_out == 0 else f"{-days_out}d ago")
            lines.append(f"  • {_fmt_date(ev['fire_date'])} ({when}) — {kind_label} for **{ev['card']}**")
        else:
            lines.append(f"  • {ev['fire_date']} — {kind_label} for **{ev['card']}**")
    return "\n".join(lines)


def _fmt_cron_block(responses: list[dict]) -> str | None:
    """Surface cron health — if anything is unhealthy across responses, flag it."""
    crons = [r.get("cron") for r in responses if r.get("cron")]
    if not crons:
        return None
    cron = crons[-1]  # all responses see the same DB state, last one is current
    if cron.get("healthy"):
        state = cron.get("state", "?")
        return f"⏰ Reminder cron: **{state}** ({cron.get('schedule', '?')})"
    if cron.get("determinable") is False:
        # Couldn't read cron state from here; the daily cron is independent — no alarm.
        return "⏰ Reminder cron: daily automatic check (state unverified from here)"
    notes = "; ".join(cron.get("notes") or ["unknown problem"])
    return f"🚨 Reminder cron: **unhealthy** — {notes}"


def format_import_summary(responses: Iterable[dict], *, include_reminders: bool = True) -> str:
    """Format one or more process_pdf() results into a Discord message.

    Set include_reminders=False when the caller appends its own reminder
    confirmation (e.g. import_and_review.py uses reminder.reminder_confirmation)
    to avoid showing two overlapping reminder blocks.

    Layout:
        ✅ Card 5999 2026-05: 47 txns, total S$1,308.10
        ✅ Card 7008 2026-05: 6 txns, total S$315.56
        ⚠️ 5999 parser warnings:
          • unknown country codes (will pass through unfiltered): ['XX']
        📅 Upcoming reminders:
          • 19 May 2026 (in 3d) — 3-day warning for **5999**
          • 22 May 2026 (in 6d) — due-date alert for **5999**
        ⏰ Reminder cron: active (0 9 20-22 * *)
    """
    responses = list(responses)
    if not responses:
        return "(no PDFs imported)"
    parts: list[str] = [_fmt_response_line(r) for r in responses]
    for r in responses:
        w = _fmt_warning_block(r)
        if w:
            parts.append(w)
    if any(r.get("reminders_resumed") for r in responses):
        parts.append("▶️ Payment reminders had been paused — a new statement turns "
                     "them back on.")
    if include_reminders:
        nr = _fmt_next_reminders(responses)
        if nr:
            parts.append(nr)
        cb = _fmt_cron_block(responses)
        if cb:
            parts.append(cb)
    return "\n".join(parts)


def format_import_failure(error: Exception, *, pdf_path: str | None = None) -> str:
    """Format a process_pdf() exception into a Discord error message."""
    label = pdf_path.rsplit("/", 1)[-1] if pdf_path else "(unknown PDF)"
    return f"❌ Import failed for `{label}`: {type(error).__name__}: {error}"
