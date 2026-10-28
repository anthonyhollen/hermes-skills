#!/usr/bin/env python3
"""
Verify the track-name filters behave correctly against a battery of
positive/negative test cases:
  - composer.py: _is_live_version, _is_instrumental_version
  - exclusions.py: is_baby_sleep_name (baby-sleep / white-noise / lullaby)

Also verifies three non-name filters, each in its own section:
  - composer.py: _is_unplayable — region-locked tracks, keyed on the API's
    is_playable / restrictions fields (data check, takes a track dict)
  - exclusions.is_album_excluded_track — whole-album exclusions from
    config.yaml → exclude.albums, keyed on track ID
  - exclusions.is_album_name_excluded — the general album-NAME rule from
    config.yaml → exclude.album_name_contains (case-insensitive substring)

Use this BEFORE trusting a regex addition or tweak — py_compile accepts broken
regexes (re.compile errors at runtime, not parse time), so syntactically valid
code can still have a filter that silently matches nothing.

Usage:
    python3 scripts/test_track_name_filters.py
    python3 scripts/test_track_name_filters.py  # prints PASS/FAIL summary

Exit code: 0 on success, 1 on any test failure.
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

# composer.py / exclusions.py live under spotify-refresh/src/ (this script sits
# at the skill root under scripts/), so parents[1] is the skill root — descend
# into spotify-refresh/src to reach them.
SRC = Path(__file__).resolve().parents[1] / "spotify-refresh" / "src"
COMPOSER = SRC / "composer.py"
EXCLUSIONS = SRC / "exclusions.py"


def _extract(source_path, re_names, fn_names):
    """Extract named regex assignments + filter functions from a source file
    without importing the module (avoids db/client/scorer deps)."""
    src = source_path.read_text()
    tree = ast.parse(src)
    ns = {"re": re}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in re_names:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<ast>", "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name in fn_names:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<ast>", "exec"), ns)
    return ns


def _load_filters_from_source():
    """Return (is_live_version, is_instrumental_version, is_remix_version, is_baby_sleep_name)."""
    comp = _extract(
        COMPOSER,
        {"_LIVE_VERSION_RE", "_INSTRUMENTAL_RE", "_REMIX_RE"},
        {"_is_live_version", "_is_instrumental_version", "_is_remix_version"},
    )
    excl = _extract(EXCLUSIONS, {"_BABY_SLEEP_RE"}, {"is_baby_sleep_name"})
    return (comp["_is_live_version"], comp["_is_instrumental_version"],
            comp["_is_remix_version"], excl["is_baby_sleep_name"])


def _load_unplayable_filter():
    """
    Extract composer._is_unplayable. Separate from _load_filters_from_source
    because it takes a raw track DICT, not a name — it reads the API's
    is_playable / restrictions fields rather than matching a regex.
    """
    return _extract(COMPOSER, set(), {"_is_unplayable"})["_is_unplayable"]


def _load_dedupe_helpers():
    """
    Extract _normalize_track_name / _track_sig / _dedupe_tracks from composer.py
    without importing the module (they only depend on `re`; exec order in the
    shared namespace doesn't matter because functions resolve globals at call
    time — all three land in ns before any is invoked).
    """
    return _extract(
        COMPOSER,
        set(),
        {"_normalize_track_name", "_track_sig", "_dedupe_tracks"},
    )


def _load_exclusions_module():
    """
    Import exclusions.py for real (it only needs os/re/yaml — no db/client deps),
    so the album exclusions are read from the live config.yaml rather than a copy
    of the list duplicated into this test.
    """
    spec = importlib.util.spec_from_file_location("exclusions_under_test", EXCLUSIONS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Test battery ──────────────────────────────────────────────────────────────
# A track is "blocked" if ANY filter (live / instrumental / remix / baby-sleep) returns True.
# Add new variants here when extending the filters.

MUST_BLOCK = [
    # Live variants — regression set, must keep working
    ("Creep (Live)", "live"),
    ("Stairway to Heaven - Live", "live"),
    ("Wonderwall [Live at Glastonbury]", "live"),
    ("Levitating (Live from Tokyo)", "live"),
    # Live — trailing / quote / tour markers (no dash immediately before "Live")
    ('NEW DANCE - XG 1st WORLD TOUR "The first HOWL" Live', "live: trailing after quote + tour (XG)"),
    ("Bohemian Rhapsody Live at Wembley", "live: 'Live at' without a dash"),
    ("Some Song - World Tour Recording Live", "live: '...Tour...Live' trailing marker"),
    # Live — "Live in/on" ONLY after a delimiter (bare "Live in X" is kept — see MUST_KEEP)
    ("Anti-Hero (Live in Paris)", "live: 'Live in' after paren delimiter"),
    ("Anti-Hero - Live in Tokyo", "live: 'Live in' after dash delimiter"),
    # Em-dash (U+2014) separator — normalized into all three dash branches
    ("Some Song — Live", "live: em-dash separator"),
    ("Some Song — Instrumental", "instrumental: em-dash separator"),
    ("Some Song — Remix", "remix: em-dash separator"),
    # Instrumental — parenthesized
    ("DREAM (Instrumental)", "instrumental"),
    ("Watermelon Sugar (instrumental)", "instrumental (case-insensitive)"),
    # Instrumental — bracketed
    ("Starlight [Instrumental]", "instrumental"),
    # Instrumental — dash / en-dash separators
    ("Sign of the Times - Instrumental", "instrumental"),
    ("Sign of the Times – Instrumental", "instrumental (en-dash)"),
    # Instrumental — Spotify-owned catalogue
    ("Levitating Instrumental Version", "instrumental"),
    ("Levitating (Instrumental Version)", "instrumental"),
    # Instrumental — "Inst." abbreviation (e.g. izna "Psycho - Inst.")
    ("Psycho - Inst.", "instrumental abbreviation, dash separator"),
    ("Psycho – Inst.", "instrumental abbreviation, en-dash separator"),
    ("Psycho (Inst.)", "instrumental abbreviation, parenthesized"),
    ("Psycho [Inst.]", "instrumental abbreviation, bracketed"),
    ("psycho - inst.", "instrumental abbreviation (case-insensitive)"),
    # Karaoke
    ("Flowers Karaoke Version", "karaoke"),
    ("Flowers (Karaoke Version)", "karaoke"),
    ("KARAOKE version", "karaoke (case-insensitive)"),
    # Remix — parenthesized / bracketed / dash / trailing / with artist
    ("SIGN (Remix)", "remix, parenthesized (the reported track)"),
    ("SIGN [Remix]", "remix, bracketed"),
    ("Closer (Tiësto Remix)", "remix with artist inside parens"),
    ("Titanium - Alesso Remix", "remix with artist after dash"),
    ("Levitating - Remix", "remix after dash"),
    ("Blinding Lights Remix", "trailing remix, no delimiter"),
    ("Padam Padam (The Remixes)", "remixes compilation marker"),
    ("Faded (Remixed)", "remixed variant"),
    ("sign (remix)", "remix (case-insensitive)"),
    # Baby-sleep / white-noise / lullaby — regression set
    ("Relaxing Baby Sleep Music", "baby sleep (the seed track)"),
    ("Deep Sleep Music", "sleep music"),
    ("Sleep Sounds for Babies", "sleep sounds"),
    ("White Noise for Newborns", "white noise"),
    ("Brown Noise 10 Hours", "brown noise"),
    ("Gentle Lullaby", "lullaby"),
    ("Twinkle Twinkle Lullabies", "lullabies"),
    ("Ocean Womb Sounds", "womb sounds"),
    ("Nursery Rhymes Collection", "nursery rhyme"),
    ("Calming Music for Toddlers", "calming music"),
    ("Soothing Sounds", "soothing sounds"),
    ("Rain Sounds for Sleeping", "rain sounds"),
    ("Nature Sounds Vol. 3", "nature sounds"),
]

MUST_KEEP = [
    # Song names containing "Live" / "Alive" without parens/bracket/dash
    ("Live and Let Die", "song title, not marker"),
    ("Stayin' Alive", "'Alive' not 'Live'"),
    # Bare / leading "Live" that must NOT trip the new trailing-marker branches
    ("Long Live", "trailing 'Live' but no quote/tour/at-from context"),
    ("Live Forever", "leading 'Live', not a live-recording marker"),
    ("Detour to Nowhere", "'tour' mid-word + no trailing 'Live'"),
    # Bare "Live in/on ..." (no delimiter) — studio titles, must be KEPT
    # (the ambiguous in/on prepositions only fire after a paren/bracket/dash)
    ("Live in the Moment", "studio: bare 'Live in', no delimiter"),
    ("Live in Color", "studio: bare 'Live in', no delimiter"),
    ("We Live On", "studio: bare 'Live On', no delimiter"),
    ("How to Live On", "studio: bare 'Live On', no delimiter"),
    # "Instrumental" as a word / band name (no anchor)
    ("The Instrumentals", "band name"),
    ("Instrumental", "bare word, no anchor"),
    ("Instrumentally Yours", "part of larger word — word boundary"),
    ("My Karaoke Night", "'Karaoke' but no 'Version' terminator"),
    # "Inst"-prefixed words the abbreviation branch must NOT catch (needs the "." period)
    ("Instant Crush", "'Inst' starts a word, no period — not the abbreviation"),
    ("Some Song - Instant Classic", "dash + 'Instant', no 'Inst.' period"),
    ("Instinct", "'Inst' mid-word, not the abbreviation"),
    ("Install (Deluxe)", "'Inst' starts a word, not the abbreviation"),
    # "remix" as a substring of another word must NOT trip the remix filter
    ("Remixing Hearts", "'Remix' mid-word (remixING) — word boundary"),
    ("Premix", "'remix' inside 'premix' — word boundary"),
    # Unrelated version markers
    ("Bohemian Rhapsody - Remastered 2011", "remaster, not filtered"),
    ("Closer (Acoustic Version)", "acoustic, not filtered"),
    ("Watermelon Sugar (Extended Version)", "extended, not filtered"),
    # Bare words the baby-sleep filter must NOT catch (needs sleep-MUSIC context)
    ("Sleep", "bare 'sleep', no sleep-music phrase"),
    ("Sleepyhead", "'sleep' mid-word, word boundary"),
    ("Ocean", "bare 'ocean', not 'nature/womb sounds'"),
    ("Rain", "bare 'rain', not 'rain sounds'"),
    ("Purple Rain", "'rain' but not 'rain sounds'"),
    ("Sound of Silence", "'sound' but no sleep-music phrase"),
    ("Music", "bare 'music', not 'sleep music'"),
    # Plain track
    ("DREAM", "normal track"),
]

# ── Album exclusion battery (ID-based, not name-based) ────────────────────────
# config.yaml → exclude.albums pins whole albums by track ID. These titles are
# deliberately NOT matched by any name filter (nothing in "PUPPET SHOW x XDM"
# reads as a version marker), which is exactly why the album list exists.
ALBUM_MUST_BLOCK = [
    ("1zoaw0B1QheyRnA7pZEqCR", "HESONOO + X-GENE x XDM"),
    ("5W5yP5z0QkTsk06GSX2GNX", "GRL GVNG x XDM"),
    ("1a5Dhd82KaUqmz3sQEDNbs", "WOKE UP x XDM"),
    ("5yRRQlRb1b8J3DiDHMsB1P", "SOMETHING AIN'T RIGHT x XDM"),
    ("3JyviRYTc6zWPuQtZtgFw2", "TGIF x XDM"),
    ("0L7zZ3Wl9KVCaGpl2vzXnH", "PUPPET SHOW x XDM"),
    ("0ytRjAjsycFlu3ZA1bVpiJ", "TIPPY TOES x XDM"),
    ("28bgnX4IxlSiNgTyh3iAU9", "NEW DANCE x XDM"),
    # The CTLBT soundtrack album (5Qg2iNuV6zTCGlIYunvvTd, formerly 31 IDs here)
    # was MERGED into the general album-name rule below — its name contains the
    # substring, so it is no longer tracked by track ID.
]

ALBUM_MUST_KEEP = [
    ("0p0Y3AVfPRHXQBSFOgtHIt", "an unrelated track ID must not be album-excluded"),
    ("", "empty track ID must not be album-excluded"),
]

# ── Album-NAME exclusion battery (general rule, output-only) ─────────────────
# config.yaml → exclude.album_name_contains: any track whose album name CONTAINS
# one of the substrings (case-insensitive) is dropped from the playlist output.
# The per-ID "exclude.albums" entry for "Can This Love Be Translated? (Soundtrack
# from the Netflix Series)" (album 5Qg2iNuV6zTCGlIYunvvTd) was MERGED into this
# rule — its name contains the substring, so the general rule subsumes the
# 31-ID list exactly. Matched against the LIVE config in main().
ALBUM_NAME_MUST_BLOCK = [
    ("Can This Love Be Translated? (Soundtrack from the Netflix Series)",
     "merged CTLBT album — same match as the old ID list"),
    ("Twilight Lovers (Soundtrack from the Netflix Series)", "same-category album"),
    ("Some Show (soundtrack from the netflix series)", "case-insensitive"),
    ("(Soundtrack from the Netflix Series)", "bare substring"),
]
ALBUM_NAME_MUST_KEEP = [
    ("XDM Unidentified Waves", "different category — still ID-based only"),
    ("Stranger Things (Soundtrack)", "'Soundtrack' but not the Netflix Series substring"),
    ("Soundtrack from the Netflix Series", "no parentheses — filter includes them, not matched"),
    ("", "empty album name — never excluded"),
]

# ── Unplayable / region-locked battery (data-based, not name-based) ───────────
# composer._is_unplayable() reads the API's playability fields off a raw track
# dict. Fail-open: only an explicit is_playable=False or a present
# restrictions.reason counts. Reported cases: izna "Animal Fries" (4RVUZ…),
# "PROUD CITIZEN" (139Ef…) — both is_playable=False, restrictions.reason=market.
UNPLAYABLE_MUST_BLOCK = [
    ({"name": "Animal Fries", "is_playable": False,
      "restrictions": {"reason": "market"}}, "reported: is_playable False + market restriction"),
    ({"name": "PROUD CITIZEN", "is_playable": False,
      "restrictions": {"reason": "market"}}, "reported: is_playable False + market restriction"),
    ({"name": "No market field", "is_playable": False},
     "is_playable False alone (no restrictions key)"),
    ({"name": "Restriction only", "restrictions": {"reason": "market"}},
     "restrictions present but is_playable absent (request omitted market=)"),
    ({"name": "Explicit restriction", "restrictions": {"reason": "explicit"}},
     "any restriction reason counts, not just 'market'"),
]

UNPLAYABLE_MUST_KEEP = [
    ({"name": "METRONOME", "is_playable": True, "restrictions": None},
     "explicitly playable"),
    ({"name": "Plain track"}, "neither field present — fail open (cached/core tracks)"),
    ({"name": "Empty restrictions", "is_playable": True, "restrictions": {}},
     "empty restrictions dict is not a restriction"),
    ({"name": "No reason key", "is_playable": True, "restrictions": {"other": "x"}},
     "restrictions without a 'reason' key is not a restriction"),
]

# ── Same-song/same-artist dedupe battery ─────────────────────────────────────
# composer._track_sig / _dedupe_tracks: catches the same song from the same
# artist(s) under DIFFERENT track IDs (single release vs album release), which
# track-ID dedupe alone misses. Reported case: 'Shapeshifter' by Baby DONT Cry
# — standalone single 0kkqkpgbkLFpopZqPAH1CO vs 'The 1st Mini Album [AFTER
# CRY]' 6ONjO8j2uE9Y9wPgbYR3fw. Each pair: (track_a, track_b, should_dedupe,
# note). When should_dedupe, the FIRST track must be the one kept.
DEDUPE_PAIRS = [
    ({"track_id": "0kkqkpgbkLFpopZqPAH1CO", "name": "Shapeshifter",
      "artist_ids": ["0dXoF2Q2WnVrhRT26SJNvs"]},
     {"track_id": "6ONjO8j2uE9Y9wPgbYR3fw", "name": "Shapeshifter",
      "artist_ids": ["0dXoF2Q2WnVrhRT26SJNvs"]},
     True, "reported: single vs mini-album release, same artist"),
    ({"track_id": "a1", "name": "Blinding Lights", "artist_ids": ["x"]},
     {"track_id": "a2", "name": "blinding  lights", "artist_ids": ["x"]},
     True, "case + whitespace normalization"),
    ({"track_id": "b1", "name": "Song", "artist_ids": ["x"]},
     {"track_id": "b2", "name": "Song", "artist_ids": ["y"]},
     False, "same name, DIFFERENT artist — not a duplicate"),
    ({"track_id": "c1", "name": "Song", "artist_ids": ["x"]},
     {"track_id": "c2", "name": "Song (Sped Up)", "artist_ids": ["x"]},
     False, "different version marker — not a duplicate"),
    ({"track_id": "d1", "name": "Ice Princess (Kor Ver.)", "artist_ids": ["z"]},
     {"track_id": "d2", "name": "Ice Princess", "artist_ids": ["z"]},
     False, "different language version — not a duplicate"),
    ({"track_id": "e1", "name": "Duet", "artist_ids": ["x", "y"]},
     {"track_id": "e2", "name": "Duet", "artist_ids": ["y", "x"]},
     True, "artist order differs but same set"),
    ({"track_id": "f1", "name": "", "artist_ids": []},
     {"track_id": "f2", "name": "", "artist_ids": []},
     False, "no name/artist info — never signature-deduped"),
    ({"track_id": "g1", "name": "Same Name", "artist_ids": []},
     {"track_id": "g2", "name": "Same Name", "artist_ids": []},
     False, "no artist IDs and no artist names — kept distinct"),
]


def main():
    live, inst, remix, baby = _load_filters_from_source()

    def blocked(name):
        return live(name) or inst(name) or remix(name) or baby(name)

    fails = []
    print("=== MUST BLOCK ===")
    for name, note in MUST_BLOCK:
        ok = blocked(name)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {name!r}  ({note})")
        if not ok:
            fails.append(name)

    print()
    print("=== MUST KEEP ===")
    for name, note in MUST_KEEP:
        ok = not blocked(name)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {name!r}  ({note})")
        if not ok:
            fails.append(name)

    # ── Album exclusions (ID-based) ───────────────────────────────────────────
    excl = _load_exclusions_module()

    print()
    print("=== ALBUM EXCLUSIONS — MUST BLOCK ===")
    for tid, title in ALBUM_MUST_BLOCK:
        ok = excl.is_album_excluded_track(tid)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {tid}  ({title})")
        if not ok:
            fails.append(f"{tid} ({title})")

    print()
    print("=== ALBUM EXCLUSIONS — MUST KEEP ===")
    for tid, note in ALBUM_MUST_KEEP:
        ok = not excl.is_album_excluded_track(tid)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {tid!r}  ({note})")
        if not ok:
            fails.append(f"{tid} ({note})")

    # ── Album-NAME exclusions (general rule) ─────────────────────────────────
    print()
    print("=== ALBUM-NAME EXCLUSIONS — MUST BLOCK ===")
    for album_name, note in ALBUM_NAME_MUST_BLOCK:
        ok = excl.is_album_name_excluded(album_name)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {album_name!r}  ({note})")
        if not ok:
            fails.append(f"{album_name} (album-name block)")

    print()
    print("=== ALBUM-NAME EXCLUSIONS — MUST KEEP ===")
    for album_name, note in ALBUM_NAME_MUST_KEEP:
        ok = not excl.is_album_name_excluded(album_name)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {album_name!r}  ({note})")
        if not ok:
            fails.append(f"{album_name} (album-name keep)")

    # is_output_excluded_track is the composer's entry point — confirm it unions
    # the ID-based album list + the general album-name rule with the name/artist
    # exclusions, and that plain tracks pass.
    print()
    print("=== is_output_excluded_track (composer entry point) ===")
    union_cases = [
        # (name, artist_ids, track_id, album_name) → expected
        (("PUPPET SHOW x XDM", ["0LOK81e9H5lr61HlGGHqwA"], "0L7zZ3Wl9KVCaGpl2vzXnH", ""), True,
         "ID-based album-excluded track"),
        (("Deep Sleep Music", [], "0p0Y3AVfPRHXQBSFOgtHIt", ""), True,
         "baby-sleep name still excluded"),
        (("Love Language", ["0LOK81e9H5lr61HlGGHqwA"],
          "6w9SgpKIzB54R2xHml1UPP", "Can This Love Be Translated? (Soundtrack from the Netflix Series)"), True,
         "merged CTLBT track: album-name rule (was ID-based)"),
        (("Love Language", ["0LOK81e9H5lr61HlGGHqwA"], "6w9SgpKIzB54R2xHml1UPP", ""), False,
         "same track, album name unknown — kept (ID list no longer holds it)"),
        (("PUPPET SHOW", ["0LOK81e9H5lr61HlGGHqwA"], "0p0Y3AVfPRHXQBSFOgtHIt", ""), False,
         "same artist, different track — kept"),
    ]
    for (name, artist_ids, tid, album_name), expected, note in union_cases:
        got = excl.is_output_excluded_track(name, artist_ids, tid, album_name)
        ok = got == expected
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {name!r} (album {album_name[:45]!r}) → {got} (expected {expected}; {note})")
        if not ok:
            fails.append(f"{name} (is_output_excluded_track)")

    # Album exclusions must NOT leak into the pre-scoring path — is_excluded_track
    # is what scorer.compute_affinity() calls, and the artist here is genuinely liked.
    scoring_leak = excl.is_excluded_track("PUPPET SHOW x XDM", ["0LOK81e9H5lr61HlGGHqwA"])
    ok = not scoring_leak
    marker = "✓" if ok else "✗ FAIL"
    print(f"  [{marker}] is_excluded_track('PUPPET SHOW x XDM') → {scoring_leak} "
          f"(expected False; album exclusions are output-only, must not block scoring)")
    if not ok:
        fails.append("album exclusion leaked into pre-scoring path")

    # ── Unplayable / region-locked (data-based) ───────────────────────────────
    unplayable = _load_unplayable_filter()

    print()
    print("=== UNPLAYABLE — MUST BLOCK ===")
    for track, note in UNPLAYABLE_MUST_BLOCK:
        ok = unplayable(track)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {track.get('name')!r}  ({note})")
        if not ok:
            fails.append(f"{track.get('name')} (unplayable)")

    print()
    print("=== UNPLAYABLE — MUST KEEP ===")
    for track, note in UNPLAYABLE_MUST_KEEP:
        ok = not unplayable(track)
        marker = "✓" if ok else "✗ FAIL"
        print(f"  [{marker}] {track.get('name')!r}  ({note})")
        if not ok:
            fails.append(f"{track.get('name')} (unplayable false-positive)")

    # ── Same-song/same-artist dedupe ─────────────────────────────────────────
    dedupe_tracks = _load_dedupe_helpers()["_dedupe_tracks"]

    print()
    print("=== SAME-SONG/SAME-ARTIST DEDUPE ===")
    for a, b, should_dedupe, note in DEDUPE_PAIRS:
        out = dedupe_tracks([a, b])
        deduped = len(out) == 1
        ok = deduped == should_dedupe
        # First occurrence must win (core/liked precede discovery in the pool)
        if ok and should_dedupe:
            ok = out and out[0]["track_id"] == a["track_id"]
        marker = "✓" if ok else "✗ FAIL"
        kept = out[0]["track_id"] if out else None
        print(f"  [{marker}] {a['name']!r} vs {b['name']!r} → {len(out)} track(s) "
              f"(kept {kept}; expected {'1 (first)' if should_dedupe else '2'}; {note})")
        if not ok:
            fails.append(f"{a['name']} vs {b['name']} (dedupe)")

    print()
    if fails:
        print(f"✗ FAIL: {len(fails)} test(s) failed")
        for n in fails:
            print(f"    - {n!r}")
        sys.exit(1)
    total = (len(MUST_BLOCK) + len(MUST_KEEP) + len(ALBUM_MUST_BLOCK)
             + len(ALBUM_MUST_KEEP) + len(ALBUM_NAME_MUST_BLOCK)
             + len(ALBUM_NAME_MUST_KEEP) + len(union_cases) + 1
             + len(UNPLAYABLE_MUST_BLOCK) + len(UNPLAYABLE_MUST_KEEP)
             + len(DEDUPE_PAIRS))
    print(f"✓ PASS: {total} tests")


if __name__ == "__main__":
    main()