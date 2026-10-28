#!/usr/bin/env python3
"""
DeepSeek discovery regression suite (v2.19) — run BEFORE trusting any change to
spotify-refresh/src/llm_discovery.py, the llm parts of composer.py, or the
llm_discovery / playlist config blocks.

  1. Matching — compact names ('CHUNG HA' == 'Chungha'), dual-language titles
     ('선물 (Gift)'), near misses, the search-result preference order.
  2. Guards — no J-pop (kana, Japanese versions, Japan albums, blocked names),
     versions / intros / short tracks.
  3. Model replies — fenced / chatty / malformed json, duplicates, non-Korean
     origins, too few picks, the thinking → non-thinking retry, no key in errors.
  4. Batches — a throwaway SQLite DB: a full run with faked DeepSeek + Spotify,
     the once-per-6h skip, the lease, the 02:50–03:25 guard.
  5. Composer — 24/12/4 with the mix layout, one song per new artist, heard /
     known-artist re-checks, shortfall → favourites + note, a broken store, and
     the enabled: false rollback (no artist-graph calls while enabled).

Usage:  python3 scripts/test_llm_discovery.py        Exit 0 on success, 1 on failure.
Needs only the stdlib + PyYAML (the skill's own deps); no network, no live DB.
"""
import json, os, sys, tempfile, time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SRC = SKILL / "spotify-refresh" / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="llm-disc-test-")
db.DB_PATH = os.path.join(_TMP.name, "test.db")
db.init_db()

import client, composer, llm_discovery as L  # noqa: E402
_LOAD_CONFIG_ORIG = L.load_config      # the real loader (tests wrap L.load_config later)
import urllib.error  # noqa: E402
NET_CALLS: list[str] = []              # tripwire: the suite must never reach DeepSeek (it bills)
def _no_network(req, timeout=None):
    NET_CALLS.append(getattr(req, "full_url", str(req)))
    raise urllib.error.URLError("network disabled in tests")
L._OPENER.open = _no_network

FAILS: list[str] = []
PASSES = [0]


def check(name: str, cond, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(f"{name}: {detail}")


CFG = dict(L.load_config())
CFG.update(blocked_artist_names=["tuki.", "YOASOBI"], min_duration_s=90, picks_requested=40,
           similar_artists_requested=25, min_valid_picks=10)


def T(tid, name, artists, album="Album", album_type="single", release="2024-05-01", dur=200000, playable=True):
    """A Spotify search-result track."""
    return {"id": tid, "uri": f"spotify:track:{tid}", "name": name, "duration_ms": dur, "is_playable": playable,
            "artists": [{"id": aid, "name": an} for aid, an in artists],
            "album": {"name": album, "album_type": album_type, "release_date": release}}


def aid(n: int) -> str:
    return f"{n:022d}"[-22:].replace("0", "A")      # a base62-looking 22-char id


# ── 1. matching ──────────────────────────────────────────────────────────────
check("compact CHUNG HA", L.compact("CHUNG HA") == L.compact("Chungha"))
check("compact full-width", L.compact("ＬＥＥ　ＨＩ") == L.compact("Lee Hi"))
check("compact keeps hangul", L.compact("선물") == "선물")
check("dual title", L._keys("Gift") & L._keys("선물 (Gift)"))
check("feat stripped", L._keys("Song") & L._keys("Song (feat. Somebody)"))
check("dash suffix stripped", L._keys("Song") & L._keys("Song - Korean Ver."))
check("(G)I-DLE vs i-dle", L._artist_match("(G)I-DLE", ["i-dle"]))
check("artist with paren", L._artist_match("Kim Min Seok", ["Kim Min Seok (MeloMance)"]))
check("different artist", not L._artist_match("Lee Hi", ["Lee Hyori"]))

pick = {"artist": "Chungha", "title": "Gotta Go", "year": 2019}
items = [T(aid(1), "Gotta Go (Japanese Ver.)", [(aid(9), "CHUNG HA")], album="Gotta Go Japan"),
         T(aid(2), "Gotta Go", [(aid(9), "CHUNG HA")], album_type="compilation", release="2020-01-01"),
         T(aid(3), "Gotta Go", [(aid(9), "CHUNG HA")], release="2019-01-02")]
best, why = L.pick_best(pick, items, CFG)
check("pick_best prefers clean single", best and best["id"] == aid(3), f"{best and best['id']} {why}")
best, why = L.pick_best({"artist": "Yerin Baek", "title": "It's Not Your Fault", "year": 2023},
                        [T(aid(4), "Maybe It's Not Our Fault", [(aid(8), "Yerin Baek")])], CFG)
check("near miss rejected", best is None and why == "near_miss", why)
best, why = L.pick_best({"artist": "Lee Hi", "title": "Alley", "year": 2024},
                        [T(aid(5), "Alley", [(aid(7), "Someone Else")])], CFG)
check("wrong artist rejected", best is None and why == "no_match", why)
best, why = L.pick_best({"artist": "X", "title": "Song", "year": None},
                        [T(aid(6), "Song (Sped Up)", [(aid(6), "X")], dur=150000)], CFG)
check("only a sped-up version → rejected", best is None and why == "version", why)

check("bracket-only match rejected", not L._name_match("Lonely Night (feat. pH-1)", "Jenga (feat. pH-1)"))
check("Korean Ver. suffix alone rejected", not L._name_match("IZNA (Korean Ver.)", "SIGN (Korean Ver.)"))
check("artist 'A & B' splits", L._artist_match("Heize & Dean", ["DEAN"]))
check("IVE is not FIVE", not L._artist_match("IVE", ["FIVE"]))
best, why = L.pick_best({"artist": "Y", "title": "Igloo", "year": None},
                        [T(aid(11), "Igloo - Instrumental", [(aid(12), "Y")]),
                         T(aid(13), "Igloo (Korean Ver.)", [(aid(12), "Y")])], CFG)
check("pick_best skips instrumental", best and best["id"] == aid(13), f"{best and best['name']} {why}")
best, why = L.pick_best({"artist": "Y", "title": "Igloo", "year": 2024},
                        [T(aid(14), "Igloo", [(aid(12), "Y")], release="2024-01-01", playable=False),
                         T(aid(15), "Igloo", [(aid(12), "Y")], album_type="compilation", release="2025-01-01")], CFG)
check("pick_best skips unplayable", best and best["id"] == aid(15), f"{best and best['id']} {why}")

# ── 2. guards ────────────────────────────────────────────────────────────────
blocked = L._blocked_names(CFG)
check("kana title", L.is_japanese("サクラキミワタシ", ["Someone"]))
check("half-width katakana", L.is_japanese("ｻｸﾗ", ["Someone"]))
check("kana artist", L.is_japanese("Song", ["あいみょん"]))
check("Japanese Ver.", L.is_japanese("Song (Japanese Ver.)", ["IVE"]))
check("Japanese Version", L.is_japanese("Really Like You (Japanese Version)", ["Gyubin"]))
check("JP ver", L.is_japanese("Song - JP ver.", ["Kep1er"]))
check("Japan album", L.is_japanese("Song", ["TWICE"], "TWICE JAPAN 1st SINGLE"))
check("blocked artist", L.is_japanese("Song", ["tuki."], "", blocked))
check("blocked artist compact", L.is_japanese("Song", ["YOASOBI"], "", blocked))
check("hangul passes", not L.is_japanese("선물", ["멜로망스"], "", blocked))
check("english passes", not L.is_japanese("Magnetic", ["ILLIT"], "SUPER REAL ME", blocked))
check("XG passes", not L.is_japanese("IYKYK", ["XG"], "AWE", blocked))
check("kanji-only passes", not L.is_japanese("花", ["Someone"]))
check("intro", L.bad_version("Intro : Two Rabbits", 200000, 90))
check("interlude", L.bad_version("Interlude", 200000, 90))
check("Introduce passes", not L.bad_version("Introduce Me", 200000, 90))
check("sped up", L.bad_version("Song (Sped Up Ver.)", 150000, 90))
check("slowed", L.bad_version("Song - slowed + reverb", 150000, 90))
check("short", L.bad_version("Song", 80000, 90))
check("normal passes", not L.bad_version("Song", 180000, 90))

check("kanji-only artist", L.is_japanese("x", ["乃木坂46"]))
check("hangul artist passes", not L.is_japanese("x", ["태연"]))
check("(Japanese) marker", L.is_japanese("Song (Japanese)", ["A"]))
check("JPN Ver.", L.is_japanese("Song - JPN Ver.", ["A"]))
check("BLACKPINK LISA not blocked", not L.is_japanese("SG", ["DJ Snake", "LISA"], "", L._blocked_names(L.load_config())))
check("middle dot is not kana", not L.is_japanese("Love・Hate", ["A"]))
for v in ("Song (English Ver.)", "Song (Acoustic Ver.)", "Song - Piano Version", "Song (Chinese Ver.)", "Song (Remastered)"):
    check(f"version tag {v}", L.bad_version(v, 200000, 90))
for v in ("POSE! - Kawaii ver.", "Song (2024 ver.)", "Song - Holiday Version"):
    check(f"alternate ver. tag {v}", L.bad_version(v, 200000, 90))
for v in ("Piano Man", "Band of Brothers", "English Teacher", "Song (Korean Ver.)", "Song (Original Ver.)", "Song (Ver. 2)"):
    check(f"not a version tag {v}", not L.bad_version(v, 200000, 90))
check("base_sig ties versions", L.base_sig({"name": "X (English Ver.)", "artist_ids": ["a"]})
      == L.base_sig({"name": "X", "artist_ids": ["a"]}))
check("origin variants", [L._korean_origin(o) for o in ("KR", "South Korea", "ROK", None, "JP", "Japan")]
      == [True, True, True, True, False, False])
_sgt = lambda hhmm: int(__import__("datetime").datetime.strptime(f"2026-10-07 {hhmm}", "%Y-%m-%d %H:%M")
                        .replace(tzinfo=L.SGT).timestamp() * 1000)
check("start guard: 02:42 + 520s budget refused", L._in_refresh_window(_sgt("02:42"), 520))
check("start guard: 02:20 allowed", not L._in_refresh_window(_sgt("02:20"), 520))
_envtmp = os.path.join(_TMP.name, "env")
with open(_envtmp, "w") as f:
    f.write("# c\nexport DEEPSEEK_API_KEY=sk-abc # inline\nDEEPSEEK_BASE_URL='https://x.example/'\n")
_old_env = L.ENV_PATH
L.ENV_PATH = _envtmp
check(".env export + inline comment + quotes", L._deepseek_creds() == ("sk-abc", "https://x.example"), L._deepseek_creds())
L.ENV_PATH = _old_env

# ── 3. model replies ─────────────────────────────────────────────────────────
def picks_json(n, **extra):
    return {"picks": [{"artist": f"Artist {i}", "title": f"Song {i}", "year": 2023, "origin": "KR",
                       "fit": "adjacent" if i % 3 == 0 else "close", "tier": "rookie", "why": "w"}
                      for i in range(n)], **extra}

ok = L.parse_llm_json("```json\n" + json.dumps(picks_json(12)) + "\n```", CFG)
check("fenced json", len(ok["picks"]) == 12)
ok = L.parse_llm_json("Sure! Here you go: " + json.dumps(picks_json(12)) + " Enjoy.", CFG)
check("chatty json", len(ok["picks"]) == 12)
dup = picks_json(12)
dup["picks"].append({"artist": "ARTIST 0", "title": "Other", "origin": "KR"})
dup["picks"].append({"artist": "Jp Act", "title": "x", "origin": "JP"})
dup["picks"] += ["junk", None, {"artist": 5, "title": ["x"]}, {"artist": "", "title": "y"}]
dup["similar_artists"] = ["Plain Name", {"artist": "With Song", "known_song": "S"}, {"artist": "plain name"}, 7]
ok = L.parse_llm_json(json.dumps(dup), CFG)
check("dup artist / JP origin / junk dropped", len(ok["picks"]) == 12, len(ok["picks"]))
check("similar parsed + deduped", [s["artist"] for s in ok["similar"]] == ["Plain Name", "With Song"], ok["similar"])
big = picks_json(12)
big["picks"][0]["title"] = "x" * 10000
ok = L.parse_llm_json(json.dumps(big), CFG)
check("long strings capped", len(ok["picks"][0]["title"]) <= 100)
for bad in ("", "no json here", "{not json}", json.dumps({"picks": "nope"}), json.dumps(picks_json(3))):
    try:
        L.parse_llm_json(bad, CFG)
        check(f"bad reply raises: {bad[:20]!r}", False, "no error")
    except L.LLMError:
        check("bad reply raises", True)

calls = []
def fake_chat_factory(replies):
    def fake_chat(messages, cfg, thinking, timeout_s):
        calls.append(thinking)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return fake_chat

orig_chat = L._chat
good = {"content": json.dumps(picks_json(15)), "finish_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 2000}}
for label, first in (("empty", {"content": "", "finish_reason": "stop", "usage": {}}),
                     ("length", {"content": "{\"picks\": [", "finish_reason": "length", "usage": {}}),
                     ("timeout", L.LLMError("no response within 200s"))):
    calls.clear()
    L._chat = fake_chat_factory([first, dict(good)])
    attempts = []
    parsed = L.ask_llm([], CFG, L._Budget(500), attempts)
    check(f"{label} → non-thinking retry", calls == [True, False] and parsed["thinking"] is False
          and len(parsed["picks"]) == 15, f"{calls} {attempts}")
calls.clear()
L._chat = fake_chat_factory([L.LLMError("HTTP 500: x"), L.LLMError("HTTP 500: y")])
try:
    L.ask_llm([], CFG, L._Budget(500), [])
    check("both attempts fail → LLMError", False, "no error")
except L.LLMError as e:
    check("both attempts fail → LLMError", "full" in str(e) and "fallback" in str(e), str(e))
# v2.22: a cut-off / timed-out thinking reply → ONE smaller thinking retry (never a bigger budget)
seen_cfg = []
def fake_chat_cfg(replies):
    def fake_chat(messages, cfg, thinking, timeout_s):
        calls.append(thinking)
        seen_cfg.append((messages, cfg["max_tokens"], cfg["picks_requested"], timeout_s))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return fake_chat
for label, first in (("cut off", {"content": "{\"picks\": [", "finish_reason": "length", "usage": {}}),
                     ("unusable", {"content": json.dumps(picks_json(3)), "finish_reason": "stop", "usage": {}})):
    calls.clear(); seen_cfg.clear()
    L._chat = fake_chat_cfg([first, {"content": json.dumps(picks_json(15)), "finish_reason": "stop", "usage": {}}])
    attempts = []
    parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(500), attempts, ["SMALL"])
    check(f"{label} → smaller THINKING retry", calls == [True, True] and parsed["stage"] == "small"
          and seen_cfg[1][0] == ["SMALL"] and seen_cfg[1][1] == CFG["retry_max_tokens"] < CFG["max_tokens"]
          and len(parsed["picks"]) == CFG["retry_picks"] and seen_cfg[1][3] <= CFG["retry_timeout_s"],
          (calls, [c[1:] for c in seen_cfg], len(parsed["picks"])))
    check(f"{label}: attempts record their stage", [a["stage"] for a in attempts] == ["full", "small"], attempts)
calls.clear(); seen_cfg.clear()
L._chat = fake_chat_cfg([L.LLMError("no response within 250s"), dict(good)])
parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(500), [], ["SMALL"])
check("timed out → NO small retry (DeepSeek is slow) → fallback", calls == [True, False] and parsed["stage"] == "fallback", calls)
calls.clear(); seen_cfg.clear()
L._chat = fake_chat_cfg([{"content": "", "finish_reason": "length", "usage": {}}, L.LLMError("no response within 150s"), dict(good)])
parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(500), [], ["SMALL"])
check("small retry fails → non-thinking fallback on the full request", calls == [True, True, False]
      and parsed["stage"] == "fallback" and seen_cfg[2][0] == ["FULL"], (calls, parsed.get("stage")))
calls.clear(); seen_cfg.clear()
L._chat = fake_chat_cfg([{"content": "", "finish_reason": "length", "usage": {}}, dict(good)])
parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(250), [], ["SMALL"])
check("small retry holds back the fallback's time + ~2 min for Spotify",
      calls == [True, True] and seen_cfg[1][3] <= 250 - CFG["fallback_timeout_s"] - 120 + 1, [c[3] for c in seen_cfg])
calls.clear()
L._chat = fake_chat_cfg([{"content": "", "finish_reason": "length", "usage": {}}, dict(good)])
parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(200), [], ["SMALL"])
check("too little time for the small retry → straight to the fallback", calls == [True, False]
      and parsed["stage"] == "fallback", calls)
calls.clear()
L._chat = fake_chat_cfg([{"content": "", "finish_reason": "length", "usage": {}},
                         {"content": json.dumps(picks_json(9)), "finish_reason": "stop", "usage": {}}])
parsed = L.ask_llm(["FULL"], dict(CFG, thinking=True), L._Budget(500), [], ["SMALL"])
check("small retry accepts 9 of 12 valid picks", parsed["stage"] == "small" and len(parsed["picks"]) == 9, calls)
check("max_tokens is never raised by a retry", all(c[1] <= CFG["max_tokens"] for c in seen_cfg))
rs = L.reasoning_share([{"stage": "full", "thinking": True, "error": "reply cut off (max_tokens)",
                         "usage": {"completion_tokens": 48000, "completion_tokens_details": {"reasoning_tokens": 47000}}}], 48000)
check("reasoning share of the ceiling", rs == {"tokens": 47000, "max_tokens": 48000, "share": 0.979, "cut_off": True}, rs)
check("reasoning share: old attempts without a stage count as full",
      L.reasoning_share([{"thinking": True, "usage": {"completion_tokens_details": {"reasoning_tokens": 24000}}}], 48000)["share"] == 0.5)
check("reasoning share: no thinking attempt → None", L.reasoning_share([{"thinking": False, "usage": {}}], 48000) is None)
L._chat = orig_chat
# the thinking call carries reasoning_effort; the non-thinking retry does not
_bodies = []
_pj = L._post_json
L._post_json = lambda url, body, key, timeout: (_bodies.append(body), {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}})[1]
_dc = L._deepseek_creds
L._deepseek_creds = lambda: ("k", "https://x")
L._chat([], dict(CFG, reasoning_effort="low"), True, 5)
L._chat([], dict(CFG, reasoning_effort="low"), False, 5)
check("thinking call sends reasoning_effort", _bodies[0].get("reasoning_effort") == "low" and "thinking" not in _bodies[0], _bodies[0])
check("non-thinking call: thinking disabled, no effort", _bodies[1].get("thinking") == {"type": "disabled"}
      and "reasoning_effort" not in _bodies[1], _bodies[1])
L._post_json, L._deepseek_creds = _pj, _dc

import urllib.request, urllib.error  # noqa: E402
_opener_open = L._OPENER.open
def boom(req, timeout=None):
    raise urllib.error.URLError("connection refused to https://api.deepseek.com with secret-key-123")
L._OPENER.open = boom                  # _post_json's opener (never the real network in tests)
try:
    L._post_json("https://api.deepseek.com/chat/completions", {}, "secret-key-123", 5)
    check("network error raises", False, "no error")
except L.LLMError as e:
    check("error text has no key", "secret-key-123" not in str(e), str(e))
L._OPENER.open = _opener_open
check("cost", abs(L._cost({"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000, "completion_tokens": 10000})
                  - (1000 * L.PRICE_IN_MISS + 10000 * L.PRICE_OUT)) < 1e-9)
check("cost matches the user's console (10-12 17–18h: 93K in + 564K out = $0.77)",
      abs(L._cost({"prompt_cache_miss_tokens": 93038, "completion_tokens": 563890}) - 0.77) < 0.02,
      L._cost({"prompt_cache_miss_tokens": 93038, "completion_tokens": 563890}))

# ── 4. batches (temp DB) ─────────────────────────────────────────────────────
NOW = int(time.time() * 1000)
HEARD_ARTIST = aid(100)
with db.get_conn() as conn:
    # a heard track by HEARD_ARTIST (known artist) and one by an excluded baby-sleep name
    conn.execute("INSERT INTO track_cache VALUES (?,?,?,?,?,?)", ("heardtrack0000000000001", "Known Song",
                 json.dumps([HEARD_ARTIST]), 200000, NOW, "A"))
    conn.execute("INSERT INTO play_events (track_id, played_at, ms_played, duration_ms) VALUES (?,?,?,?)",
                 ("heardtrack0000000000001", NOW - 3600000, 200000, 200000))
db.upsert_artist_names([(HEARD_ARTIST, "Known Act", "api")])

ARTISTS = {f"Artist {i}": aid(200 + i) for i in range(40)}
ARTISTS["Known Act"] = HEARD_ARTIST
search_calls = []

def fake_search(q, kind, token, market, cfg):
    search_calls.append(q)
    if kind == "artist":
        return []
    for name, a in ARTISTS.items():
        if f'artist:"{name}"' in q or q.endswith(" " + name):
            i = name.split()[-1]
            title = "Known Song 2" if name == "Known Act" else f"Song {i}"
            if "Similar" in q:
                return []
            return [T(("t" + a)[-22:][::-1], title, [(a, name)])]
    if "Similar" in q:
        n = q.split("Similar ")[1].split('"')[0]
        return [T(f"sim{n}".ljust(22, "x"), f"Hit {n}", [(aid(500 + int(n)), f"Similar {n}")])]
    return []

def fake_paced_get(endpoint, token, gap_s):
    if endpoint.startswith("artists/") and "/albums" in endpoint:
        a = endpoint.split("/")[1]
        if a == aid(501):       # Similar 1 released an EP 5 days ago
            rel = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 5 * 86400))
            return {"items": [{"id": "albumsim1", "name": "New EP", "album_type": "single", "release_date": rel}]}
        return {"items": [{"id": "old", "name": "Old", "album_type": "album", "release_date": "2020-01-01"}]}
    if endpoint.startswith("artists/"):
        return {"name": "Fetched Name"}
    return {"error": {"status": 404}}

def fake_album_tracks(album_id, market, token):
    a = aid(501)
    return [{"id": "ep1".ljust(22, "e"), "uri": "spotify:track:" + "ep1".ljust(22, "e"), "name": "Intro",
             "duration_ms": 60000, "artists": [{"id": a, "name": "Similar 1"}]},
            {"id": "ep2".ljust(22, "e"), "uri": "spotify:track:" + "ep2".ljust(22, "e"), "name": "New EP",
             "duration_ms": 190000, "artists": [{"id": a, "name": "Similar 1"}]},
            {"id": "ep3".ljust(22, "e"), "uri": "spotify:track:" + "ep3".ljust(22, "e"), "name": "B-side",
             "duration_ms": 200000, "artists": [{"id": a, "name": "Similar 1"}]},
            {"id": "ep4".ljust(22, "e"), "uri": "spotify:track:" + "ep4".ljust(22, "e"), "name": "Their Collab",
             "duration_ms": 200000, "artists": [{"id": aid(777), "name": "Someone Else"}, {"id": a, "name": "Similar 1"}]}]

reply = picks_json(15)
reply["picks"].append({"artist": "Known Act", "title": "Known Song 2", "origin": "KR"})
reply["similar_artists"] = [{"artist": f"Similar {i}", "known_song": f"Hit {i}", "act": "girl_group"} for i in range(1, 4)]
L._chat = fake_chat_factory([{"content": json.dumps(reply), "finish_reason": "stop",
                              "usage": {"prompt_tokens": 1500, "completion_tokens": 14000}}])
orig = (L._search, L._paced_get, composer._fetch_album_tracks, client.get_user_market)
L._search, L._paced_get, composer._fetch_album_tracks = fake_search, fake_paced_get, fake_album_tracks
client.get_user_market = lambda token=None: "SG"
L.load_config = (lambda base=L.load_config: (lambda: dict(base(), enabled=True)))()
noon = int(time.mktime(time.strptime("2026-10-07 12:00", "%Y-%m-%d %H:%M"))) * 1000   # local time
r = L.run("tok", now_ms=None, daytime=True)
check("run ok", r["status"] == "ok", r)
with db.get_conn() as conn:
    rows = conn.execute("SELECT kind, status, reason, artist, track_id FROM llm_pick").fetchall()
    batch = conn.execute("SELECT status, thinking, detail FROM llm_batch").fetchall()
picks_ok = [x for x in rows if x[0] == "pick" and x[1] == "resolved"]
check("15 picks resolved", len(picks_ok) == 15, len(picks_ok))
check("known artist filtered", any(x[0] == "pick" and x[2] == "known_artist" for x in rows), rows[-5:])
check("known artist dropped by name BEFORE any Spotify search (v2.22)", not any("Known Act" in q for q in search_calls),
      [q for q in search_calls if "Known Act" in q])
sims = [x for x in rows if x[0] == "similar" and x[1] == "resolved"]
check("similar artists resolved via known_song", len(sims) == 3, sims)
rel = {x[4]: (x[1], x[2]) for x in rows if x[0] == "new_release"}
check("new release kept", rel.get("ep2".ljust(22, "e"), ("?",))[0] == "resolved", rel)
check("new-release intro dropped", rel.get("ep1".ljust(22, "e"), ("?", "?"))[1] == "version", rel)
check("new-release where the similar artist is only featured: skipped", "ep4".ljust(22, "e") not in rel, rel)
check("batch ok + thinking", batch and batch[0][0] == "ok" and batch[0][1] == 1, batch)
d = json.loads(batch[0][2])
check("cost recorded", d.get("cost_usd", 0) > 0, d.get("cost_usd"))
check("names filled", db.get_artist_names().get(HEARD_ARTIST) == "Known Act")
r2 = L.run("tok", daytime=True)
check("second run within 6h skipped", r2["status"] == "skipped", r2)
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (created_at, status) VALUES (?, 'running')", (int(time.time() * 1000),))
r3 = L.run("tok", force=True, daytime=True)
check("live lease blocks a forced run", r3["status"] == "skipped" and "progress" in r3["summary"], r3)
with db.get_conn() as conn:
    conn.execute("UPDATE llm_batch SET created_at=? WHERE status='running'", (int(time.time() * 1000) - 3600000,))
L._chat = fake_chat_factory([L.LLMError("HTTP 503: busy")] * 3)
r4 = L.run("tok", force=True, daytime=True)
with db.get_conn() as conn:
    statuses = [s for (s,) in conn.execute("SELECT status FROM llm_batch ORDER BY batch_id")]
check("stale lease → killed; LLM failure → partial", statuses == ["ok", "killed", "partial"], statuses)
check("partial still scanned releases", "new-release" in r4["summary"], r4["summary"])
sgt = lambda hhmm: int(__import__("datetime").datetime.strptime(f"2026-10-07 {hhmm}", "%Y-%m-%d %H:%M")
                      .replace(tzinfo=L.SGT).timestamp() * 1000)
check("02:55 SGT blocked", L._in_refresh_window(sgt("02:55")))
check("03:30 SGT allowed", not L._in_refresh_window(sgt("03:30")))
check("01:20 SGT allowed", not L._in_refresh_window(sgt("01:20")))
L._search, L._paced_get, composer._fetch_album_tracks, client.get_user_market = orig
L._chat = orig_chat

# Reviewer scenarios: a null item in a search result / album list must not kill the batch,
# and a late-stage crash must not void the picks already saved (batch stays usable).
L.load_config = (lambda base=L.load_config: (lambda: dict(base(), min_interval_h=0)))()
L._search, L._paced_get, composer._fetch_album_tracks = fake_search, fake_paced_get, fake_album_tracks
client.get_user_market = lambda token=None: "SG"
# _search itself must drop null items (Spotify occasionally returns them)
_pg = L._paced_get
L._paced_get = lambda endpoint, token, gap_s: {"tracks": {"items": [None, T(aid(77), "S", [(aid(78), "A")]) | {"artists": [None, {"id": aid(78), "name": "A"}]}]}}
got = orig[0]("q", "track", "tok", "SG", CFG)   # the real _search (L._search is stubbed here)
check("_search drops null items + null artists", len(got) == 1 and got[0]["artists"] == [{"id": aid(78), "name": "A"}], got)
L._paced_get = _pg
reply2 = picks_json(14)
reply2["picks"] = [dict(p, artist=p["artist"].replace("Artist", "Artist"), title=p["title"]) for p in reply2["picks"]]
reply2["similar_artists"] = [{"artist": "Similar 1", "known_song": "Hit 1", "act": "girl_group"}]
L._chat = fake_chat_factory([{"content": json.dumps(reply2), "finish_reason": "stop", "usage": {}}])
orig_rel = L.fetch_new_releases
L.fetch_new_releases = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late boom"))
with db.get_conn() as conn:   # make every earlier pick heard-free again is unnecessary: a new batch re-resolves via cache
    pass
r5 = L.run("tok", force=True, daytime=True)
with db.get_conn() as conn:
    st5 = conn.execute("SELECT status, detail FROM llm_batch ORDER BY batch_id DESC LIMIT 1").fetchone()
check("late-stage crash → partial (02:20 retries), not error", st5[0] == "partial", st5[0])
check("late-stage crash recorded", "releases" in json.loads(st5[1]).get("stage_errors", {}), st5[1][:200])
check("late-stage crash → picks loadable", len(L.load_candidates("pick", 7)) > 0)
L.fetch_new_releases = orig_rel
items_with_null = _dicts_test = L._dicts([None, {"a": 1}, "x"])
check("_dicts drops nulls", items_with_null == [{"a": 1}])
check("_sane_track cleans artists", L._sane_track({"artists": [None, {"id": "x"}], "album": None})
      == {"artists": [{"id": "x"}], "album": {}})
L._search, L._paced_get, composer._fetch_album_tracks, client.get_user_market = orig
L._chat = orig_chat

# Similar artist: a same-name artist with a different song is NOT accepted
L._search = lambda q, kind, token, market, cfg: [T(aid(901), "Totally Different Song", [(aid(902), "Crush")])]
ctx_s = L._context(dict(CFG), int(time.time() * 1000))
rows_s = L.resolve_similar([{"artist": "Crush", "known_song": "Made-Up Song Title", "act": "female_solo"}], "tok", "SG", ctx_s, L._Budget(100))
check("similar: song must match", rows_s[0]["status"] == "unresolved" and rows_s[0]["reason"] == "no_match", rows_s)
L._search = lambda q, kind, token, market, cfg: None
rows_s = L.resolve_similar([{"artist": "Crush", "known_song": "Beautiful", "act": "female_solo"}], "tok", "SG", ctx_s, L._Budget(100))
check("similar: search failure is search_error", rows_s[0]["reason"] == "search_error", rows_s)
L._search = orig[0]
# lookup errors → partial (the 02:20 retry must run)
L._search = lambda q, kind, token, market, cfg: None
L._paced_get, composer._fetch_album_tracks = fake_paced_get, fake_album_tracks
client.get_user_market = lambda token=None: "SG"
fresh = picks_json(20)
for i, p in enumerate(fresh["picks"]):
    p["artist"], p["title"] = f"Fresh Act {i}", f"Fresh Song {i}"      # not in the resolution cache
L._chat = fake_chat_factory([{"content": json.dumps(fresh), "finish_reason": "stop", "usage": {}}])
r6 = L.run("tok", force=True, daytime=True)
check("mostly failed lookups → partial", r6["status"] == "partial" and "lookups failed" in r6["summary"], r6)
L._search, L._paced_get, composer._fetch_album_tracks, client.get_user_market = orig
L._chat = orig_chat
# retention
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (created_at, status) VALUES (?, 'ok')", (1,))
    old_id = conn.execute("SELECT MAX(batch_id) FROM llm_batch").fetchone()[0]
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, status, created_at) VALUES (?, 'pick', 1, 'resolved', 1)", (old_id,))
L._prune(int(time.time() * 1000))
with db.get_conn() as conn:
    left = conn.execute("SELECT COUNT(*) FROM llm_pick WHERE batch_id=?", (old_id,)).fetchone()[0]
    leftb = conn.execute("SELECT COUNT(*) FROM llm_batch WHERE batch_id=?", (old_id,)).fetchone()[0]
check("_prune drops >90-day batches", left == 0 and leftb == 0)

# ── 5. composer ──────────────────────────────────────────────────────────────
composer.client.get_token = lambda: "tok"
graph_calls = []
orig_disc, orig_wild = composer._pick_discoveries, composer._pick_wildcards
composer._pick_discoveries = lambda *a, **k: graph_calls.append("disc") or []
composer._pick_wildcards = lambda *a, **k: graph_calls.append("wild") or []

liked_ids = [f"liked{i:017d}" for i in range(40)]
with db.get_conn() as conn:
    for i, tid in enumerate(liked_ids):
        conn.execute("INSERT OR REPLACE INTO track_cache VALUES (?,?,?,?,?,?)",
                     (tid, f"Fav {i}", json.dumps([aid(1000 + i)]), 200000, NOW, "Fav Album"))
scores = {tid: 3.0 - i * 0.01 for i, tid in enumerate(liked_ids)}


def compose():
    graph_calls.clear()
    return composer.compose_playlist(scores, dry_run=True)


with db.get_conn() as conn:     # the latest batch is a healthy one for the first compose
    conn.execute("INSERT INTO llm_batch (created_at, status, detail) VALUES (?, 'ok', '{}')", (int(time.time() * 1000),))
pl = compose()
src = [t["source"] for t in pl]
check("40 tracks", len(pl) == 40, len(pl))
check("no duplicate tracks", len({t["track_id"] for t in pl}) == len(pl))
check("no artist-graph calls when enabled", graph_calls == [], graph_calls)
check("sources 24/12/1+ (store has 15 picks, 2 releases)",
      src.count("core") == 24 and src.count("llm") == 12 and src.count("wildcard") == 1, src)
check("mix: first 3 favourites", src[:3] == ["core"] * 3, src[:6])
check("mix: position 4 is a new song", src[3] in ("llm", "wildcard"), src[:6])
check("mix: new songs spread (none of the last 10 runs > 2 in a row)",
      max(len(r) for r in "".join("N" if s != "core" else " " for s in src).split()) <= 2, src)
new_artists = [composer._primary_artist(t) for t in pl if t["source"] in ("llm", "wildcard")]
check("one song per new artist", len(new_artists) == len(set(new_artists)))
check("no note when the latest batch is ok but supply is short", composer.LAST_INFO.get("note") is None,
      composer.LAST_INFO)
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (created_at, status, detail) VALUES (?, 'partial', ?)",
                 (int(time.time() * 1000), json.dumps({"llm_error": "HTTP 503"})))
compose()
check("note when the latest batch failed", composer.LAST_INFO.get("note") and "failed" in composer.LAST_INFO["note"],
      composer.LAST_INFO)

# Mark one pick heard (sampler segment) → it must drop out and its artist becomes known.
heard_pick = picks_ok[0][4]
with db.get_conn() as conn:
    conn.execute("INSERT INTO play_segment (track_id, data, first_seen, exit_hi, end_reason) VALUES (?,?,?,?,?)",
                 (heard_pick, json.dumps({"artist_ids": [], "artists": []}), NOW, NOW, "early"))
pl = compose()
check("heard pick re-checked at compose", heard_pick not in {t["track_id"] for t in pl})

# Broken store → favourites fill, still 40, note set.
orig_load = L.load_candidates
L.load_candidates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone"))
pl = compose()
check("broken store → 40 favourites", len(pl) == 40 and all(t["source"] in ("core", "fallback") for t in pl),
      [t["source"] for t in pl])
check("broken store → note", bool(composer.LAST_INFO.get("note")), composer.LAST_INFO)
L.load_candidates = orig_load

# Rollback: enabled false → graph discovery path with the llm slots, block order, no note.
composer.LLM_ENABLED = False
pl = compose()
check("disabled → graph paths called", graph_calls == ["disc", "wild"], graph_calls)
check("disabled → no LAST_INFO", composer.LAST_INFO == {})
check("disabled → favourites fill (stubs return nothing)", len(pl) == 40 and pl[0]["source"] == "core")
composer.LLM_ENABLED = True
composer._pick_discoveries, composer._pick_wildcards = orig_disc, orig_wild

# A stored release whose primary artist is not the similar artist (pre-2.19.1 rows) is excluded at compose time
with db.get_conn() as conn:
    bid = conn.execute("SELECT MAX(batch_id) FROM llm_batch WHERE status IN ('ok','partial')").fetchone()[0]
    feat = {"track_id": "featonly00000000000001", "name": "Feat Track", "artist_ids": [aid(888), aid(501)],
            "artist_names": ["Primary Other", "Similar 1"], "duration_ms": 200000, "album_name": "X",
            "uri": "spotify:track:featonly00000000000001", "source": "wildcard",
            "release_date": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))}
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, status, track_id, artist_id, track, created_at) "
                 "VALUES (?, 'new_release', 999, 'Similar 1', 'Feat Track', 'resolved', ?, ?, ?, ?)",
                 (bid, feat["track_id"], aid(501), json.dumps(feat), int(time.time() * 1000)))
rels = composer._llm_candidates(set(), set(), None)["releases"]
check("compose drops releases where the similar artist is only featured", "featonly00000000000001" not in {r["track_id"] for r in rels},
      [r["track_id"] for r in rels])

# _spread / _round_robin
check("_spread keeps order", composer._spread([1, 2, 3], ["a", "b"]) == [1, "a", 2, "b", 3])
check("_spread empty", composer._spread([1, 2], []) == [1, 2] and composer._spread([], ["a"]) == ["a"])
rr = composer._round_robin([{"a": 1, "n": 1}, {"a": 1, "n": 2}, {"a": 2, "n": 3}], lambda t: t["a"])
check("_round_robin rotates", [t["n"] for t in rr] == [1, 3, 2])

# stats() runs on this DB
try:
    out = L.stats(7)
    check("stats runs", "Batches:" in out, out[:200])
except Exception as e:
    check("stats runs", False, f"{type(e).__name__}: {e}")

# ── 6. learning loop (v2.20) — a fresh DB ────────────────────────────────────
db.DB_PATH = os.path.join(_TMP.name, "learn.db")
db.init_db()
L.load_config = (lambda base=L.load_config: (lambda: dict(base(), enabled=True)))()
NOW6 = int(time.time() * 1000)
DAY = 86400 * 1000
LCFG = L.load_config()


def lt(n):  # a DeepSeek track dict as stored in llm_pick.track
    return {"track_id": f"lt{n:020d}", "name": f"Learn Song {n}", "artist_ids": [aid(3000 + n)],
            "artist_names": [f"Learn Act {n}"], "duration_ms": 200000, "album_name": "LA",
            "uri": f"spotify:track:lt{n:020d}", "source": "llm", "release_date": "2026-09-01",
            "act": "girl_group", "bpm": 120, "taste_gate": True}     # as stored since v2.24


with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, angle) VALUES (1, ?, 'ok', 'angle A')", (NOW6 - 5 * DAY,))
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, angle) VALUES (2, ?, 'ok', 'angle B')", (NOW6 - 70 * DAY,))
    meta6 = {1: ("pick", "close", "rookie", 1), 2: ("pick", "adjacent", "midsize", 1), 3: ("pick", "close", "rookie", 1),
             4: ("new_release", None, None, 1), 5: ("pick", "close", "midsize", 1), 6: ("pick", "close", "rookie", 2)}
    for n, (kind, fit, tier, b) in meta6.items():
        c = lt(n)
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, fit, tier, status, track_id, artist_id, track, created_at) "
                     "VALUES (?,?,?,?,?,?,?,'resolved',?,?,?,?)",
                     (b, kind, n, c["artist_names"][0], c["name"], fit, tier, c["track_id"], c["artist_ids"][0], json.dumps(c), NOW6))

    def version(vid, days_ago, rows):
        conn.execute("INSERT INTO playlist_version (version_id, applied_at, origin) VALUES (?,?, 'refresh')", (vid, NOW6 - days_ago * DAY))
        for pos, (n, src) in enumerate(rows, 1):
            conn.execute("INSERT INTO playlist_version_track (version_id, position, track_id, source, name, artists) VALUES (?,?,?,?,?,?)",
                         (vid, pos, lt(n)["track_id"], src, lt(n)["name"], lt(n)["artist_names"][0]))

    def verdict(n, vid, pos, kind, days_ago, pct=None, src="sampler"):
        conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, duration_ms) "
                     "VALUES (?,?,?,?,?,?,?,?)", (lt(n)["track_id"], NOW6 - int(days_ago * DAY), kind, src, vid, pos,
                                                   int(200000 * (pct or 100) / 100), 200000))
    version(1, 4, [(1, "llm"), (2, "llm"), (3, "llm"), (4, "wildcard"), (5, "llm")])
    version(9, 65, [(6, "llm")])
    verdict(1, 1, 1, "completed", 3)          # T1 finished → encore
    verdict(2, 1, 2, "skip", 3, pct=5)        # T2 skipped at 5%
    verdict(3, 1, 3, "completed", 3)
    verdict(3, 1, 3, "skip", 2.5, pct=40)     # T3 finished then skipped → no encore
    verdict(4, 1, 4, "completed", 3)          # T4 (a wildcard) finished → encore
    verdict(5, 1, 5, "inferred_skip", 3, src="fallback")   # probably skipped → no encore
    verdict(6, 9, 1, "completed", 64)         # T6 finished 64 days ago → positive only with the 90-day window

enc = [c["track_id"] for c in L.encore_candidates(set(), NOW6, LCFG)]
check("encore: finished + never skipped", enc == [lt(4)["track_id"], lt(1)["track_id"]] or set(enc) == {lt(1)["track_id"], lt(4)["track_id"]}, enc)
check("encore: liked tracks are left to core", lt(1)["track_id"] not in [c["track_id"] for c in L.encore_candidates({lt(1)["track_id"]}, NOW6, LCFG)])
check("encore: older than encore_days → none", L.encore_candidates(set(), NOW6 + 10 * DAY, LCFG) == [])
with db.get_conn() as conn:
    version(2, 2, [(4, "encore")]); verdict(4, 2, 1, "completed", 1.5)
    version(3, 1, [(4, "encore")]); verdict(4, 3, 1, "completed", 0.5)
check("encore: stops after encore_max heard encores",
      lt(4)["track_id"] not in [c["track_id"] for c in L.encore_candidates(set(), NOW6, LCFG)])

with db.get_conn() as conn:      # a v2.18 graph wildcard (not in llm_pick) must not count as DeepSeek feedback
    conn.execute("INSERT INTO playlist_version_track (version_id, position, track_id, source, name) VALUES (1, 6, 'graphwild0000000000001', 'wildcard', 'Old Wild')")
    conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position) VALUES ('graphwild0000000000001', ?, 'completed', 'sampler', 1, 6)", (NOW6 - 3 * DAY,))
sm = L.feedback_summary(NOW6, LCFG)
check("summary by_fit (sampler only: T5's inferred skip not counted)",
      sm["by_fit"].get("close") == {"finished": 1, "judged": 2} and sm["by_fit"].get("adjacent") == {"finished": 0, "judged": 1}, sm["by_fit"])
check("summary by_kind", sm["by_kind"].get("new_release") == {"finished": 1, "judged": 1}
      and sm["by_kind"].get("encore") == {"finished": 2, "judged": 2}, sm["by_kind"])
check("summary by_angle", sm["by_angle"].get("angle A", {}).get("judged") == 3, sm["by_angle"])

prof = L.build_profile(NOW6, LCFG)
w = {x["title"]: x for x in prof["worked"]}
d = {x["title"]: x for x in prof["didnt_work"]}
check("WORKED: a DeepSeek pick is compact (fit / tier / angle live in FEEDBACK_SUMMARY)",
      w.get("Learn Song 1") == {"artist": "Learn Act 1", "title": "Learn Song 1"}, prof["worked"])
check("new release labelled", w.get("Learn Song 4", {}).get("from") == "new release", prof["worked"])
check("old graph wildcard labelled", w.get("Old Wild", {}).get("from") == "liked artist's new release", prof["worked"])
check("DIDNT_WORK has left_at", d.get("Learn Song 2", {}).get("left_at") == "5%", prof["didnt_work"])
check("FEEDBACK_SUMMARY in the prompt", "FEEDBACK_SUMMARY" in L.build_messages(prof, LCFG, "x")[1]["content"])

check("positive window: explicit 90 days includes a 64-day-old finish", aid(3006) in L.positive_artist_ids(days=90, now_ms=NOW6))
check("positive window 30 days excludes it", aid(3006) not in L.positive_artist_ids(days=30, now_ms=NOW6))
check("positive window default = positive_artist_days (90; only the prompt is capped)",
      aid(3006) in L.positive_artist_ids(now_ms=NOW6))

few = {"by_fit": {"close": {"finished": 2, "judged": 3}, "adjacent": {"finished": 0, "judged": 3}}}
check("adaptive share needs data", L.adaptive_close_share(few, LCFG)[0] == LCFG["close_share"])
good_close = {"by_fit": {"close": {"finished": 8, "judged": 10}, "adjacent": {"finished": 1, "judged": 10}}}
sh, _ = L.adaptive_close_share(good_close, LCFG)
check("adaptive share moves toward close (clamped)", LCFG["close_share"] < sh <= LCFG["adaptive"]["close_share_max"], sh)
good_adj = {"by_fit": {"close": {"finished": 1, "judged": 10}, "adjacent": {"finished": 8, "judged": 10}}}
sh2, _ = L.adaptive_close_share(good_adj, LCFG)
check("adaptive share moves toward adjacent (clamped)", LCFG["adaptive"]["close_share_min"] <= sh2 < LCFG["close_share"], sh2)
check("equal rates keep the base", L.adaptive_close_share({"by_fit": {"close": {"finished": 5, "judged": 10},
      "adjacent": {"finished": 5, "judged": 10}}}, LCFG)[0] == round(LCFG["close_share"], 2))

angles = LCFG["angles"]
check("angle: weekday rotation without data", L.pick_angle(LCFG, NOW6, {"by_angle": {}}) in angles)
skew = {"by_angle": {angles[0]: {"finished": 9, "judged": 10}, **{a: {"finished": 0, "judged": 6} for a in angles[1:]}}}   # all ≥ min_per_angle
picks_a = [L.pick_angle(LCFG, NOW6 + i * DAY, skew) for i in range(60)]
check("angle: Thompson favours the angle that works", picks_a.count(angles[0]) > 40, picks_a.count(angles[0]))
_d0 = int(__import__("datetime").datetime(2026, 10, 9, 1, 20, tzinfo=L.SGT).timestamp() * 1000)
check("angle: same SGT day → same angle (01:20 and 02:20 agree)",
      all(L.pick_angle(LCFG, _d0, skew) == L.pick_angle(LCFG, _d0 + h * 3600 * 1000, skew) for h in (1, 5, 20)))

ctx6 = {"positive": {aid(3006)}, "known": set()}
srcs = L.release_sources([{"status": "resolved", "artist_id": aid(4001), "artist": "Sim", "track": {"spotify_name": "Sim"}}],
                         [{"status": "resolved", "artist_id": aid(4002), "artist": "PickA"}], ctx6, LCFG, NOW6)
check("release scan: positive artists first", [a for a, _ in srcs][:3] == [aid(3006), aid(4001), aid(4002)], srcs)

# early exit at ≥ 90% is a listen (user rule)
with db.get_conn() as conn:
    version(20, 2, [(30, "llm")])
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, fit, tier, status, track_id, artist_id, track, created_at) "
                 "VALUES (1,'pick',30,'Learn Act 30','Learn Song 30','close','rookie','resolved',?,?,?,?)",
                 (lt(30)["track_id"], lt(30)["artist_ids"][0], json.dumps(lt(30)), NOW6))
    conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, score_ms, duration_ms) "
                 "VALUES (?,?,'early_exit','sampler',20,1,190000,190000,200000)", (lt(30)["track_id"], NOW6 - DAY))
check("early_exit at 95% → finished → encore", lt(30)["track_id"] in [c["track_id"] for c in L.encore_candidates(set(), NOW6, LCFG)])
check("early_exit at 95% → WORKED", any(x["title"] == "Learn Song 30" for x in L.build_profile(NOW6, LCFG)["worked"]))

# kind comes from the placement's section, even if a later batch stored the track as a new release
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, angle) VALUES (3, ?, 'ok', 'angle C')", (NOW6 - DAY,))
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, status, track_id, artist_id, track, created_at) "
                 "VALUES (3,'new_release',1,'Learn Act 30','Learn Song 30','resolved',?,?,?,?)",
                 (lt(30)["track_id"], lt(30)["artist_ids"][0], json.dumps(lt(30)), NOW6))
sm2 = L.feedback_summary(NOW6, LCFG)
check("kind from placement section", sm2["by_kind"]["pick"]["judged"] == sm["by_kind"]["pick"]["judged"] + 1, (sm["by_kind"], sm2["by_kind"]))
with db.get_conn() as conn:   # a killed batch's rows never label feedback
    conn.execute("UPDATE llm_batch SET status='killed' WHERE batch_id=3")
check("killed batch ignored by _pick_meta", L._pick_meta()[lt(30)["track_id"]]["kind"] == "pick")
with db.get_conn() as conn:
    conn.execute("DELETE FROM llm_pick WHERE batch_id=3"); conn.execute("DELETE FROM llm_batch WHERE batch_id=3")

# adaptive config robustness (the real loader against temp config files)
_cfg_old = L._CFG_PATH
for label, block, want_enabled, want_min in (
        ("quoted false", 'adaptive:\n    enabled: "false"\n', False, 8),
        ("scalar false", "adaptive: false\n", False, 8),
        ("blank min_judged", "adaptive:\n    min_judged:\n", True, 8)):
    pth = os.path.join(_TMP.name, f"cfg-{label.replace(' ', '-')}.yaml")
    with open(pth, "w") as f:
        f.write("llm_discovery:\n  enabled: true\n  " + block)
    L._CFG_PATH = pth
    c = _LOAD_CONFIG_ORIG()
    check(f"adaptive config: {label}", c["adaptive"]["enabled"] is want_enabled and c["adaptive"]["min_judged"] == want_min, c["adaptive"])
L._CFG_PATH = _cfg_old
# a broken learning step must not cost the batch (falls back to weekday angle + base share)
_fs_orig = L.feedback_summary
L.feedback_summary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad summary"))
L._chat = fake_chat_factory([{"content": json.dumps(picks_json(12)), "finish_reason": "stop", "usage": {}}])
_pg_orig, _s_orig = L._paced_get, L._search
L._search = lambda q, kind, token, market, cfg: []
L._paced_get = lambda endpoint, token, gap_s: {"items": []}
client.get_user_market = lambda token=None: "SG"
r7 = L.run("tok", dry_run=True, daytime=True)
check("learning failure → batch still runs", r7["status"] in ("ok", "partial") and "learning step failed" in (r7.get("detail") or {}).get("close_share_note", ""), r7.get("detail", {}).get("close_share_note"))
L.feedback_summary = _fs_orig
# run() wiring (v2.22): a cut-off full reply → the small retry answers; reasoning + prompt size recorded
L._chat = fake_chat_factory([{"content": "{\"picks\": [", "finish_reason": "length",
                              "usage": {"completion_tokens": 48000, "completion_tokens_details": {"reasoning_tokens": 47000}}},
                             {"content": json.dumps(picks_json(12)), "finish_reason": "stop", "usage": {}}])
r8 = L.run("tok", dry_run=True, daytime=True)
d8 = r8.get("detail") or {}
check("run: the small retry answers a cut-off batch", [a.get("stage") for a in d8.get("attempts", [])] == ["full", "small"]
      and "answered by the small retry" in r8["summary"], (d8.get("attempts"), r8["summary"]))
check("run: reasoning share recorded + warned", (d8.get("reasoning") or {}).get("cut_off") is True
      and "don't raise max_tokens" in r8["summary"], (d8.get("reasoning"), r8["summary"]))
check("run: prompt size recorded", 0 < d8.get("profile", {}).get("prompt_chars", 0) <= LCFG["prompt_chars_max"], d8.get("profile"))
L._paced_get, L._search, L._chat = _pg_orig, _s_orig, orig_chat

# replay skip: a skip within 12 h of finishing the same song is ignored
with db.get_conn() as conn:
    version(21, 2, [(31, "llm")])
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, fit, tier, status, track_id, artist_id, track, created_at) "
                 "VALUES (1,'pick',31,'Learn Act 31','Learn Song 31','close','rookie','resolved',?,?,?,?)",
                 (lt(31)["track_id"], lt(31)["artist_ids"][0], json.dumps(lt(31)), NOW6))
    verdict(31, 21, 1, "completed", 1.5)
    verdict(31, 21, 1, "skip", 1.4, pct=3)      # ~2.4 h later on the wrap-around replay
check("replay skip ignored → still an encore", lt(31)["track_id"] in [c["track_id"] for c in L.encore_candidates(set(), NOW6, LCFG)])
check("replay skip ignored → WORKED", any(x["title"] == "Learn Song 31" for x in L.build_profile(NOW6, LCFG)["worked"]))
check("replay skip ignored → positive", aid(3031) in L.positive_artist_ids(now_ms=NOW6))
ranked = L.positive_artists_ranked(now_ms=NOW6)
check("positive ranked most recent finish first", ranked.index(aid(3031)) < ranked.index(aid(3006)), ranked)
many = {"positive": set(ranked), "known": set()}
srcs2 = L.release_sources([], [], many, dict(LCFG, positive_release_max=2), NOW6)
check("positive_release_max caps positives in the scan", [a for a, _ in srcs2] == ranked[:2], srcs2)

# releases-only cool-off for heard, non-positive artists
ctx_c = {"known": {aid(7001), aid(7002)}, "positive": set(), "cfg": LCFG, "now": NOW6,
         "last_heard": {aid(7001): NOW6 - 61 * DAY, aid(7002): NOW6 - 10 * DAY}}
check("cool-off: unheard 61 days → releases allowed", not L.release_blocked_artist(aid(7001), ctx_c))
check("cool-off: heard 10 days ago → releases blocked", L.release_blocked_artist(aid(7002), ctx_c))
check("cool-off: unknown artist never blocked", not L.release_blocked_artist(aid(7003), ctx_c))

# encore placement-day cap: placed on encore_max_days days without a heard encore → stops
with db.get_conn() as conn:
    for k in range(4):
        version(30 + k, 0.9 - k * 0.2, [(31, "encore")])
check("encore stops after encore_max_days placements", lt(31)["track_id"] not in
      [c["track_id"] for c in L.encore_candidates(set(), NOW6, dict(LCFG, encore_max_days=1))])

# angle bandit waits until every angle has min_per_angle verdicts
part = {"by_angle": {angles[0]: {"finished": 9, "judged": 10}, angles[1]: {"finished": 0, "judged": 10}}}
check("angle: rotation until every angle has data", L.pick_angle(LCFG, _sgt("01:20"), part)
      == L.pick_angle(LCFG, _sgt("01:20"), {"by_angle": {}}))

# v2.20.1 release-scan cache + rate-limit handling
calls61 = []
rel_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 86400))
def pg61(endpoint, token, gap_s):
    calls61.append(endpoint)
    return {"items": [{"id": "alb61", "name": "Fresh EP", "album_type": "single", "release_date": rel_day}]}
def at61(album_id, market, token):
    calls61.append("tracks:" + album_id)
    return [{"id": "rel61".ljust(22, "r"), "uri": "spotify:track:" + "rel61".ljust(22, "r"), "name": "Fresh EP",
             "duration_ms": 200000, "artists": [{"id": aid(6100), "name": "Scan Act"}]}]
_pg61, _at61 = L._paced_get, composer._fetch_album_tracks
L._paced_get, composer._fetch_album_tracks = pg61, at61
ctx61 = L._context(LCFG, NOW6); ctx61["dry_run"] = False
rows61 = L.fetch_new_releases([(aid(6100), "Scan Act")], "tok", "SG", ctx61, L._Budget(100))
first = list(calls61)
ctx61b = L._context(LCFG, NOW6); ctx61b["dry_run"] = False
rows61b = L.fetch_new_releases([(aid(6100), "Scan Act")], "tok", "SG", ctx61b, L._Budget(100))
check("release scan: first run fetches list + tracks", len(first) == 2 and len(rows61) == 1, first)
check("release scan: second run within 72 h makes no calls", calls61 == first and len(rows61b) == 1
      and ctx61b["scan"]["cached"] == 1, (calls61, ctx61b.get("scan")))
calls61.clear()
ctxd = L._context(LCFG, NOW6); ctxd["dry_run"] = True
L.fetch_new_releases([(aid(6101), "Dry Act")], "tok", "SG", ctxd, L._Budget(100))
with db.get_conn() as conn:
    wrote = conn.execute("SELECT COUNT(*) FROM release_scan WHERE artist_id=?", (aid(6101),)).fetchone()[0]
check("dry run writes no scan cache", wrote == 0 and ctxd["scan"]["fetched"] == 1)
# a 429 stops the scan and marks the batch partial
def pg429(endpoint, token, gap_s):
    calls61.append(endpoint)
    return {"error": {"status": 429, "message": "rate limited (Retry-After 17160s)"}}
L._paced_get = pg429
calls61.clear()
ctx429 = L._context(LCFG, NOW6); ctx429["dry_run"] = True
L.fetch_new_releases([(aid(6200 + i), f"Act {i}") for i in range(5)], "tok", "SG", ctx429, L._Budget(100))
check("429 stops the release scan after one call", len(calls61) == 1 and ctx429["scan"]["rate_limited"], calls61)
L._search = lambda q, kind, token, market, cfg: []
L._chat = fake_chat_factory([{"content": json.dumps(picks_json(12)), "finish_reason": "stop", "usage": {}}])
client.get_user_market = lambda token=None: "SG"
_rs = L.release_sources
L.release_sources = lambda *a, **k: [(aid(6300), "Banned Act")]
r429 = L.run("tok", dry_run=True, daytime=True)
check("rate-limited release scan → partial + said so", r429["status"] == "partial"
      and "rate limit" in r429["summary"], r429["summary"])
L.release_sources = _rs
L._paced_get, composer._fetch_album_tracks, L._chat = _pg61, _at61, orig_chat
L._search = orig[0]

# composer: an encore takes an llm slot, placed early in the new-music block
with db.get_conn() as conn:
    for i in range(40):
        tid = f"fav6{i:018d}"
        conn.execute("INSERT INTO track_cache VALUES (?,?,?,?,?,?)", (tid, f"Fav6 {i}", json.dumps([aid(5000 + i)]), 200000, NOW6, "F"))
    for n in range(10, 24):     # fresh unheard picks in a recent ok batch
        c = lt(n)
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, fit, tier, status, track_id, artist_id, track, created_at) "
                     "VALUES (1,'pick',?,?,?,'close','rookie','resolved',?,?,?,?)",
                     (n, c["artist_names"][0], c["name"], c["track_id"], c["artist_ids"][0], json.dumps(c), NOW6))
scores6 = {f"fav6{i:018d}": 3.0 - i * 0.01 for i in range(40)}
composer.client.get_token = lambda: "tok"
pl6 = composer.compose_playlist(scores6, dry_run=True)
src6 = [t["source"] for t in pl6]
import skip_signals as _ss6
_cool = _ss6.cooldown_state()
_blocked6 = set(_cool["cooldown"]) | set(_cool["demoted"])
n_enc = len([c for c in L.encore_candidates(set(), NOW6, LCFG) if c["track_id"] not in _blocked6])
check("encore placed (candidates minus skip cooldown)", src6.count("encore") == n_enc == 2
      and lt(1)["track_id"] in {t["track_id"] for t in pl6}, (n_enc, src6))
check("encore takes an llm slot", src6.count("encore") + src6.count("llm") == 12, (src6.count("encore"), src6.count("llm")))
check("encore comes first in the new block", src6[3] == "encore", src6[:6])
check("LAST_INFO reports encores", composer.LAST_INFO.get("encore") == 2, composer.LAST_INFO)
# picks run short → more encores (up to the 12 llm slots) before favourites
_lc = L.load_candidates
L.load_candidates = lambda kind, *a, **k: [] if kind == "pick" else _lc(kind, *a, **k)
_es = LCFG["encore_slots"]
pl6b = composer.compose_playlist(scores6, dry_run=True)
check("encore top-up when picks are short", [t["source"] for t in pl6b].count("encore") == 2
      and [t["source"] for t in pl6b].count("llm") == 0, [t["source"] for t in pl6b])
L.load_candidates = _lc

# v2.20.3 — more-from cap (≤ 3 of the 12 llm slots), fetch cap, quiet guard-budget stop
def mf_pick(n, more_from):
    c = lt(400 + n)
    c.update(fit="close", tier="rookie", _batch_id=9, _rank=n, _more_from=more_from)
    return c
_lc_orig = composer._llm_candidates
composer._llm_candidates = lambda *a, **k: {
    "picks": [mf_pick(i, True) for i in range(6)] + [mf_pick(10 + i, False) for i in range(10)],
    "releases": [], "encores": [], "encore_slots": 3, "more_from_slots": 3,
    "status": {"status": "ok", "age_h": 1.0}}
pl_mf = composer.compose_playlist(scores6, dry_run=True)
llm_mf = [t for t in pl_mf if t["source"] == "llm"]
check("more-from capped at 3 of 12", sum(1 for t in llm_mf if t.get("_more_from")) == 3 and len(llm_mf) == 12,
      (sum(1 for t in llm_mf if t.get("_more_from")), len(llm_mf)))
check("LAST_INFO reports more_from", composer.LAST_INFO.get("more_from") == 3, composer.LAST_INFO)
mf_pos = [i for i, t in enumerate(llm_mf) if t.get("_more_from")]
check("more-from picks are interleaved, not first", mf_pos != [0, 1, 2], mf_pos)
composer._llm_candidates = _lc_orig
check("prompt limits MORE_FROM picks", "At most 5 picks may be by MORE_FROM" in L.build_messages(prof, LCFG, "x")[1]["content"])
calls63 = []
def pg63(endpoint, token, gap_s):
    calls63.append(endpoint)
    return {"items": []}
_pg63 = L._paced_get
L._paced_get = pg63
ctx63 = L._context(dict(LCFG, release_fetch_max=1), NOW6); ctx63["dry_run"] = True
L.fetch_new_releases([(aid(6400 + i), f"Fresh {i}") for i in range(3)], "tok", "SG", ctx63, L._Budget(100))
check("fetch cap: 1 fetched, 2 deferred", ctx63["scan"]["fetched"] == 1 and ctx63["scan"]["deferred"] == 2, ctx63["scan"])
L._paced_get = lambda endpoint, token, gap_s: {"error": {"status": 429, "message": "local guard: hourly cap (120 catalogue calls)"}}
ctx64 = L._context(LCFG, NOW6); ctx64["dry_run"] = True
L.fetch_new_releases([(aid(6500 + i), f"Cap {i}") for i in range(3)], "tok", "SG", ctx64, L._Budget(100))
check("guard budget refusal stops quietly (not a ban)", ctx64["scan"]["guard_limited"] and not ctx64["scan"]["rate_limited"]
      and ctx64["scan"]["errors"] == 0, ctx64["scan"])
L._paced_get = lambda endpoint, token, gap_s: {"error": {"status": 429, "message": "local guard: Spotify catalogue ban remembered until 22:46"}}
ctx65 = L._context(LCFG, NOW6); ctx65["dry_run"] = True
L.fetch_new_releases([(aid(6600), "Ban")], "tok", "SG", ctx65, L._Budget(100))
check("remembered ban counts as rate-limited", ctx65["scan"]["rate_limited"], ctx65["scan"])
L._paced_get = _pg63

# ── 7. Spotify catalogue guard (v2.20.2) ─────────────────────────────────────
db.DB_PATH = os.path.join(_TMP.name, "guard.db")
db.init_db()
import importlib  # noqa: E402
_dr = client._do_request
fake_calls = []
def fake_do(method, endpoint, token, body=None, max_time=30):
    fake_calls.append(endpoint)
    if endpoint.startswith("artists/BAN"):
        return {"error": {"status": 429, "message": "Too many requests"}}, 429, 100.0
    return {"items": []}, 200, None
client._do_request = fake_do
client._GET_MEMO.clear()
_g_old = dict(client.GUARD)
client.GUARD.update(min_gap_s=0.05, per_hour=5, per_day=50, ban_margin_s=10)
r1 = client.api("GET", "artists/A1/albums?limit=10", token="t")
r2 = client.api("GET", "search?q=x&type=track", token="t")
with db.get_conn() as conn:
    rows = conn.execute("SELECT t_ms, endpoint, status FROM api_call_log ORDER BY t_ms").fetchall()
check("guard logs catalogue calls", [r[1] for r in rows] == ["artists/A1/albums", "search"] and all(r[2] == 200 for r in rows), rows)
check("guard spaces catalogue calls", rows[1][0] - rows[0][0] >= 50, [r[0] for r in rows])
client.api("GET", "me/player", token="t")
with db.get_conn() as conn:
    n_log = conn.execute("SELECT COUNT(*) FROM api_call_log").fetchone()[0]
check("player calls are not guarded", n_log == 2 and "me/player" in fake_calls)
rb = client.api("GET", "artists/BAN/albums", token="t")
st = client.guard_state()
check("a 429 is remembered for every process", st["blocked_until"] > time.time() * 1000 + 100e3, st)
client._RATE_LIMITED_UNTIL = 0.0          # a fresh process has no in-memory breaker …
fake_calls.clear()
rr = client.api("GET", "albums/Z/tracks", token="t")
check("… but the DB ban refuses it without a network call", "ban remembered" in rr["error"]["message"] and not fake_calls, rr)
check("the ban does not touch player calls", "error" not in client.api("GET", "me/player", token="t"))
with db.get_conn() as conn:
    conn.execute("DELETE FROM api_guard")
for i in range(3):
    client.api("GET", f"search?q={i}&type=track", token="t")
rc = client.api("GET", "search?q=cap&type=track", token="t")
check("hourly cap refuses", "hourly cap" in rc["error"]["message"], rc)
_gs = client.guard_state
client.guard_state = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
rx = client.api("GET", "search?q=x2&type=track", token="t")
check("an unusable guard fails closed", "guard unavailable" in rx["error"]["message"], rx)
client.guard_state = _gs
# a shorter 429 must not shorten a remembered long ban (TEXT vs REAL MAX)
with db.get_conn() as conn:
    conn.execute("DELETE FROM api_guard"); conn.execute("DELETE FROM api_call_log")
client._guard_finish(client._guard_acquire("artists/X/albums")[1], 429, 17160.0)
long_until = client.guard_state()["blocked_until"]
with db.get_conn() as conn:
    cur = conn.execute("INSERT INTO api_call_log (t_ms, endpoint) VALUES (?, 'artists/Y/albums')", (time.time() * 1000,))
    rid = cur.lastrowid
client._guard_finish(rid, 429, None)
check("a shorter 429 keeps the longer ban", client.guard_state()["blocked_until"] == long_until,
      (long_until, client.guard_state()["blocked_until"]))
# a guarded 429 returns at once (no Retry-After sleep before refusing)
with db.get_conn() as conn:
    conn.execute("DELETE FROM api_guard"); conn.execute("DELETE FROM api_call_log")
client._RATE_LIMITED_UNTIL = 0.0
slept = []
_sleep = time.sleep
time.sleep = lambda x: slept.append(x)
client.set_deadline(120)
def fake_ra(method, endpoint, token, body=None, max_time=30):
    return {"error": {"status": 429, "message": "Too many"}}, 429, 8.0
client._do_request = fake_ra
r8 = client.api("GET", "artists/RA/albums", token="t")
client.set_deadline(None)
time.sleep = _sleep
check("guarded 429 returns at once", "ban remembered" in r8["error"]["message"] and not any(x >= 5 for x in slept), (r8, slept))
client._do_request = fake_do
client._RATE_LIMITED_UNTIL = 0.0
with db.get_conn() as conn:
    conn.execute("DELETE FROM api_guard")
# clock skew: a far-future row must not stall calls
with db.get_conn() as conn:
    conn.execute("DELETE FROM api_call_log")
    conn.execute("INSERT INTO api_call_log (t_ms, endpoint) VALUES (?, 'search')", (time.time() * 1000 + 600e3,))
t0 = time.time()
rk = client.api("GET", "search?q=skew&type=track", token="t")
check("a far-future log row (clock stepped back) is ignored", "error" not in rk and time.time() - t0 < 2, (rk, time.time() - t0))
# config coercion: a typo keeps that key's default; quoted numbers work
_cfgp = os.path.join(_TMP.name, "guardcfg")
os.makedirs(os.path.join(_cfgp, "src"), exist_ok=True)
with open(os.path.join(_cfgp, "config.yaml"), "w") as f:
    f.write('api_guard:\n  ban_margin_s: 10m\n  min_gap_s: "3.5"\n  per_hour: oops\n')
_file = client.__file__
client.__file__ = os.path.join(_cfgp, "src", "client.py")
client.GUARD.clear(); client.GUARD.update(client._GUARD_DEFAULTS)
client._load_guard_config()
client.__file__ = _file
check("guard config: typo keeps default, quoted number parses",
      client.GUARD["ban_margin_s"] == 600 and client.GUARD["min_gap_s"] == 3.5 and client.GUARD["per_hour"] == 120, client.GUARD)
client.GUARD.clear(); client.GUARD.update(_g_old)
client._do_request = _dr
# run(): refused in the daytime and during a remembered ban
_sgt7 = lambda hhmm: int(__import__("datetime").datetime.strptime(f"2026-10-08 {hhmm}", "%Y-%m-%d %H:%M")
                         .replace(tzinfo=L.SGT).timestamp() * 1000)
r_day = L.run("tok", force=True, now_ms=_sgt7("17:44"))
check("run refused in the daytime", r_day["status"] == "skipped" and "night window" in r_day["summary"], r_day)
check("night window: 01:20 in, 02:50 out", L._in_night_window(_sgt7("01:20")) and not L._in_night_window(_sgt7("02:50")))
with db.get_conn() as conn:
    conn.execute("INSERT OR REPLACE INTO api_guard (key, value, updated_at) VALUES ('catalogue_blocked_until', ?, 0)",
                 (_sgt7("01:20") + 3600e3,))
r_ban = L.run("tok", force=True, now_ms=_sgt7("01:20"))
check("run skipped while a ban is remembered", r_ban["status"] == "skipped" and "ban" in r_ban["summary"], r_ban)

# ── 8. input window cap (v2.21: at most 14 days of data in the prompt) ───────
db.DB_PATH = os.path.join(_TMP.name, "cap.db")
db.init_db()
NOW8 = int(time.time() * 1000)
C8 = L.load_config()
check("config: input_max_days 14", C8["input_max_days"] == 14 and L.input_days(C8) == 14, C8["input_max_days"])
check("input_days caps a longer window", L.input_days(C8, 60) == 14 and L.input_days(C8, 7) == 7)
check("shipped prompt windows are all ≤ 14 days",
      all(C8[k] <= 14 for k in ("profile_window_days", "feedback_days"))
      and C8["adaptive"]["feedback_days"] <= 14, {k: C8[k] for k in ("profile_window_days", "feedback_days")})


def cap_play(n, days_ago):
    tid, a = f"cap{n:019d}", aid(8000 + n)
    db.upsert_track_cache([{"track_id": tid, "name": f"Cap Song {n}", "artist_ids": [a], "duration_ms": 200000}])
    db.upsert_artist_names([(a, f"Cap Act {n}", "test")])
    with db.get_conn() as conn:
        conn.execute("INSERT INTO play_events (track_id, played_at, ms_played, duration_ms) VALUES (?,?,?,?)",
                     (tid, NOW8 - int(days_ago * DAY), 200000, 200000))
    return a


a_recent, a_old = cap_play(1, 3), cap_play(2, 20)
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, angle) VALUES (1, ?, 'ok', 'old angle')", (NOW8 - 20 * DAY,))
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, angle) VALUES (2, ?, 'ok', 'new angle')", (NOW8 - 2 * DAY,))
    for b, n, days_ago in ((1, 3, 20), (2, 4, 2)):        # one finished DeepSeek pick per batch
        tid, a = f"cap{n:019d}", aid(8000 + n)
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, title, fit, tier, status, track_id, artist_id, created_at) "
                     "VALUES (?, 'pick', 1, ?, ?, 'close', 'rookie', 'resolved', ?, ?, ?)",
                     (b, f"Cap Act {n}", f"Cap Song {n}", tid, a, NOW8 - days_ago * DAY))
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, status, created_at) VALUES (?, 'pick', 2, ?, 'unresolved', ?)",
                     (b, "Old Pick Act" if b == 1 else "New Pick Act", NOW8))
        conn.execute("INSERT INTO playlist_version (version_id, applied_at, origin) VALUES (?, ?, 'refresh')", (b, NOW8 - days_ago * DAY))
        conn.execute("INSERT INTO playlist_version_track (version_id, position, track_id, source, name, artists) "
                     "VALUES (?, 1, ?, 'llm', ?, ?)", (b, tid, f"Cap Song {n}", f"Cap Act {n}"))
        conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, duration_ms) "
                     "VALUES (?, ?, 'completed', 'sampler', ?, 1, 200000, 200000)", (tid, NOW8 - days_ago * DAY + 3600e3, b))
db.upsert_artist_names([(aid(8003), "Cap Act 3", "test"), (aid(8004), "Cap Act 4", "test")])

p8 = L.build_profile(NOW8, C8)
fav = [x["artist"] for x in p8["favourite_artists"]]
check("favourites: only plays inside 14 days", "Cap Act 1" in fav and "Cap Act 2" not in fav, fav)
check("library list: only artists heard inside 14 days", "Cap Act 1" in p8["library_artists"] and "Cap Act 2" not in p8["library_artists"],
      p8["library_artists"])
check("known-artist filter still covers all history", a_old in L.known_artist_ids())
check("WORKED: only verdicts inside 14 days", [x["title"] for x in p8["worked"]] == ["Cap Song 4"], p8["worked"])
check("MORE_FROM: only finishes inside 14 days", p8["more_from"] == ["Cap Act 4"], p8["more_from"])
check("composer / release scan keep the 90-day positive window", aid(8003) in L.positive_artist_ids(now_ms=NOW8)
      and C8["positive_artist_days"] == 90)
check("DO_NOT_SUGGEST: only batches inside 14 days", "New Pick Act" in p8["do_not_suggest"]
      and "Old Pick Act" not in p8["do_not_suggest"], p8["do_not_suggest"])
sm8 = L.feedback_summary(NOW8, C8)
check("FEEDBACK_SUMMARY: only verdicts inside 14 days", sm8["by_kind"].get("pick") == {"finished": 1, "judged": 1}, sm8["by_kind"])
msg8 = L.build_messages(p8, C8, "x")[1]["content"]
check("prompt carries nothing older than 14 days",
      all(f'"{x}"' not in msg8 for x in ("Cap Act 2", "Cap Song 3", "Cap Act 3", "Old Pick Act")) and '"Cap Act 1"' in msg8)
wide = dict(C8, feedback_days=60, profile_window_days=60, positive_artist_days=90,
            adaptive=dict(C8["adaptive"], feedback_days=60))
p8w = L.build_profile(NOW8, wide)
check("the ceiling holds when a window is set longer", [x["title"] for x in p8w["worked"]] == ["Cap Song 4"]
      and p8w["more_from"] == ["Cap Act 4"]
      and "Cap Act 2" not in [x["artist"] for x in p8w["favourite_artists"]]
      and L.feedback_summary(NOW8, wide)["by_kind"]["pick"]["judged"] == 1, p8w["worked"])
loose = dict(wide, input_max_days=30)
p8l = L.build_profile(NOW8, loose)
check("raising input_max_days widens every window (the cap is the mechanism)",
      sorted(x["title"] for x in p8l["worked"]) == ["Cap Song 3", "Cap Song 4"] and "Old Pick Act" in p8l["do_not_suggest"]
      and "Cap Act 3" in p8l["more_from"]
      and "Cap Act 2" in p8l["library_artists"] and L.feedback_summary(NOW8, loose)["by_kind"]["pick"]["judged"] == 2,
      (p8l["worked"], p8l["do_not_suggest"]))

# ── 8b. bounded request (v2.22) ──────────────────────────────────────────────
with db.get_conn() as conn:     # a recent pick by an artist who is also in LIBRARY_ARTISTS
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, status, created_at) VALUES (2, 'pick', 3, 'Cap Act 1', 'unresolved', ?)", (NOW8,))
p8d = L.build_profile(NOW8, C8)
check("DO_NOT_SUGGEST never repeats a LIBRARY_ARTISTS name", "Cap Act 1" in p8d["library_artists"]
      and "Cap Act 1" not in p8d["do_not_suggest"], p8d["do_not_suggest"])
data8 = L.build_messages(p8d, C8, "x")[1]["content"].split("DATA (json):\n", 1)[1]
check("DATA json is compact (no padding after separators)", '", "' not in data8 and '": ' not in data8, data8[:120])
check("the reply schema asks for no per-pick 'why'", '"why"' not in L.SYSTEM_PROMPT)
check("shipped request: 20 picks, 10 similar, fixed 48k ceiling",
      (C8["picks_requested"], C8["similar_artists_requested"], C8["max_tokens"]) == (20, 10, 48000))
big = {"favourite_artists": [{"artist": f"Fav {i}", "loved_songs": ["a", "b", "c"]} for i in range(25)],
       "worked": [{"artist": f"W {i}", "title": f"Worked Song {i}"} for i in range(30)],
       "didnt_work": [{"artist": f"D {i}", "title": f"Skipped Song {i}", "left_at": "5%"} for i in range(30)],
       "more_from": [], "library_artists": [f"Library Artist {i}" for i in range(300)],
       "do_not_suggest": [f"Suggested Artist {i}" for i in range(150)], "feedback_summary": {}}
info = {}
m_big = L.build_messages(big, dict(C8, prompt_chars_max=9000), "x", None, info)
size = sum(len(m["content"]) for m in m_big)
user = m_big[1]["content"]
check("prompt_chars_max holds", size <= 9000, size)
check("feedback trimmed first, to no fewer than 10 each", info["trimmed"].get("worked") == 20 and info["trimmed"].get("didnt_work") == 20,
      info)
check("then the longest list, oldest names first", info["trimmed"].get("library_artists", 0) > 0
      and '"Library Artist 0"' in user and '"Suggested Artist 0"' in user and '"Library Artist 299"' not in user, info)
check("newest feedback kept", '"Worked Song 0"' in user and '"Worked Song 29"' not in user)
check("under the limit nothing is trimmed", (lambda i: (L.build_messages(big, dict(C8, prompt_chars_max=10 ** 6), "x", None, i), i)[1])({})
      == {"trimmed": {}})
tiny = {}
L.build_messages(big, dict(C8, prompt_chars_max=100), "x", None, tiny)
check("an impossible limit stops at the floors (never loops)", tiny["trimmed"]["worked"] == 20
      and tiny["trimmed"]["library_artists"] == 280 and tiny["trimmed"]["do_not_suggest"] == 130, tiny)
# name prefilter: no search for a known name; a positive artist's name stays open
_s_calls = []
_s_orig = L._search
L._search = lambda q, kind, token, market, cfg: (_s_calls.append(q), [])[1]
ctx8 = {"cfg": C8, "known_names": {L.compact("Known Name")}, "now": NOW8}
r_known = L._resolve_one(1, {"artist": "KNOWN NAME", "title": "T", "fit": "close", "tier": None, "why": ""},
                         "tok", "SG", ctx8, L._Budget(100), {})
check("prefilter: known name → filtered, no search", r_known["status"] == "filtered" and r_known["reason"] == "known_artist"
      and not _s_calls, (r_known, _s_calls))
L._resolve_one(2, {"artist": "Brand New", "title": "T", "fit": "close", "tier": None, "why": ""}, "tok", "SG", ctx8, L._Budget(100), {})
check("prefilter: an unknown name is searched", len(_s_calls) == 2, _s_calls)
L._search = _s_orig
db.upsert_track_cache([{"track_id": f"cap{4:019d}", "name": "Cap Song 4", "artist_ids": [aid(8004)], "duration_ms": 200000}])
with db.get_conn() as conn:       # Cap Act 4 is now heard (known) AND positive
    conn.execute("INSERT INTO play_events (track_id, played_at, ms_played, duration_ms) VALUES (?,?,?,?)",
                 (f"cap{4:019d}", NOW8 - 2 * DAY, 200000, 200000))
check("Cap Act 4 is known and positive", aid(8004) in L.known_artist_ids() and aid(8004) in L.positive_artist_ids(now_ms=NOW8))
ctx_full = L._context(C8, NOW8)
check("context: positive_names (the finished-artist exception) come from the DB", L.compact("Cap Act 4") in ctx_full["positive_names"],
      ctx_full["positive_names"])
check("context: known names exclude positive (MORE_FROM) artists", L.compact("Cap Act 4") not in ctx_full["known_names"]
      and L.compact("Cap Act 1") in ctx_full["known_names"], sorted(ctx_full["known_names"])[:10])
# db: an unbounded IN list is chunked (SQLite caps bound variables)
db.upsert_track_cache([{"track_id": f"chunk{i:017d}", "name": f"C{i}", "artist_ids": ["a"], "duration_ms": 1} for i in range(1200)])
got = db.get_cached_tracks([f"chunk{i:017d}" for i in range(1200)] + ["missing"])
check("get_cached_tracks merges chunks (1,200 ids)", len(got) == 1200 and got[f"chunk{1199:017d}"]["name"] == "C1199", len(got))
check("IN-list chunks fit SQLite's smallest default limit (999)", db._IN_CHUNK <= 999, db._IN_CHUNK)
check("get_cached_artists survives 40,000 ids", isinstance(db.get_cached_artists([f"id{i}" for i in range(40000)]), dict))

# ── 9. DeepSeek skip report (v2.21) ──────────────────────────────────────────
import llm_report as R  # noqa: E402
db.DB_PATH = os.path.join(_TMP.name, "report.db")
db.init_db()
_sgt9 = lambda day, hhmm: int(__import__("datetime").datetime.strptime(f"2026-10-{day:02d} {hhmm}", "%Y-%m-%d %H:%M")
                              .replace(tzinfo=L.SGT).timestamp() * 1000)
_PCT = {"completed": 100, "skip": 20, "skim": 1, "stopped": 50}


def rv(day, rows):
    """A 03:00 playlist version on 2026-10-<day>: rows = [(source, sampler kind or None)] in position order."""
    applied = _sgt9(day, "03:00")
    tracks = [(f"rp{day:02d}{i:018d}", src) for i, (src, _) in enumerate(rows)]
    vid = db.save_playlist_version(tracks, applied)
    with db.get_conn() as conn:
        for i, ((tid, _), (_, kind)) in enumerate(zip(tracks, rows)):
            if kind:
                ms = 2000 * _PCT[kind]
                conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, "
                             "exposure_hi_ms, duration_ms, score_ms) VALUES (?,?,?,'sampler',?,?,?,?,200000,?)",
                             (tid, applied + 3600 * 1000 + i * 1000, kind, vid, i + 1, ms, ms, ms))
    return vid


rv(10, [("core", "completed"), ("core", "completed")] + [("llm", "completed")] * 4 + [("llm", "skip"), ("wildcard", "completed")])
rv(11, [("llm", "completed")] * 3 + [("llm", "skim"), ("wildcard", "skip"), ("encore", "completed"), ("llm", "stopped")])
rv(12, [("core", "skip"), ("llm", "completed"), ("llm", "completed"), ("llm", "skip"), ("encore", "skip"), ("llm", None)])
rep9 = R.build_report(_sgt9(12, "23:30"))
for want in ("## 🎧 DeepSeek skip report — Mon 12 Oct\n\n**Today**\n",
             "Skipped **1 of 3** new DeepSeek songs (33%) · finished 2\nRepeats of songs you'd finished: skipped 1 of 1\n\n",
             "**Since DeepSeek started (6 Oct)**\nSkipped **29%** of new songs (4 of 14) · finished 71%",
             "• Picks: 3 of 12 skipped (25%)\n• New releases: 1 of 2 skipped (50%)\n• Repeats: 1 of 2 skipped",
             "**Before DeepSeek** (artist-graph discovery, 1–6 Oct)\nSkipped **74%** (17 of 23) · finished 26%\n\n",
             "✅ **Better than before:** 45 points fewer skips (29% vs 74% skipped)\n-# 95% range: ",
             "**Skipped per day** (new songs)\n```\nSat 10 Oct    1 of 6    17%  ██\nSun 11 Oct    2 of 5    40%  ████\n"
             "Mon 12 Oct    1 of 3    33%  ███\n```",
             "⚠️ DeepSeek hasn't run in the last 36 h."):
    check(f"report: {want[:40]!r}", want in rep9, rep9)
check("report: blocks separated by blank lines", rep9.count("\n\n") >= 4, rep9)
check("report: core, stops and unjudged songs not counted", "of 15" not in rep9 and "of 16" not in rep9)
check("report: no 'Last 7 days' line before a week has passed", "Last 7 days" not in rep9)
check("report fits one Discord message", len(rep9) < 1900, len(rep9))
check("report: nothing on a day without listening", R.build_report(_sgt9(14, "23:30")) == "")
with db.get_conn() as conn:       # the 12 Oct song left at 95% (early exit) is a listen
    conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, exposure_hi_ms, "
                 "duration_ms, score_ms) VALUES (?, ?, 'early_exit', 'sampler', 3, 6, 190000, 190000, 200000, 190000)",
                 (f"rp12{5:018d}", _sgt9(12, "05:00")))
check("report: early exit at 95% counts as finished", "Skipped **1 of 4** new DeepSeek songs (25%) · finished 3"
      in R.build_report(_sgt9(12, "23:30")), R.build_report(_sgt9(12, "23:30")))
# a manual refresh at 20:00 on 12 Oct: Today covers both versions of the day
rv_manual = db.save_playlist_version([(f"rpm{i:019d}", src) for i, src in enumerate(("llm", "llm", "wildcard"))], _sgt9(12, "20:00"))
with db.get_conn() as conn:
    conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, exposure_hi_ms, "
                 "duration_ms, score_ms) VALUES (?, ?, 'skip', 'sampler', ?, 1, 20000, 20000, 200000, 20000)",
                 (f"rpm{0:019d}", _sgt9(12, "21:00"), rv_manual))
rep_m = R.build_report(_sgt9(12, "23:30"))
check("manual refresh: Today sums both versions of the day", "Skipped **2 of 5** new DeepSeek songs (40%) · finished 3" in rep_m, rep_m)
check("manual refresh: per-day line agrees with Today", "Mon 12 Oct    2 of 5    40%" in rep_m, rep_m)
# 13 Oct: the 03:00 refresh failed — the manual 12 Oct version stays live and is heard on 13 Oct
with db.get_conn() as conn:
    conn.execute("INSERT INTO skip_signal (track_id, at_ms, kind, source, version_id, position, exposure_ms, exposure_hi_ms, "
                 "duration_ms, score_ms) VALUES (?, ?, 'completed', 'sampler', ?, 2, 200000, 200000, 200000, 200000)",
                 (f"rpm{1:019d}", _sgt9(13, "09:00"), rv_manual))
rep_f = R.build_report(_sgt9(13, "23:30"))
check("failed refresh: today's listening on an older playlist still reported",
      "Skipped **0 of 1** new DeepSeek songs (0%) · finished 1" in rep_f and "Tue 13 Oct    0 of 1     0%" in rep_f, rep_f)
rv(14, [("core", "completed"), ("llm", None), ("wildcard", None)])
check("report: nothing when no DeepSeek song was judged today", R.build_report(_sgt9(14, "23:30")) == "")
# history longer than a week → the 'Last 7 days' line
rv(1, [("llm", "skip"), ("llm", "skip")])       # 1 Oct: before DeepSeek started — never counted
_rep_pre = R.build_report(_sgt9(12, "23:30"))
check("pre-launch versions are not counted", "Skipped **31%** of new songs (5 of 16)" in _rep_pre, _rep_pre)
rv(20, [("llm", "completed"), ("llm", "completed")])
rep_w = R.build_report(_sgt9(20, "23:30"))
check("report: 'Last 7 days' once the history is longer", "• Last 7 days: 0% skipped (0 of 2)" in rep_w, rep_w)
check("report: per-day table shows only the last 7 days", "10 Oct" not in rep_w and "Tue 20 Oct    0 of 2     0%" in rep_w, rep_w)
check("verdict: worse", R.verdict_line(30, 30).startswith("❌ **Worse than before:** 26 points more skips (100% vs 74% skipped)"),
      R.verdict_line(30, 30))
check("verdict: no clear difference", R.verdict_line(7, 10).startswith("➖ **No clear"), R.verdict_line(7, 10))
check("verdict: the benchmark's own uncertainty counts (65 of 100 is not 'better')",
      R.verdict_line(65, 100).startswith("➖ **No clear"), R.verdict_line(65, 100))
check("verdict: 10 of 20 is not 'better' either", R.verdict_line(10, 20).startswith("➖ **No clear"), R.verdict_line(10, 20))
check("verdict: too few", "Too few" in R.verdict_line(1, 5), R.verdict_line(1, 5))
check("listening day starts at 04:00 SGT", R.day_start(_sgt9(12, "02:00")) == _sgt9(11, "04:00")
      and R.day_start(_sgt9(12, "04:00")) == _sgt9(12, "04:00"))
# the usage footer: last night's reasoning against the fixed ceiling
def _batch9(bid, hhmm_day, detail):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO llm_batch (batch_id, created_at, status, detail) VALUES (?, ?, 'ok', ?)",
                     (bid, _sgt9(*hhmm_day), json.dumps(detail)))
_batch9(91, (20, "01:20"), {"reasoning": {"tokens": 21000, "max_tokens": 48000, "share": 0.4375, "cut_off": False}})
check("usage: quiet subtext under the warning share", R.usage_line(_sgt9(20, "23:30"))
      == "-# DeepSeek used 21k of its fixed 48k token limit last night (44%).", R.usage_line(_sgt9(20, "23:30")))
_batch9(92, (21, "01:20"), {"reasoning": {"tokens": 36000, "max_tokens": 48000, "share": 0.75, "cut_off": False}})
check("usage: ⚠️ over the warning share", R.usage_line(_sgt9(21, "23:30")).startswith("⚠️ DeepSeek used 36k")
      and "trim its request, don't raise the limit" in R.usage_line(_sgt9(21, "23:30")))
_batch9(93, (22, "01:20"), {"attempts": [{"thinking": True, "error": "reply cut off (max_tokens)", "max_tokens": 48000,
                                         "usage": {"completion_tokens": 48000, "completion_tokens_details": {"reasoning_tokens": 47900}}}]})
check("usage: a cut-off read from the attempts", "it was cut off" in R.usage_line(_sgt9(22, "23:30")), R.usage_line(_sgt9(22, "23:30")))
check("usage: no batch in 36 h", R.usage_line(_sgt9(25, "23:30")) == "⚠️ DeepSeek hasn't run in the last 36 h.")
_batch9(94, (26, "01:20"), {"attempts": [{"stage": "full", "thinking": True, "error": "no response within 250s"},
                                         {"stage": "fallback", "thinking": False, "ok": True}]})
check("usage: a timed-out thinking call is named", R.usage_line(_sgt9(26, "23:30"))
      == "⚠️ Last night's DeepSeek thinking call failed (no response within 250s).", R.usage_line(_sgt9(26, "23:30")))
check("stats baseline = the report benchmark", "artist-graph discovery, 1–6 Oct finished 6 of 23 judged (26%)" in L.stats(7))

# ── 9a. listener taste rules (v2.24: upbeat, female-led) ─────────────────────
for raw, want in (("girl_group", "girl_group"), ("Girl group", "girl_group"), ("female solo", "female_solo"),
                  ("female_band", "female_band"), ("Female-fronted band", "female_band"), ("boy group", "male"),
                  ("male", "male"), ("Male soloist", "male"), ("mixed", "mixed"), ("co-ed", "mixed"), ("", None), (None, None),
                  ("unknown", None), ("male & female duet", "mixed"), ("male/female", "mixed"),
                  ("male lead with female members", "male"), ("female-fronted band", "female_band"), ("malesolo", None)):
    check(f"act label {raw!r}", L._act(raw) == want, L._act(raw))
for raw, want in ((124, 124), (92.4, 92), ("88", 88), (" 101 ", 101), (30, 30), (True, None), (300, None), ("fast", None), (None, None), (0, None)):
    check(f"bpm {raw!r}", L._bpm(raw) == want, L._bpm(raw))
TCFG = L.load_config()
check("shipped taste rules: female-led, 95 BPM", (TCFG["vocal_filter"], TCFG["min_bpm"]) == ("female_led", 95) and L.taste_active(TCFG))
sp = L._system_prompt(TCFG)
check("system prompt carries both rules + labels", "6. Female-led acts only" in sp and "7. Upbeat songs only: about 95 BPM" in sp
      and '"act":"girl_group"' in sp and sp.index("7. Upbeat") < sp.index("The DATA block"), sp[-900:])
check("rules off → the base prompt", L._system_prompt(dict(TCFG, vocal_filter="any", min_bpm=0)) == L.SYSTEM_PROMPT)
check("the prompt goes to the model", "6. Female-led acts only" in L.build_messages(
      {"favourite_artists": [], "worked": [], "didnt_work": [], "more_from": [], "library_artists": [], "do_not_suggest": []},
      TCFG, "x")[0]["content"])
pj = L.parse_llm_json(json.dumps({"picks": [{"artist": f"A{i}", "title": f"T{i}", "origin": "KR", "act": a, "bpm": b}
                                            for i, (a, b) in enumerate([("girl_group", 124), ("boy group", 118), ("female solo", 72),
                                                                        ("mixed", "fast"), (None, 300)] * 3)],
                                  "similar_artists": [{"artist": "S", "known_song": "k", "act": "male"}]}), dict(TCFG, min_valid_picks=1))
check("parse: act / bpm labels", [(p_["act"], p_["bpm"]) for p_ in pj["picks"][:5]]
      == [("girl_group", 124), ("male", 118), ("female_solo", 72), ("mixed", None), (None, None)] and pj["similar"][0]["act"] == "male",
      [(p_["act"], p_["bpm"]) for p_ in pj["picks"][:5]])
tctx = {"cfg": TCFG, "positive_names": {L.compact("Day Six")}, "known_names": set(), "now": NOW8}
check("taste: a male act is dropped", L._taste_reject({"artist": "Boy Band", "act": "male", "bpm": 120}, tctx) == "male_act")
check("taste: a positive (finished) male artist may stay", L._taste_reject({"artist": "DAY SIX", "act": "male", "bpm": 120}, tctx) is None)
check("taste: under min_bpm is dropped — positive or not", L._taste_reject({"artist": "DAY SIX", "act": "male", "bpm": 80}, tctx) == "slow"
      and L._taste_reject({"artist": "G", "act": "girl_group", "bpm": 94}, tctx) == "slow")
check("taste: 95 BPM and up / no labels pass", L._taste_reject({"artist": "G", "act": "girl_group", "bpm": 95}, tctx) is None
      and L._taste_reject({"artist": "G"}, tctx) is None)
check("taste: rules off", L._taste_reject({"artist": "B", "act": "male", "bpm": 60}, dict(tctx, cfg=dict(TCFG, vocal_filter="any", min_bpm=0))) is None)
_s_calls = []
_s_orig = L._search
L._search = lambda q, kind, token, market, cfg: (_s_calls.append(q), [])[1]
for p_, want in (({"artist": "Boy Band", "title": "T", "fit": "close", "tier": None, "why": "", "act": "male", "bpm": 120}, "male_act"),
                 ({"artist": "Girl Act", "title": "Ballad", "fit": "close", "tier": None, "why": "", "act": "girl_group", "bpm": 70}, "slow")):
    r_ = L._resolve_one(1, p_, "tok", "SG", tctx, L._Budget(100), {})
    check(f"resolve: {want} filtered with no search", r_["status"] == "filtered" and r_["reason"] == want and not _s_calls, (r_, _s_calls))
r_sim = L._similar_one(1, {"artist": "Boy Band", "known_song": "x", "act": "male"}, "tok", "SG", tctx, L._Budget(100), {}, set())
check("similar: a male similar artist is filtered with no search (no male wildcards)", r_sim["reason"] == "male_act" and not _s_calls, r_sim)
r_unl = L._similar_one(2, {"artist": "Who Knows", "known_song": "x", "act": None}, "tok", "SG", tctx, L._Budget(100), {}, set())
check("similar: no act label → not scanned (unlabelled_act), no search", r_unl["reason"] == "unlabelled_act" and not _s_calls, r_unl)
r_pos = L._similar_one(3, {"artist": "Day Six", "known_song": "x", "act": None}, "tok", "SG", tctx, L._Budget(100), {}, set())
check("similar: an unlabelled positive artist is still looked up", r_pos["reason"] != "unlabelled_act" and _s_calls, r_pos)
_s_calls.clear()
L._search = _s_orig
okc = L._resolve_one(1, {"artist": "Girl Act", "title": "Banger", "fit": "close", "tier": None, "why": "", "act": "girl_group", "bpm": 128},
                     "tok", "SG", dict(tctx, known=set(), positive=set(), heard_ids=set(), heard_sigs=set(), heard_base=set(),
                                      blocked=set(), blocked_names=set(), taken_ids=set(), taken_sigs=set(), taken_artists=set()),
                     L._Budget(100), {L.norm_key("Girl Act", "Banger"): {"track_id": "gt" + "0" * 20, "name": "Banger", "artist_ids": [aid(9901)],
                                                                         "artist_names": ["Girl Act"], "duration_ms": 200000, "album_name": "A",
                                                                         "uri": "spotify:track:gt", "source": "llm"}})
check("a passing pick is stored with its labels + taste_gate", okc["status"] == "resolved" and okc["track"].get("taste_gate") is True
      and okc["track"].get("act") == "girl_group" and okc["track"].get("bpm") == 128, okc)
_fctx = dict(tctx, known=set(), positive=set(), heard_ids=set(), heard_sigs=set(), heard_base=set(), blocked=set(), blocked_names=set(),
             taken_ids=set(), taken_sigs=set(), taken_artists=set())
feat = L._resolve_one(2, {"artist": "Girl Singer", "title": "Duet", "fit": "close", "tier": None, "why": "", "act": "female_solo", "bpm": 120},
                      "tok", "SG", _fctx, L._Budget(100), {L.norm_key("Girl Singer", "Duet"): {
                          "track_id": "ft" + "0" * 20, "name": "Duet", "artist_ids": [aid(9902), aid(9903)],
                          "artist_names": ["Boy Act", "Girl Singer"], "duration_ms": 200000, "album_name": "A", "uri": "spotify:track:ft", "source": "llm"}})
check("a female act that is only FEATURED on a male act's track is dropped", feat["status"] == "filtered" and feat["reason"] == "featured_only", feat)
off = L._resolve_one(3, {"artist": "Girl Act", "title": "Banger", "fit": "close", "tier": None, "why": "", "act": "girl_group", "bpm": 128},
                     "tok", "SG", dict(_fctx, cfg=dict(TCFG, vocal_filter="any", min_bpm=0), taken_ids=set(), taken_sigs=set(), taken_artists=set()),
                     L._Budget(100), {L.norm_key("Girl Act", "Banger"): {"track_id": "gu" + "0" * 20, "name": "Banger", "artist_ids": [aid(9904)],
                                                                         "artist_names": ["Girl Act"], "duration_ms": 200000, "album_name": "A",
                                                                         "uri": "spotify:track:gu", "source": "llm"}})
check("rules off → no taste_gate mark (a later switch-on won't trust it)", off["status"] == "resolved" and off["track"].get("taste_gate") is False, off)
# the pool: only gated rows play while the rules are on; older similar artists need a label
db.DB_PATH = os.path.join(_TMP.name, "taste.db")
db.init_db()
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_batch (batch_id, created_at, status) VALUES (1, ?, 'ok')", (NOW8 - DAY,))
    for rank, extra in ((1, {"taste_gate": True}), (2, {})):
        c = dict(lt(50 + rank)); c.pop("taste_gate"); c.update(extra)
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, status, track_id, track, created_at) VALUES (1,'pick',?,?, 'resolved', ?, ?, ?)",
                     (rank, f"A{rank}", c["track_id"], json.dumps(c), NOW8 - DAY))
    slow = dict(lt(53)); slow.update(bpm=90)        # gated when min_bpm was lower
    conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, status, track_id, track, created_at) VALUES (1,'pick',3,'A3','resolved',?,?,?)",
                 (slow["track_id"], json.dumps(slow), NOW8 - DAY))
    for rank, track in ((1, {"spotify_name": "Old Male", "act": "male"}), (2, {"spotify_name": "Old"}), (3, {"spotify_name": "New Girl", "act": "girl_group"}),
                        (4, {"spotify_name": "Null Act", "act": None})):
        conn.execute("INSERT INTO llm_pick (batch_id, kind, rank, artist, status, artist_id, track, created_at) VALUES (1,'similar',?,?, 'resolved', ?, ?, ?)",
                     (rank, track["spotify_name"], aid(9800 + rank), json.dumps(track), NOW8 - DAY))
got = [c["track_id"] for c in L.load_candidates("pick", 7, NOW8)]
check("pool: unlabelled (pre-v2.24) picks don't play while the rules are on", got == [lt(51)["track_id"]], got)
_lc = L.load_config
L.load_config = lambda: dict(_lc(), vocal_filter="any", min_bpm=0)
check("pool: a stored pick under today's min_bpm is re-checked out", lt(53)["track_id"] not in got)
check("pool: rules off → every pick plays", len(L.load_candidates("pick", 7, NOW8)) == 3)
L.load_config = _lc
check("release scan: older similar artists need a female-led label", L._older_similar_artists(60, NOW8, labelled_only=True)
      == [(aid(9803), "New Girl")] and len(L._older_similar_artists(60, NOW8)) == 4, L._older_similar_artists(60, NOW8, labelled_only=True))
srcs_t = [a for a, _ in L.release_sources([], [], {"positive": set()}, TCFG, NOW8)]
check("release_sources passes labelled_only while the rules are on", aid(9803) in srcs_t
      and not {aid(9801), aid(9802), aid(9804)} & set(srcs_t), srcs_t)

# ── 9b. DeepSeek spend guard (v2.23) ─────────────────────────────────────────
db.DB_PATH = os.path.join(_TMP.name, "budget.db")
db.init_db()
class _Resp:
    def __init__(self, usage): self.status, self._b = 200, json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": usage}).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
_open_orig, _now_orig = L._OPENER.open, L._now_ms
_next = []
L._OPENER.open = lambda req, timeout=None: _next.pop(0) if not isinstance(_next[0], Exception) else (_ for _ in ()).throw(_next.pop(0))
CLOCK = [_sgt9(14, "17:15")]
L._now_ms = lambda: CLOCK[0]
def call(body, usage=None, exc=None):
    _next[:] = [exc if exc is not None else _Resp(usage or {})]
    try:
        L._post_json("https://x/chat/completions", body, "k", 5)
        return True
    except L.LLMError as e:
        call.last = str(e)
        return False
def wipe():
    with db.get_conn() as conn:
        conn.execute("DELETE FROM llm_usage")
B = lambda mt, chars: {"model": "m", "max_tokens": mt, "messages": [{"role": "user", "content": "x" * chars}]}
EST = lambda body: body["max_tokens"] + len(json.dumps(body["messages"])) // 3
CAP = L.load_config()["budget_tokens_day"]
check("shipped budget: 120k tokens per budget day from 01:00", (CAP, L.load_config()["budget_day_start_hour"]) == (120000, 1))
probe = B(48000, 17000)                 # ≈ 53.7k reserved, ~35k used
allowed = sum(call(probe, {"prompt_tokens": 6000, "completion_tokens": 29000}) for _ in range(23))
check("spend guard: the 10-12 probe burst stops after 2 calls (was 23)", allowed == 2, allowed)
check("spend guard: refusal names the budget", "local DeepSeek budget" in call.last and "120k" in call.last, call.last)
b = L.budget_state()
check("budget_state counts used + refused", b["used"] == 70000 and b["calls"] == 2 and b["refused"] == 21, b)
# the budget day: last night's worst case never starves tonight (review finding)
wipe()
CLOCK[0] = _sgt9(14, "01:20")
night = [(B(48000, 11000), {"prompt_tokens": 3600, "completion_tokens": 48000}),
         (B(24000, 10800), {"prompt_tokens": 3500, "completion_tokens": 24000}),
         (B(6000, 11000), {"prompt_tokens": 3600, "completion_tokens": 2400})]
check("worst-case night (cut off → small cut off → fallback) fits", all(call(b_, u) for b_, u in night)
      and L.budget_state()["used"] == 85100, L.budget_state())
CLOCK[0] = _sgt9(14, "14:00")
check("…then a daytime manual run is refused (ask first)", not call(B(48000, 11000), {"prompt_tokens": 3600, "completion_tokens": 24000}))
CLOCK[0] = _sgt9(15, "01:19")
check("…and the next night still starts with the whole allowance (01:00 reset, start-time jitter irrelevant)",
      call(B(48000, 11000), {"prompt_tokens": 3600, "completion_tokens": 24000}), call.last if hasattr(call, "last") else "")
wipe()
CLOCK[0] = _sgt9(16, "01:20")
check("01:20 times out (reservations kept) …", not call(B(48000, 11000), exc=TimeoutError("read timed out"))
      and L.budget_state()["used"] == EST(B(48000, 11000)), L.budget_state())
call(B(6000, 11000), exc=TimeoutError("read timed out"))
CLOCK[0] = _sgt9(16, "02:20")
check("… and the 02:20 retry's full thinking call still fits", call(B(48000, 11000), {"prompt_tokens": 3600, "completion_tokens": 24000}),
      L.budget_state())
wipe()
CLOCK[0] = _sgt9(17, "12:00")
exact = B(48000, 300)
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_usage (t_ms, caller, reserved, used, status) VALUES (?, 't', ?, ?, 'done')",
                 (CLOCK[0] - 3600e3, CAP - EST(exact), CAP - EST(exact)))
check("exact cap allowed", call(exact, {"prompt_tokens": 1, "completion_tokens": 1}))
wipe()
with db.get_conn() as conn:
    conn.execute("INSERT INTO llm_usage (t_ms, caller, reserved, used, status) VALUES (?, 't', ?, ?, 'done')",
                 (CLOCK[0] - 3600e3, CAP - EST(exact) + 1, CAP - EST(exact) + 1))
check("one token over the cap refused", not call(exact, {"prompt_tokens": 1, "completion_tokens": 1}))
wipe()
with db.get_conn() as conn:       # an in-flight call (used NULL) counts at its reservation
    conn.execute("INSERT INTO llm_usage (t_ms, caller, reserved, status) VALUES (?, 't', 100000, 'reserved')", (CLOCK[0] - 60e3,))
check("an in-flight reservation counts", not call(B(48000, 300), {"prompt_tokens": 1, "completion_tokens": 1}))
wipe()
with db.get_conn() as conn:       # yesterday's budget day doesn't count
    conn.execute("INSERT INTO llm_usage (t_ms, caller, reserved, used, status) VALUES (?, 't', 119000, 119000, 'done')",
                 (_sgt9(17, "00:59"),))
check("usage before 01:00 belongs to the previous budget day", call(B(48000, 300), {"prompt_tokens": 1, "completion_tokens": 1}))
wipe()
check("an HTTP 4xx releases its reservation", not call(probe, exc=urllib.error.HTTPError("https://x", 401, "auth", {}, None))
      and L.budget_state()["used"] == 0, L.budget_state())
check("an HTTP 5xx keeps it (may be billed)", not call(probe, exc=urllib.error.HTTPError("https://x", 503, "busy", {}, None))
      and L.budget_state()["used"] == EST(probe), L.budget_state())
wipe()
check("connection refused (never sent) releases it", not call(probe, exc=urllib.error.URLError(ConnectionRefusedError()))
      and L.budget_state()["used"] == 0, L.budget_state())
check("a dropped connection keeps it (may be billed)", not call(probe, exc=ConnectionResetError("reset"))
      and L.budget_state()["used"] == EST(probe), L.budget_state())
wipe()
check("odd usage fields keep the reservation, no crash", call(probe, {"prompt_tokens": "lots", "completion_tokens": None})
      and L.budget_state()["used"] == EST(probe), L.budget_state())
wipe()
check("no max_tokens → reserved as 64k", call({"messages": []}, {}) and L.budget_state()["used"] == 64000 + 2 // 3, L.budget_state())
# concurrency: the reservation is atomic across connections (BEGIN IMMEDIATE)
import threading  # noqa: E402
wipe()
_slow_now, _bcfg = L._now_ms, L._budget_cfg
L._budget_cfg = lambda: (CAP, 1)                       # no YAML parsing to space the racers out
L._now_ms = lambda: (time.sleep(0.2), CLOCK[0])[1]      # widen the read → insert window
granted, gate = [], threading.Barrier(6)
def racer():
    gate.wait()
    try:
        granted.append(L._budget_reserve(B(48000, 300)))
    except L.LLMError:
        pass
ts = [threading.Thread(target=racer) for _ in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
L._now_ms, L._budget_cfg = _slow_now, _bcfg
check("6 racing callers: exactly the 2 that fit are granted", len(granted) == 2, len(granted))
_gc = db.get_conn
db.get_conn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
check("spend guard fails closed", not call(probe, {}) and "budget unavailable" in call.last, call.last)
db.get_conn = _gc
wipe()
_next[:] = [_Resp({})]
L._post_json("https://x/models", {}, "k", 5)
check("non-chat URLs are not budgeted", L.budget_state()["calls"] == 0)
L._OPENER.open, L._now_ms = _open_orig, _now_orig

# ── 10. always-loaded docs stay small (v2.22) ────────────────────────────────
# CLAUDE.md is attached to every agent command in the skill dir; SKILL.md is loaded
# whenever the skill is used. Current state only — history goes to CHANGELOG.md.
for doc, limit in (("CLAUDE.md", 4000), ("SKILL.md", 15000)):
    n = len((SKILL / doc).read_text())
    check(f"{doc} ≤ {limit} chars (it is {n}) — move detail to references/, history to CHANGELOG.md", n <= limit, n)

check("the suite made no real network call", not NET_CALLS, NET_CALLS)

print(f"{PASSES[0]} passed, {len(FAILS)} failed")
for f in FAILS:
    print("  FAIL", f)
sys.exit(1 if FAILS else 0)
