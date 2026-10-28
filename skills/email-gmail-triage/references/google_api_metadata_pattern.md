# Google API Metadata Fetcher — Reusable Heal/Recovery Tool

The `google_api.py` wrapper at
`~/.hermes/skills/productivity/google-workspace/scripts/google_api.py`
is a CLI around the Google API client. It's invoked via
`subprocess.run([sys.executable, GAPI, "gmail", "search"|"get", ...])` and
returns JSON on stdout.

**For one-shot batch operations that need to fetch metadata for many IDs
(messages, files, calendar events, etc.) at once, the in-process
googleapiclient path is faster and more reliable.** Use the pattern below
instead of looping `google_api.py gmail get {id}` in shell.

## Pattern: In-process batch fetch with service-rebuild retry

```python
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import googleapiclient.errors
from pathlib import Path

TOKEN_PATH = Path.home() / ".hermes" / "google_token.json"
_service = None

def gmail_service():
    """Singleton Gmail service. Force-rebuild by setting _service = None."""
    global _service
    if _service is not None:
        return _service
    creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service

def get_messages_metadata(msg_ids, fmt="metadata", headers=None):
    """Fetch metadata for many IDs. Auto-retries empty-headers on token race."""
    if not msg_ids: return []
    svc = gmail_service()
    results = []
    for mid in msg_ids:
        try:
            msg = svc.users().messages().get(
                userId="me", id=mid, format=fmt,
                metadataHeaders=headers or ["From", "Subject"]
            ).execute()
            results.append(_parse(msg))
        except googleapiclient.errors.HttpError as e:
            if e.resp.status == 404:
                continue  # message deleted; skip
            raise
    return results

def get_messages_metadata_with_retry(msg_ids):
    """Wrapper that retries IDs whose from/subject came back empty.
    Defeats the OAuth token-refresh race that strips payload.headers
    on large batches."""
    results = get_messages_metadata(msg_ids)
    empty = [r["id"] for r in results
             if not r.get("from") or not r.get("subject")]
    if empty:
        global _service
        _service = None  # force re-build
        retry = {r["id"]: r for r in get_messages_metadata(empty)}
        results = [retry.get(r["id"], r) for r in results]
    return results
```

## When to use the CLI vs. in-process

**CLI (`google_api.py gmail get {id}`):**
- One-off checks
- Debugging — easier to copy/paste into a terminal
- Already what existing scripts (`gmail_triage.py`, `gmail_report.py`) call

**In-process (`gmail_service()` + `messages().get()`):**
- Batch fetches (≥5 IDs)
- Recovery scripts that need to process hundreds of entries
- Anywhere you need the OAuth token-refresh retry pattern

## OAuth token-refresh race — symptom & fix

When fetching metadata for >20 IDs in a tight loop, the OAuth token can
refresh mid-batch. The `googleapiclient` service singleton holds a stale
credential reference, so subsequent calls silently return messages with
empty `payload.headers` even though `payload.labelIds` works fine.

**Symptom:** ~3% of IDs come back with `from=""` and `subject=""`,
clustered at the END of the batch. Labels/categorization still work.

**Fix:** after a batch fetch, detect empty-header IDs, force the singleton
service to None, and re-fetch only those. The retry succeeds because the
second call rebuilds credentials from disk.

**Always include this pattern in any code path that fetches metadata for
many IDs at once.** See `scripts/gmail_state_heal_once.py` for a working
example.
