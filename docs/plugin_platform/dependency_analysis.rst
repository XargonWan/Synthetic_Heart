C. Dependency Analysis
======================

The brief asks for dependencies to be separated by kind. Counts come from
``grep`` over the 2026-11-23 tree; they measure **module-level** coupling. A
function-level graph is future work (see :doc:`api_inventory`).

Direct imports
--------------

Plugin to Core: see the table in :doc:`current_architecture`. 89 of 146
plugin ``.py`` files import ``core``. The five heaviest targets
(``config_manager``, ``logging_utils``, ``core_initializer``, ``db``,
``variables_engine``) already define the **minimum API surface**: configuration,
logging, registration, persistence and exposed variables.

Plugin to plugin: 158 ``from plugins...`` lines, 3 from ``plugins`` to
``interface``. Under the new model these become either **declared extension
dependencies** (only when it makes sense) or are removed.

Core to plugin (reverse coupling), **Verified**:

* ``core/db.py`` hard-codes the plugin table initialisation list.
* ``plugins.emotion_manager`` is imported from 7 Core places.
* ``core/optional_plugins.py`` wraps lazy imports for Vox.
* ``core/live_session_manager.py`` and ``core/live_tool_executor.py`` import
  ``plugins.live_base``; ``core/webui.py`` references engine names;
  ``core/cortex_registry.py`` falls back to importing
  ``engines.external_engines.<name>`` and ``engines.live.<name>``.
* ``interface/discord_interface`` imports ``engines.live.live_base`` and calls a
  private ``openrouter`` function.

Runtime dependencies
--------------------

* Every interface/engine library is a **base dependency** of the Core
  (``discord-py``, ``matrix-nio``, ``python-telegram-bot``, ``telethon``,
  ``google-genai``, ``openai``, ``moderngl``, ...); only ``local-voice``,
  ``vosk``, ``whisper`` and ``tts-local`` are optional extras.
* One real per-plugin runtime: Node (``mineflayer``) for Minecraft; SearXNG
  vendored under ``plugins/web_search/searxng-runtime``.
* ``selenium`` is **not** a dependency (see
  :doc:`component_classification`, Zen).

API dependencies
----------------

Surfaces that already behave like public APIs:

* REST: ``/api/karada/*`` (token optional), ``/api/stats``,
  ``/api/components/toggle``, ``/api/config``, plus others in
  ``docs/api_endpoints.rst``.
* WebSocket: ``/ws`` (WebUI) and ``/api/karada/ws``.
* MCP client (``core/mcp_bridge``) and the optional MCP server
  (``build_server``).
* OpenAI-style HTTP server (``interface/openai_api_server``, port 11435).

Shared state
------------

Process-wide singletons accessed by import: ``get_karada_state_server()``,
``get_stats()`` (``core/stats_registry.py``), ``config_registry``,
``PLUGIN_REGISTRY``/``INTERFACE_REGISTRY``/``ACTION_REGISTRY``,
``InterfaceRegistry``, ``message_queue``, the chat history cache. These are the
state that, under a process boundary, must be reached through RPC.

Event-based communication
-------------------------

No general bus (see :doc:`current_architecture`). Event-like couplings:
``scheduled_events`` rows, ``system_events``, config listeners and reload
handlers, Karada callbacks, action-state callbacks, and optional interface
hooks ``on_generation_start``/``end``. A hook protocol discovered by
``hasattr`` is also a coupling: the Core calls plugin methods by name
(``on_debrief``, ``get_recon_contributions``, ``get_static_injection``, ...).

Implicit dependencies (string names)
------------------------------------

Names that bind the Core to specific extensions, found by ``grep``:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Name
     - Where
   * - ``telegram_bot``
     - 123 references in 37 Core/plugin files; 122 test files
   * - ``ollama_serve``
     - ``plugins/message_plugin`` (action ``message_ollama_serve``), the LLM
       engines (``"ollama_serve": "send_message"`` maps), tests, docs
   * - ``zen-llm-engine``
     - ``init-db.sql``, ``core/db.py``, ``core/config.py``,
       ``core/cortex_fallback.py``, registry, tests
   * - interface/message names
     - ``core/message_chain.py::_INTERFACE_TO_MESSAGE_ACTION`` (``telegram_bot``,
       ``telegram``, ``discord_bot``, ``matrix_chat``, ``ollama_serve``,
       ``synth_webui``, ``radio_host``). Not blocking: an interface missing from it
       works (see :doc:`api_inventory`)
   * - ``CORE_PLUGIN_SHORT_NAMES``, ``CORE_INTERFACE_NAMES``
     - ``core/core_initializer.py`` (one entry each)
   * - Dashboard
     - ``synth_webui_shell.html`` and two ``allowed_sections`` lists in
       ``core/webui.py``
   * - Stage
     - ``core/webui.py`` (``/stage`` mount, ``STAGE_AVAILABLE``)
   * - Engine names
     - ``core/webui.py`` (model-key map), ``discord_interface``, ``vox_plugin``
       and ``auris_plugin`` module lists, ``media_dispatcher``
   * - Harmony
     - ``EndpointProtocol.HARMONY`` and the ``probe`` ``if`` chain

Filesystem coupling
-------------------

* ``skins/*/animations``, ``skins/*/persona.json``, ``res/synth_webui``,
  ``frontend/dist``, ``config/synth_mcp.json``, ``providers/*.json``.
* ``state_path("stats.json")``; the data root and ``<data>/pylibs``
  (``core/package_installer.py``); ``AGENT_FS_ROOTS`` sandbox roots.
* ``plugins_dev``/``interface_dev`` are enabled by a non-persistent WebUI
  toggle that restarts SyntH (``core/webui_templates/sections/plugins.html``).

Configuration coupling
----------------------

Keys are global strings in ``config_registry``. Interface settings carry the
interface name (``TRAINER_IDS = "telegram_bot:123,discord:456"``). Enable flags
are ``PLUGIN_ENABLED__*`` and ``INTERFACE_ENABLED__*``. ``OLLAMA_*`` and
``ZEN_*`` keys exist, and ``core/migrations.py`` renames legacy ``SELENIUM_*``
keys. There are no per-plugin namespaces.

Duplicated or overlapping functionality
---------------------------------------

The brief asks to identify overlap. **Verified**:

* Two interface registries, not kept in sync.
* Four engine registration mechanisms: native modules under
  ``engines/*/*_base.py``; database ``external_endpoints`` rows bridged by
  ``core/external_endpoints/bridges``; the hard-coded protocol-adapter ``if``
  chain; plugin-style engines in ``plugins/{vox,auris}_engines`` and
  ``plugins/live_engines``. Native engines duplicate what an adapter plus a
  preset already do.
* Plugin and interface discovery are two separate scans with two enable-flag
  families.
* Two ways to install optional dependencies at runtime (``module_installer`` and
  ``package_installer``) and one for build time (extras).
* Configuration keys can be declared in three places (``get_var``,
  ``register_exposed_var``, ``init-db.sql`` seeds).
