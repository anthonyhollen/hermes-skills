"""HTTP requests under a HARD wall-clock deadline (shared by summarize.py and fetch_rss.py).

Why this module exists
---------------------
`requests`' `timeout=` is a per-socket-operation timeout, NOT a budget for the request. A
host that dribbles bytes re-arms it forever. This pipeline runs as a `no_agent` cron whose
steps are SIGKILLed at a fixed timeout, and a kill lands *before* the step writes anything —
so an unbounded request doesn't slow the brief down, it destroys it. Bounding every HTTP call
is therefore a correctness requirement, not a nicety.

Three separate traps, each of which had to be closed:

1. **The body read.** Checking a deadline between chunks does not work: `iter_content(N)`
   blocks until N bytes have arrived, so the check is never reached during a drip.
   *Fix:* a watchdog thread that calls `socket.shutdown()`, which makes the blocked read
   return. `resp.close()` alone is not enough — it drops a reference while the fd stays
   open, and the read keeps waiting (measured: a drip ran 105s against a 10s budget).

2. **The request phase** — connect, TLS, request write and response headers. None of it is
   reachable by a watchdog armed *after* `requests.get/post` returns, and a host that
   dribbles header lines just under the read timeout keeps resetting it (measured: 20s
   against a 4s budget; `http.client` accepts up to 100 header lines).
   *Fix:* run the request on a daemon thread and join it with a timeout.

3. **Compressed bodies.** `iter_content` yields only non-EMPTY *decoded* chunks, so wire
   bytes that decode to nothing never run the loop body at all. *Fix:* account for RAW bytes
   whenever a content-encoding is present, and inflate afterwards, incrementally.

This module was extracted after the terminal gate found trap 2 fixed in one caller and not
the other — the two copies of the socket helpers had already begun to diverge. One owner.
"""

import socket
import threading
import time
import zlib

import requests

__all__ = ["BoundedResponse", "TooLarge", "force_close", "request_bounded",
           "read_bounded", "inflate_bounded"]


class TooLarge(requests.RequestException):
    """Body exceeded its cap — a PERMANENT condition, not a transient blip.

    Distinguished from a network error so a caller does not re-download (and re-abort) the
    same oversize document on every retry.
    """


class BoundedResponse:
    """Minimal stand-in for a requests.Response whose body we read ourselves."""
    __slots__ = ("status_code", "content", "headers", "encoding")

    def __init__(self, status_code, content, headers, encoding):
        self.status_code = status_code
        self.content = content
        self.headers = headers
        self.encoding = encoding

    @property
    def text(self) -> str:
        enc = self.encoding or "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        import json as _json
        return _json.loads(self.text)


def _underlying_socket(resp):
    """Best-effort reach for the socket beneath a streamed requests/urllib3 response."""
    for path in (lambda: resp.raw._connection.sock,
                 lambda: resp.raw._fp.fp.raw._sock,
                 lambda: resp.raw._fp.fp._sock,
                 lambda: resp.raw._original_response.fp.raw._sock):
        try:
            sock = path()
            if sock is not None:
                return sock
        except Exception:                       # noqa: BLE001 — probing private attrs
            pass
    return None


def force_close(resp) -> None:
    """Interrupt an in-progress body read (safe to call from another thread)."""
    sock = _underlying_socket(resp)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:                       # noqa: BLE001 — already closed/EBADF
            pass
    for target in (getattr(resp, "raw", None), resp):
        try:
            if target is not None:
                target.close()
        except Exception:                       # noqa: BLE001 — best-effort teardown
            pass


def _wait_for(deadline: float, kwargs: dict) -> float:
    """Seconds to wait, always FINITE and positive.

    A caller that never arms its deadline leaves it at `inf`, and `Thread.join(inf)` /
    `Timer(inf)` raise `OverflowError: timestamp too large to convert to C _PyTime_t`.
    That is not hypothetical: `feed_doctor.py` reuses `summarize.call_model` without
    arming `_DEADLINE`, so every one of its LLM candidate-generation calls failed — the
    tool still printed a report, just with the LLM suggestions silently missing. Fall
    back to the request's own timeout plus a margin so an unarmed caller still works and
    is still bounded.
    """
    wait = deadline - time.monotonic()
    if wait == wait and wait < _JOIN_CEILING:       # finite, not NaN, and sane
        return max(0.1, wait)
    timeout = kwargs.get("timeout")
    if isinstance(timeout, (tuple, list)):
        ceiling = sum(float(t) for t in timeout if t)
    elif timeout:
        ceiling = float(timeout)
    else:
        ceiling = 60.0
    return max(0.1, min(ceiling + 5.0, _JOIN_CEILING))


def request_bounded(method: str, deadline: float, **kwargs):
    """Issue a request on a daemon thread so the pre-body phase is bounded (trap 2).

    Bounds connect, TLS, the request write and the response headers. On timeout the thread
    is abandoned — a daemon thread does not hold up interpreter exit — but a response that
    arrives late is still closed, so its socket is not leaked.
    """
    box = {}

    def _runner():
        try:
            box["resp"] = requests.request(method, **kwargs)
        except BaseException as e:              # noqa: BLE001 — handed to the caller
            box["err"] = e
        finally:
            # If we already gave up, don't leave the socket dangling.
            if box.get("abandoned") and "resp" in box:
                force_close(box["resp"])

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout=_wait_for(deadline, kwargs))
    if thread.is_alive():
        box["abandoned"] = True
        raise requests.RequestException(
            "no response headers within the budget (connect/TLS/header stall)")
    if "err" in box:
        raise box["err"]
    return box["resp"]


def inflate_bounded(body: bytes, enc: str, max_bytes: int) -> bytes:
    """Inflate gzip/deflate incrementally, stopping at max_bytes.

    Not `gzip.decompress`/`zlib.decompress` because:
      - they materialise the WHOLE output before any size check, so a small compressed body
        can balloon RAM before being rejected (1MB of wire can inflate to ~980MB);
      - `gzip.decompress` raises EOFError when the 8-byte trailer is missing (one lost TCP
        segment) — an exception that matched none of the callers' handlers, so it escaped
        their retry loops and silently downgraded every article to an excerpt.

    Multi-MEMBER gzip is handled: `decompressobj` stops at the end of the first member and
    leaves the rest in `unused_data`, whereas urllib3's decoder loops — a re-compressing
    proxy emitting concatenated members would otherwise yield a silently truncated body.
    """
    parts, remaining_cap = [], max_bytes
    data = body
    wbits_options = (31,) if enc in ("gzip", "x-gzip") else (15, -15)
    while data:
        inflated = leftover = None
        for wbits in wbits_options:
            obj = zlib.decompressobj(wbits)
            try:
                chunk = obj.decompress(data, remaining_cap + 1)
            except zlib.error:
                continue
            # A WRONG wbits does not always raise — a raw-deflate stream fed to wbits=15
            # returns b"" instead of an error, and accepting that silently produced an
            # empty body (and three wasted retries). Only a non-empty result counts as a
            # successful guess; if none of the options yields one, that is a real failure.
            if not chunk:
                continue
            inflated, leftover = chunk, obj.unused_data
            break
        if inflated is None:
            if parts:
                break                           # trailing junk after a valid member
            raise requests.RequestException("could not decode %s body" % enc)
        parts.append(inflated)
        remaining_cap -= len(inflated)
        if remaining_cap < 0:
            raise TooLarge("decompressed body exceeded %d bytes" % max_bytes)
        # Only gzip streams legitimately concatenate members.
        data = leftover if (leftover and enc in ("gzip", "x-gzip")) else b""
        wbits_options = (31,)
    return b"".join(parts)


def read_bounded(resp, deadline: float, max_bytes: int) -> BoundedResponse:
    """Read a streamed response body under `max_bytes` and a hard `deadline` (traps 1 & 3)."""
    enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
    # ANY non-identity encoding takes the raw path: an exact allow-list missed multi-value
    # headers ("gzip, gzip") that urllib3 still decodes, which reopened trap 3.
    raw_mode = bool(enc) and enc != "identity"
    chunks, total = [], 0
    # Same inf/NaN hazard as the join above: Timer(inf) raises OverflowError inside the
    # timer thread, silently leaving the read unwatched. _BODY_READ_FALLBACK is what an
    # unarmed caller gets.
    watchdog = threading.Timer(_wait_for(deadline, {"timeout": _BODY_READ_FALLBACK}),
                               force_close, args=(resp,))
    watchdog.daemon = True
    watchdog.start()
    try:
        stream = (resp.raw.stream(READ_CHUNK, decode_content=False) if raw_mode
                  else resp.iter_content(READ_CHUNK))
        for chunk in stream:
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise TooLarge("response exceeded %d bytes" % max_bytes)
            if time.monotonic() > deadline:
                raise requests.RequestException("budget exhausted mid-body")
    except (TooLarge, requests.RequestException):
        raise
    except Exception as e:                      # noqa: BLE001 — watchdog socket shutdown
        if time.monotonic() > deadline:
            raise requests.RequestException("budget exhausted mid-body")
        raise requests.RequestException("%s: %s" % (type(e).__name__, e))
    finally:
        watchdog.cancel()
        try:
            resp.close()
        except Exception:                       # noqa: BLE001
            pass

    body = b"".join(chunks)
    if raw_mode and body:
        # Decode only what we can inflate ourselves; a chained/unknown encoding (e.g. br
        # with no brotli module) is handed back as-is, which is what `requests` does when
        # it has no decoder — the caller's JSON parse then fails and it retries.
        head = enc.split(",")[-1].strip()
        if head in ("gzip", "x-gzip", "deflate"):
            body = inflate_bounded(body, head, max_bytes)
    return BoundedResponse(resp.status_code, body, resp.headers, resp.encoding)


READ_CHUNK = 1024       # small, so the in-loop deadline check runs often
# Hard ceiling for any single wait, so an unarmed (inf) deadline still yields a finite,
# bounded wait instead of an OverflowError. Comfortably above every real per-request budget.
_JOIN_CEILING = 300.0
# Body-read allowance for a caller that never armed a deadline (e.g. feed_doctor reusing
# summarize.call_model). Generous, but finite.
_BODY_READ_FALLBACK = 60.0
