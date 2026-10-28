# Skip signals (v2.18.0, 2026-09-30)

Detecting skips Spotify never reports, and keeping skipped tracks out of the
playlist. Code: `src/skipsig.py` (pure logic), `src/skip_signals.py` (rebuild,
policy, digest, labels), `src/sampler.py` (resident poller). Tests:
`scripts/test_skip_signals.py` (+ `scripts/fixtures/skip_2026_09_30.json`).

## Why

`GET /me/player/recently-played` only has the **end** time of each play, and
omits most skips outright. On 2026-09-30 the user played the playlist in order
(shuffle off). The old logic recorded **1** skip (a track not even in the
playlist). The user confirmed 15 skips: positions 10, 15, 16, 18, 25–28, 32–35,
37, 38 and 40. Over the 7 days before, it recorded 195 plays but only 1 skip.

There is no rule for which short plays get recorded: a 24s play was recorded,
while several passes of 35s or more were not. Recently-played also cannot show
the following:

- **Pause then skip.** #37 was paused 34 min and then skipped, but it scored +1.0 as a full play.
- **A long exposure versus a pause.** For example the 4:30 gap across #25–28.
- **The last track before autoplay** (#40).
- **Intent inside a rapid burst of skips.** #17 Tear Drop was passed over, not skipped.
- **Shuffle and taps.**
- **Late-reported plays.** One morning play only appeared about 10 h later.

## Design

1. **Direct observation (primary).** A systemd **user** service on the Pi
   (`spotify-sampler.service`, launcher `~/.hermes/scripts/spotify_sampler.py`)
   polls `GET /me/player`. It sees the account's playback on any online device,
   including a phone on CarPlay. The token scopes `user-read-playback-state` /
   `user-read-currently-playing` were already granted.
2. **Stored playlist versions.** `refresher` saves each composition it writes
   (`playlist_version*`). At deploy the live playlist was saved as the
   `bootstrap` version. The poller now stores `play_events.context_uri`.
3. **Fallback inference** covers only time the sampler did not see: the phone
   offline, or the Pi / network / service down. It uses positional pass-overs
   between recorded plays of the playlist.
4. **Policy, split by reversibility:**
   - A **cooldown** (reversible, tuned for recall) is driven by *any* skip evidence.
   - **Score penalties and bans** (tuned for precision) come only from skips the sampler observed.
5. **Shadow mode.** `skip_signals.enabled: false` until the nightly digest
   proves accuracy. Only the user flips it. **Enabled 2026-10-01** after the
   user verified the first live morning — all policy below is now active.

## Sampler (`sampler.py`, `skipsig.Segmenter`)

Poll cadence (config `skip_signals.sampler`):

| Situation | Interval | Why |
|---|---|---|
| playing | 3s | exposure resolution |
| first 15s of a track / after an early exit | 1s | burst skips (3–4s each) |
| paused | 10s | |
| stopped < 45 min ago | 5s | resume-then-skip (the #37 pattern) |
| idle 06:00–24:00 / 00:00–06:00 | 15s / 60s | |
| 02:55–03:20 | ≥30s | the 03:00 refresh owns the rate budget |

Request limits:
- A token bucket allows at most 20 requests per 30s.
- A 429 waits 2× its Retry-After.
- On repeated 401s it re-reads the token, then forces one refresh, then backs off (up to 10 min).
- Requests use `client._do_request` with an 8s curl timeout and no retry loop. Each sample is stamped at the request midpoint.

**Segment** = one track exposure:
- **start** = `t − progress_ms`.
- **listened** = Σ clamp(Δprogress, 0, Δwall). Pauses and forward seeks never count.
- **exit** is bounded by `[last sighting, new track's start]`.
- **Continuity:** the same track id or `linked_from`, in the same context, not
  restarted, and **within 2h of when it last PLAYED** continues one segment
  across pauses, 204 windows and device switches.
  - A longer pause — e.g. the app restoring last night's track in the morning — closes the old exposure as `stopped`.
  - The resume then opens a new session segment, so a "start fresh" tap is `session_start` (neutral).
- **Exit observed or not:** an exit counts as observed only when the old
  track's last sighting and the new track's start are ≤ 20s apart **with no 204
  in between**. A 204 is ambiguous: a phone in a tunnel keeps playing offline,
  a closed app does not. Unobserved exits are `lost` (neutral).
- **Persistence:** the open segment is persisted in `sampler_state` (every 30s and on each change), so a restart resumes it.
- **Coverage:** `sampler_coverage` runs are extended only by samples showing a
  device state (playing / paused / ad / private) ≤ 20s apart. A 204, a stale
  state, an error, a 429 or any backoff breaks the run, so the fallback still
  covers offline listening.

**end_reason** values:
- `completed` — within 15s of the end (crossfade / automix).
- `next` / `jump` — adjacent vs non-adjacent **later** position in the stored version.
- Neutral:
  - `back` — "previous" to one of the last 5 tracks, or a tap on an EARLIER position.
  - `restart` — progress reset.
  - `context_switch` — another context, an ad or an episode.
  - `session_start` — the first track after ≥1h idle, left within 20s. This is the "start fresh" tap, e.g. the client resuming yesterday's last track across the 03:00 replace.
  - `private` — a private session.
  - `lost` — the exit was not bracketed within 20s.
- `stopped` — playback ended on this track (session end).

**Classification of an early exit:**

| kind | rule (listened / position reached) | effect |
|---|---|---|
| `pass` | listened < 1.5s | no score change. The track is marked heard (discovery/wildcard) |
| `skim` | 1.5s ≤ listened < 5s, position < 90% | a skip: cooldown, demotion and score penalty |
| `skip` | listened ≥ 5s and position < 90% | a skip: cooldown, demotion and score penalty |
| `early_exit` | position ≥ 90% | a listen (normal score brackets) |

**User rule (2026-10-01):** leaving a song before 90%, by pressing next or tapping
another song, is a skip, even on a first listen that turned out not to be liked
(`judge.skip_completion_max: 0.90`; it was 0.30, with 30–60% exits feeding the
cooldown only).
- **Score.** A skip is scored inside the skip bracket however far in it came: its
  `score_ms` is capped at 29% of the duration, so −1.0, or −1.5 under 10%. It is
  never a positive partial listen.
- **Not skips:** a pass < 1.5s (e.g. Tear Drop inside a burst), stopping playback
  (`stopped`, e.g. the end of a drive), and the neutral exits.
- **Recorded plays the sampler did not see** keep the base brackets
  (`scoring.weights`), because intent (skip vs stop) is unknown there.

A track resumed mid-way at session start is judged on position. The thresholds
are calibrated from the digest labels. A threshold cannot be exact for rapid
bursts: #15/16/18 at about 3–4s were skips, and #17 was a pass.

**Matching.** A segment is matched one-to-one, nearest first, with a recorded
play of the same track (id or `linked_from`).
- **Window:** `played_at` must fall in `[first_seen + 1s, exit_hi + 10s]`. A
  second pass allows up to +60s, for long crossfades; without it the play would
  be counted twice.
- **Anchor:** for stopped/lost segments the anchor is the natural end.
- **Refinement:** a matched recorded end refines the listened estimate
  (`listened_with_recorded_end`), except across a 204.
- **Truncation:** a recorded end more than 3s **before** the last sighting means
  the server kept extrapolating progress for an offline phone. The segment is
  cut there and re-classified (`truncate_at_recorded_end`); a phantom completion
  becomes the real skip.
- **Keys:** signal rows are keyed by the recorded id, with the relinked id in
  `alt_track_id`.
- **`coverage_miss`:** a recorded play inside sampler coverage with no segment
  (a diagnostic only).

## Fallback (`skipsig.infer_uncovered`)

It covers only consecutive recorded plays whose gap no `sampler_coverage` run
spans. The version used is the one live when the earlier play ended. Yesterday's
copy is used only when that track is absent from today's.

**Sequential sessions only.** `recently-played` does not report shuffle. A
listening session (split at gaps > 2h) qualifies only if all of these hold:
- it has ≥ 3 consecutive in-version pairs,
- ≥ 80% of them ascend,
- ≥ 1 is exactly adjacent,
- and no sampler segment in it saw shuffle / Smart Shuffle on.

A shuffled session yields nothing (tested).

| Tier | Rule |
|---|---|
| T1 / T1-dense | both plays from the playlist, positions ascending a→b. Positions a+1..b−1 were passed over, with ≥1s and ≤45s unrecorded per position, at most 6 positions (more = a tap / queue jump) |
| T2 | same, but more than 45s per position, up to 10 min in total (e.g. #25–28) |
| T3 | a within 6 of the end, b in the playlist context but in neither version, and followed by more such plays (autoplay after the end): positions a+1..N |
| unknown_play | an uncovered play whose own listen time is unknowable: the unexplained time before it is > 20s, or it follows a > 6h break. Only for plays after the first stored version |

Nothing is inferred when:
- the order goes backwards (e.g. a tap back to #1),
- the other play is from a different context,
- the time is covered by the sampler,
- or the gap is > 10 min (a pause).

Fallback `inferred_skip` signals feed the **cooldown only**. They never score,
demote or ban, never mark a track heard, never enter `skipped_ids`, and never
block the underfill relax. `unknown_play` is the one fallback output that
touches scoring: with the flag on, that play is **unscored**. If that would tip
a ban, the 2-day evidence gate still applies.

Replay of 2026-09-30:
- 16 positions were inferred, giving precision 14/15 and recall 14/15.
- The false positive is #17 (inside a T1-dense burst).
- The false negative is #37: after a 34-min pause it is now `unknown`, no longer +1.0.
- Full listens right after a burst (#11, #19, #36, #39) keep their score.

## Policy (flag on only)

- **Cooldown.** Any of the following puts a track out of the output for
  `first_days` (3) from the skip, i.e. the next 3 refreshes:
  - a sampler skip or skim,
  - a sampler early exit below 60%,
  - a fallback `inferred_skip`.

  It matches by track id (and relinked id) **and** same-song signature
  (`composer._track_sig`).
- **Demote.** `demote_skips` (2) sampler skips/skims within 14 days → out of core
  and fallback until a *later* completed play (a sampler `completed`, or a
  recorded ≥90% play no signal claimed). It lasts at most `demote_max_days`
  (30), so it is never an indefinite ban.
- **Relax.** If still short of 40 after every fallback, cooled-down tracks are
  let back, **oldest skip first**. Demoted tracks and anything the *sampler* saw
  skipped within 24h stay out, so a short playlist is preferred to those.
- **Heard.** Every exposure the **sampler** observed is heard, so
  discovery/wildcard won't re-pick it. Sampler-skipped tracks are never
  re-offered as "heard" wildcards. Fallback inferences don't retire candidates.
- **Scoring** (`scorer.compute_affinity(use_signals=True)`):
  - Matched plays use the sampler's listened time. It is `None` (unscored) for neutral/pass/skim, and for a stop below 50%.
  - Unrecorded sampler skips, early exits and completions become derived events.
  - Uncovered `unknown_play` plays are unscored.
- **Bans.** The existing ≤ −1.0 rule applies. If recorded plays alone would *not*
  ban the track, it also needs negative evidence on 2 separate **listening
  days** (04:00→04:00 SGT, so one session over midnight counts once;
  `ban_evidence_ok`). Inferred signals never count toward a ban.

The derived table `skip_signal` is rebuilt from scratch (last 90 days) on every
poll and before the digest, so late plays self-correct. Signals are **never**
written to `play_events`: that would corrupt `get_latest_played_at_before` and
the heard set.

## Digest, labels, acceptance

**Paused 2026-10-12** (job `bdbb183faf82`): the user is confident in the skip
logic and asked for the DeepSeek skip report instead (`llm_report.py`, same
23:30 slot; it also rebuilds the signals). `hermes cron resume bdbb183faf82`
brings the digest back. The rest of this section describes it as it ran.

- **Digest.** The cron job `spotify_skip_digest_wrapper.py` runs at 23:30 SGT and delivers to
  Discord `1501742338639401071` (playground). It covers the lifetime of the
  version that saw most of the day's listening (the 03:00 one on a normal day),
  and each flagged song appears as `#pos Song — Artist`.
  - Songs are grouped under **plain-language section headers**, which double as
    the legend: ⏭ Skipped, 👀 Skimmed, ⏩ Left early, ⏭? Probably skipped,
    ? Length unknown, · No verdict. It then shows the ✅ Played-through and
    Not-reached ranges, off-playlist skips, and accuracy so far.
  - Each run of inferred skips gets one `↳` note saying why it was flagged:
    - "~35s unaccounted for" (T1)
    - "4 passed in a quick run (13s)" (T1-dense)
    - "4 missing over 4:31; could also be a pause" (T2)
    - "right before autoplay took over" (T3)
  - The tier codes themselves never appear in the digest.
  - Artists come from, in order: the version row (saved at refresh for
    discovery/wildcard picks), the sampler, and `artist_cache`. Otherwise, ONE
    read of the live playlist at digest time fills them in
    (`fill_version_meta`, best-effort).
  - A ⚠️ line appears if the sampler heartbeat is over 10 min old.
  - Empty output (no listening) means no message.
  - It covers 03:00 → 23:30; late-night plays are not in any digest.
  - A second refresh in the same day (manual run) does not move the review:
    the digest keeps the version that saw most of the day's listening and the
    header notes `(playlist refreshed again at hh:mm — this check covers up to then)`.
  - It is sized to fit one Discord message (UTF-16 length ≤ 1900). Detail lines
    are dropped from the end first, and the reply line always survives.
- **Labels.** Run from `src/`:
  `python3 skip_signals.py label 2026-10-01 "17 not, 30 skip, 12 maybe"`, or `… ok`.
  - A label word applies to every number since the previous one: `17, 30 not`,
    `25–28 skip` and `10 15 16 skip` all work. Numbers with no label are
    rejected.
  - A reply stores the **full truth set**: the corrections, and every other
    position as the digest called it. `maybe` is excluded.
  - **No reply = correct** (user rule, 2026-10-08; v2.20.4):
    - The nightly digest wrapper runs `skip_signals.auto_accept()` first. Every
      playlist version that ended ≥ 20 h ago (its digest went out the evening
      before, plus a day to reply), had listening, and has no labels is stored
      with the digest's own calls, `origin='silent'`, judged up to the version's end.
    - A later `label` reply replaces it.
    - CLI: `python3 skip_signals.py auto-accept`.
    - **Before 2.20.4** only 3 days had labels (09-30 with 3 real corrections,
      10-01 and 10-02 "ok"), so the digest showed the same "97%" every night.
      That came from tp 31 / fp 1 / fn 1, both errors on 09-30.
- **Accuracy.** `python3 skip_signals.py accuracy` re-judges the **current**
  logic against those truth sets, only over what was reviewed (version start →
  label time).
  - It reports precision/recall overall, per source and per day, plus the
    sampler's exit-time error (median/p95 of recorded end vs observed exit).
  - Since 2.20.4 it also reports `corrections` (your explicit labels),
    `silent_days` and `last_correction`.
  - Silent days count as correct, so only your corrections can lower the
    score. The digest line therefore shows the day count and how many
    corrections you've made, e.g. `Accuracy (12 days · no reply = correct ·
    3 corrections from you, last 2026-09-30)`.
  - The reply prompt reads "Reply only if something is wrong".
- **Gates before flipping `enabled: true`** (at least 3 listening days, including a weekend):

  | Gate | Threshold |
  |---|---|
  | Sampler coverage (sampler-matched / recorded plays, digest line 2) | ≥ 90% |
  | Sampler precision | ≥ 95% (and ≥ 85% on every day) |
  | Sampler recall | ≥ 90% (and ≥ 85% on every day) |
  | Exit-time error | median ≤ 1.5s, p95 ≤ 4s |
  | Journal: Retry-After > 30s | none |
  | Journal: sampler restarts | none |
  | Refreshes | still 40/40 |

  Rollback: set `enabled: false`.

## Operations

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)          # needed for systemctl --user over SSH
systemctl --user status spotify-sampler            # is-active / restart / stop
journalctl --user -u spotify-sampler -n 50
cd ~/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src
~/.hermes/hermes-agent/venv/bin/python skip_signals.py digest        # today's digest, printed
~/.hermes/hermes-agent/venv/bin/python -c "import db,json; db.init_db(); print(db.get_sampler_state('heartbeat'))"
python3 ../../scripts/test_skip_signals.py          # regression suite (no network)
```

- Restart the sampler after any change to `src/`. It reloads `config.yaml` by
  itself when the file's mtime changes. A config that fails validation is
  rejected, keeping the old one (and built-in defaults at startup), so a typo
  can't crash-loop the unit. The unit restarts indefinitely
  (`StartLimitIntervalSec=0`, `RestartSec=30`).
- Persistence is one SQLite transaction per 30s flush (`db.sampler_flush`).
- Tables: `player_sample` is kept 14 days, `play_segment` and
  `sampler_coverage` 180 days (pruned by the sampler every 6h).
- Footprint (measured 2026-10-01 on the Pi 5): ≈0.5% of one core average
  (≈70 ms CPU per request — the per-poll curl spawn/TLS dominates, the Python
  process itself is ~7 s CPU/day); ~50 MB RSS; DB stays ~2 MB. The idle 15 s /
  night 60 s / quiet-window backoffs keep it low; 0 rate-limits so far.

## Residual limits (not closable)

- **Listening the Pi cannot see:** a phone offline or on downloaded music with no
  signal, or the Pi / network / service down. These fall back to inference.
- **Intent inside sub-second bursts,** and passes shorter than one poll interval
  (never observed, so never penalised).
- **Under shuffle / Smart Shuffle,** unobserved pass-throughs cannot be named.
  `me/player/queue` is untested.
- **Why a track was left at session start or on a context switch** — both are
  treated as neutral.
- **A #37-style skip where the pause shows as a 204** (e.g. CarPlay
  disconnected while parked) and next is pressed within a few seconds of the
  resume becomes `lost` (neutral). It is caught when the phone keeps reporting
  "paused". Check `player_sample` in the first shadow days to see which way
  CarPlay pauses actually appear.
- **Label replies containing other numbers** (e.g. a date) are read as
  positions — reply with positions only.
