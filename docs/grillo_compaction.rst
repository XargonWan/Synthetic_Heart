Grillo Memory Compaction
=========================

Overview
--------

The G.R.I.L.L.O. compactor is a nightly background plugin that consolidates older
memories by tag. It groups memories, asks the active LLM (English prompt) for a
concise summary and suggested tags/feeling, archives the source memories into
`archived_memories` and writes a new single memory with the LLM-provided tags
and feeling.

Configuration
-------------

Relevant config variables (in the "grillo" group):

- GRILLO_COMPACT_ENABLED (bool) - Enable compaction (default: True)
- GRILLO_COMPACT_TIME (HH:MM) - Local time when compaction runs (default: 03:00)
- GRILLO_COMPACT_CYCLES (int) - Number of cycles executed each run (default: 10)
- GRILLO_COMPACT_BATCH_SIZE (int) - Max memories per batch (default: 40)
- GRILLO_COMPACT_AGE_DAYS (int) - Age threshold in days (default: 30)

Behavior
--------

- Runs at configured time once per day.
- Each run executes up to `GRILLO_COMPACT_CYCLES` cycles; each cycle processes
  a batch of up to `GRILLO_COMPACT_BATCH_SIZE` memories older than
  `GRILLO_COMPACT_AGE_DAYS`.
- Tag selection: choose the tag from the oldest memory that has tags within the
  candidate set; if none are found the cycle is skipped.
- LLM prompt is in English and must return ONLY valid JSON with keys:
  `summary`, `tags`, `feeling`, `source_ids`, `confidence`.

Storage
-------

A new table `archived_memories` stores the archival summaries and the list of source memory IDs. It contains a `notes` JSON field which includes only useful data: an optional `justification` string and an optional `detailed` field with 1-3 short bullet points or a short paragraph intended as memory content. When available, `detailed` is used as the content inserted into `memories` for better context (the `summary` remains a concise title). Source memories are deleted from `ai_diary`/`memories` and a new compacted memory is inserted into `memories` with the LLM-provided `detailed` content, tags and feeling.

Testing
-------

Unit tests are in `tests/test_grillo_compactor.py` and mock DB and LLM engines to
verify tag selection, cycle behavior and successful persistence.
