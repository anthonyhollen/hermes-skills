# Output filters, category exclusions & album exclusions

Relocated from SKILL.md (v2.17.0, 2026-09-23) to keep the hot path lean; updated
for the v2.17.0 composer (guardrails applied while picking — see
`references/scoring-and-composition.md`).

All output checks except dedupe/caps live in one predicate,
**`composer._passes_output_filters(t, blocked)`**: blocklist → live →
instrumental → remix → unplayable → category + album exclusions
(`exclusions.is_output_excluded_track`). It is called by `_Picker.try_add`
(every section and every fallback stage) and at discovery ingestion
(`_pick_discoveries._consider`). Regression cases for every filter live in
`scripts/test_track_name_filters.py` (skill root; 120/120 as of 2026-09-23).

## Category exclusions (baby-sleep music etc.) — pre-scoring

Some tracks are played for reasons unrelated to taste (e.g. baby-sleep /
white-noise music played on repeat to settle a baby). Left alone these score as
repeated full listens → climb into the Like bucket → become core favorites, AND
turn their artists into discovery/wildcard "top artists" that pull in *more* of
the same. So they are excluded **before scoring** (in `scorer.compute_affinity()`
via `exclusions.is_excluded_track()`) — an excluded track earns no affinity and
never contaminates artist-graph selection — and again as a composition guardrail
(defense in depth, via `is_output_excluded_track()`). See `src/exclusions.py`.

A track is excluded if EITHER:
- **any of its `artist_ids`** is in `config.yaml` → `exclude.artist_ids` (precise;
  add IDs from `open.spotify.com/artist/<ID>` as you find new sleep-music
  artists), OR
- **its name matches the baby-sleep filter** (`is_baby_sleep_name`) — `baby sleep`,
  `sleep music`, `sleep/rain/nature/womb sounds`, `white/pink/brown noise`,
  `lullaby/lullabies`, `nursery rhyme`, `calming/soothing music/sounds`. Anchored to
  sleep-*music* phrasing so bare titles like "Sleep", "Ocean", "Rain",
  "Purple Rain" are NOT caught. (Accepted tradeoff: standalone "Lullaby"/"Nursery Rhyme"
  pop titles *are* caught — the artist-id list is the precise lever.)

Unlike the live/instrumental/remix filters (output-only), these run at the
scorer level too.

## Live version filter (`_is_live_version`)
Tracks with live-performance markers in their names are excluded from all
sections. Matches standard Spotify conventions: `(Live)`, `(Live at...)`,
`[Live]`, `- Live`, `– Live`, `— Live` (hyphen / en-dash / em-dash), plus
`Live at/from/@ ...` (no delimiter needed), a **trailing `Live` right after a
closing quote**, and `... (World) Tour ... Live` titles ending in the marker (e.g.
XG `NEW DANCE - XG 1st WORLD TOUR "The first HOWL" Live`). The ambiguous
prepositions `in`/`on` are matched **only after a paren/bracket/dash delimiter**
(`(Live in Paris)`, `- Live in Tokyo`), so bare studio titles like "Live in the
Moment" / "Live in Color" / "We Live On" are kept. Also does NOT match titles
where "Live" is part of the song name ("Live and Let Die", "Stayin Alive", "Long
Live", "Live Forever").

## Instrumental version filter (`_is_instrumental_version`)
Tracks marked as instrumental or karaoke versions are excluded from all sections.
Matches: `(Instrumental)`, `[Instrumental]`, `- Instrumental`, `– Instrumental`,
`— Instrumental` (hyphen / en-dash / em-dash), the **`Inst.` abbreviation**
(`- Inst.`, `– Inst.`, `(Inst.)`, `[Inst.]` — e.g. izna "Psycho - Inst."),
`Instrumental Version` (Spotify-owned instrumental catalogue), `Karaoke Version`.
Case-insensitive; uses the same parens/bracket/dash anchor + word-boundary
pattern as the live filter so it does NOT trigger on titles where the word
"Instrumental" is part of prose or a band name (e.g. "The Instrumentals",
"Instrumentally Yours"). The `Inst.` branch requires the trailing period, so it
does NOT catch words like "Instant"/"Instinct"/"Install" (e.g. "Instant Crush",
"Some Song - Instant Classic" are kept). Does NOT match `Acoustic Version`,
`Extended Version`, `Remastered`, or tribute cover versions.

## Remix version filter (`_is_remix_version`)
Tracks marked as remix versions are excluded from all sections. Matches
`\bremix\b` (and `remixes`/`remixed`) inside a parens/bracket group (`(Remix)`,
`[Remix]`, `(Club Remix)`, `(Tiësto Remix)`, `(The Remixes)`), after a dash
separator (`- Remix`, `– Alesso Remix`, `— Remix`; hyphen / en-dash / em-dash), or
as a trailing word at the end of the title (`Blinding Lights Remix`).
Case-insensitive; `remix` is word-bounded so it does NOT fire mid-word (e.g.
"Remixing", "Premix" are kept) and does NOT catch `Remastered`. **Accepted
tradeoff** (mirrors the baby-sleep filter): a song titled exactly "Remix" would
also be caught — remix titles are reliably version markers, so this errs toward
excluding them. Reported example: izna "SIGN (Remix)".

## Unplayable / region-locked filter (`_is_unplayable`)
Tracks Spotify keeps in the catalogue but does not license in the user's market
are excluded from all sections. They show greyed out in-app and error with *"this
content is not available"* on play (reported: izna "Animal Fries", "PROUD
CITIZEN").

Unlike the live/instrumental/remix filters, this is a **data check, not a regex**
— `_is_unplayable(track)` reads the API's own fields: `is_playable: false`, or a
present `restrictions.reason` (any reason, not just `market`). It is
**fail-open** — a track dict carrying neither field is treated as playable, since
most tracks have neither and failing closed would empty the pool. `is_playable`
is only returned when the request passes `market=` (every composer fetch does),
so `restrictions` is checked too.

Applied at **two levels**: at each live-fetch ingestion point (discovery
candidates via `_passes_output_filters` in `_consider`; wildcard candidates in
`_pick_wildcards`), where the raw API object is in hand — `_slim_track` keeps
`is_playable`/`restrictions` so cached album tracks still carry them — and again
in `_Picker.try_add` as defense in depth.

**Coverage limit:** core and fallback tracks are rebuilt from `track_cache`,
which stores no playability column, and `/me/player/recently-played` returns
neither field — so those paths have nothing to filter on. Acceptable, because
those tracks come from the user's own play history and were playable when played.
A track that becomes region-locked *after* being played can still slip through;
the fix would be a playability column on `track_cache` plus a backfill job, not
per-refresh verification (batch `GET /v1/tracks` is 403 since Feb 2026, and 40
single-track calls per run would hit the ~50 req/min "Other" rate limit). See
`references/troubleshooting.md`.

## Album exclusion (output-only: ID-based + general name rule)

Whole albums / album categories can be excluded from the playlist via
`config.yaml` → `exclude`. Two mechanisms, **both output-only** — they are *not*
applied pre-scoring, so a liked artist's real plays still drive affinity and
discovery while the unwanted album's versions stay out of the playlist:

1. **`exclude.albums` (ID-based)** — pins an album by its **track IDs**. Track IDs
   are stable, making this exact; a re-release under a new album ID needs its
   own entry. Use for one specific album.
2. **`exclude.album_name_contains` (name-based, general)** — any track whose
   **album name contains** one of the substrings (case-insensitive) is dropped.
   Use for a whole *category* of albums that share a naming pattern (e.g. every
   `(Soundtrack from the Netflix Series)`). No ID lookup needed; new albums in
   the category are caught automatically.

**Pipeline data flow** (added 2026-08-28, v2.16.0): album names ride through the
pipeline. `track_cache` gained an `album_name` column (schema migration in
`db.init_db()` → `_migrate`, auto-applied by the cron wrappers). The poller
caches `album_name` from `/me/player/recently-played` (self-healing: rows cached
before the column existed are re-upserted on their next play), and the composer
stamps `album_name` onto every track dict it builds — `_fetch_track_metadata`
(core/fallback, from `track_cache`), `_fetch_artist_top_tracks` (discovery source
a, kept in `artist_cache.top_tracks`), and `_candidate(track, album_name, ...)`
for discovery source b and wildcards. `_passes_output_filters` then passes it to
`exclusions.is_output_excluded_track(name, artist_ids, track_id, album_name)`.

Implemented as `exclusions.is_album_name_excluded()` (substring match, lowercased
both sides), unioned with the other output exclusions by
`is_output_excluded_track()`. `scorer.compute_affinity()` deliberately keeps
calling the narrower `is_excluded_track()` so album-excluded tracks still earn
affinity.

**Merge history:** the per-ID `exclude.albums` entry for **Can This Love Be
Translated? (Soundtrack from the Netflix Series)** (Kim Min Seok (MeloMance),
WENDY; album `5Qg2iNuV6zTCGlIYunvvTd`, 2026-01-16, 31 tracks; added in 2.15.1)
was removed on 2026-08-28 — its name contains the substring, so
`album_name_contains` subsumes the 31-ID list exactly. Regression cases for the
name rule live in `scripts/test_track_name_filters.py`
(`ALBUM_NAME_MUST_BLOCK` / `ALBUM_NAME_MUST_KEEP`); the XDM entry below remains
ID-based (no shared naming pattern).

Currently excluded:
- **XDM Unidentified Waves** (XG × JAKOPS, album `1z3VCBUJBHkpDHtzOZP205`,
  2025-01-31) — 8 XDM alternate versions of XG tracks. XG itself remains a liked
  artist and its regular catalogue is unaffected. (ID-based entry.)
- **Any album containing `(Soundtrack from the Netflix Series)`** — general rule;
  currently covers Can This Love Be Translated? plus future Netflix-series
  soundtrack albums from liked artists.

To add an ID-based album: `GET /v1/albums/{album_id}/tracks?market={market}&limit=50`,
paste the IDs under a new `exclude.albums[]` entry, add cases to the
`ALBUM_MUST_BLOCK` list in `scripts/test_track_name_filters.py`, and re-run that
script. To add a name-based category: append the substring to
`exclude.album_name_contains` (and a case to `ALBUM_NAME_MUST_BLOCK`).

## Adding a new track-name filter

Filters like `_is_live_version` / `_is_instrumental_version` / `_is_remix_version`
are anchored to track names. To add one (e.g. for "Acoustic Version"):

1. Add the new cases to `MUST_BLOCK` / `MUST_KEEP` in
   `scripts/test_track_name_filters.py` FIRST (and teach the script to load the
   new helper from `composer.py`).
2. Add the regex + helper next to `_is_remix_version` in `composer.py`, mirroring
   the live filter's structure (parens/bracket/dash anchor + word-boundary +
   IGNORECASE).
3. Thread the `_is_<thing>(name)` check into **`_passes_output_filters`** — the
   single predicate used by `_Picker.try_add` (all sections + fallback) and by
   discovery ingestion. There are no other call sites to update. (Before
   v2.17.0 the check had to be added at three sites; those functions no longer
   exist.)
4. Run `python3 scripts/test_track_name_filters.py`; then a dry-run compose
   (writes nothing) to confirm 40/40 still composes.
5. Document it here and in CHANGELOG.md.

> **Output filter vs. category exclusion.** The live/instrumental/remix filters
> shape the playlist *output only*. If a category should also be kept from
> **scoring** (so it can't earn affinity or drive discovery — e.g. baby-sleep
> music played on repeat), add it to `src/exclusions.py` instead, which is
> applied by both `scorer.compute_affinity()` and the composer. `exclusions.py`
> also supports an **artist-id** exclusion list (`config.yaml` →
> `exclude.artist_ids`) — prefer that when the category maps cleanly to specific
> artists/labels, since it's precise and immune to name variation. For a single
> unwanted **album by an otherwise-liked artist**, use `exclude.albums`
> (output-only, ID-based); for a **category of albums sharing a naming pattern**,
> use `exclude.album_name_contains` (output-only, name-based) so the artist's real
> listening still counts toward affinity. The test harness loads the baby-sleep
> name filter (`is_baby_sleep_name`) from `exclusions.py` too, and exercises the
> album lists via `ALBUM_MUST_BLOCK` / `ALBUM_NAME_MUST_BLOCK` /
> `is_output_excluded_track`.

## Pitfalls (learned July 2026)

**Regex character-class trap**: `\([` matches the literal two-character sequence
`([`, NOT "either `(` or `[`". The correct character class is `[\(\[]`
(square-bracketed set containing both escaped chars). The live filter uses
`[\(\[]` correctly; a copy-paste typo wrote `\(\[` in the initial instrumental
filter and silently matched nothing for parens/bracket variants. **Always run
`scripts/test_track_name_filters.py` after any regex edit** — `py_compile`
accepts broken regexes because `re.compile` errors at runtime, not parse time.

**`patch` tool duplicate/corruption trap**: When patching a function where the
`new_string` contains the `old_string` as a substring, the patch tool can match
the inner anchor and produce a file with the function duplicated and the original
body truncated. Recovery — restore from
`~/.hermes/backups/hermes_backup_YYYYMMDD_HHMMSS.zip`:

```bash
cd /tmp && mkdir -p restore && cd restore
unzip -oq ~/.hermes/backups/<latest>.zip \
    'skills/media/spotify-playlist-refresh/spotify-refresh/src/composer.py'
cp skills/media/spotify-playlist-refresh/spotify-refresh/src/composer.py \
   /home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src/composer.py
python3 -m py_compile /home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src/composer.py
```

Then apply edits as separate patches, each containing the FULL function body (not
just the changed line) so the anchor is unique. (The v2.17.0 deploy also took
pre-deploy backups named `*.bak-tuneup-20260923`.)
