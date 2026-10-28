#!/usr/bin/env python3
"""
Deterministic merchant approval for cc-bill-review.

Resolves the user's reply against the stable `merchant_uid` ids printed in the
review, so it works with no conversation memory: the id shown today is the id
acted on tomorrow. The agent's only job is to pass the user's raw reply text
through to this script (on stdin — see SKILL.md) and reply with its stdout line.

Grammar — ONE verb, first, then ids / `all` (anything else is refused, nothing
changes, and the user is asked to rephrase; the script never guesses):
    approve #18 #53            approve those merchants (whitelist forever)
    approve #9-12              approve an inclusive id range
    approve all                approve every still-unapproved merchant in the list you
                               were last shown IN THIS THREAD (the latest review, or the
                               --merchants-only list) — refused if that list didn't fully
                               reach you, was shown elsewhere, or is over 14 days old
    approve all new            ... only the 🆕 ones (review lists)
    approve all 5999           ... restricted to one card
    approve all 5999 except #18 #20  ... minus those ids (cards go BEFORE "except")
    approve all backlog [except #ids] approve EVERY unapproved merchant, all months
    unapprove #18              undo — back to the unapproved list (keeps showing)
    reject #18                 = keep it flagged (same as unapprove; never hides)
    hide #99                   permanently suppress a bank/system row (ids only)

Refused (exit 2, nothing changed): negations ("don't approve #18"), two verbs in
one message, questions, `hide all`, words the grammar doesn't know. ANNUAL FEE
and the pre-rejected holiday merchants are never approvable.

Usage:
    approve.py --post - <<'EOF'           # agent: the reply on stdin ("-")
    <the user's reply>
    EOF
    approve.py "approve #18 #53"          # manual use; prints instead of posting
"""
import re
import sqlite3
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

_TARGET = {"approve": 1, "unapprove": 0, "reject": 0, "hide": -1}
_VERB_PAST = {"approve": "Approved", "unapprove": "Un-approved (will keep showing)",
              "reject": "Kept flagged (will keep showing)", "hide": "Hidden"}
_CARDS = ("5999", "7008")
_FILLER = {"ok", "okay", "please", "pls", "plz", "yes", "yep", "sure", "alright", "kindly",
           "thanks", "thank", "you", "thx", "ty", "cheers"}
SHOWN_MAX_AGE_DAYS = 14
_VERB_RE = re.compile(r"\b(un-?approve|approve|reject|hide)\b", re.I)
_NEG_RE = re.compile(r"\b(not|don'?t|dont|do\s+not|never|no|without|isn'?t|won'?t)\b", re.I)
_EXCEPT = {"except", "but", "excluding", "besides"}
_EXCEPT_PHRASES = (("other", "than"), ("apart", "from"))
_ALL_WORDS = {"all"}
_ALL_MODS = {"new", "merchants", "merchant", "the", "of", "them", "these", "those", "ones"}
_CONNECT = {"and", "&", "+", "card", "cards", "for", "on", "ids", "id"}

EXAMPLES = ("`approve #18 #53`, `approve #9-12`, `approve all`, "
            "`approve all except #18`, `unapprove #18`, `hide #99`")


def _q(word: str) -> str:
    """A user's word, safe to echo into a Discord message / stdout."""
    return re.sub(r"[*_~`|\\<>\[\]()@#]", "", word)[:30] or "?"


class Refused(Exception):
    """The reply can't be acted on safely; nothing is changed."""


# Only the gateway's own prefixes are stripped — its "[Triggering message id: …]" and
# "[System note: …]" preambles and the user's own "[name]" tag (CC_REVIEW_NAME_TAGS,
# default "ham") — never arbitrary bracketed words ("[cancel] approve all" is refused).
import os as _os
_NAME_TAGS = "|".join(re.escape(n.strip()) for n in
                      _os.environ.get("CC_REVIEW_NAME_TAGS", "ham").split(",") if n.strip())
_PREFIX = re.compile(r"^(\s*\[(?:Triggering message id:|System note:)[^\]]*\])*"
                     r"(\s*\[(?:" + (_NAME_TAGS or "(?!)") + r")\])?\s*", re.I)


def _tokens(text: str) -> list[str]:
    text = _PREFIX.sub("", text)
    text = text.replace("`", " ").replace(",", " , ").replace(";", " , ")
    text = re.sub(r"(#?\d+)\s*(?:[-‐‑‒–—−]|\bto\b)\s*(#?\d+)", r"\1-\2", text)
    text = re.sub(r"\bthem\s+all\b|\ball\s+of\s+them\b", "all", text, flags=re.I)
    return [t for t in re.split(r"\s+", text.strip()) if t]


def parse_command(text: str) -> dict:
    """Parse a reply. Returns {verb, mode ('ids'|'all'|'backlog'), ids, cards,
    only_new, exclude}. Raises Refused with a user-facing reason."""
    if _NEG_RE.search(text):   # checked BEFORE any prefix is stripped
        raise Refused("it contains a negation (e.g. \"don't\"/\"not\") — I only act on "
                      "positive instructions")
    raw = _PREFIX.sub("", " ".join(text.split()))
    if not raw:
        raise Refused("the message was empty")
    if "?" in raw:
        raise Refused("that reads like a question")
    if re.search(r"\b\d{1,3}(?:,\d{3})+\b|\$|S\$", raw):
        raise Refused("it contains an amount — I only take merchant ids (if those are ids, "
                      "write them like `#105 #106`)")
    if _NEG_RE.search(raw):
        raise Refused("it contains a negation (e.g. \"don't\"/\"not\") — I only act on "
                      "positive instructions")
    verbs = [m.group(1).lower().replace("-", "") for m in _VERB_RE.finditer(raw)]
    if len(verbs) != 1:
        raise Refused("it needs exactly one action (approve / unapprove / reject / hide) "
                      "per message" if verbs else "I couldn't find approve / unapprove / "
                      "reject / hide")
    verb = verbs[0]

    toks = _tokens(raw)
    while toks and (toks[0] == "," or toks[0].lower().strip(".!") in _FILLER):
        toks.pop(0)
    if not toks or _VERB_RE.fullmatch(toks[0].strip(".!:")) is None:
        raise Refused("the action word must come first")
    rest = [t.strip(".!:") for t in toks[1:]]
    rest = [t for t in rest if t and t.lower() not in _FILLER]

    def take_ids(words, allow_cards=True):
        ids, cards = set(), []
        for w in words:
            lw = w.lower()
            if lw in _CONNECT or w == ",":
                continue
            if allow_cards and w in _CARDS:
                cards.append(w)
                continue
            m = re.fullmatch(r"#?(\d+)-#?(\d+)", w)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if lo > hi or hi - lo >= 200:
                    raise Refused(f"the range {w} doesn't look right")
                ids.update(range(lo, hi + 1))
                continue
            m = re.fullmatch(r"#?(\d+)", w)
            if m:
                ids.add(int(m.group(1)))
                continue
            raise Refused(f"I don't understand {_q(w)!r}")
        return ids, cards

    if rest and rest[0].lower() in _ALL_WORDS:
        words = rest[1:]
        only_new = backlog = False
        cards: list[str] = []
        exclude: set[int] = set()
        i = 0
        while i < len(words):
            lw = words[i].lower()
            two = tuple(x.lower() for x in words[i:i + 2])
            if lw in _EXCEPT or two in _EXCEPT_PHRASES:
                after = words[i + (2 if two in _EXCEPT_PHRASES else 1):]
                if any(w in _CARDS for w in after):
                    raise Refused("put the card before \"except\" (e.g. `approve all 7008 "
                                  "except #18`) — after \"except\" I only take merchant ids")
                exclude, _ = take_ids(after, allow_cards=False)
                if not exclude:
                    raise Refused("after \"except\" I need merchant ids like #18")
                break
            if lw == "new":
                only_new = True
            elif lw == "backlog":
                backlog = True
            elif words[i] in _CARDS:
                cards.append(words[i])
            elif lw in _ALL_MODS or lw in _CONNECT or words[i] == ",":
                pass
            else:
                raise Refused(f"I don't understand {_q(words[i])!r} after \"all\"")
            i += 1
        if verb == "hide":
            raise Refused("`hide` only works on explicit ids (e.g. `hide #99`), never `all`")
        if verb == "reject":
            raise Refused("`reject all` would only undo approvals — unapproved merchants "
                          "already keep showing. To undo approvals say `unapprove all`")
        if backlog and verb != "approve":
            raise Refused("`all backlog` only works with approve")
        if backlog and only_new:
            raise Refused("`all backlog` covers every month — it can't be combined with `new`")
        return {"verb": verb, "mode": "backlog" if backlog else "all", "ids": [],
                "cards": sorted(set(cards)), "only_new": only_new,
                "exclude": sorted(exclude)}

    ids, cards = take_ids(rest)
    if not ids:
        raise Refused("no merchant ids found")
    return {"verb": verb, "mode": "ids", "ids": sorted(ids), "cards": sorted(set(cards)),
            "only_new": False, "exclude": []}


def _resolve_all(cur, conn, cmd, target=None) -> tuple[list[int], str]:
    """The ids `all` refers to. Backlog = every unapproved merchant. Otherwise the
    list the user was last shown (recorded only after it was fully posted); with
    no record at all (lists posted before v3.5) — the latest statement per card.
    Raises Refused when acting on the recorded list would not be safe."""
    if cmd["mode"] == "backlog":
        rows = []
        for c in (cmd["cards"] or [None]):
            rows.extend(reviewlib.all_unapproved(cur, c))
        items = [{"uid": r["merchant_uid"], "new": None} for r in rows]
        note = " (full backlog, all months)"
    else:
        shown = reviewlib.last_shown(conn)
        if shown and shown.get("blocked"):
            raise Refused("the last list wasn't posted in full here, so I won't guess what "
                          "\"all\" covers — ask me to post the review or the merchant list "
                          "again, then reply `approve all`")
        if shown:
            if target and shown.get("target") != target:
                raise Refused("the last list I posted was in a different thread — ask me "
                              "to post it here first")
            from datetime import datetime, timedelta
            try:
                age = datetime.now() - datetime.fromisoformat(shown.get("at", ""))
            except (ValueError, TypeError):
                age = timedelta(days=SHOWN_MAX_AGE_DAYS + 1)
            if age > timedelta(days=SHOWN_MAX_AGE_DAYS):
                raise Refused(f"the last list I posted is {age.days} days old — ask me to "
                              f"post it again first")
            items = shown["merchants"]
            note = f" (from the {shown.get('source') or 'last'} list)"
        else:
            items = [{"uid": m["merchant_uid"], "new": m["is_new"] == 1}
                     for c, ym in reviewlib.latest_yms(cur)
                     for m in reviewlib.unapproved_in_statement(cur, c, ym)]
            note = " (from the latest review)"
    if cmd["only_new"] and any(it.get("new") is not None for it in items):
        items = [it for it in items if it.get("new")]
    ids = [it["uid"] for it in items if it.get("uid") is not None]
    if cmd["cards"]:
        keep = {r[0] for r in cur.execute(
            f"SELECT merchant_uid FROM merchants WHERE card_suffix IN "
            f"({','.join('?' * len(cmd['cards']))})", cmd["cards"])}
        ids = [i for i in ids if i in keep]
    ex = set(cmd["exclude"])
    unknown = sorted(ex - set(ids))
    if unknown:
        merged = [(u, reviewlib.merged_into(conn, u)) for u in unknown]
        hint = "; ".join(f"#{u} was merged into #{m}" for u, m in merged if m)
        raise Refused("#" + ", #".join(map(str, unknown))
                      + (" isn't" if len(unknown) == 1 else " aren't")
                      + " in the list \"all\" covers — check the ids"
                      + (f" ({hint})" if hint else ""))
    return sorted(set(ids) - ex), note


def apply(conn, cmd, post_target=None) -> dict:
    """Apply a parsed command. Returns {changed, skipped, scope_note, excluded}.
    Raises Refused (nothing changed) when `all` can't be resolved safely."""
    cur = conn.cursor()
    target = _TARGET[cmd["verb"]]
    if cmd["mode"] == "ids":
        ids, note = cmd["ids"], ""
    else:
        ids, note = _resolve_all(cur, conn, cmd, post_target)
    changed, skipped = [], []
    with conn:
        for uid in ids:
            row = cur.execute(
                "SELECT card_suffix, merchant_normalized, approved "
                "FROM merchants WHERE merchant_uid=?", (uid,)).fetchone()
            if row is None:
                new = reviewlib.merged_into(conn, uid)
                skipped.append((uid, f"merged into #{new} — use that id" if new
                                else "no such id"))
                continue
            name = row["merchant_normalized"]
            if cmd["cards"] and row["card_suffix"] not in cmd["cards"]:
                skipped.append((uid, f"card {row['card_suffix']}"))
                continue
            if "ANNUAL FEE" in name.upper() and target != 0:
                skipped.append((uid, "ANNUAL FEE — always flagged, not approvable"))
                continue
            if reviewlib.is_holiday(name):
                skipped.append((uid, "pre-rejected holiday spend — fixed, not changeable"))
                continue
            if row["approved"] == target:
                skipped.append((uid, "no change needed"))
                continue
            if cmd["mode"] != "ids" and row["approved"] == -1:
                skipped.append((uid, "hidden — only an explicit id changes it"))
                continue
            cur.execute("UPDATE merchants SET approved=? WHERE merchant_uid=?", (target, uid))
            changed.append((uid, row["card_suffix"], name))
    return {"changed": changed, "skipped": skipped, "scope_note": note,
            "excluded": cmd["exclude"]}


def summary_line(cmd, res, counts) -> str:
    """The stdout reply (no merchant names): what changed, what was skipped and why,
    what is left. ✅ only when something changed; ⚠️ when an id was wrong."""
    n, skipped = len(res["changed"]), res["skipped"]
    verb = _VERB_PAST[cmd["verb"]].split(" (")[0]
    shown = ", ".join(f"#{u} ({why})" for u, why in skipped[:5])
    if len(skipped) > 5:
        shown += f" and {len(skipped) - 5} more"
    left = ("nothing left to approve" if not counts else "still unapproved — "
            + ", ".join(f"{c}: {k}" for c, k in sorted(counts.items())))
    if n:
        ids_s = " ".join(f"#{u}" for u, _c, _n in res["changed"])
        return (f"✅ {verb} {n} merchant(s) ({ids_s})"
                + (f"; skipped {shown}" if skipped else "") + f"; {left}.")
    if not skipped:
        return f"ℹ️ Nothing changed — nothing in the list needed it; {left}."
    mark = "ℹ️" if all(why == "no change needed" for _u, why in skipped) else "⚠️"
    return f"{mark} Nothing changed — skipped {shown}; {left}."


def remaining_counts(cur) -> dict:
    from collections import Counter
    return dict(Counter(r["card_suffix"] for r in reviewlib.all_unapproved(cur)))


def render(cmd, res, counts) -> str:
    out = []
    changed, skipped = res["changed"], res["skipped"]
    if changed:
        out.append(f"✅ {_VERB_PAST[cmd['verb']]} {len(changed)} merchant(s){res['scope_note']}:")
        for uid, c, name in changed:
            out.append(f"  `#{uid}` [{c}] {reviewlib.esc_md(name)}")
    elif cmd["mode"] != "ids" and not skipped:
        out.append(f"Nothing to {cmd['verb']}{res['scope_note']}.")
    if res["excluded"]:
        out.append("↩️ Left as they were (you excluded them): "
                   + ", ".join(f"#{u}" for u in res["excluded"]))
    if skipped:
        out.append(f"⏭️ Skipped {len(skipped)}: "
                   + ", ".join(f"#{u} ({why})" for u, why in skipped))
    out.append("")
    if not counts:
        out.append("🎉 Nothing left to approve.")
    else:
        parts = ", ".join(f"{c}: {n}" for c, n in sorted(counts.items()))
        out.append(f"📋 Still unapproved — {parts}. Ask for the merchant list to see "
                   f"them (then `approve all` approves exactly that list).")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    post, explicit, force = False, None, False
    if "--force-target" in args:
        args.remove("--force-target")
        force = True
    if "--post" in args:
        i = args.index("--post")
        post = True
        if i + 1 < len(args) and re.fullmatch(r"(discord:)?\d{17,20}", args[i + 1]):
            explicit = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    use_stdin = args == ["-"]   # "-" is the stdin marker only on its own (never a range dash)
    text = sys.stdin.read().strip() if use_stdin else " ".join(args).strip()
    if not text:
        reviewlib.say("⚠️ Nothing changed — I didn't receive your reply text; please send "
                      "it again." if post else
                      "usage: approve.py --post - <<'EOF' (then the reply text, then EOF) "
                      "| approve.py \"approve #18 #53\"")
        return 2

    from scripts.discord_post import resolve_target, session_target
    target = session_target()   # the thread check applies with or without --post
    if post:
        target, note = resolve_target(explicit, force=force)
        if note:
            log.info("post target: %s", note)   # not stderr: the agent reads it
        if not target:
            reviewlib.say("🚨 Nothing changed — there's no Discord conversation to reply in.")
            return 2

    def deliver(body, seed):
        if not post:
            print(body)
            return None
        from scripts.discord_post import post_messages
        return post_messages(target, body, seed=seed)

    try:
        return _run(text, post, target, deliver)
    except sqlite3.OperationalError as exc:   # e.g. database is locked — nothing changed
        reviewlib.say(f"🚨 Nothing changed — the review database was busy ({exc}). Please "
                      f"send your reply again in a minute.")
        return 3


def _run(text, post, target, deliver) -> int:
    conn = get_db()
    try:
        try:
            cmd = parse_command(text)
            res = apply(conn, cmd, target)
        except Refused as why:
            body = f"⚠️ I didn't change anything — {why}. Reply with one of: {EXAMPLES}."
            import uuid
            pres = deliver(body, "refused:" + uuid.uuid4().hex)
            if pres is None:
                return 2
            if pres["ok"]:
                reviewlib.say(f"⚠️ Nothing changed: {why}. The replies I accept are posted "
                              f"above.")
            else:
                reviewlib.say(f"⚠️ Nothing changed: {why}. Reply with explicit ids, e.g. "
                              f"`approve #18 #53`. (Posting the full explanation to Discord "
                              f"failed: {reviewlib.brief(pres['detail'])}.)")
            return 2

        cur = conn.cursor()
        counts = remaining_counts(cur)
        body = render(cmd, res, counts)
        import uuid
        pres = deliver(body, "approve:" + uuid.uuid4().hex)   # a repeat must post again
        if pres is None:
            return 0
        line = summary_line(cmd, res, counts)
        if pres["ok"]:
            reviewlib.say(line)
            return 0
        if not res["changed"]:   # nothing was saved — don't say "Saved"
            reviewlib.say(f"{line.rstrip('.')} (posting the details to Discord failed: "
                          f"{reviewlib.brief(pres['detail'])}).")
            return 3
        reviewlib.say(f"🚨 Saved — {line.split(' ', 1)[1].rstrip('.')} — but posting the "
                      f"confirmation to Discord failed: {reviewlib.brief(pres['detail'])}.")
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — one line for the user, traceback to skill.log
        log.exception("approve.py crashed")
        reviewlib.say(f"🚨 Something went wrong while applying your reply "
                      f"({type(exc).__name__}) — ask me to show the merchant list to see what "
                      f"is approved now.")
        sys.exit(1)
