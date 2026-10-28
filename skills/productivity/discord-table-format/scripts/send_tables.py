#!/usr/bin/env python3
"""
Render table sections AND post them to Discord — one command, one message per
section (caption + image in the SAME message, in order).

This is the agent's delivery path for tables: it replaces the multi-call
render -> send_message -> send_message -> rm sequence and the MEDIA: tags that
the gateway re-batches. Rendering is render_tables.py, imported in-process.

Usage:
  send_tables.py [--to discord:<chat_id>[:<thread_id>] [--force-target]] [--dry-run] [--repost] [sections.json]
  (sections JSON on stdin when no file is given — use a quoted heredoc)

Sections: the render_tables.py format, plus an optional "caption" per section
(message text; default "**<title>**"). A section with no rows is not posted.

Target: the current conversation (HERMES_SESSION_KEY), or --to. Never a fixed or
home channel: no target -> exit 2, nothing posted. --to may not point away from
the current conversation, or at the home channel, without --force-target (only
when the user asked for that channel).

Duplicates: each message carries a nonce derived from (target, title, caption,
rows) with enforce_nonce, so Discord returns the existing message instead of
posting again if the same section is re-sent within a few minutes. --repost
makes the nonces unique (to post an identical table again on purpose).

Killed by a signal (SIGTERM/SIGKILL, e.g. a terminal timeout) the process just dies: the flushed
"posted: <title> -> message <id>" lines on stderr are the record of what landed (re-sending those
within a few minutes is deduplicated by the nonce).

stdout: one JSON line {"target", "posted", "message_ids", "empty", "failed", "unsent"}
(+ "dry_run": true / "would_post" under --dry-run). failed = {"title", "detail",
"may_have_posted"}. Exit 0 = every non-empty section posted; 1 = a post failed or
the run aborted (stops there, keeps order; re-send only failed + unsent);
2 = nothing posted: bad input / no or refused target / no token.
"""
import argparse, hashlib, json, math, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_tables  # noqa: E402

API = "https://discord.com/api/v10/channels/{}/messages"
ENV_FILE = "~/.hermes/.env"
MAX_CONTENT = 2000          # Discord limit, in UTF-16 code units
SEND_GAP_S = 0.5            # Discord allows 5 messages / 5 s / channel
MAX_RETRY_AFTER_S = 10      # a longer 429 wait fails fast instead of stalling the turn
_SNOWFLAKE = re.compile(r"[1-9][0-9]{16,19}")  # no leading zero: must compare equal as a string
_TOKEN_OK = re.compile(r"[A-Za-z0-9._-]+")


def _env_value(name):
    """A value from ~/.hermes/.env (the scheduler/terminal don't export the bot token).
    Tolerates a BOM, `export `, quotes and inline ` # comments`; the last assignment wins."""
    value = None
    try:
        with open(os.path.expanduser(ENV_FILE), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.lstrip("\ufeff").strip()
                line = re.sub(r"^export\s+", "", line)
                key, sep, v = line.partition("=")
                if not sep or key.strip() != name:
                    continue
                v = v.strip()
                if v[:1] in ("'", '"') and v[:1] in v[1:]:
                    v = v[1:v.index(v[0], 1)]
                else:
                    v = re.split(r"\s#", v, maxsplit=1)[0].strip()
                value = v or None
    except OSError:
        return None
    return value


def session_target():
    """The current Discord conversation's channel/thread id, or None.

    agent:main:discord:<chat_type>:<chat_id>[:...] — parts[4] is the channel/thread
    to post into (for threads parts[4] == parts[5] == the thread id); for group
    chats parts[5] is a USER id, never a channel. A non-Discord session -> None.
    """
    key = os.environ.get("HERMES_SESSION_KEY", "")
    parts = key.split(":")
    if len(parts) >= 5 and parts[2] == "discord" and parts[4]:
        return parts[4]
    if key or os.environ.get("HERMES_SESSION_PLATFORM", "discord") != "discord":
        return None
    return (os.environ.get("HERMES_SESSION_THREAD_ID", "").strip()
            or os.environ.get("HERMES_SESSION_CHAT_ID", "").strip() or None)


def explicit_target(to):
    """The id from --to discord:<ch>[:<thread>] (the last id wins), or None."""
    ref = to.strip()
    if ref.startswith("discord:"):
        ref = ref[len("discord:"):]
    elif ":" in ref:
        return None  # another platform's target
    parts = [p for p in ref.split(":") if p]
    return parts[-1] if parts else None


def _utf16_len(s):
    return len(s) + sum(1 for c in s if ord(c) > 0xFFFF)


def cap_content(s, limit=MAX_CONTENT):
    """Fit Discord's limit (UTF-16 units): cut at the exact budget, add an ellipsis; never empty."""
    if not s.strip():
        return "​"
    if _utf16_len(s) <= limit:
        return s
    out, used = [], 0
    for c in s:
        w = 2 if ord(c) > 0xFFFF else 1
        if used + w > limit - 1:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"


def make_nonce(target, section, caption, salt=""):
    """Same target + same section (title, headers, rows, bold_match, …) + same caption -> same
    nonce, so only a true re-send is deduplicated; any change to the table posts anew."""
    blob = json.dumps([target, section, caption, salt], ensure_ascii=True, sort_keys=True, default=str)
    return "dtf" + hashlib.sha256(blob.encode()).hexdigest()[:22]   # Discord: <= 25 chars


def discord_post(token, channel, content, img=None, nonce=None, _retried=False):
    """One message: content + optional image in ONE multipart request.

    Returns (ok, detail, message_id, may_have_posted). Retries once on a short 429
    (the message was not created). Never retries a timeout or 5xx: the POST may
    have landed (the nonce makes a later re-send of the same section safe).
    """
    body = {"content": cap_content(content), "allowed_mentions": {"parse": []}}
    if nonce:
        body.update(nonce=nonce, enforce_nonce=True)
    payload = json.dumps(body)
    # -q first: ignore ~/.curlrc. --form-string, not -F: -F treats ';' in the value
    # as a part separator (truncates the JSON) and a leading '@'/'<' as a file.
    # The token goes in on stdin (-K -), never argv, so it can't show up in `ps`.
    cmd = ["curl", "-q", "-sS", "--max-time", "30", "-w", "\n%{http_code}", "-K", "-",
           "-X", "POST", API.format(channel),
           "--form-string", f"payload_json={payload}"]
    if img:
        cmd += ["-F", f"file=@{img};type=image/png"]
    try:
        r = subprocess.run(cmd, input=f'header = "Authorization: Bot {token}"\n',
                           capture_output=True, text=True, errors="replace", timeout=35)
    except FileNotFoundError:
        return False, "curl not found", None, False
    except subprocess.TimeoutExpired:
        return False, "curl timed out", None, True
    except OSError as e:
        return False, f"curl {type(e).__name__}", None, False
    resp, _, code = r.stdout.rpartition("\n")
    code = code.strip()
    if code == "429" and not _retried:
        try:
            wait = float(json.loads(resp).get("retry_after", 1))
        except (ValueError, TypeError, AttributeError):
            wait = 1.0
        if not math.isfinite(wait):
            wait = float("inf")
        elif wait < 0:
            wait = 1.0
        if wait > MAX_RETRY_AFTER_S:
            return False, f"http=429 rate limited (retry_after {min(wait, 99999):.0f}s)", None, False
        time.sleep(wait + 0.25)
        return discord_post(token, channel, content, img, nonce, _retried=True)
    if r.returncode == 0 and code.startswith("2"):
        try:
            mid = str(json.loads(resp).get("id") or "") or None
        except (ValueError, AttributeError):
            mid = None
        return True, f"http={code}", mid, True
    detail = f"http={code or '?'} rc={r.returncode} body={(resp or r.stderr)[:200]!r}"
    if code == "404":
        detail += " — not a channel/thread id (a message id?)"
    # 4xx (incl. an exhausted 429) and connect/DNS failures (curl 6, 7) created nothing;
    # a 5xx, an empty/garbled status or a mid-transfer curl error may have.
    may = not (code.startswith("4") or r.returncode in (6, 7))
    return False, detail, None, may


def main(argv=None):
    sys.stdout.reconfigure(errors="backslashreplace")  # a lone surrogate in a title must not kill the report
    ap = argparse.ArgumentParser(description="Render table sections and post them to Discord.")
    ap.add_argument("file", nargs="?", help="sections JSON file (default: stdin)")
    ap.add_argument("--to", help="discord:<chat_id>[:<thread_id>] (default: the current conversation)")
    ap.add_argument("--force-target", action="store_true",
                    help="allow --to to differ from the current conversation / be the home channel")
    ap.add_argument("--dry-run", action="store_true", help="render and resolve the target, post nothing")
    ap.add_argument("--repost", action="store_true", help="unique nonces: post an identical table again")
    args = ap.parse_args(argv)

    try:
        if args.file and args.file != "-":
            with open(args.file) as f:
                payload = json.load(f)
        else:
            payload = json.load(sys.stdin)
    except (OSError, ValueError) as e:
        print(f"send_tables: error: cannot read sections: {e}", file=sys.stderr)
        return 2
    sections = payload["sections"] if isinstance(payload, dict) and "sections" in payload else payload
    if isinstance(sections, dict):
        sections = [sections]
    if not isinstance(sections, list) or not all(isinstance(s, dict) for s in sections):
        print("send_tables: error: expected a JSON list of section objects", file=sys.stderr)
        return 2
    bad = next((i for i, s in enumerate(sections) if not isinstance(s.get("caption"), (str, type(None)))), None)
    if bad is not None:
        print(f"send_tables: error: section {bad} ({sections[bad].get('title')!r}): caption must be a string",
              file=sys.stderr)
        return 2

    here = session_target()
    target = explicit_target(args.to) if args.to is not None else here
    if not target or not _SNOWFLAKE.fullmatch(target):
        print(f"send_tables: error: no Discord target (got {target!r}) — run inside a Discord "
              f"conversation or pass --to discord:<channel_id>[:<thread_id>]. Nothing was posted.",
              file=sys.stderr)
        return 2
    home = _env_value("DISCORD_HOME_CHANNEL")
    if not args.force_target and args.to is not None and (
            (here and target != here) or (home and target == home and target != here)):
        why = "the home channel" if home and target == home else f"not this conversation ({here})"
        print(f"send_tables: error: --to {target} is {why}. Omit --to to post here, or add "
              f"--force-target if the user asked for that channel. Nothing was posted.", file=sys.stderr)
        return 2
    token = None if args.dry_run else _env_value("DISCORD_BOT_TOKEN")
    if not args.dry_run and not (token and _TOKEN_OK.fullmatch(token)):
        print("send_tables: error: no usable DISCORD_BOT_TOKEN in ~/.hermes/.env", file=sys.stderr)
        return 2

    result = {"target": target, "posted": [], "message_ids": {}, "empty": [], "failed": None, "unsent": []}
    if args.dry_run:
        result.update(dry_run=True, would_post=result.pop("posted"))
    done = result["would_post"] if args.dry_run else result["posted"]
    salt = str(time.time()) if args.repost else ""
    paths, current, attempting = None, None, False
    try:
        try:
            paths = render_tables.render_sections(sections)  # validates everything before writing
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as e:
            print(f"send_tables: error: {e}", file=sys.stderr)
            return 2
        for i, title in enumerate(paths):  # render_sections preserves input order
            path = paths[title]
            if path is None:
                result["empty"].append(title)
                continue
            if result["failed"]:
                break
            caption = sections[i].get("caption")
            caption = f"**{title}**" if caption is None else caption
            if args.dry_run:
                done.append(title)
                print(f"[dry-run] would post to {target}: {cap_content(caption)[:80]!r} + {path}", file=sys.stderr)
                continue
            if done:
                time.sleep(SEND_GAP_S)
            nonce = make_nonce(target, [title, sections[i]], caption, salt)  # titles are unique
            current, attempting = title, True
            ok, detail, mid, may = discord_post(token, target, caption, path, nonce)
            if ok:
                result["message_ids"][title] = mid
                done.append(title)
                current, attempting = None, False
                print(f"posted: {title!r} -> message {mid}", file=sys.stderr, flush=True)
                try:
                    os.remove(path)  # only after a confirmed 2xx
                except OSError:
                    pass
            else:
                result["failed"] = {"title": title, "detail": detail, "may_have_posted": may}
                current, attempting = None, False
    except BaseException as e:  # Ctrl-C or a bug: still report what already landed
        result["aborted"] = f"{type(e).__name__}: {e}"
        if current is not None and current not in done and not result["failed"]:
            result["failed"] = {"title": current, "detail": f"aborted: {result['aborted']}",
                                "may_have_posted": attempting}
    finally:
        if paths is not None:  # rendering happened, so a report is owed — print it exactly once
            failed = (result["failed"] or {}).get("title")
            for t in paths:
                if t in done or t == failed:
                    continue
                if paths[t] is None:
                    if t not in result["empty"]:
                        result["empty"].append(t)
                else:
                    result["unsent"].append(t)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    if paths is None:  # interrupted while rendering: nothing was posted
        print(f"send_tables: aborted before posting ({result.get('aborted')}). Nothing was posted.", file=sys.stderr)
        return 2
    return 1 if (result["failed"] or result.get("aborted")) else 0


if __name__ == "__main__":
    sys.exit(main())
