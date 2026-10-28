#!/usr/bin/env python3
"""
Gmail Triage Script — Hermes Email Triage System
Categories: Action, ReadLater, VIP, Auto-filed
Actions: apply labels, archive auto-filed, track state
"""

import json, os, re, sys, subprocess, fcntl, time
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
GAPI_SCRIPT = f"{HERMES_HOME}/skills/productivity/google-workspace/scripts/google_api.py"
STATE_FILE = Path(f"{HERMES_HOME}/scripts/gmail_triage_state.json")
LABEL_CACHE_FILE = Path(f"{HERMES_HOME}/scripts/gmail_label_cache.json")
CORRESPONDENTS_FILE = Path(f"{HERMES_HOME}/scripts/gmail_correspondents_cache.json")
DISCORD_CHANNEL = "1499972245437550602"
DISCORD_MAX = 2000        # Discord bot message content limit, in UTF-16 code units

# Time budget. The scheduler SIGKILLs a no_agent script at the GLOBAL
# cron.script_timeout_seconds (600s on this Pi). Each google_api.py call is capped
# at GAPI_TIMEOUT and no new call / message is started after RUN_BUDGET. After the
# budget there can still be one in-flight call (≤90s) plus the two Discord posts,
# which are not budget-gated (each ≤ 15s + ≤10.25s 429 wait + 15s ≈ 41s), so the
# worst case is ≈ 360 + 90 + 2×41 ≈ 532s < 600s (gate-measured: the old 420 budget
# reached ~580s). The run ends on its own, state already persisted per message.
GAPI_TIMEOUT = 90
RUN_BUDGET = 360
_RUN_START = time.monotonic()

# Sent-folder harvest: a full 1-year pass once a day, otherwise an incremental
# 2-day pass (the cache is a grow-only union, so incremental never loses anyone).
# The full pass was ~15s of every ~23s hourly run.
FULL_HARVEST_EVERY_H = 24

SGT = timezone(timedelta(hours=8))

# VIP senders
VIP_EMAILS = {
    "akanestar@hotmail.com": "Penny",
    "henny_1961@yahoo.com": "Mom",
    "ferry_hollen@yahoo.com": "Pop",
    "valeriehollen@gmail.com": "Valerie",
    "traxgen@gmail.com": "Virgilio",
}

# Explicit Read Later senders — a single address is always ReadLater. Empty by
# default; add exact addresses here when a domain-wide rule is too broad.
READ_LATER_EMAILS = {
}

# Explicit Read Later domains — ANY sender at these domains (any local-part,
# plus subdomains) is always ReadLater. My First Skool sends enrolment/admin
# mail from several addresses at this domain (centre_admin@, billing, teachers,
# …), so we match the whole domain rather than one address. Superseded the
# single centre_admin@myfirstskool.com entry (2026-07-06).
READ_LATER_DOMAINS = {
    "myfirstskool.com": "My First Skool",
}

# Explicit Read Later sender + SUBJECT rules — for a sender whose mail is only
# always-ReadLater for one KIND of subject (the rest of their mail keeps falling
# through the normal pipeline, so we don't need a blanket address/domain rule).
# Maps an exact sender address → (description, [required subject patterns]).
# ALL patterns in the list must match for the rule to fire, and they run against
# the subject normalized by normalize_subject() (lowercased, every run of
# non-alphanumerics collapsed to one space) — so "e-Statement", "e Statement"
# and "eStatement" all reduce to something the same pattern matches.
# Deliberately loose on wording: OCBC rewords this subject line over time
# ("The e-Statement for your credit card is ready" today), so we match the
# concept — a card + a statement — not the sentence (2026-09-05).
READ_LATER_SUBJECT_RULES = {
    "documents@ocbc.com": (
        "OCBC credit card e-Statement",
        [r"\bcards?\b", r"\be?statements?\b"],
    ),
}

# Gov advertising senders — bulk promotional mail from gov agencies. These
# override the `.gov.sg → ReadLater` rule and are Auto-filed.
GOV_AD_SENDERS = {
    "intouch@e.cpf.gov.sg": "CPF InTouch (marketing blasts)",
    "edm@experience.nlb.gov.sg": "NLB eDM (marketing blasts)",
}

# Known real people ham corresponds with IRL but whose address the Sent-folder
# harvest may not capture (e.g. inbound-only, or emailed from another account).
# Allowlist — zero false-positive risk. Add addresses here as they come up.
KNOWN_HUMANS = {
    "hector90@gmail.com": "Hector Sim",
    "mandyoh@cdl.com.sg": "Oh Mandy",
    "agastabenil@canaan.com.sg": "Agasta",
}

# Addresses ham has SENT mail to — harvested from the Sent mailbox at runtime
# (see load_correspondents). Anyone ham actively emails is treated as human.
# Populated in main(); empty by default so imports/tests are deterministic.
KNOWN_CORRESPONDENTS = set()

# Thread IDs ham has participated in (sent at least one message). Anyone who
# replies within such a thread is a real correspondent — even if they reply
# from a different address than the one ham wrote to (e.g. a named human
# answering a general "contact us" inbox). Populated in main().
KNOWN_SENT_THREADS = set()

# Local-part segments that signal a role/org/automated sender, not a person.
NONHUMAN_NAME_TOKENS = {
    "noreply", "reply", "donotreply", "auto", "autoreply", "mailer", "mail",
    "support", "care", "customercare", "service", "services", "help", "helpdesk",
    "info", "admin", "system", "team", "marketing", "promo", "promotions", "news",
    "newsletter", "notification", "notifications", "notify", "alert", "alerts",
    "account", "accounts", "billing", "invoice", "statement", "estatement",
    "booking", "bookings", "appointment", "appointments", "sales", "contact",
    "enquiry", "feedback", "office", "sg", "singapore", "asia", "global",
    "official", "updates",
}
# Org/brand words that appear in company DISPLAY names, never in a person's name.
ORG_DISPLAY_TOKENS = (
    "center", "centre", "clinic", "medical", "hospital", "wellness", "bank",
    "insurance", "pte", "ltd", "inc", "llc", "gmbh", "corporation", "corp",
    "co.", "group", "official", "do not reply", "donotreply", "no-reply",
    "noreply", "notification", "subsystem",
)
# Explicit plural/derived forms (a generic s/ing suffix made the surname in
# "Tyra Banks" an org — gate lap 2).
ORG_DISPLAY_FORMS = ("centers", "centres", "clinics", "hospitals", "banking", "groups",
                     "notifications")
# Whole-token match: a plain substring test made "Vincent" an org ("inc"),
# "Corpuz" ("corp"), "Lincoln" ("inc"), "Bankole" ("bank") (2026-09-28).
_ORG_DISPLAY_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in ORG_DISPLAY_TOKENS + ORG_DISPLAY_FORMS)
    + r")(?![a-z0-9])")

# Keyword patterns (case-insensitive)
SECURITY_KEYWORDS = [
    "suspicious activity", "unusual activity", "login attempt", "sign in attempt",
    "password reset", "reset your password", "forgot password", "change password",
    "security alert", "security notice", "account locked", "unrecognized device",
]
BILLING_KEYWORDS = [
    "payment failed", "payment declined", "card declined", "renewal failed",
    "unable to process payment", "billing failure", "charge failed",
    "transaction failed", "payment not processed",
]
REPLY_PREFIXES = ["re:", "fwd:", "aw:"]
AUTOFILLED_SENDER_PREFIXES = ["no-reply", "noreply", "donotreply", "no_reply"]
SOCIAL_DOMAINS = ["linkedin.com", "notifications@facebookmail.com", "facebookmail.com", "meta.com"]
# Patterns that indicate a real human sender (specific name in address, not generic/marketing)
HUMAN_SENDER_SIGNALS = ["boon.chonghao"]  # known human senders outside VIP list
TRAVEL_KEYWORDS = ["booking confirmation", "flight confirmation", "hotel reservation",
                   "flight reminder", "check-in", "boarding pass", "agoda", "booking.com",
                   "travel", "airline"]
TOTO_KEYWORDS = ["toto", "singapore pools", "draw result", "lottery"]
RECEIPT_KEYWORDS = ["receipt", "order confirmation", "your order", "purchase complete",
                    "order #", "order no", "invoice", "payment receipt"]
PROMO_KEYWORDS = ["newsletter", "digest", "weekly roundup", "weekly", "promo", "deal", "offer",
                  "sale", "discount", "promotion", "marketing", " unsubscribe ", "<adv>"]
# Renewal/contract emails from no-reply addresses should NOT be auto-filed
RENEWAL_KEYWORDS = ["renewal", "renew", "contract expiration", "expiring", "subscription renewal",
                    "renewing", "auto-renew", "renew soon", "renewal notice", "contract renewal"]
# Renewal-of-service SUBJECTS → ReadLater, from any non-promo sender incl. no-reply
# (ham, 2026-05-10; enforced 2026-09-28). Matched on the normalize_subject() form.
# Deliberately narrower than RENEWAL_KEYWORDS: a bare "expiring"/"expire" is mostly
# game/points promos ("your Stellar Jades are about to expire"), not a service renewal.
# Whole-word promo test for the renewal gate ("ideal"/"offered" are not promos;
# "weekly" alone is not either — a renewing weekly plan is still a renewal).
_PROMO_WORD_RE = re.compile(r"\b(?:promo|promotion|deal|deals|offer|offers|sale|discount|"
                            r"newsletter|marketing|\d+ off|off sale)\b")
RENEWAL_SUBJECT_RE = re.compile(r"\b(?:auto )?renew(?:al|als|ed|ing|s)?\b"
                                r"|\bcontract expir|\bsubscription expir|\bplan expir")
# Security/billing alerts matched on the normalized SUBJECT → Action, above every
# Auto-filed path, so a no-reply account-security alert is never archived
# (ham, 2026-09-28: Google "Critical security alert", Yahoo "Unexpected sign-in
# attempt", Reddit "password reset", Dropbox "new sign in" were all Auto-filed).
# One-time/verification codes are intentionally NOT listed — they stay Auto-filed.
SECURITY_SUBJECT_EXTRA = ["new sign in", "password has been updated", "password is updated",
                          "password was changed", "password has been changed", "password changed"]
# Personal-action language (someone asking you to do something)
ACTION_PHRASES = ["please", "could you", "would you", "kindly", "help me", "need your",
                  "requested", "asap", "urgent", "important", "appreciate"]

# ─── Helpers ─────────────────────────────────────────────────────────────

def load_env():
    env_path = Path(f"{HERMES_HOME}/.env")
    vars_ = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vars_[k.strip()] = v.strip().strip("'\"").strip()
    return vars_

def past_budget() -> bool:
    return time.monotonic() - _RUN_START > RUN_BUDGET

def write_json_atomic(path: Path, data, **kw):
    """temp file in the same dir → flush+fsync → os.replace (atomic on POSIX), so a
    reader never sees a torn file. Used for every file this script writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def run_gapi(args: list, retries: int = 3) -> list:
    last_detail = ""
    for attempt in range(retries):
        # Never START a call past the run budget (see RUN_BUDGET) — the caller
        # treats this like any other API failure and leaves the item for next run.
        if past_budget():
            raise RuntimeError(f"run budget ({RUN_BUDGET}s) exhausted; not starting gapi {args[:2]}")
        try:
            result = subprocess.run(
                [sys.executable, GAPI_SCRIPT] + args,
                capture_output=True, text=True, timeout=GAPI_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            last_detail = f"timed out after {GAPI_TIMEOUT}s"
            if attempt < retries - 1 and not past_budget():
                time.sleep(2 ** attempt)
            continue
        if result.returncode == 0:
            stdout = result.stdout.strip()
            if stdout.startswith("[") or stdout.startswith("{"):
                try:
                    parsed = json.loads(stdout)
                except json.JSONDecodeError as e:
                    # Truncated/garbled JSON (flaky API, interrupted write) — retry,
                    # then fail loudly. A JSONDecodeError must never abort the run.
                    last_detail = f"malformed JSON ({e}): {stdout[:200]}"
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(
                        f"gapi returned unparseable JSON after {retries} attempts: {last_detail}")
                # Guard against error dicts leaking through (e.g. {"error": "..."})
                if isinstance(parsed, dict) and "error" in parsed:
                    raise RuntimeError(f"Gmail API error: {parsed['error']}")
                return parsed
            elif stdout == "No messages found." or stdout == "":
                return []  # No results is not an error
            else:
                # rc==0 but output is neither JSON nor a known sentinel. Do NOT
                # silently retry — this can be a mutating call (modify/archive)
                # that already succeeded, and re-issuing it is wrong. Surface it.
                raise RuntimeError(f"gapi unexpected stdout (rc=0): {stdout[:200]}")
        last_detail = (result.stderr or result.stdout or "").strip()[:300]
        if attempt < retries - 1 and not past_budget():
            time.sleep(2 ** attempt)  # exponential backoff
    raise RuntimeError(f"gapi error after {retries} attempts: {last_detail}")

def get_gmail_messages(query: str, max_results: int = 50, retries: int = 3) -> list:
    return run_gapi(["gmail", "search", query, "--max", str(max_results)], retries=retries)

def get_message_body(msg_id: str):
    """Lowercased first 3000 chars of the body, or None if the fetch FAILED
    (API error, timeout, run budget). None ≠ empty: the caller defers the email
    to the next run instead of categorizing it on a missing body."""
    try:
        msg = run_gapi(["gmail", "get", msg_id])
        return ((msg.get("body") if isinstance(msg, dict) else "") or "")[:3000].lower()
    except Exception as e:
        print(f"    [warn] body fetch failed for [{msg_id}]: {e}", file=sys.stderr)
        return None

def extract_email(addr: str) -> str:
    """Bare lowercase address from a From/To header value. Uses parseaddr so
    'Name <a@b> ' (trailing space) and 'a@b (Name)' resolve correctly — the old
    split-on-'<' kept a trailing '>'/'(name)' and missed the VIP gate. Still takes
    the LAST <...> for 'x@y <attacker@evil>', so a display name can't forge VIP."""
    addr = addr or ""
    if "<" in addr:
        return addr.split("<")[-1].split(">")[0].strip().lower()
    parsed = parseaddr(addr)[1]
    return (parsed or addr).strip().lower()

_CUR = r"sgd|usd|eur|gbp|aud|jpy|cad|chf|myr|thb|php|vnd|idr|hkd|cny|rmb|krw|twd|nzd|inr"
_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"
# Currency code before OR after the number, with or without a space: OCBC writes
# "SGD38.41 was charged" (no space, code first) — the old pattern only matched
# "38.41 sgd" and so never saw a real OCBC amount (2026-09-28). "S$" counts as SGD.
_AMOUNT_RE = re.compile(rf"(?<![a-z])({_CUR}|s\$)\s?({_NUM})|({_NUM})\s?({_CUR})(?![a-z])", re.I)

def extract_amounts(text: str) -> list:
    """[(CURRENCY, value)] pairs found in text, currency upper-cased, S$ → SGD."""
    out = []
    for m in _AMOUNT_RE.finditer(text or ""):
        cur, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        cur = "SGD" if cur.lower() == "s$" else cur.upper()
        try:
            out.append((cur, float(num.replace(",", ""))))
        except ValueError:
            continue
    return out

def normalize_subject(subject: str) -> str:
    """Lowercase a subject and collapse every non-alphanumeric run to one space.

    Makes subject matching robust to the punctuation/casing a sender changes
    between sends: "e-Statement", "e Statement" and "E-STATEMENT" all become
    "e statement"; "eStatement" becomes "estatement".
    """
    return re.sub(r"[^a-z0-9]+", " ", subject.lower()).strip()

def matches_read_later_subject_rule(sender: str, subject: str) -> bool:
    """Does this sender+subject hit a READ_LATER_SUBJECT_RULES entry?

    Sender must match exactly; then EVERY pattern for that sender must be found
    in the normalized subject. Subject only — never the body (golden rule).
    """
    rule = READ_LATER_SUBJECT_RULES.get(sender)
    if not rule:
        return False
    _desc, patterns = rule
    norm = normalize_subject(subject)
    return all(re.search(p, norm) for p in patterns)

def is_toto_email(msg) -> bool:
    sender = (msg.get("from") or "").lower()
    subject = (msg.get("subject") or "").lower()
    from_pattern = "singaporepools" in sender
    subject_pattern = any(k in subject for k in TOTO_KEYWORDS)
    return from_pattern or subject_pattern

def is_travel_email(msg, body: str) -> bool:
    sender = (msg.get("from") or "").lower()
    subject = (msg.get("subject") or "").lower()
    travel_senders = ["agoda", "booking.com", "airbnb", "expedia", "航班",
                      "singapore airlines", "scoot", "jetstar", "malindo",
                      "lufthansa", "united airlines", "delta", "american airlines",
                      "cathay pacific", "malaysia airlines"]
    return (any(s in sender for s in travel_senders) or
            any(k in subject for k in TRAVEL_KEYWORDS))

def is_ocbc_transaction(msg, body: str) -> bool:
    """OCBC card/account TRANSACTION alert in a non-SGD currency or over SGD 100.

    Scoped to subjects containing "transaction" (e.g. "Card Transaction Alert"):
    funds-transfer notices and the documents@ocbc.com e-Statements keep their own
    routing (deposit/CPFIA statements auto-file by ham's choice, 2026-09-05).
    """
    sender = extract_email(msg.get("from") or "")
    subject = (msg.get("subject") or "").lower()
    if "ocbc" not in sender or "transaction" not in subject:
        return False
    # Only the FIRST amount — the charged amount leads the alert ("SGD38.41 was
    # charged at …"); later amounts can be a credit limit / promo footer.
    amounts = extract_amounts(body)[:1]
    return any(cur != "SGD" or val > 100 for cur, val in amounts)

def is_gov_sg(msg) -> bool:
    """Sender DOMAIN is gov.sg or a subdomain of it. (Was a substring test, so
    x@iras.gov.sg.phish.xyz counted as government mail.)"""
    sender = extract_email(msg.get("from") or "")
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    return domain == "gov.sg" or domain.endswith(".gov.sg")

def is_human_sender(msg) -> bool:
    """Is this email from a real person (not a role/marketing/automated sender)?

    Used to RESCUE borderline mail from auto-filing, so it errs toward False on
    uncertainty. Positive signals are allowlists (zero false-positive risk);
    negatives are layered (Gmail category, no-reply, role/place tokens, org
    display names). See backtest: 100% precision over 1 year of mail.
    """
    sender = extract_email(msg.get("from") or "")
    display = (msg.get("from") or "").split("<")[0].strip().strip('"').lower()
    labels = msg.get("labels") or []

    # ── Positive allowlists (definitive humans) ──────────────────────────
    if any(sig in sender for sig in HUMAN_SENDER_SIGNALS):
        return True
    if sender in VIP_EMAILS:
        return True
    if sender in KNOWN_HUMANS:
        return True
    if sender in KNOWN_CORRESPONDENTS:        # ham has emailed this address
        return True
    if msg.get("threadId") in KNOWN_SENT_THREADS:  # replied within a thread ham is in
        return True

    # ── Negative signals (not a person) ──────────────────────────────────
    if any(c in labels for c in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")):
        return False
    if any(prefix in sender for prefix in AUTOFILLED_SENDER_PREFIXES):
        return False
    if any(domain in sender for domain in SOCIAL_DOMAINS):
        return False
    if _ORG_DISPLAY_RE.search(display):
        return False
    name_part = sender.split("@")[0]
    generic_words = ["support", "team", "marketing", "promo", "newsletter", "noreply",
                     "donotreply", "no_reply", "no-reply", "autoreply", "auto-reply",
                     "info", "admin", "system"]
    if any(gw in name_part for gw in generic_words):
        return False
    if any(seg in NONHUMAN_NAME_TOKENS for seg in re.split(r"[._-]+", name_part)):
        return False

    # ── Positive structural guess: looks like FirstName.LastName ─────────
    if "." in name_part and "_" not in name_part and "-" not in name_part:
        return True
    return False

def categorize_vip(msg, body: str, combined: str) -> str:
    """Categorize a VIP sender's email.

    GOLDEN RULE (inviolable): this function returns ONLY 'Action' or 'ReadLater'
    — NEVER 'Auto-filed'. VIP senders are real family members; their mail must
    never be archived/marked-read regardless of subject keywords ('invoice',
    'offer', 'renewal', ...) or Gmail CATEGORY_* labels. There is intentionally
    no Auto-filed branch in this function — that is the whole point.
    """
    subject = (msg.get("subject") or "").lower()

    # Security / billing language → needs attention now.
    for kw in SECURITY_KEYWORDS + BILLING_KEYWORDS:
        if kw in combined:
            return "Action"

    # Reply / forward prefix (Re:/Fwd:/Aw:) → they sent it for you to see or act.
    for prefix in REPLY_PREFIXES:
        if subject.startswith(prefix):
            return "Action"

    # Clearly informational content → ReadLater (no action needed).
    if is_gov_sg(msg) or is_toto_email(msg) or is_ocbc_transaction(msg, body) or is_travel_email(msg, body):
        return "ReadLater"

    # Default for a real person: assume it wants your attention.
    return "Action"

def categorize(msg, body: str) -> str:
    sender_raw = msg.get("from") or ""
    sender = extract_email(sender_raw)
    # str.split() also splits on NBSP etc., so "Security\xa0alert" still matches.
    subject = " ".join((msg.get("subject") or "").split()).lower()
    snippet = (msg.get("snippet") or "").lower()
    labels = msg.get("labels") or []
    body = body or ""
    combined = subject + " " + snippet + " " + body
    subject_norm = normalize_subject(subject)

    # ══ ABSOLUTE VIP GATE (golden rule, layer 1) ════════════════════════════
    # A VIP sender's email can NEVER be Auto-filed. We short-circuit here —
    # BEFORE any Auto-filed return path below can run — and delegate to
    # categorize_vip(), which only ever returns Action or ReadLater.
    # Do NOT add VIP-specific exemptions to the branches below; this single
    # gate replaces all of them and is the only place VIP routing lives.
    if sender in VIP_EMAILS:
        return categorize_vip(msg, body, combined)
    # ════════════════════════════════════════════════════════════════════════

    # ══ EXPLICIT READ-LATER GATE (golden rule, layer 1) ═════════════════════
    # Same hard-gate guarantee as VIP: an explicit READ_LATER sender/domain is
    # ALWAYS ReadLater, short-circuited BEFORE any Auto-filed path below —
    # including the CATEGORY_PROMOTIONS / CATEGORY_UPDATES branches. This is the
    # canonical 2026-05-19 bug (a myfirstskool.com email auto-filed via a
    # CATEGORY_* label). Matches by exact address (READ_LATER_EMAILS) OR by
    # domain incl. subdomains (READ_LATER_DOMAINS) — My First Skool mails from
    # several local-parts, so the whole @myfirstskool.com domain is ReadLater
    # (2026-07-06). This gate is authoritative; do not rely on later checks.
    sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    if sender in READ_LATER_EMAILS or any(
        sender_domain == d or sender_domain.endswith("." + d)
        for d in READ_LATER_DOMAINS
    ):
        return "ReadLater"
    # ════════════════════════════════════════════════════════════════════════

    # ══ SENDER+SUBJECT READ-LATER GATE (golden rule, layer 1) ═══════════════
    # Third form of the same hard top gate, for senders that are only always-
    # ReadLater for one kind of subject. Sits ABOVE every Auto-filed path (and
    # above the ambiguity gate) so the verdict is deterministic ReadLater, never
    # Auto-filed and never Unsure. Currently: OCBC credit card e-Statements from
    # documents@ocbc.com — a no-reply-style bulk sender Gmail tags CATEGORY_UPDATES,
    # so without this gate the statement gets archived (2026-09-05). Matching is
    # concept-level (card + statement in the normalized subject), not the exact
    # sentence, because OCBC rewords the subject line between sends.
    if matches_read_later_subject_rule(sender, (msg.get("subject") or "")):
        return "ReadLater"
    # ════════════════════════════════════════════════════════════════════════

    # ══ .GOV.SG DOMAIN GATE (domain override, hard top gate) ════════════════
    # Authentic Singapore government mail (.gov.sg) is transactional, never
    # marketing junk — it must NEVER be Auto-filed, even when Gmail tags it
    # CATEGORY_PROMOTIONS/UPDATES. Gmail reclassified a LifeSG enrolment-consent
    # email as promotional, so it hit the CATEGORY_PROMOTIONS → Auto-filed
    # branch BEFORE the old mid-function is_gov_sg → ReadLater check ever ran
    # (2026-06-01). Same structural fix as the VIP / READ_LATER gates: the
    # domain override sits ABOVE every Auto-filed path. Exception: GOV_AD_SENDERS
    # (CPF InTouch blasts) are genuine bulk marketing and still auto-file — they
    # fall THROUGH this gate to the GOV_AD_SENDERS check below. Security/billing
    # language still escalates a gov email to Action.
    if is_gov_sg(msg) and sender not in GOV_AD_SENDERS:
        if any(k in combined for k in SECURITY_KEYWORDS + BILLING_KEYWORDS):
            return "Action"
        return "ReadLater"
    # ════════════════════════════════════════════════════════════════════════

    # ══ AMBIGUITY GATE — "if unsure, ask ham; don't guess" (golden rule) ════
    # When an email carries a strong Auto-filed signal AND an Action signal, the
    # category is contradictory. Rather than let the rule order silently guess,
    # defer: return "Unsure". main() then leaves it untouched (unread, in inbox,
    # no label) and asks ham in Discord. Two tiers of Action signal:
    #   • CRITICAL (security/billing language) → always ask, even from non-human
    #     senders — this is the classic phishing-looking-like-promo case.
    #   • SOFT (reply/forward prefix or personal-action phrases) → only ask when
    #     the sender looks human AND Gmail has NOT classified the mail as
    #     CATEGORY_PROMOTIONS. Gmail's promo verdict is authoritative (see the
    #     auto-file branch below); the SOFT tier must not rescue a promo into the
    #     queue. Without this guard, a corporate dotted sender (e.g.
    #     sc.singapore@sc.com — `is_human_sender` true on the dot) plus "important"
    #     in <ADV> boilerplate produced a false-positive Unsure (2026-05-30 15:01).
    strong_autofile = ("CATEGORY_PROMOTIONS" in labels
                       or any(k in subject for k in PROMO_KEYWORDS)
                       or any(k in subject for k in RECEIPT_KEYWORDS))
    sec_words = [normalize_subject(k) for k in SECURITY_KEYWORDS + BILLING_KEYWORDS + SECURITY_SUBJECT_EXTRA]
    subject_security = any(re.search(r"\b" + re.escape(k) + r"\b", subject_norm) for k in sec_words)
    critical_action = subject_security or any(k in combined for k in SECURITY_KEYWORDS + BILLING_KEYWORDS)
    soft_action = (any(subject.startswith(p) for p in REPLY_PREFIXES)
                   or any(phrase in combined for phrase in ACTION_PHRASES))
    soft_eligible = soft_action and is_human_sender(msg) and "CATEGORY_PROMOTIONS" not in labels
    if strong_autofile and (critical_action or soft_eligible):
        return "Unsure"
    # ════════════════════════════════════════════════════════════════════════

    # ══ SUBJECT-LEVEL ALERTS — above every Auto-filed path (2026-09-28) ═════
    # These used to sit BELOW the no-reply / CATEGORY_UPDATES Auto-filed returns,
    # so an account-security alert or a service renewal from a no-reply sender
    # was archived before its rule ran. Subject-only (normalized), never the body.
    # A CATEGORY_PROMOTIONS mail with security words never reaches here — the
    # ambiguity gate above already made it Unsure.
    if subject_security:
        return "Action"
    if "CATEGORY_PROMOTIONS" not in labels:
        # Renewal: not a promo-keyword subject ("Renew your wardrobe: 30% off")
        # and not a gov-ad blast (those stay Auto-filed).
        if (RENEWAL_SUBJECT_RE.search(subject_norm) and sender not in GOV_AD_SENDERS
                and not _PROMO_WORD_RE.search(subject_norm)):
            return "ReadLater"
        if is_ocbc_transaction(msg, body):
            return "ReadLater"
    # ════════════════════════════════════════════════════════════════════════

    # ── Gmail-native category signals (strong early exit) ─────────────────
    if "CATEGORY_PROMOTIONS" in labels:
        # Gmail's promo filter is reliable — no keyword check needed
        return "Auto-filed"

    if "CATEGORY_UPDATES" in labels:
        # Gmail's updates category (routine notifications) — cross-check with our rules.
        # (VIP senders never reach here — they are handled by the VIP gate above.)
        # Exempt Singapore Pools / Toto senders — they are transactional (not promo)
        # and should fall through to is_toto_email() for ReadLater classification.
        if "singaporepools" in sender or "singapore pools" in sender_raw.lower():
            pass  # don't auto-file here — let it fall through to Toto/SingaporePools checks
        else:
            promo_indicators = ["unsubscribe", "promo", "deal", "offer", "sale", "newsletter",
                                "digest", "receipt", "order confirmation"]
            if any(k in combined for k in promo_indicators):
                return "Auto-filed"

    # ── Auto-filed checks (early exit for clear promo/no-reply) ──────────────
    # No-reply sender prefix (but exempt renewal/contract emails — they need attention)
    for prefix in AUTOFILLED_SENDER_PREFIXES:
        if sender.startswith(prefix):
            if any(kw in combined for kw in RENEWAL_KEYWORDS):
                pass  # don't auto-file — fall through to other checks
            else:
                return "Auto-filed"

    # Social media domains
    for domain in SOCIAL_DOMAINS:
        if domain in sender:
            return "Auto-filed"

    # Promo / receipt keywords in subject (strong signal) — but NEVER auto-file a
    # real human on a bare subject keyword. "offer"/"weekly"/"invoice"/"order" are
    # high-false-positive words that appear in legitimate personal mail, so the
    # promo signal MUST be paired with a sender check (the documented rule: never
    # apply a promo keyword as a standalone signal). A known human falls through to
    # the Action default below instead of being archived.
    # (bug 2026-06-06: KNOWN_HUMANS/correspondent mail with "offer"/"weekly"/
    # "invoice" in the subject was being Auto-filed + archived.)
    if not is_human_sender(msg):
        for kw in PROMO_KEYWORDS:
            if kw in subject:
                return "Auto-filed"

        # Receipt keywords in subject
        for kw in RECEIPT_KEYWORDS:
            if kw in subject:
                return "Auto-filed"

    # Unsubscribe + promotional content (but not gov/linkedin/facebook OR human senders)
    if "unsubscribe" in combined:
        promo_signals = sum(1 for k in ["promo", "offer", "sale", "deal", "newsletter", "update"] if k in combined)
        if promo_signals >= 1:
            if not is_gov_sg(msg) and "linkedin" not in sender and "facebook" not in sender and not is_toto_email(msg):
                # Human senders with promo content → flag for review, don't auto-file
                if is_human_sender(msg):
                    return "Action"
                return "Auto-filed"

    # ── Action checks ──────────────────────────────────────────────────────
    for kw in SECURITY_KEYWORDS + BILLING_KEYWORDS:
        if kw in combined:
            return "Action"

    # Reply pattern
    for prefix in REPLY_PREFIXES:
        if subject.startswith(prefix):
            return "Action"

    # Human sender asking for action (strong personal-action language).
    # MUST require is_human_sender — non-human senders (e.g. hello@e.99.co) use
    # words like "important" in marketing bodies; without this guard they were
    # wrongly flagged Action (2026-05-30). Mirrors the unsubscribe+promo branch.
    if any(phrase in combined for phrase in ACTION_PHRASES) and is_human_sender(msg):
        # Make sure it's not a promo/newsletter using similar language
        promo_count = sum(1 for k in PROMO_KEYWORDS if k in subject)
        if promo_count == 0 and "unsubscribe" not in combined[:500]:
            return "Action"

    # ── Read Later checks ─────────────────────────────────────────────────
    # Gov advertising senders override the .gov.sg → ReadLater rule: CPF InTouch
    # and similar are bulk marketing, not personal transactional mail (2026-05-30).
    if sender in GOV_AD_SENDERS:
        return "Auto-filed"

    # NOTE: non-GOV_AD_SENDERS .gov.sg senders are routed by the .gov.sg domain
    # gate at the top of categorize() — they never reach here.

    if is_toto_email(msg):
        return "ReadLater"

    if is_ocbc_transaction(msg, body):
        return "ReadLater"

    if is_travel_email(msg, body):
        return "ReadLater"

    # ── Default: Auto-filed (but human senders → flag for review) ──────────
    if is_human_sender(msg):
        return "Action"
    return "Auto-filed"

# ─── Label Management

def get_label_name_to_id_map() -> dict:
    """Fetch all Gmail labels and return a {name: id} mapping."""
    result = run_gapi(["gmail", "labels"])
    return {l["name"]: l["id"] for l in result}

def ensure_hermes_labels_simple() -> dict:
    """Read current labels, create Hermes labels if missing, return name->id map."""
    # The cache is rebuilt from Gmail every run; it is only a fallback for the
    # created-label ids. A torn/corrupt cache must not stop triage (it used to
    # raise JSONDecodeError here every hour).
    cache = {}
    try:
        if LABEL_CACHE_FILE.exists():
            with open(LABEL_CACHE_FILE) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cache = {k: v for k, v in loaded.items() if isinstance(v, str) and v != k}
    except (ValueError, OSError) as e:
        print(f"  [warn] label cache unreadable ({e}); rebuilding from Gmail.", file=sys.stderr)

    existing = get_label_name_to_id_map()
    label_names = ["Hermes/Action", "Hermes/ReadLater", "Hermes/VIP", "Hermes/Auto-filed"]

    for name in label_names:
        if name in existing:
            cache[name] = existing[name]
        else:
            # Create label via Gmail API
            try:
                from googleapiclient.discovery import build
                from google.oauth2.credentials import Credentials
                from pathlib import Path

                TOKEN_PATH = Path(HERMES_HOME) / "google_token.json"
                creds = Credentials.from_authorized_user_file(
                    str(TOKEN_PATH),
                    scopes=["https://www.googleapis.com/auth/gmail.modify"]
                )
                service = build("gmail", "v1", credentials=creds)
                created = service.users().labels().create(
                    userId="me",
                    body={"labelListVisibility": "labelShow",
                          "messageListVisibility": "show",
                          "name": name}
                ).execute()
                cache[name] = created["id"]
                print(f"  Created label '{name}' with id {created['id']}")
            except Exception as e:
                # No fallback to the NAME: Gmail's modify takes label IDS, so a
                # name "id" fails every modify silently. Leave it unmapped — an
                # email needing it is left untouched and retried next run.
                print(f"  [warn] Failed to create label '{name}': {e}", file=sys.stderr)
                cache.pop(name, None)

    try:
        write_json_atomic(LABEL_CACHE_FILE, cache, indent=2)
    except OSError as e:
        print(f"  [warn] could not write label cache: {e}", file=sys.stderr)
    return cache

# ─── State Management ──────────────────────────────────────────────────────

def lock_file(f):
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)

def unlock_file(f):
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def read_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            lock_file(f)
            try:
                return json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                # A truncated/corrupt state file (e.g. an old crash mid-write) must
                # NOT abort every future run. Log loudly and start from empty; the
                # write_state() at the end of this run rewrites a clean file. Safe:
                # the Gmail Hermes-label pre-check skips already-labeled mail, so
                # nothing is re-archived — at worst a still-unlabeled Unsure email
                # is re-surfaced once. (bug 2026-06-06)
                print(f"  [warn] state file corrupt ({e}); starting from empty state. "
                      f"File: {STATE_FILE}", file=sys.stderr)
                return {"last_run": None, "processed_ids": []}
            finally:
                unlock_file(f)
    return {"last_run": None, "processed_ids": []}

def write_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: serialize to a temp file in the same directory, flush+fsync,
    # then os.replace() into place. os.replace is atomic on POSIX, so a reader
    # (the digest, or the next triage run) always sees either the complete old
    # file or the complete new one — never a truncated/half-written file. This
    # replaces the old open("w")+flock approach, where "w" truncated the LIVE file
    # before the lock was taken, so a crash mid-dump (power loss / OOM kill) left
    # an empty/partial state that made every subsequent run abort in read_state.
    # (bug 2026-06-06)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)

# ─── Email Actions ─────────────────────────────────────────────────────────

HERMES_LABEL_NAMES = ["Hermes/Action", "Hermes/ReadLater", "Hermes/VIP", "Hermes/Auto-filed"]

def hermes_id_to_name(label_cache: dict) -> dict:
    """{Label_XX: "Hermes/..."} — Gmail returns label IDS on messages, never
    names. (Until 2026-09-28 the pre-check and the old-label removal tested
    l.startswith("Hermes/") against those ids, so both were dead code.)"""
    return {label_cache[n]: n for n in HERMES_LABEL_NAMES if label_cache.get(n)}

def apply_labels(msg_id: str, label_names: list, label_cache: dict, current_labels: list,
                 archive: bool = False) -> bool:
    """Set this message's Hermes labels in ONE modify call: add the new label ids,
    remove any other Hermes label ids it carries, and — for Auto-filed
    (archive=True) — also remove INBOX + UNREAD. One call is atomic: the email is
    either fully filed or left exactly as it was, so a failure can never archive
    an unlabeled email (bug 2026-06-06) nor leave a labeled one un-archived.
    Returns True iff the modify succeeded. False = definitely NOT applied (no id
    for a label). None = the modify call FAILED, which may still mean Gmail applied
    it (e.g. a timeout after the server committed) — the caller records the id in
    modify_unknown so the next run checks its real labels. Either way the email is
    not recorded now; an unapplied one is re-found in the inbox and retried."""
    add_ids = []
    for name in label_names:
        lid = label_cache.get(name)
        if not lid:
            print(f"    [warn] no Gmail id for label {name!r}; leaving [{msg_id}] for next run.",
                  file=sys.stderr)
            return False
        add_ids.append(lid)
    if not add_ids:
        return False
    hermes_ids = set(hermes_id_to_name(label_cache))
    remove_ids = [l for l in (current_labels or []) if l in hermes_ids and l not in add_ids]
    if archive:
        remove_ids += ["INBOX", "UNREAD"]
    args = ["gmail", "modify", msg_id, "--add-labels", ",".join(add_ids)]
    if remove_ids:
        args += ["--remove-labels", ",".join(remove_ids)]
    try:
        run_gapi(args)
        return True
    except Exception as e:
        print(f"    [warn] failed to apply labels {label_names} to [{msg_id}]: {e}", file=sys.stderr)
        return None

# ─── Correspondents (Sent-folder allowlist) ────────────────────────────────

_LAST_FULL_HARVEST = ""   # set by load_correspondents; preserved by later saves

def save_correspondents(addrs: set, threads: set, last_full: str = ""):
    global _LAST_FULL_HARVEST
    last_full = last_full or _LAST_FULL_HARVEST
    _LAST_FULL_HARVEST = last_full
    try:
        data = {"addresses": sorted(addrs), "sent_threads": sorted(threads)}
        if last_full:
            data["last_full_harvest"] = last_full
        write_json_atomic(CORRESPONDENTS_FILE, data, indent=2)
    except Exception as e:
        print(f"  [correspondents] could not save cache: {e}", file=sys.stderr)

def load_correspondents():
    """Return (addresses, sent_thread_ids) describing who ham corresponds with.

    - addresses: every address ham has SENT mail to (To/Cc), harvested from the
      Sent mailbox and union-cached so the set only ever grows.
    - sent_thread_ids: threads ham has participated in, so a human replying within
      one is recognized even when they reply from a different address than ham
      wrote to (e.g. answering a general "contact us" inbox).
    Best-effort: on API/IO error, falls back to cache. Accepts the legacy
    cache format (a bare list of addresses) for backward compatibility.
    Harvest is incremental (in:sent newer_than:2d) except for one full
    newer_than:1y pass per FULL_HARVEST_EVERY_H — safe because the cache is a
    grow-only union. The cache is only rewritten when something was actually
    harvested, so a failed harvest can never overwrite a good (or unreadable)
    cache with an empty one.
    """
    email_re = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    addrs, threads, last_full = set(), set(), ""
    cache_ok = False
    if CORRESPONDENTS_FILE.exists():
        try:
            data = json.load(open(CORRESPONDENTS_FILE))
            if isinstance(data, dict):
                addrs = set(data.get("addresses", []))
                threads = set(data.get("sent_threads", []))
                last_full = data.get("last_full_harvest", "") or ""
            elif isinstance(data, list):           # legacy format
                addrs = set(data)
            cache_ok = True
        except Exception as e:
            print(f"  [correspondents] cache unreadable ({e}); doing a full harvest.",
                  file=sys.stderr)
    now = datetime.now(SGT)
    full = True
    if cache_ok and last_full:
        try:
            age = now - datetime.strptime(last_full, "%Y/%m/%d %H:%M:%S").replace(tzinfo=SGT)
            full = not (timedelta(0) <= age < timedelta(hours=FULL_HARVEST_EVERY_H))
        except ValueError:
            full = True
    query, cap = ("in:sent newer_than:1y", 500) if full else ("in:sent newer_than:2d", 100)
    try:
        # ONE attempt: a slow full harvest retried 3× cost ~273s of every run
        # (gate lap 1). On failure we keep the cache and retry next hour.
        for msg in get_gmail_messages(query, max_results=cap, retries=1):
            if not isinstance(msg, dict):
                continue
            if msg.get("threadId"):
                threads.add(msg["threadId"])
            for field in ("to", "cc"):
                for em in email_re.findall(msg.get(field, "") or ""):
                    addrs.add(em.lower())
        if full:
            last_full = now.strftime("%Y/%m/%d %H:%M:%S")
        harvested = True
    except Exception as e:
        harvested = False
        print(f"  [correspondents] harvest failed, using cache "
              f"({len(addrs)} addrs / {len(threads)} threads): {e}", file=sys.stderr)
    addrs.discard("anthonyhollen@gmail.com")  # ham himself is not a correspondent
    global _LAST_FULL_HARVEST
    _LAST_FULL_HARVEST = last_full
    if harvested:
        save_correspondents(addrs, threads, last_full)
    return addrs, threads

# ─── Discord Reminder ─────────────────────────────────────────────────────

def sanitize_discord(text: str) -> str:
    """Neutralize untrusted email content before it is posted to Discord.

    Email subjects/senders are attacker-controllable. Without this, a subject
    like '@everyone ...' could ping the channel, and newlines/backticks could
    break the carefully-tuned digest formatting. Belt-and-suspenders alongside
    the allowed_mentions={"parse": []} on every send (which disables mention
    parsing server-side regardless)."""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("`", "'")          # don't let a stray backtick open a code span
    text = text.replace("](", "]​(")   # break [label](url) masked links
    text = text.replace("://", ":​//")  # break bare http(s):// autolinks
    text = text.replace("@", "@​")     # defang @everyone/@here/<@id> mentions
    return text.strip()

def discord_len(s: str) -> int:
    """Length as Discord counts it: UTF-16 code units (astral emoji = 2)."""
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)

def fit_lines(header: list, items: list, footer: list = (), limit: int = DISCORD_MAX) -> str:
    """Join header + as many item lines as fit + footer, replacing the rest with
    '…and N more' so the message never exceeds Discord's limit."""
    for k in range(len(items), -1, -1):
        body = list(header) + items[:k]
        if k < len(items):
            body.append(f"…and {len(items) - k} more")
        body += list(footer)
        text = "\n".join(body)
        if discord_len(text) <= limit:
            return text
    text = "\n".join(header)
    while discord_len(text) > limit:
        text = text[:-1]
    return text

def _never_sent(e: Exception) -> bool:
    """True only when the request provably never reached Discord: a connect
    timeout, or a failure to open the connection (DNS / refused). A reset AFTER
    the body was sent is also a ConnectionError — retrying that could duplicate
    the message, so it is not retried."""
    import requests
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    return any(k in repr(e) for k in ("NewConnectionError", "NameResolutionError",
                                        "Failed to resolve", "Connection refused"))

def _discord_post(bot_token: str, content: str) -> "tuple":
    """POST to Discord with mention parsing disabled. Returns (ok, detail).

    Retries ONCE, only where the message cannot already exist: HTTP 429 (honoring
    retry_after) and a connection error raised before the request was sent. A
    read timeout / 5xx is NOT retried — Discord may have created the message, and
    a re-POST would duplicate it."""
    import requests
    for attempt in range(2):
        try:
            resp = requests.post(
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages",
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "DiscordBot (hermes-agent/1.0)",
                },
                json={"content": content, "allowed_mentions": {"parse": []}},
                timeout=15,
            )
        except requests.exceptions.ConnectionError as e:
            if attempt == 0 and _never_sent(e):
                time.sleep(2)
                continue
            return False, f"connection error: {e}"
        if resp.status_code == 429 and attempt == 0:
            try:
                wait = float(resp.json().get("retry_after", 1))
            except Exception:
                wait = 1.0
            time.sleep(min(wait, 10) + 0.25)
            continue
        return 200 <= resp.status_code < 300, f"{resp.status_code} {resp.text[:100]}"
    return False, "retry exhausted"

def send_action_reminder(action_emails: list) -> bool:
    """Post the 9 PM reminder. Returns True when the reminder is DONE for today
    (sent, or nothing to remind about); False when it should be retried."""
    if not action_emails:
        print("  [reminder] No unread Action emails in inbox — nothing to remind.")
        return True
    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        print("  [reminder] No DISCORD_BOT_TOKEN, skipping reminder.", file=sys.stderr)
        return False

    # action_emails come from an `is:unread` inbox query, so no per-message
    # re-check is needed (it used to re-`get` every email).
    header = [f"**🔴 Action Reminder — {len(action_emails)} unread email(s) in inbox:**"]
    items = []
    for m in action_emails:
        sender = sanitize_discord(m.get("from") or "Unknown")[:100]
        subject = sanitize_discord(m.get("subject") or "")[:80]
        items.append(f"• `{sender}`: {subject}")

    try:
        ok, detail = _discord_post(bot_token, fit_lines(header, items))
    except Exception as e:
        ok, detail = False, str(e)
    if ok:
        print(f"  [reminder] Sent Discord reminder for {len(action_emails)} emails.")
    else:
        print(f"  [reminder] Failed: {detail}", file=sys.stderr)
    return ok

def send_unsure_query(unsure_emails: list) -> list:
    """Golden rule: when the categorizer is unsure, don't guess — ask ham.
    Posts a single batched Discord question listing the deferred emails.
    Returns the ids actually SHOWN in a delivered question ([] on failure); the
    caller keeps everything else pending and re-asks next run."""
    if not unsure_emails:
        return []
    env = load_env()
    bot_token = env.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        print("  [unsure] No DISCORD_BOT_TOKEN, skipping query.", file=sys.stderr)
        return []

    footer = ["_Left unread in your inbox, uncategorized. Tell me how to file these "
              "and I'll learn the rule._"]
    items = []
    for m in unsure_emails:
        sender = sanitize_discord(m.get("from") or "Unknown")[:100]
        subject = sanitize_discord(m.get("subject") or "")[:80]
        items.append(f"• `{sender}`: {subject}")
    # Show as many as fit in one message; the rest stay pending for the next run
    # (no "…and N more" here — those would be cleared without ever being shown).
    shown = len(items)
    while shown > 0:
        header = [f"**❓ Unsure — need your call ({shown} email(s)):**"]
        text = "\n".join(header + items[:shown] + footer)
        if discord_len(text) <= DISCORD_MAX:
            break
        shown -= 1
    if shown == 0:
        return []

    try:
        ok, detail = _discord_post(bot_token, text)
    except Exception as e:
        ok, detail = False, str(e)
    if ok:
        print(f"  [unsure] Sent Discord query for {shown} email(s)"
              + (f"; {len(items) - shown} more next run." if shown < len(items) else "."))
        return [m.get("id") for m in unsure_emails[:shown]]
    print(f"  [unsure] Failed: {detail} — will re-ask next run.", file=sys.stderr)
    return []

# ─── Main ─────────────────────────────────────────────────────────────────

EPOCH = "1970/01/01 00:00:00"
REMINDER_HOUR = 21

def main():
    now_sgt = datetime.now(SGT)
    now_str = now_sgt.strftime("%Y/%m/%d %H:%M:%S")
    print(f"[{now_sgt.strftime('%Y-%m-%d %H:%M:%S SGT')}] Gmail triage starting...")

    # Ensure Hermes labels exist
    print("  Ensuring Hermes labels...")
    label_cache = ensure_hermes_labels_simple()
    print(f"  Label cache: {label_cache}")
    hermes_ids = hermes_id_to_name(label_cache)

    # Build the known-correspondents allowlist (addresses ham has emailed) and
    # the set of threads ham has participated in (to recognize repliers).
    global KNOWN_CORRESPONDENTS, KNOWN_SENT_THREADS
    KNOWN_CORRESPONDENTS, KNOWN_SENT_THREADS = load_correspondents()
    print(f"  Correspondents: {len(KNOWN_CORRESPONDENTS)} addrs / "
          f"{len(KNOWN_SENT_THREADS)} threads | known humans: {len(KNOWN_HUMANS)}")
    learned = set()

    state = read_state()
    # Support both legacy flat-ID list and new [{id, category}] format
    raw_ids = state.get("processed_ids", [])
    if raw_ids and isinstance(raw_ids[0], str):
        processed_ids: set = set(raw_ids)
        processed_map: dict = {}  # id → category (empty for legacy)
        processed_meta: dict = {}  # id → {from, subject} for the digest body
    else:
        processed_ids = {e["id"] for e in raw_ids if isinstance(e, dict)}
        processed_map = {e["id"]: e.get("category", "") for e in raw_ids if isinstance(e, dict)}
        # Preserve any sender/subject already stored so re-writes don't drop them.
        processed_meta = {e["id"]: {"from": e.get("from", ""), "subject": e.get("subject", "")}
                          for e in raw_ids if isinstance(e, dict)}
    # Unsure ids whose Discord question has not been delivered yet (re-asked
    # every run until a post succeeds; bounded).
    unsure_pending = [i for i in state.get("unsure_pending", []) if isinstance(i, str)]
    # Emails whose modify call failed ambiguously last run (it may have been
    # applied server-side — an archived Auto-filed email is then no longer in the
    # inbox query and would be in no digest). Resolved below by reading labels.
    body_defers = {k: int(v) for k, v in (state.get("body_defers") or {}).items()
                   if isinstance(v, int)} if isinstance(state.get("body_defers"), dict) else {}
    modify_unknown = {k: v for k, v in (state.get("modify_unknown") or {}).items()
                      if isinstance(v, dict)} if isinstance(state.get("modify_unknown"), dict) else {}

    # IDs first triaged on THIS run. Only these are stamped with the current
    # triaged_at; everything else keeps its existing stamp (or EPOCH for
    # pre-feature / already-handled entries) so the digest watermark never
    # re-selects an old email. See persist() + gmail_report.py selection.
    new_this_run = {}          # id → triaged_at stamp (time it was FILED, not run start)
    existing_order = [e["id"] for e in raw_ids if isinstance(e, dict)]
    existing_by_id = {e["id"]: e for e in raw_ids if isinstance(e, dict)}

    def make_entry(pid):
        prev = existing_by_id.get(pid, {})
        meta = processed_meta.get(pid, {})
        entry = {
            "id": pid,
            # processed_map holds every id (loaded at start + updated in-loop);
            # fall back to the prior category so a legacy bare-string id (no
            # category) stays "" rather than becoming a rendered orphan — with
            # an EPOCH stamp it is simply never selected for a digest.
            "category": processed_map.get(pid, prev.get("category", "")),
            "from": meta.get("from") or prev.get("from", ""),
            "subject": meta.get("subject") or prev.get("subject", ""),
        }
        if prev.get("triaged_at"):
            entry["triaged_at"] = prev["triaged_at"]      # preserve original stamp
        elif pid in new_this_run:
            entry["triaged_at"] = new_this_run[pid]        # when it was filed this run
        else:
            entry["triaged_at"] = EPOCH                    # pre-existing / already handled
        return entry

    def persist():
        """Write state NOW. Called after every email that changed Gmail, so a
        crash/kill later in the run can't leave a labeled+archived email out of
        state (and out of every digest). Atomic via write_state()."""
        ordered_ids = list(existing_order)
        seen_ids = set(existing_order)
        for pid in processed_ids:
            if pid not in seen_ids:
                ordered_ids.append(pid)
                seen_ids.add(pid)
        state["last_run"] = now_str
        state["processed_ids"] = [make_entry(pid) for pid in ordered_ids][-5000:]
        if len(unsure_pending) > 50:
            print(f"  [unsure] pending queue over 50; dropping oldest {unsure_pending[:-50]}",
                  file=sys.stderr)
            del unsure_pending[:-50]
        state["unsure_pending"] = list(unsure_pending)
        state["modify_unknown"] = dict(list(modify_unknown.items())[-50:])
        state["body_defers"] = dict(list(body_defers.items())[-100:])
        write_state(state)

    for mid in list(modify_unknown):
        if mid in processed_ids:
            modify_unknown.pop(mid, None)
            continue
        try:
            m = run_gapi(["gmail", "get", mid], retries=1)
            if not isinstance(m, dict):
                raise RuntimeError(f"unexpected get result {type(m).__name__}")
        except Exception as e:
            meta = modify_unknown[mid]
            meta["tries"] = int(meta.get("tries") or 0) + 1
            if meta["tries"] >= 5:
                print(f"  [recover] giving up on [{mid}] after 5 failed checks ({e}).", file=sys.stderr)
                modify_unknown.pop(mid, None)
            else:
                print(f"  [recover] could not check [{mid}] ({e}); will retry.", file=sys.stderr)
            continue
        cats = [hermes_ids[l].replace("Hermes/", "") for l in (m.get("labels") or []) if l in hermes_ids]
        meta = modify_unknown.pop(mid)
        meta.pop("tries", None)
        if cats:
            # It WAS filed last run: record it as new so the digest shows it once.
            processed_ids.add(mid)
            new_this_run[mid] = datetime.now(SGT).strftime("%Y/%m/%d %H:%M:%S")
            processed_map[mid] = next((c for c in cats if c != "VIP"), cats[0])
            processed_meta[mid] = meta
            print(f"  [recover] [{mid}] was filed despite the failed call ({cats}); recorded.")
        # No Hermes label → it was not applied; the inbox query re-finds it.
    if modify_unknown or state.get("modify_unknown"):
        persist()

    # Query: all inbox emails from the last day (read + unread). processed_ids
    # dedupes by ID; the Hermes-label pre-check below catches anything already
    # labeled but missing from state (state reset/corruption).
    query = "in:inbox newer_than:1d"
    print(f"  Query: {query}")

    messages = get_gmail_messages(query, max_results=100)
    print(f"  Found {len(messages)} inbox email(s)")

    action_emails = []
    readlater_emails = []
    vip_emails = []
    autofiled_emails = []
    unsure_emails = []
    deferred = 0

    for msg in messages:
        # Skip garbage — validate msg is a dict with an id
        if not isinstance(msg, dict) or "id" not in msg:
            print(f"  [warn] Skipping invalid message: {msg}")
            continue

        msg_id = msg["id"]

        # Idempotency check: if already in processed_ids, skip
        if msg_id in processed_ids:
            continue

        if past_budget():
            deferred += 1
            continue

        # Gmail pre-check: already carries a Hermes label (compared by label ID —
        # Gmail never returns names) → it was triaged before but is missing from
        # state (state reset/corruption). Record it, don't re-label or re-archive.
        existing_labels = msg.get("labels") or []
        hermes_labels = [hermes_ids[l] for l in existing_labels if l in hermes_ids]
        if hermes_labels:
            # Handled on a prior run, so NOT added to new_this_run: it keeps an
            # EPOCH stamp and never re-enters a digest.
            processed_ids.add(msg_id)
            if modify_unknown.pop(msg_id, None) is not None:
                # Parked last run after an ambiguous modify: it WAS filed then and
                # never reported — stamp it as new so a digest shows it once.
                new_this_run[msg_id] = datetime.now(SGT).strftime("%Y/%m/%d %H:%M:%S")
            # Prefer the real category label over the VIP flag (VIP stacks on
            # Action/ReadLater; it is never a category on its own).
            cats = [l.replace("Hermes/", "") for l in hermes_labels]
            existing_cat = next((c for c in cats if c != "VIP"), cats[0] if cats else "")
            processed_map[msg_id] = existing_cat
            processed_meta[msg_id] = {"from": (msg.get("from") or ""), "subject": (msg.get("subject") or "")}
            print(f"  [skip] Already triaged [{msg_id}] with {hermes_labels}, adding to state.")
            continue

        sender_raw = msg.get("from") or ""
        sender_email = extract_email(sender_raw)
        subject = msg.get("subject") or ""
        print(f"  Processing: [{msg_id}] {sender_email} — {subject[:60]}")

        # Learn repliers: if this lands in a thread ham participated in, the sender
        # is a real correspondent — remember their address so even a future email
        # from them in a NEW thread is recognized as human.
        if msg.get("threadId") in KNOWN_SENT_THREADS and sender_email not in KNOWN_CORRESPONDENTS:
            KNOWN_CORRESPONDENTS.add(sender_email)
            learned.add(sender_email)
            print(f"    [learned] {sender_email} replied in a thread you're in — now a known correspondent.")

        body = get_message_body(msg_id)
        if body is None:
            n = body_defers[msg_id] = body_defers.get(msg_id, 0) + 1
            if n < 3 and not past_budget():
                print(f"    [defer] [{msg_id}] left untouched; retrying next run ({n}/3).", file=sys.stderr)
                deferred += 1
                persist()
                continue
            if past_budget():
                deferred += 1
                continue
            # Third consecutive failure: categorize on sender/subject/labels alone
            # rather than leave it unlabeled until it drops out of the 1-day query.
            print(f"    [defer] [{msg_id}] body unavailable 3×; categorizing without it.", file=sys.stderr)
            body = ""
        body_defers.pop(msg_id, None)
        category = categorize(msg, body)

        # ══ AMBIGUITY HANDLING — don't guess, ask ham (golden rule) ═════════
        # Leave the email completely untouched: unread, in inbox, NO Hermes
        # label and NOT archived. Record the id (so it is only categorized once)
        # and queue it in unsure_pending until the Discord question is delivered.
        # (VIP/READ_LATER senders can never be "Unsure" — they are resolved by
        # their gates in categorize().)
        if category == "Unsure":
            print(f"  [unsure] Deferring [{msg_id}] {sender_email} — {subject[:60]!r}; "
                  f"leaving untouched, will ask ham.")
            unsure_emails.append(msg)
            processed_ids.add(msg_id)
            new_this_run[msg_id] = datetime.now(SGT).strftime("%Y/%m/%d %H:%M:%S")
            processed_map[msg_id] = "Unsure"
            processed_meta[msg_id] = {"from": (msg.get("from") or ""), "subject": (msg.get("subject") or "")}
            if msg_id not in unsure_pending:
                unsure_pending.append(msg_id)
            persist()
            continue
        # ════════════════════════════════════════════════════════════════════

        is_vip = sender_email in VIP_EMAILS

        # ══ ABSOLUTE SAFETY NET (golden rule, layer 2) ══════════════════════
        # Defence in depth. Even if categorize() is ever changed and some new
        # Auto-filed path slips past the VIP gate, a VIP email must NEVER be
        # archived, marked read, or labelled Auto-filed. Force it to Action.
        if is_vip and category == "Auto-filed":
            print(f"  [GOLDEN-RULE GUARD] VIP {sender_email} was categorized "
                  f"Auto-filed — forcing to Action. Subject: {subject[:60]!r}",
                  file=sys.stderr)
            category = "Action"
        # ════════════════════════════════════════════════════════════════════

        # Determine labels
        labels_to_apply = [f"Hermes/{category}"]
        if is_vip:
            # Layer 3: strip any Auto-filed label outright; VIP never coexists
            # with Auto-filed. Then stack the VIP flag on top.
            labels_to_apply = [l for l in labels_to_apply if l != "Hermes/Auto-filed"]
            if not labels_to_apply:
                labels_to_apply = ["Hermes/Action"]
            labels_to_apply.append("Hermes/VIP")

        # Auto-filed = label + archive + mark read, in ONE atomic modify.
        archive = "Hermes/Auto-filed" in labels_to_apply
        labeled = apply_labels(msg_id, labels_to_apply, label_cache, existing_labels,
                               archive=archive)

        # If labeling failed, do NOT record the email in state: it is still in
        # the inbox with no Hermes label, so the next run re-finds it and retries.
        # (bug 2026-06-06)
        if not labeled:
            print(f"    [warn] labeling failed for [{msg_id}] (category={category}); "
                  f"leaving untouched, will retry next run.", file=sys.stderr)
            if labeled is None:
                modify_unknown[msg_id] = {"from": msg.get("from") or "", "subject": msg.get("subject") or ""}
                persist()
            continue

        if category == "Auto-filed":
            autofiled_emails.append(msg)
        elif category == "ReadLater":
            readlater_emails.append(msg)
        elif category == "Action":
            action_emails.append(msg)

        if is_vip:
            vip_emails.append(msg)

        processed_ids.add(msg_id)
        new_this_run[msg_id] = datetime.now(SGT).strftime("%Y/%m/%d %H:%M:%S")
        processed_map[msg_id] = category
        processed_meta[msg_id] = {"from": (msg.get("from") or ""), "subject": (msg.get("subject") or "")}
        persist()

    if deferred:
        print(f"  [defer] {deferred} email(s) left untouched for the next run "
              f"(body fetch failed or run budget {RUN_BUDGET}s reached).", file=sys.stderr)

    # Final write (also covers the pre-check additions and last_run).
    persist()

    # Persist any newly-learned correspondents (thread repliers)
    if learned:
        save_correspondents(KNOWN_CORRESPONDENTS, KNOWN_SENT_THREADS)
        print(f"  Learned {len(learned)} new correspondent(s): {sorted(learned)}")

    print(f"  Done: {len(action_emails)} Action, {len(readlater_emails)} ReadLater, "
          f"{len(vip_emails)} VIP, {len(autofiled_emails)} Auto-filed, "
          f"{len(unsure_emails)} Unsure")

    # Ask ham about anything we couldn't confidently categorize — including any
    # earlier Unsure whose question failed to post.
    if unsure_pending:
        pending_msgs = []
        for pid in unsure_pending:
            meta = processed_meta.get(pid, {})
            pending_msgs.append({"id": pid, "from": meta.get("from", ""),
                                 "subject": meta.get("subject", "")})
        shown = set(send_unsure_query(pending_msgs))
        if shown:
            unsure_pending[:] = [i for i in unsure_pending if i not in shown]
            state["unsure_attempts"] = 0
        else:
            # Bounded: a permanently-failing post (e.g. a 400) must not re-post
            # every hour forever. After 24 failed attempts drop the queue, loudly.
            state["unsure_attempts"] = int(state.get("unsure_attempts") or 0) + 1
            if state["unsure_attempts"] >= 24:
                print(f"  [unsure] giving up after 24 failed posts; dropping {unsure_pending} "
                      f"(still unread + unlabeled in the inbox).", file=sys.stderr)
                unsure_pending.clear()
                state["unsure_attempts"] = 0
        persist()

    # 9 PM SGT reminder — nag about EVERY unread Action email still sitting in the
    # inbox (bug 2026-06-06: fresh query, not this run's batch). Fires on the
    # FIRST run at/after 21:00 that hasn't delivered it today, so a late, failed
    # or crashed 21:00 run is caught up at 22:00/23:00 instead of skipped.
    today = now_sgt.strftime("%Y/%m/%d")
    if now_sgt.hour >= REMINDER_HOUR and state.get("reminder_date") != today:
        action_label_id = label_cache.get("Hermes/Action")
        try:
            if not action_label_id:
                raise RuntimeError("no Gmail id for Hermes/Action")
            # label:hermes-action (Gmail's search form of "Hermes/Action") so the
            # 100-result cap can't be filled by newer unread non-Action mail; the
            # label-ID filter stays as a backstop.
            unread_action = [
                m for m in get_gmail_messages("in:inbox is:unread label:hermes-action", max_results=100)
                if isinstance(m, dict) and action_label_id in (m.get("labels") or [])
            ]
            done = send_action_reminder(unread_action)
        except Exception as e:
            print(f"  [reminder] inbox query failed ({e}); will retry next run.", file=sys.stderr)
            done = False
        if done:
            state["reminder_date"] = today
            persist()

    end = datetime.now(SGT)
    print(f"[{end.strftime('%Y-%m-%d %H:%M:%S SGT')}] Triage complete "
          f"({(end - now_sgt).total_seconds():.0f}s).")

if __name__ == "__main__":
    main()
