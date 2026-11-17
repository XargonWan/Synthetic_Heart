Statistics (the stat server)
============================

``core/stats_registry.py`` is SyntH's in-process statistics: daily counters, rolling timing series, gauges, and a
small *stat server* that lets any plugin add a block to **Logs > Stats** in the WebUI. The page knows no plugin. It
draws the sections the server sends, and the server leaves out everything that has no data in the chosen period, so
a plugin that is off, or an engine nobody used, leaves no empty entry behind.

Recording
---------

.. code-block:: python

    from core import stats_registry as stats

    stats.incr("myplugin.jobs", kind="import")      # a counter per local day (bare + labelled series)
    stats.observe("myplugin.job_seconds", 2.4)      # a timing series: the last samples with their times
    stats.gauge("myplugin.queue", 3)                # the latest value
    stats.info("myplugin.state", {"mode": "auto"})  # the latest small dict (not persisted)

Nothing here raises: a statistic must never break a turn. Counters of the last 14 days and the timing windows are
saved to ``stats.json`` and survive a restart (never under pytest or with ``SYNTH_STATS_PERSIST=0``). Series whose
name starts with ``engine.`` or ``turn.seconds`` keep 1000 samples, the others 200.

Registering a section
---------------------

A plugin registers when it starts and unregisters when it stops:

.. code-block:: python

    from core import stats_registry as stats

    def build(view):                       # view = the registry seen through the chosen period
        return [
            stats.cards_block([{"label": "jobs", "value": view.count("myplugin.jobs"), "kind": "count"}]),
            stats.timing_table("Jobs", [stats.timing_row("import", view.timing("myplugin.job_seconds"))]),
            stats.note_block("Anything computed from view.info(...) or view.gauge(...)."),
        ]

    stats.register_section("myplugin", "My plugin", build, order=60)   # in start()
    stats.unregister_section("myplugin")                              # in stop()

Block types: ``cards_block``, ``timing_table`` (rows made with ``timing_row``; extra keyword arguments such as
``failed=`` become columns), ``counters_block``, ``day_table_block`` and ``note_block``. A block with nothing in it,
or a table with no sample in the period, is dropped; a section with no block left is not sent. A ``build`` that raises
is skipped, it never blanks the page. The Rift Vessel is the reference: ``core/vessel_stats.py``, registered by
``interface/vessel_interface.py``.

The period
----------

``GET /api/stats?period=`` takes ``1h``, ``24h``, ``3d``, ``7d`` (default), ``14d`` or ``all`` (everything kept; not
the default because it reads the most). Timings are filtered by the age of each sample. Counters are kept per local
day, so a period under a day shows today's counters.

Engines
-------

Every engine call is timed by the funnel it goes through, so an engine needs no code of its own and Zen, OpenRouter
and the rest report the same way:

* Cortex: ``generate_response`` of the external-endpoint bridge (``zen-llm-engine`` and every other endpoint) and of
  the native engines (``anthropic``, ``openapi``, ``openrouter``, ``gemini_api``, ``xai_grok``), through
  ``stats.tracked_engine``. This covers chat turns, Recon, Debrief, the agent loop and the background callers.
* Vox, Auris, Iris: ``_synthesize_with``, ``_transcribe_with`` and ``_describe_with`` of their plugins, through
  ``stats.track_engine``.

A successful call is a sample of ``engine.seconds``, a failed one (an exception, or no text/audio) of
``engine.failed_seconds``. Each is filed globally and by router, router+engine, router+scope (the cortex scope of the
turn: base, vessel, grillo, agent...) and purpose. The purpose is ``chat`` unless the caller says otherwise with
``with stats.purpose("name"):`` or ``@stats.with_purpose("name")`` (``recon``, ``debrief``, ``agent_round``,
``vessel_diary``, ``soul``, ``web_search``). A call that is cancelled is not counted: it is not the engine's doing.

The *Turns* section shows the general **average turn**: the mean of ``turn.seconds``, recorded for every turn whatever
it was (any interface, the vessel's beats, and agent turns), with p50 and p95 and a row per interface and kind.
