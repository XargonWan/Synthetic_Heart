Grillo Memory Compaction
=========================

Overview
--------

The G.R.I.L.L.O. compactor is a nightly background plugin that consolidates older
diary material into memories. Level 1 works on ONE DAY at a time, which is the
default: a diary day already lives in one row, so nothing is merged across days. It
asks the active LLM (English prompt) for a summary plus an `anchors` block, writes the
new memory, then the archival summary, then the archive copy, and only after all three
does it remove the day from `ai_diary`.

Setting `GRILLO_COMPACT_DAY_UNITS=false` restores the older path, which clusters up to
`GRILLO_COMPACT_BATCH_SIZE` rows by tag across as many days as the cluster happens to
cover. That path is why this plugin now works in day units: measured on 2026-09-24, 12
of 14 clusters covered more than one day and 44 days had been folded into 14 summaries,
which is how a week of rain became "rainy day, longing".

Configuration
-------------

Relevant config variables, all in the "grillo" group. The day-unit keys are read on
every run, so an edit takes effect without a restart:

- GRILLO_COMPACT_ENABLED (bool) - Enable compaction (default: True)
- GRILLO_COMPACT_TIME (HH:MM) - Local time when compaction runs (default: 03:00)
- GRILLO_COMPACT_CYCLES (int) - Number of cycles executed each run (default: 10)
- GRILLO_COMPACT_BATCH_SIZE (int) - Max memories per batch (default: 40)
- GRILLO_COMPACT_AGE_DAYS (int) - Age threshold in days (default: 30)
- GRILLO_COMPACT_DAY_UNITS (bool) - Level 1 compacts one day at a time (default: True)
- GRILLO_COMPACT_DAY_AGE_DAYS (int) - A day becomes eligible this many days after it was
  written (default: 2); the newest days stay raw because live chat stands on them
- GRILLO_COMPACT_DAY_MAX_SUMMARY_CHARS (int) - Ceiling for a day summary (default: 2000);
  it has to be large enough to hold the day's anchors
- GRILLO_COMPACT_DAY_THOUGHTS_CHARS (int) - How much of the day's `personal_thought` is
  shown to the summariser (default: 6000)
- GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE (float) - Below this the memory is written AND
  the raw day is kept beside it (default: 0.9)
- GRILLO_COMPACT_ANCHOR_CHECK (bool) - Refuse a summary that dropped the day's concrete
  terms, with one retry that names what was missing (default: True)
- GRILLO_COMPACT_SKIP_DECLINED (bool) - A day the model declined writes nothing and keeps
  its source rows (default: True)

Behavior
--------

- Runs at the configured time once per day.
- Each run executes up to `GRILLO_COMPACT_CYCLES` cycles.
- Day units (default): each cycle takes the oldest days older than
  `GRILLO_COMPACT_DAY_AGE_DAYS`, one model call per day. A day whose anchors cannot be
  verified after one retry, or whose summary arrives below
  `GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE`, keeps its raw row in `ai_diary` beside the
  memory.
- Clustering (only with day units off): choose the tag from the oldest memory that has
  tags within the candidate set; if none are found the cycle is skipped.
- LLM prompt is in English and must return ONLY valid JSON with keys: `summary`,
  `tags`, `feeling`, `source_ids`, `confidence`, and for day units also `anchors`, which
  reports the day's weather, places, names, objects and food as the day itself mentions
  them. A slot the day does not mention comes back empty with a reason instead of being
  filled from a neighbouring meaning.

Storage
-------

`archived_memories` stores the archival summary and the list of source IDs, with a
`notes` JSON field holding an optional `justification`, an optional `detailed` field of
1-3 bullets, and for day units the `anchors` and the result of the anchor check. When
available, `detailed` (coerced to text, so a JSON array lands as lines rather than a
Python list repr) is the content inserted into `memories`, and the `summary` remains a
concise title.

Order is enforced: the memory is written FIRST, then the `archived_memories` row, then
the copy into `ai_diary_archive`, and only then is the source row deleted from
`ai_diary`. A failed write reports `write_failed` and leaves the day where it was,
instead of the old behaviour where the day was deleted first and the write failed
silently.

Label width and retries (added 2026-09-26 after ten days failed to store). The model's
`feeling` is a free-text label, and `memories.emotion` is `varchar(50)` on stores that
predate the declared schema (`scripts/sql/app_main_postgres.sql` says `TEXT`); feelings
measured 63-104 characters, so the insert was rejected and the day stayed uncompacted -
57 failed writes and only 4 of 12 eligible days stored in one nightly run. The label is
now bounded to 50 characters on a word boundary by `_bound_emotion`, and the FULL
feeling is kept in `archived_memories.notes["feeling"]`, so a cosmetic label can never
fail a day's write. The startup migration
`core/migrations.py::_widen_memories_text_columns` aligns a lived-in store with the
declared schema on the next boot: it measures each column through `information_schema`
and widens only the ones positively measured as bounded (idempotent, fail-open; a store
that already matches is untouched).

Days that failed are not asked again in the same run. Only `persisted` clears a day;
`write_failed`, `anchors_failed`, `kept_raw` (memory written, raw row kept on purpose),
`no_engine` and `error` all leave the day exactly as it was, so the remaining cycles of
the same night do not pay for the same answer (about 50 of 89 attempts were being spent
on repeat failures). The set is cleared at the start of the nightly run and of a manual
`compact_now`.

Summaries orphaned by that old write path are recovered with
`scripts/backfill_compacted_memories.py`: insert-only, dry run by default, idempotent
through an `archived_id:<n>` tag, `--apply` to write, `--undo` to print the reversal.

Testing
-------

Unit tests are in `tests/test_grillo_compactor.py`, `tests/test_grillo_compactor_clusters.py`,
`tests/test_grillo_compactor_persist.py` (the write-before-delete ordering and the
connection the write joins), `tests/test_grillo_compactor_day_units.py` (one day in,
one day out: the anchors, the confidence gate, and the cases that must keep the raw row)
and `tests/test_grillo_compactor_resilience.py` (the feeling bound and the failed-day
rule). `tests/test_migrations_memories_columns.py` covers the column widen.
They mock DB and Cortex engines. Live read-only checks: `scripts/verify_live_day_units.py`
(the accounting after a run, plus an independent anchor re-check against the archived day)
and `scripts/probe_recall_reach.py` (whether a memory is inside the pool the prompt's
memory block is drawn from).
