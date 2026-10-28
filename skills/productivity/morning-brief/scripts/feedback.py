#!/usr/bin/env python3
"""Reader ratings: 👍 / 👎 reactions on the brief → a per-article ranking multiplier.
No reaction = neutral: the article adds no evidence either way.

    python3 feedback.py show                       # what the ratings have taught the ranker
    python3 feedback.py list [DATE]                # a brief's articles, numbered, with ratings
    python3 feedback.py sync [--dry-run]           # read the reactions now (fetch_rss does this)
    python3 feedback.py rate DATE N {-1,1} [--note TEXT]     # manual rating (overrides a reaction)
    python3 feedback.py unrate DATE N              # drop a manual rating
    (DATE = YYYY-MM-DD, "today" or "latest"; N = the article's position in that brief, 1-based)

Flow
  send_brief.send_messages → record_brief(): after delivery, writes briefs/<date>.json —
      one entry per DELIVERED article message (its Discord message id + the features below).
      Each article is its own message (maybe-relevant is no longer chunked) so a reaction
      names exactly one article.
  fetch_rss.main → sync_reactions(): lists the channel's messages since the oldest brief
      message in the last COLLECT_DAYS (1–2 GETs; each message carries its reaction counts)
      and reconciles feedback.json — a removed reaction removes the rating. Runs on a thread
      beside the feed fetch, so it costs no wall time; any failure keeps the stored ratings.
  fetch_rss.match_articles → preference(): `_score *= mult`, before clustering, so a liked
      outlet also tends to lead its cluster; mult < PREF_MUST_READ_FLOOR can't be must-read.

The model (all ratings, each weighted w = 0.5 ** (age_days / HALF_LIFE_DAYS)):
  affinity(feature) = Σ rating·w / (SHRINK_K + Σ w)      — in (-1, 1); unrated articles
      are not evidence (a day the reader skips the brief must not read as "all neutral").
  (a rating's title words share ONE unit of weight between them, w / n_terms each — one
  rating is one piece of evidence, not one per word)
  log mult = conf · ( W_OUTLET · a(outlet)
                    + W_TOPIC  · mean a(topic) over the article's matched interest topics
                    + W_TERM   · clip(Σ a(term) over its distinctive title words, ±1) )
           + W_STORY  · rating·w of the most recent same-story rated article (≤ STORY_DAYS)
  conf = N / (N + CONF_K), N = total rating weight: the correlated features of a few ratings
  can't stack into a big swing — one 👎 on a Verge "AI" story is ×0.89 on the next Verge AI
  story, it takes ~5 consistent 👎 on an outlet to reach the must-read floor (×0.7).
  mult = clamp(exp(log mult), MULT_MIN, MULT_MAX). No ratings → 1.0 exactly (no behaviour
  change). Nothing is excluded outright — block_keywords is the hard cut.

Fail-safe by construction: a missing/corrupt feedback.json or manifest is read as "no
ratings", and every entry point used by the pipeline catches its own errors — ratings may
be stale for a day, the brief is never lost over them.
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from article_id import (_distinctive, _slots, outlet_key, same_story_f,  # noqa: E402
                        story_features, story_tokens, title_tokens, with_slots)

BASE_DIR      = os.path.expanduser("~/.hermes/morning-brief")
BRIEFS_DIR    = os.path.join(BASE_DIR, "briefs")
FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback.json")
ENV_FILE      = os.path.expanduser("~/.hermes/.env")

# Emoji → rating. Skin-tone modifiers and VS16 are stripped before lookup (👍🏽 == 👍).
RATING_EMOJI = {"👍": 1, "👎": -1}      # no reaction = neutral = no rating
COLLECT_DAYS       = 7      # reactions are re-read for this many days; older ratings freeze
MANIFEST_KEEP_DAYS = 30
RATING_KEEP_DAYS   = 365    # at a 45-day half-life a year-old rating weighs ~0.4%
SYNC_BUDGET_SECONDS = 20    # runs beside the ~20 s feed fetch (FETCH_BUDGET_SECONDS = 100)
SYNC_MAX_PAGES     = 5      # 100 messages a page; the channel gets ~15/day
SYNC_MAX_BYTES     = 4 * 1024 * 1024

HALF_LIFE_DAYS = 45
SHRINK_K       = 2.0        # 1 rating → ±0.33, 3 → ±0.6, 8 → ±0.8
W_OUTLET, W_TOPIC, W_TERM, W_STORY = 0.8, 0.6, 0.4, 0.4
CONF_K         = 3.0        # evidence scaling: 1 rating → 25% strength, 9 → 75%
STORY_DAYS     = 14         # follow-ups only matter while the story is live
MULT_MIN, MULT_MAX   = 0.5, 1.8
PREF_MUST_READ_FLOOR = 0.7  # a strongly disliked article may still be a maybe, not a must-read

_EMOJI_MODIFIERS = re.compile("[\uFE0F\U0001F3FB-\U0001F3FF]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Small helpers ─────────────────────────────────────────────────────────────

def _load_env_value(name: str) -> str:
    """env first, then ~/.hermes/.env (the cron scrubs secrets from the child env)."""
    val = os.environ.get(name, "")
    if val:
        return val
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return ""


def _read_json(path: str, default):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):       # missing, truncated, bad bytes
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: str, data) -> None:
    """Atomic (temp + fsync + os.replace), like every other state file in this skill."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _snowflake(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _str_list(v) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def article_terms(art: dict, topics=()) -> list:
    """Distinctive title words (article_id's generic/stop lists removed), minus the words
    of the article's matched topics — those are already the topic feature."""
    topic_words = {w for t in topics for w in title_tokens(t)}
    return sorted(_distinctive(title_tokens(art.get("title", ""), art.get("source")))
                  - topic_words)


def _features(entry: dict) -> list:
    """((kind, key), share) features of a stored rating entry. A rating's title words split
    one unit of weight — counting each at full weight let one rating dominate."""
    feats = []
    if isinstance(entry.get("outlet"), str) and entry["outlet"]:
        feats.append((("outlet", entry["outlet"]), 1.0))
    feats += [(("topic", t.lower()), 1.0) for t in _str_list(entry.get("topics"))]
    terms = _str_list(entry.get("terms"))
    feats += [(("term", t), 1.0 / len(terms)) for t in terms]
    return feats


# ── Manifests (written by send_brief after delivery) ──────────────────────────

def _manifest_path(date: str) -> str:
    return os.path.join(BRIEFS_DIR, f"{date}.json")


def record_brief(delivered: list, channel_id: str, now: datetime = None) -> int:
    """Record the article messages that reached Discord, for reaction sync. Returns the
    number recorded. `delivered` is [(message_id, section, article)]. A same-day re-run
    appends to that day's manifest (numbering continues)."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    path = _manifest_path(date)
    man = _read_json(path, {})
    arts = [a for a in man.get("articles", []) if isinstance(a, dict)] \
        if isinstance(man.get("articles"), list) else []
    have = {a.get("message_id") for a in arts}
    added = 0
    for msg_id, section, art in delivered:
        if not _snowflake(msg_id) or str(msg_id) in have:
            continue                    # "unknown" id (unparseable receipt) can't be rated
        topics = _str_list(art.get("_topics"))
        arts.append({
            "n": len(arts) + 1, "message_id": str(msg_id), "section": section,
            "title": art.get("title", ""), "url": art.get("url", ""),
            "source": art.get("source", ""), "outlet": outlet_key(art),
            "topics": topics, "terms": article_terms(art, topics),
            "story": sorted(story_tokens(art))[:80], "sent_ts": now.timestamp(),
            # sequel flags judged WITH the body (as the seen ledger stores them), so a
            # sequel isn't taken for a follow-up of the rated story
            "slots": [list(t) for t in _slots(art.get("title", ""), art.get("source"),
                                              art.get("content") or "")],
        })
        added += 1
    if added:
        _write_json(path, {"date": date, "channel_id": str(channel_id), "articles": arts})
    _prune_manifests(now)
    return added


def _prune_manifests(now: datetime) -> None:
    cutoff = (now - timedelta(days=MANIFEST_KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        names = os.listdir(BRIEFS_DIR)
    except OSError:
        return
    for name in names:
        if name.endswith(".json") and _DATE_RE.match(name[:-5]) and name[:-5] < cutoff:
            try:
                os.remove(os.path.join(BRIEFS_DIR, name))
            except OSError:
                pass


def _manifest_dates() -> list:
    try:
        names = os.listdir(BRIEFS_DIR)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and _DATE_RE.match(n[:-5]))


def load_manifest(date: str) -> dict:
    man = _read_json(_manifest_path(date), {})
    arts = man.get("articles")
    man["articles"] = [a for a in arts if isinstance(a, dict)] if isinstance(arts, list) else []
    return man


def _resolve_date(date: str) -> str:
    if date == "today":
        return datetime.now().strftime("%Y-%m-%d")
    if date == "latest":
        dates = _manifest_dates()
        return dates[-1] if dates else ""
    return date if _DATE_RE.match(date or "") else ""


# ── Ratings store ─────────────────────────────────────────────────────────────

def load_ratings() -> dict:
    """{message_id: rating entry}; malformed entries dropped."""
    data = _read_json(FEEDBACK_FILE, {})
    raw = data.get("ratings") if isinstance(data.get("ratings"), dict) else {}
    # Type-check what the model/CLI index or hash: one hand-edited entry with a list
    # `outlet` or a null title used to disable learning for the whole run.
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and type(v.get("rating")) is int and v["rating"] in (-1, 1)
            and isinstance(v.get("title", ""), str) and isinstance(v.get("outlet", ""), str)}


def save_ratings(ratings: dict, now_ts: float = None) -> None:
    now_ts = now_ts or time.time()
    cutoff = now_ts - RATING_KEEP_DAYS * 86400
    kept = {k: v for k, v in ratings.items() if _num(v.get("sent_ts")) >= cutoff}
    _write_json(FEEDBACK_FILE, {"version": 1, "updated_ts": now_ts, "ratings": kept})


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and abs(f) != float("inf") else 0.0


def rating_from_reactions(reactions) -> int | None:
    """The reader's rating of one message from its reaction summary, or None if unrated.
    A reaction the bot itself added (`me`) doesn't count. 👍 + 👎 together cancel (unrated)."""
    votes = set()
    for r in reactions if isinstance(reactions, list) else []:
        if not isinstance(r, dict):
            continue
        name = (r.get("emoji") or {}).get("name") if isinstance(r.get("emoji"), dict) else None
        v = RATING_EMOJI.get(_EMOJI_MODIFIERS.sub("", name or ""))
        if v is None:
            continue
        if int(_num(r.get("count"))) - (1 if r.get("me") else 0) > 0:
            votes.add(v)
    if not votes:
        return None
    if {1, -1} <= votes:
        return None
    return 1 if 1 in votes else -1


def _entry_from_manifest(date: str, a: dict, rating: int, via: str, note: str = None) -> dict:
    e = {k: a.get(k) for k in ("title", "url", "source", "outlet", "topics", "terms",
                               "story", "slots", "sent_ts", "section", "n")}
    e.update(date=date, rating=rating, via=via, rated_ts=time.time())
    if note:
        e["note"] = note
    return e


def _discord_get(url: str, token: str, params: dict, deadline: float):
    from bounded_http import read_bounded, request_bounded   # needs requests; lazy for the CLI
    remaining = max(1.0, deadline - time.monotonic())
    resp = request_bounded(
        "GET", deadline, url=url, params=params, stream=True,
        headers={"Authorization": f"Bot {token}",
                 # python-urllib/requests UAs get Cloudflare-1010'd; the send path uses this
                 "User-Agent": "DiscordBot (hermes-agent/1.0)",
                 # read_bounded inflates gzip/deflate only; urllib3 would advertise br/zstd
                 # if those modules exist (same pin as fetch_feed, 1.10.0)
                 "Accept-Encoding": "gzip, deflate"},
        timeout=(5, min(10.0, remaining)))
    if resp.status_code != 200:
        resp.close()
        raise RuntimeError(f"Discord HTTP {resp.status_code}")
    return read_bounded(resp, deadline, SYNC_MAX_BYTES).json()


def sync_reactions(deadline: float = None, persist: bool = True,
                   now: datetime = None) -> tuple:
    """Re-read the reactions on the last COLLECT_DAYS of brief messages. Returns
    (status line, ratings). Only messages actually seen in a response are reconciled, so a
    failed or partial fetch never deletes a rating; manual ratings are never overwritten."""
    now = now or datetime.now()
    deadline = deadline or (time.monotonic() + SYNC_BUDGET_SECONDS)
    ratings = load_ratings()
    first = (now - timedelta(days=COLLECT_DAYS)).strftime("%Y-%m-%d")
    wanted = {}                         # message_id -> (date, manifest entry)
    channels = {}                       # channel_id -> [message_id]
    for date in _manifest_dates():
        if date < first:
            continue
        man = load_manifest(date)
        ch = str(man.get("channel_id") or "")
        for a in man["articles"]:
            mid = str(a.get("message_id") or "")
            if ch and _snowflake(mid):
                wanted[mid] = (date, a)
                channels.setdefault(ch, []).append(mid)
    if not wanted:
        return "no rateable brief messages in the last %dd" % COLLECT_DAYS, ratings
    token = _load_env_value("DISCORD_BOT_TOKEN")
    if not token:
        return "skipped: DISCORD_BOT_TOKEN not found", ratings

    seen, errors = {}, []
    for ch, mids in channels.items():
        after, last = min(_snowflake(m) for m in mids) - 1, max(_snowflake(m) for m in mids)
        try:
            for _page in range(SYNC_MAX_PAGES):
                page = _discord_get(f"https://discord.com/api/v10/channels/{ch}/messages",
                                    token, {"after": str(after), "limit": 100}, deadline)
                if not isinstance(page, list) or not page:
                    break
                for m in page:
                    if isinstance(m, dict) and str(m.get("id")) in wanted:
                        seen[str(m["id"])] = m.get("reactions") or []
                after = max(_snowflake(m.get("id")) for m in page if isinstance(m, dict))
                if len(page) < 100 or after >= last:
                    break
        except Exception as e:          # noqa: BLE001 — keep what was read, keep old ratings
            errors.append(f"{type(e).__name__}: {e}")

    added = changed = removed = 0
    for mid, reactions in seen.items():
        date, a = wanted[mid]
        old = ratings.get(mid)
        if old and old.get("via") == "manual":
            continue
        r = rating_from_reactions(reactions)
        if r is None:
            if old:
                del ratings[mid]
                removed += 1
        elif not old:
            ratings[mid] = _entry_from_manifest(date, a, r, "reaction")
            added += 1
        elif old.get("rating") != r:
            old.update(rating=r, rated_ts=time.time())
            changed += 1
    if persist and (added or changed or removed):
        save_ratings(ratings)
    status = (f"read {len(seen)}/{len(wanted)} brief messages; {added} new, {changed} changed, "
              f"{removed} removed rating(s); {len(ratings)} total"
              + (f" — {'; '.join(errors)}" if errors else "")
              + ("" if persist else " (dry run: not saved)"))
    return status, ratings


# ── Model ─────────────────────────────────────────────────────────────────────

class Model:
    """Affinities per (kind, key) plus the recent rated stories, built once per run."""

    def __init__(self, ratings: dict, now_ts: float = None):
        now_ts = now_ts or time.time()
        num, den, total_w = {}, {}, 0.0
        self.stories = []
        self.n = {-1: 0, 0: 0, 1: 0}
        for e in ratings.values():
            r = e.get("rating")
            if r not in (-1, 1):
                continue
            self.n[r] += 1
            age = max(0.0, now_ts - _num(e.get("sent_ts"))) / 86400
            w = 0.5 ** (age / HALF_LIFE_DAYS)
            total_w += w
            for f, share in _features(e):
                num[f] = num.get(f, 0.0) + r * w * share
                den[f] = den.get(f, 0.0) + w * share
            if r and age <= STORY_DAYS:
                ledger_like = {"title": e.get("title", ""), "url": e.get("url", ""),
                               "source": e.get("source"), "_story": _str_list(e.get("story"))}
                ef = story_features(ledger_like)
                slots = e.get("slots")
                if isinstance(slots, list):     # sequel flags judged with its body, as
                    ef = with_slots(ef, tuple((str(t[0]), bool(t[1])) for t in slots  # _hold_grade
                                              if isinstance(t, list) and len(t) == 2))
                self.stories.append((_num(e.get("sent_ts")), r * w, e.get("title", ""), ef))
        self.aff = {f: num[f] / (SHRINK_K + den[f]) for f in num}
        self.evidence = den
        self.conf = total_w / (total_w + CONF_K) if total_w else 0.0
        self.stories.sort(key=lambda s: s[0], reverse=True)       # most recent first

    def __bool__(self):
        return bool(self.aff) or bool(self.stories)

    def preference(self, art: dict, art_f: tuple = None) -> tuple:
        """(multiplier, reason) for a live candidate. `_topics` must be set (match_articles)."""
        topics = _str_list(art.get("_topics"))
        outlet = outlet_key(art)
        terms = article_terms(art, topics)
        parts, why = 0.0, []
        a = self.aff.get(("outlet", outlet), 0.0)
        if abs(a) >= 0.01:
            parts += W_OUTLET * a
            why.append(f"outlet {outlet} {a:+.2f}")
        if topics:
            ta = [self.aff.get(("topic", t.lower()), 0.0) for t in topics]
            m = sum(ta) / len(ta)
            if abs(m) >= 0.01:
                parts += W_TOPIC * m
                why.append(f"topics {m:+.2f}")
        s = max(-1.0, min(1.0, sum(self.aff.get(("term", t), 0.0) for t in terms)))
        if abs(s) >= 0.01:
            parts += W_TERM * s
            why.append(f"terms {s:+.2f}")
        parts *= self.conf
        if self.stories:
            af = art_f or story_features(art)
            for _ts, rw, title, ef in self.stories:
                if same_story_f(af, ef):
                    parts += W_STORY * rw
                    why.append(f"follow-up of {'liked' if rw > 0 else 'disliked'} "
                               f"“{title[:40]}”")
                    break
        mult = max(MULT_MIN, min(MULT_MAX, math.exp(parts)))
        return mult, ", ".join(why)


def build_model(ratings: dict = None) -> Model | None:
    """None when there is nothing to learn from — the caller then skips the pass."""
    m = Model(load_ratings() if ratings is None else ratings)
    return m or None


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_show() -> None:
    ratings = load_ratings()
    m = Model(ratings)
    print(f"{len(ratings)} rating(s): {m.n[1]} 👍 · {m.n[-1]} 👎 "
          f"(half-life {HALF_LIFE_DAYS}d; multiplier ×{MULT_MIN}–×{MULT_MAX}; "
          f"must-read needs ≥ ×{PREF_MUST_READ_FLOOR})")
    for kind, label in (("outlet", "Outlets"), ("topic", "Topics"), ("term", "Title words")):
        rows = sorted(((a, k, m.evidence[(kd, k)]) for (kd, k), a in m.aff.items()
                       if kd == kind and abs(a) >= 0.05), reverse=True)
        if not rows:
            continue
        print(f"\n{label}:")
        show = rows[:8] + [r for r in rows[-8:] if r not in rows[:8]]
        for a, k, ev in show:
            print(f"  {a:+.2f}  {k}  (weight {ev:.1f})")
    recent = sorted(ratings.values(), key=lambda e: _num(e.get("sent_ts")), reverse=True)[:10]
    if recent:
        print("\nRecent:")
        for e in recent:
            face = {1: "👍", -1: "👎"}[e["rating"]]
            print(f"  {face} {e.get('date', '')} #{e.get('n', '?')} {str(e.get('title') or '')[:70]}"
                  f" [{e.get('via')}]" + (f" — {e['note']}" if e.get("note") else ""))


def _cmd_list(date: str) -> int:
    d = _resolve_date(date or "latest")
    man = load_manifest(d) if d else {"articles": []}
    if not man["articles"]:
        print(f"no brief recorded for {date or 'latest'}", file=sys.stderr)
        return 1
    ratings = load_ratings()
    print(f"Brief {d}:")
    for a in man["articles"]:
        r = ratings.get(a.get("message_id"), {}).get("rating")
        face = {1: "👍", -1: "👎", None: "  "}[r]
        print(f"  {str(a.get('n')):>2}. {face} [{a.get('section')}] {a.get('title', '')[:80]}"
              f"  ({a.get('source')})")
    return 0


def _cmd_rate(date: str, n: str, rating, note: str = None) -> int:
    d = _resolve_date(date)
    man = load_manifest(d) if d else {"articles": []}
    hit = [a for a in man["articles"] if str(a.get("n")) == str(n).lstrip("0")]
    if not hit:
        print(f"no article #{n} in the brief for {date}", file=sys.stderr)
        return 1
    a = hit[0]
    ratings = load_ratings()
    mid = str(a.get("message_id"))
    if rating is None:
        if ratings.get(mid, {}).get("via") != "manual":
            print(f"#{n} has no manual rating", file=sys.stderr)
            return 1
        del ratings[mid]
        save_ratings(ratings)
        print(f"removed manual rating for #{n} {a.get('title', '')[:70]} "
              f"(a reaction on it will be picked up at the next sync)")
        return 0
    ratings[mid] = _entry_from_manifest(d, a, rating, "manual", note)
    save_ratings(ratings)
    face = {1: "👍", -1: "👎"}[rating]
    print(f"{face} {d} #{n} {a.get('title', '')[:70]}" + (f" — note: {note}" if note else ""))
    return 0


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.split("\nFlow")[0].strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "show":
        _cmd_show()
        return 0
    if cmd == "list":
        return _cmd_list(rest[0] if rest else None)
    if cmd == "sync":
        status, _ = sync_reactions(persist="--dry-run" not in rest)
        print(status)
        return 0
    if cmd in ("rate", "unrate"):
        note = None
        if "--note" in rest:
            i = rest.index("--note")
            note = " ".join(rest[i + 1:]).strip() or None
            rest = rest[:i]
        want = 2 if cmd == "unrate" else 3
        if len(rest) != want or (cmd == "rate" and rest[2] not in ("-1", "1", "+1")):
            print("usage: feedback.py rate DATE N {-1,1} [--note TEXT] | unrate DATE N",
                  file=sys.stderr)
            return 2
        return _cmd_rate(rest[0], rest[1], int(rest[2]) if cmd == "rate" else None, note)
    print(f"unknown command {cmd!r} (show | list | sync | rate | unrate)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
