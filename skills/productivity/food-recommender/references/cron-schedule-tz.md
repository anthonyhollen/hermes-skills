# Cron Schedule Conventions

## Hermes Cron Hours are SGT, NOT UTC

The Hermes cron scheduler (`cronjob action=create`) interprets hour fields in **SGT (Asia/Singapore, UTC+8)**, NOT UTC.

```
0 18 * * *  →  fires at 18:00 SGT (6pm SGT)
0 10 * * *  →  fires at 10:00 SGT (2am UTC)  ← WRONG if you meant 6pm
```

When creating any scheduled job that must fire at a specific SGT time, use the hour directly
as the SGT hour. No UTC conversion needed.

## Correct Schedule Examples

| Desired SGT time | Correct schedule | Wrong schedule (fires 8h early) |
|------------------|------------------|---------------------------------|
| 6pm SGT | `0 18 * * *` | `0 10 * * *` |
| 7:30am SGT | `30 7 * * *` | `30 23 * * *` (11pm UTC, 7:30am SGT-8h) |
| 10pm SGT | `0 22 * * *` | `0 14 * * *` |

## How to Verify

After creating a cron job, check `next_run_at` in the response — it will be in `+08:00` timezone.
If the time doesn't match your intended SGT time, the schedule field was wrong.

```bash
cronjob action=list  # check next_run_at for all jobs
```