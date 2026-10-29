#!/usr/bin/env python3
import sys, time as _time
_T0 = _time.monotonic()   # script start — the --send deadline is measured from here
sys.path.insert(0, '/home/amh/.local/lib/python3.11/site-packages')

import urllib.request, json, csv, math, urllib.parse, re, os, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
import requests, threading
from pyproj import Transformer


def _load_env_key(name):
    """Load a key from os.environ or, failing that, ~/.hermes/.env. Tolerates an
    `export ` prefix and surrounding quotes. Returns "" if absent — callers check
    and fail with a clear message rather than sending an empty key to the API.
    (Never hardcode a key in the script: it leaks into the skill files/backups.)"""
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


USAGE = """usage: nearest_carpark.py "<postal code or address>" [--send [<channel_id>] [--thread <thread_id>]] [--json]
       nearest_carpark.py --refresh          (monthly EV-cache refresh — the system crontab entry)

  (no flag)   print the report (text digest + image paths) to the terminal
  --send      post the report to Discord yourself (curl multipart, header+image pairs). With NO id it
              posts into the conversation this was run from (resolved from the Hermes session:
              HERMES_SESSION_KEY → the thread/channel id); with an id it posts there (--thread wins).
              Refuses to guess: exits 2 if it can't tell where to post. Exits 1 if NOTHING was
              posted; a partial post exits 0 with "PARTIAL DELIVERY … do NOT re-run".
  --json      machine-readable manifest on stdout (progress goes to stderr)
"""


def _norm_id(t):
    """Accept an id as 123…, <#123…> or discord:123… (the forms an agent copies)."""
    t = str(t).strip()
    if t.startswith("<#") and t.endswith(">"):
        t = t[2:-1]
    if t.lower().startswith("discord:"):
        t = t.split(":")[-1]
    return t


def _looks_like_id(t):
    """True for a token meant as a Discord id: <#…>, discord:…, or ≥7 digits (longer than a
    6-digit postal or a block/house number, shorter than a real 17–20-digit snowflake)."""
    t = str(t).strip()
    return (t.startswith("<#") or t.startswith("<@") or t.lower().startswith("discord:")
            or (t.isascii() and t.isdigit() and len(t) >= 7))


def _parse_cli(argv):
    """Parse argv into a dict. Flags are never mistaken for an address (the old
    argv[1]-is-the-address rule geocoded "--help"). A Discord id is a 15+-digit
    snowflake, so a 6-digit postal after --send is never taken as a channel."""
    o = {"address": None, "send": False, "channel": None, "thread": None,
         "json": False, "refresh": False}
    pos, i = [], 0
    snow = lambda t: t.isascii() and t.isdigit() and len(t) >= 15
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE); sys.exit(0)
        elif a == "--json":
            o["json"] = True
        elif a == "--refresh":
            o["refresh"] = True
        elif a == "--send":
            o["send"] = True
            nxt = _norm_id(argv[i + 1]) if i + 1 < len(argv) else None
            if nxt and snow(nxt):
                o["channel"] = nxt; i += 1
            elif nxt and _looks_like_id(argv[i + 1]):
                # an id-shaped token that isn't a valid snowflake must fail loudly, not
                # silently become part of the address and post to the session instead
                print(f"ERROR: '{argv[i + 1]}' is not a Discord channel id\n" + USAGE, file=sys.stderr)
                sys.exit(2)
        elif a == "--thread":
            nxt = _norm_id(argv[i + 1]) if i + 1 < len(argv) else None
            if nxt and snow(nxt):
                o["thread"] = nxt; i += 1
            else:
                print("ERROR: --thread requires a Discord thread id\n" + USAGE, file=sys.stderr); sys.exit(2)
        elif re.match(r"^--?[A-Za-z]", a):
            print(f"ERROR: unknown option {a}\n" + USAGE, file=sys.stderr); sys.exit(2)
        elif _looks_like_id(a):
            # a channel id anywhere else in argv would silently become part of the address
            # (and the report would go to the current conversation instead)
            print(f"ERROR: '{a}' looks like a Discord id — put it right after --send "
                  f"(or --thread)\n" + USAGE, file=sys.stderr); sys.exit(2)
        elif a.strip():
            pos.append(a.strip())
        i += 1
    if o["thread"] and not o["send"]:
        print("ERROR: --thread only makes sense with --send\n" + USAGE, file=sys.stderr); sys.exit(2)
    if pos:
        o["address"] = " ".join(pos)   # tolerate an unquoted multi-word address
    elif not o["refresh"]:
        print("ERROR: give a postal code or address\n" + USAGE, file=sys.stderr); sys.exit(2)
    return o


CLI = _parse_cli(sys.argv[1:])
EMIT_JSON = CLI["json"]
_JSON_OUT = sys.stdout
if EMIT_JSON:
    # --json: stdout must be ONLY the manifest (the progress lines used to precede it and
    # made the output unparseable), so route every other print to stderr.
    sys.stdout = sys.stderr

SKILL      = "/home/amh/.hermes/skills/productivity/sg-commute"
DATA       = "/home/amh/.hermes/skills/productivity/sg-commute/data"
ONEMAP_URL = "https://www.onemap.gov.sg/api/common/elastic/search?searchVal={q}&returnGeom=Y&getAddrDetails=Y&pageNum=1"
HDB_URL    = "https://api.data.gov.sg/v1/transport/carpark-availability"
LTA_URL    = "https://datamall2.mytransport.sg/ltaodataservice/CarParkAvailabilityv2"
LTA_KEY    = _load_env_key("LTA_DATAMALL_KEY")
EV_URL     = "https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode={p}"
STATIC_KEY = _load_env_key("GOOGLE_API_KEY")
GEO_URL    = ("https://maps.googleapis.com/maps/api/geocode/json?address={q}"
              "&components=country:SG&region=sg&key=" + STATIC_KEY)
# Singapore's bounding box (with margin): a geocode outside it is a wrong match
# (an unrestricted Google lookup of "HG22" resolved to Harrogate, UK).
SG_BBOX = (1.13, 1.48, 103.58, 104.10)   # lat_min, lat_max, lon_min, lon_max

def _in_sg(lat, lon):
    return (lat is not None and lon is not None
            and SG_BBOX[0] <= lat <= SG_BBOX[1] and SG_BBOX[2] <= lon <= SG_BBOX[3])
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
if not STATIC_KEY:
    print("WARNING: GOOGLE_API_KEY missing from env/~/.hermes/.env — geocoding/maps will fail", file=sys.stderr)
if not LTA_KEY:
    print("WARNING: LTA_DATAMALL_KEY missing from env/~/.hermes/.env — LTA/EV data will be empty", file=sys.stderr)

# ── HTTP helper with curl (avoids urllib 403s on data.gov.sg / LTA) ─────────
import subprocess

def fetch(url, headers=None, max_retries=3, timeout=15):
    """Fetch a URL via curl with retry. Pass headers as curl -H flags.

    Always either returns parsed JSON or RAISES on exhausted retries — it must
    never fall through to an implicit None. A curl non-zero exit (timeout, DNS,
    connection reset) does NOT raise a Python exception, so without this the
    function would return None and callers doing `result.get(...)` would crash
    with AttributeError outside their try/except. Raising lets their try/except
    fire and return their documented empty value.
    """
    cmd = ["curl", "-s", "--max-time", str(timeout), url]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    last_err = None
    for attempt in range(max_retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if r.returncode == 0:
                return json.loads(r.stdout)
            last_err = RuntimeError(
                f"curl exit {r.returncode}: "
                f"{r.stderr.decode('utf-8', 'replace')[:200]}")
        except Exception as e:
            last_err = e
        if attempt < max_retries - 1:
            time.sleep(1 * (attempt + 1))
    raise last_err if last_err is not None else RuntimeError(f"curl failed: {url}")

# ── SVY21 → WGS84 (EPSG:3414 → EPSG:4326) ─────────────────────────────────────
_t_fwd = Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
_t_inv = Transformer.from_crs("EPSG:4326","EPSG:3414",always_xy=True)
def svy21_to_wgs84(x, y):
    """Convert SVY21 (Easting, Northing) to WGS84 (lat, lon)."""
    lon, lat = _t_fwd.transform(x, y)
    return lat, lon
def wgs84_to_svy21(lat, lon):
    """Convert WGS84 (lat, lon) to SVY21 (Easting, Northing)."""
    x, y = _t_inv.transform(lon, lat)
    return x, y

# Verify
test = svy21_to_wgs84(23034.1, 31198.9)
assert abs(test[0] - 1.298426) < 0.001 and abs(test[1] - 103.7887) < 0.001, f"SVY21 transform broken: {test}"

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def euclidean_svy21(x1, y1, x2, y2):
    return math.sqrt((x2-x1)**2 + (y2-y1)**2)

def extract_postal(addr):
    m = re.search(r'\b(\d{6})\b', addr)
    return m.group(1) if m else None

def geocode(addr):
    """Geocode an address to lat/lon + SVY21.
    - 6-digit postal input → OneMap FIRST: it's the authoritative SG geocoder and
      returns the building-level ADDRESS (e.g. "27 PASIR RIS GROVE COCO PALMS
      SINGAPORE 518073") plus native SVY21 X/Y. Google is the fallback.
    - Street address (no extractable postal) → Google Geocoding.
    Returns None on total failure.
    (OneMap-first for postals also stops Google's terse "Singapore <postal>"
    formatted_address from winning over OneMap's building name, and saves a call.)"""
    postal = extract_postal(addr)

    # 1. Postal input → OneMap first (building-level address + authoritative coords)
    if postal:
        try:
            url = ONEMAP_URL.format(q=urllib.parse.quote(postal))
            d = fetch(url)
            if d.get("found", 0) > 0:
                row = d["results"][0]
                if _in_sg(float(row["LATITUDE"]), float(row["LONGITUDE"])):
                    return {"lat": float(row["LATITUDE"]), "lon": float(row["LONGITUDE"]),
                            "svy21_x": float(row["X"]), "svy21_y": float(row["Y"]),
                            "address": row["ADDRESS"], "postal": postal}
        except Exception as e:
            print(f"WARNING: OneMap geocode failed for '{addr}': {e}", file=sys.stderr)

    # 2. Google Geocoding (handles arbitrary street addresses; fallback for postals)
    lat = lon = x = y = None
    formatted = addr
    try:
        url = GEO_URL.format(q=urllib.parse.quote(addr))
        d = fetch(url)
        if d.get("status") == "OK" and d.get("results"):
            loc = d["results"][0]["geometry"]["location"]
            lat, lon = float(loc["lat"]), float(loc["lng"])
            # With components=country:SG an unmatched query comes back as "Singapore"
            # itself (types: country) at the island centroid — inside the bbox, but not
            # a place. Reject country/admin-level results rather than report on it.
            gtypes = set(d["results"][0].get("types") or [])
            if gtypes & {"country", "administrative_area_level_1", "administrative_area_level_2",
                         "colloquial_area"}:
                print(f"WARNING: geocode for '{addr}' matched only '{d['results'][0].get('formatted_address', '?')}' "
                      f"({', '.join(sorted(gtypes))}) — rejected", file=sys.stderr)
                return None
            if not _in_sg(lat, lon):
                print(f"WARNING: geocode for '{addr}' resolved outside Singapore "
                      f"({lat:.4f},{lon:.4f}) — rejected", file=sys.stderr)
                return None
            x, y = wgs84_to_svy21(lat, lon)
            formatted = d["results"][0].get("formatted_address", addr)
            gpostal = ""
            for comp in d["results"][0].get("address_components", []):
                if "postal_code" in comp.get("types", []):
                    gpostal = comp.get("long_name", "")
                    break
            return {"lat": lat, "lon": lon, "svy21_x": x, "svy21_y": y,
                    "address": formatted, "postal": gpostal or postal or ""}
    except Exception as e:
        print(f"WARNING: Google geocode failed for '{addr}': {e}", file=sys.stderr)

    # 3. Last resort: return whatever Google produced (may lack a postal)
    if lat is not None:
        return {"lat": lat, "lon": lon, "svy21_x": x, "svy21_y": y,
                "address": formatted, "postal": postal or ""}
    return None

HDB_STALE_H = 48  # hours; update_datetime only moves when the count changes, so be generous

def _hdb_age_h(ts):
    """Hours since an HDB `update_datetime` ("2026-09-29T21:30:47", SGT, naive). None if unparseable."""
    try:
        from datetime import datetime, timedelta, timezone
        sgt = timezone(timedelta(hours=8))
        dt = datetime.fromisoformat(str(ts)).replace(tzinfo=sgt)
        return (datetime.now(sgt) - dt).total_seconds() / 3600
    except Exception:
        return None

def hdb_live():
    """Fetch HDB live carpark availability → (dict, ok). ok=False when the feed
    failed or returned no `items` (a 429/401 body parses as JSON too) — the caller
    must then say the data is unavailable, not that there is no parking."""
    try:
        d = fetch(HDB_URL)
    except Exception as e:
        print(f"WARNING: HDB API failed ({e}), live data unavailable", file=sys.stderr)
        return {}, False
    if not isinstance(d, dict) or not isinstance(d.get("items"), list) or not d["items"]:
        print(f"WARNING: HDB API returned no items ({str(d)[:120]}), live data unavailable",
              file=sys.stderr)
        return {}, False
    out = {}
    for item in d.get("items", []):
        for cp in item.get("carpark_data", []):
            # Guard per item: a malformed HTTP-200 row (missing carpark_number,
            # non-numeric lots) must be skipped, not crash the whole report — the
            # try/except above only covers the fetch, and this script has no
            # top-level guard, so an unguarded KeyError/ValueError here kills it.
            try:
                # Only car lots (C) — skip heavy (H) and motorcycle (Y). The C entry is
                # NOT always first (15 carparks list ['H','Y','C']); reading only [0]
                # silently dropped them.
                info = next((i for i in (cp.get("carpark_info") or [])
                             if isinstance(i, dict) and i.get("lot_type") == "C"), None)
                if info is None:
                    continue
                num = cp.get("carpark_number")
                if not num:
                    continue
                age = _hdb_age_h(cp.get("update_datetime"))
                out[num] = {
                    "lots": int(info.get("lots_available") or 0),
                    "total": int(info.get("total_lots") or 0),
                    # frozen sensors (some months old) must not be shown as live
                    "fresh": age is not None and age <= HDB_STALE_H,
                }
            except (KeyError, ValueError, TypeError):
                continue
    return out, bool(out)   # a feed that parsed to zero car carparks is not "healthy"

LTA_PAGE = 500     # DataMall returns at most 500 records per call — page with $skip
LTA_MAX_PAGES = 12

def _lta_pages():
    """All CarParkAvailabilityv2 rows (the feed is ~2,600 rows / 6 pages; one call
    only ever saw the first 500). Pages are fetched concurrently in batches.
    Returns (rows, ok) — ok=False if page 0 failed."""
    hdrs = {"AccountKey": LTA_KEY, "Accept": "application/json"}
    rows, start = [], 0
    while start < LTA_MAX_PAGES:
        skips = [(start + k) * LTA_PAGE for k in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            res = list(pool.map(lambda sk: _safe_fetch(f"{LTA_URL}?$skip={sk}", hdrs), skips))
        for k, d in enumerate(res):
            v = d.get("value") if isinstance(d, dict) else None
            later = [r.get("value") for r in res[k + 1:] if isinstance(r, dict)]
            if isinstance(v, list) and len(v) < LTA_PAGE and any(isinstance(x, list) and x for x in later):
                # a short/empty page with data AFTER it = a glitch, not the end of the feed
                print(f"WARNING: LTA page $skip={skips[k]} came back short — list may be incomplete",
                      file=sys.stderr)
                rows += v
                for x in later:
                    if isinstance(x, list):
                        rows += x
                return rows, None
            if not isinstance(v, list) or (start + k == 0 and not v):
                if start + k == 0:
                    print(f"WARNING: LTA API failed ({str(d)[:120]}), live data unavailable",
                          file=sys.stderr)
                    return [], False
                print(f"WARNING: LTA page $skip={skips[k]} failed — later rows may be missing",
                      file=sys.stderr)
                return rows, None      # None = partial: rows usable, but say the list may be incomplete
            rows += v
            if len(v) < LTA_PAGE:
                return rows, True
        start += 6
    return rows, True

def _safe_fetch(url, headers):
    try:
        return fetch(url, headers=headers)
    except Exception as e:
        return {"error": str(e)}

def lta_live():
    """Fetch LTA live carpark availability (all pages) → (dict, ok)."""
    rows, ok = _lta_pages()
    if ok is False:
        return {}, False
    out = {}
    for r2 in rows:
        # Guard per item (see hdb_live): skip a malformed row rather than crash.
        try:
            lot_type = r2.get("LotType", "")
            # Only count C (car) lots — skip H (heavy vehicle) and Y (motorcycle)
            if lot_type != "C":
                continue
            cid = r2.get("CarParkID")
            if not cid:
                continue
            loc = r2.get("Location", ""); parts = loc.split()
            lat = lon = None
            if len(parts) >= 2:
                try: lat = float(parts[0]); lon = float(parts[1])
                except ValueError: pass
            lots = int(r2.get("AvailableLots") or 0)
            if cid in out:
                # Same CarParkID may appear multiple times for same LotType (e.g. C row
                # appears twice for differentAgency values); sum AvailableLots.
                out[cid]["lots"] += lots
            else:
                out[cid] = {
                    "area": r2.get("Area", ""), "dev": r2.get("Development", ""),
                    "lat": lat, "lon": lon,
                    "lots": lots, "type": lot_type,
                    "agency": r2.get("Agency", "") or "LTA",
                }
        except (KeyError, ValueError, TypeError):
            continue
    return out, (ok is True)   # a partial feed keeps its rows but reports not-ok (→ the note)

# ── EV endpoint fetch: per-thread keep-alive session + 5xx retry ─────────────
# The EV scan probes ~300 postals; a fresh curl process + TLS handshake per call
# is wasteful, and the LTA DataMall EV API intermittently returns HTTP 500 under
# concurrency. A thread-local requests.Session reuses one keep-alive connection
# per worker (~5x faster than curl-per-call), and retrying 5xx recovers the
# transient errors the old curl path silently swallowed as "no chargers".
# (This is LTA DataMall with an AccountKey header, not data.gov.sg, so requests
# is safe here — the urllib-403 caveat is data.gov.sg-specific.)
_ev_tls = threading.local()

def _ev_session():
    s = getattr(_ev_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"AccountKey": LTA_KEY, "Accept": "application/json"})
        _ev_tls.session = s
    return s

def _ev_get(url, tries=3):
    """GET an EV URL via the per-thread session. Retries transient 5xx with
    backoff; raises on persistent failure / 4xx (caller turns that into [])."""
    last = None
    for attempt in range(tries):
        try:
            r = _ev_session().get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code}")
            if r.status_code < 500:
                break  # client error — retrying won't help
        except Exception as e:
            last = e
        if attempt < tries - 1:
            time.sleep(0.3 * (attempt + 1))
    raise last if last is not None else RuntimeError(f"EV fetch failed: {url}")

def ev_by_postal(postal):
    """Fetch EV points for a single postal code → list, or None when the FETCH
    failed (so a scan can tell "no charger here" from "couldn't ask" — returning []
    for both made a failed discovery scan cache the area as covered-with-nothing)."""
    url = EV_URL.format(p=postal)
    try:
        d = _ev_get(url)
        v = d.get("value") if isinstance(d, dict) else None
        data = v.get("evLocationsData") if isinstance(v, dict) else None
        if not isinstance(data, list):
            return None   # a 200 with a fault/empty body is not "no charger here"
    except Exception:
        return None
    out = []
    for loc in data or []:
        lat = lon = None
        try: lat = float(loc["latitude"]); lon = float(loc["longitude"])
        except: pass
        name = loc.get("name", "")
        address = loc.get("address", "")
        # Aggregate all charging points at this location into one entry
        total_bays = 0
        available = 0
        unknown = 0   # bays whose status is neither "1" (free) nor "0" (in use)
        speed = power = plug = operator = ""
        for cp in loc.get("chargingPoints", []):
            pts = cp.get("plugTypes", [])
            # total_bays = count of individual EV bay IDs, not plugTypes (one plugType may have multiple evIds)
            total_bays += sum(len(pt.get("evIds", [])) for pt in pts)
            for pt in pts:
                for ev in pt.get("evIds", []):
                    st = str(ev.get("status", ""))
                    if st == "1":
                        available += 1
                    elif st != "0":
                        unknown += 1
            if pts and not speed:
                speed = pts[0].get("chargingSpeed", "")
                power = pts[0].get("powerRating", "")
                plug  = pts[0].get("plugType", "")
            operator = cp.get("operator", "") or operator
        if total_bays > 0:
            out.append({
                "name": name, "address": address,
                "lat": lat, "lon": lon,
                "available": available, "total": total_bays, "unknown": unknown,
                "speed": speed, "power": power,
                "plug": plug, "operator": operator
            })
    return out

# ── EV discovery scan ─────────────────────────────────────────────────────────
# The EV API is an exact-building lookup: a postal returns ONLY the charger AT
# that postal (verified 0-50m), not a radius. So discovery is a best-effort net
# over nearby postal numbers. We pay this scan rarely (cold area / monthly cron)
# and cache the resulting LOCATIONS — see the cache layer below.

def _narrow_variants(postal):
    """Candidate postals near `postal`: sector±1 × delivery-point±50 (≈303).

    A Singapore postal code's FIRST TWO digits are the postal sector (which maps
    to a geographic district); the last four are the delivery point. The old code
    split on the first digit only (`pi // 100000`), so for e.g. 138634 it probed
    sectors 03/13/23 — scattered across the island — and missed the true sector
    13 neighbours entirely. Keep the 2-digit sector and sweep the delivery point.
    """
    if not (postal.isdigit() and len(postal) == 6):
        return []
    pi = int(postal)
    sector = pi // 10000        # first two digits (00-99)
    dp     = pi % 10000         # last four digits (delivery point)
    variants = set()
    for so in (-1, 0, 1):       # neighbouring sectors
        s = sector + so
        if not (0 <= s <= 99):
            continue
        for do in range(-50, 51):
            td = dp + do
            if 0 <= td <= 9999:
                variants.add(f"{s:02d}{td:04d}")
    return sorted(variants)

EV_SCAN_MAX_FAIL = 0.2   # >20% failed probes → the scan is not trustworthy enough to cache as "covered"

def _scan_postals(plist):
    """Query postals concurrently (I/O-bound), dedupe locations by rounded lat/lon.
    Returns (locations, n_failed)."""
    def _safe_ev(p):
        try: return ev_by_postal(p)
        except Exception: return None
    seen = {}
    failed = 0
    # Keep-alive sessions make high fan-out unnecessary, and the EV API 500s more
    # the harder it's hit — 12 workers (each reusing one connection) is the sweet
    # spot: as fast as 64 curl-spawns but far gentler on the server.
    workers = min(12, len(plist)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_safe_ev, plist):
            if result is None:
                failed += 1; continue
            for e in result:
                if not (e['lat'] and e['lon']):
                    continue
                e = dict(e)
                e['postal'] = extract_postal(e.get('address', '')) or ''
                seen.setdefault((round(e['lat'], 5), round(e['lon'], 5)), e)
    if failed:
        print(f"WARNING: EV fetch failed for {failed}/{len(plist)} postal codes — some points may be missing", file=sys.stderr)
    return list(seen.values()), failed

def discover_ev(postal):
    """Discover EV charger LOCATIONS near `postal` via a narrow scan → (locations, failed_ratio)."""
    plist = _narrow_variants(postal)
    found, failed = _scan_postals(plist)
    return found, (failed / len(plist)) if plist else 0.0

# ── EV location cache ───────────────────────────────────────────────────────
# Charger locations are static; only per-bay availability is live. Cache the
# locations, run a live discovery scan only for an area we've never discovered
# (or on the monthly cron --refresh), and re-fetch availability live for the
# few chargers we actually display (refresh_ev_live).
EV_CACHE   = f"{DATA}/ev_locations.csv"
EV_ANCHORS = f"{DATA}/ev_anchors.csv"
ANCHOR_COVER_RADIUS = 1000   # m — reuse cache if target is within this of a prior discovery
CACHE_QUERY_RADIUS  = 5000   # m — cap on how far a cached charger may be to be a candidate
EV_CACHE_FIELDS = ["name", "address", "postal", "lat", "lon",
                   "operator", "plug", "speed", "power", "total"]

def load_ev_cache():
    if not os.path.exists(EV_CACHE):
        return []
    out = []
    with open(EV_CACHE) as f:
        for r in csv.DictReader(f):
            try: r['lat'] = float(r['lat']); r['lon'] = float(r['lon'])
            except: continue
            try: r['total'] = int(r['total'])
            except: r['total'] = 0
            r['available'] = 0   # placeholder; filled live at display time
            out.append(r)
    return out

def _atomic_write_csv(path, fieldnames, rows):
    """Write a CSV via temp file + os.replace so a crash/kill mid-write (or the monthly
    --refresh racing an interactive query) can never leave a truncated cache."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def save_ev_cache(rows):
    _atomic_write_csv(EV_CACHE, EV_CACHE_FIELDS,
                      [{k: r.get(k, "") for k in EV_CACHE_FIELDS} for r in rows])

def load_anchors():
    if not os.path.exists(EV_ANCHORS):
        return []
    out = []
    with open(EV_ANCHORS) as f:
        for r in csv.DictReader(f):
            try: out.append({"postal": r.get("postal", ""),
                             "lat": float(r["lat"]), "lon": float(r["lon"])})
            except: pass
    return out

def save_anchors(rows):
    _atomic_write_csv(EV_ANCHORS, ["postal", "lat", "lon"],
                      [{"postal": r.get("postal", ""), "lat": r["lat"], "lon": r["lon"]} for r in rows])

def merge_into_cache(found, cache):
    """Merge discovered locations into cache (dedupe by rounded lat/lon). Returns (rows, n_added)."""
    by_key = {(round(e['lat'], 5), round(e['lon'], 5)): e
              for e in cache if e.get('lat') and e.get('lon')}
    added = 0
    for e in found:
        k = (round(e['lat'], 5), round(e['lon'], 5))
        if k in by_key:
            by_key[k].update({f: e.get(f, by_key[k].get(f, "")) for f in EV_CACHE_FIELDS})
        else:
            by_key[k] = e; added += 1
    return list(by_key.values()), added

def add_anchor(postal, lat, lon, anchors):
    if any(haversine(lat, lon, a['lat'], a['lon']) <= ANCHOR_COVER_RADIUS for a in anchors):
        return anchors   # area already covered by an existing anchor
    anchors.append({"postal": postal, "lat": lat, "lon": lon})
    return anchors

def refresh_ev_live(entry):
    """Re-fetch current availability for one displayed location (≤1 quick query)."""
    p = entry.get('postal') or extract_postal(entry.get('address', '')) or ''
    if not p:
        return entry
    try:
        for e in ev_by_postal(p) or []:
            if (e['lat'] and e['lon']
                    and round(e['lat'], 5) == round(entry['lat'], 5)
                    and round(e['lon'], 5) == round(entry['lon'], 5)):
                entry = dict(entry)
                entry['available'] = e['available']
                # live availability CONFIRMED for this loc — unless some bays report a
                # status that is neither free nor in use (e.g. ""), when the count is unknown
                entry['available_live'] = not e.get('unknown')
                entry['total'] = e['total'] or entry.get('total', 0)
                for f in ("speed", "power", "plug", "operator"):
                    entry[f] = e.get(f) or entry.get(f, "")
                return entry
    except Exception:
        pass
    return entry

EV_SCAN_FAILED = False   # set when this run's discovery scan was unreliable
EV_LOCK = f"{DATA}/.ev_cache.lock"

def _commit_ev(found, anchor=None):
    """Merge `found` into the CURRENT on-disk cache (and add `anchor`) under an exclusive
    lock, re-reading the files inside it. The long discovery scan runs OUTSIDE the lock;
    only this short read-merge-write is serialised, so the monthly --refresh (which used
    to save the copy it loaded minutes earlier) can't erase a concurrent query's finds,
    and a query never waits on a whole refresh. Returns (cache_rows, n_added)."""
    import fcntl
    with open(EV_LOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cache, added = merge_into_cache(found, load_ev_cache())
            save_ev_cache(cache)             # rows first, then the anchor that vouches for them
            if anchor is not None:
                save_anchors(add_anchor(*anchor, load_anchors()))
            return cache, added
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)

def get_ev_nearby(postal, lat, lon):
    """Cache-first EV lookup → candidate locations near (lat, lon).

    Fast path: if the area was discovered before (within ANCHOR_COVER_RADIUS of
    an anchor), serve cached locations with NO live scan. Cold path: discover
    once, merge into the cache, record an anchor, then serve.
    """
    if lat is None and not postal:
        return []
    cache = load_ev_cache()
    anchors = load_anchors()

    def nearby_from_cache():
        if lat is None:
            return []
        return [e for e in cache
                if e.get('lat') and e.get('lon')
                and haversine(lat, lon, e['lat'], e['lon']) <= CACHE_QUERY_RADIUS]

    covered = lat is not None and any(
        haversine(lat, lon, a['lat'], a['lon']) <= ANCHOR_COVER_RADIUS for a in anchors)
    if covered:
        n = nearby_from_cache()
        print(f"EV cache hit — {len(n)} cached location(s) near target (no live scan)")
        return n

    if not postal:
        print("EV cache miss and no postal code — cannot discover a new area without a postal.",
              file=sys.stderr)
        return nearby_from_cache()

    print("EV cache miss — discovering this area once (live scan)...", flush=True)
    found, fail_ratio = discover_ev(postal)
    global EV_SCAN_FAILED
    EV_SCAN_FAILED = fail_ratio > EV_SCAN_MAX_FAIL
    reliable = lat is not None and not EV_SCAN_FAILED
    cache, added = _commit_ev(found, (postal, lat, lon) if reliable else None)
    if lat is not None and not reliable:
        # Don't mark the area covered: the next query re-scans instead of serving an
        # empty cache for a month (until the 1st-of-month --refresh).
        print(f"WARNING: EV scan unreliable ({fail_ratio:.0%} of probes failed) — area NOT "
              f"cached as covered; the next query will rescan", file=sys.stderr)
    print(f"Discovered {len(found)} location(s) ({added} new) → cached")
    return nearby_from_cache() if lat is not None else found

def refresh_cache():
    """Monthly cron entry point: re-discover every anchor area, merge new finds.
    Returns False if any anchor's scan was unreliable (the crontab run then exits 1,
    and the log line says which) — an outage used to log "complete — 0 new" and exit 0."""
    from datetime import datetime
    print(f"EV cache refresh started {datetime.now():%Y-%m-%d %H:%M:%S}")
    anchors = load_anchors()
    if not anchors:
        print("EV cache refresh: no anchors yet — run a normal query first to seed the cache.")
        return True
    all_found, bad = [], []
    for a in anchors:
        if not a.get('postal'):
            continue
        found, fail_ratio = discover_ev(a['postal'])
        all_found += found
        if fail_ratio > EV_SCAN_MAX_FAIL:
            bad.append(a['postal'])
        note = f" ({fail_ratio:.0%} of probes FAILED)" if fail_ratio else ""
        print(f"  anchor {a['postal']}: {len(found)} found{note}")
    _, total_added = _commit_ev(all_found)
    print(f"EV cache refresh complete — {total_added} new location(s) across {len(anchors)} anchor(s)"
          + (f"; UNRELIABLE scans for {', '.join(bad)}" if bad else "") + ".")
    return not bad

def load_csv(path):
    """Load a CSV file, exiting with a clear error if missing."""
    if not os.path.exists(path):
        print(f"ERROR: CSV not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return list(csv.DictReader(f))

jtc_rows = load_csv(f"{DATA}/jtc_carpark_info.csv")
ura_rows = load_csv(f"{DATA}/ura_carpark_info.csv")

# HDB CSV: car_park_no → (x_coord, y_coord, address)
hdb_coords = {}; hdb_addr = {}
with open(f"{DATA}/carpark_info.csv") as f:
    for row in csv.DictReader(f):
        cpn = row.get('car_park_no', '')
        hdb_addr[cpn] = row.get('address', '')
        try: hdb_coords[cpn] = (float(row['x_coord']), float(row['y_coord']))
        except: pass

# ── Display radius cap ───────────────────────────────────────────────────────
# Hard limit: never surface a parking lot or EV charger farther than this from
# the destination. The nearest_* helpers sort by distance and take the top N, so
# without this cap the closest available candidate is shown even when it is
# kilometres away (a user querying postal 569088 in Ang Mo Kio saw an EV "Spot C"
# 4.4 km away in Hougang). SVY21-euclidean (HDB/JTC) and haversine (LTA/URA/EV)
# distances are both in METRES, so a single cap applies uniformly to every source.
MAX_RADIUS_M = 1000   # 1 km

def nearest_hdb(tx, ty, hdb_dict, n=3, max_dist=MAX_RADIUS_M):
    scored = []
    for code, cp in hdb_dict.items():
        if code in hdb_coords:
            x, y = hdb_coords[code]
            if x and y:
                d = euclidean_svy21(tx, ty, x, y)
                if d <= max_dist:
                    scored.append((d, x, y, code, cp))
    return sorted(scored, key=lambda x: x[0])[:n]

def nearest_lta(tlat, tlon, lta_dict, n=3, max_dist=MAX_RADIUS_M):
    scored = [(haversine(tlat, tlon, cp["lat"], cp["lon"]), cp["lat"], cp["lon"], code, cp)
              for code, cp in lta_dict.items() if cp["lat"] and cp["lon"]]
    scored = [s for s in scored if s[0] <= max_dist]
    return sorted(scored, key=lambda x: x[0])[:n]

def nearest_ura(tlat, tlon, rows, n=3, max_dist=MAX_RADIUS_M):
    scored = []
    for r in rows:
        # Only include car lots — skip rows where no_car=0
        if int(r.get('no_car', 0) or 0) == 0:
            continue
        try:
            la, lo = float(r["latitude"]), float(r["longitude"])
            d = haversine(tlat, tlon, la, lo)
            if d <= max_dist:
                scored.append((d, la, lo, r))
        except: pass
    return sorted(scored, key=lambda x: x[0])[:n]

def nearest_jtc(tx, ty, rows, n=3, max_dist=MAX_RADIUS_M):
    scored = []
    for r in rows:
        try:
            x = float(r["x_coord"]); y = float(r["y_coord"])
            d = euclidean_svy21(tx, ty, x, y)
            if d <= max_dist:
                scored.append((d, x, y, r))
        except: pass
    return sorted(scored, key=lambda x: x[0])[:n]

def nearest_ev(tlat, tlon, ev_list, n=3, max_dist=MAX_RADIUS_M):
    scored = [(haversine(tlat, tlon, e["lat"], e["lon"]), e) for e in ev_list if e["lat"] and e["lon"]]
    scored = [s for s in scored if s[0] <= max_dist]
    return sorted(scored, key=lambda x: x[0])[:n]

def static_map_url(markers, w=600, h=400):
    base = f"https://maps.googleapis.com/maps/api/staticmap?size={w}x{h}&scale=2&format=png32"
    for lat, lon, label, color in markers:
        base += f"&markers=color:{color}%7Clabel:{label}%7C{lat:.6f},{lon:.6f}"
    base += f"&key={STATIC_KEY}"
    return base

# ─────────────────────────────────────────────────────────────────────────────
# Monthly cron entry point: refresh the EV location cache for known areas.
if CLI["refresh"]:
    sys.exit(0 if refresh_cache() else 1)


def _session_target():
    """The Discord conversation this run was invoked from, or None. The gateway exports
    the session identity to the agent's terminal subprocess; the key is
    agent:main:discord:<chat_type>:<chat_id>[:...] and parts[4] (chat_id) is the
    channel — or, for the auto-created reply thread every parking request gets, the
    THREAD — to post into. Never parts[5] (the user id for a plain channel)."""
    snow = lambda t: t.isascii() and t.isdigit() and len(t) >= 15   # a Discord snowflake
    parts = os.environ.get("HERMES_SESSION_KEY", "").split(":")
    # the gateway builds the key and CHAT_ID from the same message source (gateway/run.py
    # _set_session_env), so a mismatch means a foreign/stale env — only compare real ids
    ids = {v for v in (os.environ.get(k, "").strip()
                       for k in ("HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID")) if snow(v)}
    if len(parts) >= 5 and parts[2] == "discord" and snow(parts[4]):
        # Cross-check: the gateway exports the key AND the chat/thread ids for the same
        # turn. If they are present but disagree, the identity is ambiguous — refuse.
        if ids and parts[4] not in ids:
            print(f"ERROR: session identity is inconsistent (key chat {parts[4]} vs "
                  f"{', '.join(sorted(ids))}) — pass --send <channel_id> explicitly.", file=sys.stderr)
            return None
        return parts[4]
    if os.environ.get("HERMES_SESSION_PLATFORM", "") == "discord":
        for k in ("HERMES_SESSION_THREAD_ID", "HERMES_SESSION_CHAT_ID"):
            v = os.environ.get(k, "").strip()
            if snow(v):
                return v
    return None


SEND = CLI["send"]
SEND_CHANNEL, SEND_THREAD = CLI["channel"], CLI["thread"]
# Where --send posts: an explicit --thread, else an explicit --send <id>, else the
# conversation this was run from. Resolved BEFORE any API call, and fail closed —
# never fall back to a fixed/home channel.
TARGET_CHANNEL = SEND_THREAD or SEND_CHANNEL or (_session_target() if SEND else None)
TARGET_HOW = ("--thread" if SEND_THREAD else "--send <id>" if SEND_CHANNEL
              else "current conversation")
if SEND and not TARGET_CHANNEL:
    print("ERROR: --send could not resolve the Discord conversation to post into (no usable "
          "Hermes session identity) — pass --send <channel_id>. Refusing to guess.",
          file=sys.stderr)
    sys.exit(2)

address = CLI["address"]
print(f"\n{'='*95}")
print(f"Address: {address}")

loc = geocode(address)
if not loc:
    print("ERROR: Could not geocode address (no match in Singapore) — give a 6-digit postal "
          "code or a fuller street address"); sys.exit(1)

postal = loc.get("postal") or ""
print(f"Geocoded → SVY21({loc['svy21_x']:.1f},{loc['svy21_y']:.1f})  WGS84({loc['lat']:.5f},{loc['lon']:.5f})")
print(f"Resolved: {loc['address']} | Postal: {postal}")

tx, ty, tlat, tlon = loc['svy21_x'], loc['svy21_y'], loc['lat'], loc['lon']

hdb_dict, hdb_ok = hdb_live()
print(f"HDB live feed: {len(hdb_dict)} carparks" if hdb_ok else "HDB live feed: UNAVAILABLE")
lta_dict, lta_ok = lta_live()
# LTA's feed also carries ~2,000 HDB-agency rows (no totals). The HDB feed is the
# better source for those, so drop the LTA copy of every HDB carpark the HDB feed
# can place — otherwise they crowd the LTA malls out of LTA's top 3 and ship HDB
# carparks labelled "LTA" with no total. Kept (as HDB) only when the HDB feed failed
# or can't place that code.
for _c in [c for c, cp in lta_dict.items()
           if cp.get("agency") == "HDB" and c in hdb_dict and c in hdb_coords]:
    del lta_dict[_c]
print(f"LTA live feed: {len(lta_dict)} carparks (after dropping HDB duplicates)" if lta_ok
      else f"LTA live feed: PARTIAL — {len(lta_dict)} carparks, some pages failed" if lta_dict
      else "LTA live feed: UNAVAILABLE")
ev_list = get_ev_nearby(postal, tlat, tlon)
print(f"EV: {len(ev_list)} cached charger location(s) within 5 km (display keeps those ≤1 km)\n")

phdb = nearest_hdb(tx, ty, hdb_dict, n=3)
plta = nearest_lta(tlat, tlon, lta_dict, n=3)
pura = nearest_ura(tlat, tlon, ura_rows, n=3)
pjtc = nearest_jtc(tx, ty, jtc_rows, n=3)
pev  = nearest_ev(tlat, tlon, ev_list, n=20)

# ── Merge EV entries from same location ─────────────────────────────────────────
def merge_ev(ev_sorted):
    """Merge EV entries sharing the same lat/lon (rounded to 5dp ≈ 1m)."""
    by_loc = {}
    for d, e in ev_sorted:
        key = (round(e['lat'], 5), round(e['lon'], 5)) if e['lat'] and e['lon'] else None
        if key not in by_loc:
            by_loc[key] = (d, {
                "name": e['name'], "address": e['address'],
                "postal": e.get('postal', ''),
                "lat": e['lat'], "lon": e['lon'],
                "available": 0, "total": 0,
                # False until refresh_ev_live confirms a live count for this location;
                # gates the bays cell so an unconfirmed placeholder 0 renders "?/N"
                # (unknown) instead of "0/N" (which reads as a genuinely-full charger).
                "available_live": False,
                "speed": e['speed'], "power": e['power'],
                "plug": e['plug'], "operator": e['operator']
            })
        agg = by_loc[key][1]
        agg["available"] += e['available']
        agg["total"] += e['total']
    merged = sorted(by_loc.values(), key=lambda x: x[0])[:3]
    return [(d, e) for d, e in merged]

pev_merged = merge_ev(pev)
# Always show LIVE availability for the displayed chargers (≤3 quick queries),
# so cached availability never goes stale on screen.
pev_merged = [(d, refresh_ev_live(e)) for d, e in pev_merged]

# ── Build live-code registry for deduplication ───────────────────────────────
lta_live_registry = {}
for d, lat_wgs, lon_wgs, code, cp in plta:
    key = code.upper()
    lta_live_registry[key] = (d, lat_wgs, lon_wgs, cp['lots'], cp['dev'] or cp['area'])

# ── Merge all parking systems, take top 3 ─────────────────────────────────────
COLORS = ["blue", "green", "yellow", "purple", "orange"]
marker_list = [(tlat, tlon, "YOU", "red")]

all_parking = []

for d, x, y, code, cp in phdb:
    lat_wgs, lon_wgs = svy21_to_wgs84(x, y)
    total = cp['total']
    avail_s = f"{cp['lots']}/{total}" if total > 0 else str(cp['lots'])
    # a sensor that hasn't reported for HDB_STALE_H is shown, but not as live
    all_parking.append((d, lat_wgs, lon_wgs, code, "HDB", avail_s, hdb_addr.get(code, ""),
                        cp.get("fresh", True)))

for d, lat_wgs, lon_wgs, code, cp in plta:
    agency = cp.get("agency") or "LTA"
    all_parking.append((d, lat_wgs, lon_wgs, code, agency if agency in ("HDB", "URA") else "LTA",
                        str(cp['lots']), cp['dev'] or cp['area'], True))

for d, lat_wgs, lon_wgs, r in pura:
    addr = r.get('parking_pl', '').strip()
    total = int(r.get('no_car', 0) or 0)
    all_parking.append((d, lat_wgs, lon_wgs, r['pp_code'], "URA", str(total), addr, False))

for d, x, y, r in pjtc:
    lat_wgs, lon_wgs = svy21_to_wgs84(x, y)
    addr = r.get('address', '').strip()
    all_parking.append((d, lat_wgs, lon_wgs, r['car_park_no'], "JTC", "n/a", addr, False))

all_parking.sort(key=lambda x: x[0])

# Deduplicate: if same code appears as both URA and LTA, drop the URA (static) one
seen_codes = {}
deduped = []
for entry in all_parking:
    d, lat_wgs, lon_wgs, code, sys_, lots_s, addr, is_live = entry
    key = code.upper()
    if not is_live and key in lta_live_registry:
        continue
    if key in seen_codes:
        continue
    seen_codes[key] = entry
    deduped.append(entry)

top_parking = deduped[:3]

# ── Tables via discord-table-format renderer (PNG images) ────────────────────
# See skills/productivity/discord-table-format/SKILL.md for full pipeline.
# Empty tables produce no image (renderer maps them to null).
DISCORD_RENDERER = "/home/amh/.hermes/skills/productivity/discord-table-format/scripts/render_tables.py"

def render_discord_tables(specs):
    """Render multiple tables in ONE renderer invocation, returning
    {title: png_path_or_None}. `specs` is a list of (title, headers, rows,
    prefix). The renderer is explicitly built to render all sections in a single
    process (its docstring: amortizes interpreter + PIL/font startup), so one
    call for both the parking and EV tables beats two separate subprocesses.
    Sections with no rows are skipped; returns {} if there's nothing to render.
    """
    import os
    live = [(t, h, r, p) for (t, h, r, p) in specs if r]
    if not live:
        return {}
    if not os.path.exists(DISCORD_RENDERER):
        raise FileNotFoundError(
            f"discord-table-format renderer not found at {DISCORD_RENDERER}")
    os.makedirs("/tmp/hermes_cache", exist_ok=True)
    sections = [{"title": t, "headers": h, "rows": r, "prefix": p}
                for (t, h, r, p) in live]
    res = subprocess.run(
        ["python3", DISCORD_RENDERER],
        input=json.dumps(sections), capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"renderer failed: {res.stderr}")
    return json.loads(res.stdout)  # {title: path or null}

def _load_token():
    """Load DISCORD_BOT_TOKEN from ~/.hermes/.env. Used by --send mode.

    Tolerates an optional `export ` prefix and surrounding single/double quotes
    on the value — otherwise a `.env` written as `export DISCORD_BOT_TOKEN="…"`
    yields a token with literal quotes (→ 401) or no match at all.
    """
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(env_path):
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                val = line.split("=", 1)[1].strip()
                return val.strip('"').strip("'")
    return None

def _discord_send_pair(token, channel_id, content, img_path=None, max_retries=3, nonce=None,
                       deadline=None, embeds=None, files=None):
    """Send ONE Discord message via curl multipart. If img_path is given, the file is
    attached to the SAME message (atomic header+image pair) — the only reliable way to
    pair text+image (the gateway's `extract_media()` reorders MEDIA: tags).

    Returns (ok, status) — ok ONLY on 2xx; status is the HTTP code ("" if curl itself
    failed). content + allowed_mentions:{parse:[]} ride in payload_json (pings blocked
    server-side; @/</; stay literal). A per-message `nonce` + enforce_nonce makes a
    retry after a timeout/5xx safe: if the first POST actually landed, Discord returns
    that message instead of creating a duplicate. 429 honours retry_after.
    `embeds` + `files` send the whole report as ONE message: files[i] is uploaded as
    imgN.png and shown by the embed whose image.url is attachment://imgN.png.
    """
    last_err, status = "", ""
    body_json = {"content": content, "allowed_mentions": {"parse": []}}
    if embeds:
        body_json["embeds"] = embeds
        body_json["attachments"] = [{"id": i, "filename": f"img{i}.png"} for i in range(len(files or []))]
    if nonce:
        body_json.update({"nonce": nonce, "enforce_nonce": True})
    payload = json.dumps(body_json)
    for attempt in range(max_retries):
        # never let one POST run past the send deadline (curl's own timeout is the bound)
        mt = 15 if deadline is None else max(2, min(15, int(deadline - time.monotonic())))
        cmd = [
            "curl", "-s", "--max-time", str(mt), "-w", "\n%{http_code}", "-X", "POST",
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            "-H", f"Authorization: Bot {token}",
            "--form-string", f"payload_json={payload}",
        ]
        if files:
            for i, f in enumerate(files):
                cmd += ["-F", f"files[{i}]=@{f};filename=img{i}.png;type=image/png"]
        elif img_path and os.path.exists(img_path):
            cmd += ["-F", f"file=@{img_path}"]
        wait = 1.5 * (attempt + 1)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=mt + 5)
        except Exception as e:
            last_err, status, transient = f"curl invocation failed: {type(e).__name__}", "", True
        else:
            if r.returncode != 0:
                last_err, status, transient = f"curl exit {r.returncode}", "", True
            else:
                body, _, status = r.stdout.rpartition("\n")
                status = status.strip()
                if status.startswith("2"):
                    return True, status
                last_err = f"HTTP {status}: {body[:200]}"
                transient = status in ("429", "500", "502", "503", "504")
                if status == "429":
                    try:
                        ra = float(json.loads(body).get("retry_after", wait))
                        if math.isfinite(ra):
                            wait = max(0.0, min(ra + 0.25, 30.0))
                    except Exception:
                        pass
        if transient and attempt < max_retries - 1:
            if deadline is not None and time.monotonic() + wait > deadline:
                last_err += " (send deadline reached — not retrying)"
                status = status or "deadline"
                break
            time.sleep(wait)
            continue
        break
    print(f"SEND ERROR: {last_err}", file=sys.stderr)
    return False, status

# ── Parking table ─────────────────────────────────────────────────────────────
park_rows = []
entry_num = 0
for d, lat_wgs, lon_wgs, code, sys_, lots_s, addr, is_live in top_parking:
    entry_num += 1
    color = COLORS[(entry_num - 1) % 5]
    # Live column uses Y/N (per user preference, Nov 2026)
    park_rows.append([str(entry_num), f"{d:.0f}m", lots_s,
                      "Y" if is_live else "N", code, sys_, addr])
    marker_list.append((lat_wgs, lon_wgs, str(entry_num), color))

# Both tables are rendered together below (single renderer invocation).

# ── EV table ─────────────────────────────────────────────────────────────────
ev_rows = []
ev_num = 0
for d, e in pev_merged:
    ev_num += 1
    # "avail/total" only when a live refresh CONFIRMED the count; otherwise the
    # cached placeholder is 0 and "0/total" would falsely read as a full charger,
    # so show "?/total" (bays known, availability unconfirmed) — see refresh_ev_live.
    if e['total'] > 0 and e.get('available_live'):
        bays_str = f"{e['available']}/{e['total']}"
    elif e['total'] > 0:
        bays_str = f"?/{e['total']}"
    else:
        bays_str = "?"
    power_str = f"{e['speed']}kW" if e['speed'] else (f"{e['power']}kW" if e['power'] else "n/a")
    ev_label = chr(64 + ev_num)
    ev_rows.append([ev_label, f"{d:.0f}m", bays_str, power_str, e['operator'], e['address']])
    color = COLORS[(entry_num + ev_num - 1) % 5]

    # Offset EV marker if it lands on same coordinate as an existing parking marker
    ev_lat, ev_lon = e['lat'], e['lon']
    for (_, plat, plon, _, _, _, _, _) in top_parking:
        if abs(ev_lat - plat) < 0.0001 and abs(ev_lon - plon) < 0.0001:
            # Slight northeast offset (~4m)
            ev_lat += 0.00004
            ev_lon += 0.00004
            break
    marker_list.append((ev_lat, ev_lon, ev_label, color))

# ── Render both tables in ONE renderer invocation ────────────────────────────
# Reclaim earlier runs' PNGs by AGE (>1 h) — bounds the /tmp/hermes_cache leak without
# deleting a concurrent/just-finished run's images that its delivery still needs (a
# wipe-all glob here used to delete another query's tables before they were posted).
import glob as _glob
for _old in (_glob.glob("/tmp/hermes_cache/carpark_parking_*.png")
             + _glob.glob("/tmp/hermes_cache/carpark_ev_*.png")
             + _glob.glob("/tmp/hermes_cache/carpark_map_*.png")):
    try:
        if time.time() - os.path.getmtime(_old) > 3600:
            os.remove(_old)
    except OSError:
        pass
# …and never keep more than the newest 10 maps (terminal/--json runs don't clean up)
def _mtime(f):
    try:
        return os.path.getmtime(f)
    except OSError:
        return 0
_maps = sorted(_glob.glob("/tmp/hermes_cache/carpark_map_*.png"), key=_mtime)
for _old in _maps[:-10]:
    try:
        os.remove(_old)
    except OSError:
        pass
# A renderer failure/timeout must not sink the whole report — degrade to
# text-only (header) sections rather than crash before the map + address ship.
try:
    _table_imgs = render_discord_tables([
        ("Parking", ["#", "Dist", "Avail", "Live", "Code", "System", "Address"],
         park_rows, "carpark_parking"),
        ("EV Charging", ["#", "Dist", "Bays", "Power", "Operator", "Address"],
         ev_rows, "carpark_ev"),
    ])
except Exception as e:
    print(f"WARNING: table rendering failed ({e}) — delivering without table images",
          file=sys.stderr)
    _table_imgs = {}
park_img = _table_imgs.get("Parking")
ev_img = _table_imgs.get("EV Charging")

# ── Build section list for delivery ───────────────────────────────────────────
# Each section is (header, summary_line, image_path). Slices the report into
# multiple Discord messages with images interleaved where the text tables used
# to be.
#
# Fewer than 3 rows means the MAX_RADIUS_M (1 km) cap left little (or nothing) in
# range — each source caps its own candidates at 3 within the radius, so a total
# below 3 genuinely means that few exist within 1 km. Surface that as a note under
# the header so a short (or empty) table doesn't read as a data glitch: "No other
# …" when some are shown, "No …" when the section is empty. The 0-result section
# is now emitted WITH the note (it used to be dropped silently).
def _radius_note(count, label):
    if count >= 3:
        return None
    if count == 0:
        return f"*No {label} within 1km radius.*"
    return f"*No other {label} within 1km radius.*"

def _park_digest_line(r):
    num, dist, lots_s, live, code, sys_, addr = r
    if live == "Y":
        avail = f"{lots_s} lots free (live)"
    elif sys_ == "URA":
        avail = f"capacity {lots_s} (static — no live count)"
    elif lots_s == "n/a":
        avail = "no availability data (static)"
    else:
        avail = f"{lots_s} lots (sensor stale — not live)"
    return f"{num}) {dist} · {avail} · {code} {sys_} · {addr}"


def _ev_digest_line(r):
    lab, dist, bays, power, op, addr = r
    return f"{lab}) {dist} · {bays} bays free · {power} · {op} · {addr}"


# Plain-text digest of every row: printed in every mode (the agent can't read the
# table PNGs, so without this it answered from progress lines and misquoted counts),
# carried in --json, and used as the section body when the table image failed.
park_digest = [_park_digest_line(r) for r in park_rows]
ev_digest = [_ev_digest_line(r) for r in ev_rows]


def _with_rows(note, img, digest):
    """Section body: the note, plus the rows as text when the table image is missing
    (a renderer failure used to post a bare header with no data)."""
    if img or not digest:
        return note
    # a LIST body = one sanitized line each (format_section_content keeps the line
    # breaks between them but still strips any break INSIDE a line)
    return ([note] if note else []) + ["*(table image unavailable)*"] + digest


sections = []
park_note = _radius_note(len(park_rows), "parking")
_failed_src = [n for n, ok in (("HDB", hdb_ok), ("LTA", lta_ok)) if not ok]
if _failed_src:
    # an outage must never read as "No parking within 1km" (a false fact)
    park_note = (f"*⚠️ {' & '.join(_failed_src)} live data unavailable right now — "
                 f"this list may be incomplete.*")
if park_img or park_rows or park_note:
    sections.append(("🅿️ PARKING", _with_rows(park_note, park_img, park_digest), park_img))

ev_note = _radius_note(len(ev_rows), "EV charging")
if EV_SCAN_FAILED and ev_note:
    # a failed discovery scan must not read as "No EV charging within 1km"
    ev_note = "*⚠️ EV charger data unavailable right now — this list may be incomplete.*"
if ev_img or ev_rows or ev_note:
    sections.append(("⚡ EV CHARGING", _with_rows(ev_note, ev_img, ev_digest), ev_img))

# Address + static map is the closing section — address text is the message
# body, map is the attachment.
# Per-run map file (a fixed /tmp/carpark_map.png let a second query overwrite the
# first one's map before it was delivered).
os.makedirs("/tmp/hermes_cache", exist_ok=True)
MAP_PNG = f"/tmp/hermes_cache/carpark_map_{os.getpid()}_{int(time.time() * 1000)}.png"
sections.append((
    "📍 LOCATION",
    loc["address"],
    MAP_PNG,  # downloaded below; dropped from the section if the download fails
))

# ── Static map (download before any send) ────────────────────────────────────
map_url = static_map_url(marker_list)   # carries the key — never write it to a world-readable file

map_path = MAP_PNG
try:
    req = urllib.request.Request(map_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
    # Static Maps can answer 200 with a text/error body: attach only a real PNG
    # (morning_commute.py does the same check).
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"not a PNG ({len(body)} bytes)")
    with open(map_path, "wb") as f:
        f.write(body)
except Exception as e:
    print(f"Map download failed: {type(e).__name__}: {str(e)[:80]}", file=sys.stderr)
    map_path = None

# If the map failed to download, surface it on the LOCATION section instead of
# silently delivering address-only text with no indication the map is missing.
if map_path is None:
    _patched = []
    for h, b, i in sections:
        if h == "📍 LOCATION":
            b = (b + "\n*(map unavailable)*") if b else "*(map unavailable)*"
            i = None
        _patched.append((h, b, i))
    sections = _patched

# ── --json mode: emit manifest, exit ─────────────────────────────────────────
if EMIT_JSON:
    manifest = {
        "title": "## :red_car: Parking Lot Report",
        "address": loc["address"],
        "postal": postal,
        "sections": [
            {"header": h, "body": ("\n".join(b) if isinstance(b, list) else (b or "")), "image": i}
            for (h, b, i) in sections
        ],
        "rows": {"parking": park_digest, "ev": ev_digest},
        "live_sources_ok": {"HDB": hdb_ok, "LTA": lta_ok, "EV": not EV_SCAN_FAILED},
    }
    print(json.dumps(manifest, indent=2), file=_JSON_OUT)
    sys.exit(0)

# ── Section content formatter (single source of truth) ──────────────────────
# Both --send (Discord) and the terminal-output path must produce the EXACT same
# string for the same (header, body) tuple. Previously the two paths diverged
# (--send sent `**{header}**`, terminal printed `--- {header} ---`), which let
# an ad-hoc agent delivery loop grab the terminal decoration and post it to
# Discord verbatim. Centralising the format kills that whole class of bug.

def _sanitize_discord(s):
    """Defang untrusted text (a geocoded address) interpolated into a Discord
    message: strip newlines, neutralize backticks, defang mentions, and break
    masked/auto links. Pairs with allowed_mentions:{parse:[]} on the POST, which
    only blocks the ping — this also stops format-breaks and link injection."""
    if not s:
        return s
    z = "\u200b"  # zero-width space
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace("`", "'")
    s = s.replace("@", "@" + z)        # @everyone / @here / <@id>
    s = s.replace("](", "]" + z + "(")  # masked links [text](url)
    s = s.replace("://", ":" + z + "//")  # bare autolinks
    return s


EMBED_COLORS = [0x5865F2, 0x57F287, 0xFEE75C]


def section_body_text(body):
    """A section's body as sanitized text (the part of format_section_content after the
    header); used as an embed description. `body` is untrusted (geocoded address)."""
    if isinstance(body, list):
        return "\n".join(_sanitize_discord(b) for b in body if b)
    return _sanitize_discord(body) if body else ""


def format_section_content(header, body):
    """Return the canonical Discord content string for one section. `header` is a
    fixed trusted literal; `body` is untrusted (geocoded address) → sanitized."""
    if isinstance(body, list):
        body = "\n".join(_sanitize_discord(b) for b in body if b)
    elif body:
        body = _sanitize_discord(body)
    if body:
        return f"**{header}**\n{body}"
    return f"**{header}**"


# ── --send mode: curl multipart per section ──────────────────────────────────
def _print_digest():
    """Agent-only plain-text rows (NOT posted — the table images carry them in Discord).
    Answer the user's follow-ups from these lines, not from the progress counts above."""
    print("\nAGENT DIGEST — rows shown in the report (for your reply; do not post this):")
    print("  Parking:" + ("" if park_digest else " none within 1 km"))
    for line in park_digest:
        print("   " + line)
    if _failed_src:
        print(f"   ! {' & '.join(_failed_src)} live data unavailable — list may be incomplete")
    print("  EV charging:" + ("" if ev_digest else
                              " data unavailable (scan failed)" if EV_SCAN_FAILED else " none within 1 km"))
    if EV_SCAN_FAILED and ev_digest:
        print("   ! EV scan partly failed — list may be incomplete")
    for line in ev_digest:
        print("   " + line)


# The agent's terminal call often uses timeout: 60. The WHOLE run (geocode, feeds, EV,
# render, map, every POST) must end with a printed verdict before that, so the send
# deadline is measured from script start and each curl is clamped to what's left.
SEND_DEADLINE_S = 50   # seconds after script start
MIN_START_S = 3        # don't start a message with less than this left

if SEND:
    token = _load_token()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not found in ~/.hermes/.env", file=sys.stderr)
        sys.exit(1)

    # This run's PNGs (tables + map) — reclaimed only after a FULL delivery, so a
    # failed/partial send leaves them for a retry (the age sweep clears them later).
    generated_pngs = [img for (_, _, img) in sections if img]
    title = "## :red_car: Parking Lot Report"
    # Primary layout (user decision 2026-10-29): ONE message, the title as text plus one
    # embed card per section (header = card title, body = description, table/map = image).
    embeds, files = [], []
    for n, (h, b, i) in enumerate(sections):
        card = {"title": h, "color": EMBED_COLORS[n % len(EMBED_COLORS)]}
        desc = section_body_text(b)
        if desc:
            card["description"] = desc[:4096]
        if i and os.path.exists(i):
            card["image"] = {"url": f"attachment://img{len(files)}.png"}
            files.append(i)
        embeds.append(card)
    # Fallback layout: the title, then one header+image message per section.
    messages = [(title, None)] + [(format_section_content(h, b), i) for (h, b, i) in sections]
    total = 1
    run_id = f"{int(time.time() * 1000) % 10**11:011d}"   # nonce: "sgc" + 11 + 2 ≤ 25 chars
    sent, fatal = 0, None
    # The agent's terminal call times out at ~60–120 s; a sustained 429/5xx must end the
    # run with a clear message before that, not be killed mid-send (→ duplicate re-run).
    send_deadline = _T0 + SEND_DEADLINE_S
    ambiguous = False     # a POST that timed out / 5xx'd may have landed anyway
    try:
        ok, status = _discord_send_pair(token, TARGET_CHANNEL, title, nonce=f"sgc{run_id}99",
                                        deadline=send_deadline, embeds=embeds, files=files)
        if ok:
            sent = 1
        elif status == "400":
            # Discord rejected the carded post outright (nothing landed): send the
            # previous layout instead, one message per section.
            print("WARNING: single post rejected (HTTP 400); sending one message per section",
                  file=sys.stderr)
            total = len(messages)
        else:
            if status not in ("401", "403", "404", "429"):
                ambiguous = True   # 5xx / network / deadline: the POST may have landed
            fatal = status if status in ("401", "403", "404", "429", "deadline") else None
        for idx, (content, img) in enumerate(messages if total > 1 else []):
            if send_deadline - time.monotonic() < MIN_START_S:
                fatal = fatal or "deadline"
                break
            ok, status = _discord_send_pair(token, TARGET_CHANNEL, content, img,
                                            nonce=f"sgc{run_id}{idx:02d}", deadline=send_deadline)
            if ok:
                sent += 1
            else:
                if status not in ("401", "403", "404", "429"):
                    ambiguous = True   # 5xx / network / deadline: the POST may have landed
                if status in ("401", "403", "404", "429", "deadline"):
                    # Not transient / retries exhausted — every later post would fail the same way.
                    fatal = status
                    break
            time.sleep(0.5)  # Discord rate-limit headroom
    except Exception as e:
        # never let an unexpected error turn a partial delivery into a re-run (duplicates)
        print(f"SEND ERROR: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        fatal = fatal or "error"
    finally:
        if sent == total:
            for img in generated_pngs:
                if img and os.path.exists(img):
                    try:
                        os.remove(img)
                    except OSError:
                        pass

    where = f"{TARGET_CHANNEL} ({TARGET_HOW})"
    _print_digest()
    if sent == total:
        print(f"\n[SILENT]\nSent {sent}/{total} messages to {where}")
        sys.exit(0)
    if sent == 0 and fatal == "404" and TARGET_HOW != "current conversation":
        print(f"\nDELIVERY FAILED: {TARGET_CHANNEL} is not a channel/thread the bot can post to "
              f"(HTTP 404 — a MESSAGE id?). Re-run with plain --send (no id): it posts into the "
              f"current conversation.")
    elif sent == 0:
        why = ("HTTP " + fatal if fatal and fatal.isdigit()
               else "send time budget used up" if fatal == "deadline"
               else "unexpected error" if fatal == "error" else "every post failed (5xx/network)")
        if ambiguous:
            # a timed-out/5xx POST may have landed — a re-run gets new nonces and could duplicate
            print(f"\nDELIVERY FAILED: {why} — nothing was CONFIRMED posted to {where} (a timed-out "
                  f"post may still have landed); check the conversation before re-running.")
        else:
            hint = ("rate-limited: wait a minute, then re-run once" if fatal == "429"
                    else "check the bot can post there; do not retry blindly")
            print(f"\nDELIVERY FAILED: {why} — nothing was posted to {where}; {hint}.")
    if sent == 0:
        print(f"Sent 0/{total} messages to {where}")
        sys.exit(1)
    # Partial: some messages landed. Re-running would duplicate them, so exit 0 and say so.
    print(f"\nPARTIAL DELIVERY: sent {sent}/{total} messages to {where} — do NOT re-run "
          f"(it would duplicate the posted ones); tell the user which part is missing.")
    sys.exit(0)

# ── Default (terminal) output: human-readable slice view ─────────────────────
# This is what the agent sees when it runs the script via `terminal()`. The
# title line is printed first, then each section's CANONICAL content (the
# exact string `--send` would post to Discord) is shown, followed by the
# MEDIA: tag for the image. The canonical content is what an agent should
# grab verbatim for ad-hoc delivery — it is byte-identical to what --send
# uses, so there's no "decoration vs. canonical" trap.
print(f"\n{'='*95}")
print(f"## :red_car: Parking Lot Report")
print(f"Address: {address}")
print(f"Resolved: {loc['address']} | Postal: {postal}\n")
for header, body, img in sections:
    # Canonical content only — no separate `--- section ---` decoration line.
    # This is the exact string `--send` posts to Discord, so an agent grabs it
    # verbatim with no decoration-vs-canonical ambiguity. A blank line + the bold
    # header delimit each section.
    print(f"\n{format_section_content(header, body)}")
    if img and os.path.exists(img):
        print(f"MEDIA:{img}")
print(f"{'='*95}")
print(f"Map URL: {map_url[:120]}...")
if map_path and os.path.exists(map_path):
    print(f"Map saved ({os.path.getsize(map_path):,} bytes) → {map_path}")
_print_digest()
print()
print("DELIVERY: to post this report into the current Discord conversation, re-run with")
print("--send (no id) — it pairs each header with its image. Never hand-post it.")

# Verify SVY21→WGS84 transform
lat_bj10, lon_bj10 = svy21_to_wgs84(21026.3461, 40101.7077)
print(f"\nVerification — BJ10 SVY21→WGS84: ({lat_bj10:.5f}, {lon_bj10:.5f})  expected ~(1.38, 103.76) ✓" if abs(lat_bj10 - 1.38) < 0.02 else f"BROKEN: {lat_bj10}")
