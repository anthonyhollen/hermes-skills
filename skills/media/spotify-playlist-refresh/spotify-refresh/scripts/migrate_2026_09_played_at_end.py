#!/usr/bin/env python3
"""
One-shot migration (2026-09-23, skill v2.17.0): recompute play_events.ms_played
with played_at = play END semantics, and lift the 6 blocklist bans — every one was
caused by the old (start-time) inference (user-approved).

Before v2.17.0 the poller used ms_played_i = min(dur_i, played_at_{i+1} - played_at_i),
i.e. each track was credited with the NEXT track's listen time. All 6 blocklist
entries at the time traced to such mis-scored plays (they score +0.70..+1.00
under the corrected math); scorer.update_blocklist re-blocks anything that is
genuinely disliked on the next refresh.

Idempotent: re-running recomputes the same values. Backs up the DB first.
Usage (gateway venv):  python migrate_2026_09_played_at_end.py [--db PATH] [--dry-run]
"""
import argparse, os, sqlite3, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import poller  # noqa: E402  (_infer_ms_played — the exact rules the poller now uses)

DEFAULT_DB = os.path.join(HERE, "..", "spotify.db")
MIGRATION = "2026-09-played-at-end"
# The 6 blocklist entries on 2026-09-23 — all traced to mis-scored plays.
BANS_TO_LIFT = (
    "1SaaP6cNtcV0bzaPYNneBG",  # Can't Help Myself
    "0QICKhP44TRnb4EurQ3tN9",  # BILLIONAIRE
    "10nSl4tuKAfdHiSPppxrNY",  # To. My First Love
    "3QgQhBHtQvRpAulIfNwcRG",  # Swipe It Out
    "4jCNLZieeYkijwEXXFeixK",  # Boom Boom Boom
    "6ufVsXMC1fAePZa5ZRMKdO",  # Busy Boy
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no such DB: {args.db}")
    src = sqlite3.connect(args.db, timeout=30)
    bak = args.db + ".bak-playedat-20260923"
    # "Already ran" marker lives IN the DB, written in the same transaction as
    # the ban lift, so a failed run (e.g. "database is locked") leaves no marker
    # and can simply be re-run. A completed re-run still recomputes ms_played
    # (idempotent) but must NOT lift bans again — by then the fixed scorer may
    # have legitimately re-blocked one of these tracks.
    has_table = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'").fetchone() is not None
    already_ran = has_table and src.execute(
        "SELECT 1 FROM migrations WHERE name = ?", (MIGRATION,)).fetchone() is not None
    if not args.dry_run:
        if not os.path.exists(bak):
            dst = sqlite3.connect(bak)
            src.backup(dst)
            dst.close()
            os.chmod(bak, 0o600)
            print(f"backup: {bak}")

    rows = src.execute(
        "SELECT track_id, played_at, duration_ms, ms_played FROM play_events ORDER BY played_at, track_id"
    ).fetchall()
    events = [{"track_id": r[0], "played_at": r[1], "duration_ms": r[2]} for r in rows]
    new = poller._infer_ms_played(events, None)
    changes = [(n["ms_played"], r[0], r[1]) for r, n in zip(rows, new) if n["ms_played"] != r[3]]
    print(f"events: {len(rows)}  ms_played changed: {len(changes)}")

    ph = ",".join("?" * len(BANS_TO_LIFT))
    bans = src.execute(
        f"SELECT b.track_id, b.reason, t.name FROM blocklist b LEFT JOIN track_cache t USING(track_id) "
        f"WHERE b.track_id IN ({ph})", BANS_TO_LIFT
    ).fetchall()
    if already_ran:
        bans = []
        print("ban lift skipped: migration already ran (migrations table marker)")
    for tid, reason, name in bans:
        print(f"  lifting ban: {tid} {name!r} ({reason})")

    if args.dry_run:
        print("dry-run: nothing written")
        return
    with src:
        src.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, applied_at INTEGER)")
        src.executemany("UPDATE play_events SET ms_played = ? WHERE track_id = ? AND played_at = ?", changes)
        if not already_ran:
            src.execute(f"DELETE FROM blocklist WHERE track_id IN ({ph})", BANS_TO_LIFT)
            src.execute("INSERT INTO migrations (name, applied_at) VALUES (?, strftime('%s','now')*1000)",
                        (MIGRATION,))
    print("done")


if __name__ == "__main__":
    main()
