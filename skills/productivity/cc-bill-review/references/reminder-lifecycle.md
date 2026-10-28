# CC Reminder — Lifecycle & Cron Auto-Disable

> ⚠️ **HISTORICAL — SUPERSEDED (correction added 2026-07-11).** This document
> describes the **pre-v3** auto-pause/resume design: the cron paused itself after
> the due-date reminder fired, resumed on the next import, and used a `[SILENT]`
> token. **None of that is true anymore.** In v3+ the cron fires **daily**
> (`0 9 * * *`) and is **never auto-paused**; idempotency is per-card via
> `reminded_3day_{card}` / `reminders_fired_{card}`; `reminders_disabled` is a
> **manual override only**; there is no `[SILENT]` token; and cron *state* is read
> from `~/.hermes/cron/jobs.json`, not the `hermes` CLI. The `disable_reminder_cron`
> / `resume_reminder_cron` functions were **removed in v3.5.0** (2026-10-02) — the
> import no longer touches the cron at all (`resume` was not a no-op: it recomputed
> `next_run_at` from now and could skip that day's 09:00 run). The recipes below that
> import them no longer work. See **Reminders** in `SKILL.md` and the v3.3.0 entry in
> `CHANGELOG.md` for the current model. The text below is kept for provenance only.

*From: cc-bill-review session, May 2026*

## Problem
The payment reminder cron job (`7b68c1ef7afe`) ran every month regardless of whether the user had already paid. After the due-date reminder fired, there was no mechanism to prevent spurious 3-day-ahead reminders in subsequent months until the user explicitly asked to pause it.

## Solution — Auto-disable on due-date reminder

Two mechanisms work together:

1. **`config` table flag** — `reminders_disabled` key (`"0"` = enabled, `"1"` = disabled). Checked on every reminder.py run; if `"1"`, exits silently with `[SILENT]`.
2. **Hermes cron pause/resume** — The cron job itself is paused after the due-date reminder fires, and resumed when the next statement is imported.

### Flow

```
Cron fires (days 20-22 at 9am)
    └─ reminder.py checks reminders_disabled flag
          ├─ "1" → print("[SILENT]"), exit
          └─ "0" → compute reminder dates from DB
                    ├─ 3 days before due → fire reminder, keep enabled
                    └─ due date = today → fire due-date reminder
                                           → set reminders_disabled=1
                                           → hermes cron pause 7b68c1ef7afe

User imports next statement PDF
    └─ process_pdf() → store_statement()
          └─ store_statement() resets reminders_disabled=0
                                  → resume_reminder_cron()
```

## Config table

```sql
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Key rows:
--   reminders_disabled: "0" = enabled, "1" = disabled
```

## Key functions (parse_ocbc.py)

```python
def get_config(key: str, default: str = None) -> str | None:
    conn = get_db()
    cur = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def disable_reminder_cron():
    result = subprocess.run(
        ["hermes", "cron", "pause", "7b68c1ef7afe"],
        capture_output=True, text=True
    )
    return result.returncode == 0

def resume_reminder_cron():
    result = subprocess.run(
        ["hermes", "cron", "resume", "7b68c1ef7afe"],
        capture_output=True, text=True
    )
    return result.returncode == 0
```

`resume_reminder_cron()` is called at the end of `process_pdf()` after `store_statement()` completes.

## Manual override

```python
cd ~/.hermes/skills/productivity/cc-bill-review
python3 -c "
from scripts.parse_ocbc import set_config, resume_reminder_cron, get_config
print('Current:', get_config('reminders_disabled'))
set_config('reminders_disabled', '0')
resume_reminder_cron()
print('Re-enabled')
"
```

## Why both mechanisms?

The `reminders_disabled` flag handles the edge case where the cron is externally paused or the script runs outside the cron. The cron-pause is the primary disable mechanism because it prevents the script from being invoked at all on days 20-22 when reminders are disabled.