> **Historical (2026-09-29):** diagnosis of agent-mode runs (before the 2026-05-31 `no_agent` migration); kept as a record.

# Diagnosing "Output is Different From Previous Run" — Carpark Report

When the user says the parking report "looks different" or "isn't what I expected"
compared to a prior run, the cause is **almost always data-side, not a code bug**.
This reference is the diagnostic checklist — apply in this order before assuming
a regression.

## 30-second checklist (apply in this order)

1. **Different postal → different geography** — the single most common cause.
   Different neighborhoods have genuinely different mixes:
   - Industrial estates (e.g. Ubi 408xxx): dense EV chargers, mostly fast-DC, all
     within 1 km
   - HDB heartlands (e.g. Bukit Panjang 670xxx): sparse, mostly 7.4 kW AC
     residential blocks, often spread across 2-3 km
   - CBD / Orchard: mix of fast-DC and AC, often with LTA + URA private carparks
   The script is producing the right answer; the answers happen to be
   qualitatively different because the neighborhoods differ.
2. **Cache hit vs cache miss** — the EV subsystem is **cache-first** by design
   (`get_ev_nearby()` in `scripts/nearest_carpark.py`):
   - If `ev_anchors.csv` already has an anchor within 1 km of the target
     (`ANCHOR_COVER_RADIUS = 1000`), the script serves cached locations with
     no live scan. Log line: `EV cache hit — N cached location(s)`.
   - Otherwise it does a 303-postal discovery scan (sector ±1, delivery point
     ±50) and writes new rows to `ev_locations.csv` + a new anchor. Log line:
     `EV cache miss — discovering this area once`.
   The **user-visible side-effect**: cold areas take ~1-2 s longer (real network
   fan-out) and the cache files grow. This is intentional — see the 2026-05-27
   perf-pass notes for the 5.6× speedup that came with this design.
3. **Upstream LTA station data drift** — `ev_locations.csv` is refreshed when
   the `refresh_cache()` cron (`--refresh` flag) re-scans every anchor. Between
   refreshes, LTA can update individual stations: plug type, charging speed,
   power rating, number of bays, or even the name (e.g. `NEUROS` → `NEUROS &
   IMMUNOS` in the 2026-06-06 diff). Diff against the most recent
   `*.bak-perf-*.csv` or `*.bak-bugfix-*.csv` backup to spot these.

## How to investigate without changing code

When the user asks "investigate why the 2nd run deviates from the norm but
don't do any code fixes":

```bash
# 1. Confirm what the script saw on each run (look for cache hit/miss + log lines)
#    The script prints "EV cache hit — N cached location(s)" or
#    "EV cache miss — discovering this area once" — re-running shows the state
#    AFTER the previous run, so check the post-run log if available.

# 2. Re-derive both tables from the current cache to compare apples-to-apples
python3 -c "
import sys, math
sys.path.insert(0, '/home/amh/.hermes/skills/productivity/sg-commute/scripts')
import importlib.util
spec = importlib.util.spec_from_file_location('nc',
    '/home/amh/.hermes/skills/productivity/sg-commute/scripts/nearest_carpark.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for postal, tlat, tlon in [('<POSTAL_A>', LAT_A, LON_A), ('<POSTAL_B>', LAT_B, LON_B)]:
    cache = m.load_ev_cache()
    n = m.nearest_ev(tlat, tlon, cache, n=20)
    print(f'{postal}: {len(n)} hits')
    for d, r in n[:8]:
        print(f'  {d:6.0f}m  {r[\"name\"][:30]}')
"

# 3. Diff the cache against the most recent backup to spot upstream data drift
diff /home/amh/.hermes/skills/productivity/sg-commute/data/ev_locations.csv.bak-perf-20260606 \
     /home/amh/.hermes/skills/productivity/sg-commute/data/ev_locations.csv | head -40

# 4. Inspect the anchor list to see which areas have been discovered
cat /home/amh/.hermes/skills/productivity/sg-commute/data/ev_anchors.csv
```

## The three common "different from norm" complaints — and what they mean

| Complaint | Actual cause | Fix? |
|-----------|--------------|------|
| "I expected more diverse parking types" | The target neighborhood is genuinely almost-all one system (e.g. Bukit Panjang is HDB-only; no LTA / URA in top-3). | No — data fact, not a code issue |
| "I expected the EV table to be as broad as last time" | Different neighborhood = different EV density. The 7 chargers within 2 km really are all there is. The script correctly tops up from the next sector to fill `n=3`. | No — same reason |
| "I expected the second run to NOT trigger a discovery scan / NOT modify the cache files" | Cold cache is by design (ANCHOR_COVER_RADIUS = 1 km). Disabling it would mean empty EV lists for any new area. | No — see 2026-05-27 perf pass |

If the user wants the second run to behave *exactly* like the first, that would
mean: skip the discovery and return an empty EV list. Which would be worse UX
for a cold area. Usually this is a **perception gap, not a bug** — the script
is doing the right thing for both.

## Red flags that DO indicate a real bug (rare)

- Same postal queried twice → produces **different** outputs (cache should be
  deterministic; if not, check for a non-idempotent merge in
  `merge_into_cache()`)
- `EV cache hit` but the returned list doesn't match what's in
  `ev_locations.csv` (loader bug — check `load_ev_cache()`)
- `nearest_ev` returns chargers with distance > 5 km when closer ones exist
  (haversine bug)
- LTA API returns empty when it shouldn't (`lta_live()` silently
  returning `{}` — check the curl exit code in stderr)

## Related

- `references/carpark-availability.md` — end-to-end pipeline, output format
- `references/ev-charging.md` — EV discovery / cache mechanics in detail
- `references/changelog.md` — 2026-05-27 entry: the 5.6× perf pass that introduced
  the cache-first design (also 6 known data-drift cases since)
