#!/usr/bin/env python3
"""Evening Weather Forecast (job c967) — no_agent replacement.

Fetches the SG 24-hour + 2-hour forecasts and prints the formatted evening
weather report to stdout. The cron system auto-delivers stdout to Discord
(deliver=discord:...), so this script does NOT call the Discord API itself.

Ported verbatim from skills/productivity/sg-commute/references/
evening-weather-workflow.md (format + emoji mapping).
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

URL_24 = "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast"
URL_2 = "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast"
SGT = timezone(timedelta(hours=8))


def _datagov_key():
    """data.gov.sg API key (x-api-key) from ~/.hermes/.env — lifts the anonymous
    ~5-req rate limit on api-open.data.gov.sg."""
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("DATAGOV_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None

_DATAGOV_KEY = _datagov_key()


def curl_json(url: str, want=None, tries: int = 4) -> dict:
    # urllib silently returns HTTP 403 on the 2hr endpoint — always use curl.
    # data.gov.sg intermittently returns empty payloads, so retry until usable.
    # Sends the data.gov.sg API key (x-api-key) when present to lift the
    # anonymous ~5-req rate limit; backs off ~10s on an explicit 429.
    d = None
    for i in range(tries):
        args = ["curl", "-s", "--max-time", "20"]
        if _DATAGOV_KEY:
            args += ["-H", "x-api-key: " + _DATAGOV_KEY]
        args += [url]
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=30)
            d = json.loads(out.stdout)
        except Exception:
            d = None
        if d is not None and (want is None or want(d)):
            return d
        if i < tries - 1:
            rate_limited = isinstance(d, dict) and (
                d.get("name") == "TOO_MANY_REQUESTS" or d.get("code") == 24)
            time.sleep(10 if rate_limited else 1.5)
    return d if d is not None else {}


def emoji_for(text: str) -> str:
    """Evening mapping, priority order (header is always the moon)."""
    t = (text or "").lower()
    if any(k in t for k in ("thunder", "lightning", "storm")):
        return "\U0001F327️"  # 🌧️
    # Heavy rain/showers → heavy glyph. MUST precede the generic "rain"/"shower"
    # branch below, which would otherwise shadow it and return the light glyph
    # (e.g. "Heavy Rain" wrongly rendering as 🌦️).
    if "heavy" in t and ("rain" in t or "shower" in t):
        return "\U0001F327️"  # 🌧️
    if "drizzle" in t:
        return "\U0001F326️"  # 🌦️
    if "light rain" in t:          # two-word phrase, checked before "rain"
        return "\U0001F326️"
    if "rain" in t or "shower" in t:
        return "\U0001F326️"
    if "cloudy" in t or "overcast" in t:
        if "partly cloudy" in t:
            return "⛅"        # ⛅
        return "☁️"      # ☁️
    if "partly cloudy" in t or "sunny spells" in t:
        return "⛅"
    # "Fair (Day)" / "Fair & Warm" describe daytime (the commute-period and 24h
    # lines are about TOMORROW's day), so they get the sun; "(Night)" keeps the
    # moon. The header is always 🌙 regardless — this only affects the lines.
    if "fair" in t or "sunny" in t:
        return "\U0001F319" if "night" in t else "☀️"
    return "\U0001F319"            # 🌙 default


def _parse_ts(iso):
    """ISO timestamp → aware datetime in SGT (a naive value is taken as SGT). None if unusable."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt.replace(tzinfo=SGT) if dt.tzinfo is None else dt.astimezone(SGT)


def hhmm(iso: str) -> str:
    # Convert to SGT before formatting: the API sends +08:00 today, but a "Z"/UTC
    # stamp would otherwise print UTC clock time under an "SGT" label.
    dt = _parse_ts(iso)
    return dt.strftime("%H%M") if dt else (iso or "?")


def fc_text(v) -> str:
    """Normalise a forecast value to its text. The v2 API returns
    general.forecast as a dict {"code":.., "text":..}; older/area shapes use a
    plain string. Never return the dict itself — emoji_for()'s `.lower()` and
    %-formatting would then crash (and dump a traceback to Discord)."""
    if isinstance(v, dict):
        return v.get("text") or ""
    return v or ""


def _period_label(tp) -> str:
    """"6 am to Midday 30 Sep" → "6am–Midday" (from the period's OWN timePeriod text)."""
    text = tp.get("text") if isinstance(tp, dict) else None
    if isinstance(text, dict):
        text = text.get("text")
    if not isinstance(text, str) or not text.strip():
        return "6am–Midday"
    lab = re.sub(r"\s+", " ", text).strip()
    lab = re.sub(r"\s+\d{1,2}\s+[A-Za-z]{3,}\b", "", lab)
    lab = re.sub(r"(\d)\s+([ap]m)\b", r"\1\2", lab, flags=re.I)
    return lab.replace(" to ", "–").strip() or "6am–Midday"


def commute_period(record, now=None):
    """The 24hr-forecast period covering the NEXT morning commute: the earliest
    period that starts at 06:00 SGT after `now`. Picked by the period's own
    timePeriod.start, never by list position (the periods slide with the clock).
    The record's `general` block spans 6pm today → 6pm tomorrow (it is dominated
    by the afternoon storms), so it must NOT be presented as the 6–8am forecast.
    Returns the period dict or None."""
    now = now or datetime.now(SGT)
    best = None
    for p in (record or {}).get("periods") or []:
        if not isinstance(p, dict):
            continue
        st = _parse_ts((p.get("timePeriod") or {}).get("start")) if isinstance(p.get("timePeriod"), dict) else None
        if st is None or st.hour != 6 or st <= now:
            continue
        if best is None or st < best[0]:
            best = (st, p)
    return best[1] if best else None


def main():
    lines = ["## \U0001F319 Evening Weather Update *(Pasir Ris)*", "",
             "Tomorrow's forecast for your commute (6–8am):"]

    # Block 1: 24-hour general forecast (degrades independently).
    n_before = len(lines)
    try:
        w24 = curl_json(URL_24, want=lambda x: ((x or {}).get("data") or {}).get("records"))
        records = (((w24 or {}).get("data") or {}).get("records")) or []
        if not records:
            raise ValueError("no 24hr records")
        # The commute-window line (06:00–12:00 East, home) — guarded on its own so
        # a shape change in `periods` can't take out the 24h lines below.
        try:
            p = commute_period(records[0])
            txt = fc_text((p.get("regions") or {}).get("east")) if p is not None else ""
            txt = txt.strip() if isinstance(txt, str) else ""
            if txt:
                lines.append("• %s (East): %s %s" % (
                    _period_label(p.get("timePeriod")), txt, emoji_for(txt)))
            else:
                # say so rather than let the 24h outlook stand in for the commute window
                lines.append("• 6am–Midday (East): forecast not available yet")
        except Exception as e:
            print("evening_weather: commute period unavailable: %s" % e, file=sys.stderr)
            lines.append("• 6am–Midday (East): forecast not available yet")
        g = records[0]["general"]
        forecast = fc_text(g.get("forecast"))
        temp = g.get("temperature", {})
        hum = g.get("relativeHumidity", {})
        wind = g.get("wind", {})
        wdir = wind.get("direction", "")
        wspd = wind.get("speed", {})
        # general.* covers 6pm today → 6pm tomorrow: label it as the 24h outlook.
        # Build both lines first so a failure half-way can't leave the outlook AND
        # "outlook unavailable" in the same report.
        outlook = ["• 24h outlook: %s %s, %s – %s °C" % (
                       forecast, emoji_for(forecast), temp.get("low"), temp.get("high")),
                   "• Wind: %s %s – %s km/h, Humidity: %s – %s %%" % (
                       wdir, wspd.get("low"), wspd.get("high"), hum.get("low"), hum.get("high"))]
        lines.extend(outlook)
    except Exception as e:
        print("evening_weather: 24hr forecast unavailable: %s" % e, file=sys.stderr)
        # if the commute-period line already printed, only the 24h part is missing
        lines.append("• 24h outlook unavailable right now." if len(lines) > n_before
                     else "• Tomorrow's forecast unavailable right now.")

    lines += ["", "Next 2 hours at home:"]

    # Block 2: 2-hour Pasir Ris area forecast (degrades independently).
    try:
        w2 = curl_json(URL_2, want=lambda x: ((x or {}).get("data") or {}).get("items"))
        items = (((w2 or {}).get("data") or {}).get("items")) or []
        if not items:
            raise ValueError("no 2hr items")
        item = items[0]
        vp = item.get("valid_period", {})
        start = hhmm(vp.get("start", ""))
        end = hhmm(vp.get("end", ""))
        forecasts = item.get("forecasts", [])
        # Pasir Ris fuzzy match: exact → startswith → first available.
        area_fc, area_note = None, ""
        for f in forecasts:
            if f.get("area") == "Pasir Ris":
                area_fc = fc_text(f.get("forecast"))
                break
        if area_fc is None:
            for f in forecasts:
                if str(f.get("area", "")).startswith("Pasir Ris"):
                    area_fc = fc_text(f.get("forecast"))
                    break
        if area_fc is None and forecasts:
            area_fc = fc_text(forecasts[0].get("forecast"))
            area_note = " *(Nearest area: %s)*" % forecasts[0].get("area", "?")
        area_fc = area_fc or "n/a"
        vend = _parse_ts(vp.get("end"))
        if vend is not None and vend < datetime.now(SGT):
            area_note += " *(stale bulletin — latest available)*"
        lines.append("• %s – %s SGT: %s %s%s" % (
            start, end, area_fc, emoji_for(area_fc), area_note))
    except Exception as e:
        print("evening_weather: 2hr forecast unavailable: %s" % e, file=sys.stderr)
        lines.append("• Next 2-hour forecast unavailable right now.")

    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Last-resort guard: a traceback on stdout would be delivered to Discord
        # verbatim by the cron system. Emit a minimal valid report instead.
        print("## \U0001F319 Evening Weather Update *(Pasir Ris)*\n\n"
              "_Weather data unavailable right now._")
        print("evening_weather fatal: %s" % e, file=sys.stderr)
