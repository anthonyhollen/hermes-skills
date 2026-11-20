#!/usr/bin/env python3
"""
Away-mode / last-known-taste regression suite (v2.25.0).

Covers the vacation behaviour (user 2026-11-21): when the listener has not
played anything for a whole scoring window, the refresh must not produce an
empty playlist. It anchors the scoring clock at the last play, fills all 40
slots from the last known favourites (no new music), and can top up from the
90-day affinity history when the last active window is thin.

Run:  python3 scripts/test_taste_anchor.py     (exit 0 on success)
Needs only the stdlib + PyYAML; no network, no live DB (a temp DB is created).
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SRC = SKILL / "spotify-refresh" / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402

DAY = 86400 * 1000
now = int(time.time() * 1000)
tmpdir = tempfile.mkdtemp(prefix="taste-anchor-")
db.DB_PATH = os.path.join(tmpdir, "test.db")
db.init_db()

import scorer, composer  # noqa: E402

fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  [✓] {name}" + (f"  ({detail})" if detail else ""))
    else:
        fails.append(name)
        print(f"  [✗] {name}" + (f"  ({detail})" if detail else ""))


def add_track(tid, name, artist):
    db.upsert_track_cache([{
        "track_id": tid, "name": name, "artist_ids": [artist],
        "duration_ms": 180000, "album_name": "Test Album",
    }])


def add_full_play(tid, played_at):
    db.upsert_play_events([{
        "track_id": tid, "played_at": played_at,
        "duration_ms": 180000, "ms_played": 180000,
    }])


# ── 1. Empty DB: nothing to anchor on → real now, not inactive ───────────────
print("1. empty DB")
a, inactive = scorer.taste_anchor(now)
check("empty history is not inactive", inactive is False and a == now)

# ── 2. Last play 10 days ago (outside the 7-day window) → anchored ──────────
print("2. ten-day gap")
last = now - 10 * DAY
for i, aid in enumerate(("artistA", "artistB", "artistC", "artistD", "artistE", "artistF")):
    tid = f"liked{i:02d}" + "0" * 14
    add_track(tid, f"Test Track {i}", aid)
    add_full_play(tid, last)
    add_full_play(tid, last - 1 * DAY)

a, inactive = scorer.taste_anchor(now)
check("10-day gap is inactive", inactive is True)
check("anchor is the last play", a == last, f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(a / 1000))}")

scores, _ = scorer.compute_affinity(now_ms=a)
liked = scorer.get_liked_tracks(scores)
check("last active window scores likes", len(liked) >= 6, f"{len(liked)} liked")

# ── 3. An old like exists only in the 90-day snapshot history ───────────────
print("3. historical likes")
old_tid = "old" + "0" * 19
add_track(old_tid, "Old Favourite", "artistOld")
db.write_snapshot(1, {old_tid: 2.5}, now - 20 * DAY)
db.write_snapshot(2, {liked[0][0]: 1.4}, now - 1 * DAY)      # newer → ranks first
hist = db.get_historical_likes(now - composer.HISTORY_LIKE_DAYS * DAY, scorer.LIKE_THRESHOLD)
hist_ids = [h["track_id"] for h in hist]
check("history finds the old like", old_tid in hist_ids)
check("history is newest-liked first", hist_ids and hist_ids[0] == liked[0][0],
      f"first={hist_ids[0][:8] if hist_ids else '-'}")

# ── 4. Away-mode composition: favourites only, no new music ─────────────────
print("4. away-mode composition")
tracks = composer.compose_playlist(scores, dry_run=True, inactive=True)
srcs = {t["source"] for t in tracks}
ids = [t["track_id"] for t in tracks]
check("all slots come from favourites/history", "llm" not in srcs and "wildcard" not in srcs
      and "encore" not in srcs and "discovery" not in srcs, f"sources={sorted(srcs)}")
check("no duplicate track", len(ids) == len(set(ids)))
check("history top-up reached the playlist", old_tid in ids)
check("LAST_INFO reports away mode", composer.LAST_INFO.get("inactive") is True
      and composer.LAST_INFO.get("history_fill", 0) >= 1, str(composer.LAST_INFO))

# ── 5. One recent play flips it back to active ──────────────────────────────
print("5. back to active")
recent_tid = "recent" + "0" * 16
add_track(recent_tid, "Recent Track", "artistRecent")
add_full_play(recent_tid, now - 1 * DAY)
a2, inactive2 = scorer.taste_anchor(now)
check("recent play is active", inactive2 is False and a2 == now)

# Active composition smoke: fake token; the DeepSeek store is DB-only.
import client  # noqa: E402
client.get_token = lambda: "fake-token"
scores_now, _ = scorer.compute_affinity(now_ms=now)
try:
    tracks2 = composer.compose_playlist(scores_now, dry_run=True, inactive=False)
    ok = isinstance(tracks2, list) and all(t.get("uri") for t in tracks2)
    detail = f"{len(tracks2)} tracks"
except Exception as e:      # noqa: BLE001
    ok, detail = False, f"{type(e).__name__}: {e}"
check("active composition still runs", ok, detail)

shutil.rmtree(tmpdir, ignore_errors=True)
print(f"\n{'✓ PASS' if not fails else '✗ FAIL'}: {checks - len(fails)}/{checks} checks")
if fails:
    print("failed:", ", ".join(fails))
sys.exit(1 if fails else 0)
