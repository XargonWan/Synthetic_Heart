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

## How the dream reaches the model

`get_static_injection()` puts the dream in the prompt as the `todays_dream`
block, from the dream's own 05:00 run until `GRILLO_DREAM_INJECT_UNTIL`. The
core renderer prints it under a `[Today's dream]` heading on every ordinary chat
and beat prompt (`_PLUGIN_CONTEXT_BLOCKS` in `core/prompt_engine.py`); a key no
renderer consumes never reaches the model, so a plugin block that must be seen
has to be declared there.

The dream text itself is read from the beat's own action envelope - the
`create_personal_diary_entry` payload's `content`, which is what the dream turn
wrote. The row's `diary_entry_id` is audit linkage only: it points at whichever
diary row existed when the action was dispatched, which in practice can be an
unrelated interaction diary written hours later. A dream row with no readable
dream in its envelope therefore injects nothing rather than substituting that
diary text.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_DREAM_ENABLED` | Enable/disable the dream beat. |
| `GRILLO_DREAM_TIME` | Time of day (HH:MM) to dream. |
| `GRILLO_DREAM_SAMPLES` | How many fragments to sample for the dream. |
| `GRILLO_DREAM_INJECT_UNTIL` | How long the dream stays injected into context. |
| `GRILLO_DREAM_RECON_ENABLED` | Whether the dream may pull in Recon material. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
