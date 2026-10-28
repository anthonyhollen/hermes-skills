#!/usr/bin/env python3
"""vault_audit_cron.py — no_agent cron entry for the vault-audit skill.

The cron runner invokes a bare script name with no args, so this thin wrapper
runs the engine in the weekly mode: apply auto-fixable drift and self-post the
report to the Discord home channel ONLY when there is something to report.
Manual/on-demand runs should call vault_audit.py directly (safe-by-default: no
edits, no posting).

The job is `deliver: local`: the scheduler only SAVES stdout to
cron/output/<job>/ and never pushes it, so the full report is printed (no
--quiet) — that file is the record of what was fixed. Exit codes: a routine week
with review items (engine 1) exits 0; a failed post (3) or internal error (4)
stays non-zero so `hermes cron list` shows a real failure as one.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hermes v0.22.0 (2026-10-14) runs no_agent scripts on its Python 3.14 dependency venv,
# which has no PyYAML (the engine imports yaml at the top). Append the scripts venv's
# site-packages, as the other wrappers do — PyYAML is pure python and loads on 3.14.
_YAML_SP = os.path.expanduser("~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages")
if _YAML_SP not in sys.path:
    sys.path.append(_YAML_SP)

try:
    import vault_audit  # noqa: E402
    rc = vault_audit.main(["--fix", "--post-home"])
except Exception:
    print("[vault-audit] cron wrapper crashed:\n" + traceback.format_exc(), flush=True)
    rc = 4
sys.exit(0 if rc in (0, 1) else rc)
