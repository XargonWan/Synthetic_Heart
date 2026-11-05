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

A day longer than `GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS` is merged in parts: only
its earliest fragments go into the prompt, the merged prose is written through
`update_diary_entry` with the `diary_merge_preserve_from` offset (handed over by
`grillo_impl`) so the fragments past that offset are preserved untouched, and the
day stays eligible for the following run. A day is therefore merged across
several runs rather than in one oversized call.

## Scheduling dependency: a beat must not pre-empt a beat

This beat's model call takes seconds, and the observer beat fires on the same
`grillo/-1` lane about ten seconds later in the same minute. If a later beat
cancels this one mid-call, the day is never merged and (because no action is ever
emitted) the failure is silent: the same `PART 1 of N` plan is logged on every
run while the day's row never changes. `core/message_queue.py` therefore never
lets a Grillo beat cancel another Grillo beat (`_is_beat_message` /
`_should_cancel_background_task`); only a real user message still pre-empts a
running internal beat. If a day looks stuck, check the log for this beat's own
`update_diary_entry` action before suspecting the merge logic.

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_DIARY_CONSOLIDATE_ENABLED` | Enable/disable this beat. |
| `GRILLO_DIARY_CONSOLIDATE_LOOKBACK_DAYS` | How far back to look for a day to consolidate. |

Plus the shared Grillo settings (`GRILLO_BEAT_INTERVAL`, `GRILLO_CORTEX`, …).
See the [G.R.I.L.L.O. guide](../guide.md).
