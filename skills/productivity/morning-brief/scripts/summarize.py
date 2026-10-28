#!/usr/bin/env python3
"""
Step 2: Generate LLM summaries for matched articles.

Provider-agnostic: this script summarizes with **whatever model Hermes Agent is
currently configured to use** — it reads `model.default` from `~/.hermes/config.yaml`
and resolves the live provider/base_url/api_key/api_mode via Hermes'
`resolve_runtime_provider()`. There is no hardcoded provider. When you switch
models/providers (e.g. `hermes model`), this script follows on the next run with
no code change.

Input:  ~/.hermes/morning-brief/articles.json
Output: ~/.hermes/morning-brief/articles_summarized.json

Must-read     -> 3-5 sentence summary
Maybe relevant -> exactly 2 sentence summary

Transport: uses only `requests` (the cron interpreter /usr/bin/python3 has no
openai/anthropic SDK). Two wire formats are supported, selected by api_mode:
  - "anthropic_messages" -> POST {base_url}/v1/messages   (e.g. MiniMax /anthropic)
  - "chat_completions"   -> POST {base_url}/chat/completions  (OpenAI-compatible)
"""

import asyncio
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from article_id import MIN_CONTENT_CHARS   # noqa: E402 — single owner for the guard
from bounded_http import (TooLarge, read_bounded,   # noqa: E402
                          request_bounded)

BASE_DIR    = os.path.expanduser("~/.hermes/morning-brief")
INPUT_FILE  = os.path.join(BASE_DIR, "articles.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "articles_summarized.json")

# Hermes Agent source tree — provides the config + runtime-provider resolvers.
# These import cleanly under /usr/bin/python3 (they do NOT require the openai SDK).
HERMES_AGENT_DIR = os.path.expanduser("~/.hermes/hermes-agent")

CONCURRENT_LIMIT = 8           # = max articles per run (3 must-read + 5 maybe), so
                               # all summaries issue in a single concurrent wave
REQUEST_TIMEOUT  = 45          # per-socket read timeout for one POST
CONNECT_TIMEOUT  = 10          # separate connect timeout (a dead host fails fast)
# Hard wall-clock budget for ALL summarization, enforced inside the retry loop.
#
# Without it the retry arithmetic overran the wrapper's 200s step timeout: with the
# original 60s read timeout, 3 attempts budgeted 184.5s, and a provider that 400s
# slowly (triggering the feature-downgrade re-post) measured 243.7s on a real
# 8-article run. REQUEST_TIMEOUT is 45s now, but the budget is what guarantees it.
# Because the step is SIGKILLed *before* it writes anything, blowing the budget doesn't
# degrade the brief — it destroys it. With the deadline, a hung provider instead falls
# through to extractive summaries and the brief still ships. bounded_http extends the
# same deadline over the request phase AND the body read, where a dripping provider hides.
STEP_BUDGET_SECONDS = 150      # < STEP_TIMEOUT["summarize.py"] (200s) with margin to write
MAX_TOKENS = {                 # must cover reasoning tokens + the visible summary
    # deepseek-flash began spending ~1,000-1,550 hidden reasoning tokens per summary around
    # 2026-09-27; at 1,500 a newsletter hit finish_reason=length with 0 visible chars
    # (probed on the Pi: content 0 / reasoning 1,500; at 4,000 it finished with 455
    # chars). Unused max_tokens is not billed, so the headroom is free.
    "must_read":      4000,
    "maybe_relevant": 4000,
}
# How much article text to send per task type. The stored `content` is already capped
# at 2000 chars by fetch_rss, so the old `content[:3000]` slice was a no-op that only
# looked like a limit. A 2-sentence summary needs far less source text than a 3-5
# sentence one. (Trim the prompt slice, never the stored field — `content` also feeds
# scoring, the paywall guard and the extractive fallback.)
PROMPT_CONTENT_CHARS = {
    "must_read":      2000,
    "maybe_relevant": 1200,
}

# Retry an empty/failed summary call before giving up (#1). Today's "Summary
# unavailable" came from an intermittent empty model response — a single retry
# recovers it. Permanent rejections (handled inside call_model) are not looped.
SUMMARY_RETRIES         = 2     # extra attempts after the first
SUMMARY_BACKOFF_SECONDS = 1.5   # linear backoff between attempts
# Below this content length there's nothing to summarize OR extract — paywall teaser.
# Same threshold fetch_rss scores against; owned by article_id so the two can't drift.
MIN_SUMMARIZABLE_CHARS  = MIN_CONTENT_CHARS

# Inline reasoning wrapper emitted by some chat_completions models (stripped from
# the visible content). Anthropic-mode reasoning arrives as separate "thinking"
# blocks and is dropped during extraction instead.
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# A model truncated by max_tokens mid-thought emits an OPENING <think> with no closing
# tag. THINK_RE (non-greedy, close tag required) leaves it untouched, so the raw
# reasoning was non-empty, passed the "did we get text?" check without a retry, and was
# delivered verbatim to Discord. Drop an unterminated block too — but ONLY when it
# starts the response, which is the truncated-reasoning shape. Matching it anywhere
# swallowed legitimate prose: on an LLM-news brief, "Developers must strip the <think>
# tag…" lost everything after "the" (caught at the terminal gate).
UNTERMINATED_THINK_RE = re.compile(r"^\s*<think>(?!.*</think>).*\Z",
                                   re.DOTALL | re.IGNORECASE)


MAX_STRIP_CHARS = 40_000        # THINK_RE is O(n²) in the number of open tags

def strip_reasoning(text: str) -> str:
    """Remove complete <think>…</think> blocks AND an unterminated leading one.

    Input is length-capped first: THINK_RE is O(n²) in the count of `<think>` tokens
    (~24s at 140KB, ~95s at 280KB) and a real summary is ~2KB, so a body that large is junk
    not worth the step's budget. (UNTERMINATED_THINK_RE is `^`-anchored without MULTILINE,
    so it can only match at position 0 and is linear.)
    """
    text = (text or "")[:MAX_STRIP_CHARS]
    return UNTERMINATED_THINK_RE.sub("", THINK_RE.sub("", text)).strip()


# HTTP statuses worth retrying. A 401/403/404 will fail identically on every attempt —
# the old loop retried them 3× anyway (contradicting its own docstring), costing 3
# round-trips and 3× the timeout exposure per article on a bad API key. Everything 5xx
# IS retried, including the non-standard edge statuses a real deployment meets: 520-524
# (Cloudflare origin errors), 529 (Anthropic's documented `overloaded_error`), 598. An
# earlier version of this fix used a fixed allow-list and silently lost those.
RETRYABLE_CLIENT_HTTP = {408, 409, 429}

def _is_retryable(status: int) -> bool:
    return status >= 500 or status in RETRYABLE_CLIENT_HTTP

# Optional payload features, switched off if the provider rejects them with a 400.
# PROCESS-WIDE, not per-call. This does NOT make the first wave cheap: all 8 articles issue
# together, so on a run where the provider rejects `temperature` each of the 8 discovers it
# concurrently and re-posts (~16 POSTs for 8 articles, measured). What it buys is that the
# discovery isn't repeated by every later call in the process, and that a provider rejecting
# BOTH optional params still converges. Per-call state cost a re-post per article forever.
#
# Sharing it across the 8 concurrent workers needs care, and a first version of this
# change got it wrong: all 8 articles issue in one wave, so when the winner flipped a
# flag to False the other 7 evaluated "is this flag still on?" as False, skipped their
# own downgrade re-post, and returned "" — 7 of 8 summaries lost (measured 1/8 vs 8/8).
# The fix is to compare against a SNAPSHOT taken before the request: a 400 is worth
# re-posting whenever the effective feature set has changed since this attempt's
# payload was built, whether this thread changed it or another one did.
_FEAT = {"temperature": True, "thinking": True}

# Wall-clock deadline for all model calls, set once in main(). A one-element list so
# call_model reads the live value without a global statement.
_DEADLINE = [float("inf")]

RETRY_AFTER_CAP = 15.0          # never wait longer than this on a 429
# A summary response is ~2KB. 1MB was 2.5x too permissive: the O(n²) <think> regexes
# below take ~24s on a 140KB body and ~95s on 280KB, so a single oversized-but-accepted
# response could burn the whole step in post-processing. 200KB is 100x real headroom.
MAX_RESPONSE_BYTES = 200_000
READ_CHUNK = 1024               # small so the in-loop deadline check runs often


def _error_text(resp) -> str:
    """The provider's error message only — not the whole 400 body.

    Sniffing the raw body for a feature name misfires on a 400 that ECHOES the request
    (content-filter rejections commonly do), which permanently disabled `temperature`
    AND `thinking` for every other article in the run — including the `thinking:
    disabled` guard that exists to stop a reasoning model returning empty text. Fall
    back to the raw text only when there is no JSON error envelope to read.
    """
    try:
        data = resp.json()
    except Exception:                           # noqa: BLE001
        return resp.text or ""
    if not isinstance(data, dict):
        return resp.text or ""
    err = data.get("error")
    if isinstance(err, dict):
        return " ".join(str(err.get(k, "")) for k in ("message", "param", "code", "type"))
    if isinstance(err, str):
        return err
    for key in ("message", "detail", "error_msg"):
        if isinstance(data.get(key), str):
            return data[key]
    return resp.text or ""

def _sleep_retry_after(resp, attempt: int, deadline: float) -> None:
    """Honour a 429's Retry-After (body or header, capped), else linear backoff.

    Re-hammering a rate-limited provider on a fixed cadence is what turns one 429 into
    three; and the wait is clipped to the remaining step budget so it can't push the
    step past its timeout.
    """
    wait = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("retry_after", "retryAfter"):
                if body.get(key) is not None:
                    wait = float(body[key])
                    break
    except (ValueError, TypeError):
        pass
    if wait is None:
        hdr = resp.headers.get("Retry-After")
        if hdr:
            try:
                wait = float(hdr)
            except ValueError:
                wait = None
    if wait is None:
        wait = SUMMARY_BACKOFF_SECONDS * (attempt + 1)
    time.sleep(max(0.0, min(wait, RETRY_AFTER_CAP, deadline - time.monotonic())))


# ── Resolve the active Hermes model/provider ───────────────────────────────────

def resolve_hermes_runtime() -> dict:
    """Return the live model + transport Hermes is configured to use.

    {"model", "base_url", "api_key", "api_mode"} — sourced from config.yaml
    (model.default) and Hermes' resolve_runtime_provider() (handles OAuth tokens,
    credential pools, and provider fallbacks). HERMES_INFERENCE_MODEL overrides
    the model name if set, matching Hermes' own precedence.
    """
    if HERMES_AGENT_DIR not in sys.path:
        sys.path.insert(0, HERMES_AGENT_DIR)
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
    except Exception as e:
        raise RuntimeError(
            f"Could not import Hermes config from {HERMES_AGENT_DIR} ({e}). "
            f"summarize.py defers to Hermes' configured model — ensure the "
            f"hermes-agent source tree is present."
        )

    cfg = load_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        model = model_cfg.get("default") or model_cfg.get("model")
    elif isinstance(model_cfg, str):
        model = model_cfg
    else:
        model = None
    model = os.environ.get("HERMES_INFERENCE_MODEL", "").strip() or model

    rt = resolve_runtime_provider()
    base_url = (rt.get("base_url") or "").rstrip("/")
    api_key  = rt.get("api_key") or ""
    api_mode = rt.get("api_mode") or "chat_completions"

    if not model:
        raise RuntimeError("No model configured (config.yaml model.default is empty).")
    if not base_url:
        raise RuntimeError(f"No base_url resolved for provider {rt.get('provider')!r}.")

    return {"model": model, "base_url": base_url, "api_key": api_key, "api_mode": api_mode}


# ── LLM call (provider-agnostic) ───────────────────────────────────────────────

def _extract_anthropic(data: dict) -> str:
    """Join visible text blocks; ignore 'thinking' (reasoning) blocks.

    Defensive to the same degree as _extract_chat: a malformed-but-200 body (content
    as a bare string, or a list of strings) used to raise AttributeError — which is
    neither a RequestException nor in call_model's caught tuple, so it escaped the
    retry loop entirely. Latent while api_mode is chat_completions; live the moment
    the provider is switched back to an anthropic one.
    """
    if not isinstance(data, dict):
        return ""
    blocks = data.get("content")
    if not isinstance(blocks, list):
        return ""
    # strip_reasoning as well: a provider that inlines <think> into a text block would
    # otherwise ship it verbatim on this wire (_extract_chat already strips it).
    return strip_reasoning("".join(
        b.get("text", "") or "" for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ))

def _extract_chat(data: dict) -> str:
    """Pull choices[0].message.content; strip any inline <think> reasoning.

    Defensive against a malformed-but-200 response (missing/empty "choices", or an
    error envelope with no choices): returns "" instead of raising KeyError/
    IndexError, so call_model treats it as an empty response and retries."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):   # some providers return content as blocks
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return strip_reasoning(content)

def call_model(runtime: dict, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
    """Single chat turn against the active provider. Returns the visible text, or ""
    if it can't get one after retries.

    Hardening (2026-06-01):
      - #2 Disables extended reasoning on the anthropic wire (`thinking: disabled`):
        summarization needs no reasoning, and a reasoning model can otherwise spend
        the whole token budget thinking and return an EMPTY text block (the cause of
        today's "Summary unavailable"). Verified MiniMax-M3 honours `disabled` and
        returns text-only. Dropped automatically if the provider 400s on it.
        NOTE: this branch only exists on the anthropic wire — under `chat_completions`
        (the live mode) there is no such switch, so a reasoning model can still burn
        its budget. The bound there is the extractive fallback, not this flag.
      - #1 Retries on an empty response or a transient error (SUMMARY_RETRIES) with
        backoff — a single retry recovered today's two failures.
      - Still drops `temperature` if the provider rejects it (pre-existing).

    Hardening (2026-09-14):
      - A wall-clock `deadline` bounds every attempt, each request's read timeout is
        clipped to the time remaining, and the body is read under that same deadline
        (bounded_http), so this can't overrun the wrapper's step timeout and lose the
        whole brief to a SIGKILL before the write.
      - Retries are `_is_retryable()`: any 5xx (incl. Cloudflare 520-524 and
        Anthropic's 529) plus 408/409/429. A 401/403/404 fails fast instead of burning
        three timeouts.
      - A 429's `Retry-After` is honoured (capped) instead of the fixed backoff.
      - The feature-downgrade state is process-wide, so a provider that rejects
        `temperature` costs ONE extra POST per run rather than one per article; the
        downgrade loop compares against what was actually SENT so it is correct both
        across concurrent workers and when more than one feature is rejected.
    """
    model, base_url, api_key, api_mode = (
        runtime["model"], runtime["base_url"], runtime["api_key"], runtime["api_mode"],
    )
    deadline = _DEADLINE[0]

    def _post(read_timeout: float):
        if api_mode == "anthropic_messages":
            url = base_url + "/v1/messages"
            headers = {
                "Authorization":     f"Bearer {api_key}",
                "Content-Type":      "application/json",
                "anthropic-version": "2023-06-01",
                # Identity encoding: a summary body is ~2KB, so compression buys
                # nothing, and a compressed drip is harder to bound (see bounded_http).
                # Belt-and-braces — read_bounded still handles a server that
                # compresses anyway.
                "Accept-Encoding":   "identity",
            }
            payload = {
                "model":      model,
                "max_tokens": max_tokens,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_prompt}],
            }
            if _FEAT["thinking"]:
                payload["thinking"] = {"type": "disabled"}
        else:  # chat_completions (OpenAI-compatible)
            url = base_url + "/chat/completions"
            headers = {
                "Authorization":   f"Bearer {api_key}",
                "Content-Type":    "application/json",
                "Accept-Encoding": "identity",
            }
            payload = {
                "model":      model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
            }
        if _FEAT["temperature"]:
            payload["temperature"] = 0.3
        # request_bounded + read_bounded put the WHOLE call under `deadline` — see
        # bounded_http for why a per-socket timeout=/an inter-chunk check is not enough.
        return read_bounded(
            request_bounded("POST", deadline, url=url, headers=headers, json=payload,
                            stream=True, timeout=(CONNECT_TIMEOUT, read_timeout)),
            deadline, MAX_RESPONSE_BYTES)

    extract = _extract_anthropic if api_mode == "anthropic_messages" else _extract_chat
    last_err = "empty response"
    for attempt in range(SUMMARY_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            last_err = "step budget exhausted"
            break
        try:
            used = dict(_FEAT)            # what THIS payload was built with
            resp = _post(min(REQUEST_TIMEOUT, remaining))
            # Downgrade loop: at most one pass per optional feature. A single pass was
            # not enough — when a provider rejects BOTH `temperature` and `thinking`,
            # the re-post's own 400 was never re-examined, and (with the fast-fail
            # below) the call returned "" instead of eventually disabling the second
            # flag: 0 of 8 summaries on the anthropic wire, where the pre-tune-up code
            # got 8 of 8 by looping through raise_for_status.
            for _ in range(len(_FEAT)):
                if resp.status_code != 400:
                    break
                # Disable whichever optional param the provider rejects, then re-post.
                # Only consider "thinking" on the wire that actually sends it —
                # otherwise a chat-mode 400 whose text merely contains the word
                # re-posts a byte-identical request for nothing.
                low = _error_text(resp).lower()
                if _FEAT["temperature"] and "temperature" in low:
                    _FEAT["temperature"] = False
                if (api_mode == "anthropic_messages" and _FEAT["thinking"]
                        and "thinking" in low):
                    _FEAT["thinking"] = False
                # Re-post if the effective feature set differs from what we actually
                # SENT — comparing against `used` rather than against a fresh read of
                # _FEAT is what covers the concurrent case, where another worker had
                # already downgraded before we built our payload (all 8 workers issue
                # in one wave, and the naive check lost 7 of 8 summaries).
                if _FEAT == used:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    print("[WARN] summary call abandoned mid-downgrade: step budget "
                          "exhausted", file=sys.stderr)
                    return ""
                used = dict(_FEAT)
                resp = _post(min(REQUEST_TIMEOUT, remaining))
            if resp.status_code >= 400 and not _is_retryable(resp.status_code):
                print(f"[WARN] summary call rejected (HTTP {resp.status_code}, not "
                      f"retryable): {resp.text[:200]}", file=sys.stderr)
                return ""
            if resp.status_code == 429:
                last_err = "HTTP 429"
                if attempt < SUMMARY_RETRIES:     # no point sleeping after the last try
                    _sleep_retry_after(resp, attempt, deadline)
                continue
            if resp.status_code >= 400:
                # Retryable by _is_retryable() at this point (non-retryable returned
                # above); surface it to the retry loop.
                raise requests.RequestException("HTTP %d" % resp.status_code)
            text = extract(resp.json())
            if text:
                return text
            last_err = "empty response"
        except TooLarge as e:
            # Permanent for this response — retrying re-downloads the same oversize body.
            print(f"[WARN] summary response rejected: {e}", file=sys.stderr)
            return ""
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            # Malformed-but-200 body: resp.json() failed to decode, or the response
            # shape was unexpected. Treat as a transient bad response and retry
            # rather than letting it escape the retry loop. (ValueError covers
            # json.JSONDecodeError; AttributeError covers a content field that is a
            # bare string where a list of blocks was expected.)
            last_err = f"bad response: {type(e).__name__}: {e}"
        if attempt < SUMMARY_RETRIES:
            time.sleep(min(SUMMARY_BACKOFF_SECONDS * (attempt + 1),
                           max(0.0, deadline - time.monotonic())))
    print(f"[WARN] summary call failed after {SUMMARY_RETRIES + 1} attempts: {last_err}",
          file=sys.stderr)
    return ""


_GREETING_RE = re.compile(
    r"^(hi|hello|hey|good (morning|evening))\b|^welcome\b|^listen to\b|^sponsored\b", re.I)

def _extractive_fallback(content: str, category: str) -> str:
    """Clean sentence-boundary extract for when the model can't produce a summary (#3).

    Better than the old `content[:200] + "..."` hard cut, which sliced mid-word and
    then rendered as "Summary unavailable" — the reader got nothing. This returns the
    first 2 (maybe-relevant) or 3 (must-read) whole sentences so they at least get the
    gist. Marked via `summary_is_extractive` so send_brief shows it instead of
    suppressing it as a failed synthesis.
    """
    text = re.sub(r"\s+", " ", content).strip()
    if not text:
        return ""
    # Abbreviation-safe splitter (the naive one ended "Welcome to Installer No." at "No."),
    # and skip greeting/boilerplate openers ("Hi, friends!") and scraps under 25 chars.
    every = _sentences(text)
    kept = [x for x in every if not _GREETING_RE.match(x)] or every
    sentences = [x for x in kept if len(x) >= 25] or kept
    n = 3 if category == "must_read" else 2
    extract = " ".join(s.strip() for s in sentences[:n]).strip()
    if len(extract) > 600:                      # guard against a single runaway sentence
        extract = extract[:600].rsplit(" ", 1)[0] + "…"
    return extract


# The model sees only the RSS lede; on a short one it narrates its own input ("…though the
# provided excerpt did not include a launch date", PetaPixel must-read, 2026-09-26). Tell
# it not to, and trim what still does. Deliberately NOT a higher content floor: breaking
# items often have short ledes (Techmeme ~300 chars).
#
# The trim is narrow on purpose — it fires only on a reference to the model's INPUT
# ("the provided excerpt", "the given text") in the same sentence as an absence verb
# ("did not include", "omits", "no details"). "Penguin shared the excerpt with reviewers"
# and "the text provided to regulators" are news. It does not split after abbreviations
# ("U.S.", "Dr.", "2."), so a sentence ENDING in one is joined to the next — if that next
# one is meta, both go (rare: the prompt forbids meta). It never empties a summary.
_NO_META_RULE = ("Summarize only what the text says; never mention the excerpt, the "
                 "provided text or content, or what it does not include.")
# Requires an input qualifier: a bare "the excerpt" is too often the news itself
# ("Penguin shared the excerpt with reviewers but did not include the ending").
_META_INPUT_RE = re.compile(
    r"\b(provided|given|supplied) (excerpt|snippet|text|content|article|passage)\b",
    re.I)
_ABSENCE_RE = re.compile(
    r"\b(does|did|do) not\b|\b(doesn|didn|don)[’']t\b|\bomit(s|ted)?\b|\black(s|ed)?\b"
    r"|\bno (further |specific |additional )?(details?|information|mention|specifics)\b"
    r"|\bnot (specified|mentioned|included|provided|disclosed|stated|detailed)\b", re.I)
_META_CLAUSE_RE = re.compile(
    r"\s*[,;]\s*(though|although|but|however|while)\b[^.!?]*$", re.I)
_ABBREV = {"u.s", "u.k", "e.u", "dr", "mr", "mrs", "ms", "st", "jr", "sr", "inc", "corp",
           "vs", "no", "fig", "approx", "etc", "e.g", "i.e"}

def _sentences(line: str) -> list:
    """Split on . ! ? + space + capital, but not after an abbreviation, an initial
    ("J. R."), or a list marker ("2." as the whole segment so far). A sentence that
    merely ENDS in a number or version ("…ships in 2027.", "…Z9 II.") still splits."""
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s+[A-Z“\"'(])", line):
        seg = line[start:m.start()]
        prev = re.search(r"([\w.]+)$", seg)
        raw = prev.group(1) if prev else ""
        tok = raw.lower().rstrip(".")
        if (tok in _ABBREV or (len(raw) == 1 and raw.isupper())
                or re.fullmatch(r"\s*\d{1,2}", seg)):
            continue
        out.append(line[start:m.end()].strip())
        start = m.end()
    tail = line[start:].strip()
    if tail:
        out.append(tail)
    return out

def _is_meta(sent: str) -> bool:
    return bool(_META_INPUT_RE.search(sent) and _ABSENCE_RE.search(sent))

def _drop_meta_sentences(summary: str) -> str:
    if not summary or not _META_INPUT_RE.search(summary):
        return summary
    out = []
    for line in summary.split("\n"):
        kept = []
        for sent in _sentences(line):
            if not _is_meta(sent):
                kept.append(sent)
                continue
            body, end = (sent[:-1], sent[-1]) if sent[-1] in ".!?" else (sent, "")
            m = _META_CLAUSE_RE.search(body)
            if m and not _is_meta(body[:m.start()]) and _is_meta(body[m.start():]):
                kept.append(body[:m.start()].rstrip() + (end or "."))
            # else: the whole sentence is about the input — drop it
        out.append(" ".join(k for k in kept if k))
    trimmed = "\n".join(line for line in out if line).strip()
    return trimmed or summary        # never empty a summary — the original beats nothing


def summarize_article(article: dict, runtime: dict) -> dict:
    """Summarize one article with the active model. Returns the updated dict."""
    category = article.get("_category", "maybe_relevant")
    content  = article.get("content", "") or ""
    title    = article.get("title", "")
    source   = article.get("source", "")

    if category == "must_read":
        system_prompt = (
            "You are a news summarizer. Write a clear, factual 3-5 sentence summary "
            "of the article below. Include key facts, numbers, dates, and implications. "
            "Do not start with 'This article'. Do not add a title or label. "
            + _NO_META_RULE
        )
    else:
        system_prompt = (
            "You are a news summarizer. Write exactly 2 sentences summarizing this article. "
            "Be concise and factual. Include key facts and implications. "
            "Do not start with 'This article'. " + _NO_META_RULE
        )
    # The URL is deliberately NOT sent: the model never refers to it (it is told to
    # produce prose only, and the link is rendered by send_brief from the stored
    # field), so it was ~144 chars of dead weight in every prompt.
    user_prompt = (
        f"Article title: {title}\n"
        f"Source: {source}\n"
        f"Article content:\n{content[:PROMPT_CONTENT_CHARS.get(category, 1200)]}"
    )

    try:
        summary = call_model(runtime, system_prompt, user_prompt, MAX_TOKENS.get(category, 1500))
    except Exception as e:
        print(f"[WARN] LLM failed for '{title[:60]}...': {e}", file=sys.stderr)
        summary = ""

    summary = _drop_meta_sentences(summary)
    if summary:
        article["summary"] = summary
    elif len(content) < MIN_SUMMARIZABLE_CHARS:
        # Genuinely too little text to summarize or extract — paywall teaser.
        article["summary"] = "Summary unavailable — article may be paywalled."
    else:
        # Model couldn't summarize but we have real content — show a clean extract (#3)
        # rather than nothing. Flag it so send_brief displays it as-is.
        article["summary"] = _extractive_fallback(content, category)
        article["summary_is_extractive"] = True

    return article


# ── Async wrapper with concurrency limit ───────────────────────────────────────

async def summarize_all(articles: list, runtime: dict) -> list:
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    executor  = ThreadPoolExecutor(max_workers=CONCURRENT_LIMIT)

    async def gated(art):
        async with semaphore:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(executor, summarize_article, art, runtime)

    try:
        return await asyncio.gather(*[gated(a) for a in articles], return_exceptions=True)
    finally:
        executor.shutdown(wait=True)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    # Arm the wall-clock budget before anything can call the model, so the retry loop
    # can never overrun the wrapper's step timeout (which SIGKILLs before the write).
    _DEADLINE[0] = time.monotonic() + STEP_BUDGET_SECONDS
    try:
        runtime = resolve_hermes_runtime()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Using Hermes model: {runtime['model']} "
          f"(api_mode={runtime['api_mode']}, base_url={runtime['base_url']})")

    try:
        with open(INPUT_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[ERROR] input not found: {INPUT_FILE} (did fetch_rss.py run?)", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[ERROR] {INPUT_FILE} is not valid JSON ({e}) — a partial/corrupt write.",
              file=sys.stderr)
        sys.exit(1)
    must_read = data.get("must_read", [])
    maybe_rel = data.get("maybe_relevant", [])
    print(f"Must-read articles to summarize: {len(must_read)}")
    print(f"Maybe-relevant articles to summarize: {len(maybe_rel)}")

    all_articles = must_read + maybe_rel

    print(f"\nCalling LLM (concurrent limit: {CONCURRENT_LIMIT})...")
    results = await summarize_all(all_articles, runtime)

    processed_mr, processed_may = [], []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"[WARN] Exception for article {i}: {r}", file=sys.stderr)
            art = all_articles[i]
            # Route this through the SAME extractive fallback as an ordinary model
            # failure. The old raw `content[:200]` slice was never flagged
            # summary_is_extractive, so send_brief's echo-detector recognised it as a
            # copy of the content and replaced it with "Summary unavailable" — the
            # reader got nothing, while the intended path gives a readable excerpt.
            # (`or ""` because a null content would make the slice a TypeError here,
            # crashing main() before the output file is written.)
            art_content = art.get("content", "") or ""
            art["summary"] = _extractive_fallback(
                art_content, art.get("_category", "maybe_relevant"))
            art["summary_is_extractive"] = True
            if not art["summary"]:
                art["summary"] = "Summary unavailable — article may be paywalled."
                art["summary_is_extractive"] = False
            r = art
        if i < len(must_read):
            processed_mr.append(r)
        else:
            processed_may.append(r)

    out = {
        "total_scanned":       data.get("total_scanned", 0),
        "skipped":             data.get("skipped", 0),
        "matched_and_dropped": data.get("matched_and_dropped", 0),
        "must_read":           processed_mr,
        "maybe_relevant":      processed_may,
        "held_retellings":     data.get("held_retellings", []),   # passed through to send_brief
    }

    # Atomic write (temp + fsync + os.replace). A plain open("w") truncates first and
    # json.dump streams many small writes, so a SIGKILL — or a serialization error
    # partway through — left a corrupt articles_summarized.json behind that persisted
    # until the next successful run. Matches seen_articles.json / feed_health.json.
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUTPUT_FILE)

    print(f"\n✓ Saved to {OUTPUT_FILE}")
    print(f"  {len(processed_mr)} must-read summarized")
    print(f"  {len(processed_may)} maybe-relevant summarized")


if __name__ == "__main__":
    asyncio.run(main())
