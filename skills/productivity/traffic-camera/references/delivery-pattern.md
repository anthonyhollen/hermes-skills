# Discord Delivery Pattern

**Never** use `hermes send` or `MEDIA:` tags from scripts. Use direct REST multipart.

## Why

- `hermes` is not on PATH in non-login shells (cron/agent terminal)
- `MEDIA:` tags cause `extract_media()` to batch all text, then all images — text↔image pairing and order are lost

## Pattern: one `curl` POST per message with content + optional image

```python
import os, subprocess, time, json

def load_token():
    with open(os.path.expanduser("~/.hermes/.env")) as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None

def discord_post(token, cid, content, img=None):
    """content + optional image in ONE request. Returns True on 2xx."""
    import json as _json
    payload = _json.dumps({"content": content, "allowed_mentions": {"parse": []}})
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    cmd = ["curl", "-sS", "--max-time", "20", "-w", "\n%{http_code}",
           "-X", "POST", url, "-H", f"Authorization: Bot {token}",
           "-F", f"payload_json={payload}"]
    if img and os.path.exists(img):
        cmd += ["-F", f"file=@{img}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False
    body, _, code = r.stdout.rpartition("\n")
    if code.strip() == "429":
        try: time.sleep(float(json.loads(body).get("retry_after", 1)) + 0.25)
        except Exception: time.sleep(1)
        return discord_post(token, cid, content, img)
    return r.returncode == 0 and code.strip().startswith("2")
```

## Rules

- Pass argv as a list (no shell) — a shell turns `\n` in content into literal backslash-n
- `time.sleep(0.5)` between sends — Discord allows 5 messages / 5 s / channel
- Check HTTP status, retry once on 429
- Cap content to 2000 chars (Discord limit)
- Validate without spamming: `GET /channels/{id}` first, ask before live send
