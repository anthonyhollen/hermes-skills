"""Shared article identity helpers (used by fetch_rss.py AND send_brief.py).

Lives in one module on purpose: the seen-ledger is *written* by send_brief.py and
*read* by fetch_rss.py, so if the two computed identity differently the ledger would
silently stop matching. (A shared helper that drifts between its two callers is a
bug-class in its own right — keep both sides importing from here.)

Why identity is not just `title|source`
---------------------------------------
`feeds.py` subscribes to a publisher's main feed *and* its topic sub-feed for two
outlets (The Verge / The Verge AI, Ars Technica / Ars Technica AI). The same article
then arrives twice with the same url and title but a different `source`, so the old
`sha256(title|source)` hash produced two different ids. Consequences, both observed
live: the article was re-featured on a second day (the seen-check missed the sibling
copy), and `_num_sources` counted the two feeds as two corroborating outlets, which
promoted a single-outlet story into must-read.

So: identify an article by its URL and by its canonical title, and identify an
*outlet* by the article's host — never by the feed's display name.
"""

import hashlib
import html
import re
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

__all__ = ["normalize_url", "canonical_title", "article_hashes", "primary_hash",
           "legacy_hash", "outlet_key", "MIN_CONTENT_CHARS", "clean_text",
           "title_tokens", "title_overlap", "annotate_story_tokens", "story_tokens",
           "content_similarity", "same_story", "same_story_f", "story_features", "story_overlap_f",
           "index_keys", "AGGREGATOR_SOURCES", "deep_tokens", "with_slots", "firm_story_f"]

# Paywall guard: an RSS body shorter than this is a teaser, not an article. Single
# owner for the whole pipeline — fetch_rss uses it to zero teasers and to count a
# feed's "usable" entries, summarize uses it to decide an extract is impossible, and
# send_brief uses it to choose the paywalled-placeholder wording. It lived as three
# independent literals until 2026-09-14, which is how they drift apart.
MIN_CONTENT_CHARS = 200


def normalize_url(url: str) -> str:
    """Lowercase host, drop a leading `www.`, drop the fragment and trailing slash.

    `www.` is stripped only as a leading label — an unanchored `.replace("www.", "")`
    also mangles hosts that merely contain it (`wwww.x.com` -> `wx.com`).
    """
    if not isinstance(url, str) or not url:
        return ""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return parsed._replace(netloc=netloc, fragment="").geturl().rstrip("/")


def canonical_title(title: str) -> str:
    """Casefold + collapse every non-alphanumeric run to a single space.

    Makes the title identity robust to the punctuation/entity differences between two
    feeds of the same publisher (curly vs straight quotes, `&amp;` vs `&`).
    """
    if not isinstance(title, str):
        title = "" if title is None else str(title)
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def legacy_hash(art: dict) -> str:
    """The pre-2026-09-14 identity: sha256("title|source").

    Still written as each ledger entry's `hash` field so that reverting this change
    set (restoring the previous scripts from their .bak copies) leaves a ledger the
    OLD reader can still match — otherwise a rollback would blind the 7-day dedup
    window and produce a week of duplicate stories.
    """
    return _h("%s|%s" % (art.get("title", ""), art.get("source", "")))


def article_hashes(art: dict) -> list:
    """Every identity hash for an article, most specific first.

    An article is "already seen" if ANY of these matches a stored hash. The list
    includes the legacy `title|source` hash so a ledger written by the previous
    version keeps suppressing correctly through its 7-day window (without it, every
    article recorded before this change would become re-eligible at once).
    """
    hashes = []
    url = normalize_url(art.get("url", "") or "")
    if url:
        hashes.append(_h("u|" + url))
    title = canonical_title(art.get("title", ""))
    if title:
        hashes.append(_h("t|" + title))
    hashes.append(legacy_hash(art))
    return hashes


def primary_hash(art: dict) -> str:
    """The single most-specific id (url if present, else canonical title)."""
    return article_hashes(art)[0]


# Two-label public suffixes we actually encounter. Used only to find the registrable
# domain for outlet identity; an unknown suffix simply falls back to the last two
# labels, which is the conservative (merge-more) direction for this purpose.
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "or.jp", "com.sg", "edu.sg", "gov.sg", "co.nz", "com.br",
    "co.in", "com.hk", "com.cn", "com.tw", "co.kr", "com.mx",
})


def outlet_key(art: dict) -> str:
    """The publishing outlet, derived from the article's REGISTRABLE domain.

    Used for the cross-source corroboration count. Not the feed name: The Verge and
    The Verge AI both link to `theverge.com`, and counting feed names let one outlet
    subscribed twice satisfy the "≥2 outlets" must-read path.

    Not the full host either — that was the first attempt, and the terminal gate
    disproved the "over-counting is conservative" claim behind it: Slashdot serves one
    feed across five subdomains (news./games./science./yro./slashdot.org), so two
    clustered Slashdot articles reported `_num_sources = 2` and were promoted to
    must-read from a single outlet. Over-counting is NOT safe here; it is precisely the
    direction that fabricates corroboration. Collapsing to the registrable domain fixes
    both cases, and classify() additionally clamps the count to the number of distinct
    feed names so an unknown suffix can't reintroduce the inflation.
    """
    host = urlparse(art.get("url", "") or "").netloc.lower().split(":")[0] \
        if isinstance(art.get("url", ""), str) else ""
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) > 2:
        last_two = ".".join(parts[-2:])
        host = ".".join(parts[-3:]) if last_two in _MULTI_LABEL_SUFFIXES else last_two
    elif parts:
        host = ".".join(parts)
    src = art.get("source", "")
    return host or (src if isinstance(src, str) else "")


# ── Same-story test (shared by fetch_rss selection, send_brief's backstop, and the
#    cross-day hold-back) ─────────────────────────────────────────────────────────
# One predicate, one module: fetch_rss clusters and selects with it, send_brief re-checks
# with it, and the hold-back compares against ledger entries with it. When send_brief had
# its own copy (and fetch_rss used titles only), the fetch side filled 3+5 slots that the
# send side then emptied again — 4 of 12 briefs after 1.9.0 went out short.
#
# Calibrated 2026-09-26 on a 175-article live pool plus the June Fable-5 incident
# (references/content-dedup-analysis.md). The old rule — content Jaccard > 12% OR
# title > 40% — flagged ~half of its content matches wrongly: same-outlet boilerplate
# (MacRumors' "originates from Apple's own code (spotted by … Aaron Perris)" sentence put
# "iPad mini 8" vs "HomePod mini 2" at 36%), undecoded entities (&nbsp; &ldquo; counted as
# shared words) and generic launch vocabulary. The fix is three-part: decode entities,
# strip per-source boilerplate, and require the TITLES to name the same thing (>= 2
# distinctive shared words) before a content match counts.

TITLE_DUP_THRESHOLD      = 0.40   # title Jaccard alone, across outlets
SAME_OUTLET_TITLE_THRESHOLD = 0.65  # title Jaccard alone, within one outlet
CONTENT_DUP_THRESHOLD    = 0.12   # content Jaccard (boilerplate-stripped) ...
CONTENT_DUP_MIN_SHARED   = 2      # ... plus this many distinctive shared title words
CONTENT_WEAK_THRESHOLD   = 0.08   # a weaker content match ...
CONTENT_WEAK_MIN_SHARED  = 3      # ... needs this many shared title words
# Within ONE outlet the content path is much stricter: an outlet's items share house
# vocabulary well beyond the boilerplate shingles (TechPowerUp "RTX 30-Series Smooth
# Motion" vs "RTX 60-Series slips to 2028" hit 10%; Tom's "Google orbital data center" vs
# "China AI satellite" 14%). At the lap-1 gate 80 of 174 matches in a full fetch were
# same-outlet and most were distinct stories.
# At lap 2 even that was wrong every time (11 of 11 same-outlet non-URL merges were
# distinct stories: puzzle-hint pages, "How to watch X vs Y", PC Gamer posts sharing one
# lede), so within one outlet the content path is gone: a same-outlet pair is the same
# story only by URL, or by a > 65% title match that ALSO has > 25% content overlap on
# >= 8 shared content words.
SAME_OUTLET_CONTENT_THRESHOLD = 0.25
# Across outlets, a lede this similar is syndication whatever the headlines say (Techmeme
# quoting The Verge: 0.94 with one shared title word, so the title gate missed it).
SYNDICATED_CONTENT_THRESHOLD  = 0.50
SAME_OUTLET_MIN_CONTENT_WORDS = 8
BOILERPLATE_SHINGLE      = 5      # words per shingle
BOILERPLATE_MIN_ITEMS    = 3      # a shingle in >= this many of one source's items is boilerplate
STORY_CONTENT_CHARS      = 300    # compare the lede only
# Past the lede (1.13): some outlets open with commentary, so their lede shares nothing
# with anyone else's. PC Gamer's StarCraft-cheating story opened on "LLM companies have
# been accused of stealing human work…", so it overlapped The Verge's copy 0.03 on the first
# 300 chars but 0.19 on the first 1,000, and the pair ran as must-read + maybe (Oct 5;
# its Oct 1 "Super Intelligence" story ran twice the same way). So across outlets, titles
# sharing >= DEEP_MIN_SHARED distinctive words also match on the first DEEP_CONTENT_CHARS
# (boilerplate-stripped the same way) at >= DEEP_CONTENT_THRESHOLD, if both were published
# within DEEP_MAX_PUB_GAP_HOURS of each other. Calibrated 2026-10-05 on a full fetch (797
# fresh articles): 5 pairs reach 0.171-0.206, all the same story, published 4.5-21.4 h
# apart (StarCraft x2, Capcom's RE Engine, Shield TV price x2). The risk is FOLLOW-UPS —
# a new development recaps the original in paragraphs 2-3, which is this window: Intel
# Nova Lake (a second leak, 50 h later) scored 0.149 and Deadlock (17.5 h) 0.147, and
# hand-written follow-ups 0.15-0.33 (adversarial review). Hence the gap limit, and a
# threshold midway between 0.147 and 0.171. The margin is thin and rests on 3 stories —
# recalibrate on more fetches before moving it. One shared title word is not enough at
# any depth (Halide vs Final Cut Camera 0.23, "local", "risk").
DEEP_CONTENT_CHARS       = 1000
DEEP_CONTENT_THRESHOLD   = 0.16
DEEP_MIN_SHARED          = 2
DEEP_MAX_PUB_GAP_HOURS   = 36

# Feeds that relay many publishers: their items are NOT "one outlet" for the
# same-outlet threshold (a Techmeme item is Reuters, the next one Bloomberg).
AGGREGATOR_SOURCES = frozenset({"Techmeme", "Slashdot", "Reddit r/LocalLLaMA"})

_STOP_WORDS = frozenset({
    'the', 'a', 'an', 'to', 'in', 'on', 'of', 'for', 'and', 'with', 'that',
    'this', 'is', 'are', 'was', 'were', 'has', 'have', 'had', 'been', 'will',
    'would', 'could', 'its', 'their', 'his', 'her', 'it', 'they', 'from',
    'by', 'as', 'at', 'after', 'over', 'than', 'more', 'also', 'said', 'but',
    'not', 'all', 'be', 'do', 'does', 'no', 'so', 'if',
})
# Words that can't identify a story on their own: they're in dozens of unrelated
# titles every day (the brand/platform words are this user's interest topics).
_GENERIC_TITLE_WORDS = _STOP_WORDS | frozenset({
    'new', 'how', 'what', 'why', 'who', 'your', 'you', 'now', 'just', 'get', 'gets',
    'can', 'may', 'says', 'say', 'here', 'first', 'about', 'into', 'out', 'up', 'our',
    'one', 'two', 'top', 'best', 'more', 'most', 'report', 'reportedly', 'update',
    'updates', 'launch', 'launches', 'released', 'release', 'review', 'feature',
    'features', 'model', 'models', 'deal', 'deals', 'apple', 'google', 'meta',
    'microsoft', 'amazon', 'ios', 'macos', 'iphone', 'mac', 'ai', 'app', 'apps',
    'releases', 'unveils', 'unveiled', 'introduces', 'introduced', 'announces',
    'announced', 'debuts', 'launched', 'reveals', 'revealed', 'gaming', 'series', 'pro',
    'laptop', 'laptops', 'inch', 'week', 'today', 'podcast', 'episode',
    'introducing', 'announcing', 'meet', 'hands', 'look',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'january', 'february', 'march', 'april', 'june', 'july', 'august', 'september',
    'october', 'november', 'december',
})
# Aggregators append the original publisher to the title — "… (New York Times)",
# "… (@openai)" — and open the lede with "Wall Street Journal : …". Those are
# attribution, not story, and gave two unrelated WSJ items 2-3 "shared title words".
ATTRIBUTION_SOURCES = frozenset({"Techmeme"})   # a Slashdot "(…)" is part of the headline
_ATTRIB_TITLE_RE = re.compile(r"\s*\([^()]{1,60}\)\s*$")
_ATTRIB_LEDE_RE  = re.compile(r"^[^:.!?]{1,60}\s:\s")
_WORD_RE = re.compile(r"\w+")


_UN_RE = re.compile(r"\bU\.N\.(?=\s|$|[,;:)])|\bUnited Nations\b")

def clean_text(text) -> str:
    """Decode HTML entities (repeatedly — feeds double-encode, `&amp;#8217;`). Also used
    for DISPLAYED titles, so it must not rewrite wording (see _norm for comparison-only)."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _norm(text) -> str:
    """Comparison-only normalisation: "U.N." / "United Nations" -> "UN" (two outlets'
    ledes on the Sep 26 UN story said one each)."""
    return _UN_RE.sub("UN", clean_text(text))


def _fold(w: str) -> str:
    """Crude plural fold so "avatars"/"avatar" and "releases"/"release" match."""
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


_GENERIC_FOLDED = {_fold(w) for w in _GENERIC_TITLE_WORDS}


def _title_text(title, source=None) -> str:
    t = _norm(title)
    if source in ATTRIBUTION_SOURCES:
        t = _ATTRIB_TITLE_RE.sub("", t)
    return t


def title_tokens(title, source=None) -> set:
    return {_fold(w) for w in _WORD_RE.findall(_title_text(title, source).lower())}


_CAPS2_COMMON = frozenset({
    "us", "uk", "ai", "tv", "pc", "vr", "ar", "ok", "ev", "pr", "gb", "tb", "id", "ii",
    "hp", "os", "hn", "5g", "4k", "ip", "it", "at", "is", "to", "in", "on", "of", "an",
    "as", "be", "by", "do", "go", "if", "me", "my", "no", "or", "so", "up", "we", "vs"})

def _caps2(title, source=None) -> set:
    """Two-letter all-caps words (UN, EU) — distinctive, but too short for the
    3-letter rule. Excludes common ones ("(PR)" prefixes 48 TechPowerUp titles, "EV" is
    in every Electrek headline) and ignores an ALL-CAPS headline entirely."""
    t = _title_text(title, source)
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) > 0.6 * len(letters):
        return set()
    return {w.lower() for w in re.findall(r"\b[A-Z]{2}\b", t)} - _CAPS2_COMMON


def title_overlap(title_a, title_b) -> float:
    """Jaccard overlap of two titles' word sets (entity-decoded, case-insensitive)."""
    a, b = title_tokens(title_a), title_tokens(title_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _distinctive(tokens: set) -> set:
    return {w for w in tokens
            if len(w) >= 3 and not w.isdigit() and w not in _GENERIC_FOLDED}


def _content_words(text, source=None, chars=STORY_CONTENT_CHARS) -> list:
    t = _norm(text)
    if source in ATTRIBUTION_SOURCES:
        t = _ATTRIB_LEDE_RE.sub("", t)
    return [_fold(w) for w in _WORD_RE.findall(t[:chars].lower())]


def annotate_story_tokens(articles: list) -> None:
    """Set `_story` (the lede's boilerplate-stripped content words) and `_story_deep`
    (the same for the first DEEP_CONTENT_CHARS) on every article, in place.

    Boilerplate = any BOILERPLATE_SHINGLE-word run that appears in at least
    BOILERPLATE_MIN_ITEMS different items of the same source, within the same window. Run
    over the WHOLE fetch (not just the candidates) so a source's recurring sentence is
    recognised even when only two of its items are candidates. (Two same-story items from
    one outlet share runs too, but only 2 items — below the threshold — so their words stay.)
    Each window finds its own boilerplate, so `_story` is exactly what it was before
    `_story_deep` existed.
    """
    for key, chars in (("_story", STORY_CONTENT_CHARS), ("_story_deep", DEEP_CONTENT_CHARS)):
        _annotate_window(articles, key, chars)


def _annotate_window(articles: list, key: str, chars: int) -> None:
    by_source = defaultdict(list)
    words = []
    for art in articles:
        w = _content_words(art.get("content", ""), art.get("source"), chars)
        words.append(w)
        by_source[art.get("source", "")].append(len(words) - 1)
    n = BOILERPLATE_SHINGLE
    for src, idxs in by_source.items():
        counts = defaultdict(int)
        for i in idxs:
            for sh in {tuple(words[i][k:k + n]) for k in range(len(words[i]) - n + 1)}:
                counts[sh] += 1
        boiler = {sh for sh, c in counts.items() if c >= BOILERPLATE_MIN_ITEMS}
        for i in idxs:
            w = words[i]
            drop = set()
            if boiler:
                for k in range(len(w) - n + 1):
                    if tuple(w[k:k + n]) in boiler:
                        drop.update(range(k, k + n))
            articles[i][key] = sorted({t for j, t in enumerate(w)
                                       if j not in drop and t not in _STOP_WORDS
                                       and len(t) > 1})


def story_tokens(art: dict) -> set:
    """The article's comparison words: `_story` if annotated, else computed plainly."""
    st = art.get("_story")
    if isinstance(st, list):
        return {t for t in st if isinstance(t, str)}
    return {t for t in _content_words(art.get("content", ""), art.get("source"))
            if t not in _STOP_WORDS and len(t) > 1}


def deep_tokens(art: dict) -> set:
    """The first DEEP_CONTENT_CHARS' comparison words: `_story_deep` if annotated (or loaded
    from the ledger), else computed plainly. A ledger entry from before 1.13 has neither a
    `_story_deep` nor a body, so this is empty and the deep path never fires against it."""
    st = art.get("_story_deep")
    if isinstance(st, list):
        return {t for t in st if isinstance(t, str)}
    return {t for t in _content_words(art.get("content", ""), art.get("source"), DEEP_CONTENT_CHARS)
            if t not in _STOP_WORDS and len(t) > 1}


def content_similarity(art_a: dict, art_b: dict) -> float:
    a, b = story_tokens(art_a), story_tokens(art_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def story_features(art: dict) -> tuple:
    """Everything same_story needs, computed once per article: clustering compares each
    article with many others, and re-tokenising both sides on every call made the 1.10
    dedup ~27x slower than the old title-only one (18.8 s for 1,200 candidates)."""
    src = art.get("source")
    url = art.get("url", "")
    title = art.get("title", "")
    ttoks = title_tokens(title, src)
    return (normalize_url(url if isinstance(url, str) else ""), ttoks,
            _distinctive(ttoks), _caps2(title, src), story_tokens(art),
            src, outlet_key(art) if isinstance(url, str) and url else None,
            _slots(title, src, art.get("content", "") if isinstance(art.get("content"), str) else ""),
            deep_tokens(art), _pub_ts(art))


def _pub_ts(art: dict):
    """`published` (an ISO string) as a timestamp, or None — a ledger entry has none. A
    naive time is UTC, as in fetch_rss.parse_published."""
    p = art.get("published")
    if not isinstance(p, str) or not p:
        return None
    try:
        dt = datetime.fromisoformat(p.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def with_slots(feat: tuple, slots) -> tuple:
    """`feat` with its sequel flags (element 7) replaced by `slots` — for a ledger or
    ratings-manifest entry whose flags were judged with its own body at send time. Keeps
    every element after 7: slicing to `feat[:7] + (slots,)` silently dropped the deep tokens."""
    return feat[:7] + (slots,) + feat[8:]


# Tokens keep their case (for the name test) and join "48,000" / "27.0.1" into one.
_SLOT_RE = re.compile(r"[A-Za-z0-9]+(?:[.,][0-9]+)*")
SLOT_VETO_MAX_CONTENT = 0.90     # an identical lede overrides the sequel test

def _slot_conflict(title_a, title_b, src_a=None, src_b=None) -> bool:
    """Two headlines identical except for 1-2 swapped IDENTIFIERS are SEQUELS, not
    re-tellings: "Claude Opus 5.5" / "Claude Sonnet 5.5", "RTX 5080 Super review" /
    "RTX 5090 Super review", "iOS 27.0.1 With Bug Fixes" / "iOS 27.0.2 …" (0.64-0.80
    title Jaccard, lap-4 gate). Aligned by position; what counts as an identifier is
    defined in _slots(). Without a body, Title Case names don't count (see _slots)."""
    return _slot_conflict_t(_slots(title_a, src_a), _slots(title_b, src_b))


_SEQ_WORDS = frozenset({"beta", "part", "episode", "season", "chapter", "day", "round",
                        "phase", "vol", "volume", "issue", "gen", "generation"})
_ORDINALS = frozenset({"first", "second", "third", "fourth", "fifth", "sixth", "seventh",
                       "eighth", "ninth", "tenth"})
_MONTHS = frozenset({"january", "february", "march", "april", "may", "june", "july",
                     "august", "september", "october", "november", "december"})
_UNIT_RE = re.compile(r"\d+(?:[.,]\d+)*(?:b|bn|m|mn|k|tn)?", re.I)

def _slots(title, source=None, body="") -> tuple:
    """(token, is_identifier) per title token. Identifiers: version-like tokens, a bare
    number right after a sequel word ("beta 2"), after a token already judged an
    identifier, after a mixed-case or all-caps stem ("GPT-6"/"GPT-7", "iPhone 17"), or after a
    leading product name ("Gemini 4 hands-on") — not after any other Title Case
    word ("…In 103 Seconds" / "…In 100 Seconds" is a rounding, not a sequel); ordinals
    before beta/part…; "Month YYYY"; and Capitalised names — in a sentence-case headline
    any Capitalised non-generic word after position 0; in a Title Case headline only if
    `body` capitalises it too (so "Opus"/"Sonnet" count, "Deletes"/"Erases" don't).
    Currency amounts never count ("$380B" vs "$350B")."""
    text = _title_text(title, source)
    toks = [(m.group(0), text[m.start() - 1:m.start()] if m.start() else "")
            for m in _SLOT_RE.finditer(text)]
    # (w.islower(), not w[:1]: "iPhone"/"macOS"/"eBay" start lower-case in Title Case too)
    sentence_case = any(w.islower() and len(w) >= 4 and w.isalpha() for w, _ in toks)
    body_caps = set(re.findall(r"\b[A-Z][A-Za-z0-9]+", clean_text(body)[:STORY_CONTENT_CHARS * 2])) \
        if body and not sentence_case else set()
    out = []
    for k, (w, before) in enumerate(toks):
        low = _fold(w.lower())
        prev_raw = toks[k - 1][0] if k else ""
        prev = prev_raw.lower()
        nxt = toks[k + 1][0] if k + 1 < len(toks) else ""
        # amounts: "$380B", "50bn" — and, by the same suffix, sizes like "70B"/"8K",
        # which are therefore never sequel identifiers either
        money = (before != "" and before in "$£€") or \
            (_UNIT_RE.fullmatch(w) is not None and w[-1:].isalpha())
        version = not money and (
            ("." in w and any(c.isdigit() for c in w))
            or (any(c.isdigit() for c in w) and any(c.isalpha() for c in w))
            or (w.isdigit() and len(w) >= 4)
            or (w.isdigit() and (prev in _SEQ_WORDS or (k and out[k - 1][1])
                                 or (prev_raw.isalpha() and any(c.isupper() for c in prev_raw[1:]))
                                 # a leading product name: "Gemini 4 hands-on" / "Llama 5 is here"
                                 or (k == 1 and prev_raw[:1].isupper() and prev_raw.isalpha()
                                     and len(prev_raw) >= 3
                                     and _fold(prev) not in _GENERIC_FOLDED)))
            or (low in _ORDINALS and nxt.lower() in _SEQ_WORDS)
            or (low in _MONTHS and nxt.isdigit() and len(nxt) == 4))
        name = w[:1].isupper() and len(w) >= 3 and low not in _GENERIC_FOLDED and (
            (sentence_case and k > 0) or (not sentence_case and w in body_caps))
        out.append((low, version or name))
    return tuple(out)


def _slot_conflict_t(a: tuple, b: tuple) -> bool:
    if len(a) != len(b) or len(a) < 3:
        return False
    diff = [(x, y) for x, y in zip(a, b) if x[0] != y[0]]
    if not 1 <= len(diff) <= 2:
        return False
    return all(x[1] and y[1] for x, y in diff)


def _same_outlet_f(fa: tuple, fb: tuple) -> bool:
    if fa[5] in AGGREGATOR_SOURCES or fb[5] in AGGREGATOR_SOURCES:
        return False
    return fa[5] == fb[5] or (fa[6] is not None and fa[6] == fb[6])


def same_story_f(fa: tuple, fb: tuple):
    """same_story on precomputed story_features()."""
    if fa[0] and fa[0] == fb[0]:
        return "url"
    sa, sb = fa[4], fb[4]
    c = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
    cross = not _same_outlet_f(fa, fb)
    # Sequel veto FIRST (press-release boilerplate gives real sequels 0.58-0.88 lede
    # overlap across outlets). Skipped across outlets only for a near-identical lede;
    # within one outlet it always applies — its sequels share a template lede (MacRumors
    # "Second Beta of iOS 27.1" / "Third Beta": 0.92).
    if (not cross or c < SLOT_VETO_MAX_CONTENT) and _slot_conflict_t(fa[7], fb[7]):
        return None                       # a sequel ("Opus 5.5" / "Sonnet 5.5"), not a twin
    if cross and c >= SYNDICATED_CONTENT_THRESHOLD and \
            min(len(sa), len(sb)) >= SAME_OUTLET_MIN_CONTENT_WORDS:
        return f"content@{c:.0%}"         # syndicated lede, whatever the headlines say
    ta, tb = fa[1], fb[1]
    inter = ta & tb
    # A title overlap made only of stop words ("It is over" / "Is it over?") is not a
    # story match — and index_keys() skips stop words, so it could never be found anyway.
    t = len(inter) / len(ta | tb) if ta and tb and (inter - _STOP_WORDS) else 0.0
    if not cross:
        if t > SAME_OUTLET_TITLE_THRESHOLD:
            both = len(sa & sb)
            c = both / len(sa | sb) if sa and sb else 0.0
            if c > SAME_OUTLET_CONTENT_THRESHOLD and both >= SAME_OUTLET_MIN_CONTENT_WORDS:
                return f"title@{t:.0%}"
        return None
    shared = len(fa[2] & fb[2]) + len(fa[3] & fb[3])
    # A cross-outlet title match must share at least one DISTINCTIVE word: "Introducing
    # GPT-5.5" ~ "Introducing Qwen1.5" is 50% on "introducing" + "5" alone.
    if t > TITLE_DUP_THRESHOLD and shared >= 1:
        return f"title@{t:.0%}"
    if shared >= CONTENT_DUP_MIN_SHARED:
        if (c > CONTENT_DUP_THRESHOLD and shared >= CONTENT_DUP_MIN_SHARED) or \
           (c > CONTENT_WEAK_THRESHOLD and shared >= CONTENT_WEAK_MIN_SHARED):
            return f"content@{c:.0%}"
    # Past the lede (see DEEP_CONTENT_CHARS): needs both sides' deep words, so an entry
    # without them (pre-1.13 ledger, ratings manifest) never matches this way. A feature
    # tuple built before element 8 existed is treated the same. The publication gap is
    # checked only when both dates are known (a ledger entry has none; the hold-back's
    # own timing rules bound that case).
    if shared >= DEEP_MIN_SHARED and len(fa) > 8 and len(fb) > 8:
        da, db = fa[8], fb[8]
        pa, pb = (fa[9], fb[9]) if len(fa) > 9 and len(fb) > 9 else (None, None)
        if pa is not None and pb is not None and abs(pa - pb) > DEEP_MAX_PUB_GAP_HOURS * 3600:
            return None
        if da and db and min(len(da), len(db)) >= SAME_OUTLET_MIN_CONTENT_WORDS:
            d = len(da & db) / len(da | db)
            if d >= DEEP_CONTENT_THRESHOLD:
                return f"deep@{d:.0%}"
    return None


def firm_story_f(fa: tuple, fb: tuple):
    """same_story_f without its two weakest paths — the 8-12% lede band (3 shared title
    words) and the deep path. For linking through a THIRD article (fetch_rss chained
    members), where a weak link hides a different story for days: on the Oct 5 fetch,
    content@10% links chained "zombies in GTA 5" under a GTA 6 story and a Satisfactory
    piece under "Crimson Desert DLC delayed"."""
    why = same_story_f(fa, fb)
    if not why or why.startswith("deep"):
        return None
    if why.startswith("content") and story_overlap_f(fa, fb)[1] <= CONTENT_DUP_THRESHOLD:
        return None
    return why


def story_overlap_f(fa: tuple, fb: tuple) -> tuple:
    """(title Jaccard, content Jaccard) on precomputed features — for callers that
    grade a match (the hold-back)."""
    ta, tb, sa, sb = fa[1], fb[1], fa[4], fb[4]
    return (len(ta & tb) / len(ta | tb) if ta and tb else 0.0,
            len(sa & sb) / len(sa | sb) if sa and sb else 0.0)


def same_story(a: dict, b: dict):
    """Return why `a` and `b` are the same story ("url", "title@41%", "content@15%",
    "deep@17%"), or None. Works on article dicts and on ledger entries
    (title/url/_story/_story_deep)."""
    return same_story_f(story_features(a), story_features(b))


_MINHASH_SEEDS = (0x9E37, 0x85EB, 0xC2B2, 0x27D4)

def index_keys(feat: tuple) -> set:
    """Blocking keys for deduplicate(): two articles are compared only if they share one.

    * url / non-stop title word — covers the url path, every title match (a stop-word-
      only overlap is rejected by same_story_f), the shared-title-word content path and
      the deep path (which needs >= 2 shared title words);
    * 4 min-hash keys of the story words — covers the syndicated-lede path, which needs
      no shared title word. A pair at the 0.50 threshold shares a given min-hash with
      probability 0.5, so it is missed with ~6% probability (at 0.9: ~0.01%); the next
      day's hold-back (content >= 0.35) still catches a missed copy.
    """
    keys = {"t:" + w for w in feat[1] if w not in _STOP_WORDS}
    if feat[0]:
        keys.add("u:" + feat[0])
    story = feat[4]
    if len(story) >= SAME_OUTLET_MIN_CONTENT_WORDS:
        for seed in _MINHASH_SEEDS:
            # crc32, not hash(): str hashing is randomised per process, which would make
            # a borderline syndicated pair cluster on one run and not the next.
            keys.add("m%x:%s" % (seed, min(story, key=lambda w: zlib.crc32(
                ("%x:%s" % (seed, w)).encode()))))
    return keys
