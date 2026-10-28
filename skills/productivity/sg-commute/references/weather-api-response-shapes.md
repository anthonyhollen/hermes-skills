# Weather/Environmental API — Corrected Response Shapes

Verified against live `api-open.data.gov.sg/v2/real-time/api/` responses — multiple skills have wrong or incomplete parsing docs.

---

## twenty-four-hr-forecast

**Endpoint:** `https://api-open.data.gov.sg/v2/real-time/api/twenty-four-hr-forecast`

**Root:** `response["data"]["records"][0]`

### general (top-level) — WRONG vs CORRECT

```python
# SKILL DOCS (WRONG):
general.get("forecast")                  # → None
general.get("temperature", [30, 25])     # → always default
general.get("lowTemperature")           # → AttributeError

# ACTUAL (verified 2026-05-14 and 2026-05-26):
general = records[0]["general"]
forecast_text = general["forecast"]["text"]           # "Thundery Showers"
low_temp  = general["temperature"]["low"]              # 25
high_temp = general["temperature"]["high"]             # 34
low_humid  = general["relativeHumidity"]["low"]       # 60
high_humid = general["relativeHumidity"]["high"]      # 95
wind_dir   = general["wind"]["direction"]             # "SW"
low_wind   = general["wind"]["speed"]["low"]          # 10
high_wind  = general["wind"]["speed"]["high"]         # 20
```

### periods — structure

```python
periods = records[0]["periods"]
# Each period:
#   .regions — dict of region codes, e.g. {"east": {"code": "PC", "text": "Partly Cloudy (Day)"}}
#   .timePeriod — {"text": "6 am to Midday 14 May", "start": "2026-05-14T06:00:00+08:00", "end": "2026-05-14T12:00:00+08:00"}

# Use east region as default:
region = period["regions"].get("east", {})
fc_text = region.get("text", "N/A")
```

### period mapping (for 3-period forecast — verified 2026-05-26)

| Period index | Time range | Report label |
|---|---|---|
| 0 | 06:00–12:00 | Morning (already in past at 7am) |
| 1 | 12:00–18:00 | Midday–6pm |
| 2 | 18:00–06:00 next day | 6pm–6am tonight |

> **Correction (2026-09-29):** in the EVENING (the 22:00 run, and a 21:37 capture) the order is `[6pm–6am (tonight), 6am–Midday, Midday–6pm]` — i.e. again "`period[0]` is the current block", not the order the 2026-07-12 note guessed for the evening. Nothing depends on it: both scripts select/label periods from each period's own `timePeriod`.

**⚠️ Only 3 periods exist** — the 24hr forecast returns exactly indices 0, 1, 2. There is NO period 3. For the "6am–Midday tomorrow" line in the morning commute report, use today's general forecast text as a proxy — do not attempt to access `periods[3]`.

> **Correction (2026-06-12):** the index→time mapping above is stale for the 07:28 run window. The live API now returns the *upcoming* periods, so at ~07:28 index `0` = **Midday–6pm**, `1` = **6pm–6am (tonight)**, `2` = **6am–Midday tomorrow** — which is exactly the label list `morning_commute.py` `section1()` applies to `periods[0..2]` (verified against the live cron output). The script reads all three directly; it does NOT use the general forecast as a proxy. The "still only 3 periods, no `periods[3]`" rule remains correct.

> **Correction (2026-07-12) — the 2026-06-12 note was WRONG; the ORIGINAL table above is right.** The 2026-06-12 "correction" eyeballed the *labels* in a cron output and never checked the periods' own `timePeriod`/Day-Night codes, so it mistook a mislabel for a match ("documented ≠ verified"). Proof from three real 07:25 `no_agent` outputs (Jul 1/8/9): the line labeled **`6am–Midday tomorrow`** carried **`Partly Cloudy (Night)`** — a *night* code cannot describe a 06:00–12:00 daytime block, so `periods[2]` at 07:25 is actually the **18:00–06:00 night** block. The periods are anchored to the 06:00/12:00/18:00 boundaries and `period[0]` is the *current* block, so at 07:25 they are `[06:00–12:00, 12:00–18:00, 18:00–06:00]` (exactly the original table), and the hardcoded label list shifted all three by one *every morning*. **Fix (v1.9.9):** `section1()` no longer hardcodes labels — it derives each period's label from that period's OWN `timePeriod.text` (via `_period_label`), so labels always match the datum at any run time (07:25 → `6am–Midday / Midday–6pm / 6pm–6am (tonight)`; evening → `Midday–6pm / 6pm–6am (tonight) / 6am–Midday`). The general-forecast-as-proxy note is moot: the report now shows the 3 real API periods, correctly labelled, and never fabricates a 4th.

**⚠️ Two-level nesting in the 24hr record — KeyError trap (bug 2026-05-26):**

```python
# WRONG — directly accessing temperature on the record root:
r["temperature"]["low"]          # KeyError — temperature is UNDER general
r["relativeHumidity"]["low"]     # KeyError — same

# CORRECT — always go through r["general"] first:
r = f24["data"]["records"][0]
general = r["general"]
temp_low   = general["temperature"]["low"]       # 25
temp_high  = general["temperature"]["high"]      # 35
humid_low  = general["relativeHumidity"]["low"]   # 55
humid_high = general["relativeHumidity"]["high"]  # 90
forecast   = general["forecast"]["text"]          # "Partly Cloudy (Day)"
# periods is at TOP LEVEL of r, NOT inside general:
periods = r["periods"]   # exactly 3 elements: indices 0, 1, 2
```

**Bug history:** Session 2026-05-26 failed with `KeyError: 'temperature'` and `KeyError: 'relativeHumidity'` because the session code used `r["temperature"]` and `r["relativeHumidity"]` directly instead of `r["general"]["temperature"]`. The docs showed the correct path but the session code deviated. Always use the two-level access pattern above.

---

## two-hr-forecast

**Endpoint:** `https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast`

**Root:** `response["data"]`

```python
two_hr_data = response["data"]
two_hr_items = two_hr_data["items"]   # list of forecast bulletins

# Each item has:
#   .valid_period    — {"start": "2026-05-14T06:00:00+08:00", "end": "2026-05-14T08:00:00+08:00", "text": "6.00 am to 8.00 am"}
#   .update_timestamp
#   .timestamp
#   .forecasts — list of {"area": "Pasir Ris", "forecast": "Partly Cloudy (Day)"}

# ⚠️ Key is valid_period (underscore), NOT validPeriod (camelCase)
```

### Area list (47 areas on 2026-09-29 — "48" was the May count)

```python
areas = [a["name"] for a in response["data"]["area_metadata"]]
# "Pasir Ris" is present in the list
# Search: next(a for a in areas if "pasir ris" in a.lower())
```

---

## pm25

**Endpoint:** `https://api-open.data.gov.sg/v2/real-time/api/pm25`

**Root:** `response["data"]["items"][0]`

```python
pm25_data = response["data"]["items"][0]
readings = pm25_data["readings"]["pm25_one_hourly"]
# readings = {"east": 6, "south": 5, "west": 6, "north": 9, "central": 14}
# ⚠️ readings is a dict, NOT a list — each value is an int

east_pm25 = readings["east"]
all_vals = list(readings.values())
avg_pm25 = round(sum(all_vals) / len(all_vals))
```

---

## rainfall

**Endpoint:** `https://api-open.data.gov.sg/v2/real-time/api/rainfall`

**Root:** `response["data"]`

```python
rain_data = response["data"]
stations = rain_data["stations"]    # list of station objects
#   {"id": "S71", "deviceId": "S71", "name": "Kent Ridge Road",
#    "location": {"longitude": 103.7815, "latitude": 1.2923}}

# ⚠️ readings is a LIST with ONE element — {"timestamp", "data"} — NOT directly station readings
# WRONG:  for r in readings: r["stationId"]    ← KeyError
# CORRECT: readings[0]["data"] is the list of {stationId, value}
reading_bulletin = rain_data["readings"][0]   # {"timestamp": "...", "data": [...]}
reading_unit = rain_data["readingUnit"]       # "mm"

# Actual readings array:
reading_list = reading_bulletin["data"]        # list of {"stationId": "S71", "value": 0}

# Find reading for a station (e.g. S71):
s71_entry = next((d for d in reading_list if d.get("stationId") == "S71"), None)
rain_mm = s71_entry["value"] if s71_entry else None   # e.g. 0.0
# Timestamp of this reading:
timestamp = reading_bulletin["timestamp"]      # "2026-05-14T06:30:00+08:00"
```

**⚠️ The value is a 5-MINUTE total.** `data.readingType` = `"TB1 Rainfall 5 Minute Total F"`, `readingUnit` `"mm"`, 0.2 mm tipping-bucket steps — multiply by 12 for mm/h before applying any intensity label (verified 2026-09-29; the morning report's bands were wrong by ~12× until v1.11.0).

**⚠️ Station count guard:** Expected ~63 stations (89 on 2026-09-29). If `len(stations) < 60`, retry once before skipping. The API can return fewer stations on partial data days.

**⚠️ Station count note:** On 2026-05-14, API returned 62 stations — still valid. The skill reference claiming "must be ≥60" is a loose guard, not a hard rule.

---

## weather/flood-alerts (verified 2026-09-29)

**Endpoint:** `https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts`

```python
recs = response["data"]["records"]      # ~25 snapshots, one every 2 min (~48 min), NEWEST FIRST; plus data.paginationToken
recs[0] == {"datetime": "2026-09-29T21:34:54+08:00",
            "item": {"type": "observation", "readings": [], "isStationData": False},
            "updatedTimestamp": "2026-09-29T21:37:17+08:00"}
```
- A flood alert = a non-empty `item.readings` list. Only the **newest** snapshot says what is in effect now; counting snapshots multiplies one alert by up to ×25 and reports a cleared alert as current (the morning report did until v1.11.0).
- The field layout of a `readings[]` entry is **unverified** (no alert was active when this was captured), and so is whether a snapshot lists every alert still active or only newly-issued ones — hence the report's "reported in the last ~45 min (may have cleared)" wording for older-only readings. Capture a real alert payload when one occurs and update this section.
- A 429 body (`{"code":24,"name":"TOO_MANY_REQUESTS","data":null}`) has no `records` — treat as "status unavailable", never as "no alerts".

## Key traps that caused failures

| Bug | Symptom | Fix |
|-----|---------|-----|
| Used `r["temperature"]` directly instead of `r["general"]["temperature"]` | `KeyError: 'temperature'` | Always go through `r["general"]` first |
| Used `r["relativeHumidity"]` directly instead of `r["general"]["relativeHumidity"]` | `KeyError: 'relativeHumidity'` | Always go through `r["general"]` first |
| Used `general.get("forecast")` | `None` — wrong key | Use `general["forecast"]["text"]` |
| Used `general.get("temperature", [30,25])` | Always got default | Use `general["temperature"]["low/high"]` |
| Assumed `periods` keys as camelCase | KeyError | Use `timePeriod` not `time` |
| Assumed `two_hr["data"]["items"]` readings are dicts | `str.get()` on string | Readings are a `{"pm25_one_hourly": {...}}` dict-of-dicts |
| Assumed `rainfall` `readings` is a flat list of `{stationId, value}` | `KeyError` when accessing `r["stationId"]` directly | `readings` is `[{"timestamp", "data": [{stationId, value}, ...]}]` — index into `[0]["data"]` first |
| Background `&` in `execute_code` | "Foreground command uses '&' backgrounding" error | Use `subprocess.run()` sequentially or `terminal(background=true)` |