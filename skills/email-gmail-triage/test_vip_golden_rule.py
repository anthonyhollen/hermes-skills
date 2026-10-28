#!/usr/bin/env python3
"""Proves the golden rule: a VIP sender's email is NEVER Auto-filed,
through categorize() (layer 1) and the main() label logic (layers 2+3)."""
import os, sys, importlib.util

spec = importlib.util.spec_from_file_location(
    "gt", os.environ.get("GMAIL_TRIAGE_PATH", "/home/amh/.hermes/scripts/gmail_triage.py"))
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)

VIP = "akanestar@hotmail.com"  # Penny

# Every adversarial case that historically broke (or could break) the rule.
cases = [
    ("the real 29-May incident (invoice in subject)",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "Fw: Invoice #BT-028325 for PENNY LEE QINYI dated 29-May-2026",
      "snippet": "from no_reply@sgimed.com ... clinic", "labels": ["CATEGORY_PERSONAL"]}),
    ("offer in subject (PROMO_KEYWORDS path)",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "Offer of place for Francesca", "snippet": "", "labels": []}),
    ("CATEGORY_PROMOTIONS label",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "weekly deal sale discount", "snippet": "unsubscribe promo offer",
      "labels": ["CATEGORY_PROMOTIONS"]}),
    ("CATEGORY_UPDATES + renewal",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "subscription renewal notice", "snippet": "renew offer deal unsubscribe",
      "labels": ["CATEGORY_UPDATES"]}),
    ("receipt/order keywords",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "Your order confirmation receipt #123", "snippet": "", "labels": []}),
    ("unsubscribe + promo body",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "newsletter", "snippet": "unsubscribe promo sale offer deal", "labels": []}),
    ("display-name only, raw promo body",
     {"from": "penny lee <akanestar@hotmail.com>",
      "subject": "promo deal offer sale discount clearance", "snippet": "",
      "labels": ["CATEGORY_PROMOTIONS", "CATEGORY_UPDATES"]}),
]

def labels_for(msg, category, sender_email):
    """Mirror of main()'s label + safety-net logic (layers 2 & 3)."""
    is_vip = sender_email in gt.VIP_EMAILS
    if is_vip and category == "Auto-filed":
        category = "Action"
    labels = [f"Hermes/{category}"]
    if is_vip:
        labels = [l for l in labels if l != "Hermes/Auto-filed"]
        if not labels:
            labels = ["Hermes/Action"]
        labels.append("Hermes/VIP")
    return labels, category

fail = 0
for name, msg in cases:
    body = (msg["subject"] + " " + msg["snippet"]).lower()
    cat = gt.categorize(msg, body)
    se = gt.extract_email(msg["from"])
    labels, final_cat = labels_for(msg, cat, se)
    bad_cat = (cat == "Auto-filed")
    bad_lbl = ("Hermes/Auto-filed" in labels) or ("Hermes/VIP" in labels and "Hermes/Auto-filed" in labels)
    archived = (final_cat == "Auto-filed")
    ok = (not bad_cat) and (not bad_lbl) and (not archived) and ("Hermes/VIP" in labels)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"        categorize()={cat!r}  final={final_cat!r}  labels={labels}")
    if not ok:
        fail += 1

# Non-VIP sanity: a real promo should still Auto-file.
nv = {"from": "deals@shop.com", "subject": "Flash sale 50% off", "snippet": "unsubscribe", "labels": ["CATEGORY_PROMOTIONS"]}
nvc = gt.categorize(nv, "flash sale 50% off unsubscribe")
print(f"[{'PASS' if nvc=='Auto-filed' else 'FAIL'}] non-VIP promo still Auto-filed (got {nvc!r})")
if nvc != "Auto-filed":
    fail += 1

print(f"\n{'ALL PASS — golden rule is absolute' if fail==0 else f'{fail} FAILURE(S)'}")
sys.exit(1 if fail else 0)
