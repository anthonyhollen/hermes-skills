# Canonical Task List (for recovery)

If `task-tracker.json` is deleted or corrupted, rebuild it from this list (regenerated from the live file on 2026-09-26 — 32 tasks; frequencies last changed the same day, four tasks monthly → quarterly). Each entry is `name` → `id`; the id is the slug of the name (rules in `data-and-writes.md`). Recover `last_done` values from the newest file in `~/.hermes/data/task-tracker backups/` if available, else `null`. Keep this list in sync after every add / remove / rename / move.

**`categories` block** (include it — without it every interval falls back to the defaults):
```json
{
  "categories": {
    "weekly": {
      "label": "Weekly",
      "interval_days": 7
    },
    "fortnightly": {
      "label": "Fortnightly",
      "interval_days": 14
    },
    "monthly": {
      "label": "Monthly",
      "interval_days": 30
    },
    "quarterly": {
      "label": "Quarterly",
      "interval_days": 90
    }
  }
}
```

**Weekly** (every 7 days):
- Sayang bottle wash → `sayang-bottle-wash`
- Retainer wash → `retainer-wash`
- Toilet bowl & sink wash → `toilet-bowl-and-sink-wash`
- Mop house → `mop-house`
- Toothbrush charge → `toothbrush-charge`
- Car wash → `car-wash`
- Towel change → `towel-change`

**Fortnightly** (every 14 days):
- Car ceramic shampoo → `car-ceramic-shampoo`
- Shower area wash → `shower-area-wash`
- Kitchen sink wash → `kitchen-sink-wash`
- Toilet rug wash → `toilet-rug-wash`
- Baby bedsheet change → `baby-bedsheet-change`

**Monthly** (every 30 days):
- Quilt wash → `quilt-wash`
- Bedsheet wash → `bedsheet-wash`
- Mattress protector wash → `mattress-protector-wash`
- Maria maintenance → `maria-maintenance`
- Kitchen mitts wash → `kitchen-mitts-wash`

**Quarterly** (every 90 days):
- Change toothbrush head → `change-toothbrush-head`
- Washing machine filter → `washing-machine-filter`
- Change digital lock battery → `change-digital-lock-battery`
- Liquid plumr → `liquid-plumr`
- Change dishwashing sponge → `change-dishwashing-sponge`
- Xiaomi air filter → `xiaomi-air-filter`
- Guest bedsheet wash → `guest-bedsheet-wash`
- Sofa blanket wash → `sofa-blanket-wash`
- Shaver blade change → `shaver-blade-change`
- Clean ceiling fans → `clean-ceiling-fans`
- Baby bottle sponge change → `baby-bottle-sponge-change`
- Lock'n'lock water jug wash → `locknlock-water-jug-wash`
- Washing machine cleaning → `washing-machine-cleaning`
- Microwave wipe → `microwave-wipe`
- Water dispenser wash → `water-dispenser-wash`
