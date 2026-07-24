# Auris — Speech-to-Text

Auris is Synth's **Speech-to-Text** subsystem. It transcribes audio files
(voice messages, uploads) into text so they can enter the normal message
chain and be understood like any other input.

The active engine is selected with `ACTIVE_AURIS_ENGINE`, chosen from the Auris
engine registry (`core/auris_registry.py`). Engines are interchangeable.

## Actions

| Action | Purpose |
|--------|---------|
| `stt_transcribe` | Transcribe an audio file to text via the active Auris engine. |

## Configuration

| Key | Purpose |
|-----|---------|
| `ACTIVE_AURIS_ENGINE` | Active STT engine. |
| `AURIS_ENGINE_SETTINGS` | Per-engine settings blob. |
| `VOSK_MODEL_PATH` / `VOSK_LANGUAGE` | Local VOSK model path and language. |
| `MODEL_AUTO_DOWNLOAD` | Auto-download the STT model when missing. |
