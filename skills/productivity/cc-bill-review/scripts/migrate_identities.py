#!/usr/bin/env python3
"""Repair of merchant identities after a normalizer fix: re-normalize every stored
transaction with the CURRENT normalize_merchant() and fold the identities that
change into the real merchant. Each fix bumps MARKER, so the run happens once.

v4 (v3.5.0, 2026-10-02) — two parser bugs:
1. The statement's TOTAL AMOUNT DUE figure was glued onto the LAST transaction's
   description of every statement ("SHOPEE SG MP ... SG 2,689.54"), creating
   junk identities ("SHOPEE MP SINGAPORE 2,689.54", "ANNUAL FEE 516.87",
   one "CASH REBATE nnn" per month).
2. "FOREIGN CURRENCY USD 10.00" was kept in the name, so each USD charge of the
   same merchant got its own identity (NANONOBLE x3).
v5 (v3.5.1, 2026-10-04):
3. The page-footer strip ate the city of the row the footer is glued to (the
   last row on a page): #108 "COFFEE HIVE INNOVI" next to #109 "... SINGAPORE".
4. The user merged #18 "COFFEE HIVE - INNOV" (OCBC's old descriptor) into #109.

The glued total is also stripped from merchant_raw (a no-op once v4 ran).
Approvals are never lost: when rows fold together the surviving row keeps the
approval if ANY of them was approved (hidden -1 stays hidden); the surviving
row is the real merchant's if it exists (it keeps its id), else the oldest junk
row (renamed, keeping its id). verify() checks every approval before commit.

    migrate_identities.py            # show the plan, change nothing
    migrate_identities.py --apply    # snapshot the DB, apply in ONE transaction

Safe to re-run: the marker is written in the same transaction, and a second
run finds nothing to change.
"""
import sys
from collections import defaultdict

sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from scripts import parse_ocbc as P  # noqa: E402

MARKER = "identity_migration_v5"   # v4 = 2026-10-02


def plan(conn) -> dict:
    cur = conn.cursor()
    totals = {(r[0], r[1]): r[2] for r in cur.execute(
        "SELECT card_suffix, year_month, total_due FROM statements")}
    last_ids = {(r[0], r[1]): r[2] for r in cur.execute(
        "SELECT card_suffix, year_month, MAX(id) FROM transactions GROUP BY 1, 2")}
    txn_changes = []
    for tid, card, ym, raw, norm in cur.execute(
            "SELECT id, card_suffix, year_month, merchant_raw, merchant_normalized "
            "FROM transactions ORDER BY id").fetchall():
        new_raw = raw
        tot = totals.get((card, ym))
        if tid == last_ids.get((card, ym)) and tot is not None:
            tail = " " + f"{tot:,.2f}"
            if raw.endswith(tail) and len(raw) > len(tail):
                new_raw = raw[: -len(tail)].rstrip()
        new_norm = P.normalize_merchant(new_raw, card)
        if new_raw != raw or new_norm != norm:
            txn_changes.append({"id": tid, "card": card, "ym": ym, "raw": raw,
                                "new_raw": new_raw, "norm": norm, "new_norm": new_norm})
    renames = defaultdict(set)   # (card, old_norm) -> {new_norm}
    for c in txn_changes:
        if c["norm"] != c["new_norm"]:
            renames[(c["card"], c["norm"])].add(c["new_norm"])
    return {"txns": txn_changes, "renames": renames}


def _merge_approval(a, b):
    a, b = a or 0, b or 0
    if -1 in (a, b):
        return -1
    return max(a, b)


def expected_approvals(conn, p: dict) -> dict:
    """{(card, final_name): approved} the migration must end with, computed up front."""
    cur = conn.cursor()
    final = {}
    for (card, old), news in p["renames"].items():
        if len(news) != 1:
            continue
        new = next(iter(news))
        for name in (old, new):
            row = cur.execute("SELECT approved FROM merchants WHERE card_suffix=? AND "
                              "merchant_normalized=?", (card, name)).fetchone()
            if row is not None:
                final[(card, new)] = _merge_approval(final.get((card, new), 0), row[0])
    return final


def apply(conn, p: dict) -> list[str]:
    cur = conn.cursor()
    log = []
    P._bump_uid_mark(conn)   # ids freed below must never be reused
    stmts = sorted({(c["card"], c["ym"]) for c in p["txns"]})
    # Park the affected statements' row_num on unique negatives first: a renamed row can
    # land on a same-day, same-amount charge of the merchant it folds into, and the
    # (…, merchant, amount, row_num) unique index would reject the rename mid-way.
    for card, ym in stmts:
        cur.execute("UPDATE transactions SET row_num = -id WHERE card_suffix=? AND year_month=?",
                    (card, ym))
    for c in p["txns"]:
        cur.execute("UPDATE transactions SET merchant_raw=?, merchant_normalized=? WHERE id=?",
                    (c["new_raw"], c["new_norm"], c["id"]))
    # row_num = rank among identical (date, merchant, amount) rows of a statement
    for card, ym in stmts:
        seen = defaultdict(int)
        for tid, d, n, a in cur.execute(
                "SELECT id, txn_date, merchant_normalized, amount FROM transactions "
                "WHERE card_suffix=? AND year_month=? ORDER BY id", (card, ym)).fetchall():
            cur.execute("UPDATE transactions SET row_num=? WHERE id=?", (seen[(d, n, a)], tid))
            seen[(d, n, a)] += 1
    touched = set()
    folds = {}   # deleted uid -> surviving uid
    # Fold merchants rows: oldest uid first, so it survives when no real row exists.
    olds = sorted(p["renames"], key=lambda k: (cur.execute(
        "SELECT COALESCE(merchant_uid, 1e9) FROM merchants WHERE card_suffix=? AND "
        "merchant_normalized=?", k).fetchone() or (1e9,))[0])
    for card, old in olds:
        left = cur.execute("SELECT COUNT(*) FROM transactions WHERE card_suffix=? AND "
                           "merchant_normalized=?", (card, old)).fetchone()[0]
        news = sorted(p["renames"][(card, old)])
        touched.update((card, n) for n in news)
        touched.add((card, old))
        if left or len(news) != 1:
            log.append(f"  [{card}] {old!r}: rows split ({left} left) — row kept as is")
            continue
        new = news[0]
        src = cur.execute("SELECT merchant_uid, approved FROM merchants WHERE card_suffix=? "
                          "AND merchant_normalized=?", (card, old)).fetchone()
        dst = cur.execute("SELECT merchant_uid, approved FROM merchants WHERE card_suffix=? "
                          "AND merchant_normalized=?", (card, new)).fetchone()
        if src is None:
            continue
        if dst is not None:
            cur.execute("DELETE FROM merchants WHERE card_suffix=? AND merchant_normalized=?",
                        (card, old))
            keep = _merge_approval(src[1], dst[1])
            if keep != dst[1]:
                cur.execute("UPDATE merchants SET approved=? WHERE card_suffix=? AND "
                            "merchant_normalized=?", (keep, card, new))
            if src[0] and dst[0]:
                folds[src[0]] = dst[0]
            note = "" if src[1] == dst[1] else f" (approvals {src[1]} + {dst[1]} → {keep})"
            log.append(f"  [{card}] #{src[0]} {old!r} merged into #{dst[0]} {new!r}{note}")
        else:
            cur.execute("UPDATE merchants SET merchant_normalized=? WHERE card_suffix=? "
                        "AND merchant_normalized=?", (new, card, old))
            log.append(f"  [{card}] #{src[0]} {old!r} renamed to {new!r} (id + approval kept)")
    for card, name in sorted(touched):
        P._refresh_merchant_aggregates(cur, card, [name])
    P._assign_merchant_uids(conn)
    if folds:
        _record_folds(cur, folds)
        log.append("  merged ids remembered: " + ", ".join(
            f"#{a} → #{b}" for a, b in sorted(folds.items())))
    return log


def _record_folds(cur, folds: dict) -> None:
    """Remember merged ids ("approve #18" then answers "merged into #109") and point
    the last-shown list at the surviving ids (else "approve all" skips a deleted id
    and "all except #old" names a row that no longer exists). Same transaction."""
    import json
    from scripts import reviewlib as R
    row = cur.execute("SELECT value FROM config WHERE key=?", (R.MERGED_IDS_KEY,)).fetchone()
    merged = {}
    try:
        old = json.loads(row[0]) if row else {}
    except ValueError:
        old = {}
    for k, v in (old.items() if isinstance(old, dict) else ()):
        try:                       # one bad entry must not wipe the earlier merges
            merged[str(int(k))] = int(v)
        except (ValueError, TypeError):
            continue
    merged.update({str(a): b for a, b in folds.items()})
    for k, v in list(merged.items()):   # chains: 18 -> 109, a later 109 -> 120
        seen = set()
        while str(v) in merged and v not in seen:
            seen.add(v)
            v = merged[str(v)]
        merged[k] = v
    cur.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value = excluded.value", (R.MERGED_IDS_KEY, json.dumps(merged, sort_keys=True)))
    row = cur.execute("SELECT value FROM config WHERE key=?", (R.LAST_SHOWN_KEY,)).fetchone()
    if not row:
        return
    try:
        data = json.loads(row[0])
        out, at = [], {}
        for it in data["merchants"]:
            uid = merged.get(str(it["uid"]), it["uid"])
            if uid in at:          # two shown ids folded together: "new" if either was
                prev = out[at[uid]]
                prev["new"] = bool(prev.get("new")) or bool(it.get("new"))
                continue
            at[uid] = len(out)
            out.append({**it, "uid": uid})
        data["merchants"] = out
    except (ValueError, TypeError, KeyError, AttributeError):
        return   # malformed: last_shown() already treats it as blocked
    cur.execute("UPDATE config SET value=? WHERE key=?", (json.dumps(data), R.LAST_SHOWN_KEY))


def verify(conn, expected: dict | None = None, before: dict | None = None) -> list[str]:
    """Problems that must be zero before commit. `expected` = folded rows'
    approvals, `before` = every untouched merchant's approval before the run."""
    cur = conn.cursor()
    bad = []
    now = {(r[0], r[1]): r[2] for r in cur.execute(
        "SELECT card_suffix, merchant_normalized, approved FROM merchants")}
    for k, want in (expected or {}).items():
        if now.get(k) != want:
            bad.append(f"approval of {k} is {now.get(k)}, expected {want}")
    for k, was in (before or {}).items():
        if k in now and k not in (expected or {}) and now[k] != was:
            bad.append(f"approval of untouched {k} changed {was} -> {now[k]}")
    drift = [(r[0], r[1]) for r in cur.execute(
        "SELECT card_suffix, merchant_raw, merchant_normalized FROM transactions")
        if P.normalize_merchant(r[1], r[0]) != r[2]]
    if drift:
        bad.append(f"normalize drift on {len(drift)} rows, e.g. {drift[:2]}")
    orphans = cur.execute(
        "SELECT COUNT(*) FROM transactions t LEFT JOIN merchants m ON t.card_suffix="
        "m.card_suffix AND t.merchant_normalized=m.merchant_normalized "
        "WHERE m.merchant_normalized IS NULL").fetchone()[0]
    if orphans:
        bad.append(f"{orphans} transactions without a merchants row")
    mism = cur.execute(
        "SELECT COUNT(*) FROM merchants m WHERE txn_count != (SELECT COUNT(*) FROM "
        "transactions t WHERE t.card_suffix=m.card_suffix AND "
        "t.merchant_normalized=m.merchant_normalized)").fetchone()[0]
    if mism:
        bad.append(f"{mism} merchants rows with stale txn_count")
    dup_uid = cur.execute("SELECT COUNT(*) FROM (SELECT merchant_uid FROM merchants "
                          "GROUP BY merchant_uid HAVING COUNT(*) > 1)").fetchone()[0]
    if dup_uid or cur.execute("SELECT COUNT(*) FROM merchants WHERE merchant_uid IS NULL"
                              ).fetchone()[0]:
        bad.append("merchant_uid not unique / missing")
    for card, ym, tot in cur.execute("SELECT card_suffix, year_month, total_due FROM statements"):
        n = cur.execute("SELECT COUNT(*) FROM transactions WHERE card_suffix=? AND "
                        "year_month=? AND merchant_raw LIKE ?",
                        (card, ym, f"% {tot:,.2f}")).fetchone()[0]
        if n:
            bad.append(f"{card} {ym}: glued total still present")
    return bad


def main(argv) -> int:
    do_apply = "--apply" in argv
    P.init_db().close()
    conn = P.get_db()
    try:
        if P.get_config(MARKER):
            print(f"Already applied ({P.get_config(MARKER)}). Nothing to do.")
            return 0
        p = plan(conn)
        print(f"{len(p['txns'])} transaction row(s) to fix, "
              f"{len(p['renames'])} merchant identit(y/ies) to fold:")
        for c in p["txns"]:
            print(f"  txn {c['id']} [{c['card']} {c['ym']}] {c['norm']!r} -> {c['new_norm']!r}"
                  + (" (glued total removed)" if c["raw"] != c["new_raw"] else ""))
        if not do_apply:
            print("\n(dry run — nothing changed; re-run with --apply)")
            return 0
        snap = P.snapshot_db("pre_identity_migration")
        if snap is None:
            print("🚨 could not snapshot the DB — refusing to migrate")
            return 1
        print(f"\nsnapshot: {snap}")
        expected = expected_approvals(conn, p)
        before = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT card_suffix, merchant_normalized, approved FROM merchants")}
        conn.execute("BEGIN")
        log = apply(conn, p)
        problems = verify(conn, expected, before)
        if problems:
            conn.rollback()
            print("🚨 verification failed — rolled back, nothing changed:")
            for b in problems:
                print("  - " + b)
            return 1
        from datetime import datetime
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) "
                     "DO UPDATE SET value = excluded.value",
                     (MARKER, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        print("✅ applied:")
        print("\n".join(log) or "  (no merchant rows needed folding)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
