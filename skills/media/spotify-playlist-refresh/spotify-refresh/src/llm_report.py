"""
llm_report.py — the nightly DeepSeek skip report (v2.21).

What share of the DeepSeek songs the listener reached they skipped — today, day
by day and since DeepSeek took over — against the benchmark: the artist-graph
discovery songs DeepSeek replaced (deep cuts by the top-10 liked artists;
sampler-judged playlists of 1–6 Oct 2026: finished 6 of 23, skipped 17 → the
"26%" finish rate).

Counting is the one stats() and the old skip-check digest use: one verdict per
playlist position (skip_signals.day_outcomes / _outcome). Skipped = the sampler
saw a skip or a skim (left before 90%); finished = played past 90%. Stops,
pass-throughs and unknown lengths are not counted. First listens — picks
(source llm, incl. "more from" songs) and new releases (wildcard) — are what is
compared: the benchmark songs were first listens too. Encores (a DeepSeek song
the listener already finished, offered again) are shown apart. Today and the
per-day line count by listening day (04:00→04:00 SGT, every playlist version
live that day over its part of it — a manual refresh or a failed 03:00 one
doesn't break them); the running totals count each version over its lifetime,
as stats() does.

DB only: no Spotify or DeepSeek calls. Run by
~/.hermes/scripts/spotify_deepseek_report_wrapper.py (no_agent cron, 23:30 SGT),
which rebuilds the skip signals first; `python3 llm_report.py` (from src/)
prints the report from the signals as they are.
"""
import math, sys
from datetime import datetime, timezone, timedelta
import db, skip_signals
from llm_discovery import _wilson

SGT = timezone(timedelta(hours=8))
DAY_MS = 86400 * 1000
# The first DeepSeek playlist went live 2026-10-06 23:27 SGT.
LAUNCH_MS = int(datetime(2026, 10, 6, 23, 0, tzinfo=SGT).timestamp() * 1000)
# Before DeepSeek: artist-graph discovery in the sampler-judged playlists of
# 2026-10-01 → 10-06, recomputed 2026-10-12 with exactly these rules. Fixed here
# because the signals behind it age out of skip_signals' 90-day rebuild — so
# recompute it (from a backup DB) if the skip rules / judge thresholds change.
BENCHMARK = {"label": "artist-graph discovery, 1–6 Oct", "finished": 6, "skipped": 17}
FIRST_LISTEN = ("llm", "wildcard")
DEEPSEEK = FIRST_LISTEN + ("encore",)
MIN_JUDGED = 10          # fewer first-listen verdicts than this → no better / worse call yet
DAYS_SHOWN = 7


def _deepseek_versions(since_ms: int) -> list[dict]:
    """Playlist versions live at or after since_ms that held DeepSeek songs, oldest first."""
    return [v for v in db.get_playlist_versions(since_ms)
            if v.get("origin") != "bootstrap" and any(s in DEEPSEEK for s in v["sources"])]


def _tally(version: dict, lo: int, hi: int) -> dict[str, list[int]]:
    """{source: [finished, skipped]} over one version's DeepSeek positions, judged
    on what happened between lo and hi (inside the version's lifetime)."""
    o = skip_signals.day_outcomes(dict(version, applied_at=lo), hi)
    counts: dict[str, list[int]] = {}
    for pos, src in enumerate(version["sources"], 1):
        if src not in DEEPSEEK:
            continue
        section, _ = skip_signals._outcome(o["positions"][pos])
        c = counts.setdefault(src, [0, 0])
        if section == "done":
            c[0] += 1
        elif section in ("skip", "skim"):
            c[1] += 1
    return counts


def _add(into: dict, counts: dict):
    for src, (f, k) in counts.items():
        c = into.setdefault(src, [0, 0])
        c[0] += f
        c[1] += k


def version_tallies(since_ms: int, now_ms: int) -> list[dict]:
    """Versions applied since since_ms with DeepSeek songs, each judged over its
    whole lifetime (as stats()): [{version_id, applied_at, counts}]."""
    return [{"version_id": v["version_id"], "applied_at": v["applied_at"],
             "counts": _tally(v, v["applied_at"], now_ms)}
            for v in _deepseek_versions(since_ms) if v["applied_at"] >= since_ms]


def day_start(ms: int) -> int:
    """04:00 SGT opening the listening day that contains ms (skip_signals' day)."""
    d = datetime.fromtimestamp((ms - skip_signals.DAY_SHIFT_MS) / 1000, SGT)
    return int(d.replace(hour=4, minute=0, second=0, microsecond=0).timestamp() * 1000)


def day_tallies(days: int, now_ms: int) -> list[dict]:
    """The last `days` listening days, today last: [{day_start, counts}] — every
    DeepSeek version live that day, judged over its part of the day."""
    today = day_start(now_ms)
    starts = [today - i * DAY_MS for i in range(days - 1, -1, -1)]
    versions = _deepseek_versions(starts[0])
    ends = {v["version_id"]: skip_signals._version_end(v) or now_ms for v in versions}
    out = []
    for ds in starts:
        counts: dict[str, list[int]] = {}
        for v in versions:
            lo, hi = max(v["applied_at"], ds), min(ends[v["version_id"]], ds + DAY_MS, now_ms)
            if hi > lo:
                _add(counts, _tally(v, lo, hi))
        out.append({"day_start": ds, "counts": counts})
    return out


def _sum(tallies: list[dict], sources=FIRST_LISTEN) -> tuple[int, int]:
    """(finished, skipped) over the tallies' given sources."""
    f = sum(t["counts"].get(s, [0, 0])[0] for t in tallies for s in sources)
    k = sum(t["counts"].get(s, [0, 0])[1] for t in tallies for s in sources)
    return f, k


def _pct(k: int, n: int) -> str:
    return f"{round(100 * k / n)}%" if n else "—"


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, SGT).strftime("%-d %b")


def verdict_line(skipped: int, judged: int) -> str:
    """Better / worse than the benchmark: the 95% range of the DIFFERENCE between
    the two skip shares (Newcombe's hybrid score interval from the two Wilson
    ranges) — the benchmark is only 23 songs, so it is uncertain too."""
    bf, bk = BENCHMARK["finished"], BENCHMARK["skipped"]
    if judged < MIN_JUDGED:
        return f"➖ Too few new DeepSeek songs judged yet ({judged}) to compare."
    p1, p2 = skipped / judged, bk / (bf + bk)
    (l1, u1), (l2, u2) = _wilson(skipped, judged), _wilson(bk, bf + bk)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    a, b = round(100 * lo), round(100 * hi)          # points; negative = fewer skips than before
    shares = f"{_pct(skipped, judged)} vs {_pct(bk, bf + bk)} skipped"
    if hi < 0:
        return (f"✅ **Better than before:** {-round(100 * d)} points fewer skips ({shares})\n"
                f"-# 95% range: {max(0, -b)}–{-a} points fewer")
    if lo > 0:
        return (f"❌ **Worse than before:** {round(100 * d)} points more skips ({shares})\n"
                f"-# 95% range: {max(0, a)}–{b} points more")
    return (f"➖ **No clear difference yet** ({shares})\n"
            f"-# 95% range: {-a} points fewer to {b} more")


def usage_line(now_ms: int) -> str:
    """Last night's DeepSeek reasoning against the fixed max_tokens ceiling — the
    early warning that the request needs trimming (never a bigger limit)."""
    import json
    import llm_discovery as L
    cfg = L.load_config()
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, detail FROM llm_batch WHERE created_at >= ? ORDER BY batch_id DESC LIMIT 1",
                           (now_ms - 36 * 3600 * 1000,)).fetchone()
    if not row:
        return "⚠️ DeepSeek hasn't run in the last 36 h."
    status, detail = row
    try:
        d = json.loads(detail or "{}")
    except Exception:
        d = {}
    rs = d.get("reasoning")
    if not rs:
        att = d.get("attempts") or []
        cap = next((a.get("max_tokens") for a in att if a.get("max_tokens")), None) or cfg["max_tokens"]
        rs = L.reasoning_share(att, int(cap))
    if not rs:
        if status == "running":
            return "-# DeepSeek is still running."
        err = next((a.get("error") for a in d.get("attempts") or [] if a.get("thinking") and a.get("error")), None)
        return (f"⚠️ Last night's DeepSeek thinking call failed ({str(err)[:60]})." if err
                else f"⚠️ Last night's DeepSeek run: {status}, no reasoning recorded.")
    text = (f"DeepSeek used {rs['tokens'] / 1000:.0f}k of its fixed {rs['max_tokens'] / 1000:.0f}k token limit "
            f"last night ({round(100 * rs['share'])}%)")
    warn = float(cfg["reasoning_warn_share"])
    if rs.get("cut_off"):
        return f"⚠️ {text} — it was cut off; trim its request, don't raise the limit."
    if rs["share"] > warn:
        return f"⚠️ {text} — over {round(100 * warn)}%; trim its request, don't raise the limit."
    return f"-# {text}."


def _day_row(ms: int, f: int, k: int) -> str:
    """One aligned line of the per-day table: 'Tue  7 Oct    4 of 14   29%  ███'."""
    d = datetime.fromtimestamp(ms / 1000, SGT)
    pct = round(100 * k / (f + k))
    return (f"{d.strftime('%a')} {d.day:>2} {d.strftime('%b')}  {k:>3} of {f + k:<3} {pct:>3}%  "
            f"{'█' * round(pct / 10)}").rstrip()


def build_report(now_ms: int | None = None) -> str:
    """The nightly report ('' when no DeepSeek song was finished or skipped in
    today's listening day — empty stdout = no Discord message). Short blocks
    with blank lines between them; the per-day numbers as an aligned table."""
    now = now_ms or skip_signals._now_ms()
    days = day_tallies(DAYS_SHOWN, now)
    tf, tk = _sum(days[-1:])
    ef, ek = _sum(days[-1:], ("encore",))
    if not tf + tk + ef + ek:
        return ""
    since = max(LAUNCH_MS, now - (skip_signals.REBUILD_DAYS - 1) * DAY_MS)   # signals only reach back 90 days
    tallies = version_tallies(since, now)
    when = datetime.fromtimestamp(now / 1000, SGT).strftime("%a %-d %b")
    out = [f"## 🎧 DeepSeek skip report — {when}", ""]

    out.append("**Today**")
    out.append(f"Skipped **{tk} of {tf + tk}** new DeepSeek songs ({_pct(tk, tf + tk)}) · finished {tf}"
               if tf + tk else "No new DeepSeek songs finished or skipped")
    if ef + ek:
        out.append(f"Repeats of songs you'd finished: skipped {ek} of {ef + ek}")
    out.append("")

    f, k = _sum(tallies)
    label = f"Since DeepSeek started ({_date(LAUNCH_MS)})" if since == LAUNCH_MS \
        else f"Last {skip_signals.REBUILD_DAYS - 1} days"
    out.append(f"**{label}**")
    out.append(f"Skipped **{_pct(k, f + k)}** of new songs ({k} of {f + k}) · finished {_pct(f, f + k)}")
    for src, name in (("llm", "Picks"), ("wildcard", "New releases")):
        sf, sk = _sum(tallies, (src,))
        if sf + sk:
            out.append(f"• {name}: {sk} of {sf + sk} skipped ({_pct(sk, sf + sk)})")
    rf, rk = _sum(tallies, ("encore",))
    if rf + rk:
        out.append(f"• Repeats: {rk} of {rf + rk} skipped (not in the comparison)")
    if since < days[0]["day_start"]:
        wf, wk = _sum(days)
        out.append(f"• Last {DAYS_SHOWN} days: {_pct(wk, wf + wk)} skipped ({wk} of {wf + wk})")
    out.append("")

    bf, bk = BENCHMARK["finished"], BENCHMARK["skipped"]
    out.append(f"**Before DeepSeek** ({BENCHMARK['label']})")
    out.append(f"Skipped **{_pct(bk, bf + bk)}** ({bk} of {bf + bk}) · finished {_pct(bf, bf + bk)}")
    out.append("")
    out.append(verdict_line(k, f + k))
    out.append("")

    rows = [_day_row(d["day_start"], df, dk) for d in days for df, dk in [_sum([d])] if df + dk]
    if rows:
        out.append("**Skipped per day** (new songs)")
        out += ["```", *rows, "```"]
    try:
        out.append(usage_line(now))
    except Exception as e:          # the usage lines are a bonus — never cost the report
        out.append(f"-# (DeepSeek usage unavailable: {type(e).__name__})")
    try:
        import llm_discovery as L
        b = L.budget_state(now)
        if b["refused"]:
            out.append(f"⚠️ DeepSeek's daily budget refused {b['refused']} call(s) today "
                       f"({b['used'] / 1000:.0f}k of {b['cap'] / 1000:.0f}k tokens used).")
    except Exception:
        pass
    out.append("-# Counted: new DeepSeek songs you finished (past 90%) or skipped; stops and pass-throughs aren't.")
    return "\n".join(out)


if __name__ == "__main__":
    db.init_db()
    print(build_report() or "(nothing to report: no DeepSeek song finished or skipped today)")
    sys.exit(0)
