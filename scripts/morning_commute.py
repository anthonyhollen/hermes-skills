#!/usr/bin/env python3
"""Morning Commute Report (job b617) — no_agent replacement.

Computes the SG morning commute report (weather, rain, air quality, traffic-
aware route + static map) and prints it to stdout ending with a MEDIA: tag.
The cron system auto-delivers stdout + the referenced PNG to Discord
(deliver=discord:...), so this script does NOT call the Discord API itself.

Ported from skills/productivity/sg-commute/references/commute-report.md.
Each section is wrapped in try/except so one failing upstream API degrades
that section only, not the whole report.

Destination is derived from the weekday via SCHEDULE (Tue/Wed/Thu→Innovis since
2026-10-14; Wed was AMK). The AMK profile stays in DEST_PROFILES so it can be put
back on any day by editing SCHEDULE. Override for testing with argv[1] in
{innovis, amk} or env DEST.
"""
import json, math, os, re, subprocess, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ORIGIN = "Pasir Ris Grove Singapore"
INNOVIS = "2 Fusionopolis Way, Singapore 138634"
AMK = "12 Ang Mo Kio Street 64, Singapore 569088"
SGT = timezone(timedelta(hours=8))

# Destination profiles: slug → (label, address, slug, rain station, station name, dist).
# Keep AMK here even while no day uses it — its route (direct, ≥8 km floor) and
# rain station (S109) are the on-file format for restoring it to any day.
DEST_PROFILES = {
    "innovis": ("Innovis", INNOVIS, "innovis", "S71", "Kent Ridge Rd", "1.1km"),
    "amk": ("AMK", AMK, "amk", "S109", "Ang Mo Kio Ave 5", "1.3km"),
}

# day-of-week (Mon=0) → profile slug. To move AMK back to a day, set that day to
# "amk" here (e.g. 2: "amk" restores the old Wednesday). A day outside Tue–Thu
# also needs the cron schedule extended: hermes cron edit b617d89da15b.
SCHEDULE = {
    1: "innovis",
    2: "innovis",   # Wed: AMK until 2026-10-14
    3: "innovis",
}


def _env_val(name):
    """Read a config value from os.environ or, failing that, ~/.hermes/.env.
    The cron scheduler does NOT export ~/.hermes/.env into no_agent scripts, so
    we parse the file directly. Tolerates an `export ` prefix and surrounding
    quotes. Returns None if absent."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None

# DATAGOV key lifts the anonymous ~5-req rate limit so the concurrent weather
# calls don't 429. GOOGLE key is for Directions + Static Maps. Both live in
# ~/.hermes/.env — never hardcode a key (it leaks into the skill files/backups).
_DATAGOV_KEY = _env_val("DATAGOV_API_KEY")
API_KEY = _env_val("GOOGLE_API_KEY") or ""


def curl_json(url, fail=False, tries=4, want=None):
    """Fetch + parse JSON with retries. data.gov.sg endpoints intermittently
    return empty/partial payloads; retry until `want(d)` holds (or give up).
    Sends the data.gov.sg API key (x-api-key) when present to lift the anonymous
    rate limit, and backs off ~10s on an explicit 429 so parallel sections'
    retries don't immediately re-trip it. want: predicate(parsed)->truthy."""
    d = None
    for i in range(tries):
        args = ["curl", "-s", "--max-time", "20"]
        # The data.gov.sg key goes to data.gov.sg ONLY (curl_json also fetches
        # Google Directions — never hand one provider's key to another).
        if _DATAGOV_KEY and url.startswith("https://api-open.data.gov.sg/"):
            args += ["-H", "x-api-key: " + _DATAGOV_KEY]
        if fail:
            args += ["-f"]
        args += [url]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30)
            d = json.loads(out.stdout)
        except Exception:
            d = None
        if d is not None and (want is None or want(d)):
            return d
        if i < tries - 1:
            # Honour an explicit rate-limit (the API asks for ~10s) so concurrent
            # sections don't re-collide; otherwise the usual short retry wait.
            rate_limited = isinstance(d, dict) and (
                d.get("name") == "TOO_MANY_REQUESTS" or d.get("code") == 24)
            time.sleep(10 if rate_limited else 1.5)
    return d if d is not None else {}


def wx_emoji(text):
    t = (text or "").lower()
    # Keep this in sync with evening_weather.emoji_for and the SKILL.md fallback
    # rule ('"thunder"/"lightning"/"storm"→🌧️, "shower"/"rain"/"drizzle"→🌦️').
    # The two scripts reimplement this inline, so a branch added here must be
    # mirrored there (a prior drift shipped a sun glyph for drizzle/lightning).
    if "thunder" in t or "lightning" in t or "storm" in t:
        return "\U0001F327️"   # 🌧️
    if "partly cloudy" in t:
        return "⛅"
    if "cloudy" in t or "overcast" in t:
        return "☁️"
    # "Heavy …" rain/showers must beat the generic rain branch below, which
    # would otherwise return the light glyph.
    if "heavy" in t and ("rain" in t or "shower" in t):
        return "\U0001F327️"   # 🌧️
    if "shower" in t or "rain" in t or "drizzle" in t:
        return "\U0001F326️"   # 🌦️
    # Fair/sunny follows the forecast's own Day/Night suffix (the ⛅ default
    # otherwise shipped for "Fair (Night)" and "Fair (Day)" alike).
    if "fair" in t or "sunny" in t:
        return "\U0001F319" if "night" in t else "☀️"   # 🌙 / ☀️
    return "⛅"


def _fc_text(v):
    """A forecast field is a {"code","text"} dict in some data.gov.sg v2 shapes and
    a plain string in others — return the text either way. (The 24hr endpoint's
    general.forecast already migrated string→dict; the 2hr endpoint hasn't yet, so
    normalise everywhere to survive it when it does. Mirrors evening_weather.fc_text.)"""
    return (v.get("text") if isinstance(v, dict) else v) or ""


def _safe_err(e):
    """Short, secret-free reason for an "unavailable" line. Exception text can carry a
    full request URL — a Static Maps TimeoutExpired repr includes `&key=<GOOGLE_API_KEY>`
    and the scheduler delivers stdout verbatim — so never interpolate raw exception text."""
    if isinstance(e, (KeyError, IndexError, TypeError, AttributeError)):
        return "no data — upstream empty or rate-limited"
    if isinstance(e, subprocess.TimeoutExpired):
        return "timed out"
    msg = re.sub(r"https?://\S+", "<url>", str(e))
    msg = re.sub(r"(?i)(key=|x-api-key:\s*)[^&\s'\"]+", r"\1<redacted>", msg)
    return msg[:160] or type(e).__name__


def rain_emoji_label(mm_per_h):
    """Classify a rain RATE (mm/h) with the standard intensity bands. The rainfall
    API's value is a 5-MINUTE total (readingType "Rainfall 5 Minute Total"), so the
    caller converts ×12 first — the old bands were applied to the raw 5-min value,
    under-reporting intensity ~12× (1.2 mm/5 min = ~14 mm/h shipped as "light rain")."""
    if mm_per_h <= 0.0:
        return "no rain", "☁️"
    if mm_per_h < 2.5:
        return "light rain", "\U0001F326️"     # 🌦️
    if mm_per_h < 7.6:
        return "moderate rain", "\U0001F327️"  # 🌧️
    return "heavy rain", "\U0001F327️"         # 🌧️


def pm_level(v):
    if v <= 9:
        return "Good", "\U0001F7E2"      # 🟢
    if v <= 19:
        return "Moderate", "\U0001F7E1"  # 🟡
    if v <= 49:
        return "Unhealthy (Sensitive)", "\U0001F534"  # 🔴
    if v <= 99:
        return "Unhealthy", "\U0001F534"
    return "Very Unhealthy", "\U0001F534"


def _parse_ts(iso):
    """ISO timestamp → aware datetime in SGT (a naive value is taken as SGT). None if unusable."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt.replace(tzinfo=SGT) if dt.tzinfo is None else dt.astimezone(SGT)


def hhmm(iso):
    # Convert to SGT before formatting: the API sends +08:00 today, but a "Z"/UTC
    # stamp would otherwise print UTC clock time under an "SGT" label.
    dt = _parse_ts(iso)
    return dt.strftime("%H%M") if dt else (iso or "?")


def _period_label(tp):
    """Compact label for one 24hr-forecast period, derived from the API's OWN
    timePeriod so the label always matches the datum. The API's periods SLIDE with
    the clock — period[0] is the *current* 6-hour block, anchored to the
    06:00/12:00/18:00 boundaries — so a hardcoded label list is only right in the
    evening: at the 07:25 cron time the periods are [6am–Midday, Midday–6pm,
    6pm–6am], and the old fixed ["Midday–6pm","6pm–6am (tonight)","6am–Midday
    tomorrow"] mislabelled all three every morning (a "(Night)" forecast shipped
    under the daytime "6am–Midday tomorrow" label). Turns "6 am to Midday 12 Jul"
    → "6am–Midday" and "6 pm 12 Jul to 6 am 13 Jul" → "6pm–6am (tonight)".
    Returns None if the timePeriod is unusable → caller skips that line (as before)."""
    if not isinstance(tp, dict):
        return None
    text = tp.get("text")
    if isinstance(text, dict):            # tolerate a future {code,text} migration —
        text = text.get("text")           # regions.<region> + general.forecast already did
    if not isinstance(text, str):         # int/list/None → unusable, skip the line (never crash)
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    lab = re.sub(r"\s+\d{1,2}\s+[A-Za-z]{3,}\b", "", text)      # drop " 12 Jul"/"12 July" date stamps
    lab = re.sub(r"(\d)\s+([ap]m)\b", r"\1\2", lab, flags=re.I)  # "6 pm" → "6pm"
    lab = lab.replace(" to ", "–").strip()
    start, end = str(tp.get("start") or ""), str(tp.get("end") or "")
    if start[:10] and end[:10] and start[:10] != end[:10]:      # spans midnight → overnight
        lab += " (tonight)"
    return lab or None


def section1():
    try:
        d = curl_json("https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast",
                      want=lambda x: ((x or {}).get("data") or {}).get("records"))
        rec = d["data"]["records"][0]
        g = rec["general"]
        fc = _fc_text(g.get("forecast"))   # dict-or-string safe (see _fc_text)
        t, h, w = g["temperature"], g["relativeHumidity"], g["wind"]
        out = ["**1. Today's Weather** - Forecast: *%s*, Temp: *%s–%s°C*, "
               "Humidity: *%s–%s%%*, Wind: *%s %s–%s km/h*" % (
                   fc, t.get("low"), t.get("high"), h.get("low"), h.get("high"),
                   w.get("direction", ""), w.get("speed", {}).get("low"),
                   w.get("speed", {}).get("high"))]
        # Label each period from the API's OWN timePeriod (see _period_label): the
        # periods slide with the clock, so a fixed label list mislabels every
        # non-evening run. One line per period, in API order.
        for p in rec.get("periods", []):
            lab = _period_label(p.get("timePeriod"))
            if not lab:
                continue
            try:
                txt = _fc_text(p["regions"]["east"])
            except Exception:
                continue
            out.append("• %s: %s %s" % (lab, txt, wx_emoji(txt)))
        return "\n".join(out)
    except Exception as e:
        return "**1. Today's Weather** - *unavailable (%s)*" % _safe_err(e)


def section2():
    try:
        d = curl_json("https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast",
                      want=lambda x: ((x or {}).get("data") or {}).get("items"))
        item = d["data"]["items"][0]
        vp = item.get("valid_period", {})
        fcs = item.get("forecasts", [])
        # _fc_text keeps this dict-or-string safe: the 2hr endpoint returns a string
        # today, but the 24hr sibling already migrated to a dict — section1 and
        # evening_weather normalise, so section2 must too. Use `is None` (area
        # absent) vs "" (area present but blank) so a blank Pasir Ris shows "n/a"
        # rather than falling through and mislabelling a neighbour's forecast.
        def _match_fc(pred):
            return next((_fc_text(f.get("forecast")) for f in fcs if pred(f)), None)
        area_fc = _match_fc(lambda f: f.get("area") == "Pasir Ris")
        if area_fc is None:
            area_fc = _match_fc(lambda f: str(f.get("area", "")).startswith("Pasir Ris"))
        area_note = ""
        if area_fc is None and fcs:
            # Pasir Ris absent from the bulletin — fall back to the first area but
            # SAY which one, so the reader never mistakes a neighbour's nowcast for
            # home. Mirrors evening_weather's *(Nearest area: …)* disclosure.
            area_fc = _fc_text(fcs[0].get("forecast"))
            area_note = " *(nearest area: %s)*" % (fcs[0].get("area") or "?")
        area_fc = area_fc or "n/a"   # never %-format a bare None into the message
        vend = _parse_ts(vp.get("end"))
        if vend is not None and vend < datetime.now(SGT):
            # An expired bulletin (one archived 07:25 run printed 0100–0300) must
            # not read as the next 2 hours.
            area_note += " *(stale bulletin — latest available)*"
        return ("**2. Next 2 Hours Weather** *(Pasir Ris — Home)*\n"
                "• Valid period: %s – %s SGT\n"
                "• Pasir Ris: %s %s%s" % (
                    hhmm(vp.get("start", "")), hhmm(vp.get("end", "")),
                    area_fc, wx_emoji(area_fc), area_note))
    except Exception as e:
        return "**2. Next 2 Hours Weather** - *unavailable (%s)*" % _safe_err(e)


def section3(station, sname, sdist):
    # Rain check + flood alerts.
    rain_line = "**3. Rain Check near %s** - *%s - %s: No live rainfall data available*" % (
        sname, station, sname)
    try:
        d = curl_json("https://api-open.data.gov.sg/v2/real-time/api/rainfall",
                      want=lambda x: len(((x or {}).get("data") or {}).get("stations", [])) >= 60)
        stations = d["data"]["stations"]
        # bands only for a KNOWN 5-minute total ("15 Minute" must not match; a missing
        # readingType gets the raw value, no invented rate)
        five_min = bool(re.search(r"(?<!\d)5\s*-?\s*minute", str(d["data"].get("readingType") or ""), re.I))
        if len(stations) >= 60:
            readings = d["data"]["readings"][0]["data"]
            val = next((r["value"] for r in readings if r["stationId"] == station), None)
            if not (isinstance(val, (int, float)) and not isinstance(val, bool)
                    and math.isfinite(val) and val >= 0):
                val = None                 # NaN/negative/non-numeric → "no live data" line
            if val is not None and five_min:
                rate = val * 12.0          # 5-minute total → mm/h
                lab, em = rain_emoji_label(rate)
                rate_s = (" (~%d mm/h)" % round(rate)) if val > 0 else ""
                rain_line = ("**3. Rain Check near %s** - *%s - %s (%s away): "
                             "%.1fmm in last 5 min%s - %s %s*" % (
                                 sname, station, sname, sdist, val, rate_s, lab, em))
            elif val is not None:
                # readingType changed from the 5-minute total: show the raw value and
                # don't guess a rate (the intensity bands need a known window)
                rain_line = ("**3. Rain Check near %s** - *%s - %s (%s away): %.1fmm (%s)*" % (
                    sname, station, sname, sdist, val, d["data"].get("readingType") or "rainfall"))
    except Exception:
        pass
    flood_line = "• No flood alerts currently in effect"
    try:
        # `records` is a ~48-min history of 2-minute SNAPSHOTS (newest first). Only
        # the newest snapshot says what is in effect NOW, and its `readings` are the
        # active alerts — counting snapshots reported a cleared alert as current and
        # multiplied one alert ×25. want= makes a 429 (data:null) / empty body retry
        # and, if it persists, report "unavailable" instead of a false all-clear.
        # The normal payload always carries ~25 snapshots: an EMPTY list is treated as
        # a bad response (retried, then "unavailable"), not as "no alerts".
        fd = curl_json("https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts",
                       want=lambda x: bool(((x or {}).get("data") or {}).get("records"))
                       and isinstance(x["data"]["records"], list))
        records = ((fd or {}).get("data") or {}).get("records")
        snaps = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
        stamped = [(_parse_ts(r.get("datetime")), r) for r in snaps]
        # newest by timestamp; if any stamp is unusable fall back to the API's documented
        # newest-first order rather than guessing
        newest = (max(stamped, key=lambda t: t[0])[1] if stamped and all(t[0] for t in stamped)
                  else (snaps[0] if snaps else None))

        def _rd(r):
            it = r.get("item") if isinstance(r, dict) else None
            return it.get("readings") if isinstance(it, dict) else None
        # the NEWEST snapshot must be well-formed; a malformed OLDER one is just skipped
        # (it must not hide a live alert in the newest)
        if newest is None or not isinstance(_rd(newest), list):
            flood_line = "• Flood alert status unavailable"
        else:
            n = len(_rd(newest))
            older = max((len(_rd(r)) for r in snaps
                         if r is not newest and isinstance(_rd(r), list)), default=0)
            if n:
                flood_line = "• ⚠️ Flood alert in effect (%d active reading%s)" % (
                    n, "" if n == 1 else "s")
            elif older:
                # Only older snapshots carry readings. Whether a snapshot lists every
                # alert still active (state) or only newly-issued ones (event) is not
                # verified against a real alert, so don't claim either way.
                flood_line = ("• ⚠️ Flood alert reported in the last ~45 min "
                              "(not in the latest update — may have cleared)")
    except Exception:
        flood_line = "• Flood alert status unavailable"
    return rain_line + "\n" + flood_line


def section4():
    try:
        d = curl_json("https://api-open.data.gov.sg/v2/real-time/api/pm25",
                      want=lambda x: ((x or {}).get("data") or {}).get("items"))
        items = ((d or {}).get("data") or {}).get("items") or []
        if not items:
            raise ValueError("empty pm25 payload")
        r = items[0]["readings"]["pm25_one_hourly"]
        # Only numeric regions count: one null region used to TypeError the average
        # and blank the whole section even though East was fine.
        num = {k: r[k] for k in ("north", "south", "east", "west", "central")
               if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)}
        if not num:
            raise ValueError("no numeric pm25 readings")
        east = num.get("east")
        if east is not None:
            el, ee = pm_level(east)
            east_s = "*%s µg/m³ (%s %s)*" % (east, el, ee)
        else:
            east_s = "*n/a*"
        avg_r = round(sum(num.values()) / len(num))   # classify the SAME integer we display, so
        al, ae = pm_level(avg_r)                       # the band can't disagree at a boundary
        return ("**4. Air Quality** - East region: %s, "
                "Regional avg: *%d µg/m³ (%s %s)*\n"
                "• WHO PM2.5 scale: \U0001F7E2 0–9 Good / \U0001F7E1 10–19 Moderate / "
                "\U0001F534 20–49 Unhealthy (Sensitive) / 50–99 Unhealthy / 100+ Very Unhealthy"
                % (east_s, avg_r, al, ae))
    except Exception as e:
        return "**4. Air Quality** - *unavailable (%s)*" % _safe_err(e)


def traffic_color(normal_secs, traffic_secs):
    if not traffic_secs or normal_secs == 0:
        return "0x1565C0", "\U0001F535"  # blue / 🔵
    ratio = traffic_secs / normal_secs
    # Thresholds per SKILL.md (2026-05-28): green <1.10 / amber 1.10–1.35 / red ≥1.35
    if ratio < 1.10:
        return "0x00C853", "\U0001F7E2"  # green / 🟢
    if ratio < 1.35:
        return "0xFFAB00", "\U0001F7E1"  # amber / 🟡
    return "0xD50000", "\U0001F534"      # red / 🔴


def route_has_detour(legs):
    """True if the route leaves the expressway and weaves back — the tell-tale of a
    via: waypoint that snapped to a service road/ramp (e.g. the old ECP `via:` that
    forced ECP exit 13 → Marina East Dr → U-turn → back to MCE). A clean, all-
    expressway route contains no U-turn and never drives those local roads. Scans
    each step's instruction text; used to reject a forced route and fall back to the
    natural no-waypoint route so the delivered map is never the ugly loop."""
    tag = re.compile(r"<[^>]+>")
    for l in (legs or []):
        for s in l.get("steps", []):
            instr = re.sub(r"\s+", " ", tag.sub(" ", s.get("html_instructions", ""))).strip().lower()
            if "u-turn" in instr or "make a u turn" in instr:
                return True
            # Local roads the ECP↔MCE service-road detour weaves through; a route
            # that stays on the expressway never traverses these.
            if any(rd in instr for rd in ("marina east dr", "marina e dr",
                                          "east coast park service")):
                return True
    return False


def section5(label, address, slug):
    try:
        is_innovis = address == INNOVIS
        # Innovis: keep the ECP → MCE corridor (the user's preferred drive). Google
        # won't choose it unaided (KPE/PIE comes out a few km shorter), so we pin via
        # points to the ECP and MCE *mainline*. A single via near the ECP↔MCE
        # interchange snaps to the parallel East Coast Park Service Rd, making Google
        # exit + U-turn to reach it (the old `via:1.292,103.885` U-turn bug). TWO
        # points that bracket the interchange — one on ECP westbound east of it, one
        # on MCE west of it — hold the route on the expressway.
        #
        # Coordinates VERIFIED on the Pi 2026-07-01 (verify_ecp_route.py, derived from
        # the legacy route's own ECP/MCE step geometry — so they sit on the mainline,
        # not the service road): this pair holds the route on ECP until exit 14B for
        # MCE — 34.89 km, no detour (vs 36.59 km + U-turn for the old single via). If a
        # future Google reroute ever makes the forced route weave (route_has_detour) or
        # come back short, the block below auto-falls back to the clean no-waypoint
        # route, so the delivered map is NEVER the U-turn loop.
        INNOVIS_VIA = "via:1.30860,103.93347|via:1.27616,103.86845"

        def fetch_route(waypoint):
            url = ("https://maps.googleapis.com/maps/api/directions/json"
                   "?origin=%s&destination=%s%s&mode=driving"
                   "&departure_time=now&traffic_model=best_guess&key=%s" % (
                       urllib.parse.quote(ORIGIN, safe=""),
                       urllib.parse.quote(address, safe=""),
                       ("&waypoints=%s" % waypoint) if waypoint else "", API_KEY))
            # want= retries a transient non-OK status (OVER_QUERY_LIMIT/UNKNOWN_ERROR);
            # a persistent one raises with Google's own status instead of an opaque
            # "list index out of range".
            # retry only a transient status / no response; a definitive one (OK,
            # REQUEST_DENIED, ZERO_RESULTS, …) is final — retrying it just bills 4×
            data = curl_json(url, want=lambda x: (x or {}).get("status") not in (
                None, "OVER_QUERY_LIMIT", "UNKNOWN_ERROR"))
            if (data or {}).get("status") != "OK":
                raise ValueError("Directions %s" % ((data or {}).get("status") or "no response"))
            if not data.get("routes"):
                raise ValueError("Directions returned no routes")
            route = data["routes"][0]
            legs = route["legs"]
            dist_km = sum(l["distance"]["value"] for l in legs) / 1000
            normal_secs = sum(l["duration"]["value"] for l in legs)
            tvals = [l.get("duration_in_traffic", {}).get("value") for l in legs]
            traffic_secs = sum(tvals) if all(v is not None for v in tvals) else None
            polyline = route["overview_polyline"]["points"]
            return dist_km, normal_secs, traffic_secs, polyline, legs

        # Floor 25 km catches a truncated/wrong Innovis route (ECP corridor ~33 km,
        # no-waypoint fallback ~30 km); AMK 8 km.
        min_dist = 25 if is_innovis else 8
        route_note = ""

        if is_innovis:
            # Prefer the ECP→MCE corridor, but never ship a weaving route silently.
            # (1) try the forced ECP route; (2) if it detours / is short / errors,
            # fall back to the natural no-waypoint route; (3) if the fallback is also
            # unavailable, ship the best we have but ALWAYS flag it — a U-turn
            # polyline must never be delivered without a caveat.
            forced, why = None, []
            try:
                forced = fetch_route(INNOVIS_VIA)
            except Exception as e:
                forced = None
                why.append(_safe_err(e))
            if forced and not route_has_detour(forced[4]) and forced[0] >= min_dist:
                dist_km, normal_secs, traffic_secs, polyline, legs = forced
            else:
                fb = None
                try:
                    fb = fetch_route(None)
                except Exception as e:
                    fb = None
                    why.append(_safe_err(e))
                if fb is not None:
                    dist_km, normal_secs, traffic_secs, polyline, legs = fb
                    route_note = " *(ECP route unavailable — showing fastest route)*"
                elif forced is not None:
                    dist_km, normal_secs, traffic_secs, polyline, legs = forced
                    route_note = " *(route may be inaccurate — verify manually)*"
                else:
                    raise ValueError("no route available%s" % (   # both fetches failed
                        (" — " + "; ".join(dict.fromkeys(why))) if why else ""))
        else:
            dist_km, normal_secs, traffic_secs, polyline, legs = fetch_route(None)

        # Final safety: flag the SHIPPED route if it's implausibly short, or (Innovis
        # only — AMK's natural route may legitimately contain a U-turn) if it still
        # weaves. Append regardless of any ECP-fallback note already set, so a
        # weaving/short *fallback* is never delivered as though it were fine.
        flag_bad = dist_km < min_dist or (is_innovis and route_has_detour(legs))
        if flag_bad and "inaccurate" not in route_note:
            route_note += " *(route may be inaccurate — verify manually)*"

        color, t_emoji = traffic_color(normal_secs, traffic_secs)

        # Static map → /tmp/route_<slug>.png
        map_url = ("https://maps.googleapis.com/maps/api/staticmap"
                   "?size=640x450&scale=2&format=png32"
                   "&markers=color:green%%7Clabel:H%%7C%s"
                   "&markers=color:red%%7Clabel:W%%7C%s"
                   "&path=color:%s%%7Cweight:7%%7Cenc:%s&key=%s" % (
                       urllib.parse.quote(ORIGIN, safe=""),
                       urllib.parse.quote(address, safe=""),
                       color, urllib.parse.quote(polyline, safe=""), API_KEY))
        png = "/tmp/route_%s.png" % slug
        # -f makes curl fail (non-zero) on HTTP >=400 instead of saving the error
        # body. But Static Maps also returns an 88-byte text error with exit 0 on
        # some failures, and the cron media filter does NOT magic-byte-check — so
        # validate the PNG signature here, else a broken "image" ships to Discord.
        map_ok = False
        try:
            # own guard: a map failure (incl. a TimeoutExpired whose repr holds the
            # key-bearing URL) must cost only the image, never the computed route
            mr = subprocess.run(["curl", "-f", "-s", "--max-time", "20", "-o", png, map_url],
                                timeout=60)
            map_ok = mr.returncode == 0   # curl 28 mid-download leaves a truncated file
        except Exception as e:
            print("morning_commute: static map fetch failed: %s" % _safe_err(e), file=sys.stderr)

        cur_line = ("• Current: ~%d min (traffic-aware) %s" % (
            round(traffic_secs / 60), t_emoji)) if traffic_secs else \
            "• Current: ~%d min *(traffic data unavailable)*" % round(normal_secs / 60)
        body = ("**5. Commute to %s** *(%s)*\n"
                "• Distance: %.1f km%s\n"
                "• Normal: ~%d min\n%s\n\n"
                "*(Map polyline: \U0001F7E2 light / \U0001F7E1 moderate / "
                "\U0001F534 heavy / \U0001F535 no data)*" % (
                    label, address, dist_km, route_note,
                    round(normal_secs / 60), cur_line))
        has_png = False
        if map_ok and os.path.exists(png) and os.path.getsize(png) > 0:
            try:
                with open(png, "rb") as fh:
                    data_png = fh.read()
                # signature AND the IEND trailer — a truncated download has the first, not the second
                has_png = (data_png[:8] == b"\x89PNG\r\n\x1a\n"
                           and data_png.rstrip(b"\x00")[-8:] == b"IEND\xaeB`\x82")
            except OSError:
                has_png = False
        return body, (png if has_png else None)
    except Exception as e:
        return "**5. Commute to %s** - *route unavailable (%s)*" % (label, _safe_err(e)), None


def main():
    override = (sys.argv[1].lower() if len(sys.argv) > 1 else
                os.environ.get("DEST", "")).strip().lower()
    if override in DEST_PROFILES:
        meta = DEST_PROFILES[override]
    else:
        meta = DEST_PROFILES.get(SCHEDULE.get(datetime.now().weekday()))
    if not meta:
        # Not a commute day and no override — stay silent (no delivery).
        return
    label, address, slug, station, sname, sdist = meta

    # Cleanup stale artifacts.
    subprocess.run("rm -f /tmp/route_*.json /tmp/route_innovis.png "
                   "/tmp/route_amk.png /tmp/route_map.png /tmp/rainfall.json",
                   shell=True, timeout=10)

    # All five sections are independent network fetches, and curl_json retries
    # flaky endpoints with 1.5s sleeps — run them concurrently so total
    # wall-clock is the slowest section, not the sum (a single empty payload
    # could otherwise stall the whole report on serial retries). Each section
    # has its own try/except returning a string, so threading is safe.
    with ThreadPoolExecutor(max_workers=5) as ex:
        f1 = ex.submit(section1)
        f2 = ex.submit(section2)
        f3 = ex.submit(section3, station, sname, sdist)
        f4 = ex.submit(section4)
        f5 = ex.submit(section5, label, address, slug)
        s1, s2, s3, s4 = f1.result(), f2.result(), f3.result(), f4.result()
        s5, png = f5.result()
    parts = ["## ☀️ Morning Commute Update", "", s1, "",
             s2, "", s3, "",
             s4, "", s5]
    # The (Map polyline …) legend at the end of s5 stays the last text row;
    # the MEDIA tag (stripped from the delivered text, image attached) follows.
    if png:
        parts.append("MEDIA:%s" % png)
    print("\n".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Last-resort guard: an uncaught exception would exit non-zero and the
        # cron would deliver the traceback as a "script failed" alert. Emit a
        # minimal valid report instead (exit 0). Mirrors evening_weather.py.
        print("## ☀️ Morning Commute Update\n\n"
              "_Commute report unavailable right now._")
        print("morning_commute fatal: %s" % e, file=sys.stderr)
