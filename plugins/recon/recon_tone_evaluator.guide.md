# Recon — Tone Evaluator

A **Recon contributor**: during pre-processing it evaluates the appropriate
conversational tone for the reply and produces a `tone_hint`, helping Synth
match register (playful, formal, comforting, etc.).

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_TONE_EVALUATOR_RECON_ENABLED` | Enable this Recon contributor. |
