Logging & Log Archive
======================

Synthetic Heart writes its runtime logs to a dedicated log directory
(``LOG_DIR``, default ``logs/``). The on-disk naming scheme, rotation,
compression, retention, and gzip-aware reading are all centralised in
:mod:`core.log_archive` — a stdlib-only module shared by the running
application, the WebUI, and the standalone ``synth-logs`` MCP server.

On-disk naming scheme
---------------------

Each logical log stream has a *stem* (e.g. ``synth``, ``webui``, ``cortex_api``,
``live_api``, ``memoria``, ``gemini_extract``). Files are named as follows:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - File
     - Meaning
   * - ``synth.log``
     - Active file — currently being written, always **today**.
   * - ``synth.2026-07-29.log``
     - Daily rotated file, plain text (today / yesterday are kept uncompressed).
   * - ``synth.2026-07-29.1.log``
     - Intra-day split shard, produced when a single day exceeds the size /
       line safety cap.
   * - ``synth.2026-07-28.log.gz``
     - Compressed daily file (days older than yesterday, within the retention
       window).

Rotation
--------

Rotation is handled by :class:`core.logging_utils.TimestampedRotatingFileHandler`:

* **Daily rollover** — a new dated file is opened lazily at the first write
  after midnight.
* **Size / line safety cap** — within a single day, if the active file grows
  past ``DEFAULT_MAX_BYTES`` (50 MB) it is split into ``.<N>.log`` shards. The
  line cap (``DEFAULT_MAX_LINES``) is disabled by default (``0``); the size cap
  is the primary safety net.

Non-blocking writes
-------------------

File handlers are owned by a background writer thread (``synth-log-writer`` in
:mod:`core.logging_utils`) and fed through a bounded queue, so the thread that
logs never waits on the log destination. That thread is the asyncio event loop:
a synchronous ``write`` to a destination that stalls (a full disk, a saturated
network share) blocks the entire runtime — every component goes silent and an
in-flight turn is lost with no error anywhere, because the reporter is blocked
too.

With the writer thread in place a stalled destination degrades to *dropped* log
records: the caller enqueues and returns, a full queue drops records and counts
them, and the writer reports the loss once it catches up
(``Dropped N log record(s): the log destination is slower than the process``). A single write that takes longer than 5 seconds is likewise reported (at most once a minute) as ``Slow log destination: one write took N s``.
Per-handler levels, formatting order and the error-only companion log are
unchanged, console output stays synchronous, and the queue is drained on
graceful shutdown. ``LOG_QUEUE_ENABLED=0`` restores direct writes when the
logger itself is being debugged.

Retention & compression
------------------------

After every rollover (and once at startup in
:func:`core.logging_utils.setup_logging`) the handler runs
:func:`core.log_archive.enforce_retention`:

* **Today and yesterday** — kept as **plain text** for easy tailing.
* **Older than yesterday, within the retention window** — **gzip-compressed**.
* **Older than the retention window** — **deleted**.

The retention window defaults to **7 days** and is configurable via the
``LOG_RETENTION_DAYS`` environment variable. Because logging is initialised
during bootstrap (before the database is available), retention is driven by an
environment variable rather than the runtime config registry.

Reading is gzip-transparent
---------------------------

All read helpers in :mod:`core.log_archive` (``open_text``, ``read_lines``,
``tail_lines``, ``search``) transparently open both plain ``.log`` and
gzip-compressed ``.log.gz`` files, so callers never have to care whether a day
has been compressed yet. ``search`` also spans intra-day shards and multiple
days, and can filter by log stem, minimum level, and a ``since`` timestamp.

WebUI: Archive & Query
----------------------

The **Logs → Archive & Query** sub-tab in the WebUI exposes two capabilities
backed by the endpoints documented in :doc:`api_endpoints`:

* **Download all logs (.zip)** — streams every current log file (active, daily,
  and gzip shards) as a single ``.zip`` archive
  (``GET /api/logs/download``).
* **Search** — a full-text / regex query across all stems and rotations
  (``GET /api/logs/query``), with optional stem, level, and ``since`` filters
  and a result limit.

MCP server
----------

The ``synth-logs`` MCP server (``mcp_servers/synth_logs.py``) delegates all
file discovery and reading to :mod:`core.log_archive`, so it automatically
respects the naming scheme, spans rotations, and reads gzip-compressed archives
transparently.

Configuration summary
----------------------

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - Environment variable
     - Default
     - Purpose
   * - ``LOG_QUEUE_ENABLED``
     - ``1``
     - Write file logs through the background writer thread; ``0`` restores
       synchronous writes.
   * - ``LOG_QUEUE_MAXLEN``
     - ``5000``
     - Bounded log queue length. Records past it are dropped and reported.
   * - ``LOG_DIR``
     - ``logs``
     - Directory that holds all log files.
   * - ``LOG_RETENTION_DAYS``
     - ``7``
     - Days of logs to keep (plain + gzip combined) before deletion.
   * - ``LOGGING_LEVEL``
     - ``INFO``
     - Root logging level for the main runtime.
   * - ``TZ``
     - ``UTC``
     - Timezone used to compute the daily rollover boundary.
