# Category Detection Rules (relocated from SKILL.md, 2026-06-09)

The authoritative source is the `categorize()` function in `~/.hermes/scripts/gmail_triage.py`.
This reference documents the rules for debugging categorization issues.

## Order of evaluation in `categorize()` — each gate short-circuits

1. **VIP gate** (`VIP_EMAILS`) → `categorize_vip()` (Action/ReadLater only, NEVER Auto-filed)
2. **READ_LATER gate** (`READ_LATER_EMAILS` addresses + `READ_LATER_DOMAINS` domains) → ReadLater
2b. **READ_LATER sender+subject gate** (`READ_LATER_SUBJECT_RULES`) → ReadLater
3. **.gov.sg gate** (sender domain `gov.sg` or `*.gov.sg` — a domain test since 2026-09-28; it was a substring test, so `x@iras.gov.sg.phish.xyz` counted) → ReadLater (security/billing → Action), excluding `GOV_AD_SENDERS` which fall through
4. **Ambiguity gate** → Unsure (two-tier; see *If unsure* below)
4b. **Subject-level alerts** (2026-09-28) — see *Subject-level rules* below: security/billing subject → Action; renewal subject → ReadLater; OCBC transaction alert over the threshold → ReadLater
5. Auto-filed checks (Gmail `CATEGORY_PROMOTIONS`/`CATEGORY_UPDATES`, no-reply, promo/receipt subject)
6. Action checks (security/billing, reply prefix, **human** action-phrases)
7. `GOV_AD_SENDERS` → Auto-filed (CPF InTouch blasts), then Toto/OCBC/travel → ReadLater
8. Default: human sender → Action, else Auto-filed

> Gates 1–3 sit ABOVE every Auto-filed path — that is the structural fix for the VIP/READ_LATER/`.gov.sg` regressions. `is_human_sender()` is allowlist-first (see below).

## Auto-filed (Hermes/Auto-filed + archive)

- Gmail native `CATEGORY_PROMOTIONS` label present → immediate Auto-filed (no keyword check needed)
- `CATEGORY_UPDATES` + a promo indicator (`unsubscribe`, `promo`, `deal`, `offer`, `sale`, `newsletter`, `digest`, `receipt`, `order confirmation`) anywhere in subject/snippet/body → Auto-filed (Singapore Pools exempt)
- (`CATEGORY_SOCIAL` is *not* an Auto-filed rule — it only makes `is_human_sender()` false. Correction 2026-09-28: this doc used to list it as "immediate Auto-filed".)
- No-reply sender prefixes (`no-reply`, `noreply`, `donotreply`, `no_reply`)
- **Exception**: no-reply emails whose subject/snippet/body contains a renewal keyword (`RENEWAL_KEYWORDS`) skip *this one* branch. (Correction 2026-09-28: that skip alone never kept them out of Auto-filed — they fell to the next Auto-filed return. The renewal rule that actually works is the subject-level gate 4b.)
- Promotional keywords in subject (`PROMO_KEYWORDS`): `newsletter`, `digest`, `weekly roundup`, `weekly`, `promo`, `deal`, `offer`, `sale`, `discount`, `promotion`, `marketing`, ` unsubscribe `, `<adv>` — only for non-human senders
  > **⚠️ Pitfall:** `offer` is a high false-positive keyword — legitimate transactional emails from schools, employers, agencies routinely use "offer" in subjects. Always pair with sender identity checks, never apply `offer` as a standalone promo signal without also checking the sender.
- Receipt keywords in subject (`RECEIPT_KEYWORDS`: `receipt`, `order confirmation`, `your order`, `purchase complete`, `order #`, `order no`, `invoice`, `payment receipt`) — only for non-human senders
- `unsubscribe` + a promo signal in subject/snippet/body (non-gov, non-Toto; a human sender → Action instead)
- Social media domains (`SOCIAL_DOMAINS`, substring of the sender): `linkedin.com`, `facebookmail.com`, `meta.com` (Correction 2026-09-28: `instagram.com`/`x.com` were never in the code.)
- **Exception**: Real human senders (e.g. `FirstName.LastName@domain` pattern, known humans like Boon Chong Hao) are never auto-filed — they default to Action if nothing else matches. This prevents legitimate personal emails from being auto-archived.

## Read Later (Hermes/ReadLater)

- **Explicit senders/domains (hard top gate)**: `READ_LATER_EMAILS` (exact addresses) and `READ_LATER_DOMAINS` (whole domains incl. subdomains) → always ReadLater. Currently: any `@myfirstskool.com` sender (My First Skool, any local-part; widened from `centre_admin@myfirstskool.com` on 2026-07-06).
- **Explicit sender+subject (hard top gate, `READ_LATER_SUBJECT_RULES`)**: an exact sender whose mail is always ReadLater *for one kind of subject only*. Currently `documents@ocbc.com` + a credit-card e-Statement subject (2026-09-05). The subject is normalized by `normalize_subject()` (lowercased, non-alphanumeric runs → single space) and must match ALL of the rule's patterns — here `\bcards?\b` AND `\be?statements?\b`, so "The e-Statement for your credit card is ready", "Your Credit Card eStatement is available" and "OCBC Credit Card Statement" all fire. Deliberately concept-level: OCBC rewords the line between sends. Subject only, never the body (golden rule). Other mail from `documents@ocbc.com` (dining promos etc.) falls through and still auto-files.
- **Exception (`GOV_AD_SENDERS`)**: gov advertising senders (`intouch@e.cpf.gov.sg` CPF InTouch blasts, `edm@experience.nlb.gov.sg` NLB eDM blasts) → **Auto-filed**, overriding the `.gov.sg`→ReadLater rule
- `.gov.sg` domains in From
- Singapore Pools (`SingaporePools.com.sg`), Toto
- OCBC **transaction** alerts (sender contains `ocbc`, subject contains `transaction`, e.g. "Card Transaction Alert"): the **first** amount in the body (the charged amount — `SGD38.41 was charged…`) is non-SGD OR > SGD 100 → ReadLater (gate 4b). Never worked before 2026-09-28: the amount regex only matched "38.41 sgd", its SGD branch compared lowercase `sgd` against upper-cased text, and `noreply@notify.ocbc.com` hit the no-reply Auto-filed branch first — all 155 historical alerts auto-filed. Backtest on 40 real bodies: 2 → ReadLater (SGD 141.00, 175.34), 38 stay Auto-filed. Funds-transfer notices and `documents@ocbc.com` e-Statements are not "transaction" subjects and keep their own routing.
- Travel booking confirmations: Agoda, Booking.com, airline confirmations (SQ, MI, TR, etc.)
- Any email with a genuine booking confirmation pattern (itinerary, confirmation number, travel dates)

## Subject-level rules (gate 4b, 2026-09-28 — ham's decisions)

Matched on `normalize_subject()` (lowercase, punctuation → space, so `sign-in` = `sign in`; NBSP-safe), placed after the ambiguity gate and **above** every Auto-filed return:
- **Security/billing subject → Action**, any sender incl. no-reply: `SECURITY_KEYWORDS` + `BILLING_KEYWORDS` + `SECURITY_SUBJECT_EXTRA` (`new sign in`, `password has been updated`, `password is updated`, `password was changed`, `password has been changed`, `password changed`). One-time/verification codes are deliberately not matched — they stay Auto-filed. A `CATEGORY_PROMOTIONS` mail with such a subject goes to **Unsure** (the ambiguity gate uses the same subject test).
- **Renewal subject → ReadLater** (not `CATEGORY_PROMOTIONS`, not `GOV_AD_SENDERS`, no whole-word promo term like sale/offer/deal/discount/`N off`): `RENEWAL_SUBJECT_RE` = `renew`/`renewal`/`renewed`/`renewing`/`renews`/`auto renew`, `contract expir…`, `subscription expir…`, `plan expir…`. A bare "expiring"/"expire" is *not* enough (game/points promos). Enforces ham's 2026-05-10 request.
- **OCBC transaction alert → ReadLater** (see Read Later above).

Backtest over 1,406 real (from, subject) rows: 17 flips, all intended — 12 security alerts (Google, Yahoo, Reddit, Dropbox, Hilton, Ticketmaster) → Action, 5 renewals (GOMO "plan has renewed", Xiaomi "Token Plan Expiry") → ReadLater.

## Action (Hermes/Action) — leave unread, stay in inbox

- Security/billing keywords (`SECURITY_KEYWORDS` / `BILLING_KEYWORDS`) in subject/snippet/body: `suspicious activity`, `unusual activity`, `login attempt`, `sign in attempt`, `password reset`, `reset your password`, `forgot password`, `change password`, `security alert`, `security notice`, `account locked`, `unrecognized device`; `payment failed`, `payment declined`, `card declined`, `renewal failed`, `unable to process payment`, `billing failure`, `charge failed`, `transaction failed`, `payment not processed`. (Correction 2026-09-28: this doc used to list broader words — `verify`, `login`, `invoice due`, `confirm your account` — that were never in the code; the code's narrower list is intentional.)
- Reply pattern: `Re:`, `Fwd:` or `Aw:` prefix in subject (`Fw:` is not matched)
- Human-sender action language: `please review`, `please check`, `need your`, `action required`, `due today`, `deadline` — **only when `is_human_sender()` is true** (non-human marketing bodies also contain words like "important")
- Any email clearly requiring a response or follow-up

## VIP

Applied on top of Action/ReadLater (never with Auto-filed) for emails from known VIP senders.

## If unsure (Unsure — two-tier ambiguity gate)

Returns `"Unsure"` when a strong Auto-filed signal collides with an Action signal:
- **CRITICAL** (security/billing language) → fires from ANY sender (phishing-looking-like-promo).
- **SOFT** (reply/forward prefix or personal-action phrases) → fires only when `is_human_sender()` is true AND the mail is not `CATEGORY_PROMOTIONS` (Gmail's promo verdict is authoritative).

On Unsure, `main()` leaves the email untouched (unread, in inbox, **no label**) and `send_unsure_query()` posts one batched Discord question. Never guess on genuinely ambiguous mail.

## Singapore Pools / Toto — TWO separate Auto-filed paths

Both must be exempted:
1. `CATEGORY_UPDATES` block: Gmail tags as `CATEGORY_UPDATES` + `["unsubscribe"]` in body → Auto-filed. Exempted at line 377 (fixed 2026-05-29).
2. Unsubscribe + promo content check (line 417): `"unsubscribe"` + `"update"` in body → `promo_signals >= 1` → Auto-filed. Exempted with `not is_toto_email(msg)` guard at line 421 (fixed 2026-06-09).

If a new Singapore Pools email type is auto-filed, check ALL `return "Auto-filed"` paths in `categorize()`, not just CATEGORY_UPDATES.
