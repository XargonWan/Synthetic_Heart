# Grillo — Memory Compaction Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Nightly memory housekeeping. Groups older memories by tag, asks the active LLM
to synthesize each cluster into a single compacted memory, archives the source
rows into `archived_memories`, and inserts the new compacted memory back into
`memories` with LLM-suggested tags and feeling. Keeps long-term memory dense and
useful instead of unbounded.

## Beat

- **Beat type:** `memory_consolidation`
- **Selection:** runs nightly at `GRILLO_COMPACT_TIME`.
- **Output:** compacted memory rows; source memories moved to the archive.

## Run Now

The plugin is **runnable** from the WebUI Plugins tab: the **Run compaction**
button triggers a single compaction cycle on demand (dispatched to
`run_action("compact_now", ...)`) without waiting for the nightly schedule.

## How it works

The plugin clusters candidate memories older than `GRILLO_COMPACT_AGE_DAYS`,
skips clusters below the minimum size, summarizes each with the Grillo cortex
(bounded by the max-chars/ratio limits), then archives and replaces them.
Discovery is automatic via the plugin registry.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_COMPACT_ENABLED` | Enable/disable this beat. |
| `GRILLO_COMPACT_TIME` | Time of day (HH:MM) to run compaction. |
| `GRILLO_COMPACT_CYCLES` | How many compaction cycles per run. |
| `GRILLO_COMPACT_BATCH_SIZE` | Memories processed per batch. |
| `GRILLO_COMPACT_AGE_DAYS` | Minimum age before a memory is a candidate. |
| `GRILLO_COMPACT_WINDOW_DAYS` | Time window grouped together. |
| `GRILLO_COMPACT_MIN_CLUSTER_SIZE` | Smallest tag cluster worth compacting. |
| `GRILLO_COMPACT_MAX_SUMMARY_CHARS` | Hard cap on a compacted summary length. |
| `GRILLO_COMPACT_MAX_SUMMARY_RATIO` | Summary length relative to source. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
