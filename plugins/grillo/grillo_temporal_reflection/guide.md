# Grillo — Temporal Reflection Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Notices the passage of time since the last user message and lets Synth react to
meaningful silences — a long gap, a return after days away — rather than
ignoring the timeline. Routine short gaps (under ~12h) are deliberately ignored
so it never feels needy.

## Beat

- **Beat type:** `temporal_reflection`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** a time-aware prompt enqueued as a low-priority beat.

## How it works

The plugin measures the elapsed time since the last inbound message (reading
chat history), and only fires a reflection when the gap is significant. It uses
`get_conn_ctx` for its time lookups and the active Grillo cortex for the reply.
Discovery is automatic via the plugin registry using its `BEAT_TYPE` attribute.

## Configuration

Uses the shared Grillo settings — `GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`,
`GRILLO_ALLOWED_ACTIONS` / `GRILLO_ALLOWED_SECURITY_LEVEL`. See the
[G.R.I.L.L.O. guide](../guide.md) for the full list.
