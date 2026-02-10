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

# --- [Permissions Fix] ---
# Ensures ./logs and ./skins (mapped from Host) are writable by the container user.
LOG_DIR="/app/logs"
mkdir -p "$LOG_DIR"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Try to chown (if running as root), otherwise fallback to chmod 777
if chown -R "${PUID}:${PGID}" "$LOG_DIR" 2>/dev/null; then
    : # ownership set
else
    chmod 0777 "$LOG_DIR" 2>/dev/null || true
fi

# Ensure log file exists so Python doesn't crash on permission denied
touch "$LOG_DIR/synth.log" 2>/dev/null || true
chown "${PUID}:${PGID}" "$LOG_DIR/synth.log" 2>/dev/null || true

# Same logic for skins
SKINS_DIR="/app/skins"
mkdir -p "$SKINS_DIR"
if chown -R "${PUID}:${PGID}" "$SKINS_DIR" 2>/dev/null; then
    :
else
    chmod -R a+rx "$SKINS_DIR" 2>/dev/null || true
fi

# --- [Execution Logic] ---
MODE="${1:-run}"
shift || true

case "$MODE" in
    run)
        if [ "${1:-}" = "--as-service" ]; then
            shift
            log "Running main.py in service mode (via uv)"
            # 'uv run' handles the venv activation and python path automatically
            exec uv run main.py --service "$@"
        else
            log "Running main.py interactively (via uv)"
            exec uv run main.py "$@"
        fi
        ;;
    notify)
        log "Sending test notification"
        # We pipe the python script into 'uv run python -'
        uv run python - <<'PY'
import asyncio
from telegram import Bot
from core.config import BOT_TOKEN, TELEGRAM_TRAINER_ID
async def main():
    bot = Bot(token=BOT_TOKEN)
    trainer_id = TELEGRAM_TRAINER_ID
    if trainer_id:
        await bot.send_message(chat_id=trainer_id, text="Test notification")
asyncio.run(main())
PY
        ;;
    *)
        echo "Usage: $0 {run [--as-service]|notify}" >&2
        exit 1
        ;;
esac