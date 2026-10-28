#!/usr/bin/env python3
"""
Scrape the current biddable CAR plate prefixes from LTA's OneMotoring VRN page.
Uses ScraperAPI's rendering engine (render=true) to render the Angular SPA, then
extracts the prefix tiles for the "Car" vehicle type.

Output: a Discord-formatted message printed to stdout (the cron scheduler
delivers stdout verbatim for this no_agent job).

Exit codes (these matter — the scheduler turns a non-zero exit into a
"⚠️ Cron job failed" alert):
  0  -> normal run: prefixes found (message printed) OR a benign no-bid week
        (friendly "nothing open this week" message printed)
  1  -> a genuine failure worth alerting: scrape error, or the page structure
        was not found (possible block / empty body / LTA page change)
"""
import os
import sys
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

URL = "https://onemotoring.lta.gov.sg/content/onemotoring/home/buying/upfront-vehicle-costs/vehicle-registration-number--vrn-.html#step1"
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"
SGT = ZoneInfo("Asia/Singapore")

# A prefix tile is rendered as:
#   <a href="javascript:void(0);" id="SPK" class="btn btn-yellow">SPK</a>
# The vehicle-type SELECTOR tiles share the same class but have a numeric id and
# a word label (id="1">Car). We therefore identify a prefix tile by:
#   * class attribute contains "btn-yellow"  (order/quote tolerant)
#   * the element's id EQUALS its visible text  (rejects selector + FAQ/PDF tiles)
#   * the text looks like a plate prefix: 1-4 uppercase letters
# Button presence is authoritative — we do NOT veto a section on incidental
# "error-text"/"not available" copy, which leaks in from sibling divs and the
# page's trailing JS (that veto silently dropped real prefixes; see CHANGELOG).
# Vehicle-type sections: tolerate any attributes between the id and the closing
# ">" (e.g. id="vehicleType_1" class="tab"). The sanity check and the split MUST
# use the same start pattern, or a markup change could pass the check yet split
# into 0 sections — silently reporting a real bidding week as "no-bid".
_SECTION_START = r'id="vehicleType_\d+"[^>]*>'            # non-capturing: search + lookahead
_SECTION_SPLIT = (r'id="vehicleType_(\d+)"[^>]*>(.*?)'   # capture (vid, section_html)
                  r'(?=' + _SECTION_START + r'|$)')
_ANCHOR = re.compile(r'<a\b([^>]*)>\s*([^<]*?)\s*</a>', re.DOTALL)


def _load_env_key(name):
    """Load a key from the environment, falling back to ~/.hermes/.env.

    The cron scheduler does not reliably export ~/.hermes/.env vars to no_agent
    subprocesses, so the file fallback is required for the scheduled run."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _type_name_map(body):
    """Map vehicle-type id -> label from the selector tiles (id is numeric)."""
    names = {}
    for m in _ANCHOR.finditer(body):
        attrs, text = m.group(1), m.group(2).strip()
        if "btn-yellow" not in attrs:
            continue
        idm = re.search(r'\bid=["\'](\d+)["\']', attrs)  # numeric id -> selector
        if idm:
            names[idm.group(1)] = text
    return names


def _car_vid(names):
    """Resolve the vehicleType id for "Car" from the selector type-map.

    Raises if "Car" can't be positively identified — we must never silently
    guess an id, because extracting the wrong section would deliver another
    vehicle's prefixes (motorcycle/bus) mislabeled as car plates with exit 0
    (no alert). An unresolvable map means the page changed → alert instead."""
    for vid, nm in names.items():
        if nm.strip().lower() == "car":
            return vid
    raise RuntimeError(
        "Could not identify the 'Car' vehicle type on the LTA page "
        "(selector tiles changed?) — refusing to guess"
    )


def _prefix_tiles(section_html):
    """Extract plate-prefix tiles (id == text, 1-4 uppercase letters) from a
    vehicle-type section, ignoring everything else."""
    out = []
    for m in _ANCHOR.finditer(section_html):
        attrs, text = m.group(1), m.group(2).strip()
        if "btn-yellow" not in attrs:
            continue
        idm = re.search(r'\bid=["\']([^"\']*)["\']', attrs)
        el_id = idm.group(1) if idm else ""
        if el_id and el_id == text and re.fullmatch(r"[A-Z]{1,4}", text):
            out.append(text)
    return out


def _scraperapi_get(api_key, timeout):
    """GET the LTA page through ScraperAPI with JS rendering. Returns the rendered
    HTML body. Raises urllib.error.HTTPError on a non-2xx ScraperAPI response.

    ScraperAPI params MUST precede the target url (per the docs) so they don't
    collide with the target's own query string. ScraperAPI handles proxies,
    retries, and bot protection internally; render=true runs a real browser so
    the Angular SPA's prefix tiles are present in the returned HTML."""
    params = urllib.parse.urlencode([
        ("api_key", api_key),
        ("render", "true"),
        ("url", URL),
    ])
    req = urllib.request.Request(
        SCRAPERAPI_ENDPOINT + "?" + params,
        headers={"User-Agent": "hermes-sg-car-plate-range/1.2"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def scrape_car_prefixes():
    api_key = _load_env_key("SCRAPERAPI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "SCRAPERAPI_API_KEY not set (checked env and ~/.hermes/.env)"
        )
    # A render=true fetch takes ~40s; bound each attempt at 100s so a single
    # retry stays under the cron scheduler's 120s script-timeout watchdog. This
    # is a weekly job — a single transient blip otherwise means no data (and a
    # failure alert) for 7 days — so retry once on a transient 429/5xx/transport
    # error before giving up. A 4xx (other than 429) is a definite failure and is
    # surfaced immediately (no wasted retry).
    body = None
    for attempt in range(2):
        try:
            body = _scraperapi_get(api_key, timeout=100)
            break
        except urllib.error.HTTPError as e:
            transient = e.code == 429 or 500 <= e.code < 600
            if attempt == 0 and transient:
                time.sleep(3)
                continue
            raise RuntimeError(f"Scrape failed: HTTP {e.code}")
        except Exception:
            # Transport-level failure (timeout, connection reset) — retry once.
            if attempt == 0:
                time.sleep(3)
                continue
            raise

    # Page-structure sanity check: distinguish a real "no prefixes" state from a
    # block page / empty body / LTA markup change (which must alert, not be
    # reported as a quiet no-bid week). Uses the same tolerant section pattern
    # as the split below so the two can't disagree.
    if not re.search(_SECTION_START, body):
        raise RuntimeError(
            "LTA page structure not found (0 vehicleType sections) — "
            "possible block, empty body, or page change"
        )

    names = _type_name_map(body)
    car_vid = _car_vid(names)
    sections = re.findall(_SECTION_SPLIT, body, re.DOTALL)

    car_prefixes = []
    for vid, section in sections:
        if vid != car_vid:
            continue
        car_prefixes.extend(_prefix_tiles(section))
    return sorted(set(car_prefixes))


def format_discord_message(prefixes):
    today = datetime.now(SGT).strftime("%d-%b-%Y")
    lines = [f"## 🚗 Car Plate Bidding Check - {today}"]
    lines.append(
        f"### Available car plate prefix for [bidding](<{URL}>):"
    )
    for i, p in enumerate(prefixes, 1):
        lines.append(f"{i}. **{p}**")
    return "\n".join(lines)


def format_no_bid_message():
    today = datetime.now(SGT).strftime("%d-%b-%Y")
    return (
        f"## 🚗 Car Plate Bidding Check - {today}\n"
        f"No car plate prefixes are open for bidding this week."
    )


if __name__ == "__main__":
    try:
        prefixes = scrape_car_prefixes()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # A benign no-bid week is NOT a failure: deliver a friendly message and
    # exit 0 so the scheduler does not raise a "cron job failed" alert.
    if not prefixes:
        print(format_no_bid_message())
        sys.exit(0)

    print(format_discord_message(prefixes))
