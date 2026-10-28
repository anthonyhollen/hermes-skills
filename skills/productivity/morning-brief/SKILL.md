---
name: morning-brief
description: "Use for questions or changes about the 7:30am morning news brief — a missing or duplicate story, a feed down, adding feeds/topics, feed_doctor, format or model changes, or the reader's 👍/👎 article ratings (feedback.py)."
version: 1.13.1
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [RSS, news, morning-brief, Discord, summarization, LLM]
    related_skills: [sg-commute-report, discord-messaging, blogwatcher]
---

# Morning Brief

A daily news brief posted to <#1499972245437550602> at **7:30am SGT** by cron job
`4879434e50ed` (`no_agent`, `deliver: local`, never a `discord:` target — the script posts
itself). Pipeline: fetch ~33 RSS feeds → match interests → apply the reader's ratings →
cluster same-story articles → pick 3 must-read + 5 maybe → LLM summaries → one Discord
message per article (plus header/footer).

## Where things are
- **Entry point:** `~/.hermes/scripts/morning_brief_run.py` (not in this dir) runs the three
  steps with per-step timeouts and posts a ⚠️ alert on failure. fetch / send run under
  `/usr/bin/python3` (feedparser); `summarize.py` runs under the first interpreter that can
  import Hermes' config (since Hermes v0.22.0 that needs ruamel.yaml, which the system
  python lacks).
- **Code:** `~/.hermes/skills/productivity/morning-brief/scripts/` — wiring in `CLAUDE.md`.
- **Data:** `~/.hermes/morning-brief/` — `interests.json` (edit directly), `articles.json` /
  `articles_summarized.json` (today's picks), `seen_articles.json` (ledger), `feed_health.json`,
  `feedback.json` (ratings) and `briefs/<date>.json` (each delivered article's message id).
- **What each run did:** `~/.hermes/cron/output/4879434e50ed/<date>.md` (full log incl. `[DOWN]`,
  `[DEDUP]`, held-back counts).
- **Safe to run:** `python3 send_brief.py --dry-run` (exact messages, posts nothing) and
  `morning_brief_run.py --dry-run` (real fetch + LLM, no post, no ledger/health writes). A bare
  `send_brief.py` **posts and marks articles seen**.

## "Why wasn't X in today's brief?" — check in pipeline order
1. **Already seen** — its url/title is in `seen_articles.json` (7 days for shown stories and
   their twins; 3 days for a `"kind": "chained"` entry — a member of a shown story's cluster
   that matched one of its twins).
2. **Held back** — a re-telling of a story shown in the last 7 days (same url, headline >55%
   alike, or lede ≥35% alike) is held while eligible; a weaker match is held one run only.
3. **Blocked** — a `block_keywords` hit (exact word, no plurals), or a deal/promo title
   ("$X off", "Deals:", giveaways, price drops — never shown).
4. **Stale / undated** — older than 7 days or no date.
5. **Teaser** — under 200 chars of RSS body.
6. **No topic match** — or only `EV`/`Grok`/`Sora`, which count only alongside a specific topic.
7. **Lost its cluster** — same story as a higher-ranked article (see `_twins` on the picks in
   `articles.json`).
8. **Ranking / caps** — score = topic *families* (synonyms count once, max 3) × tier ×
   recency × outlet bonus (+25% per extra outlet, max +50%); newsletters/roundups/guides ×0.5.
   Must-read needs (≥2 outlets and ≥2 families) or ≥3 families, published ≤48 h, base score
   ≥10, not a roundup, and a C-tier feed needs ≥2 outlets — up to 3, **fewer is normal**.
   Then 5 maybe: ≤2 per outlet (Reddit 1), ≤3 stories of one area (e.g. AI) when another
   area's story scores ≥7.
9. **Ratings** — the reader's 👎s lowered it (`[PREF] ×0.xx` lines in the run log; below ×0.7
   it can't be must-read). `python3 feedback.py show` lists what was learned.

```bash
cd ~/.hermes/morning-brief && grep -il "<word>" seen_articles.json articles.json
grep -i "<word>" ~/.hermes/cron/output/4879434e50ed/$(date +%F)*.md
```

## "Today's brief had a duplicate"
Same-story detection is one function, `article_id.same_story`, used for clustering, the
send-time backstop and the hold-back. Find the pair's titles, then check it in a REPL
(`same_story(a, b)` returns why — `url`, `title@`, `content@` (lede) or `deep@` (first
1,000 chars, for commentary-led openings) — or `None`). Rules and calibration notes are in
`article_id.py`; history in `references/content-dedup-analysis.md`. Don't loosen a threshold
from one example — check the change against a full fetch. A repeat on the NEXT day: check
whether the shown copy's cluster recorded it (`_twins` / `_chained` in that day's
`articles.json` are gone by then, so look for it in `seen_articles.json`).

## Feeds
- **Down / zero-yield:** `feed_health.json` (`consecutive_failures`, `consecutive_zero_yield`;
  3 runs flags either). The brief adds a 🩺 line.
- **Find replacements:** `python3 feed_doctor.py` (dead + zero-yield) · `--all` · `--name "X"` ·
  `--url URL --category C --label L` · `--post`. Every candidate is live-probed the same way
  the pipeline fetches, so an LLM suggestion can't be a hallucination.
- **Add / remove:** edit `scripts/feeds.py` (`(name, url, category, tier)`; A=3×, B=2×, C=1×).
  Probe first — it must serve ≥200 chars of body. Why sources are missing:
  `references/feed-history.md`.

## Reader ratings (👍 / 👎)
The reader reacts on a brief's article messages: 👍 = like (+1), 👎 = dislike (−1); no
reaction = neutral (not evidence — skipping a day's brief teaches nothing); 👍 + 👎 cancel. Each 7:30 run (`fetch_rss` → `feedback.sync_reactions`) re-reads the reactions on the
last 7 days of briefs, so a rating can be added, changed or removed (un-react) for a week.
Ratings teach per-**outlet**, per-**topic** and per-**title-word** affinities plus a boost /
penalty for **follow-ups** of a rated story, which multiply a candidate's score by ×0.5–×1.8
(45-day half-life). Nothing is removed outright —
that's still `block_keywords`. The model and its constants are in `scripts/feedback.py`'s
docstring.
```bash
cd ~/.hermes/skills/productivity/morning-brief/scripts
python3 feedback.py show               # learned likes/dislikes + recent ratings
python3 feedback.py list [DATE]        # a brief's articles, numbered, with their ratings
python3 feedback.py sync [--dry-run]   # read reactions now instead of at 7:30
python3 feedback.py rate DATE N {-1,1} [--note TEXT]   # manual rating (wins over a reaction)
python3 feedback.py unrate DATE N
```
If the reader gives a rating or reason in conversation, record it with `rate` (reasons go in
`--note`; they're stored, not used for ranking) — never hand-edit `feedback.json`.
"Ratings aren't being picked up": check the run log's `Reader ratings:` line — a
`skipped`/`failed`/`HTTP 403` means the bot lost the channel's Read Message History.

## Topics & format
- **Topics:** `interests.json` `topics[]` (plurals match) / `block_keywords[]` (exact).
- **Format rules (easy to regress):** `•` bullets · `**[title](<url>)**` (the `<>` stops
  unfurling) · **one article per message** (a reaction must name one article) · each section
  header once · the U+2800 separator only after the last must-read and after the last maybe · lengths in UTF-16 units (`_dlen`) ·
  `embeds: []`, `flags: 4`, `allowed_mentions: {"parse": []}`. Always check with `--dry-run`.
  Layout: `CLAUDE.md` → Discord formatting rules.
- **Model:** follows Hermes' configured model (`hermes model`); read
  `references/provider-switch-checklist.md` first.

## Testing
```bash
cd ~/.hermes/skills/productivity/morning-brief/scripts
/usr/bin/python3 test_morning_brief.py      # offline suite (temp HOME; no network/LLM/Discord)
MB_WRAPPER=~/.hermes/scripts/morning_brief_run.py /usr/bin/python3 test_morning_brief.py
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/morning_brief_run.py --dry-run
```
The gateway venv has no `feedparser`; fetch / send and the suite run under `/usr/bin/python3`,
and summarize under a Hermes interpreter (see the entry point above).

## Invariants
- Record the ledger only after delivery, and only what was delivered. The ratings manifest
  is written after the ledger, best-effort — it can never cost the ledger write.
- Ratings only re-rank; any failure in them ranks as if there were none.
- One same-story predicate (`article_id.same_story`); one message builder (`build_messages`);
  one HTTP-with-deadline module (`bounded_http`) — second copies are how past bugs happened.
- Each step's own budget sits below its wrapper timeout, and those below the 600 s watchdog.
- Untrusted feed/LLM text is sanitized before Discord (title, URL and summary).
- Feeds are parsed from bytes, never `resp.text`.

## References
| File | Read when |
|------|-----------|
| `CLAUDE.md` | Editing code — flow, data shapes, constants, per-file gotchas |
| `CHANGELOG.md` | What changed and why, per version |
| `references/content-dedup-analysis.md` | Duplicate / missed-story questions |
| `references/feed-history.md` | Adding/removing a feed; why a source is absent |
| `references/performance-notes.md` | Timing numbers and why each tunable is set |
| `references/provider-switch-checklist.md` | Before/after switching the model |
| `references/pitfalls-log.md` | Per-bug history (31 entries) |
| `references/discord-rendering-quirks.md` | Discord formatting gotchas |
| `scripts/bounded_http.py` | Its docstring: the three request-bounding traps |
| `references/session-fixes.md`, `bug18-siri-dedup.md`, `minimax-api-quirks.md` | Historical only (superseded; see their dated notes) |
