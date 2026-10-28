#!/usr/bin/env python3
"""
Nightly DeepSeek skip report — no_agent cron job (23:30 SGT), delivered to the
Discord playground channel. Replaced the skip-check digest on 2026-10-12 (the
user trusts the skip logic; job bdbb183faf82 is paused).

Rebuilds the derived skip signals (as the digest did, so the 01:20 DeepSeek
batch learns from the evening's listening), then prints what share of the
DeepSeek songs the listener skipped — today, per day and since DeepSeek
started — against the artist-graph discovery benchmark (src/llm_report.py).
Prints NOTHING when no DeepSeek song was finished or skipped today (empty
stdout = no message). DB only: no Spotify or DeepSeek calls.
"""
import os, sys, traceback

SKILL_SRC = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/src"
DB_PATH   = "/home/amh/.hermes/skills/media/spotify-playlist-refresh/spotify-refresh/spotify.db"


def main():
    sys.path.insert(0, SKILL_SRC)
    # PyYAML from the scripts venv (pure-python; importable on any interpreter).
    _YAML_SP = os.path.expanduser("~/.hermes/scripts/.venv-scripts/lib/python3.11/site-packages")
    if _YAML_SP not in sys.path:
        sys.path.append(_YAML_SP)
    import db, skip_signals, llm_report
    db.DB_PATH = DB_PATH
    db.init_db()
    skip_signals.rebuild_signals()
    out = llm_report.build_report()
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        print(f"⚠️ DJ Hermes DeepSeek skip report failed: {type(e).__name__}: {e}")
        sys.exit(1)
