# Grillo — Dream Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Generates a "dream": a surreal, associative diary entry synthesized from recent
memory and diary fragments. It runs once a day (~05:00) and also acts as a Recon
contributor. The resulting diary entry is linked to its `grillo_activity_log`
row for traceability.

## Beat

- **Trigger:** daily at `GRILLO_DREAM_TIME` (not a weighted beat).
- **Output:** a dream diary entry, optionally injected into context until
  `GRILLO_DREAM_INJECT_UNTIL`.

## How it works

The plugin samples recent fragments (`GRILLO_DREAM_SAMPLES`), asks the Grillo
cortex to weave them into a dream, and writes the entry to the diary. When Recon
is enabled it can pull in external material. Discovery is automatic via the
plugin registry.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_DREAM_ENABLED` | Enable/disable the dream beat. |
| `GRILLO_DREAM_TIME` | Time of day (HH:MM) to dream. |
| `GRILLO_DREAM_SAMPLES` | How many fragments to sample for the dream. |
| `GRILLO_DREAM_INJECT_UNTIL` | How long the dream stays injected into context. |
| `GRILLO_DREAM_RECON_ENABLED` | Whether the dream may pull in Recon material. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
