---
name: sg-car-plate-range
description: Check the current Singapore CAR plate prefixes open for bidding on LTA's OneMotoring portal
version: 1.2.0
triggers:
  - check car plate range
  - LTA plate range
  - Singapore car plate bidding
---

# SG Car Plate Range Checker

Reports the Singapore **car** plate prefixes currently open for bidding, scraped
from LTA's OneMotoring VRN page. Runs as a weekly no-LLM cron that posts to Discord.

## Preferred: ScraperAPI script (no reCAPTCHA)

The OneMotoring VRN info page embeds the prefix data in its HTML (no reCAPTCHA).
ScraperAPI's rendering engine renders the Angular SPA; the script extracts the **Car**
vehicle-type's prefix tiles.

**Script:** `~/.hermes/scripts/lta_car_prefix.py` — run it directly:
```bash
python3 ~/.hermes/scripts/lta_car_prefix.py
```

How it works:
1. A GET to `https://api.scraperapi.com/?api_key=…&render=true&url=<LTA page>` renders the
   page in a real browser (~20–40s). ScraperAPI params come **before** `url`. One retry on a
   transient 429/5xx/transport error (a weekly job gets no second chance for 7 days); each
   attempt is bounded at 100s to stay under the cron's 120s watchdog.
2. It reads the vehicle-type **selector** tiles to find the id for "Car", then extracts
   that section's **prefix tiles** — an `<a class="btn btn-yellow">` whose `id` equals its
   text and matches `[A-Z]{1,4}`. (`id == text` rejects the type-selector and stray FAQ/PDF
   buttons; button presence is authoritative — it does **not** veto on incidental
   "error-text"/"not available" copy, which leaks in from sibling divs and trailing JS.)
3. Output (stdout, delivered verbatim by the cron):
   ```
   ## 🚗 Car Plate Bidding Check - DD-MMM-YYYY
   ### Available car plate prefix for [bidding](<…>):
   1. **SPK**
   ```

**Exit codes drive delivery** (the scheduler turns a non-zero exit into a "⚠️ Cron job
failed" alert):
- **0 + message** — prefixes found, OR a benign no-bid week (`No car plate prefixes are
  open for bidding this week.`). A no-bid week is normal, not a failure.
- **1 + stderr** — a real failure worth alerting: scrape error/quota, or the page
  structure / "Car" type could not be found (possible block or LTA page change).

**API key:** `SCRAPERAPI_API_KEY` in `~/.hermes/.env` (endpoint `api.scraperapi.com`, auth
via the `api_key` query param). The script reads it from the env / `.env` — never hardcode it.
ScraperAPI handles proxies, CAPTCHAs, and bot protection internally; `render=true` runs a real
browser. If a run reports an HTTP failure (e.g. 403 with "credits") top up the ScraperAPI plan.

## Cron reminder

A weekly script-only cron (no LLM) already exists — job `f77c2c161a24`, `0 20 * * 5`
(Fri 20:00 SGT), `--no-agent`, delivering to `discord:1499972245437550602`. Its stdout
is sent straight to Discord. To recreate or edit:
```bash
hermes cron edit f77c2c161a24 --script lta_car_prefix.py --no-agent \
  --deliver "discord:1499972245437550602" --schedule "0 20 * * 5"
```
Keep the job's `prompt` empty (it's `no_agent`; a stale prompt is dead config and would
reload as agent context if `no_agent` were ever turned off).

## Reference Files

- `references/plate-ranges.md` — historical plate-range data + the prefix-in-range comparison rule.
- `references/captcha-solver-pricing.md` — CAPTCHA-solver pricing (2Captcha / CapSolver / Anti-Captcha); no free tiers, CapSolver cheapest at ~$0.80/1k for reCAPTCHA v2.
- `references/browser-fallback.md` — the **non-preferred** CDP/manual-reCAPTCHA browser flow, only needed for the VRL app page's full *numeric* range (e.g. "SPH918X to SPH1631R"). Read it only if the prefix list isn't enough.

## Pitfalls

- **The VRL app page (`vrl.lta.gov.sg`) requires reCAPTCHA; the OneMotoring info page does
  not.** Use the script (OneMotoring) for prefix checks. The VRL numeric range needs the
  browser fallback — see `references/browser-fallback.md`.
- **Scope is Car only.** The page lists prefixes for all 8 vehicle types (Car, Motorcycles,
  Buses, HGVs…); the script filters to "Car" so a motorcycle/bus prefix is never mislabeled
  as a car plate. If you want another type, change the type filter in the script.
- **Discord embed suppression:** wrap link URLs in angle brackets — `[text](<https://…>)` —
  so Discord doesn't generate a preview/thumbnail.
- **Sister skill:** `warframe-scraper` follows the same ScraperAPI pattern (same API key,
  same `_load_env_key` helper, same retry logic, same param ordering). If you enhance the
  ScraperAPI helper here, port it there too.
