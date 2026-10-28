#!/usr/bin/env python3
"""Covers the READ_LATER hard gate, the Unsure ambiguity gate, and Discord
sanitization. Complements test_vip_golden_rule.py."""
import os, sys, importlib.util

spec = importlib.util.spec_from_file_location(
    "gt", os.environ.get("GMAIL_TRIAGE_PATH", "/home/amh/.hermes/scripts/gmail_triage.py"))
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)

fail = 0
def check(name, cond, detail=""):
    global fail
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fail += 1

RL = "centre_admin@myfirstskool.com"

# 1) READ_LATER gate beats CATEGORY_PROMOTIONS (the 2026-05-19 bug class).
m = {"from": f"My First Skool <{RL}>", "subject": "Special offer & enrolment",
     "snippet": "unsubscribe promo deal", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "offer deal unsubscribe")
check("READ_LATER beats CATEGORY_PROMOTIONS", c == "ReadLater", f"got {c!r}")

# 2) READ_LATER gate beats CATEGORY_UPDATES + offer.
m = {"from": RL, "subject": "Offer of place", "snippet": "offer renewal", "labels": ["CATEGORY_UPDATES"]}
c = gt.categorize(m, "offer renewal")
check("READ_LATER beats CATEGORY_UPDATES+offer", c == "ReadLater", f"got {c!r}")

# 3) READ_LATER beats receipt/promo subject keywords.
m = {"from": RL, "subject": "Invoice / receipt for term fees", "snippet": "", "labels": []}
c = gt.categorize(m, "invoice receipt")
check("READ_LATER beats receipt keywords", c == "ReadLater", f"got {c!r}")

# 3b) READ_LATER now matches the whole @myfirstskool.com DOMAIN, not just
#     centre_admin@ (2026-07-06). Any local-part, incl. promo-labeled, → ReadLater.
m = {"from": "Ms Tan <teacher.tan@myfirstskool.com>", "subject": "Special offer & enrolment",
     "snippet": "unsubscribe promo deal", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "offer deal unsubscribe")
check("new local-part @myfirstskool.com → ReadLater (domain rule)", c == "ReadLater", f"got {c!r}")
m = {"from": "billing@myfirstskool.com", "subject": "Your order confirmation receipt #123",
     "snippet": "", "labels": ["CATEGORY_UPDATES"]}
c = gt.categorize(m, "")
check("billing@myfirstskool.com + receipt subj → ReadLater", c == "ReadLater", f"got {c!r}")
# subdomain still matches
m = {"from": "admin@centre.myfirstskool.com", "subject": "flash sale", "snippet": "", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "flash sale")
check("subdomain @centre.myfirstskool.com → ReadLater", c == "ReadLater", f"got {c!r}")
# the original centre_admin@ address still resolves ReadLater via the domain rule
m = {"from": f"My First Skool <{RL}>", "subject": "Offer of place", "snippet": "", "labels": []}
c = gt.categorize(m, "")
check("original centre_admin@ still ReadLater", c == "ReadLater", f"got {c!r}")
# a look-alike domain must NOT match (substring/suffix safety)
m = {"from": "deals@notmyfirstskool.com", "subject": "flash sale 50% off", "snippet": "unsubscribe",
     "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "flash sale unsubscribe")
check("look-alike notmyfirstskool.com → NOT ReadLater (Auto-filed)", c == "Auto-filed", f"got {c!r}")

# 3c) READ_LATER_SUBJECT_RULES — OCBC credit card e-Statement is NEVER Auto-filed
#     (2026-09-05). Sender-scoped + concept-level subject match, so a reworded
#     subject still lands in ReadLater.
OCBC = "documents@ocbc.com"
for subj in ["The e-Statement for your credit card is ready",
             "Your Credit Card e-Statement is now available",
             "Your credit card eStatement for August 2026",
             "OCBC Credit Card Statement — ready to view",
             "YOUR CARD E-STATEMENT IS READY",
             "e-Statement ready for your OCBC 365 Credit Card"]:
    m = {"from": f"OCBC Bank <{OCBC}>", "subject": subj, "snippet": "unsubscribe view statement",
         "labels": ["CATEGORY_UPDATES"]}
    c = gt.categorize(m, "unsubscribe view your statement online")
    check(f"OCBC e-Statement {subj!r} → ReadLater", c == "ReadLater", f"got {c!r}")
# ...even when Gmail mis-tags it CATEGORY_PROMOTIONS and the body is promo-heavy.
m = {"from": f"OCBC <{OCBC}>", "subject": "The e-Statement for your credit card is ready",
     "snippet": "special offer unsubscribe", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "special offer deal unsubscribe")
check("OCBC e-Statement + CATEGORY_PROMOTIONS → ReadLater (not Auto-filed)",
      c == "ReadLater", f"got {c!r}")
# The rule is subject-scoped: other documents@ocbc.com marketing still auto-files.
m = {"from": f"OCBC <{OCBC}>", "subject": "Flash sale: 50% off dining deals",
     "snippet": "unsubscribe", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "flash sale unsubscribe")
check("documents@ocbc.com promo (no statement subject) → still Auto-filed",
      c == "Auto-filed", f"got {c!r}")
# ...and it is sender-scoped: a look-alike/phishing sender gets no free pass.
m = {"from": "OCBC <documents@ocbc-secure.com>",
     "subject": "The e-Statement for your credit card is ready",
     "snippet": "unsubscribe", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "unsubscribe")
check("look-alike ocbc-secure.com e-Statement → NOT ReadLater via this gate",
      c != "ReadLater", f"got {c!r}")
# Half a match (card but no statement) must not fire the rule.
check("'card' alone does not match the rule",
      gt.matches_read_later_subject_rule(OCBC, "Your credit card is on its way") is False)
check("'statement' alone does not match the rule",
      gt.matches_read_later_subject_rule(OCBC, "Your account statement is ready") is False)

# 4) Unsure (CRITICAL tier): promo-labeled email with security language — flagged
#    even from a non-human sender (phishing-looking-like-promo).
m = {"from": "deals@unknown.com", "subject": "Flash sale 50% off",
     "snippet": "your password reset is required", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "password reset required")
check("promo + security (any sender) → Unsure", c == "Unsure", f"got {c!r}")

# 5) Unsure does NOT fire on a plain promo (no Action signal).
m = {"from": "deals@unknown.com", "subject": "Flash sale 50% off", "snippet": "unsubscribe",
     "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "flash sale unsubscribe")
check("plain promo stays Auto-filed (not Unsure)", c == "Auto-filed", f"got {c!r}")

# 6) Unsure does NOT fire on a plain security email (no autofile conflict).
m = {"from": "alerts@bank.com", "subject": "Unusual login attempt", "snippet": "", "labels": []}
c = gt.categorize(m, "unusual activity login attempt")
check("plain security email → Action (not Unsure)", c == "Action", f"got {c!r}")

# 7) VIP is never Unsure (gate precedence).
m = {"from": "penny lee <akanestar@hotmail.com>", "subject": "invoice payment failed",
     "snippet": "", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "payment failed invoice")
check("VIP never Unsure / never Auto-filed", c in ("Action", "ReadLater"), f"got {c!r}")

# 7b) SOFT tier, HUMAN sender: promo subject + 'please' → Unsure.
m = {"from": "Jane Doe <jane.doe@gmail.com>", "subject": "Flash sale 50% off",
     "snippet": "please don't miss out", "labels": []}
c = gt.categorize(m, "please don't miss out")
check("promo + 'please' from human → Unsure", c == "Unsure", f"got {c!r}")

# 7c) SOFT tier, HUMAN sender: forwarded promo (reply prefix) → Unsure.
m = {"from": "John Tan <john.tan@gmail.com>", "subject": "Fwd: Mega sale this weekend",
     "snippet": "", "labels": []}
c = gt.categorize(m, "")
check("forwarded promo (Fwd:) from human → Unsure", c == "Unsure", f"got {c!r}")

# 7d) SOFT tier, BULK sender: foodpanda receipt with 'please' → NOT Unsure (suppressed).
m = {"from": "no-reply@foodpanda.sg", "subject": "Thank you for your order!",
     "snippet": "please rate your order", "labels": []}
c = gt.categorize(m, "please rate your order")
check("bulk-sender receipt + 'please' → NOT Unsure", c == "Auto-filed", f"got {c!r}")

# 7e) CATEGORY_PROMOTIONS overrides SOFT tier even for a dotted "human-looking"
#     corporate sender (the 2026-05-30 sc.singapore <ADV> incident).
m = {"from": "Standard Chartered Singapore <sc.singapore@sc.com>",
     "subject": "<ADV> Pay smarter for what you've already spent",
     "snippet": "important information applies", "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "important information applies")
check("CATEGORY_PROMOTIONS + dotted corp sender + 'important' → Auto-filed (not Unsure)",
      c == "Auto-filed", f"got {c!r}")
check("  ...and is_human_sender now correctly rejects the dotted corp sender too",
      gt.is_human_sender(m) is False)

# 7f) CRITICAL tier STILL overrides CATEGORY_PROMOTIONS (phishing in promo) → Unsure.
m = {"from": "Standard Chartered Singapore <sc.singapore@sc.com>",
     "subject": "<ADV> Account notice", "snippet": "your password reset is required",
     "labels": ["CATEGORY_PROMOTIONS"]}
c = gt.categorize(m, "password reset required")
check("CATEGORY_PROMOTIONS + security language → Unsure (critical tier)", c == "Unsure", f"got {c!r}")

# 8) Allowlists — is_human_sender positives (zero false-positive risk).
for em, who in [("hector90@gmail.com","Hector"),("mandyoh@cdl.com.sg","Mandy"),
                ("agastabenil@canaan.com.sg","Agasta")]:
    check(f"KNOWN_HUMANS {who} → human",
          gt.is_human_sender({"from": f"{who} <{em}>", "labels": ["CATEGORY_PERSONAL"]}) is True)

gt.KNOWN_CORRESPONDENTS = {"may.loh@income.com.sg"}
check("Sent-correspondent → human (overrides CATEGORY_UPDATES)",
      gt.is_human_sender({"from":"May Loh <may.loh@income.com.sg>","labels":["CATEGORY_UPDATES"]}) is True)
check("non-correspondent bulk → still non-human",
      gt.is_human_sender({"from":"sc.singapore@sc.com","labels":["CATEGORY_PROMOTIONS"]}) is False)
gt.KNOWN_CORRESPONDENTS = set()
check("org display-name (clinic) → non-human when not a correspondent",
      gt.is_human_sender({"from":'"Apple Wellness Center" <applewellness.one@fullertonhealth.com>',
                          "labels":["CATEGORY_PERSONAL"]}) is False)

# 9) Thread participation — a replier inside a thread ham sent in is human, even
#    with a no-dot, non-allowlisted address (the Oh-Mandy-via-contact-us pattern).
gt.KNOWN_SENT_THREADS = {"T1"}
check("replier in a thread ham sent in → human",
      gt.is_human_sender({"from":"Some Replier <replier@randomco.com>",
                          "labels":["CATEGORY_UPDATES"], "threadId":"T1"}) is True)
check("same sender outside that thread → non-human",
      gt.is_human_sender({"from":"Some Replier <replier@randomco.com>",
                          "labels":["CATEGORY_UPDATES"], "threadId":"T2"}) is False)
gt.KNOWN_SENT_THREADS = set()

# 10) Action-phrase branch requires a human sender (the 99.co regression).
m = {"from":'"99.co" <hello@e.99.co>', "subject":"Monthly Update for Coco Palms",
     "snippet":"important update", "labels":["CATEGORY_UPDATES"]}
check("non-human sender + 'important' in body → Auto-filed (not Action)",
      gt.categorize(m, "important monthly update") == "Auto-filed", f"got {gt.categorize(m,'important monthly update')!r}")

# 11) GOV_AD_SENDERS override the .gov.sg→ReadLater rule (CPF InTouch marketing).
m = {"from":"CPF Board <intouch@e.cpf.gov.sg>", "subject":"Your CPF updates",
     "snippet":"important news", "labels":["CATEGORY_UPDATES"]}
check("GOV_AD_SENDERS (CPF InTouch) → Auto-filed (not ReadLater)",
      gt.categorize(m, "important news") == "Auto-filed", f"got {gt.categorize(m,'important news')!r}")
# a normal .gov.sg sender is still ReadLater
m = {"from":"notifications@nlb.gov.sg", "subject":"Library notice", "snippet":"", "labels":[]}
check("normal .gov.sg → still ReadLater", gt.categorize(m, "") == "ReadLater", f"got {gt.categorize(m,'')!r}")
# .gov.sg must NOT be auto-filed even when Gmail mis-tags it CATEGORY_PROMOTIONS
# (LifeSG enrolment-consent email, 2026-06-01) — the domain gate sits above the
# CATEGORY_PROMOTIONS → Auto-filed branch.
m = {"from":"LifeSG <notifications@life.gov.sg>", "subject":"ECDA: Your consent required for Form 1",
     "snippet":"", "labels":["IMPORTANT","CATEGORY_PROMOTIONS"]}
check(".gov.sg + CATEGORY_PROMOTIONS → ReadLater (not Auto-filed)",
      gt.categorize(m, "") == "ReadLater", f"got {gt.categorize(m,'')!r}")
# .gov.sg + CATEGORY_UPDATES + promo body must also stay ReadLater
m = {"from":"LifeSG <notifications@life.gov.sg>", "subject":"Your offer update",
     "snippet":"unsubscribe", "labels":["CATEGORY_UPDATES"]}
check(".gov.sg + CATEGORY_UPDATES + promo body → ReadLater (not Auto-filed)",
      gt.categorize(m, "unsubscribe offer") == "ReadLater", f"got {gt.categorize(m,'unsubscribe offer')!r}")
# .gov.sg with security language still escalates to Action
m = {"from":"Singpass <notifications@singpass.gov.sg>", "subject":"Unusual activity on your account",
     "snippet":"login attempt", "labels":[]}
check(".gov.sg + security language → Action",
      gt.categorize(m, "login attempt") == "Action", f"got {gt.categorize(m,'login attempt')!r}")

# 8) sanitize_discord neutralizes mentions, newlines, backticks.
s = gt.sanitize_discord("@everyone `code`\nsecond line")
check("sanitize defangs @everyone", "@everyone" not in s, repr(s))
check("sanitize strips newlines", "\n" not in s, repr(s))
check("sanitize escapes backticks", "`" not in s, repr(s))

# 8b) sanitize_discord also neutralizes Discord masked links and bare autolinks
#     so an attacker-controlled subject cannot inject a clickable link into the
#     trusted digest (2026-06-06).
s = gt.sanitize_discord("click [here](http://evil.example/phish) now")
check("sanitize breaks masked-link ](", "](" not in s, repr(s))
check("sanitize breaks bare autolink ://", "://" not in s, repr(s))

# 12) bug 2026-06-06: a real human is NEVER auto-filed on a bare promo/receipt
#     subject keyword. "offer"/"weekly"/"invoice"/"sale" are high-false-positive
#     words in legitimate personal mail; a known human falls through to the Action
#     default instead of being archived.
for subj in ["Special offer on the condo unit", "Weekly project update",
             "Invoice for last month", "Big sale this Friday"]:
    m = {"from": "Hector Sim <hector90@gmail.com>", "subject": subj, "snippet": "", "labels": []}
    c = gt.categorize(m, "")
    check(f"human + promo/receipt subject {subj!r} → not Auto-filed", c != "Auto-filed", f"got {c!r}")
# ...and a non-human with the same keyword is STILL Auto-filed (no over-correction).
m = {"from": "deals@megashop.com", "subject": "Special offer just for you", "snippet": "", "labels": []}
check("non-human + 'offer' subject → still Auto-filed",
      gt.categorize(m, "") == "Auto-filed", f"got {gt.categorize(m, '')!r}")

# ══ 2026-09-28 tune-up regressions ══════════════════════════════════════════
def cat(frm, subj, labels=(), body=""):
    return gt.categorize({"from": frm, "subject": subj, "snippet": "", "labels": list(labels)}, body)

# Security alerts from no-reply senders → Action (were archived by the no-reply branch).
for frm, subj in [("Google <no-reply@accounts.google.com>", "Critical security alert for x@gmail.com"),
                  ("Google <no-reply@accounts.google.com>", "Security alert"),
                  ("Yahoo <no-reply@cc.yahoo.com>", "Unexpected sign-in attempt"),
                  ("Reddit <noreply@reddit.com>", "Reddit password reset"),
                  ("Dropbox <no-reply@dropbox.com>", "Hi Anthony, we noticed a new sign in to your Dropbox account"),
                  ("Ticketmaster <tickets@info.ticketmaster.sg>", "Your Password Has Been Updated")]:
    c = cat(frm, subj, ["CATEGORY_UPDATES"])
    check(f"no-reply security alert → Action: {subj[:40]!r}", c == "Action", f"got {c!r}")
c = cat("Google <no-reply@accounts.google.com>", "Security\xa0alert", ["CATEGORY_UPDATES"])
check("NBSP in subject still matches security keyword", c == "Action", f"got {c!r}")
for frm, subj in [("Epic <noreply@acct-auth.epicgames.com>", "Your two-factor sign in code"),
                  ("Hilton <noreply@h6.hilton.com>", "Your verification code from Hilton"),
                  ("GOMO <no-reply@digital.gomo.sg>", "Your one-time password from GOMO")]:
    c = cat(frm, subj, ["CATEGORY_UPDATES"])
    check(f"one-time/verification code stays Auto-filed: {subj[:36]!r}", c == "Auto-filed", f"got {c!r}")
c = cat("Shop <deals@shop.com>", "Security alert: 50% off locks", ["CATEGORY_PROMOTIONS"])
check("PROMOTIONS + security subject → Unsure (ambiguity gate, unchanged)", c == "Unsure", f"got {c!r}")

# Renewals → ReadLater even from no-reply (ham 2026-05-10).
for subj in ["Your GOMO plan has renewed", "Your subscription renewal notice",
             "Your contract is expiring - renew soon", "Token Plan Expiry Reminder"]:
    c = cat("GOMO <no-reply@digital.gomo.sg>", subj, ["CATEGORY_UPDATES"])
    check(f"renewal → ReadLater: {subj!r}", c == "ReadLater", f"got {c!r}")
for subj in ["Your 300 Stellar Jades are about to expire", "Your Primogems ×300 are expiring soon"]:
    c = cat("Pom-Pom <noreply.news@e-mail.hoyoverse.com>", subj, ["CATEGORY_UPDATES"])
    check(f"game-points expiry stays Auto-filed: {subj[:30]!r}", c == "Auto-filed", f"got {c!r}")
c = cat("Shop <deals@shop.com>", "Renew your membership — 50% off", ["CATEGORY_PROMOTIONS"])
check("PROMOTIONS renewal promo stays Auto-filed", c == "Auto-filed", f"got {c!r}")

# OCBC card transaction rule (was dead: regex never matched "SGD38.41").
OC = "noreply@notify.ocbc.com"
def ocbody(amt): return f"dear valued customer we wish to inform you that {amt} was charged at 16:25 on 28-sep-26 to your card (-5999)"
check("OCBC SGD38.41 → Auto-filed", cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("sgd38.41")) == "Auto-filed")
check("OCBC SGD 250.00 → ReadLater", cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("sgd 250.00")) == "ReadLater")
check("OCBC SGD1,250.00 → ReadLater", cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("sgd1,250.00")) == "ReadLater")
check("OCBC USD5.00 → ReadLater", cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("usd5.00")) == "ReadLater")
check("OCBC SGD100.00 exactly → Auto-filed", cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("sgd100.00")) == "Auto-filed")
c = cat("documents@ocbc.com", "OCBC Notification: The e-Statement for your deposit account is ready", ["CATEGORY_UPDATES"], "balance sgd 12,000.00")
check("OCBC deposit e-Statement still Auto-filed (ham 2026-09-05)", c == "Auto-filed", f"got {c!r}")
c = cat("Notifications@ocbc.com", "We have processed your funds transfer request", ["CATEGORY_UPDATES"], "sgd 500.00")
check("OCBC funds-transfer notice unaffected (not a 'transaction' subject)", c == "Auto-filed", f"got {c!r}")
check("extract_amounts both orders", gt.extract_amounts("sgd38.41 and 5.00 usd and s$12") ==
      [("SGD", 38.41), ("USD", 5.0), ("SGD", 12.0)], str(gt.extract_amounts("sgd38.41 and 5.00 usd and s$12")))

# is_human_sender: whole-token org words (Vincent/Lincoln/Corpuz are people).
for disp in ["Vincent Tan", "Lincoln Lee", "Grace Corpuz", "Bankole Ade"]:
    m = {"from": f"{disp} <{disp.lower().replace(' ', '.')}@gmail.com>", "subject": "Lunch?", "labels": []}
    check(f"human with org-substring name: {disp}", gt.is_human_sender(m) is True)
m = {"from": "Raffles Medical Group <appt.reminder@rafflesmedical.com>", "subject": "x", "labels": []}
check("real org display name still non-human", gt.is_human_sender(m) is False)
m = {"from": "Acme Pte Ltd <john.lim@acme.com>", "subject": "x", "labels": []}
check("'Pte Ltd' display still non-human", gt.is_human_sender(m) is False)

# extract_email edge cases (VIP gate bypass).
check("extract_email trailing space", gt.extract_email("Mom <henny_1961@yahoo.com> ") == "henny_1961@yahoo.com")
check("extract_email (comment) form", gt.extract_email("henny_1961@yahoo.com (Mom)") == "henny_1961@yahoo.com")
check("extract_email still takes the LAST <...> (no display-name VIP forge)",
      gt.extract_email('"akanestar@hotmail.com" <attacker@evil.com>') == "attacker@evil.com")
c = cat("Mom <henny_1961@yahoo.com> ", "50% off sale", ["CATEGORY_PROMOTIONS"])
check("VIP with trailing-space header never Auto-filed", c in ("Action", "ReadLater"), f"got {c!r}")

# .gov.sg is a domain test, not a substring.
check("look-alike x@iras.gov.sg.phish.xyz is NOT gov", gt.is_gov_sg({"from": "x@iras.gov.sg.phish.xyz"}) is False)
check("x@iras.gov.sg is gov", gt.is_gov_sg({"from": "IRAS <x@iras.gov.sg>"}) is True)

# None fields don't crash categorize().
try:
    c = gt.categorize({"from": None, "subject": None, "snippet": None, "labels": None}, None)
    check("None fields → no crash", c in ("Auto-filed", "Action", "ReadLater", "Unsure"), f"got {c!r}")
except Exception as e:
    check("None fields → no crash", False, repr(e))

# Discord caps.
check("discord_len counts astral emoji as 2", gt.discord_len("📬a") == 3)
txt = gt.fit_lines(["H"], [f"• `x{i}@y.com`: " + "s" * 90 for i in range(60)])
check("fit_lines caps at 2000 with '…and N more'", gt.discord_len(txt) <= 2000 and "…and" in txt, str(gt.discord_len(txt)))

# ── gate lap 1 (2026-09-28) ──
for disp, addr in [("Online Banking", "ocbc.online@ocbc.com"), ("Singtel Notifications", "mysingtel.bill@singtel.com"),
                   ("City Clinics", "front.desk@cityclinics.sg")]:
    check(f"plural/derived org word still non-human: {disp}",
          gt.is_human_sender({"from": f"{disp} <{addr}>", "labels": []}) is False)
for frm, subj, lab in [("news@brand.com", "The Renewal Issue — our weekly newsletter", ["CATEGORY_UPDATES"]),
                       ("hello@shop.com", "Renew your wardrobe: 30% off sale", []),
                       ("InTouch <intouch@e.cpf.gov.sg>", "Renew your commitment to saving", [])]:
    c = cat(frm, subj, lab)
    check(f"promo/gov-ad 'renew' subject stays Auto-filed: {subj[:32]!r}", c == "Auto-filed", f"got {c!r}")
c = cat("marketing@pwmgr.com", "Password changed? Get 50% off our password manager", ["CATEGORY_PROMOTIONS"])
check("PROMOTIONS + extra security phrase → Unsure (not Action)", c == "Unsure", f"got {c!r}")
c = cat("Yahoo <no-reply@cc.yahoo.com>", "Unexpected sign-in attempt", ["CATEGORY_PROMOTIONS"])
check("PROMOTIONS + hyphenated 'sign-in attempt' → Unsure", c == "Unsure", f"got {c!r}")
c = cat(OC, "Card Transaction Alert", ["CATEGORY_UPDATES"], ocbody("sgd38.41") + " your credit limit is s$10,000")
check("OCBC: only the charged (first) amount counts, not a limit footer", c == "Auto-filed", f"got {c!r}")

# gate lap 2
check("surname 'Banks' is not an org", gt.is_human_sender({"from": "Tyra Banks <tyra.banks@gmail.com>", "labels": []}) is True)
for subj in ["Your weekly meal plan has renewed", "Domain renewal: offered price locked in", "Your plan renews tomorrow - ideal time to review"]:
    c = cat("billing <no-reply@service.com>", subj, ["CATEGORY_UPDATES"])
    check(f"renewal with promo-substring words → ReadLater: {subj[:30]!r}", c == "ReadLater", f"got {c!r}")

print(f"\n{'ALL PASS' if fail==0 else f'{fail} FAILURE(S)'}")
sys.exit(1 if fail else 0)
