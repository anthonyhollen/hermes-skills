#!/usr/bin/env python3
"""
New-music layout regression suite (v2.25.1).

Covers the 22 Nov 2026 defect: after a listening break the scoring window is
thin, so the core section is empty and every favourite arrives via the fallback
fill. The mix layout counted only `core` as a favourite, so `_spread([], ...)`
returned the new songs first — a contiguous block at positions 1–12 (v56), where
a listener resuming mid-playlist never reaches them (all 23 plays that day were
favourites; the DeepSeek skip report had nothing to count).

`composer._apply_layout()` must treat fallback-filled favourites as favourites
and spread the new songs through them.

Run:  python3 scripts/test_new_music_layout.py     (exit 0 on success)
Needs only the stdlib + PyYAML; no network, no DB.
"""
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "spotify-refresh" / "src"))

import composer  # noqa: E402

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


def pool(n_core, n_fallback, n_llm, n_wild=0, n_disc=0):
    out = []
    for i in range(n_core):
        out.append({"track_id": f"core{i}", "name": f"Core {i}", "source": "core"})
    for i in range(n_fallback):
        out.append({"track_id": f"fb{i}", "name": f"Fallback {i}", "source": "fallback"})
    for i in range(n_llm):
        out.append({"track_id": f"llm{i}", "name": f"LLM {i}", "source": "llm"})
    for i in range(n_wild):
        out.append({"track_id": f"wild{i}", "name": f"Wild {i}", "source": "wildcard"})
    for i in range(n_disc):
        out.append({"track_id": f"disc{i}", "name": f"Disc {i}", "source": "discovery"})
    return out


def new_positions(ordered):
    return [i for i, t in enumerate(ordered) if t["source"] in ("llm", "wildcard", "encore")]


def max_gap(pos):
    if len(pos) < 2:
        return 0
    return max(b - a for a, b in zip(pos, pos[1:]))


def fav(t):
    return t["source"] in ("core", "fallback")


def check_mix(name, src):
    out = composer._apply_layout(src, use_llm=True)
    pos = new_positions(out)
    check(f"{name}: all tracks kept", len(out) == len(src), f"{len(out)}/{len(src)}")
    check(f"{name}: permutation preserved",
          sorted(t["track_id"] for t in out) == sorted(t["track_id"] for t in src))
    check(f"{name}: first 3 are favourites",
          all(fav(t) for t in out[:composer.MIX_LEAD_CORE]), str([t["source"] for t in out[:3]]))
    check(f"{name}: new songs spread (max gap ≤ 4)", max_gap(pos) <= 4, f"positions {pos}")
    check(f"{name}: not a block at the top", pos[:4] != list(range(pos[0], pos[0] + 4)) if pos else False,
          f"positions {pos}")


print("mix layout, empty core (the 22 Nov case: 28 favourites + 12 DeepSeek picks)")
check_mix("empty-core", pool(0, 28, 12))

print("\nmix layout, healthy core (12 core + 16 fallback + 12 picks)")
check_mix("healthy-core", pool(12, 16, 12))

print("\nmix layout with wildcards (12 core + 8 fallback + 12 picks + 4 wildcards)")
check_mix("wildcards", pool(12, 8, 12, n_wild=4))

print("\nno DeepSeek songs (use_llm=False keeps the composed order)")
src = pool(24, 16, 0)
check("no-llm: order untouched", composer._apply_layout(src, use_llm=False) == src)

print("\nblock layout (config new_music_layout: block)")
saved = composer.NEW_MUSIC_LAYOUT
composer.NEW_MUSIC_LAYOUT = "block"
try:
    out = composer._apply_layout(pool(0, 28, 12), use_llm=True)
    check("block: all favourites first", all(fav(t) for t in out[:28]))
    check("block: new songs after", all(t["source"] == "llm" for t in out[28:]))
finally:
    composer.NEW_MUSIC_LAYOUT = saved

print("\naway mode (inactive=True, no new music)")
out = composer._apply_layout(pool(36, 4, 0), inactive=True)
check("away: 40 tracks", len(out) == 40)
check("away: first 3 core", all(t["source"] == "core" for t in out[:3]))

print(f"\n{checks - len(fails)}/{checks} checks passed")
if fails:
    print("FAILED: " + ", ".join(fails))
    sys.exit(1)
