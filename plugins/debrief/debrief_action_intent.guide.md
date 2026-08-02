# Debrief — Action Intent

A **debrief post-processor**. After a turn completes it inspects the LLM output
for recovery opportunities and action candidates that were implied but not
emitted as proper actions, so Synth can follow through (e.g. proactively act on
an intent it expressed). It runs in the debrief phase and exposes no
model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `ACTION_INTENT_DEBRIEF_ENABLED` | Enable the debrief pass. |
| `ACTION_INTENT_MAX_ACTIONS` | Cap on candidate actions per debrief. |
| `ACTION_INTENT_ALLOW_MESSAGE_ACTIONS` | Whether message actions may be proposed. |
| `ACTION_INTENT_PROACTIVE_ENABLED` | Enable proactive follow-through. |
