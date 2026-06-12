#!/usr/bin/env bash
set -e

log() { echo "[synth.sh] $*"; }

log "Launcher invoked: $*"

cd /app
ENV_FILE="/app/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# VENV_DIR may be set by the image; default to /opt/venv but fall back to /app/venv
# for backwards compatibility if present (e.g., older images)
VENV_DIR="${VENV_DIR:-/opt/venv}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    # If VENV_DIR isn't present (e.g., user bind-mounted /app and removed symlink)
    # fall back to /app/venv to preserve older behavior.
    if [ -x "/app/venv/bin/python" ]; then
        VENV_DIR="/app/venv"
    fi
fi

# Ensure logs directory exists and is writable by the runtime user
# This handles the common case where the host bind-mount (./logs:/app/logs)
# is owned by root or another UID and would otherwise prevent the app from
# writing logs out-of-the-box. We attempt to chown to PUID:PGID (if set),
# otherwise fallback to making the directory world-writable so the container
# can start without manual host intervention.
LOG_DIR="/app/logs"
mkdir -p "$LOG_DIR"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
# Prefer chown (will modify host bind-mounted dir owner if running as root),
# but if it fails (e.g., root-squash NFS) we relax perms to allow writing.
if chown -R "${PUID}:${PGID}" "$LOG_DIR" 2>/dev/null; then
    : # ownership set
else
    chmod 0777 "$LOG_DIR" 2>/dev/null || true
fi
# Ensure log file exists so FileHandlers can open it immediately
touch "$LOG_DIR/synth.log" 2>/dev/null || true
chown "${PUID}:${PGID}" "$LOG_DIR/synth.log" 2>/dev/null || true

# Ensure skins directory exists and is accessible (named volumes will be used by default)
SKINS_DIR="/app/skins"
mkdir -p "$SKINS_DIR"
# Try to correct ownership; if it fails, at least relax read/execute perms
if chown -R "${PUID}:${PGID}" "$SKINS_DIR" 2>/dev/null; then
    :
else
    chmod -R a+rx "$SKINS_DIR" 2>/dev/null || true
fi

MODE="${1:-run}"
shift || true

case "$MODE" in
    run)
        if [ "${1:-}" = "--as-service" ]; then
            shift
            log "Running main.py in service mode"
            exec "$VENV_DIR/bin/python" /app/main.py --service "$@"
        else
            log "Running main.py interactively"
            exec "$VENV_DIR/bin/python" /app/main.py "$@"
        fi
        ;;
    notify)
        log "Sending test notification"
        "$VENV_DIR/bin/python" - <<'PY'
import asyncio
import os
from telegram import Bot
from core.config import get_trainer_id
async def main():
    token = os.environ.get("BOTFATHER_TOKEN", "")
    trainer_id = get_trainer_id("telegram_bot")
    if not token:
        print("BOTFATHER_TOKEN not configured")
        return
    if not trainer_id:
        print("No telegram_bot trainer id configured (TRAINER_IDS)")
        return
    bot = Bot(token=token)
    await bot.send_message(chat_id=trainer_id, text="Test notification")
asyncio.run(main())
PY
        ;;
    *)
        echo "Usage: $0 {run [--as-service]|notify}" >&2
        exit 1
        ;;
esac

