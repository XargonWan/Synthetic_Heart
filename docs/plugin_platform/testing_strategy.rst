J. Testing Strategy
===================

A **Proposal**. The repository has about 470 test files under ``tests/``
(including ``tests/interface/``, ``tests/plugins/``, ``tests/soul/``,
``tests/integration/``, ``tests/e2e/``). A bare ``uv run pytest`` takes roughly
five minutes and has known order-dependent failures (``config_registry``
pollution), so development uses **focused runs**; the wider relevant suite runs
for broad changes (``AGENTS.md`` section 9, ``CLAUDE.md``). Agentic/drone tests
must run with ``DB_HOST=127.0.0.1 DB_PORT=1`` because they otherwise write to
the live database.

Existing tests that must change
-------------------------------

* ``tests/test_dashboard_section_presence.py``: string-asserts the Dashboard nav
  and whitelists in the Core.
* The Zen tests: ``test_zen_endpoint_no_resurrect``, ``test_db_auto_heal``,
  ``test_setup_page``, ``test_stats_server`` use ``zen-llm-engine`` as a
  fixture or guard a shipped preset.
* ``tests/plugins/test_zen_ttsfree.py``: moves with the Selenium plugins.
* ``tests/interface/*`` and the 122 files using ``telegram_bot`` as a fixture
  string: replaced by a generic test interface.
* Tests that import ``engines.external_engines.*``.

Layers of new tests
-------------------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Layer
     - What it covers
   * - Characterisation (M0)
     - Pin today's behaviour before changing anything: ``INTERFACE_REGISTRY``
       register/overwrite, ``parse_interface_path`` and ``extract_legacy_ids``,
       ``load_chat_history`` with ``match_chat_level``, Karada getters/setters and
       ``get_full_state``, ``stats_registry`` counters/timings/sections,
       ``_dispatch_send_message`` resolution order.
   * - Unit
     - Manifest schema and validator; dependency resolver (cycles, conflicts,
       optional, version ranges); capability checker; ``storage`` namespacing;
       stats prefix enforcement; Interface Path declared-schema validation;
       repository index parsing, hash/signature verification.
   * - Integration
     - Gateway with a fake extension process: authentication, per-call
       capability checks, timeouts, per-call error isolation, supervisor restart
       with backoff; inbound goes through ``message_queue.enqueue`` and the
       message chain; outbound is invoked by the Core; Karada subscribe and
       snapshot over RPC; stats sections published remotely.
   * - Compatibility
     - Old ``chat_history_cache``/``interface_paths``/``interface_aliases`` rows still
       resolve after an interface moved out; ``core_api`` range mismatch refuses
       to start.
   * - Lifecycle
     - Install, on, off, update (with a failing new version rolls back from the
       local copy), uninstall (files deleted; data policy), partial uninstall
       recovery, ``default_install`` on fresh installs vs upgrades, repository
       unreachable, signature failure.
   * - Local repository
     - A repository added from a local path behaves like a remote one: the
       transport adapter returns the same index and files; install, update,
       rollback and uninstall pass identically against a local and a (fake)
       remote transport; a signature failure or hash mismatch is refused for a
       local repository too; a missing or unreadable path fails cleanly. The
       same repository content must be installable after being moved to another
       transport.
   * - Security
     - A **hostile extension** that tries: reading ``.env`` and the environment,
       opening the database, importing ``core``, importing another extension,
       calling an API without the capability, calling with another extension's
       token, flooding the gateway, enqueueing without the message chain, writing
       outside its data directory. Each must fail. Run in CI on Linux.
   * - Regression
     - The Core boots with **zero** extensions (no interface, engine, renderer,
       Dashboard, Stage, Zen); the explicit "no Cortex" message; Karada active
       with no renderer; Telegram absent leaves no reference in the Core
       (``rg -i telegram core/``); ``rg -i selenium`` finds only migrations.
   * - Conformance (extensions)
     - The developer kit's test: no ``core.*`` or ``plugins.*`` import, manifest
       valid, capabilities declared match calls made.
   * - Performance
     - RPC round-trip and throughput on the turn path (hooks, Recon, Debrief),
       large payloads (audio, attachments), many concurrent extensions.

Rule for every milestone
------------------------

A milestone is done when its listed validation criteria
(:doc:`migration_plan`) pass **and** the focused tests for the area it touched
pass (``uv run pytest tests/test_<area>.py``), with unrelated failures reported
instead of fixed.
