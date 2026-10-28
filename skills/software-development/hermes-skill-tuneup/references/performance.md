# Pass 2 — Performance

## Method: measure, then cut the biggest thing
**Profile before you touch anything.** Optimizing the part that's already cheap is the most common waste.

```bash
# wall-clock of a path, a few runs:
python3 - <<'PY'
import subprocess, sys, time
def t(label, argv, stdin=None, n=3):
    ts=[]
    for _ in range(n):
        s=time.perf_counter(); subprocess.run(argv, input=stdin, capture_output=True, text=True)
        ts.append((time.perf_counter()-s)*1000)
    print(f"{label}: {min(ts):.0f}-{max(ts):.0f} ms")
t("interp startup", [sys.executable,"-c","pass"])
t("the script (dry mode)", [sys.executable,"<script>","--json"])
PY
# where does import time go?  python3 -X importtime <script> 2>&1 | sort -k2 -n -t'|' | tail
```

Note the floor (interpreter startup ~18 ms) so you don't chase noise.

**Cold vs warm cache — run it twice.** The *first* touch of a large file or DB table is disk-bound (the Pi's storage is slow); the second is served from the OS page cache. In one tune-up the same query measured **~10.5 s cold vs ~0.85 s warm** — a 12× swing. Always run a path ≥2× and report **both** numbers; a one-shot cold timing reads as a code bug when it's really an I/O cliff. Optimize for the warm steady state unless the path genuinely runs cold every time (e.g. a once-a-day cron on a busy box).

## The usual bottleneck is NOT compute
In the source tune-up the numbers were:

| Stage | Cost | Verdict |
|------|------|---------|
| load + compute | ~50 ms | negligible |
| render all tables (one process) | ~700 ms | import-bound (PIL) — already optimal |
| **each `hermes send` (CLI) invocation** | **~500–620 ms cold start** + network | **× N sequential = the whole budget** |

Lesson: **repeated process spawns and sequential network calls dominate.** Compute and rendering rarely matter.

## Levers (in order of impact)
1. **Fewer process spawns.** Spawning a heavy CLI (or a fresh interpreter) per item pays its cold-import N times. Batch into one process, or replace the CLI with a direct call (e.g. `curl` to a REST API instead of a Python CLI wrapper — also fixes reliability, see Pass 4). **Ownership:** when that direct call *is* the Discord delivery path, the rewrite belongs to the **Delivery** pass — apply it there with its safety rules (content limits, untrusted-field sanitization, `allowed_mentions`, record-state-after-all-sends), *not* as a bare `curl` shipped from here. The speed win and the reliability fix are the same change; do it once, in Delivery, so the guardrails travel with it.
2. **Fewer / combined messages.** Merge a header and its image into one request instead of two.
3. **Reuse connections / one render process for all items** (the shared renderer already does this — don't regress it).
4. **Overlap independent work** (e.g. render while sending an unrelated text message) only if 1–3 aren't enough; it adds complexity.
5. **Raise a concurrency cap to collapse waves.** A per-item gate set below the item count forces multiple sequential waves of network/LLM calls. Set it = the max items per run (e.g. `CONCURRENT_LIMIT = 8` for a brief of ≤8 summaries) so they all issue in one wave — ~halving wall-clock. **Isolate the win with a fixed-latency stub**: replace the per-item work with `time.sleep(LAT)` and time the path at the old vs new limit, so you measure the wave-count structure without live-network noise.

## Refactoring a hot path? Prove it didn't change behavior
When the optimization should be *behavior-preserving* (caching, single-pass, pushing a filter down), capture an **output fingerprint** before and after and assert equality: run the function over a representative corpus, hash a canonical projection of its output (sorted tuples → `sha256`), and confirm the hash is identical post-change. (This let a `match`/`dedupe` rewrite ship with proof the scored/clustered output was byte-identical.) Two cheap levers that surface here: **duplicated work across a helper boundary** — a function that computes X then calls a helper that recomputes X (matching keywords + parsing a date, then a scorer that redoes both); inline or thread the result through — and **per-item values recomputed inside an O(n²) compare** (title token sets, normalized URLs); cache them once.

- **A fingerprint that embeds `now()` is NOT a stable baseline — it drifts every call even with byte-identical code + data, so a changed hash is not proof of a regression.** A scorer whose output depends on wall-clock (recency decay `0.5^(age/half_life)`, a "days until due", an age-bucket) produces a *different* hash each run purely because time advanced between the baseline capture and the re-check — the classic time-relative-output trap (see the Golden Rule) applied to the **baseline** itself. Don't use such a fingerprint to prove behavior-preservation and don't panic when it moves. Instead: (a) for a change that doesn't touch the scorer, prove it trivially by confirming the scorer **source file is byte-identical** to baseline (`shasum`); (b) for a real scorer change, freeze time (`now` as a fixed param/env) and diff at that fixed instant, or compare **time-invariant projections** — the *count* of scored items and the *top-N ordering* are stable while the absolute floats decay. (spotify-playlist-refresh 2.13.1: `compute_affinity`'s hash went `6f98…`→`a9b1…` between baseline and gate with n_scores steady at 52 and top-5 unchanged — the drift was pure recency decay; two back-to-back calls even differ by microseconds of decay. The proof of no-regression was `scorer.py` being byte-identical, not the hash.)

## A speedup can hide a regression — measure *success*, not just wall-clock
Concurrency and connection reuse cut latency, but they change the *load shape*, and an upstream may answer differently under load. Two findings from the `sg-commute` EV-scan rewrite (curl-per-call → keep-alive `requests.Session`, ~5.6× faster):
- **The faster path raised the upstream error rate.** Reused connections + higher concurrency provoked HTTP **500s the old slow path had been silently swallowing** (curl returns exit 0 on an HTTP error, so the body parsed as "no results"). Wall-clock looked great while the **true success-count dropped** — i.e. it was *losing data faster*. Always measure the count of **successful** responses (2xx), not just ms; a faster path that drops more answers is a regression. The right fix was *lower* concurrency + retry-on-5xx, not maximum speed.
- **Fan-out concentrates load on ONE host.** Parallelizing N calls that all hit the same host (the morning report's 5 weather calls → one `data.gov.sg` IP) trips that host's **per-IP rate/concurrency limit** far more readily than spaced sequential calls — and synchronized retries re-collide in lockstep. When you fan out same-host calls: bound concurrency, stagger, send an API key to lift the limit, and back off on 429/5xx (don't retry on the same fixed cadence).

## If the skill queries a database (SQLite / `state.db`)
Compute is rarely the cost; **what the query is forced to read off disk** is. Method:
- **`EXPLAIN QUERY PLAN <q>`** first. `SCAN <table>` over a wide table (rows carrying big TEXT/BLOB columns) drags overflow pages even when you only select small columns; `SEARCH … USING INDEX` is what you want.
- **Push filters into SQL, don't post-filter in Python.** Fetching every row and `json.loads`-ing it to keep 4% is the classic waste. A coarse `WHERE col LIKE '%marker%'` prefilter measured **854 ms → 60 ms (~14×)** by cutting 9,766 parsed rows to 352 — exact when a false positive is harmless (an inner check still gates the result).
- **Covering index — a tradeoff, not a freebie.** An index over exactly the grouped/aggregated columns makes a hot aggregate index-only (no wide-row reads), but adds write cost on every insert/update to a shared DB. Worth it only for a frequent/interactive query, not a weekly cron. Say which and why.
- **Don't VACUUM blindly.** Check `PRAGMA freelist_count` vs `page_count` first — if the free fraction is tiny (e.g. 0.4%) VACUUM reclaims almost nothing and isn't worth the lock.

## The single-shot case: sometimes the honest answer is "nothing to optimize"
Not every skill has a bottleneck worth cutting. A one-call utility — build a URL → one HTTPS `GET` → write a file (google-route-map's `download_static_map.py`) — is dominated by the lone network round-trip; the URL-building, percent-encoding, and file write run **once** and are noise next to it. The integrity move is to **say so plainly** ("I/O-bound, single request, no meaningful win") rather than invent micro-optimizations that don't move the number. For a single-shot fetch the only real levers are:
- **Payload size** (image dimensions / format, response fields requested) — and here it was *pinned by a correctness requirement* (`png32` + `scale=2` retina), so there was nothing to take.
- **Connection reuse** — matters **only under batching** (a `requests.Session` across many calls); for one shot there's nothing to reuse. Flag it as conditional ("if this is ever called in a loop, …"), don't add it speculatively.

And if you propose a "simplification" while you're in there (e.g. replacing a hand-rolled query-string loop with `urllib.parse.urlencode`), **prove it's byte-identical** to the current output before recommending it, and label it **readability, not performance** — same discipline as the output-fingerprint check above. (google-route-map: `urlencode(params, doseq=True, quote_via=quote, safe='')` was verified byte-identical to the loop, then offered as cleanup only.)

## When the bottleneck is an irreducible transfer, the lever is the timeout — not the code
Some failures look like "too slow" but the work is genuinely bandwidth-bound and can't be cut (a weekly backup that must zip a 444 MB DB and upload ~360 MB). There's nothing to optimize in the hot path; the real fix is **giving the watchdog enough headroom** and bounding each step below it. For a `no_agent` cron that means the **global** `cron.script_timeout_seconds` (there is no per-job timeout — see bug-hunt.md), raised with margin over the *measured* pipeline. Two method points this surfaced:
- **Measure throughput against the REAL endpoint, not a generic speed test.** Reviewers split on whether the run fit the 600 s timeout: one "measured" the uplink at ~495 KB/s (a generic/throttled path) and projected a ~745 s upload (→ doesn't fit); a direct probe of the *actual* Google Drive resumable path measured **3.3 MB/s** → ~112 s (→ fits with ~260 s to spare) — a **7× difference** that flips the decision. The skill's tie-breaker (a targeted probe, not a vote) settled it: upload a throwaway 50 MB file through the *same client/path the script uses*, time it, delete it. A cloud provider's edge is often far faster than `speedtest`-style numbers suggest. (hermes-backup.)
- **Cold vs warm matters here too:** the zip step measured ~118 s warm but the skill's own docs cited ~177–220 s cold (first Sunday-3am touch, page cache empty, and a larger DB). Size the timeout for the **cold** path, and note the data is growing so today's margin shrinks (re-raise to 900 s when it tightens).

## Report what you skipped
State what you measured (cold **and** warm), the bottleneck, the change, and the expected win (e.g. "9 → 5 sends, ≈2.8 s saved"). If you deliberately left a possible optimization (too fragile, shared dependency, marginal at current scale), say so and why — silent caps read as "done" when they aren't.

## `timeout=` is not a request budget (morning-brief 2026-09-15)

For any scheduled job whose step is SIGKILLed at a fixed timeout, **bounding each outbound
request is a correctness requirement, not a performance nicety** — the kill lands before the
step writes anything, so an unbounded call doesn't slow the report down, it destroys it (or,
in a delivery step, delivers it and loses the "already sent" state).

`requests`' `timeout=` is per-socket-operation. This tune-up needed **four** attempts to
actually bound it, each of which the next terminal-gate round disproved — record all three
holes, because fixing one reads exactly like fixing all of them:

1. **A dripping host re-arms the read timer forever.** `timeout=(10, 45)` never fires against
   one byte every 3 s.
2. **Checking the deadline BETWEEN chunks never fires.** `iter_content(N)` (and
   `raw.stream(N)`) blocks until N bytes have arrived, so with `Content-Length` framing a
   ~2 KB body is one single iteration *after the whole body landed*: measured 212 s against a
   150 s budget, past the 200 s step timeout. Smaller chunks only shrink the window.
   Compression makes it worse — `iter_content` yields only non-EMPTY *decoded* chunks, so wire
   bytes that decode to nothing never run the loop body at all (a gzip drip overran a 2 s
   budget by 30×).
3. **Nothing bounds the phase BEFORE the body** — connect, TLS, request write, response
   headers — because a watchdog can only be armed once `requests.get/post` has returned. A
   host dribbling header lines just under the read timeout keeps resetting it (20 s against a
   4 s budget; `http.client` accepts 100 header lines).

**What actually works, and is worth copying verbatim:**
- Run the request on a `daemon=True` thread and `join(timeout=remaining)`. Bounds phase 3; a
  daemon thread doesn't hold up interpreter exit, and you close a late-arriving response so
  the socket isn't leaked.
- Arm a `threading.Timer` that calls `socket.shutdown(SHUT_RDWR)` on the underlying socket.
  **`resp.close()` is not enough** — it drops a reference while the fd stays open, so a thread
  already blocked in `recv` keeps waiting (a drip still ran 105 s against a 10 s budget with
  `close()` alone). Reach the socket via `resp.raw._connection.sock` with fallbacks, and
  swallow errors.
- Account for RAW bytes whenever a `Content-Encoding` is present (treat *any* non-identity
  value as raw — an exact allow-list misses `gzip, gzip`), and inflate afterwards with
  `zlib.decompressobj(wbits).decompress(body, cap)`: it bounds memory as it goes (1 MB of wire
  inflates to ~980 MB), tolerates a missing trailer (`gzip.decompress` raises `EOFError`,
  which matched none of the handlers and silently degraded every item), and handles
  concatenated gzip members. **A wrong `wbits` does not always raise** — a raw-deflate stream
  fed to `wbits=15` returns `b""`, so only accept a non-empty result as a successful guess.
- Ask for `Accept-Encoding: identity` when the response is small; compression buys nothing and
  is strictly harder to bound.

**Derive each step's budget from its own retry arithmetic, then check it against the
wrapper's timeout.** A 120 s step cap was unsupported by the step's own numbers: 3 attempts ×
a 30 s read timeout + backoff = 96 s for ONE silent feed, and with 38 feeds over 20 workers
that is two waves — measured 192 s. Fixed by fetching in one wave (workers = item count), a
tighter per-request timeout, and a hard budget as the backstop. Extract the bounding logic
into ONE module: the gate found this fix applied in one caller and not the other, with the two
copies of the socket helpers already diverging.

## Rate-limited catalogue APIs: memo + pacing + TTL (spotify-playlist-refresh 2026-09-23)
- **Count duplicate URLs before optimizing anything else.** A cold refresh made 92 requests, 55% of them repeats (the same `artists/{id}/albums` fetched by two helpers, the same album's tracks up to 3×). A per-process memo of successful catalogue GETs (never errors, never user-state endpoints like recently-played) took cold 92→52 and, with album tracks cached in the artist row, warm 49→1.
- **Pace bursts under a per-minute limit** (e.g. ≥1.2s between catalogue GETs for ~50 req/min): ~60 cold requests fired in 20–40s trip 429s; paced they cost ~75s of a 290s stage budget. Pace only the rate-limited endpoint family, not the essential write.
- **A cache TTL equal to the job period is a cache miss** (see bug-hunt.md). Measure hit rate from `cached_at` vs run start times, not from the code.

## Per-partition fixed costs and batching (foodpanda 2026-09-28)
- **Count spawns per run as a function of data growth, not just today's wall-clock.** A sync did 2 `google_api.py` spawns per month tab on every run (a migration check that had been a no-op for months) — 28 s / 27 spawns at 9 tabs, +~1.8 s per month, against a 75 s self-imposed deadline. Replacing them with ONE in-process `values.batchGet` took it to ~10.5 s / 9 spawns with a byte-identical outcome on live data (same receipts, watermarks, zero writes — proven with a sandboxed run: live reads, writes blocked by a proxy service, state in `/tmp`). Simulate the growth with a fake clock (spawns × measured latency) to find the onset before it happens.
- **Size a run deadline from the REAL watchdog.** Read `cron.script_timeout_seconds` (global) from `config.yaml` rather than trusting a number in the skill's comments.
