#!/bin/sh
# Run the Spotify sampler under the install's committed dependency environment.
# Resolves the environment at start from the install's runtime facts, so Hermes
# env rebuilds (which change the environment directory hash) don't break this unit.
set -eu
FACTS=$(ls /home/amh/.hermes/installs/*/facts.json 2>/dev/null | head -1)
[ -n "$FACTS" ] || { echo "spotify_sampler_run: no runtime facts found" >&2; exit 78; }
ENV_DIR=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['packages']['venv']['environment'])" "$FACTS")
exec "$ENV_DIR/bin/python" -u /home/amh/.hermes/scripts/spotify_sampler.py
