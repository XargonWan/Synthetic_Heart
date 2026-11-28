#!/usr/bin/env bash
# Container entrypoint: prepare the persistent directories, seed the default skin, then
# hand over to the launcher (automation_tools/container_synth.sh). PID 1 is tini, which reaps the processes
# the extensions spawn.
set -e

# Ensure synth config directory exists and is writable
mkdir -p /config/.config/

# Ensure persistent storage sub-directories exist under the /config volume so
# that models, HuggingFace cache, generated TTS audio and received media are
# written to persistent storage (survive rebuilds) rather than the ephemeral
# container layer. These paths are pointed here via SYNTH_MODELS_DIR / HF_HOME /
# VOX_OUTPUT_DIR / TMPDIR in the compose file.
mkdir -p \
  "${SYNTH_MODELS_DIR:-/config/models}" \
  "${HF_HOME:-/config/hf-cache}" \
  "${VOX_OUTPUT_DIR:-/config/media/tts}" \
  "${TMPDIR:-/config/media/tmp}"

chmod a+rwx -R /config

# The blanket chmod above must not leave credentials world-readable.
for secret in /config/.synth_secret /config/.webui_token; do
  if [ -f "$secret" ]; then
    chmod 600 "$secret"
  fi
done

# Seed the default skin into a fresh skins volume. The image does not carry Rei in
# /app/skins (a named volume shadows it anyway); it keeps one copy, here.
if [ ! -f /app/skins/Rei/model.vrm ] && [ -f /app/.skins-seed/Rei/model.vrm ]; then
  echo "[entrypoint] Seeding default skin 'Rei'..."
  mkdir -p /app/skins
  cp -a /app/.skins-seed/Rei /app/skins/
  chmod -R a+rX /app/skins/Rei
fi

echo "[entrypoint] Starting Synthetic Heart..."
cd /app
exec /app/automation_tools/container_synth.sh run
