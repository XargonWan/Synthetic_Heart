# Grillo — Tag Elaboration Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Periodically reflects on the topics and tags that emerged in recent
conversations, letting Synth deepen or connect ideas she has been thinking
about instead of forgetting them.

## Beat

- **Beat type:** `tag_elaboration`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** an introspective prompt enqueued as a low-priority beat that flows
  through the standard message chain, typically producing a diary entry or a
  new memory.

## How it works

The plugin builds a prompt from recent conversation tags/topics and hands it to
the active Grillo cortex. Discovery is automatic: the core (`grillo_impl.py`)
finds it through the plugin registry by its `BEAT_TYPE` attribute.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
