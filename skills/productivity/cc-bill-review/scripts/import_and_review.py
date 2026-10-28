#!/usr/bin/env python3
"""
One-shot upload handler for cc-bill-review: import one or more statement PDFs
AND deliver the review in a single run — so "imported but forgot to post the
review" can't happen.

Usage:
    import_and_review.py --post <pdf1> [pdf2 ...]                  # what SKILL.md runs
    import_and_review.py <pdf1> [pdf2 ...]                         # manual: print only

For each PDF: process_pdf() auto-detects the card + period from the PDF body
(atomic; a parse failure leaves existing data untouched). Then the review for
each imported (card, month) is built, with stable approval ids.

With `--post`, the review is POSTed to Discord by this script (byte-safe — see
scripts/discord_post.py), to the CURRENT conversation (HERMES_SESSION_KEY; an
explicit id is only used when there is no session), split into several
messages if it is longer than Discord's 2000-unit limit. stdout then carries
ONE line written for the user — it is the agent's whole reply (SKILL.md): no
merchant names, no thread ids, and no instructions to the agent (a trailing
"Relay this; add nothing." made an agent spend 6 minutes investigating where
that instruction came from, 2026-10-03). What the agent does on a failure is
keyed by the exit code in SKILL.md, not written into stdout.

Exit codes: 0 = all PDFs imported and the review delivered; 1 = at least one
PDF failed (the others are imported and reviewed; the failure is in the posted
message); 2 = usage error / nothing to post to; 3 = imported but the review
could not be delivered (re-run the SAME command once — re-import is a clean
replace and already-delivered messages are de-duplicated); 4 = this Python
can't read PDFs (no pymupdf) — nothing was imported; re-run the same command
with /usr/bin/python3.
"""
import importlib.util
import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
try:   # a broken edit must still give the agent ONE line for the user, not a traceback
    from scripts.parse_ocbc import process_pdf, get_db, log
    from scripts.import_response import format_import_summary, format_import_failure
    from scripts.reminder import reminder_confirmation
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

USAGE = ("usage: import_and_review.py [--post [<channel_id>] [--force-target]] "
         "<pdf1> [pdf2 ...]")


def _parse_args(argv: list[str]) -> tuple[list[str], bool, str | None, bool]:
    """Return (pdfs, post, explicit_channel, force). The id after --post is optional."""
    args = list(argv)
    post, explicit = False, None
    force = "--force-target" in args
    if force:
        args.remove("--force-target")
    if "--post" in args:
        i = args.index("--post")
        post = True
        if i + 1 < len(args) and re.fullmatch(r"(discord:)?\d{17,20}", args[i + 1]):
            explicit = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    return args, post, explicit, force


def build_review(responses: list[dict], failures: list) -> tuple[str, list[dict], str]:
    """Return (body, shown_merchants, reminder_line)."""
    parts, shown = [], []
    if responses:
        parts.append(format_import_summary(responses, include_reminders=False))
    for pdf, exc in failures:
        parts.append(format_import_failure(exc, pdf_path=pdf))
    reminder_line = ""
    if responses:
        conn = get_db()
        cur = conn.cursor()
        try:
            seen = []
            for r in responses:
                card, ym = r.get("card_suffix"), r.get("year_month")
                if not card or not ym or (card, ym) in seen:
                    continue
                seen.append((card, ym))
                row = cur.execute(
                    "SELECT statement_date, due_date, total_due FROM statements "
                    "WHERE card_suffix=? AND year_month=?", (card, ym)).fetchone()
                if not row:
                    continue
                lines, s = reviewlib.render_statement_ex(
                    cur, card, ym, row["statement_date"], row["due_date"],
                    row["total_due"], with_hint=False)
                parts.append("\n".join(lines).rstrip())
                shown.extend(s)
            if shown:
                parts.append(reviewlib.APPROVE_HINT)
        finally:
            conn.close()
        conf = reminder_confirmation()
        parts.append(conf)
        reminder_line = conf.splitlines()[0] if conf else ""
    return "\n\n".join(p for p in parts if p), shown, reminder_line


def _record(shown, target, blocked=None) -> bool:
    """Write the "last shown" record; False (never an exception) if the DB is busy."""
    try:
        conn = get_db()
        try:
            reviewlib.record_shown(conn, shown, "latest review", target, blocked=blocked)
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        # skill.log, not stderr: the terminal tool hands stderr to the agent too
        log.warning("record_shown failed: %s", exc)
        return False


def pdf_reader_available() -> bool:
    return importlib.util.find_spec("pymupdf") is not None


def _plain(text: str) -> str:
    """Text for the stdout line: one line, no markdown emphasis, no trailing period."""
    return re.sub(r"[*_`]", "", " ".join(str(text).split())).rstrip(".")


def _first_clause(exc: BaseException) -> str:
    """A failure reason for the user: the message's first sentence only (later
    sentences of some errors are advice for whoever runs the script)."""
    text = _plain(str(exc)).split(". ")[0]
    return text or type(exc).__name__


def _reminder_status(line: str) -> str:
    """'✅ Payment reminders are SET — automatic …' -> '✅ Payment reminders are SET'.
    The full line (in the posted review) may tell the user to run a command."""
    return re.split(r" — | \(", _plain(line))[0].strip() if line else ""


def main(argv: list[str]) -> int:
    say = reviewlib.say
    pdfs, post, explicit, force = _parse_args(argv[1:])
    if not pdfs:
        say("🚨 Nothing imported — I didn't get the statement PDF(s); please send them "
            "again." if post else USAGE)
        return 2
    if not pdf_reader_available():
        say(f"🚨 Not imported — this Python ({sys.executable}) can't read PDFs (pymupdf "
            f"isn't installed for it). Nothing was imported or posted.")
        return 4

    target = None
    if post:
        from scripts.discord_post import resolve_target
        target, note = resolve_target(explicit, force=force)
        if note:
            log.info("post target: %s", note)
        if not target:
            say("🚨 Not imported — there's no Discord conversation to post the review to. "
                "Nothing was imported or posted.")
            return 2

    responses, failures = [], []
    for pdf in pdfs:
        try:
            responses.append(process_pdf(pdf))
        except Exception as exc:  # noqa: BLE001 — report every failure per PDF
            failures.append((pdf, exc))

    try:
        body, shown, reminder_line = build_review(responses, failures)
    except Exception as exc:  # noqa: BLE001 — the import is committed; say so
        log.exception("build_review failed")
        say(f"🚨 Imported {len(responses)} statement(s), but building the review failed "
            f"({reviewlib.why(exc)}). Nothing was posted.")
        return 3
    uniq = len({m["uid"] for m in shown})
    n_warn = sum(len(r.get("warnings") or []) for r in responses)
    cards = ", ".join(f"{r.get('card_suffix')} {r.get('year_month')} "
                      f"({len(r.get('transactions') or [])} txns)" for r in responses)
    failed = "; ".join(f"{pdf.rsplit('/', 1)[-1]}: {_first_clause(e)}" for pdf, e in failures)
    status = _reminder_status(reminder_line)
    facts = "; ".join(x for x in (
        f"imported {cards}" if responses else "",
        f"FAILED: {failed}" if failed else "",
        f"{n_warn} parser warning(s) — see the review" if n_warn else "",
        f"{uniq} merchant(s) awaiting approval" if responses else "",
        status if responses else "") if x)

    from scripts.discord_post import post_messages, session_target
    if not post:
        print(body)
        if responses and session_target():
            # printed inside an agent session: what the user sees is unknown, so an
            # older posted list must not stay live for "approve all"
            if not _record(shown, None, blocked="the review was printed, not posted"):
                say("🚨 The review database was busy — ask again in a minute.")
                return 3
        return 1 if failures else 0

    seed = "import:" + ",".join(sorted(f"{r.get('card_suffix')}-{r.get('year_month')}"
                                       for r in responses))
    if responses and not _record(shown, target, blocked="posting in progress"):
        say(f"🚨 Imported {cards}, but the review database was busy, so nothing was posted.")
        return 3
    res = post_messages(target, body, seed=seed)
    if responses:
        ok = _record(shown, target) if res["ok"] else _record(
            shown, target, blocked=f"post failed after {res['posted']}/{res['total']}")
        if not ok and res["ok"]:
            say(f"🚨 Review posted above, but saving which list was shown failed, so "
                f"\"approve all\" won't work until the review is posted again — {facts}.")
            return 3
    if not res["ok"] and not responses:
        say(f"🚨 Nothing imported ({facts}), and posting the failure notice to Discord "
            f"failed: {reviewlib.brief(res['detail'])}.")
        return 1
    if not res["ok"]:
        say(f"🚨 Imported {cards}, but the review was not fully posted "
            f"({res['posted']}/{res['total']} message(s)): {reviewlib.brief(res['detail'])}.")
        return 3
    dup = (f" ({res['already_there']} part(s) were already there and weren't repeated)"
           if res.get("already_there") else "")
    head = ("✅ Review posted above" if responses
            else "🚨 Nothing imported — the failure notice is posted above")
    say(f"{head}{dup} — {facts}.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — one line for the user, traceback to skill.log
        log.exception("import_and_review crashed")
        reviewlib.say(f"🚨 Something went wrong ({reviewlib.why(exc)}) — the review may not "
                      f"have been posted.")
        sys.exit(3)
