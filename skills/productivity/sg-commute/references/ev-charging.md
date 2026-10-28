> **Failure handling + cache safety (v1.11.0, 2026-09-29):** `ev_by_postal()` returns `None` when the fetch fails (vs `[]` = no charger at that postal), so a scan now counts its failures; a discovery scan with >20 % failed probes does **not** record an anchor (the next query re-scans) and the report says EV data is unavailable instead of "No EV charging within 1km" — previously an API outage on a first query cached the area as covered-with-nothing for up to a month. A bay `status` other than `"1"`/`"0"` (e.g. `""`, seen live) makes the location `?/N`. Cache writes are atomic and the read-merge-write happens under an `flock` on `data/.ev_cache.lock` with a re-read inside the lock (`_commit_ev`), so the monthly `--refresh` can no longer overwrite a concurrent query's new finds; `--refresh` logs a start timestamp and exits 1 if any anchor scan was unreliable.

# LTA EV Charging Points

> **⚠️ 2026-06 — ARCHITECTURE CHANGE: cache-first lookup. Read this first.**
>
> Verified facts that drove the change:
> - The endpoint is **NOT a radius/proximity search** — it is an **exact-building lookup**. A postal returns only the charger(s) *at that exact postal* (measured 0–50m away). There is **no national dump** (no-param / `$skip` → `400 Invalid parameter for postal code`), and **OneMap revgeocode requires a token** we don't have.
> - Therefore guessing postals by arithmetic (the old `block±50` + "targeted sweep") was both wasteful (queried hundreds of charger-less/non-existent postals) and lossy (missed chargers whose postal differs by >50 blocks but sits next door).
>
> **New design (`nearest_carpark.py`):** charger *locations* are static, only per-bay availability is live, so:
> 1. **Cache** discovered locations in `data/ev_locations.csv`; record discovered areas in `data/ev_anchors.csv`.
> 2. **Query path** — if the target is within `ANCHOR_COVER_RADIUS` (1 km) of a prior anchor → serve from cache by haversine, **no live scan** (~1s). Otherwise run a one-time **narrow** discovery scan (2-digit `sector±1 × delivery-point±50`, ≈303 postals, ~1.4s on a keep-alive session — see the v1.9.0 note below), merge into the cache, record an anchor.
> 3. **Availability** for the ≤3 *displayed* chargers is always re-fetched live (`refresh_ev_live`), so cached availability never goes stale on screen.
> 4. **Monthly cron** `nearest_carpark.py --refresh` re-discovers every anchor area and merges new chargers (logs to `data/ev_refresh.log`). Cron: `0 4 1 * *`.
>
> Measured: repeat queries ~1.1–1.3s (was ~22s). The old always-on "targeted sweep" over fixed sectors 1–3 was removed (it scanned the wrong region for any non-central address and never changed a top-3 result).

> **⚠️ 2026-06-06 (v1.9.0) — EV scan rewritten: connection reuse + retry + correct sector decomposition.**
> - **Transport:** the scan no longer spawns one `curl` process per postal. It uses a **per-thread keep-alive `requests.Session`** (`_ev_session()` / `_ev_get()`), reused across each worker's ~25 sequential calls. Measured **~5.6× faster** on a 303-postal scan (6.7s → 1.2s). `requests` is safe here — this is LTA DataMall with an `AccountKey` header; the urllib-403 caveat is **data.gov.sg-specific** (HDB/OneMap/rainfall stay on curl).
> - **Reliability:** the EV API intermittently returns **HTTP 500 under concurrency**, which the old curl path silently swallowed as "no chargers" (curl ignores HTTP status). `_ev_get()` now **retries transient 5xx** with backoff, and the scan uses **12 workers** (not 64 — keep-alive makes high fan-out pointless and lower concurrency provokes fewer server 500s). A `--refresh` of 11 anchors recovered **24 chargers** prior scans had been losing.
> - **Correct decomposition:** the scan now splits the **2-digit postal sector** (`pi // 10000`). The previous `pi // 100000` used only the *first digit*, so for a sector-13 address (138634) it probed sectors 03/13/23 — scattered across the island, missing the true neighbours. (The HISTORICAL section at the bottom of this file still shows the old 1-digit model; it is wrong — see the note there.)

**Endpoint:** `https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode=<POSTAL>`

**Auth:** `AccountKey: ${LTA_DATAMALL_KEY}` (same as LTA CarParkAvailability)

**Method:** GET — **exact-postal lookup** (NOT a radius search): returns only the EV charging location(s) registered at that exact postal code (verified 0–50m from the queried postal's coordinates).

## Response Schema

```json
{
  "value": {
    "evLocationsData": [{
      "address": "2 FUSIONOPOLIS WAY SINGAPORE 138634",
      "name": "INNOVIS",
      "longitude": 103.788698,
      "latitude": 1.298426,
      "locationId": "788698138634",
      "status": "",
      "chargingPoints": [{
        "status": "1",
        "operatingHours": "",
        "operator": "COMFORTDELGRO ENGIE PTE. LTD.",
        "position": "B3 145",
        "name": "INNOVIS",
        "id": "",
        "plugTypes": [{
          "plugType": "Type 2",
          "powerRating": "AC",
          "chargingSpeed": "22",
          "price": "",
          "priceType": "",
          "evIds": [{ "id": "", "evCpId": "R102014C-002", "status": "" }]
        }]
      }]
    }]
  }
}
```

## Field Guide

| Field | Description |
|-------|-------------|
| `locationId` | Derived ID: `{lon_6dp}{postal}` (lon in 1/1000000 deg + postal) — e.g. `103.788698`+`138634` → `788698138634`. Lat is NOT in the ID. |
| `evIds[].status` | Bay availability — `"1"` = available, `"0"` = occupied. **This is the correct level — NOT `chargingPoints.status` or `plugTypes.status`.** |
| `chargingPoints[].position` | Bay/lot identifier (e.g. "B3 145") |
| `plugTypes[].chargingSpeed` | kW (e.g. "22" = 22kW) |
| `plugTypes[].powerRating` | "AC" or "DC" |
| `plugTypes[].plugType` | "Type 2", "CCS", "CHAdeMO", etc. |

## Bay Count Bug (Fixed May 2026)

**PROBLEM:** A single postal like 138634 returns MULTIPLE `chargingPoints` entries (e.g. 4) ALL with the SAME lat/lon. The old code created one output entry per CP, then deduped by lat/lon — keeping only 1 of 4 and showing wrong bay count (1 instead of 4).

**SOLUTION:** In `ev_by_postal()`, BEFORE the lat/lon dedup step, aggregate all `chargingPoints` at a single `evLocationsData` entry into ONE entry:
- `total_bays` = sum of `len(evIds)` across ALL `plugTypes` in ALL `chargingPoints` at this location (NOT `len(plugTypes)` — one plugType may have multiple evIds)
- `available` = count of `evIds.status == "1"` across ALL chargingPoints

**⚠️ Bug #2 (Fixed May 2026):** `total_bays` was counting `len(plugTypes)` — number of plug type entries — instead of the actual number of EV bay IDs. One plugType (e.g. "Combo 2") can have multiple `evIds` (bay units). Correct formula: `total_bays += sum(len(pt.get("evIds", [])) for pt in pts)`.

**Then** dedupe by (round(lat,5), round(lon,5)) and keep top-3 nearest. |

## Known EV Locations

| Postal | Name | Operator | Bays | Speed | Verified |
|--------|------|----------|------|-------|----------|
| 138634 | INNOVIS | ComfortDelGro Engie | 4 (B3 144-147) | Type 2 AC 22kW | ✅ live API |
| 151129 | BLK 129A Bukit Merah View MSCP | TotalEnergies | 1 (L2) | Type 2 AC 3.7kW | sample file |

## Verified Postal Codes

| Location | Postal | EV found? |
|----------|--------|-----------|
| Innovis | 138634 | ✅ 1 location |
| AMK | 569088 | ❌ |
| AMK alt | 560123 | ✅ 1 location |
| Pasir Ris | 518141 | ❌ |
| Bukit Merah | 151129 | ✅ (sample) |

## Quick Query

```bash
curl -s "https://datamall2.mytransport.sg/ltaodataservice/EVChargingPoints?PostalCode=138634" \
  -H "AccountKey: ${LTA_DATAMALL_KEY}" \
  -H "Accept: application/json"
```

**EV `evIds[].status` — VERIFIED meaning (May 2026):**
`status` lives inside `plugTypes[].evIds[0].status` — NOT at `chargingPoints.status` or `plugTypes.status`.
- `"1"` = bay available, `"0"` = bay occupied.
- Available bays = count of plugs across ALL chargingPoints at this location where `evIds[0].status == "1"`.

**Speed field:** `plugTypes[].chargingSpeed` is the kW rating (e.g. `"22"` = 22kW). The `powerRating` field is just "AC" or "DC".

**Key verified locations:**
- Innovis (postal 138634): 4 bays B3 144-147, Type 2 AC 22kW, ComfortDelGro Engie
- AMK alt (postal 560123): 1 location nearby
- Pasir Ris (postal 518141): no EV charging points found

## Multi-Postal Search (HISTORICAL — superseded 2026-06 by the cache-first design above)

> This section documents the original always-scan approach. It is retained for context, but the **discovery scan is now run only on a cache miss or the monthly `--refresh`**, and the narrow `sector±1 × delivery-point±50` net (no sectors-1-3 sweep) is the variant in use. Do not reintroduce per-query broad scanning — see the cache-first block at the top of this file.
>
> **⚠️ The postal-format model below is WRONG (fixed v1.9.0):** a Singapore postal is `[sector 2-digit][delivery-point 4-digit]`, NOT `[sector 1-digit][area 2-digit][block 3-digit]`. The `sector = postal // 100000` (first digit) decomposition shown here probes scattered sectors (e.g. 03/13/23 for a sector-13 address). The live code now uses `sector = pi // 10000` (the true 2-digit sector). Do not copy the snippet below.

The EV API returns locations only for the **exact postal code queried** — not a radius search. A single postal code returns 1-2 locations even in dense EV areas. The original strategy scanned nearby postals arithmetically:

Singapore postal format: `[sector 1-digit][area 2-digit][block 3-digit]`
- For postal `238879`: sector=2, area=38, block=879
- Scan: sector±1 × block±10 → e.g. sector 1-3 × block 869-889 ≈ 63 postals
- This works for both city (sector 0-2) and suburban (sector 3-9) areas

```python
def ev_by_postal_multi(postal):
    """Fetch EV points from multiple nearby postal codes and dedupe by location.
    Singapore postal: [sector 1-digit][area 2-digit][block 3-digit]
    Strategy: sector±1 × block±10 (≈63 postals, ~15-20s).
    """
    sector = postal // 100000       # e.g. 238879 → 2
    area   = (postal // 1000) % 100  # e.g. 238879 → 38
    block  = postal % 1000           # e.g. 238879 → 879

    seen = {}
    for sec in range(max(0, sector-1), min(10, sector+2)):
        for blk in range(max(0, block-10), min(1000, block+11)):
            p = sec * 100000 + area * 1000 + blk
            if p < 100000 or p > 999999:
                continue
            try:
                for e in ev_by_postal(p):
                    key = (round(e['lat'], 5), round(e['lon'], 5))
                    if key not in seen:
                        seen[key] = e
            except Exception:
                pass
    return list(seen.values())
```

**Wrong approach (do NOT use):** `prefix[:3] + suffix` where suffix is `870-899` — this generates 4-digit suffixes like `2388870` which are invalid Singapore postals (suffix must be 3 digits). The `prefix±delta × suffix-870-899` strategy was tested and produced wrong postals. Always use sector±1 × block±10.

**Why this works:** Singapore's postal system groups carparks by sector and block within area. Scanning sector±1 catches adjacent districts; scanning block±10 catches adjacent building blocks — matching how carparks are distributed across postals.

## Verified Postal Codes

| Location | Postal | EV found (single) | EV found (multi) |
|----------|--------|------------------|-----------------|
| Innovis | 138634 | ✅ 1 location | ✅ 1 location |
| AMK | 569088 | ❌ | TBC |
| Pasir Ris | 518141 | ❌ | ❌ |
| Orchard Hotel | 238879 | ✅ 1 location (Orchard Hotel) | ✅ 11 locations |

## Limitations

- **Radius unknown** — API returns locations "near" a postal code but no radius is documented
- **No live availability per bay** — only aggregate position-level availability count
- **No pricing** — `price` and `priceType` fields are empty strings in all observed responses
