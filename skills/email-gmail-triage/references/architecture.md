# Architecture (relocated from SKILL.md, 2026-06-09)

## File map

```
gmail_triage.py        — Hourly triage: fetch, categorize, label, archive, state
gmail_report.py       — 3× daily digest: selects by triaged_at watermark from state, posts to Discord
gmail_triage_state.json       — processed_ids [{id, category, from, subject, triaged_at}] + last_run
gmail_triage_report_log.json  — last_report_ts (the digest WATERMARK) + reported_at_watermark (tie-break) + last_reported_ids
gmail_label_cache.json        — maps Hermes/ label names → Gmail Label_XX IDs (rebuilt from Gmail every run)
gmail_correspondents_cache.json — {addresses, sent_threads, last_full_harvest} (Sent harvest: 2-day incremental, full 1-year pass per 24h)
test_vip_golden_rule.py / test_gates.py — categorization regression suites (in the skill dir)
test_report_window.py / test_triage_state.py — digest-watermark + state-write suites (in the skill dir)
scripts/gmail_state_heal_once.py — ⚠️ DEPRECATED (2026-06-03). One-shot orphan-recovery tool.
```

## State file paths (all under `~/.hermes/scripts/`)

- `gmail_triage_state.json`
- `gmail_triage_report_log.json`
- `gmail_label_cache.json`
- `gmail_correspondents_cache.json`

## State format (`gmail_triage_state.json`)

```json
{
  "last_run": "2026/06/03 01:00:12",
  "processed_ids": [
    {"id": "19e2d63a36822baf", "category": "Auto-filed", "from": "noreply@ex.com", "subject": "...", "triaged_at": "2026/06/03 01:00:12"},
    {"id": "19ded6a79032af17", "category": "Action", "from": "ops@github.com", "subject": "...", "triaged_at": "2026/06/03 01:00:12"},
    {"id": "19e4836f969b2d04", "category": "ReadLater", "from": "noreply@lh.gov.sg", "subject": "...", "triaged_at": "1970/01/01 00:00:00"}
  ]
}
```

- `triaged_at` (added 2026-06-03) is the digest watermark key. New entries get the time they were **filed** (per email since 2026-09-28; previously the run's start time — unsafe once state is persisted per email); entries written before the feature, or already handled on a prior run (the already-labeled skip path), get `EPOCH` (`1970/01/01 00:00:00`) so they are never re-reported. Stamps use the fixed `%Y/%m/%d %H:%M:%S` format, so lexical comparison == chronological.
- The digest selects `triaged_at > last_report_ts`, plus entries stamped exactly `last_report_ts` whose id is not in the log's `reported_at_watermark` (a digest can read a triage run half-way).
- Other top-level keys (2026-09-28): `unsure_pending` + `unsure_attempts` (Unsure questions not yet delivered), `modify_unknown` (emails whose modify call failed ambiguously — next run reads their labels and records them if they were filed), `body_defers` (per-id count of failed body fetches; filed without the body on the 3rd), `reminder_date` (day the 9 PM reminder was done).

## Execution / failure model (2026-09-28)

- Triage writes state atomically after **every** filed email, so a crash or SIGKILL keeps everything already filed. Each `google_api.py` call is capped at 90s; nothing new starts after 360s (watchdog 600s).
- An Auto-filed email is labelled + archived + marked read in **one** `gmail modify` call.
- Digest send failure → full digest in the run output, exit 1, watermark unchanged; the digest jobs' `failure_deliver: discord:1499972245437550602` turns that into a failure notice. Triage and digests are `deliver: local` — without that, a failure alerts nobody.
- Residual limits: an unreadable report log falls back to its mtime as the watermark, so an email filed in exactly that second can be missed; the Unsure queue drops after 24 failed posts (logged).

> **Legacy format:** Very old state files may have `processed_ids` as a flat list of strings. The triage script converts them to dict form on its next run (with `triaged_at=EPOCH`, so they never surface in a digest). The report no longer has a Gmail-API "legacy fallback" — a bare-string state simply yields no selectable entries until triage normalizes it.

## Report reads from state, not Gmail

The triage script records `{id, category, from, subject, triaged_at}` for each processed email. The report reads these directly from the state file and selects by the `triaged_at` watermark — it does NOT re-fetch from Gmail in the normal case. This keeps digests instant (<1s) regardless of batch size. A **read-only** metadata heal fetches from/subject only for the rare selected entry missing them (and logs a `[warn]` so the gap is visible next run); it never writes state. To inspect a specific email, use `google_api.py gmail get {id}` separately.

## OAuth token-refresh pitfall (now rare)

When fetching Gmail metadata for many IDs in a tight loop, the OAuth token can refresh mid-batch. The googleapiclient service singleton (`_service` in `gmail_report.py`) holds a stale credential reference, so subsequent `users().messages().get()` calls silently return messages with empty `payload.headers` even though `payload.labelIds` works fine. Symptom: a batch fetch returns blank from/subject for ~3% of IDs clustered at the END. **Fix pattern:** detect empty-header IDs, force the singleton service to None, re-fetch only those. See `references/google_api_metadata_pattern.md`.
