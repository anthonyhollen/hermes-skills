# Env-Key Loading Pattern

Skills that need API keys (Google Maps, Discord, etc.) often break when the agent's
`terminal()` tool runs them as subprocesses — `~/.hermes/.env` is loaded by the
gateway at startup but NOT sourced by each terminal session. The result: `os.environ.get("KEY")`
returns `None` even though the key is in `.env`.

## The pattern: `_load_env_key()`

```python
import os

def _load_env_key(name):
    """Load a key from environment or ~/.hermes/.env file."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

API_KEY = _load_env_key("GOOGLE_API_KEY")
```

The function tries env first (fast, already loaded), then falls back to parsing `.env`
directly. Returns empty string if neither works — callers check and fail with a clear
message rather than sending an empty key to the API.

## When to use

Any script that:
- Runs via `terminal()` tool (not via gateway subprocess)
- Needs an API key that lives in `~/.hermes/.env`
- Currently uses `os.environ.get("KEY")` and breaks when the env isn't sourced

## When NOT to use

- Scripts that only run as cron jobs (the scheduler loads the profile's `.env`)
- Scripts that only run under the gateway's subprocess env (inherits the gateway's loaded vars)
- Scripts where the key can be passed as a CLI argument (`--api-key`)

## Interaction with argparse defaults

If a script uses `argparse` with `--api-key` defaulting to a module constant, and that
constant is now loaded at import time via `_load_env_key()`, subprocess callers (like
`traffic_camera_report.py` calling `commute_camera_map.py`) don't need to pass `--api-key`
explicitly — the child inherits the parent's env AND the fallback reads `.env` in both
processes. Redundant but safe.

## Import-time dependency validation

A related pattern from the same tune-up: validate dependencies at script startup,
**before** incurring API costs. If a script fetches a route (Google Directions →
API cost), downloads camera data (API call), then crashes on `import PIL`, the user
paid for the API calls with no delivery. Move import validation to `main()` entry,
before the first API call:

```python
def main():
    # Validate dependencies BEFORE incurring API costs
    try:
        from PIL import Image, ImageDraw, ImageFont
        from tabulate import tabulate
    except ImportError:
        try:
            sys.path.insert(0, "/usr/lib/python3/dist-packages")
            from PIL import Image, ImageDraw, ImageFont
            from tabulate import tabulate
        except ImportError as e:
            print(f"ERROR: missing dependency — {e}", file=sys.stderr)
            sys.exit(1)
    # ... API calls go here
```

Note the `/usr/lib/python3/dist-packages` fallback: on Debian/Pi, system packages
install there, and the gateway venv has user-site disabled. The fallback catches
the case where `PIL` is system-installed but the venv can't see it normally.
