#!/usr/bin/env python3
"""
Step 3: Format the brief and send to Discord.
Usage: python send_brief.py [--dry-run]

Sends the brief as multiple Discord messages, all assembled by build_messages() so
--dry-run renders byte-for-byte what would be posted:
  1. Header
  2. Must-read articles (one message per article)
  3. Maybe-relevant articles (one message per article — so a 👍/👎 reaction names
     exactly one article; see feedback.py)
  4. Footer
  5. A feed-health notice, only when a feed is down or has stopped yielding articles
     (best-effort — its failure must never block the seen-ledger write)

After all messages are sent successfully, records the shown articles AND the twins the
send-time dedup dropped in seen_articles.json, so neither reappears in a future brief.
Then (best-effort, after the ledger) records each delivered article's message id in
briefs/<date>.json so fetch_rss can read the reader's reactions on it.
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from article_id import (MIN_CONTENT_CHARS, article_hashes, legacy_hash,  # noqa: E402
                        content_similarity, deep_tokens, same_story, story_tokens,
                        title_overlap, _slots)
try:        # briefs/<date>.json manifest for reaction ratings; optional (see record step)
    import feedback  # noqa: E402
except Exception as _e:                         # noqa: BLE001
    print(f"[WARN] feedback.py unavailable ({type(_e).__name__}: {_e})", file=sys.stderr)
    feedback = None

# ── Config ───────────────────────────────────────────────────────────────────

BASE_DIR          = os.path.expanduser("~/.hermes/morning-brief")
DEFAULT_CHANNEL   = "1499972245437550602"
ENV_FILE          = os.path.expanduser("~/.hermes/.env")


def _load_env_value(name: str) -> str:
    """Read KEY=value from ~/.hermes/.env (the cron scheduler scrubs secrets from the
    child environment, so this file — not os.environ — is the live source)."""
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ""


# Resolution order, applied independently per setting: environment first, then
# ~/.hermes/.env, then the built-in default.
#
# Previously the .env read was gated on `if not DISCORD_BOT_TOKEN`, and inside it
# CHANNEL_ID was overwritten unconditionally from DISCORD_HOME_CHANNEL — so an explicit
# DISCORD_BRIEF_CHANNEL_ID was honoured only when the bot token happened to be exported
# too, and silently ignored on the live cron path (which has neither). DISCORD_BRIEF_
# CHANNEL_ID was also never looked for in .env at all, despite being the documented
# override. Both settings now resolve the same way, so the documented knob works.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "") or _load_env_value("DISCORD_BOT_TOKEN")
CHANNEL_ID        = (os.environ.get("DISCORD_BRIEF_CHANNEL_ID", "")
                     or _load_env_value("DISCORD_BRIEF_CHANNEL_ID")
                     or os.environ.get("DISCORD_HOME_CHANNEL", "")
                     or _load_env_value("DISCORD_HOME_CHANNEL")
                     or DEFAULT_CHANNEL)

INPUT_FILE        = os.path.join(BASE_DIR, "articles_summarized.json")
SEEN_FILE         = os.path.join(BASE_DIR, "seen_articles.json")
FEED_HEALTH_FILE  = os.path.join(BASE_DIR, "feed_health.json")
DEAD_AFTER_FAILURES = 3      # mirror of fetch_rss.py — a feed "dead" after N failed runs
ZERO_YIELD_AFTER_RUNS = 3    # mirror of fetch_rss.py — fetches OK but yields nothing usable
MAX_AGE_DAYS      = 7
MONTHS            = ["Jan","Feb","Mar","Apr","May","Jun",
                     "Jul","Aug","Sep","Oct","Nov","Dec"]
# Section separator: a line holding only U+2800 (Braille blank). Discord treats it
# as a printable char so it survives trimming and renders as an empty row — unlike
# a plain blank line (collapsed) or "---" (a heavy horizontal rule). Prefixed with
# \n so it sits on its own line at the end of a section's last message.
SECTION_SEP       = "\n⠀"

# ── Discord send limits / retry (post_message) ─────────────────────────────────
# Discord rejects message content over 2000 chars (code 50035) and empty content
# (code 50006). The brief fires ~8-10 POSTs in a burst, so a transient 429 rate
# limit or 5xx is expected occasionally — retry those (honoring Retry-After)
# rather than aborting the whole brief mid-send (which would also skip seen-
# tracking and cause duplicate re-sends next run).
DISCORD_CONTENT_LIMIT = 2000     # hard char limit on a Discord message's content
MAX_SUMMARY_CHARS     = 1500     # cap a single summary so a message fits the limit
POST_RETRIES          = 4        # extra attempts after the first (transient only)
POST_BACKOFF_SECONDS  = 1.0      # linear backoff when Retry-After is absent
RETRY_AFTER_CAP       = 10.0     # never wait longer than this on a 429
TRANSIENT_DISCORD_HTTP = {429, 500, 502, 503, 504}
# Ledger retention. Twins removed by the send-time dedup get a SHORTER window than
# articles we actually showed: recording them is what stops the dropped copy being
# promoted as "new" on a later run. This shorter window only governs the twin's identity
# hashes, and only for twins dropped HERE (a rare backstop path since 1.10): the
# fetch-side hold-back matches ledger entries by url for the full week regardless, so a
# twin — true or false — is effectively suppressed for MAX_AGE_DAYS either way. That is
# the trade-off for never re-showing a true twin on day 4 (same-story precision measured
# ~85-90% at the gate). Fetch-side cluster twins are recorded with MAX_AGE_DAYS directly. Note fetch_rss
# reads the ledger ~40 s before the send stamps it, and runs jitter by ~1 min, so an
# N-day window suppresses N or N-1 runs — shortening it is how resurfacing happened.
DEDUPED_TWIN_TTL_DAYS = 3
# A chained cluster member (fetch_rss deduplicate `_chained`, 1.13): same story as one of
# the shown article's direct twins, not as the shown article itself. Recorded by identity
# only (`"kind": "chained"`, no story words, so it never joins the hold-back set). It
# was published BEFORE the story was shown, so by the third run it is past the 48 h
# recency boost and competes at x1. This window covers the runs where it would otherwise
# out-rank fresh news, without hiding a wrongly chained story for the whole week.
CHAINED_TTL_DAYS = 3
# Never prune below this many of the most recent entries, whatever the clock says.
# A single run with a forward-skewed clock (e.g. before NTP sync after a boot) computes
# a cutoff newer than every real entry and wipes the ledger, and the next correctly-timed
# run then re-sends a week of stories. Age-pruning is the normal path; this is the floor.
# (The Pi does have an RTC and NTP; this is cheap insurance, not a known failure.)
MIN_LEDGER_ENTRIES = 200
# Wall-clock budget for the whole send, mirroring summarize.py's STEP_BUDGET_SECONDS.
# Worst case for ONE message is 5 attempts x (10s connect + 15s read) + 4 x a 10s capped
# Retry-After ~= 165s, so a single rate-limited message could approach — and two could
# exceed — the wrapper's 240s send step timeout, and
# and a SIGKILL there is the worst place for one: the messages are already delivered but
# save_seen_articles never runs, so the entire brief re-sends next run. Bound the retry
# waits by the remaining budget instead, and give up on a message rather than the ledger.
SEND_BUDGET_SECONDS = 195
_SEND_DEADLINE = [float("inf")]

def _dlen(text: str) -> int:
    """Length as Discord counts it — UTF-16 code units, not Python code points.

    Astral characters (emoji, some CJK extensions) are 2 units each, so a message of
    1,852 Python chars can be 3,652 units and get rejected with code 50035 — a
    non-transient 4xx that aborts the brief mid-send. Counting the way the server does
    removes the question. For BMP-only text (the overwhelming majority) this equals
    len(text).
    """
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)

# ── Deduplication ─────────────────────────────────────────────────────────────

# Same-story test: article_id.same_story, shared with fetch_rss (clustering + selection)
# and the hold-back. This used to be a private copy here — title Jaccard > 40% OR content
# Jaccard > 12% over the raw first 300 chars — and it was both the backstop AND the only
# content-aware dedup, so it silently removed articles from an already-capped 3 + 5
# (4 of 12 briefs short after 1.9.0), and about half its content matches were false
# (same-outlet boilerplate, undecoded entities). fetch_rss now clusters with the same
# predicate, so these passes can't drop anything it produced (see main()).
token_overlap = title_overlap           # kept for REPL/back-compat callers

def dedup_mr_vs_may(mr: list, may: list, threshold: float = 0.40) -> tuple[list, list]:
    """Backstop: drop maybe-relevant articles that are the same story as a must-read.

    `threshold` is ignored (kept for call compatibility): the test is
    article_id.same_story. Returns (mr, filtered_may).
    """
    may_filtered = []
    for may_art in may:
        is_dup = False
        for mr_art in mr:
            by_what = same_story(mr_art, may_art)
            if by_what:
                print(f"[DEDUP] Dropping maybe-relevant duplicate: '{may_art['title'][:60]}' "
                      f"(overlaps with must-read '{mr_art['title'][:60]}' "
                      f"via {by_what})",
                      file=sys.stderr)
                is_dup = True
                break
        if not is_dup:
            may_filtered.append(may_art)
    return mr, may_filtered

def dedup_within_may(may: list, threshold: float = 0.40) -> list:
    """Backstop: collapse same-story variants within maybe-relevant (article_id.same_story),
    keeping the highest-scoring one. `threshold` is ignored (call compatibility).
    Motivating case: TechCrunch / Engadget / The Verge all covering one Siri story.
    """
    if not may:
        return may

    clusters = []   # list of (representative, [members])
    for art in may:
        placed = False
        for i, (rep, members) in enumerate(clusters):
            if same_story(art, rep):
                members.append(art)
                # Keep the highest-scoring article as representative
                if art["_score"] > rep["_score"]:
                    clusters[i] = (art, members)
                placed = True
                break
        if not placed:
            clusters.append((art, [art]))

    deduped = []
    dropped = []
    for rep, members in clusters:
        deduped.append(rep)
        if len(members) > 1:
            for m in members:
                if m is not rep:
                    dropped.append(m["title"][:60])

    if dropped:
        print(f"[DEDUP] Within-maybe-relevant: dropped {len(dropped)} variants: {dropped}",
              file=sys.stderr)

    return deduped

def dedup_within_mr(mr: list, threshold: float = 0.40) -> list:
    """Backstop: collapse same-story variants within must-read (article_id.same_story),
    keeping the highest-scoring one. `threshold` is ignored (call compatibility).
    Motivating case: two outlets reaching must-read by different routes (≥2 sources vs
    ≥3 keywords) with headlines too different for the title signal.
    """
    if len(mr) <= 1:
        return mr

    clusters = []   # list of (representative, [members])
    for art in mr:
        placed = False
        for i, (rep, members) in enumerate(clusters):
            if same_story(art, rep):
                members.append(art)
                if art["_score"] > rep["_score"]:
                    clusters[i] = (art, members)
                placed = True
                break
        if not placed:
            clusters.append((art, [art]))

    deduped = []
    dropped = []
    for rep, members in clusters:
        deduped.append(rep)
        if len(members) > 1:
            for m in members:
                if m is not rep:
                    dropped.append(m["title"][:60])

    if dropped:
        print(f"[DEDUP] Within-must-read: dropped {len(dropped)} variants: {dropped}",
              file=sys.stderr)

    return deduped

# ── Formatting helpers ─────────────────────────────────────────────────────────

def format_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{MONTHS[dt.month - 1]}-{dt.year}"

def _escape_plain(text: str) -> str:
    """Escape Markdown metacharacters in a short plain-text fragment (e.g. a feed name).

    A feed named `Ars_Technica` used to terminate the italic wrapper on the feed-health
    line mid-sentence."""
    out = re.sub(r"\s+", " ", text or "").strip()
    for ch in ("\\", "`", "*", "_", "~", "|"):
        out = out.replace(ch, "\\" + ch)
    return out

def dead_feeds_line() -> str:
    """One-line feed-health notice for the brief, or '' if all feeds are healthy.

    Reads feed_health.json (written by fetch_rss.py). A feed counts as dead after
    DEAD_AFTER_FAILURES consecutive failed runs. A feed that fetches fine but has
    produced no usable (non-teaser) article for ZERO_YIELD_AFTER_RUNS runs is reported
    as a COUNT only — those are long-lived conditions (a permanently teaser-only feed),
    so naming them daily would be noise; the names go to stderr from fetch_rss and to
    feed_health.json. Surfaced so feed rot is visible where the brief is read; run
    feed_doctor.py to get verified replacements.
    """
    if not os.path.exists(FEED_HEALTH_FILE):
        return ""
    try:
        with open(FEED_HEALTH_FILE) as f:
            health = json.load(f)
    except (ValueError, OSError):   # incl. UnicodeDecodeError from a truncated write
        return ""
    if not isinstance(health, dict):
        return ""
    def _as_int(value, default=0):
        """A hand-edited/foreign feed_health.json can hold a string or null, and comparing
        that to an int raised TypeError — here, AFTER every message had been delivered and
        before the ledger write, i.e. the one place this function must never fail."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    dead = sorted(name for name, v in health.items()
                  if isinstance(v, dict)
                  and _as_int(v.get("consecutive_failures")) >= DEAD_AFTER_FAILURES)
    zero_yield = sorted(name for name, v in health.items()
                        if isinstance(v, dict) and name not in dead
                        and _as_int(v.get("consecutive_zero_yield")) >= ZERO_YIELD_AFTER_RUNS)
    if not dead and not zero_yield:
        return ""

    parts = []
    if dead:
        # Cap both the name count AND each name's length: 12 pathologically long feed
        # names reached 5,008 chars, which post_message then clipped (non-fatal, but
        # the notice lost its tail and dry-run never counted it).
        shown = [_escape_plain(n)[:40] for n in dead[:12]]
        names = ", ".join(shown) + (f" +{len(dead) - 12} more" if len(dead) > 12 else "")
        parts.append(f"{len(dead)} source(s) down — {names}")
    if zero_yield:
        parts.append(f"{len(zero_yield)} fetching OK but yielding nothing usable")
    line = (f"🩺 _Feed health: {'; '.join(parts)}. "
            f"Ask Claude to run `feed_doctor.py` for verified replacements._")
    if _dlen(line) > DISCORD_CONTENT_LIMIT:
        line = line[:DISCORD_CONTENT_LIMIT - 2].rstrip() + "_"
    return line

# Markdown/link metacharacters escaped in an untrusted title. `\` is first so the
# escapes added for the others aren't themselves re-escaped. Escaping `[` and `]`
# alone prevents any masked-link `[label](url)` from forming in the title, so `(`/`)`
# are deliberately NOT escaped (avoids ugly backslashes on legit parenthetical
# headlines). `<`/`>` neutralize autolink/mention angle-bracket syntax.
_TITLE_META = ("\\", "`", "*", "_", "[", "]", "<", ">")
# A title long enough to fill a message on its own leaves no room for the closing
# `](<url>)**`, so post_message's last-resort truncation would cut the link's markdown
# in half and Discord would render a raw unterminated `**[`. No live feed does this,
# but the title is untrusted input.
MAX_TITLE_CHARS = 300
MAX_URL_CHARS   = 500

def _safe_url(url: str) -> str:
    """Make an untrusted feed URL safe to interpolate into `](<url>)`.

    The URL comes from the same untrusted RSS item as the title, but only the title was
    escaped: a `>` in the URL closed the angle bracket early and everything after it
    became live Markdown, so a crafted link could inject a clickable masked link into
    the brief (`…/a>)** CLICK [FREE BITCOIN](<https://evil>)`). Percent-encode the
    characters that can break out, drop whitespace, and refuse a non-http(s) scheme.
    """
    url = re.sub(r"\s+", "", url or "")
    if not url.lower().startswith(("http://", "https://")):
        return ""
    for ch, enc in (("<", "%3C"), (">", "%3E"), ("(", "%28"), (")", "%29"),
                    ("[", "%5B"), ("]", "%5D"), ("`", "%60"), ("\\", "%5C")):
        url = url.replace(ch, enc)
    return url[:MAX_URL_CHARS]

def format_headline(title: str, url: str) -> str:
    """Return bolded inline link: **[title](<url>)**

    Angle brackets around the URL are REQUIRED for Discord link-preview suppression.
    Without them, Discord auto-scrapes simonwillison.net and similar URLs even when
    embeds=[]/flags:4 are set in the API payload.

    BOTH fields are untrusted feed text (some feeds — e.g. Reddit self-posts — carry
    user-submitted titles, and the link is equally attacker-controllable). The title is
    decoded (&#8217; etc.), whitespace/newlines are collapsed to one line, length-capped,
    then every Markdown/link metacharacter is escaped; the URL is run through _safe_url.
    Together they ensure a crafted item cannot break out of the link label, inject a
    masked/phishing link (`x](<evil>)`), or forge extra lines/sections. The escape runs
    AFTER html.unescape so an entity-encoded payload (`&#91;` → `[`) can't slip through.
    Pings are additionally blocked by allowed_mentions on every POST.
    """
    escaped = html.unescape(title or "")
    # Drop the section-separator glyph for the same reason _defang_untrusted does: it is
    # what makes a section boundary meaningful, so untrusted text must never carry it.
    escaped = escaped.replace("\u2800", " ")
    escaped = re.sub(r"\s+", " ", escaped).strip()
    if len(escaped) > MAX_TITLE_CHARS:
        escaped = escaped[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] + "…"
    for ch in _TITLE_META:
        escaped = escaped.replace(ch, "\\" + ch)
    safe_url = _safe_url(url)
    if not safe_url:
        # No usable link. The title still goes out as plain message content, so defang
        # any bare URL inside it: without the `](<…>)` wrapper there is nothing else
        # suppressing an autolink, and an item that supplies no <link> (legal Atom —
        # fetch_rss then falls back to entry.id, e.g. a `tag:` URI) can put an
        # attacker's URL straight into the headline. Caught at the terminal gate.
        escaped = escaped.replace("://", ":" + "​" + "//")
        return f"**{escaped}**"
    return f"**[{escaped}](<{safe_url}>)**"

def _defang_untrusted(text: str) -> str:
    """Neutralize injection in free-text summary (LLM output over untrusted article
    content).

    Three classes of injection, all reachable because the summary is model output over
    attacker-controllable article text:
      - clickable links: break masked-link `](` and bare-autolink `://` with a
        zero-width space so a crafted summary can't inject a phishing link;
      - pings: `@everyone`/`@here` (belt-and-suspenders — allowed_mentions already
        blocks these server-side);
      - FORGED STRUCTURE: the summary was the only untrusted field never collapsed to
        one line, so a newline plus `### Must-read news` / `• **…**` / the U+2800
        section separator let it fabricate a whole extra section of the brief. Collapse
        all whitespace (summaries are a single paragraph by construction) and strip the
        separator glyph, which is what makes the section boundaries meaningful.
    """
    zwsp = "​"
    text = (text or "").replace("⠀", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("](", "]" + zwsp + "(").replace("://", ":" + zwsp + "//")
    text = text.replace("@everyone", "@" + zwsp + "everyone").replace("@here", "@" + zwsp + "here")
    return text

def strip_thinking(text: str) -> str:
    """Remove reasoning blocks — complete ones, plus a LEADING unterminated one.

    A model truncated by max_tokens mid-thought emits `<think>` with no closing tag;
    the non-greedy complete-block pattern leaves that untouched, so raw reasoning was
    delivered verbatim to Discord. The unterminated form is anchored to the start of
    the text: matching it anywhere ate legitimate prose ("…must strip the `<think>`
    tag…" lost its tail), and a truncated reasoning block always leads.
    (summarize.py strips the same two shapes; this is the defensive second line.)"""
    open_tag  = chr(60) + "think" + chr(62)
    close_tag = chr(60) + "/think" + chr(62)
    # IGNORECASE to match summarize.THINK_RE: without it an uppercase <THINK> block
    # passed straight through, and on the anthropic wire this is the ONLY stripper
    # (_extract_anthropic never calls strip_reasoning).
    complete    = re.compile(open_tag + r".*?" + close_tag, re.DOTALL | re.IGNORECASE)
    unterminated = re.compile(r"^\s*" + open_tag + r"(?!.*" + close_tag + r").*\Z",
                              re.DOTALL | re.IGNORECASE)
    return unterminated.sub("", complete.sub("", text)).strip()

def _cap_summary(summary: str, budget: int = MAX_SUMMARY_CHARS) -> str:
    """Cap a summary so headline + summary fits Discord's 2000-char message limit.

    A reasoning model given a 2000-token budget can occasionally ignore the
    '3-5 sentence' instruction and emit a very long summary. A FIXED 1500-char cap was
    not actually sufficient: today's longest real article has a 217-char title and a
    280-char URL, so its headline alone is 507 chars and a summary at the cap produced
    a 2010-char message — over the limit, with no split branch on the must-read path.
    Callers therefore pass a budget computed from the actual headline length; the
    constant is only the ceiling. Truncates on a word boundary with an ellipsis."""
    budget = max(80, min(budget, MAX_SUMMARY_CHARS))
    if _dlen(summary) <= budget:
        return summary
    # Trim by Discord units, not code points, so an emoji-dense summary still fits.
    out = summary
    while out and _dlen(out) > budget:
        out = out[: max(1, int(len(out) * budget / max(1, _dlen(out))))]
    # Prefer a word boundary, but only if one is near the end: rsplit keeps everything
    # before the LAST space, which for space-sparse text (CJK, a long URL/base64 token)
    # threw away almost the entire summary — 1,609 chars in, 9 chars out.
    head = out.rsplit(" ", 1)[0].rstrip()
    if len(head) >= len(out) * 0.8:
        out = head
    return out.rstrip() + "…"

def _is_content_copy(summary: str, content: str) -> bool:
    """True only when the WHOLE summary is (near-)verbatim article text.

    The old test — "the first 100 chars match the content's" — threw away good summaries
    that merely open by restating a short lede: the Sep 28 must-read #1 (a 532-char Decoder
    lede) had a correct 3-sentence summary whose sentences 2-3 were new, and was replaced by
    "Summary unavailable". Now: normalise both, and call it a copy only if >= 90% of the
    summary's word sequence matches the content's opening (difflib)."""
    import difflib
    norm = lambda t: re.findall(r"\w+", html.unescape(t).lower())
    s, c = norm(summary), norm(content)
    if not s or not c:
        return False
    head = c[:len(s) + 20]
    m = difflib.SequenceMatcher(None, s, head, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks()) >= 0.9 * len(s)

def get_summary(art: dict, budget: int = MAX_SUMMARY_CHARS) -> str:
    """Extract, clean, decode, and deduplicate summary from article."""
    raw = art.get("summary") or ""   # `or ""`, not .get(..,""), so an explicit null → ""
    summary = strip_thinking(raw)
    summary = html.unescape(summary)   # decode &#8217;, &#8212;, etc.

    # Extractive fallback from summarize.py (#3): the model couldn't summarize, so this
    # is the article's clean opening sentences. Show it as-is — do NOT run it through
    # the echo-detector below (it intentionally opens like the content, and a real
    # excerpt is more useful to the reader than "Summary unavailable").
    if art.get("summary_is_extractive") and summary.strip():
        return _cap_summary(_defang_untrusted(summary.strip()), budget)

    # Deduplicate: if summary repeats the same text block twice, truncate at the repeat
    # e.g. "Paragraph1.\n\n\nParagraph1.\n\n\nParagraph1." → "Paragraph1."
    if summary:
        # Try splitting on 2+ consecutive newlines and look for a repeated block
        parts = re.split(r'\n{2,}', summary)
        if len(parts) >= 2 and parts[0].strip() == parts[-1].strip():
            summary = parts[0].strip()

        # Also handle direct concatenation (no intervening blank line):
        # Case: "Sentence A. Sentence A. Sentence A." — same phrase repeated verbatim
        mid = len(summary) // 2
        first_half = summary[:mid].strip()
        second_half = summary[mid:].strip()
        if first_half and second_half and first_half == second_half:
            summary = first_half

        # MiniMax repeat-pattern bug: same sentence/phrases repeated with trailing
        # punctuation, e.g. "Drugs crystallize differently in microgravity. Drugs
        # crystallize differently in microgravity." Collapse the repeated run IN PLACE
        # (keep one copy) rather than returning just the repeating unit: the real
        # failure shape is "good summary, then degenerates into repetition", and
        # returning group(1) threw away the substance and kept only the junk.
        # `\S` anchors the unit to a non-space char so the separating space before it
        # stays outside the match and isn't eaten by the replacement.
        summary = re.sub(
            r'(\S.{29,}?)(?:\s*(?:\n\n?|\.)\s*)(?:\1\s*(?:\n\n?|\.)\s*){2,}',
            lambda m: m.group(1).strip() + ". ", summary, flags=re.IGNORECASE).strip()

    # Whether this text is the article's own opening rather than a synthesis. Set when
    # we build an excerpt ourselves, so the echo-detector below doesn't then discard
    # the excerpt it was handed — which made the excerpt branch unreachable: every
    # article with no summary and >=200 chars of content showed the placeholder.
    is_excerpt = False
    if not summary:
        content = art.get("content", "") or ""
        if len(content) < MIN_CONTENT_CHARS:
            summary = "Summary unavailable — article may be paywalled."
        else:
            summary = content[:200].strip() + "..."
            is_excerpt = True
    # Detect truncated summary: MiniMax sometimes returns the first ~200 chars of
    # content instead of generating a proper 3-5 sentence summary. This happens when
    # (a) content is sparse/ambiguous and LLM has nothing to synthesize, or
    # (b) max_tokens is too low. If summary starts the same way as content (first
    # 150 chars match), treat it as a failed summary and replace with placeholder.
    if summary and not is_excerpt and _is_content_copy(summary, art.get("content", "") or ""):
        # Summary is essentially a verbatim copy of the content — LLM failed to synthesize
        summary = "Summary unavailable — preview limited. Click to read full article."
    return _cap_summary(_defang_untrusted(summary), budget)

# ── Discord send ───────────────────────────────────────────────────────────────

def _retry_after_seconds(resp) -> float | None:
    """Discord's requested rate-limit wait, from the JSON body 'retry_after'
    (seconds) or the Retry-After header. Capped so a bad value can't stall cron.
    Returns None if unavailable."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("retry_after") is not None:
            return min(float(body["retry_after"]), RETRY_AFTER_CAP)
    except (ValueError, TypeError):
        pass
    hdr = resp.headers.get("Retry-After")
    if hdr:
        try:
            return min(float(hdr), RETRY_AFTER_CAP)
        except ValueError:
            return None
    return None

def _bounded_wait(seconds: float) -> float:
    """Clip a retry sleep to the remaining send budget (never negative).

    A NaN `retry_after` used to propagate through min()/max() and collapse the wait to
    0.0, turning an honoured backoff into an instant re-hammer; treat it as the default.
    """
    if seconds != seconds:                      # NaN
        seconds = POST_BACKOFF_SECONDS
    return max(0.0, min(seconds, _SEND_DEADLINE[0] - time.monotonic()))

def post_message(url: str, headers: dict, payload: dict, fatal: bool = True) -> str:
    """POST to Discord API. Returns message ID on success.

    Retries TRANSIENT failures — HTTP 429 (honoring Retry-After), 5xx, and network
    errors — up to POST_RETRIES times with backoff, so a routine rate-limit doesn't
    abort the brief mid-send. A non-transient 4xx (or exhausted retries) is fatal
    and exits WITHOUT recording seen articles, so the brief is re-sent next run
    rather than silently marked delivered. Content is truncated to Discord's
    2000-char limit as a last-resort guard against a 50035 rejection.

    fatal=False makes a failed send best-effort (log to stderr, return None instead of
    sys.exit) — used for the optional trailing feed-health notice, whose failure must
    NOT abort before save_seen_articles (which would re-send the already-delivered brief).
    """
    def _giveup(msg: str):
        print(msg, file=sys.stderr)
        if fatal:
            sys.exit(1)
        return None

    content = payload.get("content", "")
    if isinstance(content, str) and _dlen(content) > DISCORD_CONTENT_LIMIT:
        out = content
        while out and _dlen(out) > DISCORD_CONTENT_LIMIT - 1:
            out = out[: max(1, int(len(out) * (DISCORD_CONTENT_LIMIT - 1) / max(1, _dlen(out))))]
        payload = {**payload, "content": out.rstrip() + "…"}

    last_err = "unknown error"
    for attempt in range(POST_RETRIES + 1):
        # Stop retrying once the send budget is gone: a SIGKILL mid-burst leaves the
        # brief delivered and the ledger unwritten (duplicate brief next run), so it is
        # strictly better to give up on THIS message while the process still lives to
        # write the ledger.
        if time.monotonic() > _SEND_DEADLINE[0]:
            return _giveup(f"[ERROR] send budget exhausted after {attempt} attempt(s): "
                           f"{last_err}")
        try:
            remaining = _SEND_DEADLINE[0] - time.monotonic()
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=(10, max(1.0, min(15.0, remaining))))
        except requests.exceptions.RequestException as e:
            last_err = f"network error: {e}"
            if attempt < POST_RETRIES:
                time.sleep(_bounded_wait(POST_BACKOFF_SECONDS * (attempt + 1)))
                continue
            return _giveup(f"[ERROR] Sending to Discord failed after {POST_RETRIES + 1} attempts: {last_err}")

        if resp.status_code in TRANSIENT_DISCORD_HTTP:
            last_err = f"HTTP {resp.status_code}"
            if attempt < POST_RETRIES:
                wait = _retry_after_seconds(resp)
                time.sleep(_bounded_wait(wait if wait is not None
                                         else POST_BACKOFF_SECONDS * (attempt + 1)))
                continue
            return _giveup(f"[ERROR] Discord {last_err} after {POST_RETRIES + 1} attempts: {resp.text}")

        if resp.status_code >= 400:
            return _giveup(f"[ERROR] Discord HTTP {resp.status_code}: {resp.text}")
        if not (200 <= resp.status_code < 300):
            # A 1xx/3xx is not a delivery. Treating "anything under 400" as success would
            # record the articles as seen although Discord never received the message.
            return _giveup(f"[ERROR] Discord returned an unexpected "
                           f"{resp.status_code}; not treating as delivered")

        # Parse INSIDE the try: a 2xx with a non-JSON body (a Cloudflare interstitial, a
        # 204) raises requests' JSONDecodeError, which is a RequestException — it was
        # simply being raised outside the only handler, so it escaped post_message
        # entirely, after the message had already been delivered.
        try:
            data = resp.json()
        except ValueError:
            # Delivered, but we can't read the id. Treat as success: the alternative is
            # aborting the brief (and the ledger write) over an unreadable receipt.
            print(f"[WARN] Discord returned {resp.status_code} with an unparseable body; "
                  f"treating the send as delivered.", file=sys.stderr)
            return "unknown"
        if isinstance(data, dict) and data.get("code") not in (0, None):
            code = data["code"]
            msg  = data.get("message", "(no message)")
            return _giveup(f"[ERROR] Discord API error {code}: {msg}")

        return data.get("id", "unknown") if isinstance(data, dict) else "unknown"

# ── Message assembly (shared by send AND dry-run) ──────────────────────────────
# One builder, two consumers. Previously send_messages assembled the real messages
# while dry_run printed one contiguous stream — so --dry-run modelled neither the
# chunking nor the 2000-char limit, and a brief the live path would silently truncate
# looked perfectly healthy in dry run. Since --dry-run is the only way to validate this
# script without posting, that divergence made the safe check the useless one.

SAFETY_MARGIN = 16      # slack for the ellipsis/rounding when fitting to the limit

def _render_block(art: dict, fixed_overhead: int) -> str:
    """Render one article as its own message body, budgeted by its ACTUAL headline.

    The summary cap is derived from the headline length rather than a fixed constant:
    today's longest real article has a 217-char title and a 280-char URL, so its
    headline alone is 507 chars and a summary at the flat 1500-char cap produced a
    2010-char message — over the limit, on the must-read path, which has no split
    branch. Deriving it keeps a single-article message inside the limit by construction
    while leaving the full MAX_SUMMARY_CHARS available whenever the headline is normal.
    """
    headline = format_headline(art.get("title", ""), art.get("url", ""))
    budget = (DISCORD_CONTENT_LIMIT - fixed_overhead - SAFETY_MARGIN
              - _dlen(headline) - len("• ") - len("\n  "))
    return f"• {headline}\n  {get_summary(art, budget)}"

def build_messages(must_read: list, maybe_rel: list, total_scanned: int,
                   skipped: int, matched_and_dropped: int) -> list:
    """Return the exact messages the brief will send, as (label, content, articles).

    `articles` is what that message displays — used to record only the articles that
    actually reached Discord if a send is cut short (see send_messages).
    """
    messages = []
    date_str = format_date(datetime.now())

    # 1. Header
    messages.append(("header", f"## \U0001f916 Morning brief  |  {date_str}", []))

    # 2. Must-read — one message per article
    if must_read:
        for idx, art in enumerate(must_read):
            header   = "### Must-read news\n" if idx == 0 else ""
            # Add SECTION_SEP after the last must-read so there's a braille-blank
            # row between the last article and "### Maybe relevant".
            trailing = SECTION_SEP if idx == len(must_read) - 1 else ""
            block    = _render_block(art, len(header) + len(trailing))
            messages.append((f"must-read {idx + 1}/{len(must_read)}",
                             f"{header}{block}{trailing}", [art]))
    else:
        messages.append(("must-read (none)",
                         f"### Must-read news\n*No must-read stories today.*{SECTION_SEP}",
                         []))

    # 3. Maybe-relevant — one message per article, like must-read, so a reaction on a
    #    message rates exactly one article (feedback.py). It used to be chunked 2 per
    #    message; a 👍 on a chunk couldn't say which of the two it meant.
    if maybe_rel:
        for idx, art in enumerate(maybe_rel):
            # Section header appears exactly once, on the first message only (Pitfall 9).
            head     = "### Maybe relevant\n" if idx == 0 else ""
            trailing = SECTION_SEP if idx == len(maybe_rel) - 1 else ""
            block    = _render_block(art, len(head) + len(trailing))
            messages.append((f"maybe {idx + 1}/{len(maybe_rel)}",
                             f"{head}{block}{trailing}", [art]))
    else:
        messages.append(("maybe (none)",
                         f"### Maybe relevant\n*Nothing to show today.*{SECTION_SEP}",
                         []))

    # 4. Footer
    messages.append(("footer",
                     f"__​{total_scanned} articles scanned · "
                     f"{len(must_read)} must-read · "
                     f"{len(maybe_rel)} maybe · "
                     f"{matched_and_dropped} matched & dropped · "
                     f"{skipped} skipped · rate with 👍 👎__", []))
    return messages

def send_messages(must_read: list, maybe_rel: list, total_scanned: int, skipped: int,
                  matched_and_dropped: int, dropped_at_send: list = None,
                  held_retellings: list = None) -> None:
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not found.", file=sys.stderr)
        sys.exit(1)

    # Arm the send budget before the first POST (see SEND_BUDGET_SECONDS).
    _SEND_DEADLINE[0] = time.monotonic() + SEND_BUDGET_SECONDS

    url     = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type":  "application/json",
        "User-Agent":    "DiscordBot (hermes-agent/1.0)",
    }
    # embeds:[] + flags:4 (SUPPRESS_EMBEDS) is the actual Discord mechanism to stop
    # link unfurling on our own message. The old unfurl_media/unfurl_links/
    # disable_web_page_preview keys are Slack/Telegram params Discord ignores (audit
    # D2). The load-bearing suppressor is still the <url> angle brackets in
    # format_headline — flags:4 is the supported belt-and-suspenders.
    # allowed_mentions:{"parse":[]} blocks ALL mention parsing server-side, so a
    # crafted feed title / LLM summary containing @everyone/@here/<@&role> can never
    # ping this channel (complements the metachar escaping in format_headline).
    base_payload = {
        "embeds": [],
        "flags": 4,
        "allowed_mentions": {"parse": []},
    }

    # Discord nonce, the SAME across every retry of one message and unique per run:
    # post_message retries a read timeout / 5xx, but a POST Discord already accepted
    # would be created twice. With enforce_nonce, Discord returns the existing message
    # for a repeated nonce instead of posting a duplicate. (≤ 25 chars.)
    run_tag = "mb%d" % (int(time.time()) % 10**9)       # per run, so a manual re-run posts
    delivered, failed_at, n_sent = [], None, 0
    rated_msgs = []                     # (message id, section, article) for feedback.py
    for idx, (label, content, carried) in enumerate(
            build_messages(must_read, maybe_rel, total_scanned, skipped,
                           matched_and_dropped)):
        # fatal=False so a failure cannot skip the ledger write below; we stop sending
        # and record what did land.
        msg_id = post_message(url, headers,
                              {**base_payload, "content": content,
                               "nonce": f"{run_tag}{idx:02d}", "enforce_nonce": True},
                              fatal=False)
        if msg_id is None:
            failed_at = label
            print(f"  ✗ {label} FAILED — stopping; recording only what was delivered",
                  file=sys.stderr)
            break
        delivered.extend(carried)
        rated_msgs += [(msg_id, "must_read" if label.startswith("must-read") else "maybe_relevant", a)
                       for a in carried]
        n_sent += 1
        print(f"  ✓ {label} sent ({_dlen(content)} units). ID: {msg_id}")

    # Feed-health notice (only when one or more feeds are persistently down or have
    # stopped yielding usable articles). Best-effort (fatal=False): this trailing,
    # non-essential notice must never abort before save_seen_articles below — a failure
    # here would otherwise re-send the entire already-delivered brief next run.
    health_line = dead_feeds_line()
    if health_line:
        msg_id = post_message(url, headers,
                              {**base_payload, "content": f"{SECTION_SEP}\n{health_line}",
                               "nonce": f"{run_tag}hn", "enforce_nonce": True},
                              fatal=False)
        print(f"  ✓ Feed-health notice sent. ID: {msg_id}" if msg_id
              else "  ⚠ Feed-health notice failed to send (non-fatal; brief already delivered)")

    # Record the articles that were actually delivered. A twin is a duplicate of something
    # we showed, so it is recorded exactly when EVERY article-carrying message landed — not
    # when the footer did. Keying off `failed_at` alone discarded all twins on a footer-only
    # failure, even though every article had been delivered and recorded, so a duplicate of
    # an already-shown story went out the next day.
    all_articles_delivered = len(delivered) == len(must_read) + len(maybe_rel)
    twins = list(dropped_at_send or []) if all_articles_delivered else []
    # The other members of each DELIVERED article's fetch-side cluster (fetch_rss
    # deduplicate `_twins`): the same story, never shown. Recording them lets the hold-back
    # catch a chained member tomorrow that doesn't match the shown copy itself.
    twins += [t for a in delivered for t in _dict_items(a, "_twins")]
    # Re-tellings fetch_rss held back today (they carry `_strong` → MAX_AGE_DAYS): recorded
    # once anything was delivered, so each stays held for its OWN eligible life.
    if delivered:
        twins += [t for t in (held_retellings or []) if isinstance(t, dict)]
    # Chained members of each delivered article's cluster (fetch_rss `_chained`): never
    # shown and not a match for the shown copy, so the hold-back can't be relied on to
    # catch them tomorrow — they're recorded by identity (see CHAINED_TTL_DAYS).
    chained = [c for a in delivered for c in _dict_items(a, "_chained")]
    recorded = save_seen_articles(delivered, [], twins, chained)
    print(f"✓ Recorded {recorded} articles to seen list "
          f"({len(delivered)} delivered, {len(twins)} twins, {len(chained)} chained)")
    # Reader-ratings manifest: AFTER the ledger (which is what prevents a duplicate brief)
    # and best-effort — a failure here only means these messages can't be rated.
    try:
        n_rec = feedback.record_brief(rated_msgs, CHANNEL_ID) if feedback else 0
        print(f"✓ Recorded {n_rec} article message(s) for reaction ratings")
    except Exception as e:                      # noqa: BLE001
        print(f"[WARN] ratings manifest not written ({type(e).__name__}: {e}); "
              f"today's brief can't be rated by reaction", file=sys.stderr)
    # On an empty day nothing carries an article, so "all delivered" is vacuous: only a
    # lost footer is harmless then — a lost header / "no stories" placeholder is not.
    empty_day = not must_read and not maybe_rel
    if failed_at is not None:
        if all_articles_delivered and n_sent > 0 and (not empty_day or failed_at == "footer"):
            # Every article landed; only the footer/health line was lost. Report it, but
            # don't exit non-zero — that would fire a "morning brief failed" alert for a
            # brief the reader received in full.
            print(f"[WARN] brief delivered, but {failed_at} did not send.",
                  file=sys.stderr)
        else:
            # Also reached when NOTHING was delivered on an empty day (0 articles, so
            # "all articles delivered" was vacuously true and a 403 on the header exited 0).
            print(f"[ERROR] brief incomplete — failed at {failed_at}; the undelivered "
                  f"articles remain unrecorded and will be re-sent next run.",
                  file=sys.stderr)
            sys.exit(1)

def dry_run(must_read: list, maybe_rel: list, total_scanned: int, skipped: int,
            matched_and_dropped: int, dropped_at_send: list = None,
            held_retellings: list = None) -> None:
    """Print the EXACT messages send_messages would post, with their sizes.

    Uses the same build_messages() the live path uses, so a dry run now surfaces the
    chunk boundaries, the section separators, and any message that would be truncated.
    """
    print("=== DRY RUN (messages as they would be posted) ===\n")
    messages = build_messages(must_read, maybe_rel, total_scanned, skipped,
                              matched_and_dropped)
    over = 0
    for i, (label, content, _carried) in enumerate(messages, 1):
        flag = ""
        if _dlen(content) > DISCORD_CONTENT_LIMIT:
            flag = (f"  !! OVER LIMIT by {_dlen(content) - DISCORD_CONTENT_LIMIT}"
                    f" — would be truncated")
            over += 1
        print(f"--- message {i}/{len(messages)}: {label} ({_dlen(content)} units){flag}")
        print(content)
        print()
    health_line = dead_feeds_line()
    n_messages = len(messages)
    if health_line:
        # Render the EXACT content send_messages posts, separator included — measuring
        # the bare line under-reported it by 3 units and left it out of the total.
        notice = f"{SECTION_SEP}\n{health_line}"
        note = ""
        if _dlen(notice) > DISCORD_CONTENT_LIMIT:
            note = "  !! OVER LIMIT — post_message would truncate"
            over += 1
        n_messages += 1
        print(f"--- message {n_messages}/{n_messages}: feed-health notice "
              f"({_dlen(notice)} units, best-effort){note}")
        print(notice)
        print()
    print(f"would post {n_messages} message(s)"
          + (f" — {over} OVER the {DISCORD_CONTENT_LIMIT}-char limit" if over else "")
          + f"; would record {len(must_read) + len(maybe_rel)} shown + "
            f"{len(dropped_at_send or [])} deduped-at-send + "
            f"{sum(len(_dict_items(a, '_twins')) for a in list(must_read) + list(maybe_rel))}"
            f" cluster-twin + {len(held_retellings or [])} held re-telling + "
            f"{sum(len(_dict_items(a, '_chained')) for a in list(must_read) + list(maybe_rel))}"
            f" chained article(s) as seen")
    print(f"channel: {CHANNEL_ID}  token loaded: {bool(DISCORD_BOT_TOKEN)}")
    print("=== END DRY RUN ===")

# ── Seen article tracking ─────────────────────────────────────────────────────

def _entry_ts(entry: dict) -> float:
    """A ledger entry's timestamp as a float, or 0.0 if it is missing/garbage.

    The READER already coerced defensively; the WRITER did not, and compared `ts`
    straight against a float during the prune. A hand-edited or partially-written
    ledger carrying `ts: null` / `"2026-09-14"` / a dict therefore raised TypeError
    *after* every Discord message had been delivered — so the ledger never got written
    and the entire brief was re-sent the next run.
    """
    try:
        ts = float(entry.get("ts", 0))
    except (TypeError, ValueError):
        return 0.0
    if ts != ts or ts in (float("inf"), float("-inf")):     # NaN / inf
        return 0.0
    # Clamp a future stamp to now (a clock that was skewed forward): such a run would otherwise
    # write entries that never expire and never prune — permanent suppression plus unbounded
    # growth. MIN_LEDGER_ENTRIES guards only the opposite direction.
    return min(ts, datetime.now(timezone.utc).timestamp())

def _dict_items(art, key: str) -> list:
    """The dicts in `art[key]` (`_twins` / `_chained`) — anything else is ignored. These are
    read AFTER delivery and before the ledger write, so a malformed value (a hand-edited
    articles.json) must not raise there: that would re-send the whole brief next run."""
    v = art.get(key) if isinstance(art, dict) else None
    return [t for t in v if isinstance(t, dict)] if isinstance(v, list) else []

def _entry_ttl(entry: dict) -> float:
    """A ledger entry's retention window in days, defaulting to MAX_AGE_DAYS.

    Coerced exactly the way `fetch_rss.load_seen_hashes` coerces it — the reader was
    already defensive here and the writer was not, so a malformed `ttl_days` raised
    inside the prune (i.e. after delivery) and cost the whole brief a re-send. The two
    sides MUST agree about what is expired.
    """
    try:
        ttl = float(entry.get("ttl_days", MAX_AGE_DAYS))
    except (TypeError, ValueError):
        return float(MAX_AGE_DAYS)
    if ttl != ttl or ttl < 0:                              # NaN / negative
        return float(MAX_AGE_DAYS)
    # Pre-1.10 twin (no `story`): full window — same rule as fetch_rss.load_seen_hashes.
    # A chained entry (1.13) has no `story` by design and keeps its own ttl.
    if "ttl_days" in entry and "story" not in entry and entry.get("kind") != "chained":
        return float(MAX_AGE_DAYS)
    return min(ttl, float(MAX_AGE_DAYS))

def _prune_ledger(entries: list, now_ts: float) -> list:
    """Drop entries past their retention window, keeping a most-recent floor.

    Per-entry `ttl_days` (used for deduped-at-send twins) overrides MAX_AGE_DAYS.
    MIN_LEDGER_ENTRIES is a clock-skew guard: one run with a forward-skewed clock computes a cutoff newer than every real entry, empties the
    ledger, and makes the next correct run re-send a week of stories.
    """
    kept = [e for e in entries
            if _entry_ts(e) >= now_ts - _entry_ttl(e) * 86400]
    if len(kept) < MIN_LEDGER_ENTRIES:
        by_recency = sorted(entries, key=_entry_ts, reverse=True)[:MIN_LEDGER_ENTRIES]
        kept_ids = {id(e) for e in kept}
        kept += [e for e in by_recency if id(e) not in kept_ids]
    return kept

def _own_slots(a: dict) -> dict:
    """{"slots": …} judged with the article's own body — carried from fetch_rss for
    twins / held re-tellings (they have no content here), computed for shown articles.
    Omitted when neither is available: flags judged WITHOUT a body would never mark a
    Title Case name, and the reader then can't fall back to the incoming body (lap-9
    gate: a twin's body-less flags held the Sonnet-after-Opus sequel)."""
    carried = a.get("_slots")
    if isinstance(carried, list):
        return {"slots": carried}
    body = a.get("content")
    if isinstance(body, str) and body.strip():
        return {"slots": [list(t) for t in _slots(a.get("title", ""), a.get("source"), body)]}
    return {}

def save_seen_articles(must_read: list, maybe_rel: list,
                       dropped_at_send: list = None, chained: list = None) -> int:
    """Record delivered articles in the seen ledger. Returns the number added.

    `dropped_at_send` is the articles the send-time dedup removed because they duplicate
    something in this brief. They MUST be recorded too: they were never shown, so they
    were never marked seen, while the twin that *was* shown is — so next run the dedup
    pair can't re-form and the duplicate is promoted and delivered as if it were a new
    story. Observed live: MacRumors' "Here's When iOS 27 Rolls Out in Every Time Zone"
    was dropped as a duplicate of "iOS 27 Available Tomorrow…", and only the latter
    reached the ledger.

    Every identity hash is stored (url / canonical title / legacy title|source) so the
    reader can match any of them — see article_id.py. Retention is per entry:
    MAX_AGE_DAYS for a shown story and for a fetch-side cluster twin (a dict carrying
    `_strong`), DEDUPED_TWIN_TTL_DAYS for a twin dropped by the send-time backstop.

    `chained` (1.13) is the shown stories' chained cluster members: recorded LAST, by
    identity only (`"kind": "chained"`, CHAINED_TTL_DAYS, no story words), so an article
    that is also a twin keeps its full twin entry.
    """
    os.makedirs(BASE_DIR, exist_ok=True)
    existing = []
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                existing = [e for e in loaded if isinstance(e, dict)]
        except (ValueError, OSError):   # incl. UnicodeDecodeError
            existing = []

    now_ts = datetime.now(timezone.utc).timestamp()
    # `hash` stays the LEGACY title|source id so that reverting this change set leaves
    # a ledger the previous reader can still match (otherwise a rollback blinds the
    # 7-day dedup window and ships a week of duplicates). `hashes` carries the full
    # identity list that the current reader matches against. `ttl_days` lets a
    # deduped-at-send twin expire sooner than a story we actually showed.
    new_entries = [
        {"hash": legacy_hash(a), "hashes": article_hashes(a), "ts": now_ts,
         "title": a.get("title", ""), "source": a.get("source", ""),
         # url + story words let fetch_rss's hold-back run same_story against shown entries
         "url": a.get("url", "") if isinstance(a.get("url", ""), str) else "",
         "story": sorted(story_tokens(a))[:80],
         # the first 1,000 chars' words, for the deep same-story path (article_id
         # DEEP_CONTENT_CHARS); the window bounds the count, the cap only a malformed entry
         "story_deep": sorted(deep_tokens(a))[:300],
         # sequel-identifier flags of the title, judged with the article's OWN body, so the
         # hold-back compares like with like (article_id._slots; see fetch_rss._hold_grade)
         **_own_slots(a),
         **({} if ttl is None else {"ttl_days": ttl})}
        for a, ttl in ([(a, None) for a in list(must_read) + list(maybe_rel)]
                       # a cluster twin (has `_strong`) matched the shown story directly:
                       # full window, or it resurfaces on day 4 while still eligible
                       + [(a, MAX_AGE_DAYS if "_strong" in a else DEDUPED_TWIN_TTL_DAYS)
                          for a in (dropped_at_send or [])])
    ] + [
        {"hash": legacy_hash(a), "hashes": article_hashes(a), "ts": now_ts,
         "title": a.get("title", ""), "source": a.get("source", ""),
         "url": a.get("url", "") if isinstance(a.get("url", ""), str) else "",
         "kind": "chained", "ttl_days": CHAINED_TTL_DAYS}
        for a in (chained or []) if isinstance(a, dict)
    ]
    seen = set()
    for e in existing:
        # Only entries still INSIDE their retention window suppress a re-record — the
        # same test load_seen_hashes() applies. Using every stored entry meant an entry
        # kept alive only by the MIN_LEDGER_ENTRIES floor blocked the refresh of an
        # article the reader had already expired, so that article was re-delivered every
        # single run and never re-stamped.
        if _entry_ts(e) < now_ts - _entry_ttl(e) * 86400:
            continue
        if isinstance(e.get("hashes"), list):
            seen.update(h for h in e["hashes"] if isinstance(h, str))
        if isinstance(e.get("hash"), str):
            seen.add(e["hash"])
    added = 0
    for entry in new_entries:
        if seen.isdisjoint(entry["hashes"]):
            existing.append(entry)
            seen.update(entry["hashes"])
            added += 1

    existing = _prune_ledger(existing, now_ts)
    # Normalise before serializing: json.load ACCEPTS bare NaN/Infinity, and the coercers
    # above only sanitize for comparison — they never rewrite the entry. With
    # allow_nan=False the dump would then raise, after every message had been delivered.
    for e in existing:
        e["ts"] = _entry_ts(e)
        if "ttl_days" in e:
            e["ttl_days"] = _entry_ttl(e)

    # Atomic write: serialize to a temp file in the same dir, fsync, then os.replace
    # (atomic on POSIX). A SIGKILL from the cron watchdog mid-write can otherwise
    # truncate seen_articles.json; load_seen_hashes then reads it as empty and every
    # article from the last 7 days becomes re-eligible → a run of duplicate briefs.
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SEEN_FILE)
    return added

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run_mode = len(sys.argv) > 1 and sys.argv[1] == "--dry-run"

    try:
        with open(INPUT_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] summarized articles not found: {INPUT_FILE} "
              f"(did summarize.py run?)", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[ERROR] {INPUT_FILE} is not valid JSON ({e}) — a partial/corrupt "
              f"write; not sending.", file=sys.stderr)
        sys.exit(1)

    must_read, maybe_rel     = data.get("must_read", []), data.get("maybe_relevant", [])
    total_scanned, skipped   = data.get("total_scanned", 0), data.get("skipped", 0)
    matched_and_dropped      = data.get("matched_and_dropped", 0)

    # Keep the pre-dedup articles themselves (not just a count): the ones dropped below
    # are duplicates of what we DO deliver, so they must be written to the seen ledger
    # or they resurface as "new" on a later run (see save_seen_articles).
    displayed_before = list(must_read) + list(maybe_rel)

    # Backstop dedup, same predicate as fetch_rss's clustering (article_id.same_story).
    # Articles that fetch_rss clustered can't pair here (each survivor already failed
    # same_story against the others), so a [DEDUP] line means articles.json came from
    # somewhere else — an older fetch_rss or a hand edit.
    must_read, maybe_rel = dedup_mr_vs_may(must_read, maybe_rel, threshold=0.40)

    must_read = dedup_within_mr(must_read, threshold=0.40)

    maybe_rel = dedup_within_may(maybe_rel, threshold=0.40)

    # Articles dropped at send time are no longer displayed but were still matched —
    # fold them into "matched & dropped" so the footer math balances (audit B2).
    kept = {id(a) for a in must_read} | {id(a) for a in maybe_rel}
    dropped_at_send = [a for a in displayed_before if id(a) not in kept]
    matched_and_dropped += len(dropped_at_send)

    held_retellings = data.get("held_retellings")
    if not isinstance(held_retellings, list):
        held_retellings = []
    if dry_run_mode:
        dry_run(must_read, maybe_rel, total_scanned, skipped, matched_and_dropped,
                dropped_at_send, held_retellings)
    else:
        send_messages(must_read, maybe_rel, total_scanned, skipped, matched_and_dropped,
                      dropped_at_send, held_retellings)

if __name__ == "__main__":
    main()
