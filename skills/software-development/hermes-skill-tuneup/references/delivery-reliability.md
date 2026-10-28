# Pass 4 — Delivery Reliability (Discord)

The single most repeated reliability bug across Hermes skills is Discord delivery. `token-usage`, `sg-commute`, and `task-tracker` all converged on the same fix — copy it, don't reinvent it.

**This pass owns the CLI→`curl` rewrite** wherever it's motivated — including when the Performance pass identifies `hermes send` process-spawns as the bottleneck (the speed win and the reliability fix are the *same* change). Do the swap here, not in the perf pass, so the content-limit, state-ordering, and sanitization rules below travel with it; never ship a bare `curl` from elsewhere.

## Rip out these anti-patterns
- **`hermes send` from a script.** `hermes` is **not on PATH in non-login shells** — exactly how the cron scheduler and the agent's terminal tool invoke scripts. The call fails (or, worse, an over-timeout `hermes send` raises an uncaught `TimeoutExpired` and looks like a hang).
- **`MEDIA:` tags / `send_message` for paired text+image.** The gateway's `extract_media()` scans the whole reply, batches **all** text, then batches **all** images separately — header↔image pairing and order are lost. A `---` separator does not help. **And** each `MEDIA:` path is gated by `validate_media_delivery_path` (gateway/platforms/base.py): in default (non-strict) mode it delivers any *existing* non-credential file but **silently drops a path that doesn't resolve to a real file** (logs `Skipping unsafe MEDIA directive path`). So the classic failure is an **unsubstituted template path** — a live agent emitted `MEDIA:/tmp/route_<dest>.png` (the `<dest>` placeholder never filled in) and the map vanished, text-only shipped. (Corollary: a *real* file delivers fine in default mode — which is why `discord-table-format`, writing a real PNG to `/tmp/hermes_cache/`, works while a placeholder path fails. The reuse advice stays "use curl multipart," but know the actual gate is file-existence, not the tag itself.)
- **`DISCORD_BOT_TOKEN` from `os.environ`.** The cron scheduler does **not** export it to `no_agent` scripts; `~/.hermes/hermes-agent/.env` may hold a stale token. Read `~/.hermes/.env`.
- **`send_message` with no explicit `target` → the *home* channel.** With no target it falls back to `config.get_home_channel(platform)` = `DISCORD_HOME_CHANNEL` in `~/.hermes/.env`. If that's a broadcast/reports channel (here `#alerts`, `1499972245437550602`), **every untargeted send lands there** — the usual root cause of "the agent keeps posting to #alerts" on *interactive* replies. Crons are unaffected (their prompts pass the target explicitly). Don't rely on a no-target send to "reply where I am."

## The reliable pattern: direct REST multipart
One `curl` POST per section, with `content` (caption) **and** `file` (image) in the **same** multipart request — Discord attaches the image to that one message, atomically and in order.

```python
import os, subprocess, time, json

def load_token():
    for line in open(os.path.expanduser("~/.hermes/.env")):
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None

def discord_post(token, cid, content, img=None):
    """content + optional image in ONE request. cid may be a thread id (a thread
    is a channel for this endpoint). Returns True on 2xx."""
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    cmd = ["curl", "-sS", "--max-time", "20", "-w", "\n%{http_code}",
           "-X", "POST", url, "-H", f"Authorization: Bot {token}",
           "-F", f"content={content}"]
    if img and os.path.exists(img):
        cmd += ["-F", f"file=@{img}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False
    body, _, code = r.stdout.rpartition("\n")
    if code.strip() == "429":                      # rate limited — honor retry_after
        try: time.sleep(float(json.loads(body).get("retry_after", 1)) + 0.25)
        except Exception: time.sleep(1)
        return discord_post(token, cid, content, img)
    return r.returncode == 0 and code.strip().startswith("2")
```

Rules baked into the snippet:
- **Pass argv as a list (no shell)** — a shell turns `\n` in `content` into a literal backslash-n. (`token-usage` documents this exact bug.)
- **`time.sleep(0.5)` between sends** — Discord allows 5 messages / 5 s / channel.
- **Check the HTTP status** (`-w "%{http_code}"`), retry once on 429.
- **Send status to stderr, keep stdout clean** — an auto-deliver scheduler treats a no_agent script's stdout as the message body (or print `[SILENT]` as the first stdout line to suppress).

## Content limits & multi-message state ordering
Two failure modes beyond the transport itself:
- **Size limits.** Discord rejects content >2000 chars (code 50035) **and** empty/whitespace content (code 50006). Cap each message's content to 2000 before POST; when splitting a long body into chunks, never emit an empty chunk or a single line longer than the limit (hard-split it).
- **Don't `sys.exit` mid-burst on a transient error.** A brief that POSTs ~8–10 messages and *then* records "seen"/delivered state will, if it aborts on message 6, leave a **partial** brief delivered *and* — because the state write never ran — **re-send everything next run** (duplicates). Retry only transient failures (429/5xx/network) with bounded backoff; on a *permanent* failure exit **before** the state write, so the run re-sends cleanly rather than being silently marked delivered. Invariant: **record delivered-state only after all sends succeed.**

## The image *format* can break rendering, not just the transport
Getting the bytes to Discord isn't enough — the wrong encoding renders wrong. **PNG8 (256-color palette) can silently drop or misrender thin colored overlays** (a route polyline, a highlight stroke) in Discord's image pipeline; emit **PNG32** (full RGB — e.g. `&format=png32` on Google Static Maps) for any image carrying thin colored lines or markers. The tell is an image that looks fine locally but loses its overlay once posted. (google-route-map: the blue route line vanished under the default PNG8; `format=png32` fixed it — and the skill made it a mandatory rule.)

## Untrusted content reaching Discord — sanitize every interpolated field
When the message body interpolates **attacker-controllable** text (email subjects/senders, scraped titles, inbound message content), getting it to Discord *correctly* isn't enough — the content itself is an injection surface. Three things a crafted string can do: **ping the channel** (`@everyone`/`@here`/`<@id>`), **break your formatting** (a stray backtick closes a code span; a newline splits a line; backticks/`|` wreck a table), and **inject a clickable phishing link into your trusted channel** via Discord's masked-link syntax `[looks legit](https://evil)` or a bare `https://evil` autolink.
- **Sanitize *every* interpolated untrusted field — not just the obvious one.** The tell is one field cleaned while a sibling on the same line isn't. (email-gmail-triage: the digest ran `sanitize_discord(subject)` but interpolated the **sender** raw — same row, one safe, one not.) Audit each f-string/template for a field that skipped the helper.
- **The sanitizer must do all four:** strip `\r`/`\n` → space, neutralize backticks (`` ` `` → `'`), defang mentions (insert a zero-width space: `@` → `@​`), **and break link syntax** — `](` → `]​(` (kills masked links) and `://` → `:​//` (kills bare autolinks).
- **Belt-and-suspenders: also send `allowed_mentions: {"parse": []}`** on the POST. It blocks the *ping* server-side regardless — but it does **not** stop a backtick/newline format-break or a masked-link injection, so it complements the sanitizer, it doesn't replace it.

## Text-only status notifications vs the text+image pattern
The curl-multipart pattern above exists for **paired text+image**. A plain **text-only** status ping (a backup ✅/❌, a watchdog alert) doesn't need it — a `urllib` JSON POST to `/channels/{id}/messages` with `{"content": …, "allowed_mentions": {"parse": []}}` and the token from `~/.hermes/.env` is fine (cap content to 2000 chars, pass an explicit `urlopen(timeout=…)`, **and set a `User-Agent` header** — Discord sits behind Cloudflare, which **403/1010-blocks the bare `Python-urllib` user-agent**; curl's UA passes, urllib needs one set explicitly). Don't "upgrade" a working text notifier to multipart.
- **A `deliver: local` `no_agent` job will NOT have its failures pushed by the scheduler — the script must self-notify.** `deliver: local` saves the run output to disk and pushes nothing, so a failed run is invisible unless the *script itself* posts. Make the failure notification its own first-class path (a `fail()` that notifies **then** exits non-zero), not a happy-path branch at the end of a long script — a timeout/early-exit never reaches the end. (hermes-backup: the original posted only on completion, so every timeout failed silently for weeks. See the deliver:local bug-class in bug-hunt.md.)
- **For a `deliver: local` job that is a MULTI-STEP WRAPPER, put the self-notify + a per-step timeout in the WRAPPER, and give the delivery step the largest per-step budget.** When the job's `script` is a thin runner that shells out to N pipeline scripts in sequence (fetch → summarize → send), a failure can occur in *any* child, and the scheduler still alerts on nothing (deliver:local) — so the wrapper itself must own the `fail()` (notify-then-exit-nonzero) and must catch **every** way a child can fail: non-zero exit, `subprocess.TimeoutExpired`, **and** `OSError` at launch (a bad cwd/missing interp raises before the returncode check — catching only `TimeoutExpired` lets a launch failure crash with a bare traceback and skip the notify; the gate caught exactly this in the wrapper's own fix — an instance of *`except` narrower than the failures that occur*, on a self-introduced guard). Give each child a `subprocess.run(timeout=…)` whose **sum stays under the global `cron.script_timeout_seconds`** (so a hung step fails *with* a notify before the scheduler's silent SIGKILL — the hermes-backup watchdog lesson, applied per-step), and budget the caps by realistic worst-case, **not** evenly: the *delivery* step deserves the largest slice because a 429-throttled send legitimately runs long (Retry-After backoff × ~N POSTs) and a killed send costs a duplicate report next run, whereas fetch/parse steps have tighter internal timeouts. The wrapper runs under the **gateway venv** while the children may run under `/usr/bin/python3` (different deps) — so the wrapper's `fail()` should use **stdlib `urllib`** (with a `User-Agent` set — Cloudflare 1010-blocks bare urllib) rather than importing the child's `requests`, and must **suppress the post under `--dry-run`** so manual testing never spams. (morning-brief 1.8.0: `morning_brief_run.py` gained per-step `STEP_TIMEOUT` {fetch 120 / summarize 200 / send 240, sum 560 < the box's 600s watchdog} + a `fail()` that urllib-POSTs "⚠️ Morning brief failed at <step>" then exits non-zero; validated by a forced launch-failure dry-run that printed the alert to stderr and posted nothing.)
- **If a `deliver: local` script SELF-POSTS to Discord and runs `--quiet`, a failed post silently loses the whole report — fall back to stdout.** The scheduler still saves the script's stdout to the job-output file even for `deliver: local`; `--quiet` suppresses that, so when the *only* delivery channel is the script's own `post_discord` and it fails (429 exhausted, 50035, network), nothing reaches Discord **and** nothing was saved. Fix: have the post **return success/failure**, and on a required-post failure **print the full report to stdout even under `--quiet`** (so it lands in the saved job output) and **exit non-zero**. The quiet flag is for the happy path only; a delivery failure must break the silence. (vault-audit 1.1.2: `post_discord` was fire-and-forget under the weekly `--quiet` cron — a single Discord hiccup would have evaporated the drift report with no trace; now it falls back to stdout + exit 1.)
- **The top-level crash/failure alert must be a SINGLE bounded post, NOT the normal retry envelope — a retry-storm alert fired late in a run gets SIGKILLed with the run, losing the only failure signal on a `deliver: local` job.** A self-posting script's crash handler that routes through the same retried Discord path as its happy-path posts (`retry(_send)` → 3× `urlopen(timeout=15)` + 5+10s backoff ≈ **60s** worst case) can itself exceed the remaining budget when the crash happens late in a slow run — and the scheduler SIGKILLs mid-alert, so nothing posts. Give the crash handler its **own** poster: a single `urlopen` with a short timeout (~8s) and **no retry**, and **truncate the body** (a deep traceback exceeds Discord's 4096-char embed-description limit → 400 → posts nowhere; wrap/clamp to the tail). Budget it into the watchdog math with the timeout-hardening rule in bug-hunt.md: `RUN_DEADLINE + CALL_TIMEOUT + crash-alert(~8s) < ~120s`. (foodpanda 2026-07-18: the crash handler used the retry-based `discord_alert`; the terminal-gate refuter showed a hang at t≈95 + the 60s alert storm breached 120s — replaced with a single-shot `quick_alert(timeout=8)` + a 1500-char traceback tail. The complement of the deliver:local *self-notify* rule above: it's not enough to self-notify — the notify itself must be bounded.)
- **When `deliver` IS a platform target, the scheduler delivers for you — and the script's exit code + stdout ARE the delivery contract. Use them; don't add a `curl`.** For a `no_agent` job with `deliver: discord:<id>` (etc.), the scheduler's no_agent short-circuit (`cron/scheduler.py`) maps: **exit 0 + non-empty stdout → delivered verbatim**; **exit 0 + empty stdout → silent no-op (no message)**; **non-zero exit / timeout → delivered as a `⚠️ Cron job failed: …` alert** ("failed jobs always deliver", with stderr+stdout in the body; secrets are redacted from delivered output first). This is the *correct* pattern for a text-only push — the script just `print()`s and the scheduler posts; **do not** add a self-`curl` or `hermes send` (that double-delivers and re-introduces the PATH/MEDIA hazards). The bugs live in *misusing the exit code*: `print(...); sys.exit(1)` on a benign empty result fires a false "failed" alert (→ exit 0 with a friendly message or empty stdout instead), and a genuine breakage that returns exit 0 with empty/wrong stdout delivers silence or wrong data (→ a structure-sanity check that raises). See the *exit-1-for-benign-empty* and *200-but-wrong-page* bug-classes in bug-hunt.md. (sg-car-plate-range: a weekly LTA prefix scraper printed "No available prefixes found." + `exit 1` on a no-bid week → a scary failure alert for a routine state; the fix kept the no-curl scheduler-delivery and just corrected the exit-code/stdout contract. Validate it without sending: run the script under the gateway venv with a stubbed scrapeninja/HTTP layer and assert (exit code, first stdout line) per state — prefixes→0+msg, empty→0+friendly, blocked-page→1+stderr-only.)

## Self-posting scripts: the agent-orchestrator double-delivery trap
A `no_agent` script that self-posts to Discord (its hardcoded channel) will **also** deliver via the cron scheduler's `deliver` setting. When the *agent* runs such a script directly (user says "run it and send output to this thread"), three things happen: (1) the script posts to its hardcoded channel, (2) the agent captures stdout and sends to the requested destination, (3) the user sees duplicate content in two places — or, worse, the script's stderr/stdout mix is what the agent captures, not the formatted output.

**Fix: every self-posting script MUST support `--dry-run` (or `--no-post`)** — prints the formatted output to stdout without posting. The agent then captures stdout and routes it via `send_message` to wherever the user asked. Without this flag, the agent has no clean way to redirect output.

**Tell:** the user says "run X and send to this channel/thread" and the script has a hardcoded `DISCORD_CHANNEL_ID` / `post_results()` call. Running it full will post to the hardcoded destination AND the user's requested one.

**Pattern:**
```bash
# Agent runs with --dry-run, captures formatted output
output=$(python3 script.py "input" --dry-run 2>/dev/null)
# Agent sends to user's requested destination
send_message(target=..., message=output)
```

(food-recommender: user asked to "run food recommender and send the output to this thread" — the script posted to #alerts AND the agent sent to the thread. Added `--dry-run` flag.)

## When the agent itself misroutes: self-resolve the destination, don't hand it to `send_message`
The `--dry-run` + agent-`send_message` hand-off above assumes the agent routes to the right place. For *interactive* "reply here" delivery it often won't: a no-target `send_message` goes to the **home channel** (#alerts, see anti-patterns), and a **soft instruction** (system prompt / `agent.environment_hint`) telling the agent "reply in-thread, never #alerts" does **not** reliably override a habituated tool default — we set one and it still misfired. When an interactive skill keeps misrouting, **take the LLM out of the delivery path**: convert it to a self-delivering script the agent runs with ONE command, and have the *script* resolve the destination itself.
- **Resolve the current Discord thread from `HERMES_SESSION_KEY`** — set per-turn in `os.environ` (gateway/run.py, kept "as fallback for CLI/cron"), so a script the agent spawns inherits it. Format `agent:main:<platform>:<chat_type>:<chat_id>[:…]`; **post to `parts[4]` (chat_id)** — for a Discord thread the chat_id *is* the thread's own channel id, so `POST /channels/{chat_id}/messages` lands in the thread. **Do NOT grab `parts[5]`**: for a non-threaded `group` chat that trailing element is the **user_id** (per-user session isolation), not a channel (see the positional-key bug-class in `bug-hunt.md`).
- **Fail closed.** If you can't resolve the conversation (no session key, a non-discord key, no explicit `--to` override) → **exit non-zero**; never fall back to a fixed/home channel, or you reintroduce the misfire.
- **You can't trigger a real inbound gateway turn yourself**, so the full agent-in-the-loop path isn't tuner-testable: unit-test the resolver against real key shapes (thread / group / dm / empty), do a live send via an explicit `--to discord:<id>` override, then hand the final in-thread confirmation to the user — and say exactly that in your report.

(traffic-camera: interactive route replies kept landing in #alerts; an `environment_hint` didn't hold; the fix was a self-delivering `traffic_camera_report.py` that resolves the thread from `HERMES_SESSION_KEY` and posts the map + table itself.)

## Validate without spamming
Prove the path end-to-end **before** any real post:
```bash
# read-only: confirms token + channel/thread reachability, posts nothing
curl -sS -w '\n%{http_code}' https://discord.com/api/v10/channels/<id> \
     -H "Authorization: Bot <token>"     # expect 200; type 11 = public thread
```
Use **`curl`** for this probe, not `urllib`/`requests` with a default UA — Cloudflare 1010-blocks the bare `Python-urllib` user-agent and you'll get a 403 that masquerades as an auth failure. Then **ask the user** before sending a single live message — delivery is outward-facing and a thread post is visible to others.

- **You can confirm a *send-path change* without posting — by crafting probes the API rejects at validation while still proving your field was parsed.** When you alter how a message is sent (e.g. moving `content` from a `-F content=` form field into a multipart `payload_json` field) you don't have to do a real send to prove it works. Drive two real POSTs the server *rejects*: (1) **empty content** → Discord `400 {"code":50006,"message":"Cannot send an empty message"}` proves the server *read* your `content` field and found it empty; (2) **non-empty content gated by an invalid `message_reference`** → `400 {"code":50035 ... MESSAGE_REFERENCE_UNKNOWN_MESSAGE}` proves the content *passed* validation (no 50006) before hitting the reference gate. Neither posts anything. (sg-commute terminal gate: this settled "is `payload_json` honored for a *text-only* multipart message (no file part)?" — the load-bearing regression question for the delivery rewrite — with live server evidence instead of a doc guess or a real send.) The mirror of the read-only `GET /channels/{id}` probe: that proves reachability; this proves your *request shape* is parsed.

- **To prove a delivery-TRANSPORT change is content-preserving without a real send, stub `curl` to emit a fake `\n200` and md5 the would-be-attached file.** When you swap the *shape* of the request (`-F content=` → `--form-string payload_json=`, or add `allowed_mentions`) but must keep the *delivered* message + image byte-identical, put a stub `curl` first on `PATH` that (a) `printf`s `"{}\n200"` so the script's status check (`-w '%{http_code}'`) sees success and exercises the happy path, and (b) copies + `md5sum`s any `-F file=@<path>` arg before the script's cleanup deletes it. Diff the captured content strings + PNG md5s old-vs-new: identical md5s prove the rendered artifact is unchanged, and the logged `payload_json` proves the caption round-trips. Then flip the stub to emit `\n500` to confirm the failure path (exit non-zero + artifact retained). (token-usage 1.1.0: the `-F content=` → `payload_json` rewrite was proven byte-identical this way — the 3 PNG md5s matched the pre-change baseline exactly — with zero real Discord posts.) The complement of the reject-probe above: that proves the *server parsed* your new field; this proves the *content/artifact* didn't change.

## `MEDIA:` on the `no_agent`-deliver path vs the agent-reply path — they are NOT the same
The "never use `MEDIA:` tags" rule is about the **agent reply** path, where `extract_media()` batches all text then all images and reorders multi-image replies. The **`no_agent` cron + `deliver: discord:<id>`** path is different: the scheduler's `_deliver_result` runs `extract_media()` then `filter_media_delivery_paths()` on the script's stdout and attaches the file, so a **single trailing `MEDIA:/tmp/x.png` line is a valid, working delivery mechanism** there (one image, deterministic order). Two caveats that bite: (a) the wake-gate parses the **last non-empty stdout line** as JSON `{"wakeAgent":false}` to suppress — a `MEDIA:` line or a `*(legend)*` line is non-JSON so it always wakes, but a script whose last line could be a bare JSON dict would self-suppress; (b) the outbound media filter does **no magic-byte check** (see the `curl -o` broken-image bug-class in bug-hunt.md) — validate the bytes yourself before emitting the tag. (sg-commute morning_commute.py delivers its route map this way; confirmed against the live `cron/scheduler.py`.)

## Hardened Discord multipart: bundle content + allowed_mentions in `payload_json`
An alternative to `-F content=…` + a separate `allowed_mentions` field: send the JSON body as one `payload_json` multipart field alongside the file —
```python
payload = json.dumps({"content": content, "allowed_mentions": {"parse": []}})
cmd = ["curl","-sS","--max-time","20","-w","\n%{http_code}","-X","POST", url,
       "-H", f"Authorization: Bot {token}", "--form-string", f"payload_json={payload}"]
if img and os.path.exists(img): cmd += ["-F", f"file=@{img}"]
```
`payload_json` is honored for text-only messages too (no file part required — verified, see *Validate without spamming*). JSON-encoding `content` also sends a leading `@`/`<` literally (no need for `--form-string content=` to dodge `@file` reading), and `allowed_mentions:{"parse":[]}` rides in the same atomic request. It still **complements, not replaces, the field sanitizer** (allowed_mentions blocks only the ping; the sanitizer breaks masked links/autolinks and format-breaks). (sg-commute nearest_carpark.py `_discord_send_pair`.)

- **`-F content=<dynamic text>` is itself a bug: curl's `-F` treats `;` in the value as a form-part parameter separator and SILENTLY TRUNCATES the caption at the first `;` (and a leading `@`/`<` reads a local file).** This is the reason to prefer the `payload_json` form above for *any* caption that isn't a fixed literal — not just for mention-suppression. `-F content='Descale kettle; boil vinegar'` sends only `Descale kettle` (proven on the Pi's curl 7.88.1 via a local-socket byte capture); `-F content='hello;type=text/plain'` even injects a bogus part `Content-Type`. Reachable wherever untrusted/stored text reaches the caption (task-tracker: a task name containing `;` in `--text-only` mode truncated the whole summary). Fix: send the body as `--form-string payload_json={json.dumps(...)}` (verbatim `;`, `@`, `<`, newlines).
- **The fix MUST use `--form-string`, NOT `-F`, for `payload_json` — passing `payload_json={...}` via `-F` re-introduces the identical `;` truncation, and now it corrupts the JSON → HTTP 400.** The adversarial refuter killed the naive fix "move content into a `-F payload_json=` field": a `;` anywhere in the content truncates the JSON blob mid-string (`{"content":"Descale kettle` → invalid JSON → Discord `400`), so `-F payload_json=` is *worse* than `-F content=` (a hard reject instead of a silent drop). Only `--form-string payload_json=` (which disables `@`/`<`/`;` interpretation entirely) sends the JSON intact. Verify the on-wire bytes with `curl --trace-ascii` or a throwaway local socket before shipping — don't assume `-F` and `--form-string` behave the same. (task-tracker 1.6.0.)

## Discord message limits and partial sends (morning-brief 2026-09-15)

- **Discord's 2000-char limit is in UTF-16 code units, not Python code points.** An
  emoji/astral-dense message of 1,852 chars is 3,652 units and is rejected with code 50035 — a
  non-transient 4xx that aborts a multi-message report mid-send. Measure with
  `len(s) + sum(1 for c in s if ord(c) > 0xFFFF)` everywhere you check the limit, and when
  truncating, shrink proportionally and re-check rather than slicing by code points. Related
  trap: a word-boundary trim (`out.rsplit(" ", 1)[0]`) destroys space-sparse text — CJK, or a
  long URL/base64 token — turning 1,609 chars into 9. Only accept the word-boundary cut if it
  keeps most of the budget.
- **A FIXED summary cap does not bound the message.** `MAX_SUMMARY_CHARS` says nothing about
  the rendered headline: a real 217-char title with a 280-char URL is a 507-char headline, so a
  summary at a 1500-char cap produced a 2010-char message — on the one path that had no split
  branch. Derive each body's budget from the ACTUAL rendered length of everything else in that
  message.
- **Record delivered-state PER MESSAGE, not all-or-nothing.** The standard rule ("record only
  after all sends succeed") has a failure mode of its own: with a fatal `sys.exit` on a
  permanent error, the messages already posted are delivered and the state write never runs, so
  the ENTIRE report re-sends. Have the builder return, with each message, the items that
  message displays; on failure record exactly what landed and exit non-zero. An interrupted
  report then re-sends only what never arrived. Two corollaries: a failure on a message that
  carries no items (a footer, a trailing notice) must NOT be reported as a failed report, and
  "delivered" must mean **2xx** — treating "anything under 400" as success records items as
  seen that a 3xx interception never delivered.
- **`--dry-run` must share the message builder, or it validates nothing.** A dry run that
  prints one contiguous stream while the live path chunks and length-caps will show a healthy
  report that the real send silently truncates. One `build_messages()`, two consumers. This is
  the *only* safe validation channel for a self-posting script, so a divergence here makes the
  safe check the useless one.
- **Sanitize the LINK, not just the title.** Both come from the same untrusted feed item. A
  `>` inside the URL closes the `](<…>)` angle bracket and everything after it becomes live
  Markdown — a working masked-link injection into a trusted channel, past a title escaper that
  reviewers had already signed off. Percent-encode `<>()[]\`\\` in the URL and allow-list the
  scheme. And note the fallback: when you refuse to link, you also lose the `<>` that
  suppresses unfurling, so defang bare URLs in the plain-text branch too.
- **The free-text field is the one nobody collapses to a single line.** Titles get
  whitespace-collapsed; an LLM-written summary usually doesn't — so a newline plus
  `### Heading` plus the section-separator glyph lets it forge a whole extra section of your
  report. Collapse whitespace in every untrusted field and strip whatever glyph makes your
  section boundaries meaningful.

## Non-idempotent POSTs and `deliver: local` failure routing (foodpanda 2026-09-28)

- **Don't retry a POST after a read timeout.** See the bug-hunt class *`retry()` wrapped around a non-idempotent POST*: retry only `HTTPError` 429/503 and `URLError` (the request never fully went out); a bare `TimeoutError` from `getresponse()` or a 500/502/504 may mean the message exists. For a message that must not duplicate (a reminder that pings), send Discord's `nonce` (≤25 chars, e.g. `fp-remind-Sep2026`) + `"enforce_nonce": true` — Discord then returns the existing message for a same-nonce re-send within a few minutes — and only then retry ambiguous failures. The known cost of the conservative policy: a transient 502 on an *informational* post (a daily summary) loses that one notification; say so in the skill's docs.
- **Past the run deadline, post single-shot and post ONCE.** A per-item loop that alerts per failed item after the deadline stacks up (~15–50 s each); stop the loop at the deadline with one combined alert.
- **`hermes cron create/edit --failure-deliver <target>`** exists (per `--help`, Hermes 0.21): it overrides where FAILURE notices go, even for a `deliver: local` job — a possible scheduler-side backstop for a script whose own alert post fails. Not exercised yet; a self-alerting script plus this would double-notify, so pick one deliberately. **Configured 2026-09-28** on email-gmail-triage's three digest jobs (`hermes cron edit <id> --failure-deliver discord:<channel>` → `jobs.json` `failure_deliver`), whose script exits 1 on a failed send instead of self-alerting (the send itself is what failed); not yet seen firing.

## One-command table delivery for the interactive agent (discord-table-format 2.0, 2026-09-29)

- **`send_message` facts (tools/send_message_tool.py, 2026-09):** `target` and `message` are both required; a
  platform-only target (`"discord"`) resolves to the **home channel** and the result says "Sent to discord home channel";
  `"<text>\nMEDIA:<png>"` in ONE call sends the text as the first file's caption — one message (29/29 succeeded in the
  history). Several `MEDIA:` tags in one *final reply* are still re-batched.
- **When the agent posts for itself, give it one script, not a procedure** (`discord-table-format/scripts/send_tables.py`
  is the reference implementation) — hardening the gate added on top of the proven siblings:
  - token read from `~/.hermes/.env`, checked `[A-Za-z0-9._-]+`, passed as `curl -q … -K -` with
    `header = "Authorization: Bot …"` on **stdin** — never in argv (`ps`), and `-q` so a `~/.curlrc -v` can't echo it;
  - target = the current session (`HERMES_SESSION_KEY` parts[4]); an explicit `--to` that differs from it, or equals
    `DISCORD_HOME_CHANNEL`, is refused unless `--force-target`; only 17–20 ASCII-digit ids (`str.isdigit()` accepts
    Arabic-Indic digits); a non-Discord session fails closed;
  - `nonce` (hash of the WHOLE section + its unique title + caption + target, ≤ 25 chars) + `enforce_nonce: true` so
    an LLM re-running the command after a partial failure doesn't duplicate — verified live: the re-send returns the
    same message id and posts nothing; hashing only part of the payload silently drops a corrected re-send; `--repost`
    salts it;
  - failure classification for the caller: 4xx and curl exit 6/7 → `may_have_posted: false`; 5xx, curl 28/52/56,
    timeouts, garbled status → `true`; a 404 hints "not a channel/thread id (a message id?)";
  - the result JSON is printed once on every normal exit, Ctrl-C or internal error; a flushed per-message stderr line
    (`posted: <title> -> message <id>`) is the record if the process is killed — don't build signal handlers for this
    (three gate laps each found a new hole; see bug-hunt.md);
  - a 429 longer than ~10 s fails fast instead of stalling the agent's turn.
- **Validate it without posting:** a stub `curl` first on PATH under `env -i` with a sandbox `HOME` holding a FAKE token
  (the real token never enters the test), driven by a response sequence file (200 / 429:<s> / 500 / 404 / rc28 / rc7 /
  badutf8 / slow), asserting `token_in_argv == False`, the stdin config line, the nonce and the payload; then a read-only
  `GET /channels/<playground>` and ONE live post to the playground channel.


## Script-owned interactive delivery: chunking, nonces, deadlines, the delivered set (cc-bill-review 3.5, 2026-10-02)

- **Split, never slice.** Split a long body on paragraph → line → hard boundaries into ≤ 2000-UTF-16-unit messages (`content[:2000]` slices code points, so an emoji-dense body still exceeds the limit and is rejected; a rejected post that falls back to "print it for the agent" re-enters the relay corruption the script exists to avoid). Print the per-message hint/footer once, not per section.
- **Nonce policy depends on the command.** Deterministic seed (hash of target + seed + index + chunk) for a command the agent is told to *re-run* after a partial failure — Discord's `enforce_nonce` then returns the already-delivered messages and only the missing ones post (detect it: a returned message id whose snowflake time is older than the run = "already there"). A per-run random salt for commands whose identical output is a *legitimate repeat* (an "approve" confirmation, "show me the list again") — otherwise the second identical post silently returns the old message and the user sees nothing. Retry 5xx/timeouts only when a nonce is attached.
- **Bound interactive posts in WALL time.** urllib's `timeout` is per socket operation *and per resolved address* — five silent Discord addresses took 5 × 20 s against a 90 s budget. Run the request on a daemon thread joined with the remaining budget (performance.md's pattern), check the deadline before each message and before each retry sleep, and keep the total well inside the agent's command timeout (tell the agent the timeout to use in SKILL.md). Don't apply the deadline to the cron path (its watchdog is 600 s).
- **Record what was delivered, not what was rendered** — and only after the post: a pending (blocked) record before posting, the shown set after a full post, a blocked record after a failure; any guarded write failure is an explicit exit, never a crash or a silently stale record. Downstream commands that act on "what the user saw" (`approve all`) refuse on a blocked/foreign-thread/stale record.
- **stdout is ONE line written for the USER — it is the agent's whole reply, never an instruction to the agent** (corrected 2026-10-04: this bullet used to say "on every failure path it tells the agent the exact next step"). Build it from the facts (imported/failed with reasons, counts, the reminder status — its first clause only, since the full line may tell the user to run a command); never a hard-coded "✅ … confirmed", never merchant names, markdown or thread ids, never a ✅ for a no-op. The agent's next step on a failure lives in SKILL.md, keyed by the **exit code**, per script (cc-bill-review: import 3 → run it once more, import 4 → `/usr/bin/python3` once, approve/review → reply with the line and never re-run). Why: cc-bill-review 3.5.0 ended its lines with "Relay this; add nothing." On the first live run the agent (deepseek-flash) treated that as an instruction of unknown origin. It spent ~6 min and 59 tool calls tracing it (session_search, state.db, the Obsidian vault, leftover tune-up kits in /tmp), every call visible as tool-progress in the user's thread, then echoed "Relay this line; add nothing." to the user. The post-session curator then created a global `script-relay` skill about it.
  - **stderr counts:** the terminal tool merges stderr into the `output` the agent reads, so a `note: ignored --post 1555… (a message id?)` on stderr is agent-visible. Send notes, busy-DB details and tracebacks to the skill's log, but inside an agent turn (`HERMES_SESSION_KEY` set) make the log's own fallback a `NullHandler`, never `StreamHandler(sys.stderr)`. Keep the stderr fallback for cron, whose stderr is the job output. Set `logging.raiseExceptions = False` and silence library diagnostics (MuPDF: `pymupdf.TOOLS.mupdf_display_errors(False)`). Give each entry script a top-level crash guard AND a guarded import (a broken edit otherwise prints a traceback and no line), each printing one line.
  - **One writer:** route every reply through one `say()` that collapses whitespace. A Cloudflare HTML 503 body interpolated into the line otherwise makes it multi-line; strip tags from error details.
  - **Pin it with tests that can fail:**
    - **Static scan:** a raw-source regex misses phrases split across adjacent literals (9 of 12 reviewer mutants survived). Scanning only `say()` arguments misses text built in helpers and variables. Scan EVERY string literal in the entry scripts (AST `Constant`/`JoinedStr`, holes as `{expr}`, docstrings excepted) against a wide list: `relay`, `the user`, `re-?run`, `retry`, `run (this|it|the same)`, `again with`, `verbatim`, `nothing else`, `this line`, `from the … thread`, `your own words`, `do not paste`, `{target}`, `HERMES_SESSION`, `skill.log`. Ban every stderr writer, not just `sys.stderr`: `traceback.print_exc` and `warnings.warn` bypass a text ban. Also ban any bare `print` other than the body.
    - **Path matrix:** run every success AND failure path (post 4xx, HTML 5xx, network error, busy DB, no session, explicit message id, typo/merged id, a no-op whose post fails, the re-run dedupe branch) and assert exit code, exactly one line, no snowflake, no agent talk, empty stderr. Add `fullmatch` patterns on the main lines.
    - **The `__main__` guards only run as `__main__`:** exercise them in a subprocess (patch a dependency to raise RuntimeError and KeyboardInterrupt, with a temp DB and log), together with a broken import and an unwritable log. In-process tests of `main()` never reach the guard.
