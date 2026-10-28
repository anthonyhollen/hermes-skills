#!/usr/bin/env python3
"""
Step 1: Fetch RSS feeds, match against user interests, deduplicate.
Output: ~/.hermes/morning-brief/articles.json

Features:
  - Recency boost: today=3×, yesterday=2×, older=1×
  - Feed authority weighting: A-tier=3×, B-tier=2×, C-tier=1×
  - Seen article tracking: skip articles featured within their retention window
  - Broad-keyword scoping (A4): EV/Grok/Sora only count alongside a specific topic
  - Same-story clustering (article_id.same_story): paraphrased coverage merges, so
    _num_sources reflects how many outlets ran the story
  - Hold-back: a re-telling of a story already shown (another outlet's copy) is not
    re-featured while still eligible; a weaker match is held for one run only
  - Per-source diversity cap (B2): ≤2 maybe-relevant slots per source
  - Selection walks the whole ranked pool, skipping same-story twins, so the
    3 + 5 slots stay filled (send_brief's dedup is only a backstop)
  - Reader ratings (feedback.py): reactions on past briefs are synced beside the fetch
    and turned into a per-article score multiplier (×0.5–×1.8)
"""

import calendar
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedparser
import requests

from article_id import (MIN_CONTENT_CHARS, annotate_story_tokens, article_hashes,
                        clean_text, index_keys, outlet_key, same_story_f,
                        story_features, story_overlap_f, with_slots, firm_story_f, _slots,
                        _same_outlet_f, _slot_conflict_t,
                        AGGREGATOR_SOURCES, CONTENT_DUP_THRESHOLD)
from bounded_http import TooLarge, read_bounded, request_bounded
try:        # reader ratings (👍/👎 → score multiplier); optional — a broken module
    import feedback  # noqa: E402   must never cost the brief, it just ranks without ratings
except Exception as _e:                         # noqa: BLE001
    print(f"[WARN] feedback.py unavailable ({type(_e).__name__}: {_e}) — no ratings",
          file=sys.stderr)
    feedback = None

BASE_DIR       = os.path.expanduser("~/.hermes/morning-brief")
INTERESTS_FILE = os.path.join(BASE_DIR, "interests.json")
OUTPUT_FILE    = os.path.join(BASE_DIR, "articles.json")
SEEN_FILE      = os.path.join(BASE_DIR, "seen_articles.json")
FEED_HEALTH_FILE = os.path.join(BASE_DIR, "feed_health.json")
MAX_WORKERS    = 20         # floor; fetch_all_feeds raises this to len(FEEDS) so all
                            # feeds fetch in ONE wave (see FETCH_BUDGET_SECONDS)
MAX_WORKERS_CEILING = 48    # don't open an unbounded number of sockets on the Pi
FETCH_TIMEOUT  = 15        # per-socket read timeout for one feed (normally <1s)
FETCH_CONNECT_TIMEOUT = 10
# Wall-clock budget for the whole fetch, mirroring the other two steps. The 120s step
# timeout was NOT supported by this step's own arithmetic: 3 attempts x a 30s read
# timeout + 2+4s backoff = 96s for one silent feed, and with 38 feeds over MAX_WORKERS=20
# that is two waves — measured 192.3s, i.e. a SIGKILL before update_feed_health() runs,
# so the dead-feed counters never even increment. Fixed three ways: one wave, a 15s
# read timeout, and this hard budget as the backstop.
FETCH_BUDGET_SECONDS = 100  # < STEP_TIMEOUT["fetch_rss.py"] (120s)
_FETCH_DEADLINE = [float("inf")]
MAX_AGE_DAYS   = 7          # forget seen articles older than this
# Hard freshness cutoff: articles older than this (or with no parseable date) are
# excluded BEFORE scoring, so stale content can never be featured in must-read or
# maybe-relevant. Recency is otherwise only a score multiplier, which is too weak
# to keep a high-keyword 5-month-old post out of must-read.
MAX_ARTICLE_AGE_DAYS = 7

# ── Fetch retry + feed-health tracking ─────────────────────────────────────────
# fetch_feed retries TRANSIENT failures (network errors, 5xx, 429) so a one-off
# blip doesn't silently drop a feed for the day; permanent failures (403/404) are
# not retried. Each run's per-feed outcome is recorded in feed_health.json with a
# consecutive-failure counter — a feed is only "dead" after DEAD_AFTER_FAILURES
# straight failed runs (so transient ≠ dead). send_brief.py surfaces dead feeds in
# the brief; feed_doctor.py proposes verified replacements.
FETCH_RETRIES         = 2          # extra attempts after the first (transient only)
RETRY_BACKOFF_SECONDS = 2          # linear backoff: 2s, 4s, …
TRANSIENT_HTTP        = {429, 500, 502, 503, 504}
DEAD_AFTER_FAILURES   = 3
# A feed can fetch perfectly and still be useless: several outlets serve RSS bodies
# shorter than the paywall guard (MIN_CONTENT_CHARS), so every entry scores 0. Tracking
# only fetch success reported those feeds as healthy forever — Hugging Face Blog
# returned 862 entries with ZERO content every run and never showed up as a problem.
# So health also counts USABLE entries (>= MIN_CONTENT_CHARS) and flags a feed that
# fetches fine but has produced nothing usable for ZERO_YIELD_AFTER_RUNS runs.
ZERO_YIELD_AFTER_RUNS = 3

# MIN_CONTENT_CHARS (the paywall guard) is imported from article_id — it is shared with
# summarize.py and send_brief.py, and lived as three separate literals until they were
# consolidated. Applied before keyword matching (a teaser can never score) and as the
# "usable entry" test for feed health above.

# Bounds on one feed's response. A feed host can serve an arbitrarily large document
# (a hijacked/misconfigured feed, or just an archive that keeps growing): 20k entries
# parsed in 3.5s but pushed deduplicate()'s O(n²) compare to ~28s at 8k matched and
# past the 120s fetch step timeout beyond that. Both caps are far above any real feed
# (largest today: OpenAI Blog, 1,193 entries / 725 KB).
MAX_FEED_BYTES        = 8 * 1024 * 1024
# Set ABOVE the largest real feed (OpenAI Blog, 1,193 entries) on purpose. An earlier
# value of 400 bounded the DoS just as well but cut that feed's usable-article count
# from 228 to 10, which starves the zero-yield detector and could flip a healthy feed
# into a false "yielding nothing usable" notice in the brief. 1,500 still turns a
# hostile 20k-entry feed into 1,500.
MAX_ENTRIES_PER_FEED  = 1500

# ── Authority tier multipliers ────────────────────────────────────────────────
TIER_MULT = {"A": 3, "B": 2, "C": 1}

# ── Broad-keyword scoping (A4) ─────────────────────────────────────────────────
# These interest topics are too broad to be a relevance signal on their own — a bare
# "EV" mention matches any car story. They only count toward score/classification when
# at least one *specific* (non-broad) interest keyword also matches the article, i.e.
# the article is about a core topic AND happens to be EV-flavoured. Strings must match
# the topic spellings in interests.json exactly.
BROAD_KEYWORDS = {"EV", "Grok", "Sora"}
# "Grok" is a common verb on HN/Simon Willison ("grok the codebase") and "Sora" is a
# Kingdom Hearts character on the gaming feeds — both only count alongside a specific
# topic (an xAI/OpenAI release story will also match the vendor name or "LLM").
# "Singapore" was removed from this set on 2026-09-14 together with the Singapore topic
# and the three SG feeds: Singapore was the ONLY interest topic those feeds could hit,
# and scoping made it never count alone, so the whole category was structurally
# unreachable (19 raw keyword hits -> 0 matched, measured over a live run).


# ── Deduplication / corroboration (B1) ─────────────────────────────────────────
# Clustering, selection, the send-time backstop and the hold-back all use ONE
# predicate, article_id.same_story (thresholds + calibration notes live there). The
# fetch-side dedup used to compare titles only (40% across / 65% within an outlet), so
# paraphrased coverage almost never merged: _num_sources >= 2 held for 1 of 175
# candidates, the corroboration route to must-read was dead, and a three-outlet story
# (the Sep 26 OpenAI/Australia breach) sat as three separate score-8 items below the cut.
#
# Promotional posts (deals, giveaways) never count as corroboration: two outlets' deal
# round-ups share product names and would otherwise promote a sale into must-read.
# Anchored to promo SHAPES: a bare "deal"/"sale" is news ("Anthropic signs $11.6 billion
# cloud deal with Akamai", "Tesla rules out sale") and must still corroborate.
_PROMO_RE = re.compile(r"^deals?\s*:|[$£€][\d,.]+\s*(off|discount)\b|\b\d+%\s*off\b|"
                       r"\bgiveaway\b|\bcoupons?\b|\bpromo codes?\b|\bprime (big )?deal days\b|"
                       r"\bprime day\b|\bblack friday\b|\bcyber monday\b|"
                       r"\bclearance (sale|price)\b|\bweek (deals|sale)\b|\b(launch|holiday) deals\b|"
                       r"\bdeals? hit\b|\bprice drops? to [$£€]\d|"
                       r"\b(grab|get|buy|snag|save)\b[^.]*\bfor (just|only|under) [$£€][\d,.]+", re.I)

# Hold-back: don't re-feature a story already shown through ANOTHER outlet's copy.
# Exact-copy suppression (the 7-day hash window) can't see a paraphrase: Meta's Muse
# agent reached 8 briefs in 9 days, and the Sep 21 antitrust suit re-ran on Sep 22 via
# Slashdot. Candidates are compared with every ledger entry recorded in the window
# (shown stories + their direct twins); entries written before 1.10.0 have no
# `url`/`story`, so for them only the TITLE signal applies.
#
# Two tiers, because a real follow-up's lede recaps the background ("Australia fines
# OpenAI over the Medicare breach" matches the breach) and every constructed follow-up
# at the gate matched the story it followed:
#   * a RE-TELLING — same url, a near-identical headline (title > HOLD_BACK_RETELL_TITLE)
#     or a heavily shared lede (content >= HOLD_BACK_RETELL_CONTENT) — is held while the
#     shown story's entry is in the window (HOLD_BACK_DAYS = MAX_ARTICLE_AGE_DAYS; a
#     3-day window just moved the repeat to day 4), and is itself recorded after the send
#     (`held_retellings`), so it stays held for its own eligible life too;
#   * a weaker match may be news: held for HOLD_BACK_CONTENT_DAYS (one run), and only
#     against a story recorded before it was published; after that it may be shown.
# 0.35 content: at the lap-3 gate, measured paraphrases scored 0.23-0.49 and follow-ups
# 0.08-0.31 (constructed set) — 0.35 put 7/11 repeats and 0/7 follow-ups in the 7-day
# tier. The one-run tier can still delay a follow-up by a day.
# The slack turns "N days" into "the next N daily runs" (start times jitter by ~1 min).
#
# Held articles are removed BEFORE clustering, so a HELD repeat can never be chosen as
# the representative that swallows the new development it clusters with (the lap-3
# gate's "OpenAI pulls GPT-6.5" vanishing under a repeat of the release). A repeat that
# is NOT recognised as one can still out-score a development it clusters with — a known
# limit. Marking held articles and filtering inside clusters (tried at lap 3) recorded
# weak-held follow-ups as 7-day twins (lap 4); de-preferring weak "echoes" (lap 5) lost
# follow-ups, which almost always echo their background (lap 6) — don't do either.
HOLD_BACK_DAYS           = MAX_ARTICLE_AGE_DAYS
HOLD_BACK_SLACK_SECONDS  = 6 * 3600
HOLD_BACK_CONTENT_DAYS   = 1
HOLD_BACK_RETELL_TITLE   = 0.55
HOLD_BACK_RETELL_CONTENT = 0.35

# ── Brief quality (1.12, 2026-09-28) ───────────────────────────────────────────
# The Sep 28 brief (reader: "noticeably worse") made a 6-day-old CLI release note
# must-read #2 at score 6.7 while four maybe items scored 12+, and filled maybe with a
# $900-off deal, a Verge newsletter issue and two keyword-heavy Reddit posts. User's
# choices: must-read must be FRESH and STRONG (fewer than 3 is fine); promos out;
# newsletters/roundups demoted; keyword stuffing capped; one Reddit slot.
MUST_READ_MAX_AGE_HOURS = 48     # a must-read is news from the last two days
MUST_READ_MIN_SCORE     = 10.0   # e.g. fresh B-tier + 2 keywords (12) passes; 6.7 doesn't
SCORE_KEYWORD_CAP       = 3      # keyword hits counted in the SCORE (eligibility uses all)
ROUNDUP_MULT            = 0.5    # newsletter issues, daily/weekly roundups, podcast episodes
# Shapes of a roundup rather than a story. TITLE patterns are checked on the title only
# ("…this week in the Apple vs. Epic case" / "said on his podcast" in a LEDE are news);
# LEDE patterns only on the first 200 chars of the body.
_ROUNDUP_RE = re.compile(
    r"\binstaller no\.|\bnewsletter\b|\b9to5mac daily\b|\bovertime \d+|\bpodcast\b|"
    r"\bepisode \d+|\bweek in review\b|\bthis week in\b|\bweekly roundup\b|^the download\b|"
    r"\bmacrumors show\b", re.I)
_ROUNDUP_LEDE_RE = re.compile(
    r"^(hi|hello|hey),? (friends|everyone|all)\b|\bwelcome to (the |this |issue )?"
    r"(installer|newsletter|edition|issue)\b|^listen to a recap\b", re.I)
# Per-outlet maybe cap overrides (default MAX_PER_SOURCE_MAYBE).
MAX_PER_OUTLET_MAYBE = {"reddit.com": 1}
# Guides and listicles are demoted like roundups (evergreen buying guides took maybe slots:
# "Top 12 Fujifilm primes", "Best iPhone 18 Pro cases").
_GUIDE_RE = re.compile(
    # A list count only at the start or after a colon/"these" — "iOS 27 Features" is a
    # version number, not a listicle ("watchOS 27: 12 New Things" is).
    r"^(the )?(\d+ )?best\b(?! (practices|buy)\b)|^top \d+\b|\blooking for your next\b|"
    r"(^|: |\bthese )\d+ (new )?(things|features|tips|ways)\b|\bthings to (know|check out|try)\b|"
    r"\bbuy(ing|er'?s|ers'?) guide\b|^how to (watch|get|buy)\b|^(here are|these are) the \d+\b|"
    r"^the top \d+\b", re.I)

# Topic FAMILIES: synonyms count once. The interest list has ~25 AI terms and one "Apple",
# so an AI post naming "LLM" and "language model" scored two hits for one idea and the
# brief was 8/11 AI across two days while 3-outlet Apple stories sat below Reddit posts.
# Topics not listed are their own family. (Grouping GPU with PC gaming was tried and
# rejected at review — it pushed a GPU-hardware story out.)
TOPIC_FAMILIES = {
    "llm":     ["LLM", "language model", "foundation model", "frontier model", "reasoning model",
                "budget LLM"],
    "openai":  ["OpenAI", "ChatGPT", "GPT-6", "GPT6", "GPT-5", "Sora"],
    "agents":  ["AI agent", "agentic", "automation"],
    "release": ["AI model release", "model release", "open source model", "open-weights",
                "open weights"],
    "xai":     ["Grok", "xAI"],
    "cost":    ["affordable AI", "price performance"],
    "camera":  ["Fujifilm", "mirrorless camera"],
    "claude":  ["Claude", "Anthropic"],
}
# Interest AREAS for the maybe-section diversity rule. A family not listed is "ai".
NON_AI_FAMILIES = {"apple": "apple", "camera": "camera", "ev": "ev", "nvidia gpu": "hardware",
                   "pc gaming": "gaming"}
MAYBE_MAX_PER_AREA  = 3     # at most this many maybe slots to stories only in ONE area…
MAYBE_OTHER_AREA_MIN = 7.0  # …when a story from another area scores at least this
OUTLET_BONUS_PER    = 0.25  # score × (1 + this per extra outlet), capped below
OUTLET_BONUS_MAX    = 0.50

def topic_families(topics) -> set:
    fam = {}
    for f, members in TOPIC_FAMILIES.items():
        for m in members:
            fam[m.lower()] = f
    return {fam.get(t.lower(), t.lower()) for t in topics}

def article_areas(art: dict) -> set:
    return {NON_AI_FAMILIES.get(f, "ai") for f in (art.get("_families") or [])}

# ── Per-source diversity cap (B2) ──────────────────────────────────────────────
# Cap how many maybe-relevant slots any single source can occupy, so one prolific
# blog (e.g. Simon Willison filling 3 of 5 slots) can't crowd out variety.
MAX_PER_SOURCE_MAYBE = 2

# ── Interests ─────────────────────────────────────────────────────────────────

def load_interests() -> dict:
    """Load interests.json (hand-edited config).

    Exits with a clear message on a missing or malformed file rather than a bare
    traceback, since this is an edited-by-hand config and a typo would otherwise
    fail the whole brief with an opaque error."""
    try:
        with open(INTERESTS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] interests file not found: {INTERESTS_FILE}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:      # incl. json.JSONDecodeError and UnicodeDecodeError
        print(f"[ERROR] interests file is not valid JSON/text ({INTERESTS_FILE}): {e}",
              file=sys.stderr)
        sys.exit(1)
    except OSError as e:          # unreadable, a directory, permission denied
        print(f"[ERROR] could not read interests file ({INTERESTS_FILE}): {e}",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"[ERROR] interests file must be a JSON object: {INTERESTS_FILE}",
              file=sys.stderr)
        sys.exit(1)
    return data

# ── Feed fetching ─────────────────────────────────────────────────────────────

def parse_date(entry) -> datetime | None:
    """Try published_parsed, then updated_parsed, return UTC datetime or None."""
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                # feedparser gives a UTC struct_time. calendar.timegm() treats it
                # as UTC (time.mktime would wrongly treat it as local time and skew
                # every article's age by the server's UTC offset — see audit B1).
                return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except (ValueError, OSError):
                pass
    return None

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

def _extract_articles(feed, name: str, tier: str) -> tuple[list, int]:
    """Turn a parsed feedparser result into our article dicts.

    Returns (articles, usable_count) where usable_count is measured BEFORE the
    MAX_ENTRIES_PER_FEED cap — see the comment at the cap.
    """
    articles = []
    for entry in feed.entries:
        # Content: pick longest from content list, then summary, then description.
        # Tom's Hardware (and similar Future plc feeds) put the author bio in
        # content[0] and the actual article in content[1] — always grab the
        # longest entry so the real article text wins.
        content = ""
        if hasattr(entry, "content") and entry.content:
            if isinstance(entry.content, list) and len(entry.content) > 1:
                content = max(entry.content, key=lambda c: len(c.value)).value
            else:
                content = entry.content[0].value
        elif hasattr(entry, "summary"):
            content = entry.summary
        elif hasattr(entry, "description"):
            content = entry.description

        # Strip HTML tags, then decode entities (feeds double-encode: `&amp;nbsp;`).
        # Undecoded entities were
        # counted as shared words by the same-story test (`nbsp`, `ldquo`, `mdash`),
        # and reached Discord titles as `Google&#8217;s`.
        # Decoding AFTER stripping means a literal "&lt;div&gt;" (code in a post) survives
        # as text rather than being decoded into a tag and deleted.
        content = clean_text(re.sub(r"<[^>]+>", " ", content or ""))
        content = re.sub(r"\s+", " ", content).strip()

        article_url   = getattr(entry, "link", "") or getattr(entry, "id", "")
        published_dt  = parse_date(entry)
        published_str = published_dt.isoformat() if published_dt else ""

        articles.append({
            "title":     re.sub(r"\s+", " ", clean_text(entry.get("title", ""))).strip(),
            "url":       article_url,
            "content":   content[:2000],
            "source":    name,
            "tier":      tier,
            "published": published_str,
        })
    # Count usable entries BEFORE any capping: `usable` feeds the zero-yield detector,
    # which must describe the FEED, not our truncation of it (post-cap counting reported
    # a feed serving 3,000 usable articles as 1,500, and could report 0 for a feed that
    # did serve a usable article).
    usable = sum(1 for a in articles if len(a["content"]) >= MIN_CONTENT_CHARS)

    if len(articles) > MAX_ENTRIES_PER_FEED:
        # Keep every FRESH article first, then fill with the newest stale ones.
        #
        # Sorting by date and taking the head is not sufficient: the sort is stable, so a
        # feed whose entries share one timestamp (a date-only `pubDate` gives every item
        # 00:00:00) keeps document order, and an oldest-first feed of that shape loses its
        # newest entries — demonstrated at the gate. Partitioning on the freshness cutoff
        # the pipeline already applies makes the cap provably unable to drop an article
        # that could have been featured, whatever the feed's ordering or date resolution.
        cutoff = datetime.now(timezone.utc).timestamp() - MAX_ARTICLE_AGE_DAYS * 86400
        def _ts(a):
            dt = parse_published(a)
            return dt.timestamp() if dt else float("-inf")
        fresh = [a for a in articles if _ts(a) >= cutoff]
        stale = [a for a in articles if _ts(a) < cutoff]
        stale.sort(key=_ts, reverse=True)
        articles = (fresh + stale)[:MAX_ENTRIES_PER_FEED]
    return articles, usable

def fetch_feed(name: str, url: str, tier: str) -> tuple[str, str, str, list, str, int]:
    """Fetch + parse one feed. Returns (name, url, tier, articles, status, usable).

    `usable` counts entries with >= MIN_CONTENT_CHARS of body BEFORE MAX_ENTRIES_PER_FEED
    is applied, so feed health measures the feed rather than our cap.

    Retries TRANSIENT failures (network errors, 5xx, 429) up to FETCH_RETRIES extra
    times with linear backoff; does NOT retry permanent failures (403/404) or a
    200-with-no-entries (retrying won't change those). `status` is "ok" on success
    or a short reason string ("HTTP 404", "ERR ConnectTimeout", "0 entries") used
    for feed-health tracking. Browser UA — a bot UA gets 403'd by Cloudflare.
    """
    # Accept-Encoding pinned: read_bounded only inflates gzip/deflate, but requests
    # advertises whatever urllib3 can decode (br once brotli is installed, zstd on newer
    # urllib3) — a br body would reach feedparser still compressed and parse to 0 entries.
    headers = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}
    status = "unknown"
    for attempt in range(FETCH_RETRIES + 1):
        try:
            remaining = _FETCH_DEADLINE[0] - time.monotonic()
            if remaining <= 0.5:
                status = "fetch budget exhausted"
                break
            # request_bounded covers connect/TLS/headers too. A watchdog armed only after
            # requests.get() RETURNS leaves that whole phase unbounded — a host dribbling
            # header lines under the read timeout sits inside requests.get indefinitely, and
            # ThreadPoolExecutor cannot exit until every worker returns, so one such feed
            # hangs the step past its 120s timeout (killing it before update_feed_health
            # runs, so the dead-feed counters never even increment).
            resp = request_bounded(
                "GET", _FETCH_DEADLINE[0], url=url, headers=headers, stream=True,
                timeout=(FETCH_CONNECT_TIMEOUT, min(FETCH_TIMEOUT, remaining)))
            if resp.status_code == 200:
                body = read_bounded(resp, _FETCH_DEADLINE[0], MAX_FEED_BYTES).content
                status_code = 200
            else:
                status_code = resp.status_code
                body = b""
                resp.close()
            if status_code == 200:
                # body is BYTES, never resp.text: several feeds (TechRadar, Tom's
                # Hardware, PC Gamer, Qwen Blog) send Content-Type with no charset, so
                # requests leaves .encoding None and .text falls back to chardet —
                # which misdetects UTF-8 as MacRoman and renders every em-dash/
                # curly-quote as mojibake. Passing bytes lets feedparser use the XML
                # declaration (encoding="UTF-8"), which is authoritative.
                articles, usable = _extract_articles(feedparser.parse(body), name, tier)
                if articles:
                    return name, url, tier, articles, "ok", usable
                status = "0 entries"
                break                                   # empty feed — retry won't help
            status = f"HTTP {status_code}"
            if status_code not in TRANSIENT_HTTP:
                break                                   # 403/404/… — permanent
        except TooLarge as e:
            status = "too large (>%dMB)" % (MAX_FEED_BYTES // (1024 * 1024))
            print(f"  [DOWN] {name}: {e}", file=sys.stderr)
            break                                       # permanent — retry won't help
        except requests.RequestException as e:
            status = f"ERR {type(e).__name__}"
        if attempt < FETCH_RETRIES:                     # transient — back off and retry
            if time.monotonic() > _FETCH_DEADLINE[0]:
                status = "fetch budget exhausted"
                break
            time.sleep(max(0.0, min(RETRY_BACKOFF_SECONDS * (attempt + 1),
                                    _FETCH_DEADLINE[0] - time.monotonic())))
    return name, url, tier, [], status, 0

def fetch_all_feeds() -> tuple[list, dict]:
    from feeds import FEEDS

    all_articles = []
    statuses = {}
    # One wave: a second wave doubles the worst-case wall clock (see FETCH_BUDGET_SECONDS).
    workers = min(max(MAX_WORKERS, len(FEEDS)), MAX_WORKERS_CEILING)
    _FETCH_DEADLINE[0] = time.monotonic() + FETCH_BUDGET_SECONDS
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_feed, name, url, tier): (name, url, tier)
            for name, url, category, tier in FEEDS
        }
        for future in as_completed(futures):
            f_name, f_url, f_tier = futures[future]
            # fetch_feed only catches requests.RequestException, so anything else a
            # worker raises (a feedparser blow-up, MemoryError on a huge feed) used to
            # escape future.result() and abort the WHOLE run — update_feed_health would
            # never run and articles.json would silently keep yesterday's brief. One bad
            # feed must cost one feed, not the day.
            try:
                name, url, tier, articles, status, usable = future.result()
            except Exception as e:                      # noqa: BLE001 — deliberate catch-all
                # MemoryError lands here too (an inflate bomb): keep it distinguishable
                # in feed health from an ordinary network blip.
                name, url, tier, articles, usable = f_name, f_url, f_tier, [], 0
                status = "ERR %s" % type(e).__name__
            statuses[name] = {"url": url, "ok": bool(articles), "status": status,
                              "count": len(articles), "usable": usable}
            if articles:
                print(f"  {name}: {len(articles)} articles ({usable} usable)")
            else:
                print(f"  [DOWN] {name}: {status}", file=sys.stderr)
            all_articles.extend(articles)
    return all_articles, statuses

def _as_int(value, default: int = 0) -> int:
    """Coerce a feed_health counter to int. A hand-edited/foreign file can hold a string or
    null, and comparing that to an int raised — in the writer, before the health file was
    written, and in send_brief AFTER the brief had been delivered."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def update_feed_health(statuses: dict) -> tuple[list, list]:
    """Update feed_health.json from this run's per-feed outcomes.

    Returns (dead, zero_yield).

    Each record: {consecutive_failures, consecutive_zero_yield, last_ok, last_usable,
    last_status, url}. Success resets the failure counter; failure increments it but
    preserves last_ok. A feed is "dead" once consecutive_failures >= DEAD_AFTER_FAILURES.

    Separately, a feed that fetches fine but yields no article passing the paywall
    guard is tracked by consecutive_zero_yield — a fetch-healthy feed can still be
    dead weight (teaser-only RSS), which the old fetch-only tracking called healthy
    forever. Records for feeds no longer in FEEDS are pruned.
    """
    health = {}
    if os.path.exists(FEED_HEALTH_FILE):
        try:
            with open(FEED_HEALTH_FILE) as f:
                health = json.load(f)
        except (ValueError, OSError):   # incl. UnicodeDecodeError from a truncated write
            health = {}
    # Shape guard: send_brief.dead_feeds_line already checks this; the writer did not, so
    # a hand-edited list/scalar file crashed the run on the first prev.get().
    if not isinstance(health, dict):
        health = {}
    health = {k: v for k, v in health.items() if isinstance(v, dict)}

    now = datetime.now(timezone.utc).isoformat()
    for name, s in statuses.items():
        prev = health.get(name, {})
        if s["ok"]:
            rec = {"consecutive_failures": 0, "last_ok": now,
                   "last_status": "ok", "url": s["url"]}
        else:
            rec = {"consecutive_failures": _as_int(prev.get("consecutive_failures")) + 1,
                   "last_ok": prev.get("last_ok"),
                   "last_status": s["status"], "url": s["url"]}
        # Zero-yield is tracked independently of fetch success: a failed fetch yields
        # nothing either, so it counts toward zero-yield as well.
        if s.get("usable", 0) > 0:
            rec["consecutive_zero_yield"] = 0
            rec["last_usable"] = now
        else:
            rec["consecutive_zero_yield"] = _as_int(prev.get("consecutive_zero_yield")) + 1
            rec["last_usable"] = prev.get("last_usable")
        health[name] = rec
    health = {k: v for k, v in health.items() if k in statuses}   # prune removed feeds

    # Atomic write (temp + fsync + os.replace) so a cron-watchdog SIGKILL mid-write
    # can't leave a truncated feed_health.json for the next run / send_brief to read.
    tmp = FEED_HEALTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(health, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, FEED_HEALTH_FILE)

    dead = sorted([k for k, v in health.items()
                   if _as_int(v.get("consecutive_failures")) >= DEAD_AFTER_FAILURES])
    # A dead feed is already reported as dead — don't also list it as zero-yield.
    zero_yield = sorted([k for k, v in health.items()
                         if k not in dead
                         and _as_int(v.get("consecutive_zero_yield")) >= ZERO_YIELD_AFTER_RUNS])
    if dead:
        print(f"  ⚠ {len(dead)} feed(s) down ≥{DEAD_AFTER_FAILURES} runs: {', '.join(dead)} "
              f"(run feed_doctor.py for replacements)", file=sys.stderr)
    if zero_yield:
        print(f"  ⚠ {len(zero_yield)} feed(s) fetching OK but yielding no usable "
              f"(≥{MIN_CONTENT_CHARS}-char) article for ≥{ZERO_YIELD_AFTER_RUNS} runs: "
              f"{', '.join(zero_yield)}", file=sys.stderr)
    return dead, zero_yield

# ── Recency scoring ───────────────────────────────────────────────────────────

def recency_multiplier(dt: datetime | None) -> float:
    """Return multiplier based on article age.
    today → 3.0, yesterday → 2.0, older → 1.0 (no boost).
    """
    if dt is None:
        return 1.0   # no date = assume older, no boost
    now = datetime.now(timezone.utc)
    try:
        age_days = (now - dt).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1.0
    if age_days < 1:
        return 3.0
    elif age_days < 2:
        return 2.0
    return 1.0

# ── Interest matching ─────────────────────────────────────────────────────────

def compile_keyword_patterns(keywords: list, allow_plural: bool = True) -> list:
    r"""Compile keywords into (keyword, word-boundary regex) pairs (case-insensitive).

    Plain substring matching is too loose: "EV" matches "rEVamped"/"dEVice",
    "AWS" matches "lAWS"/"flAWS", inflating keyword counts and causing spurious
    blocks. Word boundaries (\bEV\b) fix this — see audit B3 / SKILL.md Pitfall 11.

    `allow_plural` adds an optional trailing "s" (`\bLLMs?\b`) because exact matching
    was plural-blind: "LLM" missed "LLMs", "AI agent" missed "AI agents". Measured over
    a live 3,357-article corpus, 115 articles per run scored 0 purely on word form and
    16 would have crossed the must-read keyword threshold.

    It is ON for interest topics and deliberately OFF for `block_keywords`: this
    function serves both, and widening the BLOCK list was an unintended side effect —
    it newly suppressed on-topic articles ("container" matching "containers" in an
    OpenAI Responses-API post, "shipping containers" in a drone story). A block list is
    a precision instrument; widen it only by editing interests.json.

    The original keyword string is kept alongside the pattern so callers can apply
    broad-keyword scoping (A4) — which needs to know *which* keywords matched, not
    just how many.
    """
    suffix = r"s?\b" if allow_plural else r"\b"
    return [(kw, re.compile(r"\b" + re.escape(kw) + suffix, re.IGNORECASE))
            for kw in keywords]

def matched_keywords(text: str, patterns: list) -> set:
    """Set of interest keywords that match the text, with broad-keyword scoping (A4).

    A broad keyword (BROAD_KEYWORDS — EV/Grok/Sora) only counts when at least one
    specific keyword also matched: a bare "EV" mention would otherwise score any car
    story as a core-topic hit. If only broad keywords match, none count.
    """
    hits = {kw for kw, pat in patterns if pat.search(text)}
    if not (hits - BROAD_KEYWORDS):
        # Only broad keywords matched (or nothing) — not genuinely relevant.
        return set()
    return hits

def count_keyword_hits(text: str, patterns: list) -> int:
    """Number of distinct interest keywords that match (after broad scoping)."""
    return len(matched_keywords(text, patterns))

def parse_published(article: dict) -> datetime | None:
    """Parse the article's stored ISO 'published' string to an aware UTC datetime,
    or None if missing/unparseable."""
    s = article.get("published", "")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Guarantee an aware UTC datetime: _extract_articles always stores an aware
    # ISO string, but a hand-edited/foreign articles.json could carry a naive one,
    # and match_articles subtracts it from an aware `now` (unguarded, unlike
    # recency_multiplier) — a naive value there would raise TypeError and abort the run.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def score_article(article: dict, topic_patterns: list) -> float:
    """Composite score = keyword_hits × authority_tier × recency_multiplier.
    Articles with <MIN_CONTENT_CHARS of content are paywalled teasers — return 0.0.

    NOT on the pipeline path: match_articles inlines this same formula so it can reuse
    one keyword pass and one date parse (see the comment there). Kept as the readable
    definition of the score, for REPL/tuning use — if you change the formula, change
    BOTH.
    """
    # Paywall detection: RSS teasers are short, real articles have more
    if len(article.get("content", "")) < MIN_CONTENT_CHARS:
        return 0.0
    text = article["title"] + " " + article["content"]
    keyword_hits = count_keyword_hits(text, topic_patterns)
    if keyword_hits == 0:
        return 0.0
    tier_mult    = TIER_MULT.get(article.get("tier", "C"), 1)
    recency_mult = recency_multiplier(parse_published(article))
    return keyword_hits * tier_mult * recency_mult

def _hold_grade(art: dict, entry: dict, art_f: tuple = None):
    """"retell" (hold while eligible), "weak" (hold this run), "echo" (a weak match that
    no longer holds — informational), or None."""
    ef = entry.get("_f") or story_features(entry)
    af = art_f or story_features(art)
    # Sequel identifiers of the entry's title: stored at send time from its OWN body
    # (`slots`, 1.10+), so the test is symmetric and matches same-day clustering. A legacy
    # entry has no body; its Title Case names would never count and a sequel ("…Claude
    # Opus 5.5" → "…Claude Sonnet 5.5") looked like a re-telling (lap-7 gate), so for
    # those only, judge its title with the INCOMING article's body — which can wrongly
    # veto an alias re-telling ("OpenAI down" / "ChatGPT down", lap 8), a limit that
    # ages out with the pre-1.10 entries.
    if "_slots" in entry:
        ef = with_slots(ef, entry["_slots"])
    else:
        body = art.get("content", "") if isinstance(art.get("content"), str) else ""
        ef = with_slots(ef, _slots(entry.get("title", ""), entry.get("source"), body))
    why = same_story_f(af, ef)
    if not why:
        return None
    t, c = story_overlap_f(af, ef)
    if why == "url" or t > HOLD_BACK_RETELL_TITLE or c >= HOLD_BACK_RETELL_CONTENT:
        return "retell"
    # The weakest same-story path (content > 8% with 3 shared title words) doesn't hold
    # at all: it held a distinct score-18 development ("OpenAI agents tried to
    # 'bruteforce' a UN website") against yesterday's 53-images story (content 0.08, title
    # 0.15). Only when the headlines differ too: the same band holds true re-tellings with
    # a closer headline ("OpenAI pauses training…" at content 0.11, title 0.28).
    # A deep match (1.13; its lede overlap is low by definition) is treated the same way:
    # a follow-up recaps the original story in its second and third paragraphs, which is
    # exactly the window the deep path reads, so it holds one run at most.
    if why.startswith(("content", "deep")) and c <= CONTENT_DUP_THRESHOLD and t < 0.20:
        return "echo"
    # The one-run tier applies only against a story recorded BEFORE this article was
    # published: at the lap-5 gate each day's newly shown sibling of a big story re-armed
    # the hold on its follow-up ("OpenAI pulls GPT-6.5") until its recency boost expired.
    pub = parse_published(art)
    rec_ts = entry.get("_ts", time.time() - entry.get("_age_s", 0))
    if entry.get("_age_s", 0) <= HOLD_BACK_CONTENT_DAYS * 86400 + HOLD_BACK_SLACK_SECONDS \
            and (pub is None or rec_ts <= pub.timestamp()):
        return "weak"
    return "echo"

def _held_by(art: dict, entry: dict, art_f: tuple = None) -> bool:
    return _hold_grade(art, entry, art_f) in ("retell", "weak")

def match_articles(articles: list, interests: dict, seen_hashes: set,
                   recent_shown: list = (), held_out: list = None,
                   prefs=None) -> tuple[list, int, int]:
    """Returns (scored, too_old, held_back); held-back articles are not in `scored`.
    Re-tellings that were held are appended to `held_out` (if given) so send_brief can
    record them — otherwise one published after the story was shown outlives its ledger
    entry and resurfaces on day 8+ (lap-5 gate).

    `prefs` (a feedback.Model, or None for no ratings) multiplies `_score` by the reader's
    learned preference and sets `_pref` / `_pref_why`. Applied here, BEFORE clustering, so
    it also decides which outlet's copy represents a cluster."""
    topic_patterns = compile_keyword_patterns(interests.get("topics", []))
    for e in recent_shown:                  # the ledger side's features, once
        if isinstance(e, dict) and "_f" not in e:
            e["_f"] = story_features(e)
    # allow_plural=False: a block list must stay exact (see compile_keyword_patterns).
    block_patterns = compile_keyword_patterns(interests.get("block_keywords", []),
                                              allow_plural=False)
    now = datetime.now(timezone.utc)

    scored = []
    too_old = held_back = 0
    for art in articles:
        # Skip if already seen. Checks EVERY identity hash (url / canonical title /
        # legacy title|source), so the same article arriving through a publisher's
        # sibling feed is recognised — that miss re-featured an Ars Technica story on
        # two consecutive days via the Ars Technica AI feed.
        if seen_hashes.intersection(article_hashes(art)):
            continue

        text = art["title"] + " " + art.get("content", "")

        # Block keyword filter (word-boundary — see audit B3)
        if any(pat.search(text) for _kw, pat in block_patterns):
            continue

        # Promotions never appear (deals, giveaways, price drops — user's call, 1.12).
        if _PROMO_RE.search(art["title"]):
            continue

        # Hard freshness cutoff: drop stale or undated articles before scoring, so
        # a high-keyword months-old post can never reach must-read/maybe-relevant.
        published_dt = parse_published(art)
        if published_dt is None or (now - published_dt).total_seconds() / 86400 > MAX_ARTICLE_AGE_DAYS:
            too_old += 1
            continue

        # Paywall guard: teasers always score 0 — drop them here, before the expensive
        # keyword pass, rather than recomputing it inside score_article.
        if len(art.get("content", "")) < MIN_CONTENT_CHARS:
            continue

        # Single keyword pass, reused for both the score and classification. (Inlined
        # rather than calling score_article, which would recompute the keyword match
        # AND re-parse the date — both already done above.)
        topics = matched_keywords(text, topic_patterns)
        if not topics:
            continue
        # Count topic FAMILIES, not synonyms (see TOPIC_FAMILIES).
        families = topic_families(topics)
        kw_count = len(families)

        # Hold-back, after the cheap filters (~200 candidates × a few dozen entries).
        # Dropped BEFORE clustering: a held article must not lead a cluster, bridge two
        # clusters, or be recorded as a twin of whatever live item it clusters with —
        # at the lap-4 gate a weak-held follow-up ("OpenAI pulls GPT-6.5") was recorded
        # as a 7-day twin of a weaker hands-on piece and lost for the week.
        art_f = story_features(art)
        grades = {_hold_grade(art, e, art_f) for e in recent_shown} - {None}
        if grades & {"retell", "weak"}:
            held_back += 1
            if "retell" in grades and held_out is not None:
                held_out.append(art)
            continue
        # (An "echo" — an old weak match — is deliberately NOT penalised: nearly every
        # real follow-up recaps its background and grades as an echo, so de-preferring
        # echoes as cluster representative (tried at lap 5) handed the rep slot to a
        # 4-point item and lost "OpenAI pulls GPT-6.5" at the lap-6 gate.)

        # Keyword hits are capped in the SCORE: a post that merely names many models
        # (Claude, GPT-6, LLM, OpenAI…) outranked real news on count alone.
        art["_score"] = (min(kw_count, SCORE_KEYWORD_CAP) * TIER_MULT.get(art.get("tier", "C"), 1)
                         * recency_multiplier(published_dt))
        if _ROUNDUP_RE.search(art["title"]) or _GUIDE_RE.search(art["title"]) \
                or _ROUNDUP_LEDE_RE.search((art.get("content") or "")[:200]):
            art["_score"] *= ROUNDUP_MULT
            art["_roundup"] = True
        art["_interest_kw_count"] = kw_count
        art["_families"] = sorted(families)
        # Which topics matched (not just how many): the manifest records them so a rating
        # can teach per-topic affinity, and the preference pass reads them.
        art["_topics"] = sorted(topics)
        if prefs:
            try:
                mult, why = prefs.preference(art, art_f)
            except Exception as e:              # noqa: BLE001 — a rating bug must not cost the brief
                mult, why = 1.0, f"error: {type(e).__name__}"
            if mult != 1.0:
                art["_score"] *= mult
                art["_pref"], art["_pref_why"] = mult, why
        scored.append(art)
    return scored, too_old, held_back

# ── Seen article tracking ─────────────────────────────────────────────────────

def load_seen_hashes(recent_shown: list = None, hold_back_days: float = 0) -> set:
    """Load seen_articles.json and return every hash still inside its retention window.

    That is MAX_AGE_DAYS for a story that was shown, or the entry's own `ttl_days` for
    one the send-time dedup dropped (send_brief.DEDUPED_TWIN_TTL_DAYS).

    If `recent_shown` is a list, entries recorded within the last `hold_back_days` (+
    HOLD_BACK_SLACK_SECONDS) are appended to it as {title, url, source, _story,
    _story_deep, _age_s} for the hold-back — shown stories and their twins alike (a twin
    is by definition the same story as a shown one). `_age_s` / `_ts` feed the one-run
    tier in _hold_grade. A `"kind": "chained"` entry (1.13) is identity-only: its hashes
    suppress that one article, but it never joins the hold-back set.

    Defensive about the ledger's shape: it is a plain JSON file on disk, and a
    truncated/hand-edited one used to abort the whole run with a bare KeyError or
    AttributeError. A malformed entry is skipped, not fatal — worst case an article
    re-appears, which is far better than losing the brief.
    """
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE) as f:
            entries = json.load(f)
    except (ValueError, OSError):
        # ValueError covers json.JSONDecodeError AND UnicodeDecodeError (a truncated
        # write can leave an invalid byte sequence); the narrower JSONDecodeError missed it.
        return set()
    if not isinstance(entries, list):
        return set()
    cutoff_base = datetime.now(timezone.utc).timestamp()
    hashes = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if not (ts == ts) or ts in (float("inf"), float("-inf")):   # NaN / inf
            continue
        # Clamp a future timestamp to now: a forward-skewed run (e.g. before NTP sync) would
        # otherwise write entries that never expire and never prune — permanent suppression
        # plus unbounded growth. MIN_LEDGER_ENTRIES guards only the opposite direction.
        ts = min(ts, cutoff_base)
        # Hold-back set BEFORE the per-entry expiry below: a twin whose own (shorter)
        # ttl_days has lapsed is still the same story as something shown.
        chained = entry.get("kind") == "chained"
        if recent_shown is not None and not chained and \
                ts >= cutoff_base - hold_back_days * 86400 - HOLD_BACK_SLACK_SECONDS:
            slots = entry.get("slots")
            recent_shown.append({k: entry.get(k) for k in ("title", "url", "source")}
                                | {"_age_s": cutoff_base - ts, "_ts": ts}
                                | ({"_slots": tuple((str(t[0]), bool(t[1])) for t in slots
                                                    if isinstance(t, list) and len(t) == 2)}
                                   if isinstance(slots, list) else {})
                                | ({"_story": entry["story"]}
                                   if isinstance(entry.get("story"), list) else {})
                                | ({"_story_deep": entry["story_deep"]}
                                   if isinstance(entry.get("story_deep"), list) else {}))
        # Per-entry retention of the identity HASHES: a send-time twin's hashes expire
        # sooner (send_brief.DEDUPED_TWIN_TTL_DAYS). Note the hold-back above still
        # compares against the entry for the full HOLD_BACK_DAYS, so in practice a
        # (rare, since 1.10) send-time twin is suppressed for the week as well.
        try:
            ttl = float(entry.get("ttl_days", MAX_AGE_DAYS))
        except (TypeError, ValueError):
            ttl = MAX_AGE_DAYS
        # Clamp exactly as send_brief._entry_ttl does, or the reader and writer disagree
        # about what is expired and an article loops (delivered every run, never re-recorded).
        if ttl != ttl or ttl < 0:
            ttl = MAX_AGE_DAYS
        ttl = min(ttl, float(MAX_AGE_DAYS))
        # A pre-1.10 twin (no `story` field) was recorded by the old, looser dedup with a
        # 2-day window; its story's shown copy is still suppressed for 7 days, so the twin
        # itself came back as "new" on day 4 (Ars AI "…make same promise", Sep 23 → 27).
        # Give those the full window; they all age out within a week of the upgrade.
        # (send_brief._entry_ttl applies the same rule — reader and writer must agree.)
        # A chained entry has no `story` by design and keeps its own short ttl.
        if "ttl_days" in entry and "story" not in entry and not chained:
            ttl = float(MAX_AGE_DAYS)
        if ts < cutoff_base - ttl * 86400:
            continue
        # New ledger entries carry every identity hash; older ones only "hash".
        stored = entry.get("hashes")
        if isinstance(stored, list):
            hashes.update(h for h in stored if isinstance(h, str))
        h = entry.get("hash")
        if isinstance(h, str):
            hashes.add(h)
    return hashes

# NOTE: seen-article *writing* lives in send_brief.py::save_seen_articles, which
# runs only after confirmed Discord delivery (SKILL.md Pitfall 12). Do not add a
# save step here — recording before delivery would skip articles that never sent.

# ── Deduplication ──────────────────────────────────────────────────────────────

def deduplicate(articles: list) -> list:
    """Cluster articles covering the same story; keep the highest-scoring per cluster.

    Single-linkage on article_id.same_story: an article joins a cluster if it is the
    same story as ANY member (not only the representative — matching the rep alone made
    merges order-dependent: Tom's Hardware's copy of the Australia breach matched Ars'
    but not TechXplore's, so whether it merged depended on which feed answered first).
    Two clusters an article bridges are merged.
    """
    # Union-find over articles, comparing only pairs that share a blocking key (url, title
    # word, or a story-word min-hash — see article_id.index_keys), on features computed
    # once per article. Same single-linkage result as comparing every pair except for a
    # small, documented miss rate on syndicated ledes, without the O(n²) re-tokenising.
    feats  = [story_features(a) for a in articles]
    parent = list(range(len(articles)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    index: dict = {}
    for i, f in enumerate(feats):
        cands = set()
        for k in index_keys(f):
            cands.update(index.setdefault(k, []))
            index[k].append(i)
        for j in cands:
            if find(i) != find(j) and same_story_f(f, feats[j]):
                parent[find(i)] = find(j)
    groups: dict = {}
    for i in range(len(articles)):
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for idxs in sorted(groups.values()):
        members = [articles[i] for i in idxs]
        # Representative by the full deterministic ranking, not "first to arrive wins
        # ties": arrival order is as_completed order, i.e. network timing (30 shuffles of
        # one fetch gave 4 different line-ups at the lap-1 gate).
        k = max(idxs, key=lambda i: _rank_key(articles[i]))
        clusters.append((articles[k], members, feats[k], {id(articles[i]): feats[i] for i in idxs}))

    deduped = []
    for rep, members, rep_f, mf in clusters:
        # Corroboration counts distinct OUTLETS (registrable domain of the article
        # link), not distinct feed names. feeds.py subscribes to both a publisher's
        # main feed and its topic sub-feed for two outlets (The Verge / The Verge AI,
        # Ars Technica / Ars Technica AI); counting feed names made one outlet look
        # like two and promoted single-source stories into must-read — reproduced live
        # on an Ars Technica article that appeared under both feeds with an identical
        # URL. The count is ALSO clamped to the number of distinct feed names: a single
        # feed spread across subdomains (Slashdot serves news./games./science./yro.)
        # would otherwise inflate the count the other way and promote a one-outlet
        # story, which the terminal gate caught. Both bounds can only under-count, and
        # an under-count merely fails to promote. _sources keeps the feed names.
        # Only members that are the same story as the REP itself corroborate it: single
        # linkage chains through round-ups ("Leaks reveal a new HomePod mini, iPad mini,
        # and Apple TV 4K" links three separate leak stories), which is fine for hiding
        # duplicates but would fabricate outlets for the rep. Promotional members
        # (see _PROMO_RE) never corroborate.
        counted = [rep] if _PROMO_RE.search(rep["title"]) else \
                  [m for m in members if m is rep or (not _PROMO_RE.search(m["title"])
                                                      and same_story_f(mf[id(m)], rep_f))]
        names   = {m["source"] for m in counted}
        outlets = {outlet_key(m) for m in counted}
        rep["_sources"]     = sorted({m["source"] for m in members})
        rep["_num_sources"] = min(len(outlets), len(names))
        # Coverage by several outlets is itself a signal of importance (1.12): it used to
        # decide eligibility and ties only, so a 3-outlet Apple verdict scored below a
        # Reddit post. +25% per extra outlet, capped at +50%.
        if rep["_num_sources"] > 1:
            rep["_score"] *= 1 + min(OUTLET_BONUS_MAX, OUTLET_BONUS_PER * (rep["_num_sources"] - 1))
        rep["_cluster_size"] = len(members)
        # The other members, so send_brief can record them as twins when the rep is shown
        # (a chained member need not match the rep, so the hold-back alone can't catch it —
        # the lap-1 gate saw "iPad Mini 8 Leaked" return the day after its cluster showed).
        # Only members that match the REP directly: single linkage chains through
        # round-ups (one 15-member chain on a full fetch linked a Hugging Face incident to
        # "Airbnb widens access to GPT-6 Astra"), and recording a chained member would
        # hide that distinct story for days. `_strong` = a url match, or a title match
        # sharing >= 2 distinctive words. send_brief keeps every direct twin for the full
        # 7-day window (a 3-day twin simply resurfaced on day 4); `_strong` is only a
        # diagnostic in articles.json (it marks the dict as a cluster twin, not its TTL).
        rep["_twins"] = []
        direct = []
        for m in members:
            if m is rep:
                continue
            why = same_story_f(mf[id(m)], rep_f)
            if not why:
                continue
            direct.append(m)
            strong = why == "url" or (why.startswith("title")
                                      and len(mf[id(m)][2] & rep_f[2]) >= 2)
            rep["_twins"].append({k: m.get(k) for k in ("title", "url", "source", "_story",
                                                         "_story_deep")}
                                 | {"_strong": strong,
                                    # sequel flags judged with the member's own body (it
                                    # carries no content onward) — see save_seen_articles
                                    "_slots": [list(t) for t in mf[id(m)][7]]})
        # One step further out (1.13): members that are the same story as a direct twin
        # but not as the rep. Unrecorded, they came back the next day. On Oct 3, 9to5Mac's
        # "tightening macOS privacy controls" sat in the "Full Disk Access" cluster via
        # TechCrunch's copy, then ran on Oct 4. The hold-back matched it against the
        # recorded twins but graded it an echo, because it was published before they were
        # recorded. send_brief records these by identity only (they hold nothing else back)
        # for send_brief.CHAINED_TTL_DAYS. Every step must be solid, because a wrong record
        # hides a different story for days (the independent review found each of these):
        # - the link is a direct twin that is neither an aggregator item nor a round-up (a
        #   Techmeme summary names several stories: on the Oct 5 fetch "…Ternus… design
        #   chief…" chained two MacBook OLED rumours into the Ternus cluster; round-ups are
        #   how the 15-member chains formed), and both hops are firm matches (firm_story_f:
        #   no 8-12% lede band, no deep path);
        # - the member is not an aggregator item or a round-up, and is not one the
        #   predicate SEPARATES from the rep by rule: the same outlet (matched only by the
        #   strict same-outlet test — 4 of 5 chained members on that fetch were the rep's own
        #   outlet) or a sequel of it ("Sonnet 5.5 review" via an Opus+Sonnet launch story).
        # (A round-up rep spans several stories, so nothing is chained under it at all.)
        links = [] if rep.get("_roundup") else \
                [mf[id(d)] for d in direct if d.get("source") not in AGGREGATOR_SOURCES
                 and not d.get("_roundup") and not _PROMO_RE.search(d["title"])
                 and firm_story_f(mf[id(d)], rep_f)]
        rep["_chained"] = [{k: m.get(k) for k in ("title", "url", "source")}
                           for m in members
                           if m is not rep and not any(m is d for d in direct)
                           and not m.get("_roundup") and m.get("source") not in AGGREGATOR_SOURCES
                           and not _same_outlet_f(mf[id(m)], rep_f)
                           and not _slot_conflict_t(mf[id(m)][7], rep_f[7])
                           and any(firm_story_f(mf[id(m)], lf) for lf in links)] if links else []
        deduped.append(rep)
    return deduped

# ── Classify ───────────────────────────────────────────────────────────────────

MUST_READ_SLOTS = 3
MAYBE_SLOTS     = 5

def _rank_key(art: dict) -> tuple:
    """Deterministic ranking: score, corroboration, keywords, tier, recency, url.

    The trailing keys matter: a 9.0 / 1-source / 3-keyword tie between four items was
    otherwise broken by list order, i.e. by which feed's HTTP response completed first
    (as_completed), so the must-read line-up depended on network timing.
    """
    # .get: deduplicate() ranks members BEFORE _num_sources exists (it sets it).
    return (art["_score"], art.get("_num_sources", 0), art.get("_interest_kw_count", 0),
            TIER_MULT.get(art.get("tier", "C"), 1), str(art.get("published") or ""),
            str(art.get("url") or ""), str(art.get("source") or ""), str(art.get("title") or ""))

def _must_read_eligible(art: dict) -> bool:
    """(≥2 distinct outlets AND ≥2 keywords) OR (≥3 keywords), published within
    MUST_READ_MAX_AGE_HOURS with _score >= MUST_READ_MIN_SCORE, not a roundup — and a C-tier source
    (r/LocalLLaMA) only when its cluster spans ≥2 outlets. feeds.py always said
    C-tier "cannot reach must-read on its own"; classify() never checked, and two
    Reddit posts took 2 of 3 must-read slots on 2026-09-26."""
    num_sources = art["_num_sources"]
    kw_count    = art.get("_interest_kw_count", 0)
    if art.get("tier", "C") == "C" and num_sources < 2:
        return False
    # Reader ratings: something they've clearly disliked may still be a maybe, but it
    # doesn't take one of the three must-read slots on keyword count alone.
    if feedback and art.get("_pref", 1.0) < feedback.PREF_MUST_READ_FLOOR:
        return False
    # Fresh and strong (1.12): must-read is recent news of real weight, not whatever
    # else happens to name three topics — fewer than 3 must-reads is fine.
    pub = parse_published(art)
    if pub is None or (datetime.now(timezone.utc) - pub).total_seconds() > MUST_READ_MAX_AGE_HOURS * 3600:
        return False
    # The floor is on the score BEFORE reader-preference scaling: ratings have their own
    # must-read gate (PREF_MUST_READ_FLOOR); applying both made a mild 👎 (×0.83) act like
    # a strong one on a 12-point story.
    base = art.get("_score", 0) / (art.get("_pref") or 1.0)
    if base < MUST_READ_MIN_SCORE or art.get("_roundup"):
        return False
    return (num_sources >= 2 and kw_count >= 2) or kw_count >= 3

def classify(articles: list) -> tuple[list, list]:
    """Pick MUST_READ_SLOTS + MAYBE_SLOTS articles from the ranked pool.

    Walks the WHOLE ranked pool and skips any article that is the same story as one
    already picked, so a twin costs nothing: the old code cut to 3 + 5 first and let
    send_brief's dedup remove twins afterwards, which left 4 of 12 briefs short
    (Sep 23: 5 of 8). send_brief still re-checks as a backstop.
    """
    ranked = sorted(articles, key=_rank_key, reverse=True)
    picked = []
    fcache = {}

    def _f(a):
        if id(a) not in fcache:
            fcache[id(a)] = story_features(a)
        return fcache[id(a)]

    def _twin(art):
        return any(same_story_f(_f(art), _f(p)) for p in picked)

    must_read = []
    for art in ranked:
        if len(must_read) >= MUST_READ_SLOTS:
            break
        if _must_read_eligible(art) and not _twin(art):
            art["_category"] = "must_read"
            must_read.append(art)
            picked.append(art)

    # Articles that qualified for must-read but lost the cap stay in the maybe pool
    # (ranked by score like everything else) rather than being discarded — discarding
    # them once dropped an 18.0 / 6-keyword article while a 12.0 / 2-keyword one showed.
    # Maybe-relevant: by score, ≤ MAX_PER_SOURCE_MAYBE per source (B2), no twins.
    per_source: dict = {}
    maybe_rel = []
    per_area: dict = {}
    # Diversity: a single-area story beyond MAYBE_MAX_PER_AREA only goes in if no story
    # from another area clears MAYBE_OTHER_AREA_MIN — checked lazily below.
    def _placeable(a):
        o = outlet_key(a) or a["source"]
        return per_source.get(o, 0) < MAX_PER_OUTLET_MAYBE.get(o, MAX_PER_SOURCE_MAYBE)

    def _other_area_waiting(area):
        # Only a story that could actually be PLACED counts — one blocked by its outlet
        # cap would otherwise "wait" forever and leave the section short (lap-2 gate).
        return any(not any(a is m for m in must_read + maybe_rel)
                   and article_areas(a) and article_areas(a) != {area}
                   and a.get("_score", 0) >= MAYBE_OTHER_AREA_MIN and not _twin(a)
                   and _placeable(a)
                   for a in ranked)
    for art in ranked:
        if len(maybe_rel) >= MAYBE_SLOTS:
            break
        if any(art is m for m in must_read):
            continue
        areas = article_areas(art)
        if len(areas) == 1:
            (area,) = areas
            if per_area.get(area, 0) >= MAYBE_MAX_PER_AREA and _other_area_waiting(area):
                continue
        # Per OUTLET (The Verge + The Verge AI are one), so one publisher can't take 4 of
        # 5 slots through its two feeds.
        src = outlet_key(art) or art["source"]
        if per_source.get(src, 0) >= MAX_PER_OUTLET_MAYBE.get(src, MAX_PER_SOURCE_MAYBE) or _twin(art):
            continue
        per_source[src] = per_source.get(src, 0) + 1
        for a_ in areas:
            per_area[a_] = per_area.get(a_, 0) + 1
        art["_category"] = "maybe_relevant"
        maybe_rel.append(art)
        picked.append(art)

    # Second pass: the area rule only REORDERS — if skipping single-area stories left
    # slots empty (the other-area stories ran out or were outlet-capped), fill them now,
    # still honouring the outlet cap and twins. One pass lost 1-2 slots (lap-3 gate).
    for art in ranked:
        if len(maybe_rel) >= MAYBE_SLOTS:
            break
        if any(art is m for m in must_read) or any(art is m for m in maybe_rel):
            continue
        if not _placeable(art) or _twin(art):
            continue
        src = outlet_key(art) or art["source"]
        per_source[src] = per_source.get(src, 0) + 1
        art["_category"] = "maybe_relevant"
        maybe_rel.append(art)
        picked.append(art)

    return must_read, maybe_rel

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # --dry-run (passed by the wrapper's --dry-run): fetch and write articles.json as
    # usual, but leave feed_health.json alone — its counters count RUNS, so a few manual
    # dry runs during an outage would otherwise flag a feed dead.
    dry_run = "--dry-run" in sys.argv[1:]
    print("Loading interests...")
    interests = load_interests()
    print(f"  Topics: {', '.join(interests.get('topics', []))}")

    print("\nLoading seen articles (last 7 days)...")
    recent_shown = []
    seen_hashes = load_seen_hashes(recent_shown, HOLD_BACK_DAYS)
    # A ledger entry carries ~3 identity hashes (url / title / legacy), so the hash count
    # is not an article count — the old line reported 156 "articles" for 52.
    print(f"  {len(seen_hashes)} identity hashes in the window; "
          f"{len(recent_shown)} stories shown in the last {HOLD_BACK_DAYS}d (hold-back set)")

    # Reader ratings: read the reactions on recent briefs on a thread beside the fetch (it
    # costs no wall time). Bounded by its own budget; if it is late or fails, the ratings
    # already on disk are used. A dry run reads Discord but doesn't save.
    fb = {}
    def _sync():
        if feedback is None:
            fb["status"] = "unavailable (feedback.py failed to import)"
            return
        try:
            fb["status"], fb["ratings"] = feedback.sync_reactions(
                time.monotonic() + feedback.SYNC_BUDGET_SECONDS, persist=not dry_run)
        except Exception as e:                  # noqa: BLE001
            fb["status"] = f"failed: {type(e).__name__}: {e}"
    fb_thread = threading.Thread(target=_sync, daemon=True)
    fb_t0 = time.monotonic()
    fb_thread.start()

    print("\nFetching RSS feeds...")
    all_articles, statuses = fetch_all_feeds()
    if dry_run:
        print("  (--dry-run: feed_health.json not updated)")
    else:
        update_feed_health(statuses)
    usable_total = sum(s.get("usable", 0) for s in statuses.values())
    down = [n for n, s in statuses.items() if not s["ok"]]
    print(f"\nTotal articles fetched: {len(all_articles)} "
          f"({usable_total} usable, {len(down)}/{len(statuses)} feeds down)")

    # A near-total fetch failure is an infrastructure problem (DNS/network down at
    # 07:30), not "a quiet news day". Exiting 0 here let the pipeline post a brief
    # reading "0 articles scanned · 0 · 0 · 0 · 0" with no failure alert at all — the
    # dead-feed notice can't help either, since it needs DEAD_AFTER_FAILURES runs.
    # The check is on USABLE articles, not raw entries: an outage that leaves one
    # teaser-only feed alive still cannot produce a brief, and gating on `len(
    # all_articles) == 0` alone let that case through with exit 0.
    if usable_total == 0:
        print("[ERROR] no usable articles from any of the %d feeds (%d down) — "
              "treating as a fetch failure rather than an empty brief."
              % (len(statuses), len(down)), file=sys.stderr)
        sys.exit(1)

    fb_thread.join(timeout=max(0.0, feedback.SYNC_BUDGET_SECONDS + 2
                                - (time.monotonic() - fb_t0)))
    print(f"\nReader ratings: {fb.get('status', 'sync still running — using saved ratings')}")
    try:
        prefs = feedback.build_model(fb.get("ratings")) if feedback else None
    except Exception as e:                      # noqa: BLE001
        print(f"[WARN] ratings unusable ({type(e).__name__}: {e}) — ranking without them",
              file=sys.stderr)
        prefs = None
    if prefs:
        print(f"  {prefs.n[1]} 👍 · {prefs.n[-1]} 👎 → "
              f"{len(prefs.aff)} learned features")

    print("Matching against interests (with recency + authority scoring)...")
    annotate_story_tokens(all_articles)   # per-source boilerplate needs the whole fetch
    held_out = []
    matched, too_old, held_back = match_articles(all_articles, interests, seen_hashes,
                                                 recent_shown, held_out, prefs)
    print(f"Matched articles: {len(matched)}  (dropped {too_old} stale/undated, "
          f">{MAX_ARTICLE_AGE_DAYS}d old; {held_back} held back as a story already shown)")

    print("Deduplicating...")
    deduped = deduplicate(matched)
    print(f"After deduplication: {len(deduped)}")

    must_read, maybe_rel = classify(deduped)
    adjusted = [a for a in deduped if "_pref" in a]
    if adjusted:
        print(f"Preferences adjusted {len(adjusted)} of {len(deduped)} candidates "
              f"(×{min(a['_pref'] for a in adjusted):.2f}–×{max(a['_pref'] for a in adjusted):.2f})")
        for a in must_read + maybe_rel:
            if "_pref" in a:
                print(f"  [PREF] ×{a['_pref']:.2f} {a['title'][:70]}  ({a['_pref_why']})")

    total_scanned = len(all_articles)
    skipped       = total_scanned - len(matched)
    # Matched articles that didn't make it to final display, as known AT THIS STAGE:
    #  - dropped during dedup (lower-scoring in cluster)
    #  - demoted from must-read and then cut by the maybe-relevant cap
    #  - didn't make the maybe-relevant cap (ranked 6+) or the per-source cap
    # send_brief.py's three send-time dedup passes (mr_vs_may, within_mr, within_may)
    # run later and ADD their drops to this figure themselves — this stage cannot know
    # them, and the footer identity is what keeps the two halves consistent.
    matched_and_dropped = len(matched) - (len(must_read) + len(maybe_rel))

    result = {
        "total_scanned":       total_scanned,
        "skipped":             skipped,
        "matched_and_dropped": matched_and_dropped,
        "must_read":           must_read,
        "maybe_relevant":      maybe_rel,
        # re-tellings held back today: send_brief records them after delivery so they
        # stay held for their own eligible life (not just the shown story's)
        "held_retellings":     [{k: a.get(k) for k in ("title", "url", "source", "_story",
                                                       "_story_deep")}
                                | {"_strong": True,
                                   "_slots": [list(t) for t in story_features(a)[7]]}
                                for a in held_out],
    }

    os.makedirs(BASE_DIR, exist_ok=True)
    # Atomic write (temp + fsync + os.replace), same as feed_health/seen_articles: a
    # watchdog SIGKILL mid-write would otherwise leave a truncated articles.json that
    # summarize.py rejects, costing the brief.
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUTPUT_FILE)

    print(f"\n✓ Saved to {OUTPUT_FILE}")
    print(f"  {total_scanned} articles scanned")
    print(f"  {len(must_read)} must-read, {len(maybe_rel)} maybe-relevant, {skipped} skipped")

if __name__ == "__main__":
    main()
