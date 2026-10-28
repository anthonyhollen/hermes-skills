#!/usr/bin/env python3
"""
Generate a monthly statement review for Discord.

Thin CLI over `reviewlib` (the shared source of truth). Flags high-value,
foreign, annual-fee, and new/unapproved merchants — deduped to one row per
unique merchant, with a stable `#id` for the approval workflow.

Usage:
    review.py                      # latest statement per card
    review.py --all                # every imported statement
    review.py --card 5999          # restrict to one card
    review.py --month 2026-06      # restrict to one statement month
    review.py --merchants-only     # just the de-duped unapproved-merchant list
    ... --post                     # POST it to the current Discord conversation
                                   # (byte-safe; what SKILL.md runs)

Whatever is displayed is recorded as "last shown", so a following
`approve all` approves exactly that list.
"""
import argparse
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
try:   # a broken edit must still give the agent ONE line for the user, not a traceback
    from scripts.parse_ocbc import get_db, log
    from scripts import reviewlib
except Exception as _exc:  # noqa: BLE001
    if __name__ != "__main__":   # imported (selftest): fail loudly with the traceback
        raise
    try:                         # skill.log by hand — the logger may be what's broken
        import traceback
        with open(str(__file__).rsplit("/", 2)[0] + "/skill.log", "a") as _f:
            _f.write("import failed:\n" + traceback.format_exc())
    except OSError:
        pass
    print("🚨 The card-review scripts failed to load (" + type(_exc).__name__ + ") — "
          "nothing was done.")   # the one bare print: reviewlib may be what's broken
    sys.exit(1)


def _record(conn, shown, source, target, blocked=None) -> bool:
    try:
        reviewlib.record_shown(conn, shown, source, target, blocked=blocked)
        return True
    except Exception as exc:  # noqa: BLE001 — a busy DB must not crash the reply
        log.warning("record_shown failed: %s", exc)   # not stderr: the agent reads it
        return False


def _statements(cur, card=None, month=None, latest_only=False):
    sql = ("SELECT card_suffix, year_month, statement_date, due_date, total_due "
           "FROM statements")
    clauses, params = [], []
    if card:
        clauses.append("card_suffix=?"); params.append(card)
    if month:
        clauses.append("year_month=?"); params.append(month)
    if latest_only and not month:
        clauses.append(
            "year_month = (SELECT MAX(year_month) FROM statements s2 "
            "WHERE s2.card_suffix = statements.card_suffix)"
        )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY year_month DESC, card_suffix"
    return cur.execute(sql, params).fetchall()


def render_merchants_only(cur, card=None, shown=None) -> str:
    rows = reviewlib.all_unapproved(cur, card)
    if shown is not None:
        # new=None: this list carries no new/seen split, so "approve all new" = all of it
        shown.extend({"uid": r["merchant_uid"], "new": None} for r in rows)
    if not rows:
        scope = f" for {card}" if card else ""
        return f"✅ No unapproved merchants{scope} — nothing needs approval."
    out = ["**🆕 Merchants awaiting approval:**"]
    cur_card = None
    for r in rows:
        if r["card_suffix"] != cur_card:
            cur_card = r["card_suffix"]
            out.append(f"__Card {cur_card}__")
        first = (r["first_seen"] or "")[:10]
        out.append(
            f"  `#{r['merchant_uid']}` {reviewlib.esc_md(r['merchant_normalized'])} | "
            f"{r['txn_count']}x | S${r['total_spent']:,.2f} | since {first}"
        )
    out.append("")
    out.append("→ `approve #<id> [#<id> ...]`, `approve #<a>-<b>`, or `approve all` "
               "(= everything in this list) to whitelist. Unapproved merchants keep showing.")
    return "\n".join(out)


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # one stdout line (SKILL.md: the agent replies with it)
        reviewlib.say(f"⚠️ usage error: {message}")
        sys.exit(2)


def main():
    ap = _Parser()
    ap.add_argument("--all", action="store_true", help="every statement, not just latest")
    ap.add_argument("--card")
    ap.add_argument("--month")
    ap.add_argument("--merchants-only", action="store_true")
    ap.add_argument("--post", nargs="?", const="", default=None, metavar="CHANNEL_ID",
                    help="post to the current Discord conversation (byte-safe)")
    ap.add_argument("--force-target", action="store_true",
                    help="let an explicit --post id win over the current conversation")
    args = ap.parse_args()

    conn = get_db()
    cur = conn.cursor()
    try:
        shown: list[dict] = []
        if args.merchants_only:
            body = render_merchants_only(cur, args.card, shown)
            source = "merchant"
        else:
            statements = _statements(cur, card=args.card, month=args.month,
                                     latest_only=not args.all)
            if not statements:
                reviewlib.say("No statement matches that card/month." if (args.card or args.month)
                              else "No statements imported yet.")
                return 0
            blocks = []
            for card, ym, stmt_dt, due_dt, total_due in statements:
                lines, s = reviewlib.render_statement_ex(
                    cur, card, ym, stmt_dt, due_dt, total_due, with_hint=False)
                blocks.append("\n".join(lines).rstrip())
                shown.extend(s)
            body = "\n\n---\n\n".join(blocks)
            if shown:
                body += "\n\n" + reviewlib.APPROVE_HINT
            source = "review"
        from scripts.discord_post import post_messages, resolve_target, session_target
        if args.post is None:
            print(body)   # print-only: NOT recorded as shown (the user may never see it)…
            if session_target():   # …and inside an agent session an older list must not stay live
                if not _record(conn, [], source, None, "the list was printed, not posted"):
                    reviewlib.say("🚨 The review database was busy — ask again in a minute.")
                    return 3
            return 0
        import uuid
        target, note = resolve_target(args.post or None, force=args.force_target)
        if note:
            log.info("post target: %s", note)
        if not target:
            reviewlib.say("🚨 Not posted — there's no Discord conversation to post to.")
            return 2
        if not _record(conn, [], source, target, "posting in progress"):
            reviewlib.say("🚨 The review database was busy, so nothing was posted — ask again "
                          "in a minute.")
            return 3
        # per-run salt: asking for the same list again must post it again
        res = post_messages(target, body, seed=f"{source}:{uuid.uuid4().hex}")
        uniq = len({m["uid"] for m in shown})
        if res["ok"]:
            if not _record(conn, shown, source, target):
                reviewlib.say("🚨 Posted above, but saving which list was shown failed, so "
                              "\"approve all\" won't work until the list is posted again.")
                return 3
            what = "Review" if source == "review" else "Merchant list"
            reviewlib.say(f"✅ {what} posted above — {uniq} merchant(s) awaiting approval.")
            return 0
        _record(conn, [], source, target, f"post failed after {res['posted']}/{res['total']}")
        reviewlib.say(f"🚨 The list couldn't be posted to Discord ({res['posted']}/"
                      f"{res['total']} message(s) sent): {reviewlib.brief(res['detail'])}.")
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    # Discord-safe wrapper: even on a crash, emit a deliverable error message.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — one line for the user, traceback to skill.log
        log.exception("review.py crashed")
        reviewlib.say(f"⚠️ Couldn't build the review ({reviewlib.why(exc)}).")
        sys.exit(1)
