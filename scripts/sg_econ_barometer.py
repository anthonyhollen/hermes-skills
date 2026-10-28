#!/usr/bin/env python3
"""SG Economy Barometer (Tier-1).

Fetches Singapore macro indicators (SingStat Table Builder + data.gov.sg),
computes month/quarter-on-period and year-on-year trends, a polarity-aware
diffusion composite, and posts a Discord report.

Pure-Python, near-zero token (no LLM in v1). All HTTP goes through `curl`
because SingStat's WAF 403s Python's urllib (same class as the NEA gotcha in
the vault's API Reference). Indicator set is data-driven via
sg_econ_indicators.json. State/history in .sg_econ_state.json (atomic writes).
"""
import argparse, json, os, re, statistics, subprocess, sys, time, urllib.parse
from datetime import datetime, timezone, timedelta

HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
ENVF = os.path.join(HOME, ".env")
SCRIPTS = os.path.join(HOME, "scripts")
REGISTRY = os.path.join(SCRIPTS, "sg_econ_indicators.json")
STATE_FILE = os.path.join(SCRIPTS, ".sg_econ_state.json")
SGT = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
STEP = {"monthly": 12, "quarterly": 4, "annual": 1}
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
IMON = {v: k for k, v in MONTHS.items()}


# ---------- env (read-only; never prints secret values) ----------
def gt_env_value(key):
    try:
        for l in open(ENVF, encoding="utf-8"):
            m = re.match(r"\s*%s\s*=\s*(.+)" % re.escape(key), l)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except Exception:
        pass
    return None


# ---------- http via curl (urllib is 403'd by SingStat) ----------
def http_get(url, timeout=30):
    try:
        r = subprocess.run(["curl", "-s", "-A", UA, "--max-time", str(timeout), url],
                           capture_output=True, text=True, timeout=timeout + 6)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def http_json(url, timeout=30):
    s = http_get(url, timeout)
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------- period parsing ----------
def parse_period(key):
    """(ordinal, freq) for '2026 Apr' | '2026 1Q' | '2026' | '2026-04'."""
    key = (key or "").strip()
    m = re.match(r"^(\d{4})\s+([A-Za-z]{3})", key)
    if m and m.group(2).title() in MONTHS:
        return (int(m.group(1)) * 12 + MONTHS[m.group(2).title()], "monthly")
    m = re.match(r"^(\d{4})\s*([1-4])Q$", key) or re.match(r"^(\d{4})\s*Q([1-4])$", key)
    if m:
        return (int(m.group(1)) * 4 + int(m.group(2)), "quarterly")
    m = re.match(r"^(\d{4})-(\d{2})$", key)
    if m:
        return (int(m.group(1)) * 12 + int(m.group(2)), "monthly")
    m = re.match(r"^(\d{4})$", key)
    if m:
        return (int(m.group(1)), "annual")
    return (None, None)


def fmt_period(key):
    o, f = parse_period(key)
    if f == "monthly":
        m = re.match(r"^(\d{4})\s+([A-Za-z]{3})", key)
        if m:
            return "%s %s" % (m.group(2).title(), m.group(1))
        m = re.match(r"^(\d{4})-(\d{2})$", key)
        if m:
            return "%s %s" % (IMON[int(m.group(2))], m.group(1))
        return key
    if f == "quarterly":
        m = re.search(r"([1-4])\s*Q", key) or re.search(r"Q\s*([1-4])", key)
        return "Q%s %s" % (m.group(1) if m else "?", key[:4])
    return key


def to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s.lower() in ("", "-", "na", "n.a.", "n.a", "...", "nan"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def month_minus(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    idx = y * 12 + m - 1 - n
    return "%04d-%02d" % (idx // 12, idx % 12 + 1)


# ---------- fetchers -> sorted points [(ordinal,key,value,freq)] ----------
def pick_row(rows, match):
    if not rows:
        return None
    if not match:
        return rows[0]
    ml = match.lower()
    for r in rows:
        if (r.get("rowText") or "").strip().lower() == ml:
            return r
    for r in rows:
        if ml in (r.get("rowText") or "").strip().lower():
            return r
    return None


def fetch_singstat(ind, limit=140):
    sn = ind.get("series_no", "1")
    url = ("https://tablebuilder.singstat.gov.sg/api/table/tabledata/%s"
           "?seriesNoORrowNo=%s&sortBy=key%%20desc&limit=%d" % (ind["table"], sn, limit))
    j = http_json(url)
    if not j or j.get("StatusCode") != 200:
        return None, "fetch/status failed"
    data = j.get("Data", {})
    rows = data.get("row", [])
    row = rows[0] if rows else None
    if not row:
        return None, "series %s empty" % sn
    pts = []
    for c in row.get("columns", []):
        o, f = parse_period(c.get("key"))
        v = to_float(c.get("value"))
        if o is not None and v is not None:
            pts.append((o, c["key"], v, f))
    pts.sort()
    return {"points": pts, "matched_row": row.get("rowText"),
            "title": data.get("title"), "updated": data.get("dataLastUpdated")}, None


def _hdb_month_median(resource, ym):
    filt = urllib.parse.quote(json.dumps({"month": ym}))
    url = ("https://data.gov.sg/api/action/datastore_search?resource_id=%s"
           "&limit=8000&filters=%s" % (resource, filt))
    j = http_json(url)
    if not j or not j.get("success"):
        return None
    vals = [to_float(r.get("resale_price")) for r in j["result"]["records"]]
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def fetch_datagov_hdb(ind, state, months=14):
    res = ind["resource"]
    head = http_json("https://data.gov.sg/api/action/datastore_search?resource_id=%s"
                     "&limit=1&sort=month%%20desc" % res)
    if not head or not head.get("success") or not head["result"]["records"]:
        return None, "latest-month probe failed"
    latest = head["result"]["records"][0]["month"]
    now_ym = datetime.now(SGT).strftime("%Y-%m")
    anchor = latest
    while anchor >= now_ym:                 # exclude the current (incomplete) registration month
        anchor = month_minus(anchor, 1)
    want = [month_minus(anchor, n) for n in range(months)]
    stored = (state.get("series", {}).get(ind["key"], {}) or {}).get("points", {})
    pts_map = dict(stored)
    # always refresh the two newest months (revisions); backfill the rest if missing
    refresh = set(want[:2])
    for ym in want:
        if ym in pts_map and ym not in refresh:
            continue
        med = _hdb_month_median(res, ym)
        if med is not None:
            pts_map[ym] = med
    pts = []
    for ym, v in pts_map.items():
        o, f = parse_period(ym)
        if o is not None:
            pts.append((o, ym, float(v), "monthly"))
    pts.sort()
    pts = pts[-(months + 14):]
    return {"points": pts, "matched_row": "median resale_price", "title": "HDB resale",
            "updated": anchor}, None


# ---------- transforms ----------
def yoy_series(points):
    if not points:
        return []
    step = STEP.get(points[0][3], 12)
    by = {o: v for o, k, v, f in points}
    keyof = {o: k for o, k, v, f in points}
    out = []
    for o, k, v, f in points:
        p = by.get(o - step)
        if p not in (None, 0):
            out.append((o, k, (v / p - 1) * 100))
    return out


def last_change(points):
    """latest value minus the immediately preceding period's value."""
    if len(points) < 2:
        return None
    return points[-1][2] - points[-2][2]


# ---------- per-indicator metrics ----------
def compute(ind, info):
    pts = info["points"]
    if not pts:
        return None
    kind = ind["kind"]
    if kind in ("index", "level"):
        hs = yoy_series(pts)                 # display headline = YoY %
        if not hs:
            return None
        headline = hs[-1][2]
        period_key = hs[-1][1]
        delta = (hs[-1][2] - hs[-2][2]) if len(hs) >= 2 else None
        level = pts[-1][2]
        unit_h = "%"
    else:                                    # yoy_pct | rate -> raw value is the headline
        headline = pts[-1][2]
        period_key = pts[-1][1]
        delta = last_change([(o, k, v) for o, k, v, f in pts])
        level = None
        unit_h = ind.get("unit", "%")
    pol = ind.get("polarity", "up_good")
    improving = None
    if delta is not None and abs(delta) > 1e-9 and pol != "neutral":
        up = delta > 0
        improving = up if pol == "up_good" else (not up)
    scored = bool(ind.get("scored")) and pol != "neutral"
    return {"key": ind["key"], "label": ind["label"], "sector": ind["sector"],
            "headline": headline, "unit": unit_h, "delta": delta, "level": level,
            "improving": improving, "scored": scored, "polarity": pol,
            "weight": float(ind.get("weight", 1.0)), "period_key": period_key,
            "matched_row": info.get("matched_row"), "updated": info.get("updated"),
            "freq": pts[0][3]}


# ---------- composite ----------
def composite(metrics):
    scored = [m for m in metrics if m["scored"] and m["improving"] is not None]
    if not scored:
        return None
    improving = sum(1 for m in scored if m["improving"])
    diffusion = improving / len(scored) * 100
    num = sum(m["weight"] * (1 if m["improving"] else -1) for m in scored)
    den = sum(m["weight"] for m in scored) or 1
    score = num / den * 100
    if score > 40:
        verdict = "Expanding"
    elif score > 10:
        verdict = "Firming"
    elif score >= -10:
        verdict = "Steady"
    elif score >= -40:
        verdict = "Cooling"
    else:
        verdict = "Contracting"
    return {"diffusion": diffusion, "improving": improving, "scored": len(scored),
            "score": score, "verdict": verdict}


# ---------- report ----------
def arrow(delta):
    if delta is None or abs(delta) < 0.05:
        return "▬"          # ▬ flat
    return "▲" if delta > 0 else "▼"   # ▲ / ▼


def light(m):
    if m["improving"] is True:
        return "\U0001f7e2"      # 🟢
    if m["improving"] is False:
        return "\U0001f534"      # 🔴 worse
    if m["scored"]:
        return "\U0001f7e1"      # 🟡 scored but flat / no signal
    return "\u26aa"             # ⚪ context (not scored)


def fmt_signed(x, suffix):
    if x is None:
        return ""
    return "%+.1f%s" % (x, suffix)


def build_report(metrics, comp, errors, now):
    head = now.strftime("%d-%b-%Y")
    L = ["\U0001f1f8\U0001f1ec  **Singapore Economy Barometer** — %s" % head, ""]
    if comp:
        L.append("**%s**  ·  score %+d  ·  %d/%d indicators improving"
                 % (comp["verdict"], round(comp["score"]), comp["improving"], comp["scored"]))
    else:
        L.append("_composite unavailable (insufficient data)_")
    L.append("")
    body = ["```"]
    for m in metrics:
        val = "%+.1f%%" % m["headline"] if m["unit"] == "%" else "%.0f" % m["headline"]
        d = fmt_signed(m["delta"], "pp") if m["delta"] is not None else ""
        tag = "" if m["scored"] else " (context)"
        body.append("%-24s %8s  %s %-8s %s  %s%s"
                     % (m["label"][:24], val, arrow(m["delta"]), d, light(m),
                        fmt_period(m["period_key"]), tag))
    body.append("```")
    L += body
    if errors:
        L.append("⚠️ unavailable: " + ", ".join(errors))
    L.append("_\U0001f7e2 better \u00b7 \U0001f7e1 flat \u00b7 \U0001f534 worse \u00b7 \u26aa context (unscored) \u2014 light = is the latest move good for the economy; \u25b2/\u25bc vs previous period, pp = %-points_")
    L.append("_Sources: SingStat, data.gov.sg \u00b7 tracked monthly_")
    return "\n".join(L)


# ---------- state ----------
def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {"version": 1, "runs": [], "series": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def persist(state, ind, info, metric):
    pts = {k: v for o, k, v, f in info["points"]}
    keep = dict(sorted(pts.items(), key=lambda kv: parse_period(kv[0])[0] or 0))
    if len(keep) > 80:
        keep = dict(list(keep.items())[-80:])
    state.setdefault("series", {})[ind["key"]] = {
        "label": ind["label"], "as_of": metric["period_key"], "headline": metric["headline"],
        "updated": info.get("updated"), "points": keep}


# ---------- Discord (429-aware, hard-split <2000) ----------
def _retry_after(e):
    try:
        ra = json.loads(e.read().decode()).get("retry_after")
        if ra is not None:
            return float(ra)
    except Exception:
        pass
    try:
        return float(e.headers.get("Retry-After", 1))
    except Exception:
        return 1.0


def _split(text, limit=1900):
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) >= limit:
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return [c for c in out if c.strip()]


def post_discord(channel, content):
    import urllib.request, urllib.error
    token = gt_env_value("DISCORD_BOT_TOKEN")
    if not token:
        sys.stderr.write("no DISCORD_BOT_TOKEN; cannot post\n")
        return False
    url = "https://discord.com/api/v10/channels/%s/messages" % channel
    for chunk in _split(content):
        body = json.dumps({"content": chunk, "allowed_mentions": {"parse": []}}).encode()
        ok = False
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": "Bot %s" % token, "Content-Type": "application/json",
                "User-Agent": "DiscordBot (hermes-agent/1.0)"})
            try:
                urllib.request.urlopen(req, timeout=30)
                ok = True
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(min(_retry_after(e), 10) + 0.25); continue
                if 500 <= e.code < 600:
                    time.sleep(1.5 * (attempt + 1)); continue
                sys.stderr.write("discord post failed: HTTP %s\n" % e.code)
                return False
            except Exception as e:
                sys.stderr.write("discord error: %s\n" % e)
                time.sleep(1.5 * (attempt + 1))
        if not ok:
            return False
        time.sleep(0.4)
    return True


# ---------- main ----------
def load_registry():
    return json.load(open(REGISTRY, encoding="utf-8")).get("indicators", [])


def run(args):
    if args.list_rows:
        ind = {"table": args.list_rows, "row": None}
        info, err = fetch_singstat(ind)
        if err:
            print("ERR:", err); return 1
        print("title:", info["title"], "| updated:", info["updated"])
        for o, k, v, f in info["points"][-1:]:
            print("latest period:", k, "freq:", f)
        url = ("https://tablebuilder.singstat.gov.sg/api/table/tabledata/%s?sortBy=key%%20desc&limit=1"
               % args.list_rows)
        j = http_json(url)
        for r in j["Data"]["row"]:
            print("  row=%r uoM=%s" % (r.get("rowText"), r.get("uoM")))
        return 0

    state = load_state()
    metrics, errors = [], []
    for ind in load_registry():
        if ind.get("enabled") is False:
            continue
        src = ind["source"]
        if src == "singstat":
            info, err = fetch_singstat(ind)
        elif src == "datagov_hdb":
            info, err = fetch_datagov_hdb(ind, state)
        else:
            info, err = None, "source %s not implemented" % src
        if err or not info:
            errors.append("%s (%s)" % (ind["label"], err))
            continue
        m = compute(ind, info)
        if not m:
            errors.append("%s (no usable series)" % ind["label"])
            continue
        if args.debug:
            sys.stderr.write("[%s] row=%r updated=%s headline=%.2f delta=%s n=%d\n"
                             % (ind["key"], info.get("matched_row"), info.get("updated"),
                                m["headline"], m["delta"], len(info["points"])))
        metrics.append(m)
        persist(state, ind, info, m)

    comp = composite(metrics)
    now = datetime.now(SGT)
    report = build_report(metrics, comp, errors, now)

    state.setdefault("runs", []).append({
        "ts": now.strftime("%Y-%m-%d %H:%M"), "verdict": comp["verdict"] if comp else None,
        "score": round(comp["score"]) if comp else None,
        "diffusion": round(comp["diffusion"]) if comp else None})
    state["runs"] = state["runs"][-30:]
    if not args.no_state:
        save_state(state)

    if not args.quiet:
        print(report)
    channel = args.post or (gt_env_value("DISCORD_HOME_CHANNEL") if args.post_home else None)
    if channel:
        if not post_discord(channel, report):
            sys.stderr.write("[sg-econ] Discord delivery FAILED\n")
            if args.quiet:
                print(report)
            return 1
    return 0 if metrics else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="Singapore Economy Barometer")
    ap.add_argument("--post", metavar="CHANNEL", help="post report to a Discord channel id")
    ap.add_argument("--post-home", action="store_true", help="post to DISCORD_HOME_CHANNEL")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout")
    ap.add_argument("--no-state", action="store_true", help="do not write the state file")
    ap.add_argument("--debug", action="store_true", help="per-indicator diagnostics to stderr")
    ap.add_argument("--list-rows", metavar="TABLE", help="list a SingStat table's row labels and exit")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
