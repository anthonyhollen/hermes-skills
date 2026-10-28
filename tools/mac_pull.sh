#!/bin/bash
# mac_pull.sh - keep the Mac clone (~/hermes-skills) current with GitHub.
#
# launchd job com.anthonyhollen.hermes-skills-pull runs an INSTALLED COPY of this file,
# ~/Library/Scripts/hermes-skills-pull.sh, never the one in the repo, so a push can't
# change what runs on the Mac unreviewed. After reviewing a change here, re-install:
#   cp tools/mac_pull.sh ~/Library/Scripts/hermes-skills-pull.sh
#
# Fast-forward only: never merges, and skips the run while there are uncommitted
# changes to tracked files, unpushed commits, or a branch other than main checked out.
set -u
REPO="$HOME/hermes-skills"
LOG="$HOME/Library/Logs/hermes-skills-pull.log"
GIT=/usr/bin/git
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=2"
export GIT_OPTIONAL_LOCKS=0   # don't take index.lock for status; an agent may be committing

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
# Keep the log small (about the last 1000 lines).
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ] && tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

cd "$REPO" || { log "repo missing: $REPO"; exit 1; }
$GIT fetch -q --tags --force --prune origin 2>>"$LOG" || { log "fetch failed (offline?)"; exit 0; }
[ "$($GIT rev-parse --abbrev-ref HEAD)" = main ] || { log "skip: not on main"; exit 0; }
# Untracked files are fine: merge --ff-only refuses to overwrite one.
[ -z "$($GIT status --porcelain --untracked-files=no)" ] || { log "skip: uncommitted changes"; exit 0; }
[ "$($GIT rev-list --count origin/main..main)" = 0 ] || { log "skip: unpushed commits"; exit 0; }
behind=$($GIT rev-list --count main..origin/main)
[ "$behind" = 0 ] && exit 0
if $GIT merge -q --ff-only origin/main 2>>"$LOG"; then
  log "pulled $behind commit(s), now at $($GIT log -1 --format='%h %s')"
else
  log "fast-forward failed (an untracked file in the way?)"
fi
