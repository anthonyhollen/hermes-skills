#!/usr/bin/env python3
"""
Food Recommender — ranked restaurant suggestions based on personal FoodPanda order history.
Reads your order history from Google Sheets → builds a preference profile → queries nearby
restaurants via OSM → filters out already-ordered places + sub-4.0 Google rated → scores by
variety + cuisine affinity → top 10.

Usage:
    python3 ~/.hermes/scripts/food_recommender.py "Pasir Ris Grove, Singapore"
    python3 ~/.hermes/scripts/food_recommender.py "518141"
"""

import json, os, sys, time, urllib.request, urllib.parse, re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
SHEET_ID = "1Ml2TBE0PUHnW9JePHcK2rOjhJOUeQ7YU8sRg-J5Itn4"
def _load_google_api_key():
    """Read GOOGLE_API_KEY from ~/.hermes/.env."""
    env_path = os.path.join(HERMES_HOME, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GOOGLE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None

GOOGLE_API_KEY = _load_google_api_key()
SEARCH_RADIUS_M = 3000   # 3 km
TOP_N = 10
FUZZY_THRESHOLD = 0.70   # minimum Levenshtein ratio to count as "already ordered"
MIN_GOOGLE_RATING = 4.0
SGT = timezone(timedelta(hours=8))
CACHE_DIR = os.path.join(HERMES_HOME, "scripts", "food_cache")
CACHE_TTL_DAYS = 7   # don't repeat any restaurant within this window

# ── Weekly cache (prevent same recommendations within 7 days) ─────────────────

def _cache_path():
    return os.path.join(CACHE_DIR, "recent_recommendations.json")

def save_recommendation_cache(candidates):
    """Save this week's recommendations to avoid repeats within 7 days."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = _cache_path()
    # Load existing list, append new names, trim to TTL window
    try:
        with open(cache_path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {"date": datetime.now(SGT).strftime("%Y-%m-%d"), "candidates": []}

    # If cache is stale (>7 days old), reset — don't re-activate old entries
    try:
        cache_date = datetime.strptime(cache["date"], "%Y-%m-%d").date()
        age = (datetime.now(SGT).date() - cache_date).days
        if age > CACHE_TTL_DAYS:
            cache = {"date": datetime.now(SGT).strftime("%Y-%m-%d"), "candidates": []}
    except (KeyError, ValueError):
        cache = {"date": datetime.now(SGT).strftime("%Y-%m-%d"), "candidates": []}

    existing = cache.get("candidates", [])
    existing.extend([r["name"] for r in candidates])
    # Deduplicate while preserving order (last occurrence wins — newer entry takes priority)
    seen = set()
    unique = []
    for name in reversed(existing):
        if name not in seen:
            seen.add(name)
            unique.append(name)
    unique.reverse()   # restore original order
    cache["candidates"] = unique[:50]   # keep enough for a full week

    cache["date"] = datetime.now(SGT).strftime("%Y-%m-%d")
    with open(cache_path, "w") as f:
        json.dump(cache, f)

def get_recent_recommendations():
    """Return list of restaurant names shown in the last CACHE_TTL_DAYS, or [] if stale/missing."""
    try:
        cache_path = _cache_path()
        if not os.path.exists(cache_path):
            return []
        with open(cache_path) as f:
            cache = json.load(f)
        cache_date = datetime.strptime(cache["date"], "%Y-%m-%d").date()
        age = (datetime.now(SGT).date() - cache_date).days
        if age > CACHE_TTL_DAYS:
            return []   # stale — treat as no cache
        return cache.get("candidates", [])
    except Exception as e:
        print(f"WARNING: Could not read recommendation cache: {e}", file=sys.stderr)
        return []

# ── Google Sheets (direct API, same auth as foodpanda_sync.py) ─────────────────

_sheets_service = None

def get_sheets_service():
    global _sheets_service
    if not hasattr(_sheets_service, "_inited"):
        try:
            import googleapiclient.discovery, google.oauth2.credentials
            token_path = os.path.join(HERMES_HOME, "google_token.json")
            if not os.path.exists(token_path):
                print(f"ERROR: Google token file not found: {token_path}", file=sys.stderr)
                return None
            token_data = json.loads(open(token_path).read())
            creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
                token_data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            _sheets_service = googleapiclient.discovery.build("sheets", "v4", credentials=creds)
            _sheets_service._inited = True
        except Exception as e:
            print(f"ERROR: Failed to initialise Google Sheets API: {e}", file=sys.stderr)
            return None
    return _sheets_service

def list_sheet_tabs():
    svc = get_sheets_service()
    if svc is None:
        return []
    result = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets.properties.title"
    ).execute()
    return [s["properties"]["title"] for s in result.get("sheets", [])]

def read_tab(tab_name):
    svc = get_sheets_service()
    if svc is None:
        return []
    result = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{tab_name}'!A:C"
    ).execute()
    return result.get("values", [])

# ── Order history → preference profile ───────────────────────────────────────

KNOWN_CUISINES = {
    "sushi": "Japanese", "sashimi": "Japanese", "ramen": "Japanese", "udon": "Japanese",
    "tonkatsu": "Japanese", "tempura": "Japanese", "yakitori": "Japanese",
    "korean": "Korean", "bbq": "Korean", "samgyeopsal": "Korean", "bulgogi": "Korean",
    "chicken": "Western", "burger": "Western", "steak": "Western", "fries": "Western",
    "pizza": "Western", "pasta": "Italian", "italian": "Italian",
    "thai": "Thai", "curry": "Thai",
    "chinese": "Chinese", "dim sum": "Chinese", "dumplings": "Chinese", "bak kut teh": "Chinese",
    "nasi": "Malay", "laksa": "Malay", "satay": "Malay", "mee rebus": "Malay",
    "indian": "Indian", "naan": "Indian", "tandoori": "Indian", "roti": "Indian",
    "vietnamese": "Vietnamese", "pho": "Vietnamese",
    "noodle": "Asian",
    "seafood": "Seafood", "fish": "Seafood", "crab": "Seafood",
    "vegetarian": "Vegetarian", "vegan": "Vegetarian",
    "western": "Western", "cafe": "Cafe", "coffee": "Cafe",
    "dessert": "Dessert", "ice cream": "Dessert", "gelato": "Dessert",
    "fast food": "Fast Food", "mcdonald": "Fast Food", "kfc": "Fast Food", "burger king": "Fast Food",
    "soup": "Soup", "hotpot": "Chinese", "steamboat": "Chinese",
    "taiwanese": "Taiwanese", "bubble tea": "Taiwanese",
    "mexican": "Mexican",
    "indonesian": "Indonesian", "malay": "Malay", "singaporean": "Singaporean",
}

def extract_cuisine(merchant_name):
    name_lower = merchant_name.lower()
    for keyword, cuisine in KNOWN_CUISINES.items():
        if keyword in name_lower:
            return cuisine
    return "General"

def load_order_history():
    """Return list of (date_str, merchant, amount)."""
    tabs = list_sheet_tabs()
    orders = []
    for tab in tabs:
        rows = read_tab(tab)
        for row in rows[1:]:  # skip header
            if len(row) < 3:
                continue
            date_str = str(row[0]).strip()
            merchant = str(row[1]).strip()
            if not date_str or not merchant:
                continue
            try:
                amount = float(str(row[2]).replace(",", "").replace("S$", "").strip())
            except ValueError:
                continue
            orders.append((date_str, merchant, amount))
    # Deduplicate by (normalized_merchant, amount) per SKILL.md
    seen = set()
    unique_orders = []
    for order in orders:
        key = (order[1].lower().strip(), order[2])
        if key not in seen:
            seen.add(key)
            unique_orders.append(order)
    return unique_orders

def build_profile(orders):
    """Return (merchant_freq, cuisine_counts, recent_cuisines, top_cuisines)."""
    from collections import Counter

    merchant_freq = Counter()
    cuisine_counts = Counter()
    recent_cuisines = set()   # cuisines ordered in last 30 days

    now = datetime.now(SGT)
    thirty_days_ago = (now - timedelta(days=30)).replace(tzinfo=None)

    for date_str, merchant, amount in orders:
        merchant_freq[merchant] += 1
        cuisine = extract_cuisine(merchant)
        cuisine_counts[cuisine] += 1
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            try:
                d = datetime.strptime(date_str, "%d %b %Y")
            except ValueError:
                continue
        # Compare naive datetimes
        d_naive = d.replace(tzinfo=None) if d.tzinfo else d
        if d_naive >= thirty_days_ago:
            recent_cuisines.add(cuisine)

    top_cuisines = [c for c, _ in cuisine_counts.most_common()]
    return merchant_freq, cuisine_counts, recent_cuisines, top_cuisines

# ── Geocoding (Nominatim) ─────────────────────────────────────────────────────

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_last_nominatim_call = 0

def geocode(address):
    global _last_nominatim_call
    elapsed = time.time() - _last_nominatim_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_nominatim_call = time.time()

    url = f"{NOMINATIM_URL}?q={urllib.parse.quote(address)}&format=json&limit=1&countrycodes=sg"
    req = urllib.request.Request(url, headers={"User-Agent": "HermesAgent/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        results = json.loads(resp.read())
    if not results:
        raise ValueError(f"Geocoding failed for: {address}")
    r = results[0]
    return float(r["lat"]), float(r["lon"]), r.get("display_name", address)

# ── Nearby restaurants (Overpass API) ────────────────────────────────────────

OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

def haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def search_restaurants(lat, lon, radius_m=SEARCH_RADIUS_M):
    overpass_q = f"""
[out:json][timeout:25];
(
  node["amenity"="restaurant"](around:{radius_m},{lat},{lon});
  way["amenity"="restaurant"](around:{radius_m},{lat},{lon});
  node["amenity"="fast_food"](around:{radius_m},{lat},{lon});
  way["amenity"="fast_food"](around:{radius_m},{lat},{lon});
);
out center;
"""
    last_err = None
    for url in OVERPASS_URLS:
        try:
            data_encoded = urllib.parse.urlencode({"data": overpass_q}).encode()
            req = urllib.request.Request(url, data=data_encoded, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "HermesAgent/1.0"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            break
        except Exception as e:
            last_err = e
            continue
    else:
        raise RuntimeError(f"All Overpass endpoints failed: {last_err}")

    restaurants = []
    for el in result.get("elements", []):
        if el["type"] == "node":
            lat2, lon2 = el["lat"], el["lon"]
        elif el["type"] == "way" and "center" in el:
            lat2, lon2 = el["center"]["lat"], el["center"]["lon"]
        else:
            continue

        dist = haversine_m(lat, lon, lat2, lon2)
        tags = el.get("tags", {})
        name = tags.get("name", "")
        cuisine_raw = tags.get("cuisine", "")
        cuisine = cuisine_raw.split(";")[0].strip() if cuisine_raw else "General"

        restaurants.append({
            "name": name or "Unnamed",
            "lat": lat2,
            "lon": lon2,
            "dist_m": int(dist),
            "dist_km": round(dist / 1000, 2),
            "cuisine": cuisine,
            "tags": tags,
        })
    return restaurants

# ── Fuzzy matching ────────────────────────────────────────────────────────────

def fuzzy_ratio(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def is_already_ordered(name, merchant_freq, threshold=FUZZY_THRESHOLD):
    for merchant in merchant_freq:
        if fuzzy_ratio(name, merchant) >= threshold:
            return True
    return False

# ── Google Places API (rating lookup) ─────────────────────────────────────────

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

def get_google_rating(name, lat, lon):
    """Look up a restaurant's Google rating via Places API text search.
    Returns (rating, places_ref) or (None, None) if not found / below threshold."""
    try:
        query = f"{name} Singapore"
        params = urllib.parse.urlencode({
            "key": GOOGLE_API_KEY,
        })
        body = json.dumps({
            "textQuery": query,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": 300
                }
            },
            "includedType": "restaurant",
            "maxResultCount": 3,
        }).encode()

        req = urllib.request.Request(
            f"{PLACES_SEARCH_URL}?{params}",
            data=body,
            headers={
                "X-Goog-FieldMask": "places.displayName,places.rating,places.id",
                "Content-Type": "application/json",
                "User-Agent": "HermesAgent/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        places = data.get("places", [])
        if not places:
            return None, None

        # Pick the best match by name similarity
        best = None
        best_ratio = 0
        for p in places:
            p_name = p.get("displayName", {}).get("text", "")
            ratio = fuzzy_ratio(name, p_name)
            if ratio > best_ratio:
                best_ratio = ratio
                best = p

        if best is None:
            return None, None

        rating = best.get("rating")
        return (rating, best.get("id")) if rating is not None else (None, None)

    except Exception as e:
        print(f"    [Places API error for '{name}': {e}]", file=sys.stderr)
        return None, None


def check_rating_batch(restaurants_batch, checked):
    """Given a batch of restaurants, check Google ratings for any not yet cached.
    Returns list of (restaurant, rating) tuples where rating >= MIN_GOOGLE_RATING."""
    valid = []
    for r in restaurants_batch:
        name = r["name"]
        if name in checked:
            rating = checked[name][0]
        else:
            rating, place_id = get_google_rating(name, r["lat"], r["lon"])
            checked[name] = (rating, place_id)
            if rating is not None:
                print(f"    {name}: {rating} ★")
            else:
                print(f"    {name}: no rating found")
            time.sleep(0.2)

        if rating is not None and rating >= MIN_GOOGLE_RATING:
            r["google_rating"] = rating
            r["place_id"] = checked[name][1]
            valid.append(r)
    return valid


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_all(restaurants, merchant_freq, cuisine_counts, recent_cuisines, top_cuisines,
              recent_recos=None):
    """
    Score ALL restaurants by 'newness' + cuisine variety/affinity (distance deprioritised).
    Does NOT call Google Places API here — just ranking.
    recent_recos: list of restaurant names to skip (shown in last 7 days).
    Returns sorted list (best score first).
    """
    if recent_recos is None:
        recent_recos = []
    skip_names = set(recent_recos)

    scored = []
    for r in restaurants:
        name = r["name"]
        if is_already_ordered(name, merchant_freq):
            continue
        if name in skip_names:
            continue

        cuisine = r["cuisine"]

        # "General" is a sentinel for unclassifiable — don't reward it
        if cuisine == "General":
            variety_bonus = 0
            affinity_bonus = 0
            recency_penalty = 0
        else:
            # Cuisine variety bonus: +10 pts if cuisine not in top-5 most-ordered
            variety_bonus = 10 if cuisine not in top_cuisines[:5] else 0
            # Cuisine affinity bonus: +5 pts if cuisine IS in top cuisines
            affinity_bonus = 5 if cuisine in top_cuisines[:10] else 0
            # Recency penalty: -5 pts if ordered in last 30 days
            recency_penalty = -5 if cuisine in recent_cuisines else 0
        # Distance tiebreaker (0–2 pts)
        dist_tiebreak = max(0, (3.0 - r["dist_km"]) * 0.67)

        total = variety_bonus + affinity_bonus + recency_penalty + dist_tiebreak
        r["score"] = round(total, 1)
        scored.append(r)

    scored.sort(key=lambda x: (x["score"], -x["dist_m"]), reverse=True)
    return scored


def fill_top_n(scored, checked):
    """
    Two-pass fill:
    1. Take top 10 from scored list → check Google ratings
    2. Any below 4.0★ → replace with next-best from scored list (11th, 12th, ...)
    3. Keep going until 10 valid recommendations or list exhausted
    Returns up to TOP_N validated recommendations.
    """
    BATCH_SIZE = 10
    result = []
    result_keys = set()
    scored_idx = 0

    while len(result) < TOP_N and scored_idx < len(scored):
        # Grab next batch starting from scored_idx
        batch = scored[scored_idx : scored_idx + BATCH_SIZE]
        scored_idx += BATCH_SIZE

        valid = check_rating_batch(batch, checked)
        for r in valid:
            # Dedup by (name, lat, lon) — different locations of same chain are distinct
            key = (r["name"], round(r["lat"], 4), round(r["lon"], 4))
            if key not in result_keys:
                result.append(r)
                result_keys.add(key)
                if len(result) >= TOP_N:
                    break

    return result


def score_candidates(restaurants, merchant_freq, cuisine_counts, recent_cuisines, top_cuisines,
                     recent_recos=None):
    """
    Full pipeline: score all → two-pass fill → return top N validated recommendations.
    recent_recos: list of restaurant names to skip (shown in last 7 days).
    """
    checked = {}   # name → (rating, place_id)

    print(f"  Scoring {len(restaurants)} OSM restaurants (excluding already-ordered)...")
    scored = score_all(restaurants, merchant_freq, cuisine_counts, recent_cuisines,
                       top_cuisines, recent_recos)
    print(f"  → {len(scored)} candidates after scoring")

    print(f"  Two-pass fill: checking top candidates until ≥4.0★ threshold met...")
    candidates = fill_top_n(scored, checked)
    print(f"  → {len(candidates)} validated recommendations")

    return candidates, len(restaurants)

# ── Delivery platform heuristic ──────────────────────────────────────────────

CHAIN_KEYWORDS = [
    "sushi", "ramen", "pizza", "burger", "kfc", "mcdonald", "starbucks", "subway",
    "fish", "popeyes", "long john", "dunkin", "coca", "panda",
    "express", "junction", "garden", "house", "cafe", "kitchen",
    "ichiban", "soban", "tonkatsu", "yakitori", "bbq", "genki", "mos burger",
    "maki", "sakae", "sushi king", "pringles", "pizza hut", "domino",
]

def likely_on_foodpanda(name):
    name_lower = name.lower()
    return any(kw in name_lower for kw in CHAIN_KEYWORDS)

def get_delivery_platforms(name):
    platforms = []
    if likely_on_foodpanda(name):
        platforms.append("FoodPanda")
    if any(c in name.lower() for c in ["food", "express", "kitchen", "cafe", "restaurant", "house"]):
        platforms.append("GrabFood")
    return platforms if platforms else ["GrabFood"]

# ── Output ───────────────────────────────────────────────────────────────────

def format_results(candidates, location_name, total_searched, radius_km=3):
    if not candidates:
        return (
            f"**🍽️ No recommendations near {location_name}**\n\n"
            f"(Searched {total_searched} restaurants within {radius_km}km — none met the ≥4.0★ Google rating filter. "
            f"Try a different location or lower the minimum rating threshold.)"
        )

    lines = [
        f"**🍽️ Food Recommendations near {location_name}**",
        f"_Based on your order history · ≥4.0★ Google rating · top {len(candidates)} shown_\n"
    ]

    for i, r in enumerate(candidates, 1):
        cuisine = r["cuisine"]
        platforms = get_delivery_platforms(r["name"])
        platform_count = len(platforms)
        platform_str = " · ".join(platforms)
        count_str = f" ({platform_count}/{platform_count})" if platform_count > 1 else ""
        maps_url = f"https://www.google.com/maps/search/?api=1&query={r['lat']},{r['lon']}"
        dir_url = f"https://www.google.com/maps/dir/?api=1&destination={r['lat']},{r['lon']}"
        stars = "★" * int(r["google_rating"]) + "☆" * (5 - int(r["google_rating"]))

        lines.append(f"**{i}. {r['name']}** :round_pushpin: {r['dist_km']}km · {cuisine} · {stars} {r['google_rating']}")
        lines.append(f"[Maps](<{maps_url}>) | [Directions](<{dir_url}>) | :takeout_box: {platform_str}{count_str}")

    return "\n".join(lines)

# ── Discord delivery ─────────────────────────────────────────────────────────

DISCORD_CHANNEL_ID = "1499972245437550602"
DISCORD_MSG_LIMIT = 1990  # safe margin under Discord's 2000-char hard limit

def _load_discord_token():
    """Read DISCORD_BOT_TOKEN from ~/.hermes/.env."""
    env_path = os.path.join(HERMES_HOME, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None

def _discord_post(token, channel_id, content, _retries=0, _max_retries=5):
    """Post a text message to Discord. Returns True on success. Max 5 retries on 429."""
    if not content or not content.strip():
        return True
    data = json.dumps({
        "content": content[:2000],
        "allowed_mentions": {"parse": []}
    }).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesAgent/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 429:
            if _retries >= _max_retries:
                print(f"  [discord] Rate limited {_max_retries} times, giving up", file=sys.stderr)
                return False
            try:
                retry_after = json.loads(e.read()).get("retry_after", 1)
            except Exception:
                retry_after = 1
            time.sleep(retry_after + 0.5)
            return _discord_post(token, channel_id, content, _retries + 1, _max_retries)
        print(f"  [discord] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [discord] {e}", file=sys.stderr)
        return False

def split_and_post(token, channel_id, text):
    """Split text on newline boundaries (≤1990 chars each) and post each chunk."""
    if not text or not text.strip():
        return True

    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = current + "\n" + line if current else line
        if len(candidate) > DISCORD_MSG_LIMIT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)

    all_ok = True
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.5)
        if not _discord_post(token, channel_id, chunk):
            all_ok = False
            print(f"  [discord] Failed to send chunk {i+1}/{len(chunks)}", file=sys.stderr)
    return all_ok

def post_results(output):
    """Post formatted recommendations to Discord."""
    token = _load_discord_token()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN not found in ~/.hermes/.env", file=sys.stderr)
        return False
    return split_and_post(token, DISCORD_CHANNEL_ID, output)

def fail(error_msg):
    """Post error notification to Discord, then exit."""
    token = _load_discord_token()
    if token:
        _discord_post(token, DISCORD_CHANNEL_ID, f"**⚠️ Food recommendation failed**\n_{error_msg}_")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Parse flags
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")
    if not args:
        print("Usage: food_recommender.py <address_or_postal_code> [--dry-run]")
        sys.exit(1)

    location = " ".join(args)

    if not GOOGLE_API_KEY:
        fail("GOOGLE_API_KEY not found in ~/.hermes/.env")
        sys.exit(1)

    print(f"📍 Location: {location}")

    try:
        # 1. Load order history
        print("📊 Loading order history from Google Sheet...")
        orders = load_order_history()
        print(f"   Found {len(orders)} orders")

        if not orders:
            print("WARNING: No orders found — will return unfiltered nearby restaurants.")

        merchant_freq, cuisine_counts, recent_cuisines, top_cuisines = build_profile(orders)
        print(f"   Unique merchants: {len(merchant_freq)}")
        print(f"   Top cuisines: {top_cuisines[:8]}")
        print(f"   Recent (30d) cuisines: {recent_cuisines}")

        # 2. Geocode
        print(f"🗺️  Geocoding '{location}'...")
        lat, lon, display_name = geocode(location)
        print(f"   Coordinates: {lat:.6f}, {lon:.6f}")

        # 3. Search nearby restaurants
        actual_radius_km = SEARCH_RADIUS_M / 1000
        print(f"🔍 Searching restaurants within {actual_radius_km:.0f}km...")
        restaurants = search_restaurants(lat, lon, SEARCH_RADIUS_M)
        print(f"   Found {len(restaurants)} restaurants in OSM")
        if not restaurants:
            fallback_m = 5000
            actual_radius_km = fallback_m / 1000
            print(f"   ⚠️ 0 results at {SEARCH_RADIUS_M}m — widening to {actual_radius_km:.0f}km")
            restaurants = search_restaurants(lat, lon, fallback_m)
            print(f"   Found {len(restaurants)} restaurants at {actual_radius_km:.0f}km")

        # 4. Score and rank (with Google rating filter)
        recent_recos = get_recent_recommendations()
        if recent_recos:
            print(f"  ⏭️  Skipping recent recommendations: {recent_recos}")
        print("🏆 Scoring, filtering ≥4.0★, and ranking...")
        candidates, total_searched = score_candidates(
            restaurants, merchant_freq, cuisine_counts, recent_cuisines, top_cuisines,
            recent_recos=recent_recos
        )
        print(f"   → {len(candidates)} recommendations after filtering and scoring")

        # 5. Format and post to Discord (or print in dry-run mode)
        short_location = display_name.split(",")[0]
        output = format_results(candidates, short_location, total_searched, radius_km=actual_radius_km)
        if dry_run:
            print(output)
            print("[DRY RUN] Output printed to stdout, not posted to Discord", file=sys.stderr)
        elif post_results(output):
            print("✅ Posted to Discord", file=sys.stderr)
            # Save cache ONLY after successful delivery — prevents silent eat of recommendations
            if candidates:
                save_recommendation_cache(candidates)
        else:
            print("⚠️ Discord delivery failed (see errors above)", file=sys.stderr)
            print("  [cache] Skipped — recommendations will reappear tomorrow", file=sys.stderr)

    except Exception as e:
        print(f"ERROR: food_recommender failed: {e}", file=sys.stderr)
        fail(str(e))
        sys.exit(1)

if __name__ == "__main__":
    main()