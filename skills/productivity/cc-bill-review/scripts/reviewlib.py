#!/usr/bin/env python3
"""
Shared review logic for cc-bill-review — the single source of truth for every
"what should be flagged" query and how it is rendered.

`review.py`, `import_and_review.py`, and `approve.py` all import from here so
the numbering shown in a review is the *same* numbering `approve.py` resolves
later. The fraud-signal queries (high-value / foreign / new-merchant) and the
de-duplication rules live in one place instead of being copy-pasted (and
drifting) across scripts.

Key correctness rules baked in here:
  * Every charge is shown. `row_num > 0` rows are real, billed repeat charges
    (same date, merchant and amount — e.g. two coffees); statement totals only
    reconcile with them. They used to be filtered out as "page-break
    duplicates", which hid double charges (fixed 2026-10).
  * The new-/unapproved-merchant list is GROUPED BY merchant — one row per
    unique merchant, never one row per transaction (the old bug).
  * Approval is keyed on the stable `merchant_uid`, so an id printed today is
    the same id `approve.py` acts on tomorrow.
  * "New this cycle" = the merchant's earliest-ever statement month equals this
    statement's month (i.e. genuinely first-seen) — the real fraud signal.
  * Foreign = country_code not SG/blank OR the description carries a
    "FOREIGN CURRENCY" marker (catches USD-billed-by-SG-entity charges that
    OCBC tags with country SG).
"""
from __future__ import annotations

import re

REVIEW_AMOUNT_THRESHOLD = 100.0  # SGD — flag transactions above this

MONTH_NAMES = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}

# Bank/system entries — never a "merchant" needing approval, never a fraud
# signal. Matched against merchant_normalized with LIKE.
_SYSTEM_LIKE = (
    "%PAYMENT%INTERNET%",
    "%NETS FLASHPAY TOPUP%",
    "%CASH REBATE%",
    "%CCY CONVERSION FEE%",
)

# Italian one-off holiday spend (Nov–Dec 2025) the user pre-rejected: hidden
# (approved = -1 in the DB), excluded from the foreign + approval lists, and
# never approvable. Matched on the EXACT canonical names the parser maps them
# to — substrings ("%TPER%") also matched unrelated merchants such as
# SPORTPERFORMANCE / OUTPERFORM and silently dropped their foreign flag.
HOLIDAY_NAMES = (
    "TPER SPA",
    "CLESS TICKET",
    "ATB SERVIZI",
    "START ROMAGNA",
    "LA BOTTEGA DEL TART",
    "TAP & GO",
)

# ANNUAL FEE is a flag-only item: it is ALWAYS surfaced in its own review
# section (so the user calls the bank to reverse it) and must NEVER appear in
# an approval list or be approvable/hideable. Covers "ANNUAL FEE 516.87" and
# "ANNUAL FEE REVERSAL".
_ANNUAL_LIKE = ("%ANNUAL FEE%",)

# What never belongs in a merchant approval list (a/b sections, approve all).
_APPROVAL_EXCLUDE = _SYSTEM_LIKE + _ANNUAL_LIKE

APPROVE_HINT = ("→ Reply `approve #<id> [#<id> ...]` (e.g. `approve #18 #53`), "
                "`approve #<a>-<b>`, `approve all`, or `approve all except #<id>`. "
                "Anything you don't approve keeps showing next month.")


def _like_not(column: str, patterns) -> str:
    """Build an `AND column NOT LIKE ?` clause fragment for each pattern."""
    return "".join(f" AND {column} NOT LIKE '{p}'" for p in patterns)


def _not_holiday(column: str) -> str:
    names = ", ".join("'" + n.replace("'", "''") + "'" for n in HOLIDAY_NAMES)
    return f" AND {column} NOT IN ({names})"


def is_holiday(name: str) -> bool:
    return name in HOLIDAY_NAMES


def display_ym(ym: str) -> str:
    y, m = ym.split("-")
    name = MONTH_NAMES.get(m)
    return f"{name} {y}" if name else ym


# ---------------------------------------------------------------------------
# Fraud-signal queries (per transaction)
# ---------------------------------------------------------------------------
def high_value(cur, card: str, ym: str) -> list:
    cur.execute(
        f"""
        SELECT txn_date, merchant_normalized, amount, country_code
        FROM transactions
        WHERE card_suffix=? AND year_month=?
          AND amount > 0 AND amount > ?
          {_like_not('merchant_normalized', _SYSTEM_LIKE)}
          {_like_not('merchant_normalized', _ANNUAL_LIKE)}
        ORDER BY amount DESC
        """,
        (card, ym, REVIEW_AMOUNT_THRESHOLD),
    )
    return cur.fetchall()


def foreign(cur, card: str, ym: str) -> list:
    """Foreign = country not SG/blank, OR a FOREIGN CURRENCY marker in the raw
    description (USD charges billed by SG-registered entities are tagged SG; the
    marker is checked on merchant_raw because canonical names drop it)."""
    cur.execute(
        f"""
        SELECT txn_date, merchant_normalized, amount, country_code, merchant_raw
        FROM transactions
        WHERE card_suffix=? AND year_month=?
          AND amount > 0
          AND (
                (COALESCE(country_code, '') NOT IN ('SG','') )
             OR (merchant_raw LIKE '%FOREIGN CURRENCY%')
             OR (merchant_normalized LIKE '%FOREIGN CURRENCY%')
          )
          {_like_not('merchant_normalized', ('%CCY CONVERSION FEE%',))}
          {_not_holiday('merchant_normalized')}
        ORDER BY txn_date, id
        """,
        (card, ym),
    )
    return cur.fetchall()


_FX_RE = re.compile(r"FOREIGN CURRENCY\s+([A-Z]{3})\s+([\d,]+(?:\.\d{1,3})?)\b")


def fx_detail(raw: str | None) -> str:
    """'USD 10.00' from a raw 'FOREIGN CURRENCY USD 10.00' marker, else ''."""
    m = _FX_RE.search(raw or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


def annual_fee(cur, card: str, ym: str) -> list:
    cur.execute(
        """
        SELECT txn_date, merchant_normalized, amount
        FROM transactions
        WHERE card_suffix=? AND year_month=?
          AND merchant_normalized LIKE '%ANNUAL FEE%'
          AND amount > 0
        ORDER BY txn_date, id
        """,
        (card, ym),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Merchant approval lists (de-duplicated — one row per unique merchant)
# ---------------------------------------------------------------------------
def unapproved_in_statement(cur, card: str, ym: str, only_new: bool = False) -> list:
    """Unique unapproved merchants that appear in this statement.

    Returns rows: (merchant_uid, merchant_normalized, n_txns, total, ccy,
    first_seen, is_new) where is_new == 1 iff this statement is the merchant's
    earliest-ever statement month for this card.
    """
    rows = cur.execute(
        f"""
        SELECT m.merchant_uid,
               t.merchant_normalized,
               COUNT(*)                AS n_txns,
               SUM(t.amount)           AS total,
               MAX(t.country_code)     AS ccy,
               m.first_seen,
               CASE WHEN (
                   SELECT MIN(t2.year_month) FROM transactions t2
                   WHERE t2.card_suffix = t.card_suffix
                     AND t2.merchant_normalized = t.merchant_normalized
               ) = t.year_month THEN 1 ELSE 0 END AS is_new
        FROM transactions t
        LEFT JOIN merchants m
          ON t.card_suffix = m.card_suffix
         AND t.merchant_normalized = m.merchant_normalized
        WHERE t.card_suffix=? AND t.year_month=?
          AND COALESCE(m.approved, 0) = 0
          AND t.amount > 0
          {_like_not('t.merchant_normalized', _APPROVAL_EXCLUDE)}
          {_not_holiday('t.merchant_normalized')}
        GROUP BY m.merchant_uid, t.merchant_normalized
        ORDER BY is_new DESC, total DESC
        """,
        (card, ym),
    ).fetchall()
    if only_new:
        rows = [r for r in rows if r["is_new"] == 1]
    return rows


def all_unapproved(cur, card: str | None = None) -> list:
    """Every currently-unapproved merchant (across all statements), deduped.

    This backs the on-demand "show me the full list to approve" command.
    Returns rows: (merchant_uid, card_suffix, merchant_normalized, txn_count,
    total_spent, first_seen, last_seen).
    """
    where = "WHERE COALESCE(approved,0) = 0 AND COALESCE(txn_count,0) > 0"
    params: tuple = ()
    if card:
        where += " AND card_suffix=?"
        params = (card,)
    return cur.execute(
        f"""
        SELECT merchant_uid, card_suffix, merchant_normalized,
               txn_count, total_spent, first_seen, last_seen
        FROM merchants
        {where}
          {_like_not('merchant_normalized', _APPROVAL_EXCLUDE)}
          {_not_holiday('merchant_normalized')}
        ORDER BY card_suffix, merchant_uid
        """,
        params,
    ).fetchall()


def latest_yms(cur, card: str | None = None) -> list[tuple[str, str]]:
    """Return [(card_suffix, latest_year_month)] — the statements the routine
    review shows. `approve all` is scoped to these so it only touches what the
    user just saw."""
    sql = "SELECT card_suffix, MAX(year_month) FROM statements"
    params: tuple = ()
    if card:
        sql += " WHERE card_suffix=?"
        params = (card,)
    sql += " GROUP BY card_suffix ORDER BY card_suffix"
    return [(r[0], r[1]) for r in cur.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Rendering (Discord markdown — matches SKILL.md formatting rules)
# ---------------------------------------------------------------------------
def say(line) -> None:
    """The ONLY way an entry script writes its reply to stdout: ONE line, whitespace
    collapsed (a Cloudflare HTML error body in `detail` must not become a multi-line
    reply). The agent sends this line to the user as its whole answer (SKILL.md) —
    so it is written for the user: no thread ids, no instructions to the agent.
    Any 17–20-digit Discord id that slips in (an exception text) is masked."""
    import re
    print(re.sub(r"(?<!\d)\d{17,20}(?!\d)", "[id]", " ".join(str(line).split())))


def brief(detail) -> str:
    """A delivery error for the reply line: HTML tags dropped, one line, capped.
    `<urlopen error [Errno -3] …>` is a reason, not a tag — unwrapped first."""
    import re
    text = re.sub(r"<urlopen error (.*?)>", r"\1", str(detail))
    return " ".join(re.sub(r"</?[A-Za-z!][^>]*>", " ", text).split())[:160]


def why(exc: BaseException) -> str:
    """'TypeName: first sentence of the message' (just the name if it has none)."""
    text = " ".join(str(exc).split()).split(". ")[0].rstrip(".")
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


MERGED_IDS_KEY = "merged_merchant_ids"   # {"18": 109} — written by migrate_identities


def merged_into(conn, uid: int) -> int | None:
    """The id a deleted merchant id was merged into (None if it never existed)."""
    import json
    row = conn.execute("SELECT value FROM config WHERE key=?", (MERGED_IDS_KEY,)).fetchone()
    try:
        new = json.loads(row[0]).get(str(uid)) if row else None
        return int(new) if new is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def esc_md(s: str) -> str:
    """Make untrusted text (merchant names from the bank PDF) inert in Discord.

    OCBC masks merchant names with `**` (e.g. `SMP**JUNG SUNG HAIR`). Interpolated
    raw into a review, a stray `**` corrupts Discord's bold parsing for the rest of
    the message — bolding pairs shift and the two card sections render
    inconsistently. Escaping at render time keeps the stored merchant name intact.
    Also defangs (with a zero-width space) masked links `](`, autolinks `://`,
    spoilers `||` and `<@id>` / `<#id>` / `<t:…>` / `<:emoji:>` tokens; pings are
    additionally blocked server-side (`allowed_mentions` in discord_post.py).
    """
    zw = "\u200b"
    s = (s.replace("\\", "\\\\")
          .replace("*", "\\*")
          .replace("_", "\\_")
          .replace("~", "\\~")
          .replace("`", "\\`"))
    s = s.replace("](", "]" + zw + "(").replace("://", ":" + zw + "//")
    s = s.replace("||", "|" + zw + "|")
    s = re.sub(r"<(?=[@#:at]|/)", "<" + zw, s)
    return s


def _uid(m) -> str:
    return f"#{m['merchant_uid']}" if m["merchant_uid"] is not None else "#?"


def render_statement_ex(cur, card: str, ym: str, stmt_date: str, due_date: str,
                        total_due: float, with_hint: bool = True):
    """Render one card/month review block.

    Returns (lines, shown) where `shown` lists {"uid", "new"} for every merchant
    id the block displays — the set `approve all` acts on (see approve.py)."""
    out = [
        f"### 💳 {card} - {display_ym(ym)}",
        f"Statement date: {stmt_date}",
        f"Due: **{due_date}**",
        f"Total due: **S${total_due:,.2f}**",
        "",
    ]

    hv = high_value(cur, card, ym)
    if hv:
        out.append("**🔴 High-value (>$100):**")
        for txn_dt, merchant, amt, ccy in hv:
            bold = "**" if amt > 200 else ""
            ccy_suffix = f" | {ccy}" if ccy else ""
            out.append(f"  {txn_dt} | {bold}S${amt:,.2f}{bold} | {esc_md(merchant)}{ccy_suffix}")
        out.append("")

    af = annual_fee(cur, card, ym)
    if af:
        out.append("**💳 Annual Fee — call bank to reverse:**")
        for txn_dt, merchant, amt in af:
            out.append(f"  {txn_dt} | S${amt:,.2f} | {esc_md(merchant)}")
        out.append("")

    fg = foreign(cur, card, ym)
    if fg:
        out.append("**🌍 Foreign transactions:**")
        for txn_dt, merchant, amt, ccy, raw in fg:
            fx = fx_detail(raw)
            fx_s = f" · {fx}" if fx else ""
            out.append(f"  {txn_dt} | S${amt:,.2f} | {esc_md(merchant)} | {ccy or '??'}{fx_s}")
        out.append("")

    merchants = unapproved_in_statement(cur, card, ym)
    new = [m for m in merchants if m["is_new"] == 1]
    seen = [m for m in merchants if m["is_new"] == 0]
    shown = [{"uid": m["merchant_uid"], "new": m["is_new"] == 1}
             for m in merchants if m["merchant_uid"] is not None]

    if new:
        out.append("**🆕 NEW merchants — never seen before (approve or they keep showing):**")
        for m in new:
            ccy_suffix = f" | {m['ccy']}" if m["ccy"] and m["ccy"] != "SG" else ""
            out.append(
                f"  `{_uid(m)}` {esc_md(m['merchant_normalized'])} | "
                f"{m['n_txns']}x | S${m['total']:,.2f}{ccy_suffix}"
            )
        out.append("")
    if seen:
        out.append(f"**🔁 Previously-seen, still unapproved ({len(seen)}):**")
        for m in seen:
            out.append(
                f"  `{_uid(m)}` {esc_md(m['merchant_normalized'])} | "
                f"{m['n_txns']}x | S${m['total']:,.2f}"
            )
        out.append("")

    if new or seen:
        if with_hint:
            out.append(APPROVE_HINT)
            out.append("")
    elif hv or af or fg:
        # There are fraud-signal flags but nothing to approve — say so
        # explicitly so an empty approval list never reads as "nothing happened".
        out.append("🆕 New merchants to approve: **none** — nothing on this "
                   "statement is waiting for approval. ✅")
        out.append("")

    if not (hv or af or fg or new or seen):
        out.append("✅ No flags this month — nothing high-value, foreign, or "
                   "waiting for approval.")
        out.append("")

    return out, shown


def render_statement(cur, card: str, ym: str, stmt_date: str, due_date: str,
                     total_due: float) -> list[str]:
    """Render one card/month review block. Returns a list of lines."""
    return render_statement_ex(cur, card, ym, stmt_date, due_date, total_due)[0]


# ---------------------------------------------------------------------------
# "What the user was last shown" — `approve all` acts on exactly this set
# ---------------------------------------------------------------------------
LAST_SHOWN_KEY = "last_shown_merchants"


def record_shown(conn, shown: list[dict], source: str, target: str | None = None,
                 blocked: str | None = None) -> None:
    """Persist what a POSTED list displayed, so a later `approve all` touches
    only what the user actually saw. Callers write `blocked="posting in
    progress"` BEFORE posting (a killed run then can't leave an older list live),
    the shown list after a full post, and `blocked=<reason>` after a failed or
    partial post / a print-only run inside an agent session — `approve all` then
    refuses until the list is posted again."""
    import json
    from datetime import datetime
    # Deliberately no merging of an earlier list: after two separate uploads,
    # "approve all" covers only the latest posted review (narrower, never wider —
    # a lap-3 gate probe showed a merge re-approving merchants the user had just
    # un-approved). The user approves the earlier review's ids explicitly.
    payload = {"source": source, "at": datetime.now().isoformat(timespec="seconds"),
               "target": target, "merchants": [] if blocked else list(shown)}
    if blocked:
        payload["blocked"] = blocked
    with conn:
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (LAST_SHOWN_KEY, json.dumps(payload)))


def last_shown(conn) -> dict | None:
    import json
    row = conn.execute("SELECT value FROM config WHERE key=?", (LAST_SHOWN_KEY,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0])
        items = data.get("merchants")
        if isinstance(items, list) and all(
                isinstance(it, dict) and isinstance(it.get("uid"), int) for it in items):
            return data
    except (ValueError, AttributeError, TypeError):
        pass
    return {"blocked": "the saved list record is unreadable", "merchants": []}
