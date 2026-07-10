#!/bin/sh
# Runs on the host (not in the container, which has no docker access).
# Triggered periodically by rebuild-watcher.timer. Deploys when either:
#   - shell_bot's "touch .rebuild-requested" quick-command button left a
#     marker here, or
#   - `git fetch` shows origin/main has commits we don't have yet (i.e. a
#     push happened) — auto-deploy on push, no marker needed.
# Reports success/failure straight to the bot's chat via Telegram's HTTP API
# (not through the bot process — works even if this deploy is what's
# restarting it).
set -eu
cd "$(dirname "$0")"

MARKER=.rebuild-requested
LOG=rebuild-watcher.log

# BOT_TOKEN / ALLOWED_USER_ID come from .env (same file docker compose reads).
set -a
[ -f .env ] && . ./.env
set +a

notify() {
    # $1 = message text. No-ops quietly if BOT_TOKEN/ALLOWED_USER_ID aren't
    # set, so this script still works before .env is fully configured.
    if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALLOWED_USER_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${ALLOWED_USER_ID}" \
            --data-urlencode "text=$1" \
            >/dev/null 2>&1 || true
    fi
}

git fetch origin main >>"$LOG" 2>&1 || true

LOCAL_REV=$(git rev-parse HEAD)
REMOTE_REV=$(git rev-parse origin/main 2>/dev/null || echo "$LOCAL_REV")

if [ ! -f "$MARKER" ] && [ "$LOCAL_REV" = "$REMOTE_REV" ]; then
    exit 0
fi
rm -f "$MARKER"

echo "=== $(date -Iseconds) deploy starting (local=$LOCAL_REV remote=$REMOTE_REV) ===" >>"$LOG"

if OUTPUT=$( { git pull && docker compose up -d --build; } 2>&1 ); then
    echo "$OUTPUT" >>"$LOG"
    notify "✅ shell-bot deployed: $(git log -1 --format='%h %s')"
else
    echo "$OUTPUT" >>"$LOG"
    # Telegram messages cap at 4096 chars; leave room for the prefix.
    notify "❌ shell-bot deploy failed:
$(echo "$OUTPUT" | tail -c 3000)"
fi
