#!/bin/sh
# Runs on the host (not in the container, which has no docker access).
# Triggered periodically by rebuild-watcher.timer. If shell_bot's
# "touch .rebuild-requested" quick-command button left a marker here, pull
# and rebuild, then clear the marker.
set -eu
cd "$(dirname "$0")"

MARKER=.rebuild-requested
LOG=rebuild-watcher.log

[ -f "$MARKER" ] || exit 0
rm -f "$MARKER"

{
    echo "=== $(date -Iseconds) rebuild requested ==="
    git pull
    docker compose up -d --build
} >>"$LOG" 2>&1
