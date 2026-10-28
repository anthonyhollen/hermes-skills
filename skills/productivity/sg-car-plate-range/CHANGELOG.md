# Changelog — sg-car-plate-range

## 1.2.0 — 2026-06-11 (scraper provider swap)

Switched the scrape transport from Scrapeninja (apiroad) to **ScraperAPI** at the
user's request; the apiroad key had hit "Quota exhausted".

- **Fetch layer:** replaced the `scrapeninja` client with a `urllib` GET to
  `https://api.scraperapi.com/?api_key=…&render=true&url=<LTA page>` (params before
  `url` per the docs; `render=true` runs a real browser for the Angular SPA).
- **Key:** now `SCRAPERAPI_API_KEY` in `~/.hermes/.env` (auth via the `api_key`
  query param). `SCRAPENINJA_API_KEY` is no longer used by this script.
- **Retry/timeout:** one retry on a transient 429/5xx/transport error; each attempt
  bounded at 100s to stay under the 120s cron watchdog. A 4xx (≠429) fails fast.
- **Unchanged:** all parsing, Car-only scoping, `id==text` tile detection, the
  page-structure sanity check, and the exit-code/delivery contract — the parser is
  transport-agnostic and was re-validated byte-for-byte against the ScraperAPI body.
- **Validated:** live run returned Car-only `SPH / SPJ / SPK`, exit 0, ~22s.

## 1.1.0 — 2026-06-11 (tune-up)

Full tune-up via hermes-skill-tuneup (bug hunt + fan-out, perf, delivery,
terminal gate, docs). Live copy: `~/.hermes/scripts/lta_car_prefix.py`.

### Fixed (code)
- **Section-veto silent data drop (HIGH).** The old parser skipped a vehicle-type
  section if `"error-text"`/`"not available"` appeared anywhere in it, but the
  section regex had no end-anchor so each section absorbed sibling divs and the
  last section absorbed ~30 KB of trailing page JS. Real prefixes were silently
  dropped (reproduced: a stray `error-text` div after the Car section dropped
  `SPK`; a prefix in the last vehicle type dropped by trailing JS). Fix: identify
  prefix tiles by `id == text` + `[A-Z]{1,4}`; **button presence is authoritative**
  — no error-text veto.
- **No-bid week reported as a failure (HIGH).** Empty result did `sys.exit(1)`,
  which the scheduler delivers as "⚠️ Cron job failed". A week with nothing open
  is normal. Fix: friendly message + `exit 0`. A genuine block/empty/changed page
  now raises (`exit 1`/alert) via a page-structure sanity check.
- **All vehicle types mislabeled as car plates (HIGH).** The parser merged prefixes
  from all 8 types under the "Car Plate" banner (caught live: it would have reported
  motorcycle `FBZ`, bus `PD`, trailer `TRE` as car plates and dropped `XF`). Fix:
  resolve the "Car" type from the selector tiles and filter to it; if "Car" can't be
  resolved, raise instead of guessing.
- **Hardcoded Scrapeninja API key (HIGH).** The apiroad key was hardcoded in the
  script and duplicated in SKILL.md. Fix: load `SCRAPENINJA_API_KEY` from
  `~/.hermes/.env` via a `_load_env_key` fallback (env vars aren't reliably exported
  to no_agent scripts). **The key was committed — rotate it at apiroad.**
- **Brittle prefix regex (MED).** `[A-Z]{2,3}` + exact `class="btn btn-yellow">`
  dropped 1/4-letter prefixes and was attribute-order/quote fragile, and leaked
  stray `FAQ`/`PDF` yellow buttons. Fix: attribute-tolerant anchor match + `id==text`.
- **No retry (MED).** A single transient blip on the weekly run meant no data + an
  alert for 7 days. Fix: one retry on transient transport/5xx/429, bounded under the
  120 s scheduler watchdog (httpx timeout 50 s).
- **Naive `datetime.now()` (LOW).** Now explicit `ZoneInfo("Asia/Singapore")`.
- **Terminal-gate regressions (caught + fixed before ship):** `_car_vid` fell back to
  `"1"` (could deliver wrong-vehicle data) → now raises; the structure-sanity regex and
  the section-split regex disagreed on attribute-bearing section divs (silent false
  no-bid) → unified to one attr-tolerant pattern (`id="vehicleType_\d+"[^>]*>`).

### Changed (config / docs)
- Cleared the stale `prompt` on cron job `f77c2c161a24` (it described the old
  browser/VRL flow — dead config + agent-mode relapse risk on a `no_agent` job).
- Retired the dead `lta-plate-reminder.sh` (no longer referenced by the cron).
- SKILL.md rewritten to document the actual current script/flow and slimmed
  (~2690 → ~1040 tokens) by relocating the verbose CDP/browser fallback to
  `references/browser-fallback.md`. Added `version`. Scrubbed the API key.

### Validation
- Parser + exit codes + retry state machine proven on synthetic data and the real
  page (offline where the API quota was exhausted mid-tune-up). Live run before the
  quota ran out returned Car-only `['SPH','SPJ','SPK']` (exit 0), confirming the
  Car-only filter against a page that had just opened prefixes for every type.
- Two independent review fan-outs (bug hunt + re-entrant terminal gate); the gate
  caught the two regressions above before shipping.
