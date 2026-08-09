# TTS Lip-Sync (legacy)

> **Legacy.** The model-facing `tts_speak` action now lives in the **Vox**
> subsystem (`vox_plugin.py`). This plugin is kept only for backward
> compatibility with older callers that still invoke its internal audio
> generation directly.

It retains the internal text → audio helper used by legacy code paths but
exposes **no** model-facing action. New work should use Vox instead.

## Configuration

| Key | Purpose |
|-----|---------|
| `TTS_ENABLED` | Enable the legacy helper. |
| `TTS_ENDPOINTS` | Legacy TTS endpoint list. |
| `TTS_TIMEOUT_SECONDS` | Request timeout. |
| `TTS_OUTPUT_DIR` | Where generated audio is written. |
| `TTS_FALLBACK_TO_TEXT` | Fall back to text if synthesis fails. |
