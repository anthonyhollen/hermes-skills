#!/usr/bin/env python3
"""feed_doctor.py — diagnose dead feeds and propose VERIFIED replacements.

Part of the morning-brief feed self-healing loop:
  - fetch_rss.py records each feed's outcome in feed_health.json (consecutive-
    failure counter; a feed is "dead" after DEAD_AFTER_FAILURES failed runs, and
    "zero-yield" after ZERO_YIELD_AFTER_RUNS runs with no usable article).
  - send_brief.py surfaces dead AND zero-yield feeds in the daily brief; the default
    mode here diagnoses both (it used to pick dead ones only, so the brief's
    "N yielding nothing usable — run feed_doctor" led to "All feeds healthy").
  - THIS script finds replacements with minimal effort: for each dead feed it
    generates candidate URLs — same-domain alternate RSS paths PLUS LLM-suggested
    sources for the feed's category/interests — then LIVE-PROBES every candidate
    and reports only the ones that actually return articles. LLM hallucinations are
    filtered out by the probe, so every suggestion is real. You just pick which to
    apply (tell Claude, or edit feeds.py).

Usage:
  python feed_doctor.py                       # diagnose dead feeds, print report
  python feed_doctor.py --post                # also post the report to Discord
  python feed_doctor.py --all                 # check EVERY feed (full audit), not just dead
  python feed_doctor.py --name "DPReview"     # force-check one current feed by name
  python feed_doctor.py --url URL --category cameras --label DPReview
                                              # find replacements for an arbitrary (dead) URL
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedparser
import requests

from article_id import MIN_CONTENT_CHARS
from bounded_http import read_bounded, request_bounded
# Probe EXACTLY the way the pipeline fetches: same extraction (longest content element,
# entity decoding), same usable test, same size cap and encodings. The old probe used
# content[0] and averaged the first 5 entries, so a Tom's-Hardware-shaped feed (author
# bio first, article second) read "avg 17, teaser" while fetch_rss counted 5/5 usable.
from fetch_rss import (DEAD_AFTER_FAILURES, MAX_FEED_BYTES, ZERO_YIELD_AFTER_RUNS,
                       _as_int, _extract_articles)

BASE_DIR            = os.path.expanduser("~/.hermes/morning-brief")
INTERESTS_FILE      = os.path.join(BASE_DIR, "interests.json")
FEED_HEALTH_FILE    = os.path.join(BASE_DIR, "feed_health.json")
MIN_FULL_CONTENT    = MIN_CONTENT_CHARS   # below this the paywall guard zeroes an entry
PROBE_BUDGET_SECONDS = 25                 # per candidate, whole request incl. body
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
# Common RSS/Atom endpoint paths to try on a dead feed's own domain.
ALT_PATHS = ["/feed/", "/feed", "/rss", "/rss/", "/rss.xml", "/feed.xml",
             "/index.xml", "/atom.xml", "/feeds/all", "/rss/news", "/?feed=rss2"]


# ── Probing ────────────────────────────────────────────────────────────────────

def probe(url: str) -> dict:
    """Live-fetch a candidate URL. Returns {ok, status, entries, usable, avg}.

    `usable` = entries passing the paywall guard, counted like fetch_rss's feed health;
    `avg` = mean extracted length of the first 5 entries (display only).
    """
    fail = {"ok": False, "entries": 0, "usable": 0, "avg": 0}
    try:
        deadline = time.monotonic() + PROBE_BUDGET_SECONDS
        r = request_bounded("GET", deadline, url=url, stream=True, timeout=(10, 15),
                            headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        if r.status_code != 200:
            r.close()
            return {**fail, "status": f"HTTP {r.status_code}"}
        body = read_bounded(r, deadline, MAX_FEED_BYTES).content
        f = feedparser.parse(body)   # bytes, never .text — see SKILL.md invariants
        arts, usable = _extract_articles(f, "probe", "C")
        if not arts:
            return {**fail, "status": "0 entries"}
        lens = [len(a["content"]) for a in arts[:5]]
        return {"ok": True, "status": "ok", "entries": len(arts), "usable": usable,
                "avg": sum(lens) // len(lens)}
    except Exception as e:
        return {**fail, "status": f"ERR {type(e).__name__}"}


# ── Candidate generation ─────────────────────────────────────────────────────────

def alt_path_candidates(old_url: str) -> list:
    """Alternate RSS endpoint URLs on the same domain as the dead feed."""
    try:
        p = urlparse(old_url)
        if not p.scheme or not p.netloc:
            return []
        base = f"{p.scheme}://{p.netloc}"
    except Exception:
        return []
    old = old_url.rstrip("/")
    return [base + path for path in ALT_PATHS if (base + path).rstrip("/") != old]

def llm_candidates(name: str, old_url: str, category: str, topics: list) -> list:
    """Ask the Hermes-configured model for replacement feed URLs (corrected same-
    publisher URL + reputable alternatives in-category). Returns a list of URL
    strings; every one is probed before being trusted, so bad guesses are harmless."""
    try:
        from summarize import resolve_hermes_runtime, call_model
        runtime = resolve_hermes_runtime()
    except Exception as e:
        print(f"  (LLM unavailable, skipping suggestions: {e})", file=sys.stderr)
        return []
    system = (
        "You are an RSS feed expert. Given a dead news feed, propose replacement "
        "RSS/Atom feed URLs. Return ONLY a JSON array of candidate feed-URL strings — "
        "no prose, no markdown. Include (1) the corrected current RSS URL for the SAME "
        "publisher if you know it, and (2) 2-4 reputable ALTERNATIVE sources in the same "
        "category that fit the reader's interests. Give direct feed endpoints "
        "(e.g. ending in /feed, /rss.xml, /index.xml), never homepages."
    )
    user = (
        f"Dead feed: {name}\nOld URL: {old_url}\nCategory: {category}\n"
        f"Reader interests: {', '.join(topics)}\n\n"
        "Return a JSON array of candidate RSS feed URLs."
    )
    try:
        txt = call_model(runtime, system, user, 600)
    except Exception as e:
        print(f"  (LLM call failed: {e})", file=sys.stderr)
        return []
    m = re.search(r"\[.*\]", txt, re.DOTALL)
    urls = []
    if m:
        try:
            urls = [u for u in json.loads(m.group(0)) if isinstance(u, str)]
        except Exception:
            pass
    if not urls:                       # fall back to scraping any URLs out of the text
        urls = re.findall(r"https?://[^\s\"'<>)\]}]+", txt)
    return urls


def diagnose(targets: list, topics: list) -> list:
    """targets: [(name, old_url, category)]. Returns report rows with working cands."""
    report = []
    for name, old_url, category in targets:
        candidates, seen = [], set()
        for u in alt_path_candidates(old_url) + llm_candidates(name, old_url, category, topics):
            u = (u or "").strip()
            if u and u not in seen:
                seen.add(u)
                candidates.append(u)
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda u: (u, probe(u)), candidates))
        working = sorted([(u, r) for u, r in results if r["ok"]],
                         key=lambda x: (-x[1]["usable"], -x[1]["avg"]))
        report.append({"name": name, "old_url": old_url, "category": category,
                       "n_candidates": len(candidates), "working": working})
    return report


# ── Reporting ────────────────────────────────────────────────────────────────────

def format_report(report: list) -> str:
    lines = ["🩺 **Feed Doctor** — replacement candidates (all live-probed):", ""]
    for row in report:
        lines.append(f"❌ **{row['name']}** ({row['category']}) — was `{row['old_url']}`")
        if not row["working"]:
            lines.append(f"   no working replacement found ({row['n_candidates']} probed). "
                         f"May need a manual source.")
        else:
            for u, r in row["working"]:
                tag = "FULL" if r["usable"] else f"teaser<{MIN_FULL_CONTENT}⚠"
                lines.append(f"   ✅ {u}  ({r['entries']} entries, {r['usable']} usable, "
                             f"avg {r['avg']} chars, {tag})")
            if not any(r["usable"] for _, r in row["working"]):
                lines.append("   ⚠ all candidates are teaser-only (<200 chars) — "
                             "would be zeroed by the paywall guard.")
        lines.append("")
    lines.append("_Tell Claude which to apply (it'll swap them into feeds.py and validate)._")
    return "\n".join(lines)

def post_to_discord(text: str) -> None:
    try:
        from send_brief import DISCORD_BOT_TOKEN, CHANNEL_ID, post_message
    except Exception as e:
        print(f"[ERROR] cannot import Discord poster: {e}", file=sys.stderr)
        return
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN not set; cannot post.", file=sys.stderr)
        return
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json",
               "User-Agent": "DiscordBot (hermes-agent/1.0)"}
    # Discord 2000-char limit — chunk on blank lines if needed.
    for chunk in _chunk(text, 1900):
        post_message(url, headers, {"embeds": [], "flags": 4,
                                    "allowed_mentions": {"parse": []}, "content": chunk})
    print("✓ Posted feed-doctor report to Discord.")

def _chunk(text: str, limit: int) -> list:
    """Split text into <=limit-char chunks on line boundaries.

    Never emits an empty chunk (Discord rejects empty content with code 50006),
    and hard-splits any single line longer than the limit so no chunk exceeds it
    (a line that alone exceeds the limit would otherwise be posted un-split and
    rejected with HTTP 400)."""
    out, cur = [], ""
    for line in text.split("\n"):
        # Hard-split a single line that alone exceeds the limit.
        while len(line) > limit:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


# ── Target selection ─────────────────────────────────────────────────────────────

def load_topics() -> list:
    try:
        with open(INTERESTS_FILE) as f:
            return json.load(f).get("topics", [])
    except Exception:
        return []

def load_health() -> dict:
    """Same guards as the pipeline's readers: a truncated file (UnicodeDecodeError), a
    non-dict file, non-dict records and string counters must not crash the tool."""
    if not os.path.exists(FEED_HEALTH_FILE):
        return {}
    try:
        with open(FEED_HEALTH_FILE) as f:
            health = json.load(f)
    except (ValueError, OSError):
        return {}
    if not isinstance(health, dict):
        return {}
    return {k: v for k, v in health.items() if isinstance(v, dict)}

def feeds_by_name() -> dict:
    from feeds import FEEDS
    return {name: (name, url, category, tier) for name, url, category, tier in FEEDS}


def main():
    ap = argparse.ArgumentParser(description="Diagnose dead feeds and propose verified replacements.")
    ap.add_argument("--all", action="store_true", help="check every feed in feeds.py, not just dead ones")
    ap.add_argument("--name", help="force-check one current feed by name")
    ap.add_argument("--url", help="find replacements for an arbitrary feed URL (with --category/--label)")
    ap.add_argument("--category", default="tech", help="category for --url mode")
    ap.add_argument("--label", default="ad-hoc", help="label for --url mode")
    ap.add_argument("--post", action="store_true", help="also post the report to Discord")
    args = ap.parse_args()

    topics = load_topics()

    if args.url:
        targets = [(args.label, args.url, args.category)]
    else:
        feeds = feeds_by_name()
        if args.name:
            if args.name not in feeds:
                print(f"[ERROR] '{args.name}' not found in feeds.py", file=sys.stderr)
                sys.exit(1)
            _, url, cat, _ = feeds[args.name]
            targets = [(args.name, url, cat)]
        elif args.all:
            targets = [(n, u, c) for n, (n, u, c, t) in feeds.items()]
        else:
            health = load_health()
            dead = [n for n, v in health.items() if n in feeds and (
                    _as_int(v.get("consecutive_failures")) >= DEAD_AFTER_FAILURES
                    or _as_int(v.get("consecutive_zero_yield")) >= ZERO_YIELD_AFTER_RUNS)]
            if not dead:
                print("✓ All feeds healthy — nothing to diagnose. "
                      "(Run after fetch_rss.py has recorded a few runs.)")
                return
            targets = [(n, feeds[n][1], feeds[n][2]) for n in sorted(dead)]

    print(f"Diagnosing {len(targets)} feed(s); generating + probing candidates...\n",
          file=sys.stderr)
    report = diagnose(targets, topics)
    text = format_report(report)
    print(text)
    if args.post:
        post_to_discord(text)


if __name__ == "__main__":
    main()
