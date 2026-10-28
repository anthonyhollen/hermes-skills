#!/usr/bin/env python3
"""
find_duplicate_merchants.py — detect same-shop, two-normalized-name splits in the
cc_review.db merchants/transactions tables.

OCBC periodically rephrases merchant descriptions in their PDF statements
(e.g. SMP**JUNG SUNG HAIR SG in Jan → JUNGSUNGHAIR PTE LT SINGAPORE in Jun). The
normalizer cleans each variant independently, producing two different
`merchant_normalized` strings for the same real-world shop — which then surfaces
as a spurious "🆕 new merchant" flag every time the description changes again.

This script runs a token-Jaccard cluster pass over every (card_suffix,
merchant_normalized) group and prints clusters likely to be the same shop, each
WITH its evidence (every distinct `merchant_raw`), so a cluster can be judged
without querying the DB by hand. The leading `-NNNN` on a raw description is a
payment-channel code shared by many merchants (-9113 covers 25 different shops
in the live data), NOT a shop id — judge on the names, and ask the user before
merging (a merge moves an approval). Clusters already verified as different
shops (VERIFIED_DISTINCT below) are not re-reported unless a new name joins them.

Usage:
    cd ~/.hermes/skills/productivity/cc-bill-review
    python3 scripts/find_duplicate_merchants.py                # full sweep, all cards
    python3 scripts/find_duplicate_merchants.py --card 5999    # one card
    python3 scripts/find_duplicate_merchants.py --json          # machine-readable output

False-positive guard: clusters where the names share a `PTE`/`LTD`/`SINGAPORE`
token but NOT a distinctive (len>=5) token, and where the Jaccard overlap is
< 0.5, are skipped. The script does NOT modify the database.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent / "cc_review.db"

# Compared against LOWERCASED tokens (this used to be uppercase, so it never
# applied and "PTE"/"LTD"/"SINGAPORE" alone could pair unrelated shops).
STOP = {w.lower() for w in (
    "SINGAPORE", "PTE", "LTD", "THE", "AND", "FOR", "BY", "OF", "IN", "AT", "CO",
    "SDN", "BHD", "SGP", "SG", "SGD", "FOREIGN", "CURRENCY", "USD", "EUR", "JPY",
    "GBP", "AUD", "MYR", "AFTER", "AFTERS", "AFTER-MKT", "AFTERMARKET",
)}

# Clusters the user/agent verified as DIFFERENT shops (e.g. per-merchant sub-account
# codes differ) — see references/duplicate-merchant-sweep.md "False positives".
# A cluster is suppressed only if its names are exactly one of these sets.
VERIFIED_DISTINCT = {
    ("5999", frozenset({"GRAB* A-8RWXUPAGWVI SINGAPORE", "GRAB* AZPAKGNPMCCJ- SINGAPORE"})),
}

# Skip names matching any of these patterns — they are app stores / payment
# processors / generic tokens that legitimately fragment by region.
SYSTEMY_PATTERNS = (
    "CASH REBATE", "PAYMENT BY INTERNET", "PAYMENT INTERNET",
    "CCY CONVERSION FEE", "ANNUAL FEE", "ANNUAL FEE REVERSAL",
    "NETS FLASHPAY", "STEAMGAMES", "OPENAI", "CHATGPT",
    "PAYPAL *", "GOOGLE *", "APPLE.COM", "SPOTIFY", "NANONOBLE",
)


def tokens(name: str) -> list[str]:
    raw = re.split(r"[^A-Za-z0-9]+", name.lower())
    return [t for t in raw if t and t not in STOP and len(t) >= 3]


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_systemy(name: str) -> bool:
    up = name.upper()
    return any(p in up for p in SYSTEMY_PATTERNS)


def find_clusters(card_suffix: str | None = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = "WHERE card_suffix = ?" if card_suffix else ""
        params = (card_suffix,) if card_suffix else ()
        rows = conn.execute(f"""
            SELECT card_suffix, merchant_normalized,
                   COUNT(*) AS txn_count,
                   MIN(txn_date) AS first_seen,
                   MAX(txn_date) AS last_seen,
                   SUM(amount) AS total
            FROM transactions
            {where}
            GROUP BY card_suffix, merchant_normalized
        """, params).fetchall()
    finally:
        conn.close()

    by_card: dict[str, list] = defaultdict(list)
    for r in rows:
        by_card[r["card_suffix"]].append(r)

    out = []
    for card, members in by_card.items():
        n = len(members)
        parent = list(range(n))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                ni, nj = members[i]["merchant_normalized"], members[j]["merchant_normalized"]
                if ni == nj:
                    union(i, j)
                    continue
                if is_systemy(ni) or is_systemy(nj):
                    continue
                ti, tj = tokens(ni), tokens(nj)
                jv = jaccard(ti, tj)
                shared_long = {t for t in set(ti) & set(tj) if len(t) >= 5}
                short = ni.upper().replace(" ", "").replace("SINGAPORE", "").replace("PTE", "")
                shortj = nj.upper().replace(" ", "").replace("SINGAPORE", "").replace("PTE", "")
                prefix_match = (
                    (short.startswith(shortj) or shortj.startswith(short))
                    and min(len(short), len(shortj)) >= 6
                )
                if jv >= 0.5 or (shared_long and jv >= 0.4) or prefix_match:
                    union(i, j)

        clusters: dict[int, list] = defaultdict(list)
        for i in range(n):
            clusters[find(i)].append(members[i])

        for members_in_cluster in clusters.values():
            names = sorted({m["merchant_normalized"] for m in members_in_cluster})
            if len(names) < 2 or (card, frozenset(names)) in VERIFIED_DISTINCT:
                continue
            out.append({
                "card": card,
                "names": names,
                "txn_count": sum(m["txn_count"] for m in members_in_cluster),
                "total": round(sum(m["total"] for m in members_in_cluster), 2),
                "first_seen": min(m["first_seen"] for m in members_in_cluster),
                "last_seen": max(m["last_seen"] for m in members_in_cluster),
            })

    out.sort(key=lambda c: (c["first_seen"], -c["total"]))
    if out:
        conn = sqlite3.connect(DB_PATH)
        try:
            for c in out:
                c["evidence"] = {
                    n: sorted({r[0] for r in conn.execute(
                        "SELECT merchant_raw FROM transactions WHERE card_suffix=? AND "
                        "merchant_normalized=?", (c["card"], n))})
                    for n in c["names"]}
        finally:
            conn.close()
    return out


def _terminal(raw: str) -> str:
    m = re.match(r"^\s*-(\d{3,5})\b", raw)
    return f"channel code -{m.group(1)}" if m else "no channel code"


def print_human(clusters: list[dict]) -> None:
    if not clusters:
        print("No duplicate clusters found. ✅")
        return
    print(f"Found {len(clusters)} potential duplicate cluster(s)\n")
    for c in clusters:
        print(f"--- card {c['card']} | {c['first_seen']} → {c['last_seen']} | "
              f"{len(c['names'])} names | {c['txn_count']} txns | S${c['total']:.2f}")
        for n in c["names"]:
            print(f"     '{n}'")
            for raw in c.get("evidence", {}).get(n, [])[:4]:
                print(f"         raw: {' '.join(raw.split())[:70]!r}  ({_terminal(raw)})")
        print()
    print("Judge each cluster on the NAMES — the -NNNN channel code is shared by many "
          "different shops and proves nothing. Ask the user before merging (fix recipe: "
          "references/duplicate-merchant-sweep.md); verified-different clusters go in "
          "VERIFIED_DISTINCT.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--card", help="restrict to one card (e.g. 5999 or 7008)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    clusters = find_clusters(args.card)
    if args.json:
        print(json.dumps(clusters, indent=2, default=str))
    else:
        print_human(clusters)
    return 0


if __name__ == "__main__":
    sys.exit(main())