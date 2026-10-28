#!/bin/bash
# mac_pull.sh - keep the Mac clone current with GitHub (launchd job
# com.anthonyhollen.hermes-skills-pull, every 10 min). Fast-forward only: it never
# merges, and it skips the run while there are local edits or unpushed commits.
set -u
cd "$(dirname "$0")/.." || exit 1
log() { echo "$(date '+%F %T') $*"; }

git fetch -q --tags --force origin || { log "fetch failed"; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { log "skip: not on main"; exit 0; }
[ -z "$(git status --porcelain)" ] || { log "skip: local changes"; exit 0; }
[ "$(git rev-list --count origin/main..main)" = 0 ] || { log "skip: unpushed commits"; exit 0; }
behind=$(git rev-list --count main..origin/main)
[ "$behind" = 0 ] && exit 0
git merge -q --ff-only origin/main && log "pulled $behind commit(s), now at $(git log -1 --format='%h %s')"
