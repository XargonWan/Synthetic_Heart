# Grillo — Growth Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

A weekly retrospective: Synth reflects on the past week and rewrites her
self-growth reflection along with her likes/dislikes, so her persona actually
evolves over time instead of staying static.

## Beat

- **Trigger:** weekly (not a weighted beat).
- **Output:** an updated self-growth reflection and refreshed likes/dislikes.

## How it works

The plugin gathers the week's activity, asks the Grillo cortex to summarize the
growth, and persists the rewritten reflection. It also declares a `static_inject`
contribution (handled outside `get_supported_actions`) so the growth reflection
can be surfaced in context. When Recon is enabled it may enrich the reflection.
Discovery is automatic via the plugin registry.

## Configuration

| Key | Purpose |
|-----|---------|
| `GROWTH_MODE` | Self-growth approval mode (`off` / `on` / `request`). |
| `GRILLO_GROWTH_RECON_ENABLED` | Whether growth may pull in Recon material. |

Plus the shared Grillo settings (`GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`, …).
See the [G.R.I.L.L.O. guide](../guide.md).
