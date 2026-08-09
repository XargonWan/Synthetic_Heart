# Emotion Manager

Centralizes Synth's **emotional state**. It persists emotion intensities,
applies time-based decay so feelings fade naturally, and balances opposite
emotions on the Plutchik wheel. The current emotional state is injected into
every prompt so Synth's tone reflects how she feels.

Backed by the `emotion_state` and `emotion_diary` tables.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject the current emotion state into the prompt. |
| `get_emotion_state` | Read the current emotion intensities. |
| `update_emotion_from_tags` | Adjust emotions from `[em_NAME:intensity]` tags in a reply. |
| `update_emotion_state` | Set/adjust emotions directly. |

## Configuration

| Key | Purpose |
|-----|---------|
| `EMOTION_DECAY_TAU` | Decay time constant (seconds). |
| `EMOTION_MAX_DISPLAY` | Max emotions shown in the UI. |
