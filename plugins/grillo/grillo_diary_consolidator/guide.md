# Grillo — Diary Consolidation Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Merges the fragmented diary rows written across a past day into a single,
coherent diary entry — so Synth's diary reads as a narrative instead of a pile
of disconnected snippets.

## Beat

- **Beat type:** `diary_consolidation`
- **Selection:** weighted-selected by the Grillo scheduler on a normal tick.
- **Output:** a consolidation prompt enqueued as a low-priority beat; the merged
  result replaces the day's fragmented diary rows.

## How it works

The plugin gathers the diary fragments for a target day (within its lookback
window), asks the active Grillo cortex to synthesize them into one entry, and
writes the consolidated version back. Discovery is automatic via the plugin
registry using its `BEAT_TYPE` attribute.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_DIARY_CONSOLIDATE_ENABLED` | Enable/disable this beat. |
| `GRILLO_DIARY_CONSOLIDATE_LOOKBACK_DAYS` | How far back to look for a day to consolidate. |

Plus the shared Grillo settings (`GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`, …).
See the [G.R.I.L.L.O. guide](../guide.md).
