# Vox — Text-to-Speech

Vox is Synth's **Text-to-Speech** subsystem. It owns the full
text → audio → dispatch → lip-sync pipeline: it turns a reply into spoken
audio, hands it to the Karada state server so every connected client hears it,
and drives lip-sync on the avatar.

The active engine is selected with `ACTIVE_VOX_ENGINE`, chosen from the Vox
engine registry (`core/vox_registry.py`). Engines are hot-swappable.

## Actions

| Action | Purpose |
|--------|---------|
| `tts_speak` | Synthesize speech from text and broadcast it as playable avatar audio. |

## Per-language voice routing

`VOX_LANGUAGE_OVERRIDES` (a JSON map `iso639-1 → {engine, model, voice}`) lets
TTS use a different engine/model/voice per detected language. After `lingua`
detects the reply language, Vox resolves the override via
`core/config.py::get_vox_language_override()` and forwards the per-call
`model`/`voice`. An explicit per-call `engine_name` always wins.

## Configuration

| Key | Purpose |
|-----|---------|
| `ACTIVE_VOX_ENGINE` | Active TTS engine. |
| `VOX_LANGUAGE_OVERRIDES` | Per-language engine/model/voice map. |
