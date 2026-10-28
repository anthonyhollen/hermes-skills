# Changelog

## 1.6.0 — 2026-06-08

Full tune-up (hermes-skill-tuneup) — bug hunt + delivery hardening.

### Fixed (HIGH)
- **Hardcoded Google API key** removed from `traffic_cameras_map.py` and `commute_camera_map.py`. Replaced with `_load_env_key()` that reads from `GOOGLE_API_KEY` env var or `~/.hermes/.env` file as fallback.
- **Marker label mismatch** in `traffic_cameras_map.py`: `print_table` used raw `i` (numeric) while `build_map_url` used A-Z for indices 10-35. Added shared `marker_label()` function, used by both. Table and map now agree for all label ranges.
- **Uncaught `TimeoutExpired` / `JSONDecodeError`** in `traffic_camera_report.py` subprocess pipeline. Added try/except around `subprocess.run` and `json.loads`.
- **Import crash after API costs**: `traffic_camera_report.py` now validates Pillow/tabulate at startup (with `/usr/lib/python3/dist-packages` fallback) before incurring API calls.

### Fixed (MEDIUM)
- **`fetch_cameras()` uncaught network errors**: added try/except (`URLError`/`HTTPError`/`OSError`) in both `traffic_images.py` and `commute_camera_map.py` `get_route()`.
- **Bare `items[0][timestamp]` access** hardened in `traffic_images.py` and `traffic_cameras_map.py` — uses `.get()` chaining.
- **File descriptor leak** in `traffic_camera_report.py` `load_token()` — switched to `with open()`.
- **Dead code** removed: unreachable `return "unknown"` in `assign_region()`, spurious `or "/tmp"` fallback.
- **`"NonxNone"` formatting** in `traffic_images.py --summary` when width/height is null — shows `"?x?"` now.
- **Empty routes guard** added to `commute_camera_map.py` `get_route()`.
- **Duplicated `build_map_url` label logic** in `commute_camera_map.py` now calls shared `marker_label()`.

### Delivery
- Added `allowed_mentions` to `discord_post` curl payload (blocks @everyone/@here pings).
- Delivery-pattern.md reference synced with actual code.

### Token efficiency
- SKILL.md Common Pitfalls consolidated: removed stale "API spec vs reality" note, corrected URL limit (8192, not 16k), added env-key pitfall.
- Duplicate "### Deliver format (two messages)" header deduped.

### Validated
- Phase 0: auditor + green baseline (89→90 cameras live).
- Phase 1: 3-reviewer fan-out (control-flow, data-shape, adversarial refuter).
- Terminal gate: 2-reviewer regression gate → 2 regressions found + fixed → clean re-pass.
- All 4 scripts compile clean under gateway venv. Live dry-run: Home→Innovis (18 cameras, 3044-char URL, 305 KB map).

## 1.4.0 — 2026-06-08

Workflow 3 reworked into a **self-delivering on-demand script** so the LLM agent is no longer in the delivery path — fixes responses landing in #alerts.

### Root cause
Interactive replies went to **#alerts** because the agent delivered images with `send_message` and **no explicit target**, which falls back to the Discord **home channel** (`DISCORD_HOME_CHANNEL=1499972245437550602` = #alerts). An `agent.environment_hint` instruction did not reliably stop it (soft, LLM-level). Crons are unaffected — their prompts pass the target explicitly.

### Added
- **`~/.hermes/scripts/traffic_camera_report.py`** — on-demand, self-delivering route report. Wraps the `commute_camera_map.py` pipeline, renders the `# / CamID / Dist / Address` table (tabulate + Pillow), and posts the map + table to Discord **itself** via direct REST multipart `curl`. The agent runs ONE command and does NO delivery, so it can't misfire.
- **Destination = the current thread, resolved by the script** from the gateway session key `HERMES_SESSION_KEY` (`agent:main:discord:<chat_type>:<chat_id>[:…]`, set per-turn in `os.environ`). It posts to `chat_id` (parts[4]) — for a Discord thread that IS the thread's own channel id. **Never falls back to a fixed/home channel**: if it can't resolve the conversation (no session key, no `--to`), it exits non-zero rather than guessing. `--to discord:<id>` overrides for testing.

### Changed
- **SKILL.md Workflow 3** now leads with `traffic_camera_report.py` (run one command; do NOT `send_message` or post images yourself — that's what routed to #alerts). `commute_camera_map.py` is retained and documented as the underlying raw-data/no-delivery pipeline.

### Validated
- Unit: destination resolver returns the channel for thread/`group`/`dm` keys (the non-threaded `group` key carries a trailing **user_id** — using `parts[4]` avoids posting to the user id) and returns None (fail-closed) for non-discord/empty keys — never #alerts.
- Independent adversarial gate over the new script (caught the original `parts[5]` group-key bug, now fixed). Live end-to-end delivery to #playground (Home→670238 19 cams; Home→Innovis 18 cams). `commute_camera_map.py` + Workflow 1 unaffected.

### Reverted
- `agent.environment_hint` (the earlier soft-instruction attempt) reset to empty.

## 1.3.0 — 2026-06-08

Feature: Workflow 3 now emits a per-camera **`# / CamID / Dist / Address`** table (reverse-geocoded street address), delivered via the `discord-table-format` skill.

### Added
- **Reverse-geocoded address per along-route camera** (`commute_camera_map.py`). Reuses `geocode_cameras` from `traffic_cameras_map.py` (cached, atomic writes), adds `address` to the JSON `cameras[]` and prints a `# / CamID / Dist / Address` table. New `--no-geocode` flag skips it (faster, no Google geocoding calls). The map path is unchanged — `build_map_url` input is untouched, so the PNG is byte-identical (verified: geocoded vs `--no-geocode` produce the same map URL/PNG).
- **`marker_label(i)` helper** (`commute_camera_map.py`) — single source for the 1‑9 / A‑Z(10‑35) / unlabeled(>35) label, now used by the table **and** the JSON. Also fixes the pre-existing >35 label overflow in the printed list/JSON (they previously emitted invalid chars like `[`), matching `build_map_url`'s cap.
- **SKILL.md Workflow 3:** instructions to build the `#`/`CamID`/`Dist`/`Address` table and render it with `discord-table-format` (`format_table_for_discord`), sending map + table as two multipart `curl` messages.

### Fixed
- **Plus Codes instead of street addresses** (`traffic_cameras_map.py` `reverse_geocode`, shared by Workflow 1 too). Google returns a `plus_code` as `results[0]` for some expressway points; the geocoder now prefers the first result whose `types` is not `plus_code`, falling back to `results[0]`. E.g. `9X66+6H → Loyang Ave`, `8V9G+R2 → KPE`, `9Q8H+WQ → Bukit Timah Expy`. (Route points legitimately have no postal code → `postal` is `None`; both tables tolerate that.)

### Validated
- Independent adversarial gate over the cumulative diff: CLEAN (map unaffected, label↔map agreement exact, `--no-geocode` safe, no circular import, Workflow 1 region tables still render). Live: Home→670238 (19 cams, all street/route names) and `--region east` (29 cams).

## 1.2.2 — 2026-06-08

Fixes a live failure: "show camera route between Home and 670238" delivered **text only, no map**, to Discord.

### Fixed
- **Workflow 3 had no Discord-delivery instructions → agent used `MEDIA:` and the image was dropped.** 1.2.0 fixed Workflow 2's `MEDIA:` anti-pattern but left Workflow 3 silent on delivery, so the live agent fell back to `MEDIA:/tmp/route_<dest>.png` (an unsubstituted `morning_commute.py` template path) + `MEDIA:/tmp/route.png`; the gateway logged `Skipping unsafe MEDIA directive path` for both and sent text only. Added an explicit "Deliver to Discord" step to Workflow 3 pointing at `references/delivery-pattern.md` (direct REST multipart, never `MEDIA:`/`hermes send`) — same as Workflow 2. Root cause was a documentation gap, not the scripts: `commute_camera_map.py` reproduces a valid 640×500 PNG for Home→670238 (19 cameras).

### Changed (reconciling an automated skill-curator edit)
- **Route start/end marker labels `H`/`W` → `S`/`E`** in `commute_camera_map.py` (start=green **S**, end=blue **E**) — changed by the post-session curator at 10:37 without a changelog/version bump; SKILL.md's "H…W" description is now synced to `S`/`E`.
- **Carpark-code note corrected.** The curator's added note claimed `commute_camera_map.py` "expects an address, not a carpark code"; Google actually resolves a bare code like `670238` directly (verified). Reworded: bare code works, resolving to a street address (via `sg-commute/nearest_carpark.py`) just gives a more precise endpoint.

### Known minor (not changed here)
- Start/end labels `S`/`E` share the A-Z space with camera labels, so a route with ≥14 cameras gives camera #14 the label `E` (duplicate of the End marker). Pre-existing; the `S`/`E` rename makes the collision a bit more likely than `H`/`W` (camera #17). Cosmetic; flag if it matters.

## 1.2.1 — 2026-06-08

Follow-up tune-up: post-review remediation of residuals left by the 1.2.0 pass (caught when 1.2.0 was audited against the `hermes-skill-tuneup` method). Code edits verified byte-identical to 1.2.0 on the happy path (synthetic + live: east=29, commute=18 cameras), then re-reviewed by an independent adversarial gate (clean).

### Fixed (docs — the 1.2.0 doc-sync missed these)
- **Workflow 2 was not executable.** SKILL.md told the agent to "download image + map" via `traffic_images.py` (which only downloads JPEGs) and to use a "single-camera mode" of `traffic_cameras_map.py` that does not exist. Rewrote Workflow 2 to download the image via `traffic_images.py` and build the single-camera Static Map URL directly from the "Single marker" snippet in `references/google-static-maps-numbered-markers.md`.
- **Stale function name.** `references/region-boundaries.md` referenced `assign_center_cameras()`; the real function is `assign_region()` (center-gap branch). This was a relocated-doc-goes-stale miss — the reference was written before the code froze.
- **Wrong worked example.** `region-boundaries.md` claimed Thomson Flyover (1.329, 103.841) is assigned to South; it actually resolves to **North** (both centers are ≈0.1090 away — equidistant — and the strict-`<` north-first scan keeps the earlier region). Verified on the Pi.
- **Label-cap off-by-one.** `references/google-static-maps-numbered-markers.md` said `Z`=36 / max 36 markers; `Z` is index 35, and index ≥36 produced invalid labels (`[`, `\`).

### Fixed (code hardening — same bug-classes the 1.2.0 pass named but applied inconsistently)
- **Truthy lat/lon guards at format sites.** `traffic_images.py` summary and `traffic_cameras_map.py` `print_table` used `if c["latitude"]` (a coord of `0.0` would print `"?"`) — changed to `is not None`, matching the `is not None` style used in the filter functions.
- **Invalid marker labels for >35 cameras.** `build_map_url` in both `traffic_cameras_map.py` and `commute_camera_map.py` emitted `chr(ord("A")+i-10)` past `Z`. Now caps at `Z` (index 35) and places index >35 unlabeled. Defensive (regions top out ≈29). Also skips cameras with null coordinates (defensive; nulls never reach here in normal flow).
- **Orphaned cache `.tmp`.** `geocode_cameras` write now wraps `.tmp`-write/`os.replace` in try/except and removes the `.tmp` on failure (the live cache stays intact).
- **Unguarded Directions coords.** `commute_camera_map.py` dereferenced `start_loc["lat"]`/`end_loc["lng"]` (introduced by the 1.2.0 double-geocode fix) with no guard; now validates the Directions response has both before use.

## 1.2.0 — 2026-06-08

### Fixed
- **Null longitude crash in all 3 scripts.** `filter_bbox`, `filter_radius`, `geocode_cameras`, and `find_cameras_along_route` checked `latitude is not None` but not `longitude` — a camera with null longitude would crash on format/comparison. Added `and c["longitude"] is not None` to all guards.
- **Corrupt geocode cache crash.** `json.load()` on `geocode_cache.json` had no try/except — a mid-write crash left a truncated file that bricked every subsequent run. Added try/except fallback to `{}` on decode error.
- **Non-atomic cache write.** `json.dump()` wrote directly to the live file; a crash mid-write corrupted it permanently. Now writes to `.tmp` + `os.replace()` (atomic on POSIX).
- **Double geocoding in commute_camera_map.py.** Re-geocoded start/end addresses via Geocoding API (2 extra HTTP calls) even though the Directions API response already contains `start_location`/`end_location` with lat/lon. Now extracts coordinates from the route response directly.
- **Unhandled HTTP error on image download.** Both `traffic_cameras_map.py` and `commute_camera_map.py` had bare `urlopen()` with no try/except — an HTTP error crashed the entire script. Added try/except + PNG magic byte verification.
- **MEDIA: tag delivery anti-pattern in SKILL.md.** Workflow 2 used `MEDIA:` tags which lose text↔image pairing via `extract_media()`. Replaced with pointer to direct REST multipart pattern.

### Removed
- **Dead code: 53 lines.** `make_numbered_marker()`, `get_numbered_marker()`, `get_google_marker_url()`, and `_marker_cache` in `traffic_cameras_map.py` — leftover from failed KML/data-URI marker experiments, never called by `build_map_url()`. Also removed now-unused `geocode_address()` from `commute_camera_map.py`.

### Changed
- **SKILL.md slimmed ~55%** (2305 → ~1050 tokens). Moved inline code blocks to references, removed duplicate "Numbered Map Markers" sections, replaced Workflow 1 detailed steps with single script invocation, added `references/delivery-pattern.md`.

## 1.1.0 — prior release
