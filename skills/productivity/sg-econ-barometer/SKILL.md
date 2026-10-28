---
name: sg-econ-barometer
category: productivity
version: 1.1.1
description: Gauges the overall Singapore economy by tracking a basket of official indicators (GDP, inflation, industrial production, retail sales, unemployment, HDB resale) month-on-month, computing a polarity-aware diffusion composite, and posting a Discord barometer report.
author: ham
triggers:
  - "singapore economy"
  - "economy barometer"
  - "sg econ"
  - "how is the economy"
  - "economic indicators"
metadata:
  hermes:
    tags: [singapore, economy, macro, indicators, discord, data.gov.sg, singstat]
    related_skills: [sg-commute, discord-table-format]
---

# SG Economy Barometer

## Overview
Reads a basket of **official Singapore macro indicators**, computes their
month/quarter-on-period and year-on-year trends, rolls them into a single
**diffusion composite** (Expanding → Contracting), and posts a barometer to
Discord. **Pure Python, near-zero token** — no LLM in v1 (the LLM narrative is a
planned Phase 3). The indicator set is **data-driven** via
`sg_econ_indicators.json`, so adding an indicator is a config edit, not a code
change.

## Indicators (v1.1 — Tier-1, clean keyless APIs)
13 active (10 scored + 3 context); COE stubbed/disabled. SingStat series fetched by `series_no` (sN).
| Indicator | Source | Table | Scored |
|---|---|---|---|
| GDP (real, YoY) | SingStat | `M014811` s1 (chained-2015 $, qtrly) | ✓ |
| NODX exports (YoY) | SingStat | `M451301` s1 (monthly) | ✓ |
| Container throughput (YoY) | SingStat | `M651101` s4 (monthly) | ✓ |
| Industrial production (YoY) | SingStat | `M355351` s1 (monthly) | ✓ |
| Retail sales (YoY) | SingStat | `M602121` s1 (monthly) | ✓ |
| F&B services (YoY) | SingStat | `M602131` s1 (monthly) | ✓ |
| Visitor arrivals (YoY) | SingStat | `M550071` s1 (monthly) | ✓ |
| Unemployment (SA) | SingStat | `M182342` s1 (qtrly, SA) | ✓ |
| Mfg outlook 6-mo (balance) | SingStat | `M250141` s1 (qtrly, forward-looking) | ✓ |
| HDB resale (avg, YoY) | data.gov.sg | `d_8b84c4ee…` (monthly median) | ✓ |
| Inflation (CPI YoY) | SingStat | `M213751` s1 (monthly) | context |
| MAS core inflation (YoY) | SingStat | `M213891` s1.2 (monthly) | context |
| Money supply M2 (YoY) | SingStat | `M701111` s1.1 (monthly) | context |
| COE Cat A premium | data.gov.sg | _unresolved_ | disabled |

SingStat rows are fetched by **`seriesNoORrowNo`** (the headline/total series is
`"1"`); a full `limit` then returns that one series' whole history (the API's
`limit` caps total cells **across all rows**, so fetching the whole table starves
multi-row tables of periods — fetch the single series instead).

## Composite
Each scored indicator gets a **headline** (YoY % for flows, level for the rate)
and a **delta** vs the previous period, then an **improving / worsening** flag by
its `polarity` (rising GDP good; rising unemployment bad). Inflation is shown for
context but **not scored** (both high and negative are bad). The composite:
- **diffusion** = % of scored indicators improving;
- **score** (−100…+100) = weight-averaged improving(+1)/worsening(−1);
- **verdict**: >40 Expanding · >10 Firming · ±10 Steady · <−10 Cooling · <−40 Contracting.

## How to run
Engine: `~/.hermes/scripts/sg_econ_barometer.py` (safe by default — prints, no post).

| Command | Effect |
|---|---|
| `python3 sg_econ_barometer.py` | Fetch + compute + print report; update history state. |
| `python3 sg_econ_barometer.py --debug` | Per-indicator diagnostics to stderr. |
| `python3 sg_econ_barometer.py --post <channel>` | Also post to a Discord channel. **Test in the playground** channel. |
| `python3 sg_econ_barometer.py --post-home` | Post to `DISCORD_HOME_CHANNEL`. |
| `python3 sg_econ_barometer.py --no-state` | Don't write the state file (dry run). |
| `python3 sg_econ_barometer.py --list-rows <TABLE>` | Dump a SingStat table's row labels (to wire a new series). |

## Data-source quirks (important)
- **All HTTP goes through `curl`** with a browser User-Agent. SingStat's WAF
  returns **403 to Python urllib** even with a UA set (same class as the NEA
  gotcha in the vault's API Reference). data.gov.sg `datastore_search` tolerates
  urllib but we use curl uniformly.
- **HDB resale lags**: prices are keyed by *registration month*, so the current
  calendar month is incomplete — the engine **excludes the current month** and
  uses the latest completed one. The median (not mean) is used, so a thin recent
  month is still fairly stable.
- **COE is disabled**: data.gov.sg's v2 keyword search is non-functional (returns
  the same 3 unrelated datasets for every query), so the COE resource id couldn't
  be resolved programmatically. Set `resource` + `enabled:true` on the `coe_cat_a`
  entry once the dataset id is known (from its data.gov.sg page URL).

## History / trends
`~/.hermes/scripts/.sg_econ_state.json` (atomic writes) stores, per indicator, the
period→value series (SingStat backfills years instantly; HDB fills incrementally)
plus the last 30 run summaries. This is what enables month-on-month trend.

## Extending — add an indicator as DATA
Edit `sg_econ_indicators.json`: add `{key,label,sector,source,table|resource,
series_no,row,kind,unit,polarity,weight,scored}`. `kind` ∈ `yoy_pct` (value already
a YoY %), `rate` (value already a level %), `index`/`level` (engine computes YoY).
See `references/indicators.md` for the full field reference and the Tier-2 backlog
(MAS rates [API currently down], URA private-property, PMI/NODX via web fallback).

## Cron
`no_agent`, `deliver: local`, monthly — runs `sg_econ_barometer.py --post-home
--quiet` (~25th, after the month's CPI/retail/NODX releases). Self-posts to the
home channel; on a delivery failure it prints the report to the saved job output
and exits non-zero (the scheduler pushes no alert of its own on `deliver: local`).

## Keeping in sync (Ham's rule)
Cron runs `/home/amh/.hermes/scripts/sg_econ_barometer.py`; the canonical copy
lives here in `scripts/`. After any edit, deploy to **both** and keep
`sg_econ_indicators.json` identical in both places.

## Verification
- `python3 -m py_compile sg_econ_barometer.py` clean.
- A run prints a verdict line + the indicator table and writes `.sg_econ_state.json`.
- v1.1.0 tested live 19-Jun-2026: 13/13 active indicators fetched; posted to the playground channel.
