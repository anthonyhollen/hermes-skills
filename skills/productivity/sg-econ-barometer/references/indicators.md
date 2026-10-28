# Indicator registry — field reference & backlog

The live basket is `~/.hermes/scripts/sg_econ_indicators.json` (mirrored in this
skill's `scripts/`). One object per indicator.

## Fields
- `key` — stable id (state key). `label` — shown in the report. `sector` — grouping.
- `source` — `singstat` | `datagov_hdb` | `datagov_generic`.
- SingStat: `table` (e.g. `M213751`), `series_no` (headline/total = `"1"`), `row`
  (human label + sanity note).
- data.gov.sg: `resource` (`d_…`), `field`, `period_field`.
- `kind` — `yoy_pct` (value already a YoY %, e.g. a growth-rate table) · `rate`
  (value already a level %, e.g. unemployment) · `index` (compute YoY from an
  index) · `level` (compute YoY from a $ level, e.g. HDB median).
- `polarity` — `up_good` | `down_good` | `neutral` (neutral ⇒ shown, never scored).
- `weight` — composite weight. `scored` — include in composite? `enabled:false` — skip.

## Resolved IDs (verified 17-Jun-2026)
- GDP real level (chained 2015 $, qtrly): `M014811`, series 1 = "GDP In Chained (2015) Dollars".
- CPI monthly: `M213751`, series 1 = "All Items".
- Industrial Production monthly: `M355351`, series 1 = "Total". (Note `M355411`/`M355431` are YoY tables but `…431` is **annual** — compute YoY from the index instead.)
- Retail Sales Index monthly: `M602121`, series 1 = "Total".
- Unemployment qtrly SA: `M182342`, series 1 = "Total Unemployment Rate, (SA)".
- HDB resale: `d_8b84c4ee58e3cfc0ece0d773c8ca6abc` (records since 2017-01; ~233k rows).
- NODX (non-oil domestic exports): `M451301`, series 1 = "Total" ($ value, monthly) -> YoY.
- Container throughput: `M651101`, series **4** = "Total Container Throughput" (monthly) -> YoY.
- F&B Services Index: `M602131`, series 1 = "Total" (monthly index) -> YoY.
- Visitor arrivals: `M550071`, series 1 = "Total International Visitor Arrivals" (monthly count) -> YoY.
- Mfg business expectations: `M250141`, series 1 = "General Business Expectations (Forecast For Next 6 Months)" (qtrly net weighted balance, rate).
- MAS core inflation YoY: `M213891`, series **1.2** = "Percent Change Over Corresponding Period Of Previous Year" (monthly, yoy_pct, context).
- Money supply M2: `M701111`, series **1.1** = "M2" ($ monthly) -> YoY (context).

## Tier-2 backlog (not in v1)
- **COE premiums** — data.gov.sg dataset; resource id unresolved (v2 search broken). Wire `coe_cat_a` once known.
- **MAS rates / SGD NEER / T-bill yields / reserves** — the old `eservices.mas.gov.sg` datastore API currently returns a **maintenance page**; revisit, or source SORA/T-bill from news.
- **URA private-property price & rental index** — needs free token registration.
- **NODX, PMI (SIPMM), GDP advance estimate, MAS policy stance** — no clean API → web/news fallback (ScraperAPI + LLM extraction), provenance-tagged and down-weighted. This is the planned Phase 2.
- **STI** — Yahoo Finance `^STI` for market sentiment. NOTE: Yahoo returns empty/HTML to datacenter curl (needs a crumb/cookie); route via ScraperAPI or another quote provider if added.

## How to add one
1. `python3 sg_econ_barometer.py --list-rows <TABLE>` to find the row/series.
2. Add the object to `sg_econ_indicators.json` (both copies).
3. `python3 sg_econ_barometer.py --debug` and confirm the headline/delta/n look right.
