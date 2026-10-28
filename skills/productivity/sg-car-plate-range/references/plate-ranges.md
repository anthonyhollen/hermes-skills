# SG Car Plate Range — LTA Data Reference

## Current Range (as of session 2026-05-16)

**Source:** LTA VRL Portal — https://vrl.lta.gov.sg/vrls/app/ao/enq-highest-sys-no-proxy
**Vehicle type:** Car
**Last checked:** 16 May 2026 / ~11:21 SGT

### Range
```
SPH918X to SPH1631R (both vehicle no. inclusive)
```

## Plate-in-Range Check: SPR8008

**Result: NOT in range**

Reason:
- SPR ≠ SPH — different letter prefix
- Even if same prefix, numeric portion 8008 > 1631 (above range maximum)

## Historical Ranges (known)

| Date | Range |
|------|-------|
| 2026-05-16 | SPH918X – SPH1631R |

## LTA Plate Series Prefix Rules

- First 3 letters define the series: `SPH`, `SPR`, `SBA`, `SCA`, etc.
- Numeric portion can include a letter suffix: `918X`, `1631R`
- Range is specific to each prefix — no cross-prefix comparison valid
- For bidding purposes: if target plate prefix differs from current range prefix, the plate is either already taken (older series) or not yet released (newer series)

## How to Verify Current Range

1. Navigate to `https://vrl.lta.gov.sg/vrls/app/ao/enq-highest-sys-no-proxy`
2. Select **Car** (pre-selected by default)
3. Click the **LabelText** wrapper (ref `e6` — the row containing the checkbox AND "I have read and agree..." text) to check the T&C box — clicking just the `<input>` does NOT trigger Angular change detection
4. Click **Next**
5. Read range from the results panel

**⚠️ This page has no API.** No back-end endpoint exists for programmatic queries. Must use browser automation or manual checking.