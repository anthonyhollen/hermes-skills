# Changelog — sg-econ-barometer

## 1.1.1 — 2026-06-19
- Report: traffic-light icons 🟢/🟡/🔴 + ⚪ for context rows (replaced the ◻ hollow square); added a legend line.

## 1.1.0 — 2026-06-19
- Expanded basket 6 -> 13 active indicators (10 scored + 3 context), all SingStat (no new key):
  NODX, container throughput, F&B services, visitor arrivals, mfg 6-mo outlook (scored);
  MAS core inflation + money supply M2 (context). Widened the report label column.
- Evaluated STI via Yahoo Finance — left out: Yahoo blocks datacenter curl (empty response).

## 1.0.0 — 2026-06-17
- Initial build. Tier-1 (clean-API) basket: GDP (real, YoY), CPI inflation
  (context), industrial production, retail sales, unemployment (SA), HDB resale
  median. SingStat (via `seriesNoORrowNo`, curl-only) + data.gov.sg datastore.
- Polarity-aware diffusion composite (diffusion %, −100…+100 score, verdict).
- Local JSON history (`.sg_econ_state.json`, atomic) with instant SingStat
  backfill and incremental HDB month fills.
- Discord report (429-aware, <2000-char split). Tested to the playground channel.
- COE entry stubbed/disabled pending a resolvable data.gov.sg resource id.
- Known: HDB excludes the current (incomplete) registration month; uses median.
