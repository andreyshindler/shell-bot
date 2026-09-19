#!/bin/sh
# Runs on the HOST (not in the container, which has no docker access) as komodo,
# who is in the docker group. Triggered every few seconds by docker-watcher.timer.
#
# Two jobs:
#   1. Refresh .docker-status.txt — the container list (project|name|state,
#      one per line) that shell_bot's /docker button reads and builds buttons
#      from.
#   2. Execute the start/stop/restart requests that /docker's inline buttons
#      appended to .docker-request — but ONLY those three verbs, and ONLY on
#      containers that actually exist. The container can *request* a bounded
#      action; it can never run docker itself.
#
# Results go straight to the bot's chat via Telegram's HTTP API (same approach
# as rebuild-watcher.sh).
set -eu
cd "$(dirname "$0")"

PROJECTS=/home/komodo/projects
STATUS="$PROJECTS/.docker-status.txt"
REQUEST="$PROJECTS/.docker-request"
LOG=docker-watcher.log

# BOT_TOKEN / ALLOWED_USER_ID come from .env (same file docker compose reads).
set -a
[ -f .env ] && . ./.env
set +a

notify() {
    if [ -n "${BOT_TOKEN:-}" ] && [ -n "${ALLOWED_USER_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${ALLOWED_USER_ID}" \
            --data-urlencode "text=$1" \
            >/dev/null 2>&1 || true
    fi
}

# --- 1. process any queued requests (claim the file atomically first) ---
if [ -f "$REQUEST" ]; then
    QUEUE="$REQUEST.processing"
    if mv "$REQUEST" "$QUEUE" 2>/dev/null; then
        EXISTING=$(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
        while IFS=' ' read -r VERB NAME _rest; do
            [ -z "${VERB:-}" ] && continue
            case "$VERB" in
                start|stop|restart) ;;
                *) notify "⚠️ docker: ignored unknown verb '$VERB'"; continue ;;
            esac
            # NAME must be exactly one existing container. docker is invoked with
            # NAME as a single argv (no shell), so this is validation, not an
            # injection guard, but it keeps the surface tight.
            if ! printf '%s\n' "$EXISTING" | grep -qxF "$NAME"; then
                notify "⚠️ docker: no such container '$NAME'"
                continue
            fi
            echo "=== $(date -Iseconds) $VERB $NAME ===" >>"$LOG"
            if OUT=$(docker "$VERB" "$NAME" 2>&1); then
                echo "$OUT" >>"$LOG"
                notify "✅ docker $VERB: $NAME"
            else
                echo "$OUT" >>"$LOG"
                notify "❌ docker $VERB $NAME failed:
$(echo "$OUT" | tail -c 1000)"
            fi
        done < "$QUEUE"
        rm -f "$QUEUE"
    fi
fi

# --- 2. refresh the status snapshot (project|name|state, includes stopped) ---
if OUT=$(docker ps -a \
    --format '{{.Label "com.docker.compose.project"}}|{{.Names}}|{{.State}}' \
    2>/dev/null); then
    printf '%s\n' "$OUT" | sort > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
fi
