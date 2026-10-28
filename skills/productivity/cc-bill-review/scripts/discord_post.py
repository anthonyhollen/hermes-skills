#!/usr/bin/env python3
"""Byte-safe Discord delivery for cc-bill-review.

Why this exists: an agent-relayed review loses backslash escapes (2026-08-20:
the escaped `SMP\\*\\*JUNG` reached Discord as `SMP**JUNG`, corrupting the bold
parsing), so any message whose markdown correctness matters (reviews, approval
confirmations, the merchant list) is POSTed here via the REST API directly —
the exact bytes the script emits are what Discord stores. The no-agent reminder
cron (`~/.hermes/scripts/cc_payment_reminder.py`) imports this module too.

What it guarantees:
  * Target = the current Discord conversation (`HERMES_SESSION_KEY`, exported by
    the gateway into every command the agent runs). An explicit id is used only
    when there is no session (cron / manual runs). The id agents tend to pass is
    the *triggering message* id, which equals the thread id only for the message
    that started the thread — so the session wins when the two differ.
  * Long bodies are split on line boundaries into messages of <= 2000 UTF-16
    code units (Discord's real limit — emoji count 2), never truncated.
  * Every message carries a nonce + `enforce_nonce`, so re-running the same
    command within a few minutes returns the existing message instead of
    posting a duplicate; that is also what makes it safe to retry a 5xx/timeout.
  * `send_discord()` never raises — it returns (ok, detail, message_id,
    may_have_posted).
"""
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request

HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
API = "https://discord.com/api/v10/channels/{}/messages"
MAX_UNITS = 2000          # Discord content limit, in UTF-16 code units
SEND_GAP_S = 0.5          # Discord allows 5 messages / 5 s / channel
MAX_RETRY_AFTER_S = 10    # a longer 429 wait fails instead of stalling the turn
_SNOWFLAKE = re.compile(r"[1-9][0-9]{16,19}")
NO_MENTIONS = {"parse": []}


def discord_token() -> str | None:
    """DISCORD_BOT_TOKEN from ~/.hermes/.env (not exported to scripts), or None."""
    value = None
    try:
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line = re.sub(r"^export\s+", "", line.lstrip("﻿").strip())
                key, sep, v = line.partition("=")
                if sep and key.strip() == "DISCORD_BOT_TOKEN":
                    value = v.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return value


def session_target() -> str | None:
    """The current Discord conversation's channel/thread id, or None.

    agent:main:discord:<chat_type>:<chat_id>[:...] — parts[4] is the channel or
    thread to post into (for a thread parts[4] == the thread id; for a group
    chat parts[5] is a USER id, never a channel). Non-Discord session -> None.
    """
    key = os.environ.get("HERMES_SESSION_KEY", "")
    parts = key.split(":")
    if len(parts) >= 5 and parts[2] == "discord" and _SNOWFLAKE.fullmatch(parts[4]):
        return parts[4]
    if key:
        return None
    for name in ("HERMES_SESSION_THREAD_ID", "HERMES_SESSION_CHAT_ID"):
        v = os.environ.get(name, "").strip()
        if _SNOWFLAKE.fullmatch(v):
            return v
    return None


def resolve_target(explicit: str | None, force: bool = False) -> tuple[str | None, str]:
    """Pick where to post. Returns (target, note). target None = refuse.
    `force` (the scripts' --force-target) makes a valid explicit id win over the
    session — only for when the user explicitly asked for another channel or
    the session variables are known to be stale."""
    explicit = (explicit or "").strip()
    if explicit.startswith("discord:"):
        explicit = explicit.split(":")[-1]
    if force and _SNOWFLAKE.fullmatch(explicit):
        return explicit, ""
    sess = session_target()
    if sess:
        if explicit and explicit != sess:
            return sess, (f"note: ignored --post {explicit} (not this conversation's "
                          f"thread — a message id?); posting to {sess}")
        return sess, ""
    if explicit and _SNOWFLAKE.fullmatch(explicit):
        return explicit, ""
    if explicit:
        return None, f"--post {explicit!r} is not a Discord channel/thread id"
    return None, ("no Discord conversation to post to (no HERMES_SESSION_KEY and no "
                  "--post <channel_id>)")


def utf16_len(s: str) -> int:
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


def _hard_split(line: str, limit: int) -> list[str]:
    out, cur, used = [], [], 0
    for c in line:
        w = 2 if ord(c) > 0xFFFF else 1
        if used + w > limit:
            out.append("".join(cur))
            cur, used = [], 0
        cur.append(c)
        used += w
    if cur:
        out.append("".join(cur))
    return out


def _pack(pieces: list[str], sep: str, limit: int) -> list[str]:
    """Greedily join pieces (each already <= limit) with sep into <= limit chunks."""
    out, cur = [], ""
    for p in pieces:
        cand = f"{cur}{sep}{p}" if cur else p
        if cur and utf16_len(cand) > limit:
            out.append(cur)
            cur = p
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def split_message(content: str, limit: int = MAX_UNITS) -> list[str]:
    """Split into chunks of <= limit UTF-16 units without losing text: whole
    paragraphs (blank-line separated) where possible, else whole lines, else a
    hard split of an over-long line. Never returns an empty chunk."""
    content = content.strip("\n")
    if not content.strip():
        return []
    if utf16_len(content) <= limit:
        return [content]
    pieces = []
    for para in re.split(r"\n\s*\n", content):
        if not para.strip():
            continue
        if utf16_len(para) <= limit:
            pieces.append(para)
            continue
        lines = []
        for line in para.split("\n"):
            lines.extend(_hard_split(line, limit) if utf16_len(line) > limit else [line])
        pieces.extend(_pack(lines, "\n", limit))
    return [c for c in _pack(pieces, "\n\n", limit) if c.strip()]


def make_nonce(*parts) -> str:
    """Same inputs -> same nonce (<= 25 chars), so only a true re-send dedupes."""
    blob = json.dumps(parts, ensure_ascii=True, sort_keys=True, default=str)
    return "ccbr" + hashlib.sha256(blob.encode()).hexdigest()[:21]


def _urlopen_bounded(req, timeout: float, deadline: float | None) -> bytes:
    """urlopen + read, bounded in WALL time when a deadline is set: urllib's timeout
    is per socket operation and per resolved address (5 silent Discord addresses ->
    5 x the timeout), so the request runs on a daemon thread joined with the
    remaining budget. A late answer is abandoned — the nonce makes a re-send safe."""
    if deadline is None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    box: dict = {}

    def run():
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                box["data"] = resp.read()
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            box["exc"] = exc
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(max(1.0, deadline - time.monotonic()))
    if th.is_alive():
        raise TimeoutError("posting deadline reached while waiting for Discord")
    if "exc" in box:
        raise box["exc"]
    return box["data"]


def send_discord(channel_id: str, content: str, *, nonce: str | None = None,
                 allowed_mentions: dict | None = None, deadline: float | None = None,
                 _attempt: int = 0):
    """POST one message (content must already fit). Never raises.

    Returns (ok, detail, message_id, may_have_posted). Any 2xx is success (a
    non-JSON 2xx body still means Discord accepted it). 429 is retried after
    Retry-After (<= MAX_RETRY_AFTER_S). With a nonce, a 5xx / timeout / network
    error is retried up to twice — Discord returns the existing message for a
    repeated nonce, so a retry can't duplicate. Without a nonce those are NOT
    retried (the POST may have landed).
    """
    token = discord_token()
    if not token:
        return False, "DISCORD_BOT_TOKEN not found in ~/.hermes/.env", None, False
    body = {"content": content,
            "allowed_mentions": NO_MENTIONS if allowed_mentions is None else allowed_mentions}
    if nonce:
        body.update(nonce=nonce, enforce_nonce=True)
    req = urllib.request.Request(
        API.format(channel_id), data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/NousResearch/hermes-agent, 1.0)"})
    retry_wait = None
    timeout = 20.0
    if deadline is not None:
        timeout = max(3.0, min(timeout, deadline - time.monotonic()))
    try:
        raw = _urlopen_bounded(req, timeout, deadline)
        try:
            mid = str((json.loads(raw or b"{}") or {}).get("id") or "") or None
        except (ValueError, AttributeError):
            mid = None
        return True, "ok", mid, True
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            text = ""
        if exc.code == 429 and _attempt < 2:
            try:
                wait = float(json.loads(text or "{}").get("retry_after", 1))
            except (ValueError, TypeError, AttributeError):
                wait = 1.0
            wait = wait if math.isfinite(wait) and wait >= 0 else 1.0
            if wait > MAX_RETRY_AFTER_S:
                return False, f"HTTP 429 rate limited (retry_after {wait:.0f}s)", None, False
            retry_wait = wait + 0.25
        elif exc.code >= 500 and nonce and _attempt < 2:
            retry_wait = 2.0 * (_attempt + 1)
        else:
            detail = f"HTTP {exc.code}: {text}"
            if exc.code == 404:
                detail += " — not a channel/thread id (a message id?)"
            return False, detail, None, exc.code >= 500
    except Exception as exc:  # noqa: BLE001 — timeout / network / TLS / bad response
        if nonce and _attempt < 2:
            retry_wait = 2.0 * (_attempt + 1)
        else:
            refused = isinstance(exc, urllib.error.URLError) and isinstance(
                getattr(exc, "reason", None), ConnectionRefusedError)
            return False, f"{type(exc).__name__}: {exc}", None, not refused
    if deadline is not None and time.monotonic() + retry_wait + 3 > deadline:
        return False, "gave up: posting deadline reached", None, True
    time.sleep(retry_wait)
    return send_discord(channel_id, content, nonce=nonce, allowed_mentions=allowed_mentions,
                        deadline=deadline, _attempt=_attempt + 1)


_DISCORD_EPOCH_MS = 1420070400000


def _snowflake_ms(mid) -> int | None:
    try:
        return (int(mid) >> 22) + _DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return None


POST_DEADLINE_S = 90   # an interactive post must finish well inside the agent's command timeout


def post_messages(target: str, content: str, *, seed: str = "",
                  allowed_mentions: dict | None = None,
                  deadline_s: float = POST_DEADLINE_S) -> dict:
    """Split `content` and post every chunk, in order, stopping at the first
    failure. Returns {"ok", "posted", "total", "message_ids", "detail",
    "may_have_posted", "already_there"}. Re-running with the same
    content/target/seed within a few minutes is deduplicated by the per-chunk
    nonces: Discord returns the EXISTING message, counted in "already_there"
    (its id is older than this run), so the caller can say so."""
    chunks = split_message(content)
    res = {"ok": True, "posted": 0, "total": len(chunks), "message_ids": [],
           "detail": "", "may_have_posted": False, "already_there": 0}
    started_ms = int(time.time() * 1000)
    deadline = time.monotonic() + deadline_s
    for i, chunk in enumerate(chunks):
        if i:
            if time.monotonic() + SEND_GAP_S + 5 > deadline:
                res.update(ok=False, may_have_posted=False,
                           detail=f"gave up before message {i + 1}/{len(chunks)}: "
                                  f"posting deadline reached")
                break
            time.sleep(SEND_GAP_S)
        ok, detail, mid, may = send_discord(
            target, chunk, nonce=make_nonce(target, seed, i, chunk),
            allowed_mentions=allowed_mentions, deadline=deadline)
        if not ok:
            res.update(ok=False, detail=f"message {i + 1}/{len(chunks)}: {detail}",
                       may_have_posted=may)
            break
        res["posted"] += 1
        res["message_ids"].append(mid)
        ms = _snowflake_ms(mid)
        if ms is not None and ms < started_ms - 10_000:
            res["already_there"] += 1
    return res
