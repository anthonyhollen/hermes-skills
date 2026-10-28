# Scheduler no_agent Script Execution — Technical Notes

**Date:** May 2026  
**Affected:** All `no_agent: true` cron jobs running `.py` scripts that depend on system packages (PIL, etc.)

---

## How the scheduler runs no_agent scripts

The scheduler (scheduler.py) runs `.py` scripts via `sys.executable` directly:

```python
# scheduler.py ≈ line 786
subprocess.run([sys.executable, script_path], ...)
```

This is a **direct exec** — no shell, no env resolution, no `PATH` lookup. The invoked Python gets a minimal `sys.path` that does NOT include `/usr/lib/python3/dist-packages`.

**Contrast with `.sh` wrapper scripts:** When the script is a `.sh` file, the scheduler runs it through bash (`#!/usr/bin/env python3`), which does env resolution and inherits the shell's `PATH`. System packages are found.

**Root cause of PIL failure:**
- `token_report_dispatch.py` (`.py`) → scheduler uses `sys.executable` → PIL not found
- `token_report_no_agent.py` with `sys.path.insert(0, "/usr/lib/python3/dist-packages")` → works

---

## The fix — sys.path.insert MUST be before all imports

```python
# Lines 12-14 of token_report_no_agent.py — MUST be before ALL imports
import sys
_system_pkgs = "/usr/lib/python3/dist-packages"
if _system_pkgs not in sys.path:
    sys.path.insert(0, _system_pkgs)

import os, subprocess, glob, time
from PIL import Image, ImageDraw, ImageFont  # ← now works
```

`sys.path.insert` must come **before** `import os` because the subsequent imports (PIL, dotenv) need the path set up first.

---

## Why [SILENT] suppresses delivery

`SILENT_MARKER = "[SILENT]"` in scheduler.py. The scheduler checks:
```python
if SILENT_MARKER in deliver_content.strip().upper():
    # suppress Discord delivery
```

For `no_agent` scripts, the captured stdout becomes `deliver_content`. `[SILENT]` as the absolute first line (with `flush=True`) ensures it reaches the scheduler's stdout capture before anything else.

**Critical rules:**
1. `[SILENT]` must be the very first output — not after any print statements
2. `flush=True` ensures it writes to the pipe immediately
3. All Discord sends must use `subprocess.DEVNULL` so curl output doesn't pollute stdout
4. `os._exit(0)` hard-terminates — `sys.exit()` allows atexit handlers to produce final output that bypasses suppression

---

## Bot token is not passed to no_agent scripts

The scheduler does NOT pass `DISCORD_BOT_TOKEN` via `os.environ` to `no_agent` scripts. Read from `.env` file directly:

```python
ENV_FILE = os.path.expanduser("~/.hermes/.env")  # NOT hermes-agent/.env — stale token there
TOKEN = None
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("DISCORD_BOT_TOKEN="):
                TOKEN = line.split("=", 1)[1].strip()
                break
```

---

## Test vs production routing

| Destination | Channel ID | Thread ID |
|---|---|---|
| Test (playground) | `1499972245437550602` | `1501742338639401071` |
| Production (work home) | `1499972245437550602` | **(none — direct channel)** |

For no_agent scripts, hardcode the channel ID in the script. The `deliver` field in the cron job is for LLM output; no_agent scripts send directly via curl.