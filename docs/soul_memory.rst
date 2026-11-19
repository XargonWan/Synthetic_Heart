Long-term memory (SOUL)
=======================

SOUL is the one store for what the persona remembers. Everything that needs to be
*recalled later* is filed in SOUL; everything else (the diary as narrative, the
people profiles, the mood) keeps its own owner and feeds SOUL where it makes
sense. ``AGENTS.md`` §3a is the short routing table for contributors.

What lives where
----------------

.. list-table::
   :header-rows: 1
   :widths: 24 30 46

   * - Store
     - Owner
     - What it is
   * - ``mem_cells`` (+ ``mem_cell_vectors``)
     - SOUL
     - Recallable memories. ``source_kind`` says where a cell came from:
       ``chat`` (compiled from conversations), ``journal`` (a diary entry, first
       person), ``diary_day`` (a compacted day summary) or ``legacy_memory`` (a row
       of the retired ``memories`` table).
   * - ``mem_cells_archive``
     - SOUL
     - Every cell the curator or a consolidation removes, copied here first.
   * - ``situational_notes``, ``retracted_claims``
     - SOUL (written by Debrief)
     - Short-lived situations and "this is false" corrections.
   * - ``dsp_extractions``, ``dsp_versions``
     - SOUL
     - The nightly profile of the trainer.
   * - ``ai_diary``
     - AI Diary
     - The persona's own journal. Each write is mirrored into SOUL as a
       ``journal`` cell.
   * - ``bio``
     - Bio Manager
     - Per-person profiles; switchable on its own, and a profile can be private.
   * - ``emotion_state``
     - Emotion Manager
     - Live mood. SOUL goes quiet when this plugin is off.
   * - ``memories``, ``archived_memories``, ``ai_diary_archive``
     - legacy
     - Being retired; see *Migrating* below.

The service API
---------------

Code outside SOUL does not touch its tables. It uses
:class:`core.soul.service.SoulService`::

    from core.soul.service import SoulService

    service = SoulService.from_registry()      # None when SOUL is switched off
    if service is not None:
        await service.ingest_summary(
            kind="journal",                     # journal | diary_day | legacy_memory
            source_key="ai_diary:42",           # stable: a second call updates, never duplicates
            text="...",
            event_timestamp=when,
        )
        hits = await service.recall(
            "the garden",
            sources={"journal"},                # optional; chat | journal | diary_day | legacy_memory
            date_from=start, date_to=end,       # optional
        )

``ingest_summary`` is idempotent: the cell id is ``<kind>:<source_key>``. Same text
again changes nothing; new text replaces the trace and embedding but keeps the
cell's retrieval history. ``retire_source`` removes a cell (archiving it).

Recall
------

Every turn SOUL embeds the incoming message and ranks stored cells by meaning,
word overlap, emotional weight and recency. At most two of the five recalled slots
go to ingested cells (diary, day summaries, retired memories); unused slots fall
through to them. Diary lines are shown as ``my diary``: what the persona itself
thought, never a fact about a user.

The curator
-----------

Once a day (and never again on a restart) the curator keeps the store within
``SOUL_CURATOR_MAX_MEMORIES`` (default 5000). With
``SOUL_CURATOR_EVICT_ONLY_OVER_CAP`` on (the default) a calm memory is kept while
the store is under its cap and is the first to go when it is over. Every eviction
is archived in ``mem_cells_archive``. The previous behaviour deleted every memory
below a salience threshold after a week, whatever the store size.

The curator counts and evicts only conversation cells. Diary blocks, day summaries and
legacy memories (``source_kind`` other than ``chat``) have their origin in another table
and are not a chat's leftovers, so the cap never archives them; thinning them over the
years belongs to a later compaction by topic.

The archive does not grow without end. Each archived row records why it left
(``reason``: ``evicted``, ``duplicate`` or ``housekeeping``) and, for a duplicate,
which cell absorbed it (``merged_into``). The daily job deletes archived rows older
than ``SOUL_ARCHIVE_RETENTION_DAYS`` (default 365), and the copies kept only
because they repeated another cell (``duplicate``, ``housekeeping``) after
``SOUL_ARCHIVE_COPY_RETENTION_DAYS`` (default 30).

Repeated memories
-----------------

The same subject comes back over several days, or from several people. When SOUL
compiles a chat window it compares the new cell with the last sixty cells of the
same audience (``shared`` or ``private`` are never mixed). A cell whose words
overlap an existing one by 0.8 or more (Jaccard on word sets of three letters or
more, so it works in any language and uses no word list) is not stored again:
the fuller of the two is kept, the other is archived with ``reason='duplicate'``,
and the people who said it are added to the surviving cell's ``participants``
(who, and when). A different audience is never merged.

``shared`` cells are compared across chats as well: the same notion coming back from
another person or another chat is absorbed into the existing cell and the speakers
are added to its ``participants``. ``private`` cells are compared only inside their
own chat, and system sessions never take part.

What is not stored at all: the extraction asks the model to label each entry with a
``kind`` (``fact``, ``preference``, ``plan``, ``event``, ``small_talk``,
``one_off_command``, ``joke``). Greetings, one-off commands and jokes are dropped,
unless the session holds a dated plan, and a distilled cell with two content words or
fewer and no fact is dropped too. The label is cautious rather than complete: the
model sometimes keeps a command it should have dropped.

``scripts/dedupe_soul_cells.py`` applies the same rule to an existing store. It
previews by default; ``--apply`` archives the cells that hold system text and the
repeats.

Daily jobs and restarts
-----------------------

The nightly rollup and the curator record the day they last ran in the config
store (``SOUL_LAST_ROLLUP_DATE``, ``SOUL_LAST_CURATOR_DATE``), so a restart does
not repeat them. A compile that is running when new lines arrive keeps those lines
for the next pass.

Cache and chat isolation
------------------------

When a plugin's static injection times out, the last good payload is served. For
SOUL that payload is scoped to the chat it was built for, so one conversation's
recalled memories (and any private note) are never shown in another. On the
normal path situational notes that are not private are injected in every chat,
which is what keeps the persona aware of what is happening outside the
conversation it is in; only the timeout fallback is per chat.

Questions about a period
------------------------

The Recon memory recollector asks for an optional ``time_window``, told today's date
and asked for an ABSOLUTE ISO interval whatever language the human wrote in
("ieri", "先週" and "last week" are resolved by the model; the parser,
``core.time_window.parse_time_window``, understands only ISO intervals, a single
ISO date, ``start``/``end`` objects and numeric durations, never words). When the
message is about a specific past period it recalls that period from SOUL with
``SoulService.recall(date_from=..., date_to=...)`` and drops keyword hits outside it.

Privacy
-------

Every cell has a ``visibility``. A cell compiled from a one-to-one chat is
``private`` and is recalled only in that same chat; cells from groups are
``shared``. What someone confides to the persona is therefore not repeated to
anyone else through recall. Cells written before the field existed are all
``shared``: ``scripts/backfill_cell_visibility.py`` marks the Telegram private-chat
ones (preview by default, ``--apply`` to write); other interfaces carry no such
signal in their session id and are left alone. Diary (``journal``) cells and
situational notes are not filtered by audience.

The **Debrief judges privacy**. The call that already extracts situational notes
after a turn also says whether the exchange was shared in confidence (a secret,
something the human asked to keep to themselves, sensitive matters of health,
family, money, the law or a third party), judged from the whole exchange and not
from words. When it was, SOUL marks the cells compiled from the current window
``private`` whatever the kind of chat (so interfaces that cannot say whether a chat
is one to one are covered), and the notes filed that turn are ``private`` to that
chat and are injected nowhere else. A broken or missing verdict counts as "not
confidential": the flag only ever tightens who can be reminded of a memory.

Sessions that hold system text rather than a conversation (the diary
consolidator's transcripts, web-search notices, the nightly rollup) are never
recalled.

Emotions
--------

SOUL's mood engine and its mood-based recall boost run only while the *Emotion
Manager* plugin is enabled (``PLUGIN_ENABLED__emotion_manager``). Switching that
plugin off, as the README describes for deployments that must not run an emotional
subsystem, silences SOUL's emotion output too.

People (Bio)
------------

The **People** tab lists every profile, shows it, marks it ``private`` and deletes
it. A private profile is shown to the model, and returned by ``bio_full_request``,
only when that same person is the one talking. Deleting removes the profile; the
conversations that mention the person are history and are not changed.

Migrating
---------

1. Back up the database.
2. ``uv run python scripts/ingest_legacy_into_soul.py --dry-run`` and then without
   ``--dry-run``. It files the diary, its archive (one cell per day) and the
   ``memories`` rows worth keeping. Rows written by the chat observer are skipped:
   they are raw copies of ``chat_history_cache``. Running it again changes nothing.
2b. ``uv run python scripts/dedupe_soul_cells.py`` (preview) and then ``--apply``
   archives the system-text cells and the repeats the old compile left behind.
3. After the new curator is live, ``scripts/restore_curator_evicted_cells.py
   --since 2026-01-01`` rebuilds the cells the old curator had deleted from the
   scenes that still reference them (preview by default; it embeds every row, so run
   it when the machine is idle), then ``scripts/backfill_cell_visibility.py``.
4. Set ``MEMORY_LEGACY_TIERS_ENABLED`` to ``0`` so the keyword search stops
   returning the same entries a second time. Raw chat history is still searched.
6. Long diary entries are filed as blocks of about 1,500 characters (cells ``...#0``,
   ``#1``), because the embedder reads only the start of a text. Running
   ``scripts/ingest_legacy_into_soul.py`` again after an upgrade re-files the entries
   that were stored as one long cell (they are archived with ``reason='rechunked'``).
   Recall returns one block per entry, so a day is not told twice.
5. ``GRILLO_OBSERVER_STORE_MEMORIES`` is off by default; leave it off.

``SOUL_REPOSITORY_BACKEND`` is ignored. SOUL persists when the main database is
PostgreSQL; with anything else it is in memory and is lost on restart.

Scheduled times
---------------

``GRILLO_COMPACT_TIME``, the weekly review and the growth schedule are wall-clock
times in the configured ``TZ``. The database and all timestamps stay UTC.
