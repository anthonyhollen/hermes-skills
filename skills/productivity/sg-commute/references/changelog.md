# sg-commute — Changelog & Lessons Learned

Full dated history of bugs, fixes, and design changes for the sg-commute skill. Moved out of `SKILL.md` (v1.9.2) to keep the always-loaded skill lean — this file is read **on demand**, e.g. when debugging a recurring issue, understanding *why* something is the way it is, or before changing route / waypoint / delivery / EV-scan logic. The distilled still-live gotchas live in `SKILL.md` → "Changelog & Lessons".

---

#### 2026-10-14 — Wednesday commute → Innovis; AMK kept on file (v1.11.0 → v1.12.0)

**User request:** plan the Wednesday morning commute for Innovis instead of AMK, but keep the AMK format so it can be restored to any day later. `morning_commute.py`: the weekday-keyed `DESTS` tuple map is split into `DEST_PROFILES` (slug → label, address, slug, rain station, station name, distance; `innovis` + `amk`) and `SCHEDULE` (weekday → slug; Tue/Wed/Thu all `"innovis"`). The AMK profile is unchanged (S109 Ang Mo Kio Ave 5, direct route, ≥8 km floor), and the `amk` argv/`DEST` override still renders it. **To restore AMK to a day:** set that weekday to `"amk"` in `SCHEDULE`; a day outside Tue–Thu also needs `hermes cron edit b617d89da15b --schedule …` (cron is `25 7 * * 2,3,4`). The cron job and the evening report are unchanged (the evening report doesn't depend on the destination).

---

#### 2026-09-29 — hermes-skill-tuneup pass #3 (v1.10.0 → v1.11.0)

Full tune-up (Phase 0 → blind fan-out → adversarial refuters → re-entrant terminal gate → docs last). Live copies: `~/.hermes/scripts/morning_commute.py` + `evening_weather.py` (no_agent crons, venv 3.11), `scripts/nearest_carpark.py` (interactive, `/usr/bin/python3`; monthly `--refresh` in the system crontab). Baseline: 40/40 archived morning runs and 40/40 evening runs green, every entry point re-run on the Pi. Reviewers (offline kit, no SSH): control-flow/state (first attempt killed by the daily budget cap, re-run after the reset), data-shape, scheduled-output/doc-drift, usage-forensics (the agent's 12 real parking requests since 07-14), two adversarial refuters, then a re-entrant terminal gate: lap 1 (regression hunter + adversarial refuter), laps 2–3 (one reviewer each), lap 4 self-probed by re-running lap 3's harnesses — clean. Laps 1–3 each found a real hole in the previous lap's fix (the SG-restricted geocoder answering garbage with "Singapore" the country; a negative `retry_after` crashing mid-delivery; a sustained 429 retrying past the agent's 60 s terminal timeout; the send budget measured from send start instead of script start).

**Cron reports**
- **HIGH — evening "commute (6–8am)" line was the whole-day forecast.** It printed `general.forecast`, whose `validPeriod` is 6 PM today → 6 PM tomorrow (dominated by afternoon storms): "Thundery Showers" on 28/40 evenings while the next 6am–Midday period was thundery 9/40. Now `• 6am–Midday (East): <period>` from the period starting at the first 06:00 after now, then `• 24h outlook: …` (user-approved format).
- **MED — rain labels on a 5-minute total.** The rainfall value is a 5-minute accumulation; the hourly-style bands under-reported ~12× (07-23: "1.2mm - light rain" ≈ 14 mm/h). Now `X.Xmm in last 5 min (~Y mm/h) - light/moderate/heavy rain` (<2.5 / <7.6 / ≥7.6 mm/h; user-approved), only when `readingType` says "5 Minute".
- **MED — flood line.** Counted 2-minute history snapshots ("25 active station readings" for one alert, cleared alerts "in effect") and read a 429/empty body as an all-clear. Now newest snapshot = in effect; older-only = "reported in the last ~45 min (may have cleared)"; bad/empty payload = "Flood alert status unavailable".
- LOW: a null PM2.5 region blanked the section; legend now lists the 50–99/100+ bands already in `pm_level`; section error text no longer interpolates raw exceptions (a Static Maps `TimeoutExpired` repr carried the key-bearing URL — `_safe_err`); map fetch has its own guard and needs curl exit 0 + PNG signature + IEND trailer (a truncated PNG used to ship); Directions retries only transient statuses and shows the real status; the data.gov.sg `x-api-key` is no longer sent to Google; `hhmm` converts to SGT; stale 2-hour bulletin flagged; Fair/Sunny emoji follow Day/Night; Static Maps `700x450` → `640x450` (Google clamped it anyway); AMK station label 1.6km → 1.3km. Refuted and reverted: classifying traffic colour on rounded minutes (seconds are more accurate).

**Parking tool (`nearest_carpark.py`)**
- **HIGH (usage) — the docs taught the agent to hand-post.** SKILL.md showed a curl recipe (read `DISCORD_BOT_TOKEN` from `.env`, `-F content=`) above `--send`; on 07-16 the agent printed the bot token into a tool result (now in `state.db`) and posted literal `\n`s. Recipe + the "use `_discord_send_pair()`" footer removed; SKILL.md now leads with the single `--send` command.
- **HIGH (usage) — `--send` target + exit contract.** Agents passed the triggering MESSAGE id as `--thread` (a follow-up's id → 404 ×4, `Sent 0/4`, exit 0, `[SILENT]`). `--send` with no id now posts into the current conversation from `HERMES_SESSION_KEY` parts[4] (the gateway bridges the per-turn session vars into the terminal subprocess — verified in `tools/environments/local.py`; cross-checked against `HERMES_SESSION_CHAT_ID`/`THREAD_ID`; fail closed). Exit 1 only when nothing was posted ("nothing was CONFIRMED posted — check before re-running" when a POST timed out); partial → "PARTIAL DELIVERY … do NOT re-run"; per-message `nonce` + `enforce_nonce` make retries duplicate-safe; `retry_after` honoured (clamped); 401/403/404/exhausted-429 stop the loop; the whole run ends within ~50 s of script start (curl clamped to the time left) so the agent's 60 s terminal timeout can't kill it mid-send.
- **HIGH (usage) — the agent couldn't see the results** (tables are images), so 5/10 replies misquoted counts ("1,983 HDB lots" = feed size). New agent-only `AGENT DIGEST` (never posted) + `rows` in `--json`; progress lines relabelled.
- **MED — outage read as fact.** A failed/429 HDB or LTA feed produced "No parking within 1km radius"; a failed EV discovery scan was cached as "covered, no chargers" for up to a month. Now "⚠️ … live data unavailable — list may be incomplete" and the area is not anchored.
- **MED — HDB `carpark_info[0]`** skipped 15 carparks whose C entry isn't first (Sengkang SK4x…). **LTA feed paged** (500-row cap; 2,608 rows); HDB-agency rows deferred to the HDB feed (they had crowded malls out and shipped HDB carparks labelled LTA).
- **MED — EV cache.** Atomic writes + `flock` read-merge-write (`_commit_ev`) so the monthly `--refresh` can't erase a concurrent query's finds (reproduced by the control-flow reviewer); `--refresh` timestamps its log and exits 1 on unreliable scans; unknown bay statuses (`""`, seen live) → `?/N`.
- LOW: renderer failure posted bare headers → `*(table image unavailable)*` + text rows; `--help`/flags no longer geocoded as addresses; Google geocode restricted to SG with country-level results rejected (the gate caught the SG restriction turning garbage input into "Singapore" the country); stale HDB sensors (>48 h) shown as Live N; per-run map file + age/count sweep (a second query deleted the first one's images); the key-bearing map URL no longer written to `/tmp/carpark_map_url.txt`; `--json` stdout is pure JSON.
- Not changed (user's call, 2026-09-29): static URA rows still rank by distance and show capacity under "Avail" (AMK shows 3 URA rows; the digest labels them capacity). Report-only: EV coordinate drift can duplicate a charger; `sys.path` pins the 3.11 user site.

**Docs:** SKILL.md 24 KB → ~7 KB (parking-first; description now says "parking / car park" — agents had loaded `maps`/searched files first in 7/7 sessions); CLAUDE.md (auto-attached to every parking run) → a short pointer, long form relocated to `references/developer-guide.md` with corrections; dated corrections in weather-api-response-shapes (5-minute rainfall, flood-alerts shape, 47 areas/89 stations, evening period order), traffic-duration-troubleshooting (a Unix `departure_time` works — the old "INVALID_REQUEST" note was wrong), google-routes, static-map, geocoding, LTA paging, JTC path; agent-era docs bannered historical. Old SKILL.md body and the removed delivery sections are archived below (*Relocated 2026-09-29*).

**Config:** both no_agent jobs still carried stale prompts (the old single `via:1.292,103.885` U-turn waypoint, thresholds 1.15/1.40) and `skills: [sg-commute]` — would reload the skill if `no_agent` were ever flipped off. Cleared with `hermes cron edit … --clear-skills --prompt "<pointer>"`.

**Secrets (user action):** the Discord bot token (07-16 session) and a MiniMax API key, MiniMax OAuth token and the Xiaomi key (07-30 session) were printed into tool results and persist in `~/.hermes/state.db` — rotate them.

---

#### 2026-07-14 — 1 km display radius cap for parking + EV (v1.9.9 → v1.10.0)

**User report:** a parking/EV query for postal **569088** (12 Ang Mo Kio St 64, UE BizHub Central) surfaced an EV "Spot C" ~4.4 km away in Hougang. **Root cause:** the five `nearest_*` helpers (`nearest_hdb/lta/ura/jtc/ev`) sort candidates by distance and take the top N with **no distance ceiling**, so the closest *available* option fills a slot even when it is kilometres away. For EV this was amplified by `get_ev_nearby` serving cached candidates within `CACHE_QUERY_RADIUS = 5000 m`, then `nearest_ev(n=20) → merge_ev` picking the top 3 of those.

**Fix:** added a module-level `MAX_RADIUS_M = 1000` (1 km) constant; every `nearest_*` helper now filters candidates to `d <= max_dist` **before** ranking (new keyword arg `max_dist=MAX_RADIUS_M`). SVY21-euclidean (HDB/JTC) and haversine (LTA/URA/EV) distances are both in metres, so one cap applies uniformly. `CACHE_QUERY_RADIUS` / `ANCHOR_COVER_RADIUS` are unchanged — they govern EV cache *retrieval/reuse* breadth, deliberately wider than the display cap (the 1 km filter is the authoritative display limit downstream). A source with nothing within 1 km now yields no rows for its section rather than a far-away filler; the renderer already omits empty tables.

**Verified live:** `569088` — EV dropped the 4428 m Hougang charger, keeping only the 229 m + 461 m chargers; parking kept 3 lots at 409/437/690 m; all surviving map markers re-checked ≤ 1000 m. Dense-area regression `138634` (Fusionopolis) still returns the full 3 parking + 3 EV (all < 1 km), confirming the cap does not over-filter.

**Follow-up (same day) — short/empty-section note.** Added `_radius_note()`: when a section has fewer than 3 results within the 1 km cap it shows a note under the header (`*No other parking/EV charging within 1km radius.*`; `*No … within 1km radius.*` when zero). Because each source caps its own candidates at 3 within the radius, a combined total <3 genuinely means that few exist within 1 km — the note tells the user the short (or empty) table is the whole answer within range, not a fetch glitch. The 0-result section is now emitted *with* the note rather than dropped silently. Verified: `569088` EV shows `*No other EV charging within 1km radius.*` (2 chargers) while its parking (3 lots) shows none; `138634` (3 + 3) shows neither.

---

#### 2026-07-12 — hermes-skill-tuneup pass (v1.9.8 → v1.9.9)

Full tune-up (Phase 0 → bug-hunt fan-out → terminal gate → docs-last). Live copies: `~/.hermes/scripts/morning_commute.py`, `~/.hermes/scripts/evening_weather.py` (no_agent crons), `scripts/nearest_carpark.py` (interactive). Findings from independent blind reviewers (control-flow / data-shape / carpark-deep lenses) + an adversarial refuter, each reproduced on live data. Baselines: both crons render clean; carpark `--json 138634` = 3-section manifest.

**HIGH — 24hr forecast period labels shifted by one, every morning (CONFIRMED, morning_commute.py `section1`).** The report hardcoded `["Midday–6pm","6pm–6am (tonight)","6am–Midday tomorrow"]` onto `periods[0..2]`. But the NEA 24hr periods are anchored to the 06:00/12:00/18:00 boundaries and `period[0]` is the *current* block, so at the 07:25 cron time they are `[06:00–12:00, 12:00–18:00, 18:00–06:00]` — the hardcoded list mislabelled all three. **Proof:** three real 07:25 `no_agent` outputs (Jul 1/8/9) printed `6am–Midday tomorrow: Partly Cloudy (Night)` — a *night* code cannot describe a daytime block, so `periods[2]` at 07:25 is the 18:00–06:00 night period. The labels only line up in the *evening* (which is where the mistaken `weather-api-response-shapes.md` "Correction 2026-06-12" was eyeballed — it never checked the Day/Night codes; "documented ≠ verified"). **Fix:** new `_period_label(tp)` derives each line's label from that period's OWN `timePeriod.text` (strips the date, tightens "6 pm"→"6pm", " to "→"–", adds "(tonight)" when start/end cross midnight), so labels match the datum at any run time. Validated by isolation test on synthetic morning+evening period orderings. Reverted the wrong doc "correction" (dated 2026-07-12 note).
  - **Gate-caught regression in my own fix:** `_period_label` was called *outside* section1's inner try/except, so a non-string `timePeriod.text` (realistically a future `{code,text}` migration — `regions.<region>` + `general.forecast` already migrated that way) would crash the *whole* section (losing the temp/humidity/wind header too). Hardened to tolerate a dict/None/non-string `text` (returns None → line skipped) + whitespace-collapse + longer month names. Re-tested against those exact adversarial inputs. Both terminal-gate reviewers flagged this independently.

**MED — section2 mislabels a neighbour's nowcast as "Pasir Ris" (morning_commute.py).** When Pasir Ris is absent from the 2hr bulletin, the `fcs[0]` fallback printed it under the "Pasir Ris:" label with no disclosure; the evening sibling already appended `*(Nearest area: …)*`. Ported the disclosure (sibling-divergence class). Low trigger (Pasir Ris is normally in the 48-area bulletin), zero change when present.

**LOW fixes:**
- **PM2.5 regional avg band mismatch (morning_commute.py `section4`):** avg was displayed rounded (`%.0f`) but classified on the unrounded float (e.g. 9.4 → shows "9" but labels "Moderate", contradicting the printed "0–9 Good" scale). Now classifies `round(avg)` and displays the same int.
- **Rainfall `fail=True` defeated the 429 backoff (`section3`):** `curl -f` emitted an empty body on 429 → the rate-limit detector saw `None` → 1.5 s wait instead of 10 s. Removed `fail=True` (the `want=len≥60` predicate already guards) so the 429 body is seen and backed off; aligns with the other 4 calls.
- **Static-map curl had no `--max-time` (`section5`):** only bounded by the 60 s subprocess timeout; added `--max-time 20` (every other curl has one) to trim the worst-case tail.
- **EV `0/N` on a failed live refresh (nearest_carpark.py):** a cached charger keeps `available=0` until `refresh_ev_live` confirms it live; a refresh failure (LTA 500 / coord mismatch) left the 0, rendering "0/N" (reads as *full*). Now a per-entry `available_live` flag (init `False` in `merge_ev`, set `True` only on a confirmed live match) gates the cell: `avail/total` when confirmed, else `?/total`. A genuinely-full charger (confirmed live `available=0`) still shows `0/total`. (`available` stays numeric so `merge_ev`'s sum is unaffected.)

**Refuted / not changed (reported, below the fix bar):** EV availability is NOT always-0 (live `evIds[].status` is populated "1"/"0" — the committed `ev-charging-sample.json` with blank statuses is stale; the code reads the right level). EV "ACkW" fallback is dead code (`chargingSpeed` always present live). URA↔LTA dedup by `code.upper()` assumes a shared code space they don't share, but URA `pp_code`s are alpha-prefixed (BL021/U0014) vs LTA's short IDs → negligible real collision, can only drop (never corrupt), risky to change. section5's ~229 s worst-case under sustained Google timeouts can hit the 120 s SIGKILL, but this is `deliver: discord` so a SIGKILL delivers a "⚠️ Cron job failed" alert (not silent) — rare + self-alerting, left as-is beyond the map `--max-time`.

**Perf:** morning ~2.2 s warm (10.9 s cold tail), evening ~0.8 s — network-bound, already concurrent, far under 120 s. No steady-state lever (matches prior pass).

**Doc-sync:** corrected `weather-api-response-shapes.md` (dated 2026-07-12 note reverting the wrong period "correction"); updated `SKILL.md` + `CLAUDE.md` morning-report format + period-index mapping to describe the timePeriod-derived labels; fixed `SKILL.md`'s fallback-emoji default (☁️ → ⛅, matching `wx_emoji`); documented the EV `?/total` behaviour in `CLAUDE.md`; added a dated correction to `geocoding-troubleshooting.md` (postal → OneMap-first, street → Google — the doc's unconditional "Google first" was wrong).

---

Triggering the morning job manually on a **Friday** delivered nothing; the live agent called it a "no-op". Diagnosed: a manual trigger (`trigger_job`) sets `next_run_at=now` and the scheduler **does** run the script on its next tick (updates `last_run_at`, writes an output doc — `last_error=None`), so the older "`cronjob action='run'` is a pure no-op / doesn't update last_run_at" claim was stale. The real behaviour: `morning_commute.py` only reports Tue/Wed/Thu, so off-schedule with no override it returns silently → `Status: silent (empty output)`, nothing delivered (correct, not a failure). Corrected the stale note in SKILL.md + CLAUDE.md (incl. the pitfalls table) and added the off-schedule verification recipe (`morning_commute.py innovis|amk` / `DEST=`). No code change. (Unrelated to the v1.9.4 key/bug fixes; the v1.9.4 `jobs.json` key-scrub was confirmed not to affect cron execution.)

---

Full `hermes-skill-tuneup` pass (Phase 0 → bug-hunt fan-out → terminal gate → docs). Live copies: `~/.hermes/scripts/morning_commute.py`, `~/.hermes/scripts/evening_weather.py` (no_agent crons), `scripts/nearest_carpark.py` (interactive). Findings were produced by independent blind reviewers (control-flow / data-shape / security lenses) + an adversarial refuter, each reproduced on live data.

**Security (HIGH):**
- **Hardcoded API keys removed from scripts.** `morning_commute.py` hardcoded the Google Maps key; `nearest_carpark.py` hardcoded the Google key *and* the LTA DataMall AccountKey. Both now load via env helpers (`_env_val` / `_load_env_key`) that read `os.environ` then parse `~/.hermes/.env`. `GOOGLE_API_KEY` was already in `.env`; `LTA_DATAMALL_KEY` was added to `.env` (it had only ever lived in the script). The Google key literal was also scrubbed from CLAUDE.md + 4 references + the dead `b617` cron prompt in `jobs.json`; the LTA key from CLAUDE.md + 3 references. **⚠️ ROTATE both keys** at Google Cloud + LTA DataMall — they sat in plaintext docs/`.backups/` and must be considered compromised.
- **Discord injection hardening (`nearest_carpark.py --send`).** `_discord_send_pair` now sends `content` + `allowed_mentions:{"parse":[]}` as a `payload_json` multipart field (blocks @everyone/@here/@id pings server-side; verified the field is honoured for text-only messages via live read-only Discord probes). Added `_sanitize_discord()` (strips newlines, neutralizes backticks, defangs mentions, breaks masked/auto links) applied to the untrusted geocoded address in `format_section_content`. Reachable because geocode fallback #3 echoes the raw input as the "address".

**morning_commute.py robustness:**
- **Broken-PNG delivery fixed (MED).** A failed Static Maps fetch wrote an ~88-byte text error body that `curl -o` saved with exit 0; the size-only gate passed and the cron media filter (no magic-byte check) delivered it as a broken image. Now uses `curl -f` + validates the `\x89PNG` signature before emitting the `MEDIA:` tag — a bad fetch degrades to text-only.
- **`wx_emoji` heavy-rain glyph (LOW).** Added a `"heavy" + rain/shower → 🌧️` branch (the generic rain branch had been returning the light glyph for "Heavy Rain"), matching `evening_weather.emoji_for` and the SKILL.md rule.
- **`section1` string-forecast crash (LOW, latent).** `general.forecast` is normally a `{code,text}` dict but the API can return a bare string; `.get("text")` on a str raised AttributeError and took out the whole Weather section. Now handles both shapes.
- **`section2` literal "None" (LOW).** Empty `forecasts` list now renders `n/a` instead of `None`.
- **Top-level guard.** `__main__` now wraps `main()` in try/except emitting a minimal valid report (exit 0) instead of letting a traceback become a "script failed" alert — mirrors `evening_weather.py`.

**nearest_carpark.py:**
- Renderer call wrapped in try/except (a renderer failure/timeout now degrades to text-only sections instead of crashing before the map/address ship).
- Prior-run table PNGs in `/tmp/hermes_cache/` are reclaimed before each render (bounds the leak without deleting the current run's files the agent path still needs).

**Doc↔code sync:** corrected the stale traffic-colour thresholds (1.15/1.40 → the code's 1.10/1.35) in CLAUDE.md + commute-report.md; replaced CLAUDE.md's stale single-digit EV-scan algorithm with the live 2-digit-sector `_narrow_variants`; fixed the cron script paths (`~/.hermes/scripts/…`) and schedule (07:28); added a dated correction to weather-api-response-shapes.md's period table; de-duped the `jobs.json`-editing snippet in SKILL.md (→ pointer to CLAUDE.md).

**Not changed (refuted / accepted):** the `curl_json` retry budget can only exceed the 120s scheduler timeout under multi-minute network degradation (the empty-payload retry path is cheap and the DATAGOV key prevents the 429 path) — left as-is. The `merge_ev` None/0.0-coordinate collapse is dead code in the live flow (coords are filtered upstream; SG never has a 0.0 coordinate). Performance: both crons run ~1–2.6s (far under 120s) and the EV scan was already optimized in a prior pass — no perf lever.

---

Two user-visible bugs in the same session, both around Discord delivery paths:

**1. Section header format divergence between `--send` and terminal output (v1.9.0 → v1.9.1):**
- **Symptom:** Agent ran the script without `--send`, copied the terminal-output text
  verbatim into an ad-hoc curl loop, and posted `--- ⚡ EV CHARGING ---` to Discord.
  The user expected `**⚡ EV CHARGING**` (the format `--send` uses).
- **Root cause:** Terminal output printed `\n--- {header} ---\n` for human readability;
  `--send` posted `f"**{header}**"`. Two different text paths for the same field.
  The agent's ad-hoc loop grabbed the terminal decoration, not the canonical text.
- **Fix:** Added `format_section_content(header, body)` helper in `nearest_carpark.py`.
  Both `--send` and the terminal-output path call it. The terminal path now prints the
  canonical content prefixed by a clearly-marked debug delimiter
  (`--- section: {header} ---`) so an agent can never mistake decoration for content.
  See `references/discord-image-delivery.md` "Single source of truth for Discord text"
  for the class-level lesson.

**2. `--send` defaulted to parent channel even when invoked from inside a thread (v1.9.1 → v1.9.2):**
- **Symptom:** User asked for a carpark report from inside a Discord thread. Agent ran
  `nearest_carpark.py --send <parent_channel_id>`. Report landed in #alerts (parent),
  not the thread the user was reading. User noticed within one round-trip:
  "the report should be sent as a threaded reply and not to <#1499972245437550602>".
- **Root cause:** `--send` only accepted a channel ID; no concept of threads. The first
  carpark run this session had the same issue (`408868` went to #alerts instead of the
  thread) and was silently accepted as "the norm."
- **Fix:** Added `--thread <thread_id>` companion flag. Routes the POSTs to the thread's
  message endpoint (Discord's v10 API treats threads as channels; the URL is identical,
  only the ID changes). `--send` still names the parent channel for the delivery-summary
  print line; the summary now distinguishes "thread" vs "channel" so the user can see
  at a glance where the report landed. See `references/discord-image-delivery.md`
  "Threads vs. channels" for the foot-gun description and the API-uniformity fact.

**Verification:**
- `python3 .../nearest_carpark.py "670238" --send 1499972245437550602 --thread 1512615425844838480`
  → `Sent 4/4 messages to thread 1512615425844838480 (parent channel 1499972245437550602)`,
  parent channel untouched.
- Terminal output for the same query now shows `--- section: 🅿️ PARKING ---` then
  `**🅿️ PARKING**` on the next line — debug marker + canonical content, byte-identical
  to what `--send` posts.

**Lesson:** Two distinct failure modes, one shared root cause — **Discord delivery paths
that diverge from terminal output paths**. The terminal output is the agent's primary
read source; if it doesn't match what gets posted, the agent will post the wrong thing.
Centralise the format string. Also: **when the user is in a thread, default to posting
to the thread**, not the parent channel. Both lessons captured in
`references/discord-image-delivery.md` so they survive beyond this session.

**Symptoms:**
1. Blue polyline route invisible in Discord despite pixel analysis confirming it existed in the file
2. AMK route showing 60km polyline (via city) instead of correct 14km direct route
3. Innovis route truncated at ECP (16km) — never reached destination

**Root Causes:**

1. **Polyline invisible — PNG8 palette stripping:** Default Static Maps format is PNG8 (256-color palette). Discord re-encodes uploaded images and may strip or misrender thin blue lines on palette-indexed images. The polyline was in the file (confirmed: 5,626–11,074 blue pixels) but became invisible after Discord's processing. **Fix:** Add `format=png32` to force full RGB rendering. File size jumps from ~180KB (PNG8) to ~570KB+ (PNG32).

2. **AMK wrong route — waypoints used when they shouldn't be:** Adding `ECP+near+Marina+Bay+Singapore|1+Ayer+Rajah+Ave+Singapore` waypoints for AMK (Wednesday destination) forced Google to route via Marina Bay city, producing a 60km polyline instead of the correct 14km TPE→PIE direct route. The cron agent apparently used waypoints for AMK despite the skill having distance guards. **Fix:** AMK = direct route, no waypoints.

3. **Innovis truncated at ECP — address waypoints treated as stops:** `ECP+near+Marina+Bay+Singapore|1+Ayer+Rajah+Ave+Singapore` as waypoints caused Google to route to ECP only (Leg 1: 16km) and **stop** — Leg 2 was ECP→Ayera Rajah, Leg 3 was Ayer Rajah→Innovis, but the `overview_polyline` (used for the Static Map) only covered Leg 1. Address-based waypoints are treated as stopovers, not pass-through points. **Fix:** Use coordinate via: waypoint `via:1.292,103.885` (a point on the ECP westbound carriageway). This produces a clean TPE→PIE→ECP→MCE→AYE→Innovis polyline (35–38km) with no Marine Parade Road detour. Note: `East+Coast+Parkway+Singapore` text waypoint was used as an intermediate fix but geocodes near Marine Parade surface streets, causing a ~1.6km detour.

**Confirming fixes in Discord:**
- Always send test images to playground channel (`1501742338639401071`) first — do not rely on internal pixel analysis alone
- Visual confirmation in Discord is the only reliable test — PNG file size (>500KB) is a proxy indicator but not definitive
- When testing route variations, generate and compare ALL variants in the same session before sending to Discord

**Verified working configuration (as of 2026-05-20):**

| Destination | Waypoint | Expected distance | Polyline visible |
|-------------|----------|-------------------|-----------------|
| AMK (Wed) | none (direct) | 13–15 km | ✅ PNG32 + traffic colour |
| Innovis (Tue/Thu) | `via:1.292,103.885` | 34–38 km | ✅ PNG32 + traffic colour |

**Changes made in v1.6.0:**
- `format=png32` added to all Static Map URLs (SKILL.md + google-routes.md)
- Polyline color changed from `0x1E88E5` (light blue) to `0x1565C0` (darker, more saturated); further changed to dynamic traffic-based colour: 🟢 `0x00C853` light / 🟡 `0xFFAB00` moderate / 🔴 `0xD50000` heavy / 🔵 `0x1565C0` no data (2026-05-25)
- Polyline weight changed from `5` to `7`
- Waypoint logic corrected: Innovis uses `East+Coast+Parkway+Singapore`, AMK uses no waypoints; further corrected to `via:1.292,103.885` to eliminate Marine Parade Road detour (2026-05-25)
- Distance validation thresholds: Innovis >35km, AMK >8km

### 2026-05-21 — Cron Prompt Preamble Leak (v1.6.0 → v1.7.0)

**Symptoms:**
1. Agent reasoning text ("Now let me compile the full morning commute report:") appearing before the report header in Discord
2. The text persisted across multiple cron runs despite skill-level fixes to the `## Response` section

**Root Cause:**
The cron prompt (stored in `jobs.json`) had the correct `deliver: "discord"` and instruction to "just produce it and end the session", but lacked an explicit first-line output rule. The MiniMax-M2.7 model has a tendency to narrate its work before producing structured output. Even though the skill's `## Response` section had an output guard, it was loaded as background context and could be overridden by the model's own reasoning pattern.

**Why skill-level fixes didn't work:**
The `## Response` section in `commute-report.md` was patched to say "do not write preamble text", but the skill content is loaded as background context. The model's internal reasoning still generated "Now let me compile..." before the visible output.

**Fix applied (2026-05-21):**
1. Cron prompt rewritten to include a **strict first-line rule**: "Your very first line of output must be `## ☀️ Morning Commute Update` — start immediately with the header, nothing else"
2. Skill `## Response` section already has the guard — confirmed correct
3. JSON editing used Python `json.load()` → direct assignment (NOT the `patch` tool) because `patch` inserts raw `0x0a` bytes instead of escaped `\n` in multi-line JSON strings, silently corrupting `jobs.json`

**File-mutation verifier warning:**
A `patch` call on `commute-report.md` failed with "Could not find a match for old_string" — the tool was searching for "Now let me compile" in the skill file, but that phrase never existed there (it only appeared in cron run output logs). The patch was a no-op; the verifier correctly flagged it.

**jobs.json editing pitfall:**
`patch` is unsafe for editing multi-line JSON strings with newlines/quotes. Always use Python `json.load()` → edit → `json.dump()` for `jobs.json` modifications.

**Verification:**
Check `~/.hermes/cron/output/b617d89da15b/<timestamp>.md` — good output starts immediately with `## ☀️ Morning Commute Update`; bad output has preamble text 3-4 lines before the header.

**Can it run with agent off?**
Yes — as of 2026-05-31 it does. `scripts/morning_commute.py` replicates all branching logic in Python: day-of-week → destination, weather/PM2.5/rainfall formatting, traffic-aware fallback, and static-map colour. The cron job runs it as `no_agent: true` (no LLM). The script derives the day from the system clock (`datetime.now().weekday()`) and degrades each section independently if an upstream API fails.

### 2026-05-27 — EV Markers Hidden by Parking Markers on Static Map

**Symptom:** Google Static Map renders only one marker per coordinate pair. EV markers A and B were invisible — they shared exact lat/lon with parking markers #1 and #3 respectively.

**Root Cause:** The script assigned sequential color labels to parking markers (1=blue, 2=green, 3=yellow) and EV markers (A=purple, B=orange, C=blue). When EV A lands at the same coordinate as parking marker #1, Google only renders one of them — typically the first in the URL order.

**Fix:** Before appending each EV marker, the script now checks if its coordinates are within 0.0001° of any already-added parking marker. If so, it applies a ~4m NE offset (+0.00004° lat, +0.00004° lon) before adding to the marker list. C has no overlap and retains original coordinates.

**Verification:** After fix, Static Map URL for postal 670238 shows:
- A: 1.378939, 103.770655 → shifted to 1.378979, 103.770695
- B: 1.380764, 103.771880 → shifted to 1.380804, 103.771920

**Lesson:** Always visually verify multi-marker Static Maps in Discord, not just URL construction. Overlapping markers are a silent rendering failure — the URL looks correct but some pins never appear.

### 2026-05-28 — Blue Polyline Despite Traffic Data (v1.8.0 → v1.8.1)

**Symptom:** Cron run at 07:05 reported `Current: ~44 min (traffic-aware)` — `duration_in_traffic` was returned (ratio 1.128). Yet the Static Map polyline was blue (`0x1565C0`).

**Root Cause:** The route computation block returned traffic data, but the Static Map generation block re-ran the route fetch with a different parameter set that returned no `duration_in_traffic`, then coloured the polyline based on that second, traffic-less response.

**Fix:** The Static Map colour decision must use the same route response (or explicitly passed `normal_secs` / `traffic_secs` variables) as the duration display — not a separate API call. The cron prompt was updated to inline both the route computation and Static Map generation in a single execution block with explicit variable passing.

**Traffic colour thresholds tightened (< 1.10 green / 1.10–1.35 amber / ≥ 1.35 red)** — the 07:05 run had ratio 1.128, which is borderline moderate. Thresholds updated in `references/traffic-colour-update-2026-05-28.md`. **(Update 2026-06-06: the no_agent `morning_commute.py` was still using stale `1.15/1.40` thresholds despite this doc — corrected to `1.10/1.35` in v1.9.0. See the 2026-06-06 entry below.)**

### 2026-06-01 — Parking Table Column Reorder + Live Emoji (v1.8.1 → v1.8.2)

**Symptom:** Old parking table column order was `# | System | Code | Dist | Avail | Live | Address` with the Live cell rendered as plain text `live`/`static` — inconsistent with the rest of the skill (which uses `✅`/`❌` for live/static indicators in many other contexts, and matches the user's stated preference).

**Root cause:** The script's `render_table()` call inherited an early column order that put lookup identifiers (`System`, `Code`) before the user-facing facts (`Dist`, `Avail`, `Live`). The Live cell string `live`/`static` was also legacy — the SKILL.md example already showed `✅`/`❌` but the script never matched it.

**Fix (2026-06-01):**
1. Reordered columns to user's spec: `# | Dist | Avail | Live | Code | System | Address` (lookup facts first, identifiers last)
2. Changed Live cell from `live`/`static` to `✅`/`❌` (HDB/LTA = live, URA/JTC = static). The `_dwidth()` function is east-asian-width aware so the table still aligns.
3. Updated SKILL.md "Output Format (verified)" example to match
4. Updated `references/carpark-availability.md` to document the new column order as a class-level format rule (do not change without asking) and to fix the obsolete "keep cell content ASCII, no emoji in columns" warning — that was a misread of the original limitation, not a real constraint

**Lesson:** The parking table's column order and Live-cell rendering are user-facing format choices, not implementation details. They belong in `references/carpark-availability.md` as an explicit rule so future agents don't quietly reformat the output. When changing table column order, the `render_table()` headers AND the row data tuple must move together — they're two halves of the same contract.

**Verification:** `python3 scripts/nearest_carpark.py "530685"` (Hougang) shows `1  81m  145/458  ✅  HG45  HDB  ...` — correct new order, ✅ Live cell.

### 2026-06-01 — `extract_media()` Gateway Batches Out of Order; --send Curl Multipart Is the Only Reliable Image+Text Pairing (v1.8.2 → v1.8.3)

**Symptom:** Multi-section reports (carpark, EV, location map) had their images reordered or detached from their headers in Discord. Header `🅿️ PARKING` would arrive with the location map image; `⚡ EV CHARGING` would arrive with the parking table.

**Root cause:** The Discord gateway's `extract_media()` (in `gateway/platforms/base.py` 1888–1943) scans the **entire** reply string for `MEDIA:<path>` tags, sends all text in one batch, then sends all images as a separate batch. Order is lost. The `---` separator pattern does not work — re-batching happens regardless of how the response is structured. This is NOT a cron-only bug; it fires any time the agent uses `send_message` to deliver interleaved text+image content.

**Fix applied (2026-06-01):**
1. New reference doc `references/discord-image-delivery.md` — documents the `extract_media()` gotcha, the curl multipart pattern, `[SILENT]` cron suppression, and the section-list slicing contract
2. `nearest_carpark.py --send <channel_id>` mode — script self-delivers. Reads `DISCORD_BOT_TOKEN` from `~/.hermes/.env`, posts each section via `curl -F content=… -F file=@…` so header + image are atomic
3. Section list pattern — `sections = [(header, body, image_path_or_none), ...]` — empty-table sections are skipped; renderer maps empty tables to `null`
4. Generated table PNGs auto-cleaned after delivery; static map kept (small, may be reused)
5. `[SILENT]` printed to **stderr** in `--send` mode so the cron system auto-delivery is suppressed

**Verification:** `python3 scripts/nearest_carpark.py "508529" --send 1499972245437550602` → 3/3 sections delivered, each as a single Discord message with the image attached to the header atomically.

**Lesson:** The Discord `extract_media()` is a *gateway-level* constraint, not a cron-only or sender-only quirk. **Never rely on `send_message` to pair text with images** — even one section at a time risks reordering under load. The atomic primitive is the HTTP request, not the agent's call order. Any new sg-commute script (or any other skill that needs interleaved image delivery) must use the curl multipart pattern or extend `nearest_carpark.py --send` mode.

### 2026-06-01 — "Total parking: N" Verbiage Removed; LOCATION Address Body Dropped Then Restored (v1.8.3 → v1.8.4)

**Symptom 1 (user request):** User asked to remove the `Total parking: 3` / `Total EV: 1` verbiage from each section header. The counts were redundant with the table content.

**Symptom 2 (regression I introduced):** To remove the verbiage, I collapsed the section tuple from `(header, body, image)` to `(header, image)` — removing the `body` field entirely. This silently dropped the LOCATION section's resolved address (e.g. `200 NETHERAVON ROAD SINGAPORE 508529`). User caught it within one round-trip: "The address after location pin is now missing."

**Root cause:** The `body` field carried two unrelated kinds of content — count summaries for PARKING/EV (which the user wanted gone) AND the resolved address for LOCATION (which the user wanted to keep). A naive field removal cleaned up one but broke the other.

**Fix applied (2026-06-01):**
1. Kept the `body` field. Per-section `body` is now `None` for table sections (PNG is self-describing) and `loc["address"]` for LOCATION.
2. `references/discord-image-delivery.md` got a new pitfall: *"Remove X" requests — the field may carry more than X.* Grep for the exact phrase, inspect every consumer of the same field, keep the field but clear only the offending content, then run the script to verify.

**Lesson:** When the user asks to remove specific text, the change is rarely a clean field removal. Treat every "remove X" as a request that *might* require per-section branching, not a one-line field drop. Always run the script after the edit and visually confirm the unchanged sections still produce the expected output. The bug was caught in one round-trip because the LOCATION address is a user-visible fact that the user can immediately spot as missing — but a less-visible regression (e.g. dropping a timestamp or a system label) might have lurked for weeks. Always sanity-check by running the script and looking at the full output, not just the field the user asked about.

### 2026-06-01 — LLM Boundary: Who Spends Tokens, Anyway? (no_agent Plan Abandoned)

**Symptom:** User asked for the carpark search to become a no_agent Python script. I wrote a 9KB plan wrapping `nearest_carpark.py` in a `~/.hermes/scripts/` cron wrapper. User pushed back: "isn't it already not requiring additional LLM calls since nearest_carpark.py is just python script?"

**Root cause:** I failed to state the *who-spends-tokens* boundary upfront in the plan. The script is pure Python; running it from a terminal or cron already costs zero tokens. A wrapper would add value only for: (a) `[SILENT]` cron auto-suppression when the script does its own Discord delivery (which the carpark script already does via `--send`), (b) a default target for the cron schedule (which wasn't requested). Neither was requested. User abandoned the plan.

**Lesson for next time:** Before proposing a no_agent/cron wrapper, explicitly state the LLM boundary in one line. *"`nearest_carpark.py` is already zero-token when invoked from terminal/cron. The wrapper would add only [X marginal value]. Want me to proceed?"* If the marginal value is small, the user will likely say no — and they'll appreciate the upfront clarity, not a 9KB plan they have to read to find the cost. This applies to any "should this run as no_agent?" question: first state the cost boundary, then propose the wrapper only if the marginal value is real.

### 2026-06-06 — Code-Review Bug-Fix Pass + Performance Overhaul (v1.8.4 → v1.9.0)

A full review of the three sg-commute scripts surfaced correctness bugs and efficiency wins, all fixed and verified end-to-end against live APIs. **File locations** (note: the cron scripts are NOT under the skill dir): `nearest_carpark.py` → `skills/productivity/sg-commute/scripts/`; `morning_commute.py` (cron `b617d89da15b`) and `evening_weather.py` (cron `c967008ca121`) → `~/.hermes/scripts/`. **Backups:** `*.bak-bugfix-20260605` (correctness pass), `*.bak-perf-20260606` (perf pass), plus `ev_*.csv.bak-perf-20260606`.

**Bug fixes — `nearest_carpark.py`:**

| Sev | Bug | Fix |
|-----|-----|-----|
| Critical | `fetch()` fell through to an implicit `None` on a curl non-zero exit (timeout/DNS/reset don't raise in Python), so `hdb_live`/`lta_live`/`ev_by_postal` crashed with `AttributeError` on `.get()` outside their try/except | `fetch()` now **raises** on exhausted retries → each caller's existing try/except returns its documented empty value |
| Critical | `geocode` did `comp["postal_code"]` — a `KeyError` on *every* Google-resolved postal (the value lives in `long_name`), silently defeating the Google fast path (masked only by the OneMap fallback) | `comp.get("long_name", "")` |
| High | `_discord_send_pair` sniffed the body for `'401'` and treated 429/400/403/5xx as success → silent partial delivery reported as `Sent N/N` | reads real HTTP status via `-w '%{http_code}'`; 2xx-only success; retries transient 429/5xx with backoff |
| High | static-map download failure silently delivered the LOCATION section with no map and no indication | appends `*(map unavailable)*` and drops the image |
| Med | `_load_token()` didn't handle an `export ` prefix or quoted value → wrong/`None` token → 401 | strips `export ` and surrounding quotes |
| Med | `--send`/`--json` argv parsed via `sys.argv.index()` (first match) → wrong channel on duplicate/ambiguous tokens | positional parse |
| Med | static-map URL missing `format=png32` | added |
| Low | `-F content=` let an address starting with `@`/`<` be read by curl as a file | `--form-string` |
| Low | table PNGs leaked if a send raised; redundant `x != 0` filter in `nearest_hdb` | cleanup in `finally`; filter simplified to `if x and y` |

> **Not changed:** the EV `status == "1"` = *available* assumption. The code-review flagged it as unverifiable from the script alone, but `references/ev-charging.md` documents it as **verified (May 2026)**: `evIds[].status` `"1"` = available, `"0"` = occupied. So the code is correct — left as-is.

**Bug fixes — `morning_commute.py`:**
- **High** — traffic-colour thresholds were `1.15/1.40`, not the documented `1.10/1.35` (under-reported congestion across the 1.10–1.15 and 1.35–1.40 bands). Corrected to match `references/traffic-colour-update-2026-05-28.md`.
- **High** — no distance validation existed (the SKILL.md guard was never in the no_agent script). Added a retry-without-waypoint fallback when the route returns `< 30 km` (Innovis) / `< 8 km` (AMK), appending `*(route may be inaccurate — verify manually)*` if still short.

**Bug fixes — `evening_weather.py`:**
- **Critical** — no error handling: an empty/failed API response raised a `KeyError` whose **traceback was delivered to Discord verbatim** (cron posts stdout). Now each block (24-hr, 2-hr) degrades independently with a top-level guard emitting a minimal valid report.
- **Med** — "Heavy Rain"/"Heavy Showers" rendered as the light 🌦️ because the generic `"rain"/"shower"` branch shadowed the heavy check. Heavy-rain check moved ahead of it.
- **Med** — added `fc_text()` so a dict-shaped `forecast` value never reaches `.lower()`/`%s` (would crash → traceback to Discord).

**Performance / efficiency — before vs after:**

| Change | Workload | Before | After |
|--------|----------|--------|-------|
| Parallelize the 5 morning sections (`ThreadPoolExecutor(5)`) | full `morning_commute innovis` run | 3.8–10.4s (median 8.5s) | **5.5–5.7s** (median 5.5s) — variance collapsed; flaky-endpoint retries no longer stack serially |
| EV scan: per-thread keep-alive `requests.Session` + retry-5xx; workers **64→12** | one 303-postal scan | 6.7s (curl-per-call) | **1.2s** (~5.6×) |
| Both tables in **one** renderer invocation (`render_discord_tables`) | warm carpark run | — | ~0.12s saved (renderer is built for batch) |
| `nearest_ura` parses each lat/lon once | per-row over ~2000 URA rows | — | sub-ms |

- The EV-scan change is also a **correctness** win: high curl concurrency provoked LTA-server HTTP 500s that the old path silently swallowed as "no chargers." `--refresh` (11 anchors) dropped from a projected ~70s to **15.5s** and recovered **24 chargers** prior scans had lost.
- The EV scan now decomposes the **2-digit postal sector** (`pi // 10000`); the old `pi // 100000` (first digit) probed sectors 03/13/23 for a sector-13 address — scattered across the island, missing the true neighbours.
- `requests` is safe for the EV endpoint (LTA DataMall + AccountKey header); the **urllib-403 caveat is data.gov.sg-specific**, so HDB / OneMap / NEA rainfall stay on curl.

**Lesson:** A "faster" change can hide a *correctness regression* — the session EV scan looked great on wall-clock until measuring **true 200-count** exposed that high concurrency triggers server 500s the old curl path was swallowing. Always measure success, not just speed. The inverse held for the morning report: the real win wasn't a lower median but killing the long tail — parallelism turned a 3.8–10.4s spread into a tight ~5.5s. And several of these bugs (the `fetch()` `None`, the `comp["postal_code"]` `KeyError`, the swallowed 500s) were *latent* — masked by fallbacks or silent except paths — so they only surfaced under review and load testing, not in normal runs.

### 2026-06-06 — "Air Quality unavailable" = data.gov.sg 429 rate-limit; added API key (v1.9.0 → v1.9.1)

**Symptom:** A regression-test of the morning report (AMK + Innovis sent back-to-back to #playground) showed **section 4 "Air Quality — unavailable"** on the Innovis report only. AMK was fine.

**Root cause:** Not flaky data and not a parser bug — **HTTP 429 from `api-open.data.gov.sg`.** The endpoint allows only **~5 requests per ~10s per IP** anonymously (verified: calls 1–5 → 200, call 6 → 429), returning `{"code":24,"name":"TOO_MANY_REQUESTS","data":null,...}` with **no `Retry-After` header**. The report fires **5 data.gov.sg calls per run** (24hr/2hr/rainfall/flood/pm25) — exactly at the limit — and because the 429 body has `data:null`, `curl_json`'s `want` predicate can't tell it apart from an empty payload, so it surfaces as "unavailable." Innovis lost because the two reports ran seconds apart **sharing one per-IP budget**; the second one hit the depleted window.

**Contributing factor (v1.9.0 #1 change):** the section-parallelization fires all ~4–5 weather calls *simultaneously* instead of spaced out. A concurrent burst trips the limit far more readily, and the synchronized 1.5s retries re-collide. Burst test: pm25 empty 3/5 rounds (vs 2/8 sequential) — in the bad rounds **all four endpoints 429'd together**. (Same class of issue as the EV-endpoint 500s under concurrency, above.)

**Fix applied (2026-06-06):**
1. **API key** — registered a data.gov.sg key, stored in `~/.hermes/.env` as `DATAGOV_API_KEY` (NOT hardcoded). Sent as the **`x-api-key`** header on every `api-open.data.gov.sg` call. Verified: lifts the limit from ~5 to **~12+/window** — comfortably covers a report's 5 concurrent calls and both reports back-to-back.
2. **429-aware backoff** — `curl_json` in `morning_commute.py` and `evening_weather.py` now detects `name == "TOO_MANY_REQUESTS"` / `code == 24` and backs off **~10s** (not 1.5s) so any residual rate-limit retry actually recovers instead of re-colliding.
3. Both scripts read the key via the same `.env`-parse pattern as `nearest_carpark.py`'s `_load_token` (tolerates `export `/quotes). See SKILL.md → "Environmental Data" for the correct `x-api-key` fetch pattern.

**Verification:** AMK + Innovis run back-to-back → **both** show Air Quality populated (10–12 µg/m³ Moderate 🟡); re-delivered to #playground, both HTTP 200 with the section present. Backups: `*.bak-datagov-20260606` (scripts + `.env`).

**Lesson:** "Section unavailable" in a data.gov.sg report is far more often a **429 rate-limit** than a parser or empty-data problem — and a `data:null` 429 body is indistinguishable from empty unless you check the HTTP status / `name`. Always send the `DATAGOV_API_KEY` (`x-api-key`). Also: parallelizing same-host calls (the v1.9.0 win) quietly raised the 429 rate — when you fan out requests, remember they may all hit **one** rate-limited host, and stagger, key, or back off accordingly.

### 2026-06-06 — Progressive Disclosure: SKILL.md slimmed, history moved here (v1.9.1 → v1.9.2)

**Change:** `SKILL.md` had grown to ~631 lines / ~13.4K tokens — and it loads into agent context on *every* interactive skill invocation (the cron paths are `no_agent`, so they pay nothing). Token-efficiency pass:
1. **This changelog** — the entire "Lessons Learned" section (~203 lines / ~6.4K tokens, 8 dated entries) moved here, leaving a pointer + a distilled "Active gotchas" list in SKILL.md.
2. **Route Computation** slimmed in SKILL.md to the actionable rules (waypoint rule, distance guards, png32, traffic-colour thresholds); full code/static-map/duration details remain in `references/google-routes.md`.
3. **End-to-end pipeline diagram, output-format example, and verified-test/runtime tables** dropped from SKILL.md — they live in `references/carpark-availability.md` (pipeline, verified results) and `references/discord-image-delivery.md` (delivery format). The standalone "API Verification" status table was dropped (Google status is in `google-routes.md`; data.gov.sg status is the rate-limit note in SKILL.md → Environmental Data).

Result: SKILL.md ~13.4K → ~4K tokens (~70% lighter) with no information lost — everything is one reference-read away, and the References table + inline pointers tell the agent exactly where.

**Lesson:** A mature skill's SKILL.md should be a lean *index + critical operational rules*, not an archive. Historical post-mortems are valuable but belong in an on-demand reference, because the always-loaded baseline is paid on every single invocation. Progressive disclosure (lean core → lazy-loaded references) is the right structure once a skill accumulates history.

### 2026-06-06 — `--thread` delivery + terminal-output dedup + OneMap address restore (v1.9.2 → v1.9.3)

**`--thread <thread_id>` (added):** `nearest_carpark.py --send <channel> --thread <thread>` routes the POSTs to a thread's message endpoint (Discord v10 treats threads as channels — same URL, different ID). `TARGET_CHANNEL = SEND_THREAD or SEND_CHANNEL`; the delivery summary distinguishes thread vs parent channel. Documented in SKILL.md → Quick Start. (Note: passing a thread ID straight to `--send` already worked; `--thread` mainly adds the clearer summary label.)

**Terminal-output dedup (fix):** a `format_section_content()` helper now produces the canonical `**header**\n body` string for **both** `--send` and terminal output, so an ad-hoc agent delivery loop can't grab a divergent decoration and post it verbatim. Initial version printed *both* `--- section: HEADER ---` and `**HEADER**` (header twice); removed the decoration line so terminal output shows only the canonical content + `MEDIA:` tag (blank line + bold header delimit sections).

**OneMap address restore (fix):** the 2026-06-06 `comp["postal_code"]→long_name` geocode fix had a side effect — Google geocode now succeeds for bare postal inputs and early-returned its terse `formatted_address` ("Singapore 518073"), so the LOCATION section lost OneMap's building-level name ("27 PASIR RIS GROVE COCO PALMS SINGAPORE 518073"). Coordinates were always correct (cosmetic only). **Fix:** `geocode()` now tries **OneMap first for 6-digit postal inputs** (authoritative SG geocoder, building-level ADDRESS + native SVY21), Google first only for street addresses. Also one fewer API call on the common postal path.

**Lesson:** when two providers can resolve the same input, "try A then B" silently depends on A's failure modes — the moment you *fix* A's errors, B stops winning and output quality can regress in a way unrelated to the original change. Order providers by *who gives the best answer for this input class*, not by incidental fall-through.

### 2026-06-30 — ECP waypoint detour investigated, route left as-is

**Observation:** The morning Innovis route (job `b617d89da15b`, Tue/Thu) detours off ECP onto Marina East Dr / East Coast Park Service Rd before reaching MCE. Step-by-step trace confirms it: `Merge onto ECP (11.6km) → Take exit 13 for Fort Rd → Marina E Dr loop (1.7km) → Make a U-turn → Take the ramp to MCE`. Net ~3.5km weaving on local roads. Cause traced to `morning_commute.py` line 256 hardcoded waypoint `via:1.292,103.885` (a coordinate just past the ECP→MCE junction); Google treats it as a stopover and physically routes off ECP. The `>30km` distance gate doesn't fire (route still ~36km).

**Attempted fix (option 1 — move via: east):** Tested ~10 alternative `via:` coordinates along ECP. **All triggered the same detour pattern** (off at the nearest exit ramp, weave through E Coast Park Service Rd with U-turn). Singapore expressway ramps are tightly packed; Google's snap-to-road for `via:` keeps landing on the nearest exit rather than the expressway carriageway. No clean alternative coordinate found.

**Tested option 2 (no waypoint) for context:** Google defaults to **TPE → KPE → MCE → AYE**, 29.84km, zero off-ramp detour, all expressway. This is actually cleaner than the current route. But changing to "no waypoint" requires lowering the Innovis distance gate (>30km) since the new natural route is right at the edge — and is a behavior change beyond the user's "investigate only" scope.

**Decision (user, initial):** Leave the route as-is (this was an "investigate only" session). **SUPERSEDED later the same day — the user asked to fix the detour and Option 2 (no waypoint) was applied. See the next entry.**

**Lesson:** Coordinate `via:` waypoints on dense expressway networks are fragile — Google's snap logic favours the nearest vertex (often a ramp junction) over the expressway carriageway lane. Verifying a fix requires actual step-by-step inspection, not just "the polyline looks continuous and total distance looks right." A clean-looking polyline can still encode an exit-ramp-off-expressway-and-back bug.

### 2026-06-30 (later) — ECP waypoint detour FIXED: dropped the Innovis `via:`

> **⚠️ SUPERSEDED 2026-07-02** — the user wanted to keep the ECP→MCE corridor, so the waypoint was *restored* (cleanly, via two mainline via-points + a detour guard). This "no waypoint" state was live only 2026-06-30 → 07-02. See the next entry.

**Change:** Reversed the "leave as-is" decision above — the user asked to fix the detour. Applied **Option 2** in `morning_commute.py` `section5()`:
- `wp = None` for **both** destinations (was `via:1.292,103.885` for Innovis). Innovis now takes Google's natural no-waypoint route.
- Innovis distance gate lowered **30 → 25 km** — the no-waypoint route is ~30 km, below the old 30 km floor, so the gate would otherwise have falsely appended `*(route may be inaccurate)*` to every Innovis report.
- Removed the now-dead retry-without-waypoint fallback (nothing to retry when there's no waypoint); the gate now simply flags an implausibly short single route.

**Verified live (~22:10 SGT):** `morning_commute.py innovis` → 31.6 km, no inaccuracy note, traffic-aware 🟢, valid 658 KB PNG32. Step trace = TPE→PIE→CTE→AYE→Buona Vista, **no U-turn, no off-ramp weaving, all expressway**. PNG visually inspected — clean polyline, no Marina East loop. (An earlier same-evening trace gave a 29.84 km KPE route; the corridor varies with live traffic, but the no-waypoint route never detours.) Backup of the pre-fix script: `~/.hermes/scripts/morning_commute.py.bak-pre-waypoint-fix-20260630`.

**Docs updated:** `SKILL.md` (waypoint rule, distance table, distilled guards), `references/commute-report.md` (waypoint rule, table, code snippet, distance-validation text), `references/google-routes.md` (waypoint guidance). The traffic-camera files (`commute_camera_map.py`, `traffic-images-api.md`, `commute-camera-filtering.md`) still reference a `via:` waypoint — that's the separate route-camera feature, left unchanged.

**Lesson:** "Investigate only" conclusions age into stale advice fast — the `via:` survived partly because the prior session signed off with "leave as-is." When the natural, unconfigured behaviour is cleaner than a workaround, prefer deleting the workaround over tuning it.

### 2026-07-02 — ECP→MCE corridor RESTORED cleanly (two mainline via-points + detour guard)

**Why:** The user wanted the morning Innovis map to show their actual ECP→MCE drive, not the shorter KPE/PIE route the no-waypoint fix produced. The detour was never "ECP is impossible" — it was a *single* `via:` near the ECP↔MCE interchange snapping to the parallel East Coast Park Service Rd, forcing an exit + U-turn.

**Fix (`morning_commute.py` `section5()`):**
- Innovis now forces **two** coordinate `via:` points that *bracket* the interchange — one on the ECP mainline east of it, one on the MCE mainline west of it: `INNOVIS_VIA = "via:1.30860,103.93347|via:1.27616,103.86845"`. AMK stays direct. Distance gate unchanged (Innovis > 25 km; the ECP route is ~35 km).
- Coordinates are **derived, not guessed** — `~/.hermes/scripts/verify_ecp_route.py` fetches the legacy `via:1.292,103.885` route (which does traverse the ECP mainline before detouring), lifts a mid-carriageway vertex from its ECP and MCE steps, re-tests, and prints the `INNOVIS_VIA` line. Mainline-derived points snap back onto the expressway, not the service road.
- Added `route_has_detour(legs)`: if the forced route has a U-turn / drives Marina East Dr / East Coast Park Service Rd (or comes back < 25 km), it auto-refetches with **no** waypoint and appends `*(ECP route unavailable — showing fastest route)*`. The U-turn map can't ship again even if Google's geometry shifts.

**Verified:** `verify_ecp_route.py` → derived route 34.89 km, `detour=False` (ECP → exit 14B → MCE). Today's 07:25 Thu→Innovis cron run delivered 34.9 km, no fallback note (`last_status: ok`). Live re-run + step trace: TPE→PIE→ECP→exit 14B→MCE→Keppel Viaduct→AYE, **U-turn: False**, valid PNG32; map visually inspected — clean coastal ECP→MCE sweep, no Marina Bay loop. Deployed script sha256 matches the local staged copy. Pi backups: `morning_commute.py.bak-pre-waypoint-fix-20260630`, `…bak-waypointfix`, `…bak-<timestamp>`.

**Docs updated:** `SKILL.md` (v1.9.6 → 1.9.7: waypoint rule, distance table, validation bullet, distilled guards), `references/commute-report.md` (waypoint rule, table, code snippet, distance/detour text), `references/google-routes.md` (waypoint guidance). Traffic-camera files unchanged (separate feature).

**Lesson:** "Remove the workaround" (06-30) and "keep the corridor" (07-02) were each right for their goal — the durable fix was making the waypoint *robust* (two mainline-**derived** points + a detour guard) rather than choosing between "fragile single via" and "no via". When a workaround is fragile, harden it against its failure mode and guard it; don't just delete it.

### 2026-07-02 (later) — Full hermes-skill-tuneup pass (v1.9.7 → 1.9.8)

Ran the four-pass tune-up (Phase 0 → bug-hunt fan-out → terminal gate → docs). Three blind reviewers (control-flow / data-shape / security lenses) + an adversarial refuter, then a fresh terminal-gate fan-out over the cumulative diff — every finding reproduced on live/synthetic data via the gateway venv interpreter.

**Fixed — `morning_commute.py`:**
- **section5 could ship a U-turn polyline with no caveat.** When the forced ECP route detoured/was short AND the no-waypoint fallback was *also* bad, the old code silently kept the forced detour route (dist ≥ 25 → no note). Now the forced fetch is wrapped in try/except; on detour/short/error → fall back to no-waypoint ("ECP route unavailable"); if the fallback is unavailable too → ship the best route but always flag it; a final safety net flags any shipped route < 25 km or (Innovis) still weaving. (The old code also lost the whole section if the forced Google request threw — fallback was unreachable on an exception.)
- **section2 sibling-drift landmine.** The 2hr section used the raw `forecasts[].forecast`; if that field migrates string→dict (as the 24hr sibling already did), `wx_emoji().lower()` would AttributeError and drop the whole section. Normalised through a new shared `_fc_text()` (dict-or-string), matching section1 + `evening_weather.fc_text`; section1's per-period region text got the same guard.
- **`wx_emoji` diverged from `evening_weather.emoji_for`.** morning lacked `drizzle`/`lightning`/`storm` branches, so a drizzly/stormy morning rendered ⛅ (violating SKILL.md's own fallback rule). Aligned (`drizzle`→🌦️, `lightning`/`storm`→🌧️); both now agree on every real NEA enum (day/night default glyph is the only intended difference).
- `rm -f` cleanup got `timeout=10`.

**Fixed — `evening_weather.py`:** removed the `emoji_for` `startswith("heavy")` branch that mapped non-precip "Heavy …" strings to 🌧️ (real "Heavy Rain/Showers" still → 🌧️ via the earlier branch).

**Fixed — `nearest_carpark.py`:** `hdb_live`/`lta_live` parse loops now guard per-item (`try/except (KeyError,ValueError,TypeError): continue`) — a malformed HTTP-200 row (missing `carpark_number`/`CarParkID`, non-numeric lots) previously crashed the whole report (no top-level guard), contradicting the "returns empty dict on failure" docstring.

**Terminal gate caught two regressions in the above fixes** (fixed before shipping): the final-safety detour check was initially gated on `not route_note`, so a weaving *fallback* still shipped unflagged, and it flagged legitimate AMK U-turns (now scoped to Innovis); section2's `not area_fc` was corrected to `is None` (presence vs blank) so a blank Pasir Ris forecast shows "n/a" instead of mislabelling a neighbour's.

**Verified:** all three scripts `py_compile` + dry-run green under the gateway venv; morning innovis 34.9 km clean ECP (no note), amk 13.8 km, evening OK; deployed sha256 == local. No hardcoded secrets; confirmed the `MEDIA:` stdout line is the *correct* attach mechanism for a no_agent cron (not the interactive `extract_media` reorder bug).

**Docs synced:** SKILL.md (v1.9.7→1.9.8), commute-report.md, and **CLAUDE.md** (was stale from the ECP-restore session — waypoint rule/table, distance gate 30→25 km, cron time 07:28→07:25, evening emoji list incl. the removed "heavy prefix" rule).

**Report-only (not fixed):** empty `argv[1]` shadows the `DEST` env override (manual-only); `verify_ecp_route.py` helpers hard-subscript (manual deploy tool); `nearest_carpark.py` has no `if __name__=="__main__"` guard so importing runs the live report (pre-existing; worth a future pass).

---

## Relocated 2026-09-29 (superseded — kept as a record, do NOT follow)

These blocks were cut from the live docs in the v1.11.0 tune-up because they instructed behaviour that no longer exists or that caused incidents (e.g. the hand-rolled Discord curl that led an agent to print the bot token). Current behaviour: `SKILL.md`, `discord-image-delivery.md`, `commute-report.md`, `developer-guide.md`.

<details><summary>SKILL.md v1.10.0 body (before the 2026-09-29 slimming)</summary>

#### Singapore Commute

Complete Singapore commute management — route planning, live environmental conditions, carpark availability, and the daily morning commute report for Discord.

##### Shared Locations

| Label | Address | SVY21 | WGS84 |
|-------|---------|-------|-------|
| Home | Pasir Ris Grove, Singapore 518141 | — | 1.37290, 103.95060 |
| Innovis | 2 Fusionopolis Way, Singapore 138634 | 23148.2, 31188.9 | 1.29834, 103.78972 |
| AMK | 12 Ang Mo Kio Street 64, Singapore 569088 | 29466.6, 41365.8 | 1.39037, 103.84650 |

**"Innovis" shortcut**: When user says "Innovis", use `2 Fusionopolis Way, Singapore 138634` directly — do not geocode. (Verified in user's memory profile.)

##### Quick Start — Parking Queries

**When user asks for parking/carpark near a Singapore address or postal code, run this FIRST — do not manually fetch LTA API or use the generic `maps` skill:**

```bash
PYTHONUNBUFFERED=1 python3 /home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py "<postal_or_address>"
```

This searches ALL 5 systems (HDB live, LTA live, URA static, JTC static, EV charging), generates table images + map, and outputs sections ready for Discord delivery. Manual LTA-only fetches miss URA/HDB/JTC coverage and lack the formatted table output.

**Delivery to Discord thread** (atomic header+image pairing required):

```bash
# Read DISCORD_BOT_TOKEN from ~/.hermes/.env, then curl multipart per section
curl -s -X POST "https://discord.com/api/v10/channels/<thread_id>/messages" \
  -H "Authorization: Bot <token>" \
  -F "content=<section header>" \
  -F "file=@<section_image>"
```

The gateway's `extract_media()` reorders images — always use curl multipart, not `send_message` with `MEDIA:` tags. See `references/discord-image-delivery.md` for details.

**`--send` mode** — script self-delivers (reads bot token from `.env`), and the agent reads only `[SILENT]\nSent N/N` (cheapest path, no manual curl needed):

```bash
PYTHONUNBUFFERED=1 python3 .../nearest_carpark.py "<postal>" --send <channel_id>
# When the report should land in a thread (e.g. the user asked inside one):
PYTHONUNBUFFERED=1 python3 .../nearest_carpark.py "<postal>" --send <channel_id> --thread <thread_id>
```

`--thread <thread_id>` routes the POSTs to the thread's message endpoint (Discord's v10 API treats threads as channels; the URL is identical, only the ID changes). Default (no `--thread`) posts to the parent channel. `--send` still names the parent channel purely for the delivery-summary print line.

##### References

| Reference | Purpose |
|-----------|---------|
| `references/carpark-availability.md` | HDB/LTA/JTC/URA/EV carpark systems — live APIs, static CSVs, geocoding, EV charging; **the end-to-end pipeline, output format & verified test results/runtimes** |
| `references/static-map-discord-rendering.md` | Why polyline may be invisible in Discord despite being in the file — PNG8 vs PNG32, pixel analysis, URL fixes |
| `references/commute-report.md` | Morning commute report workflow and Discord format |
| `references/google-routes.md` | Google Routes/Directions API — DRIVE mode, polyline encoding, traffic-aware, Static Map generation, **full Python code, API status, pitfalls** — **traffic colour thresholds: green <1.10 / amber 1.10–1.35 / red ≥1.35** |
| `references/silent-delivery-failure.md` | Recurring bug: agent skips Discord delivery; diagnosis, fixes, and verification |
| `references/evening-weather-workflow.md` | Evening Weather Forecast (10pm Mon/Tue/Wed): emoji mapping, double-delivery fix, bug history |
| `references/cron-prompt-delivery-patterns.md` | Cron prompt anti-patterns: why agent reasoning leaks into output, correct `deliver` vs `send_message` patterns |
| `references/weather-api-response-shapes.md` | Corrected response shapes for twenty-four-hr-forecast, two-hr-forecast, pm25, and rainfall v2 APIs |
| `references/traffic-duration-troubleshooting.md` | Why `duration_in_traffic` is often absent — ISO vs Unix timestamps, `traffic_model`, time-of-day limitations |
| `references/traffic-colour-update-2026-05-28.md` | Traffic colour thresholds updated (<1.10 green, 1.10–1.35 amber, ≥1.35 red) — why the May 28 polyline was blue despite traffic data |
| `references/discord-image-delivery.md` | **Atomic header+image pairing for Discord** — `extract_media()` gotcha (gateway batches all `MEDIA:` tags out of order), curl multipart pattern with `-F content=... -F file=@...` for atomic pairing, `[SILENT]` cron suppression, section-list slicing pattern, `--send <channel_id>` self-delivery mode |
| `references/commute-camera-filtering.md` | Filter cameras along a commute route (polyline decode → distance check → labeled map markers) |
| `references/traffic-images-api.md` | Data.gov.sg Traffic Images API — camera data, image CDN auth, Google Static Maps markers, region splitting, reverse geocoding |
| `references/changelog.md` | **Full dated history** of every bug, fix & design change (2026-05 →). Read before changing route/waypoint/delivery/EV-scan logic, or when debugging a recurring issue or "why is it like this". |

##### Route Computation (Google Directions / Routes API)

Routes for the morning report are computed by `morning_commute.py` (no_agent). For ad-hoc route work — full Python, polyline encoding, Static Map generation, the Directions-vs-Routes-v2 format differences, and pitfalls — see **`references/google-routes.md`**.

**Waypoint rule (the one thing that bites):** Innovis is forced onto the **ECP → MCE** corridor with **two coordinate `via:` points on the ECP and MCE mainline** (`morning_commute.py` → `INNOVIS_VIA`); AMK uses **no** waypoint (direct). Google won't pick ECP→MCE unaided (KPE/PIE is a few km shorter), and a *single* `via:` near the ECP↔MCE interchange snaps to the parallel East Coast Park Service Rd → Google exits + U-turns to reach it (the old `via:1.292,103.885` bug: ECP exit 13 → Marina East Dr → U-turn → MCE). **Two** points that *bracket* the interchange keep the route on the expressway (verified 2026-07-02: ECP → exit 14B → MCE, 34.9 km, no detour). **Never** use a single via or an address-based waypoint (Google treats addresses as stopovers and truncates the `overview_polyline`); derive coords from a real route's step geometry (`verify_ecp_route.py`, changelog 2026-07-02). A `route_has_detour()` guard auto-falls back to the no-waypoint route if the forced route ever weaves, so the U-turn map can't return.

| Day | Dest | Waypoint | Valid distance |
|-----|------|----------|----------------|
| Tue/Thu | Innovis | 2× `via:` ECP+MCE mainline (`INNOVIS_VIA`) | **> 25 km** (expect ~35) |
| Wed | AMK | none (direct) | **> 8 km** (expect 13–15) |

- **Validate distance / detour:** the forced Innovis route is kept only if `route_has_detour()` sees no U-turn/service-road weave **and** distance ≥ 25 km; otherwise it auto-refetches with no waypoint and appends `*(ECP route unavailable — showing fastest route)*`. A final safety net then flags whatever route ships with `*(route may be inaccurate — verify manually)*` if it's still < 25 km, **or** (Innovis only) still weaves — so a weaving/short *fallback* is never delivered unflagged.
- **Static maps: always `format=png32`** — PNG8 (256-colour palette) silently drops/misrenders the polyline in Discord's image pipeline (file size jumps ~180KB→~570KB+).
- **Traffic colour** (ratio = traffic_secs / normal_secs): 🟢 `0x00C853` < 1.10 / 🟡 `0xFFAB00` 1.10–1.35 / 🔴 `0xD50000` ≥ 1.35 / 🔵 `0x1565C0` no data. The colour MUST come from the **same** route response as the displayed duration (not a second fetch). `duration_in_traffic` is often absent before ~7am — accept normal duration then (`references/traffic-duration-troubleshooting.md`).

##### Environmental Data

Load `sg-singapore-environment` skill for weather, rainfall, PM2.5, and flood alerts via `api-open.data.gov.sg/v2/real-time/api/`.

**⚠️ All v2 API responses are wrapped in a `data` key** — access via `response["data"]["records"]` or `response["data"]["items"]`, NOT a flat object.

**⚠️ Fetch environmental data using `subprocess.run()` — NOT `&` background in `execute_code`:** Background `&` in a foreground `execute_code` cell raises "Foreground command uses '&' backgrounding. Use terminal(background=true)...". Use `subprocess.run(["curl", "-s", "url"], capture_output=True, text=True)` sequentially in a loop. (Verified 2026-05-14.)

**⚠️ Parse with corrected field paths — the spec/skills docs are wrong for several endpoints.** See `references/weather-api-response-shapes.md` for verified shapes of twenty-four-hr-forecast, two-hr-forecast, pm25, and rainfall v2 APIs.

**⚠️ data.gov.sg RATE LIMIT — always send the API key (added v1.9.1).** `api-open.data.gov.sg` allows only **~5 requests per ~10s per IP** anonymously, then returns **HTTP 429** `{"code":24,"name":"TOO_MANY_REQUESTS","data":null,...}`. The morning report fires 5 calls (24hr + 2hr + rainfall + flood + pm25), so it sits right at the limit — a single retry or a second back-to-back run tips it over, and a 429 looks identical to an empty payload (`data:null`), surfacing as `*Air Quality unavailable*` (or any section "unavailable"). **Fix:** pass the data.gov.sg key as the `x-api-key` header (lifts the limit to ~12+/window). The key lives in `~/.hermes/.env` as `DATAGOV_API_KEY`; `morning_commute.py` / `evening_weather.py` read it and send it automatically (and back off ~10s on an explicit 429). If you see "Air Quality / weather unavailable," **check that `DATAGOV_API_KEY` is set and being sent** before suspecting the parser.

**Fetch all environmental data in parallel (use terminal or subprocess):**
```bash
KEY=$(grep '^DATAGOV_API_KEY=' ~/.hermes/.env | cut -d= -f2-)   # lifts the ~5-req/10s anon limit
curl -s -H "x-api-key: $KEY" "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast" &
curl -s -H "x-api-key: $KEY" "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" &
curl -s -H "x-api-key: $KEY" "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts" &
curl -s -H "x-api-key: $KEY" "https://api-open.data.gov.sg/v2/real-time/api/pm25" &
wait
```

##### Daily Morning Commute Report

See `references/commute-report.md` for the full workflow. Key points:

**Destination by day:**
- Tuesday → Innovis
- Wednesday → AMK
- Thursday → Innovis

**Report format** (5 sections):
```
## ☀️ Morning Commute Update

**1. Today's Weather** - Forecast: <forecast>, Temp: <low>–<high>°C, Humidity: <low>–<high>%, Wind: <dir> <low>–<high> km/h
• <period label>: <period text> <emoji>   ← one line per 24hr period; each label is derived
• <period label>: <period text> <emoji>      from that period's OWN `timePeriod` (the periods
• <period label>: <period text> <emoji>      slide with the clock, so labels are NOT hardcoded —
                                              at the 07:25 run: 6am–Midday / Midday–6pm / 6pm–6am (tonight))

**2. Next 2 Hours Weather** *(Pasir Ris — Home)*
• Valid period: <HHMM start> – <HHMM end> SGT
• Pasir Ris: <forecast> <emoji>

**3. Rain Check near <X>** - *S71 - Kent Ridge Rd (1.1km away): 0.0mm - no rain ☁️*
  (Station mapping: Tue/Thu→S71 Innovis, Wed→S109 AMK)

**4. Air Quality** - East region: <value> µg/m³ (<level> <emoji>), Regional avg: <avg> µg/m³ (<level> <emoji>)
• WHO PM2.5 scale: 🟢 0–9 Good / 🟡 10–19 Moderate / 🔴 20–49 Unhealthy (Sensitive)

**5. Commute to <DEST>** *(<address>)*
• Distance: <X.X> km
• Normal: ~<YY> min
• Current: ~<ZZ> min (traffic-aware)

**Route Map:**
MEDIA:/tmp/route_<dest>.png
```

**⚠️ Air Quality classification — PM2.5 WHO scale (must be in cron prompt):**
```
0–9 µg/m³: Good 🟢
10–19 µg/m³: Moderate 🟡
20–49 µg/m³: Unhealthy (Sensitive) 🔴
50–99: Unhealthy
100+: Very Unhealthy
```
Always include this scale verbatim in the cron job prompt. The agent will otherwise use a wrong scale (e.g. 0–15 = Good) and mis-classify values like 11 as "Good" when it should be "Moderate 🟡".

**Fallback for unknown forecasts**: `"thunder"/"lightning"/"storm"` → 🌧️, `"heavy"`+rain/shower → 🌧️, `"shower"/"rain"/"drizzle"` → 🌦️, `"partly cloudy"` → ⛅, `"cloudy"/"overcast"` → ☁️, default ⛅ (`wx_emoji`; evening's `emoji_for` defaults to 🌙).

> **🔄 Execution model (updated 2026-05-31): both cron jobs now run as `no_agent` scripts — no LLM agent, and this skill is NOT loaded at runtime.**
> - Morning Commute Report (`b617d89da15b`) → `~/.hermes/scripts/morning_commute.py`
> - Evening Weather Forecast (`c967008ca121`) → `~/.hermes/scripts/evening_weather.py`
>
> The scripts live in **`~/.hermes/scripts/`** (the cron scripts dir, not this skill's `scripts/`). Each computes the report and prints it to stdout; the cron system auto-delivers that text — plus the `MEDIA:/tmp/route_<dest>.png` attachment for the commute report (`extract_media` strips the tag and attaches the file) — to **#alerts** via the job's `deliver: "discord:1499972245437550602"`. The scripts are now the source of truth for formatting and delivery. The `send_message` / `execute_code` / "the agent outputs…" guidance below applies **only to manual or ad-hoc runs**; the data-shape and format references (`commute-report.md`, `evening-weather-workflow.md`, `weather-api-response-shapes.md`) document the logic those scripts implement.
>
> **API keys are loaded from `~/.hermes/.env`, never hardcoded** (`GOOGLE_API_KEY`, `DATAGOV_API_KEY`, `LTA_DATAMALL_KEY`). The scripts parse `~/.hermes/.env` directly (the cron scheduler doesn't export it). Never paste a literal key into a script or doc.

**⚠️ Discord delivery for cron runs: `deliver` handles it, not the agent.** The morning commute cron `b617d89da15b` has `deliver: "discord:1499972245437550602"`. `morning_commute.py` prints the full report (including the `MEDIA:/tmp/route_<dest>.png` path) to stdout and exits — the cron system posts it to Discord automatically. No `execute_code` or `send_message` calls are involved.

**⚠️ Discord delivery for ad-hoc/manual runs: use `send_message` tool.** When invoking manually or when `deliver: "local"`, call `send_message(action="send", message="<report>\n\nMEDIA:/tmp/route.png", target="discord:1499972245437550602")`. Do NOT use `execute_code` + `requests` — the bot token is not accessible in the sandbox.

**⚠️ Discord delivery via agent-side API calls (deprecated for morning report):** Only use `execute_code` + `requests` for evening weather (`c967008ca121`, `deliver: "local"`) or other `deliver: "local"` workflows where `send_message` is unavailable. See `references/commute-report.md` for the full two-call pattern.

###### Evening Weather Forecast (10pm Mon/Tue/Wed)

See `references/evening-weather-workflow.md` for the full workflow. Key points:

- Cron job `c967008ca121`, schedule `0 22 * * 1,2,3`
- `deliver: "discord:1499972245437550602"` — cron system auto-delivers; do NOT call `send_message` or any Discord API
- Header: always `## 🌙 Evening Weather Update` — never ☀️ regardless of forecast
- Emoji mapping (priority order): `"light rain"` → 🌦️ (check BEFORE `"rain"`), `default → 🌙`
- After final output: terminate — no confirmation text, no HTTP status echo, no extra reasoning

**⚠️ `send_message` is NOT available in the Discord platform toolset.** Only platform-level actions exist: `search_members`, `fetch_messages`, `create_thread`. Do NOT use `send_message` in any Discord-context cron prompt — it will fail with `Unknown action: send_message`. Always use `deliver: "discord:channel"` for auto-delivery.

**Discord delivery — correct pattern:**

| Workflow | Method | Why |
|----------|--------|-----|
| Morning Commute Report | `deliver: "discord:1499972245437550602"` (cron auto-delivery) | Cron posts `morning_commute.py` stdout + MEDIA attachment directly |
| Evening Weather Forecast | `deliver: "discord:1499972245437550602"` (cron auto-delivery) | Same pattern — cron posts `evening_weather.py` stdout |

**⚠️ Double-delivery bug class — two distinct patterns:**

1. **`deliver=discord` + explicit `send_message`/`requests` call in the same session** → two copies. Fix: use `deliver: local` when the agent calls Discord directly.

2. **Manual re-send after cron auto-delivered** → two copies. Scenario: cron had `deliver: local` (silent failure), user fixes `deliver` to `discord:channel`, cron runs and auto-delivers, user then manually sends the same report → 3 total. **Prevention**: After investigating a missing report or changing `deliver`, always check `~/.hermes/cron/output/<job_id>/` for a fresh timestamp before manually re-sending.

**⚠️ Silent delivery failure (May 14+): `deliver=discord` but no Discord message appears.** Scheduler logs "delivered via live adapter" but `gateway.log` has no "Sending response" entry for the channel. This is NOT a `deliver: local` issue — the setting is correct. Diagnosis: check both `agent.log` (scheduler finished) AND `gateway.log` (actual Discord send). If gateway.log is empty → manual re-delivery via `send_message` tool. Full diagnosis in `references/silent-delivery-failure.md`.

**⚠️ Triggering the job runs it on the next tick — but the morning script is SILENT on a non-commute day.** A manual trigger (`trigger_job` / "run now") sets `next_run_at=now` and the scheduler runs the script on its next tick — this DOES update `last_run_at` and write an output doc (the older "`cronjob action='run'` is a pure no-op" claim is outdated). **However**, `morning_commute.py` only produces a report Tue/Wed/Thu (its `DESTS` weekdays); triggered on any other day with **no** destination override it returns silently → empty stdout → the scheduler records `Status: silent (empty output)` and delivers **nothing**. That silent run is not a failure — it's the non-commute-day path. To verify the morning report **off-schedule**, force a destination: `~/.hermes/scripts/morning_commute.py innovis` (or `amk`), or `DEST=innovis …`. (Confirmed 2026-06-12: a Friday trigger ran the script, stayed silent, `last_error=None`.)

**⚠️ CRITICAL: Always use `curl` for data.gov.sg APIs** — `urllib.request` silently returns HTTP 403. In `execute_code`, use `subprocess.run(["curl", "-s", "url"], capture_output=True, text=True)` — it runs synchronously unlike `&` background.

**⚠️ CRITICAL: Always run with `PYTHONUNBUFFERED=1`**
Bare `print()` without `flush=True` causes LTA fetch output to be delayed — the script actually completes fine (confirmed 518073, 238879 tests) but appears as a false hang without this env var.

**⚠️ CRITICAL: Never patch skill reference files for runtime output text**
Skill reference files (everything under `references/` and `SKILL.md` itself) are documentation — they describe workflows, not execution artifacts. If something goes wrong in a cron run (e.g., unwanted text in output, wrong format), **do not patch the skill file** to fix it. The issue is in the cron prompt (`jobs.json`) or the execution output log, not the skill reference.
- "Now let me compile" preamble text → fix the cron prompt in `jobs.json`, not `commute-report.md`
- Wrong emoji in output → fix the cron prompt directive, not the skill
- Skill reference files are read-only documentation for this skill

**⚠️ Editing `jobs.json` — always use Python (`json.load` → edit → `json.dump`), never `patch`** (it inserts raw `0x0a` bytes instead of `\n` escapes, silently corrupting multi-line JSON strings). Full snippet in `CLAUDE.md` → *Editing jobs.json*.

##### Carpark Availability

See `references/carpark-availability.md` for full details — five separate systems (HDB/LTA/JTC/URA/EV) with different APIs, coverage, and coordinate systems, **plus the end-to-end pipeline, output format, and verified test results/runtimes** (the parking-report flow lives there, not inline, to keep this file lean).

**Quick reference:**

| System | Coverage | Live? | Coordinates | Key Field |
|--------|----------|-------|-------------|-----------|
| HDB | data.gov.sg | ✅ | SVY21 (x/y) | `carpark_number` |
| LTA | DataMall | ✅ | WGS84 (`"lat lon"` string) | `CarParkID` + `Development` |
| JTC | manual CSV | ❌ | SVY21 | policy info only |
| URA | data.gov.sg GeoJSON | ❌ | WGS84 (lon/lat) | lot counts only |
| EV | LTA DataMall postal-code API | ✅ | WGS84 (lat/lon) | postal code search |

**⚠️ Car lots only — all systems filter to car parking:** HDB filters `lot_type == "C"`, LTA filters `LotType == "C"` (skipping H heavy-vehicle and Y motorcycle rows), URA skips rows where `no_car == 0`. JTC is car-only by nature. Never sum or rank non-car lots.

**⚠️ 1 km display radius cap:** parking and EV results are capped at `MAX_RADIUS_M = 1000` m from the destination — all five `nearest_*` helpers filter by distance *before* ranking, so a far-away lot/charger can never fill a top-3 slot (this fixed an EV "Spot C" that showed up 4.4 km away for postal 569088). When a section has fewer than 3 results within range it shows a note under the header — `*No other parking/EV charging within 1km radius.*` (or `*No …*` when zero, in which case the otherwise-empty section is still emitted) — so a short/empty table never reads as a data glitch. (v1.10.0.)

**Never use `urllib.request`** for HDB carpark availability, NEA rainfall, or OneMap geocoding — all silently return 403. Use `curl`.

**Geocoding**: OneMap API (free, no token) for postal codes → SVY21. Google Geocoding API for street addresses. Never auto-derive postals — ask user if geocoding returns no postal code.

##### Changelog & Lessons

The full dated history of every bug, fix, and design change (8+ entries, 2026-05-20 →) lives in **`references/changelog.md`** — read it when debugging a recurring issue, understanding *why* something is the way it is, or before changing route / waypoint / delivery / EV-scan logic. (Moved out of this file in v1.9.2 to keep the always-loaded skill lean.)

**Still-live gotchas distilled (everything else is history in the changelog):**
- **Test images in #playground first** (`1501742338639401071`) — Discord re-encodes uploads; visual confirmation there is the only reliable check (PNG > 500 KB is a proxy, not proof).
- **Static maps: `format=png32`** — PNG8 silently drops the polyline in Discord. (See Route Computation.)
- **Route: Innovis forced onto ECP→MCE via two mainline `via:` points (`INNOVIS_VIA`), AMK direct.** A single via detours off the expressway (U-turn); a `route_has_detour()` guard falls back to the no-waypoint route if the forced route weaves. Distance guards: Innovis > 25 km (~35 expected), AMK > 8 km. (changelog 2026-07-02; coords via `verify_ecp_route.py`.)
- **data.gov.sg needs the `x-api-key` key** (see Environmental Data) — "section unavailable" is usually a **429 rate-limit**, not a parser bug; a `data:null` 429 body is indistinguishable from empty unless you check status.
- **Editing `~/.hermes/cron/jobs.json`:** use Python `json.load()`/`json.dump()`, never the `patch` tool (it corrupts multi-line JSON).
- **Never patch skill files to fix runtime *output* text** — that belongs in the cron prompt (`jobs.json`), not `references/` or `SKILL.md`.
- **Discord delivery:** `--send` / curl-multipart for atomic text+image (never `send_message` with `MEDIA:` tags — `extract_media()` reorders). Cron uses `deliver:`; don't also send manually (double-delivery).
- **Fan-out hits one rate-limited host:** parallelizing same-host API calls can raise the 429/5xx rate — key it, stagger, or back off.

</details>

<details><summary>commute-report.md — agent-mode "Discord Delivery", cleanup and pipeline sections</summary>

##### Discord Delivery

###### Cron delivery (Option B — default since 2026-05-14)

The morning commute cron `b617d89da15b` has `deliver: "discord:1499972245437550602"`. The cron system handles posting the agent's full text output directly to Discord — **no agent-side Discord API calls needed**. Agent produces the report and terminates.

**What the agent should output** — the final response IS the report:
```
**☀️ Morning Commute Update** — <date>
[5 sections]
MEDIA:/tmp/route_<dest>.png
```

The `MEDIA:` path will be resolved and attached by the cron delivery system.

###### Ad-hoc / manual runs (`deliver: "local"`)

When `deliver` is `"local"` or for any manual invocation, use `send_message` tool directly — **do NOT use `execute_code` + `requests`** (the bot token is not accessible in the sandbox). `send_message` handles both text and image attachment in one call.

```python
# Pattern for send_message with image:
target = "discord:1499972245437550602"   # main channel
# or for thread:
target = "discord:1499972245437550602:1504250855421902988"

send_message(action="send", message="<report text>\n\nMEDIA:/tmp/route_innovis.png", target=target)
```

**⚠️ Do NOT output MEDIA: paths as raw text** — always pass as attachment in `send_message`. Raw `MEDIA:` text is not rendered.

###### Legacy `execute_code` + `requests` pattern (pre-2026-05-14)

The two-call `execute_code` pattern below is **deprecated for morning commute report** but remains valid for evening weather or any `deliver: "local"` workflow where `send_message` is unavailable.

```python
import requests, os
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ[k] = v
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = "1499972245437550602"

# 1. Text message with embeds:[] to suppress URL auto-embed
requests.post(
    f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
    headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json",
             "User-Agent": "DiscordBot (hermes-agent, 1.0)"},
    json={"content": text_report, "embeds": []}
)

# 2. Image as multipart with neutral emoji caption
with open("/tmp/route_map.png", "rb") as f:
    requests.post(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        data={"content": "🗺️"},
        files={"file": ("route_map.png", f, "image/png")}
    )
```

**⚠️ Thread posting**: Use thread ID as `channel_id` in the URL (not `message_reference`):
```python
THREAD_ID = "1501743328125911070"
requests.post(f"https://discord.com/api/v10/channels/{THREAD_ID}/messages", ...)
```

##### Cleanup

Always run cleanup before building report:
```bash
rm -f /tmp/route_*.json /tmp/route_innovis.png /tmp/route_amk.png /tmp/route_map.png /tmp/rainfall.json
```

##### Full Script Pipeline

```bash
# Step 0: Cleanup
rm -f /tmp/route_*.json /tmp/route_innovis.png /tmp/route_amk.png /tmp/route_map.png /tmp/rainfall.json

# Step 1: Fetch all SG environmental data in parallel
curl -s "https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast" -o /tmp/24hr.json &
curl -s "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast" -o /tmp/2hr.json &
curl -s "https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts" -o /tmp/flood.json &
curl -s "https://api-open.data.gov.sg/v2/real-time/api/pm25" -o /tmp/pm25.json &
curl -s -f "https://api-open.data.gov.sg/v2/real-time/api/rainfall" -o /tmp/rainfall.json &
wait

# Step 2: Compute route (Python — two calls: normal + traffic-aware)
# See google-routes.md for full Python code

# Step 3: Build report and send via execute_code (two calls)
```

</details>

<details><summary>discord-image-delivery.md before the 2026-09-29 rewrite</summary>

#### Discord Image Delivery — Atomic Header+Image Pairing

**Class of problem:** Sending reports (carpark, weather, route, etc.) to Discord where each section has its own image (PNG table, EV charging, static map) that must arrive in the **same Discord message** as its header text.

**⚠️ The `extract_media()` gotcha (gateway-level, NOT cron-only):**
`send_message` cannot reliably pair text with images. The gateway's `extract_media()` (in `gateway/platforms/base.py` lines 1888–1943) scans the **entire** reply string for `MEDIA:<path>` tags, sends all text in one batch, then sends all images as a separate batch. Order is lost. The `---` separator pattern does NOT work — re-batching happens regardless of how the response is structured.

**The ONLY reliable pattern: programmatic curl multipart** where `content` and `file` are in ONE HTTP request per section. The Discord API attaches the file to the same message as the content text, atomically.

##### Implementation in `nearest_carpark.py`

The script has a `--send <channel_id>` mode that does this end-to-end. Three pieces:

###### 1. Bot token from `.env`

```python
def _load_token():
    env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(env_path):
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1]
    return None
```

Never hardcode the token, never log it, never print it.

###### 2. Curl multipart send

```python
def _discord_send_pair(token, channel_id, content, img_path=None):
    """Send ONE Discord message with content + file in the SAME request.
    If img_path is None, sends text-only. This is atomic — Discord receives
    one message with the image as a native attachment."""
    cmd = [
        "curl", "-s", "-X", "POST",
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        "-H", f"Authorization: Bot {token}",
        "-F", f"content={content}",
    ]
    if img_path and os.path.exists(img_path):
        cmd.append(f"-F")
        cmd.append(f"file=@{img_path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return False
    # Empty stdout from Discord = 2xx success; non-2xx returns JSON with "code"
    if r.stdout and ('"code"' in r.stdout or '"message"' in r.stdout):
        return False
    return True
```

**Why `-F content=...` and not `content@<file>`:** Multiline content (newlines, bold markers) needs careful handling. For single-line content (a header + summary), inline `-F` is fine. For multi-line content with embedded newlines, write to a temp file and use `-F content@<file>` instead — the `token-usage` skill's `references/token-report-delivery-log.md` documents the literal-`\n` bug if you pass newlines inline.

###### 3. The `[SILENT]` cron pattern

For `no_agent: true` cron jobs: print `[SILENT]` as the **very first line** of stdout. The scheduler captures all stdout as the cron delivery — even `print("Done")` becomes a Discord message. `[SILENT]` suppresses delivery while still allowing the script to print to stderr (logs) for debugging.

```python
# At the top of --send mode, before sending anything:
print(f"[SILENT]\nSent {sent}/{len(sections)} sections to channel {SEND_CHANNEL}",
      file=sys.stderr)
sys.exit(0)
```

`[SILENT]` goes to stderr, NOT stdout — the cron system only auto-delivers stdout.

##### Section list pattern (multi-message slice)

Build a `sections` list of `(header, body, image_path_or_none)` tuples. The `body` field is `None` for table sections (the image IS the body — the PNG speaks for itself) and is the **resolved address string** for the LOCATION section. Empty-table sections are skipped entirely (the `discord-table-format` renderer maps empty tables to `null`).

```python
sections = []
if park_img:
    sections.append(("🅿️ PARKING", None, park_img))
elif park_rows:
    sections.append(("🅿️ PARKING", None, None))  # rare — section header only

if ev_img:
    sections.append(("⚡ EV CHARGING", None, ev_img))
elif ev_rows:
    sections.append(("⚡ EV CHARGING", None, None))

# LOCATION section ALWAYS carries the resolved address as body text.
# Do NOT drop the body — the user expects "200 NETHERAVON ROAD SINGAPORE 508529"
# as the Discord message line under the 📍 pin. (Regression observed 2026-06-01:
# collapsing the tuple to 2-tuple lost the address line; user caught it within
# one round-trip.)
sections.append(("📍 LOCATION", loc["address"], "/tmp/carpark_map.png"))
```

Then loop:

```python
for header, body, img in sections:
    content = f"**{header}**"
    if body:
        content += f"\n{body}"
    _discord_send_pair(token, channel_id, content, img)
    time.sleep(0.5)  # Discord rate-limit headroom (5 req/sec/channel)
```

The `time.sleep(0.5)` is required — Discord's rate limit is 5 messages per 5 seconds per channel. Below 200ms intervals can trigger 429s under burst conditions.

**⚠️ Do NOT add `Total parking: N` / `Total EV: N` summary lines** — user explicitly removed them (2026-06-01). The PNG table is self-describing (header row has `# / Dist / Avail / Live / Code / System / Address`); redundant totals are noise.

**⚠️ Do NOT collapse the 3-tuple to 2-tuple by dropping `body`** — the LOCATION section depends on `body` being the address string. If you remove the field, the static map arrives without the address text and the user has to look it up from another source. If a future change drops the `body` field, restore it as `loc["address"]` for LOCATION and `None` for table sections.

**⚠️ "Remove X" requests — the field may carry more than X (June 2026 regression):** When the user asks to remove specific text (e.g. "remove the `Total parking: 3` / `Total EV: 1` verbiage"), do not blindly drop the field that holds it. The `body` field originally held both the count summary AND the LOCATION address. A naive `body` removal cleaned up the totals but silently dropped the address. **Process:** (a) grep for the exact phrase the user named, (b) inspect every other consumer of the same field, (c) keep the field but clear only the offending content (e.g. `body = None` for table sections, `body = loc["address"]` for LOCATION), (d) run the script after the edit and verify the LOCATION section still prints the address — this is a one-line sanity check that catches field-level regressions in seconds.

##### Cleanup pattern

After delivery, delete generated table PNGs. Keep the static map (small, may be reused).

```python
for header, body, img in sections:
    if img and img != "/tmp/carpark_map.png" and os.path.exists(img):
        try:
            os.remove(img)
        except OSError:
            pass  # best effort
```

Temp images accumulate fast — each ~30-90KB, and `discord-table-format` uses millisecond timestamps so duplicates pile up under burst conditions.

##### When to use which path

| Context | Pattern | Why |
|---|---|---|
| Cron `no_agent: true` | `--send <channel>` flag in script | Script self-delivers, `[SILENT]` suppresses auto-delivery |
| Agent inside a Discord thread | `--send <channel> --thread <thread_id>` | Posts land in the thread (default would go to parent channel — see June 2026 thread-routing note below) |
| Interactive agent (this session) | Agent reads script output, uses `send_message` ONLY for text-only messages, uses curl multipart for image-bearing messages | `send_message` is fine for text-only; never for image+text |
| Manual / ad-hoc CLI | Default terminal output (sections with `---` separators) | Human reads it; copy-paste headers for manual send |

##### Threads vs. channels — `--thread` flag (added 2026-06-06)

Discord's v10 API treats threads as channels — the message-endpoint URL is the same
`https://discord.com/api/v10/channels/{id}/messages`, only the ID changes. Any channel
ID you have for a guild channel works for a thread, and vice versa.

`nearest_carpark.py` accepts `--thread <thread_id>` (companion to `--send`) to route
the POSTs to a thread. When the user is in a thread and asks for a report, **always
pass `--thread` with the thread ID** — defaulting to the parent channel means the
report lands in the wrong place (parent channel) instead of the conversation the user
is reading. This bit the carpark skill on 2026-06-06: agent ran
`--send 1499972245437550602` from inside a thread, report went to #alerts (parent)
instead of the thread the user was reading. The fix was the `--thread` flag; the
lesson is the foot-gun.

**Summary line distinction:** When `--thread` is set, the script prints
`Sent N/N messages to thread <id> (parent channel <id>)` instead of the bare
`Sent N/N messages to channel <id>`, so the agent (and user) can see at a glance
where the report landed.

##### Single source of truth for Discord text (added 2026-06-06)

If a script has two output paths — `--send` (posts to Discord) and default terminal
output (prints to stdout for the agent to read) — they must produce **byte-identical
section text** for the same (header, body) tuple. Don't add a `--- {header} ---`
decoration to one path and not the other.

**The foot-gun:** terminal output is naturally decorated for human readability
(`--- header ---`, blank lines, `MEDIA:` tags). An agent reading the terminal
output and then posting to Discord via its own curl loop will grab the *decoration*
verbatim unless the script's actual posted text and the terminal-printed text
are the same string. On 2026-06-06 this posted `--- ⚡ EV CHARGING ---` to Discord
when the user expected `**⚡ EV CHARGING**`. The script's `--send` mode was
correct; the agent's ad-hoc delivery loop was wrong; the bug surfaced as a
user-visible format violation.

**The fix pattern:** centralise the format. Add a `format_section_content(header, body)`
helper and call it from BOTH the `--send` loop and the terminal-output loop.
Terminal output should still be human-readable, but the *actual content string*
for each section is identical to what gets posted. Prefix the terminal output
with a clearly-marked debug delimiter (e.g. `--- section: {header} ---`) that
an agent cannot mistake for the content itself.

```python
def format_section_content(header, body):
    if body:
        return f"**{header}**\n{body}"
    return f"**{header}**"

# --send path
for header, body, img in sections:
    _discord_send_pair(token, channel, format_section_content(header, body), img)

# terminal path — decoration is clearly separated from the canonical content
for header, body, img in sections:
    print(f"--- section: {header} ---")        # debug marker
    print(format_section_content(header, body)) # IDENTICAL to what --send posts
    if img: print(f"MEDIA:{img}")
```

**Rule of thumb:** if an agent could ever copy text from the terminal output
and paste it into a Discord message, the terminal-printed text must be the
same string the script would post itself.

**Cross-reference:** The same class-level lesson (single source of truth for
Discord text) is documented in the `discord-messaging` skill at
`~/.hermes/skills/social-media/discord-messaging/references/script-delivery-patterns.md`,
along with the multipart text+image pattern and the thread-routing foot-gun.
That skill is the general Discord API reference; this one is the sg-commute
specific implementation. The two reinforce each other.

**⚠️ Agent + `send_message` anti-pattern (Nov 2026):** Earlier in this session, the agent sent a table image via plain `send_message` with `MEDIA:` inline. The gateway batched the image after all text. The fix was the `--send` mode + curl multipart. **Do not rely on `send_message` for image delivery, ever.** Even one section at a time risks reordering under load.

</details>
