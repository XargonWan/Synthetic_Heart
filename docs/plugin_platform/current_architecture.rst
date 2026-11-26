A. Current Architecture
=======================

This chapter describes how SyntH worked **on 2026-11-23, before the extraction began**.
Nothing here is a proposal. It is kept as the baseline the milestones were measured
against, so it still names components that have since moved (Telegram, Fluxer, Matrix, the
OpenAI server, Harmony, Fish Audio, the radio host); see :doc:`migration_plan` for where
each one stands now.

Startup and initialization
--------------------------

**Verified** (``main.py``, ``core/core_initializer.py``):

#. ``main.py`` loads ``.env`` at import, runs ``core.legacy_state``
   migration, then ``asyncio.run(start_application())``.
#. The database is retried (30 attempts, 2 s apart). ``initialize_database``
   runs ``init_db`` and persists bootstrap configuration; a backup scheduler
   starts.
#. A restart loop calls ``CoreInitializer.initialize_all()``
   (``core/core_initializer.py``), then starts the WebUI interface and the
   ``message_queue`` consumer task.
#. A restart clears ``INTERFACE_REGISTRY`` and ``PLUGIN_REGISTRY`` and loops.
   ``sys.modules`` is not purged, so module-level code probably does not
   re-run. **Unknown**: not exercised in this investigation.

``initialize_all`` performs, in order:

#. ``_initialize_registries``: ``register_default_engines`` scans
   ``engines/*/*_base.py`` and calls each module's ``discover_and_register``
   (``core/cortex_registry.py``).
#. Pre-load of the ``BASE_CORTEX`` setting and registration of external
   endpoints.
#. ``_load_cortex_engine`` (``core/plugin_instance.py::load_plugin``).
#. ``_load_plugins()`` (plugin discovery, below).
#. ``ensure_plugin_tables()``, validation-rule auto-registration, the first
   ``config_registry.load_all_from_db()``.
#. ``start_pending_async_plugins()``, the persona manager.
#. ``_discover_interfaces()`` (interface discovery, below).
#. A second ``load_all_from_db``, ``_build_actions_block()``,
   ``_initialize_interface_instances()`` and ``_start_interfaces()``.

Interfaces start as ``asyncio.create_task(interface.start())``.

.. _pp-doc-discrepancies:

Documentation and code disagree
-------------------------------

The brief asks to report discrepancies instead of silently choosing one side.

.. note::

   **Status (2026-11-23).** The documentation side of these discrepancies was
   corrected (M0): ``docs/component_pattern.rst``, ``docs/interface_path.rst``,
   ``docs/chat_context.rst``, ``docs/interfaces.rst``, ``docs/architecture.rst``,
   ``docs/dev_components.rst``, ``docs/two_phase_init_implementation.rst``,
   ``docs/llm_engines.rst`` (rewritten), ``docs/gemini/synth-live-voice-integration.md``;
   ``docs/zen_driver_recovery.rst`` was removed (it documented a module that no
   longer exists). The table below is kept as the record of what was wrong. The
   items that are **code, not docs**, are still open and deliberately untouched:
   the stale ``pyproject.toml`` include lists, ``interface_dev/example_dev_interface.py``,
   the ``cortex_login`` stub, ``task_cancellable``, the ``speech_zen_elevenlabs``
   reference and the ``/api/karada/ws`` docstring.

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Document
     - Says
     - Code says
   * - ``docs/component_pattern.rst``
     - A separate ``_initialize_plugin_instances()`` phase exists.
     - No such method. Plugins are instantiated inside ``_load_plugins``; only
       an async ``start()`` is deferred until after config load.
   * - ``docs/interface_path.rst``
     - ``parse_interface_path`` returns a dict; Matrix uses ``matrix/``.
     - It returns a tuple ``(name, [levels])``; the Matrix registry key is
       ``matrix_chat``. The history table column is ``created_at``, not
       ``timestamp`` as in ``docs/chat_context.rst``.
   * - ``docs/interfaces.rst``, ``docs/architecture.rst``
     - Interfaces extend ``AIPluginBase``.
     - Interfaces are duck-typed; there is no interface base class.
   * - ``docs/llm_engines.rst``, ``docs/zen_driver_recovery.rst``
     - ``ZenLLMBase`` and ``llm_engines/zen_*.py``.
     - Neither exists in the repository.
   * - ``pyproject.toml``
     - ``[tool.setuptools.packages.find]`` lists ``cortex*`` and
       ``llm_engines*``.
     - Those directories do not exist; ``engines*`` is missing.
   * - ``core/webui.py`` ``cortex_login``
     - (Zen login endpoint)
     - A stub that returns HTTP 422.
   * - ``core/message_queue.py``
     - Comments say Zen is not cancellable.
     - ``task_cancellable`` is read but never set anywhere in the repository.
   * - ``core/action_parser.py``
     - References ``speech_zen_elevenlabs``.
     - No such action exists.
   * - ``interface_dev/example_dev_interface.py``
     - Imports ``BaseInterface`` from ``core.interface_adapters``.
     - ``core/interface_adapters.py`` exists, but it only defines
       ``register_interface_with_trainer``, ``create_generic_adapter`` and
       ``create_generic_reply_function``: neither ``BaseInterface`` nor
       ``register_interface`` is there, so the example fails to import.

Plugin and interface discovery
------------------------------

**Verified** (``core/core_initializer.py``):

* ``_load_plugins`` (``rglob("*.py")``) walks ``plugins/`` and, only when
  development components are enabled, ``plugins_dev/`` and ``interface_dev/``.
  ``is_plugin_candidate`` skips ``_*.py``, ``__init__.py``, tests,
  ``node_modules`` and SearXNG paths.
* A module is a plugin if it exports a module-level ``PLUGIN_CLASS`` that has
  ``get_supported_actions`` (or ``get_supported_action_types``) and a
  no-argument constructor. The registry key is the last segment of the module
  name.
* A plugin file is **rejected before import** only if a text scan finds a write
  to ``message_queue._queue``. That is the single pre-import check; it is
  grep-level, not a security control.
* Interfaces are found with ``pkgutil.iter_modules`` over ``interface/`` in
  ``_discover_interfaces``; each module exposes ``initialize_interface()``,
  which calls ``register_interface``. A directory outside ``interface/`` and
  ``plugins/`` is not scanned.
* Media engines self-register at import (``register_vox_engine``,
  ``register_auris_engine``, ``register_iris_engine``,
  ``register_live_engine``); the host lists are hard-coded in
  ``plugins/vox_plugin`` and ``plugins/auris_plugin``, and
  ``media_dispatcher`` for Live.
* Every import, instantiate and start step has its own ``try``/``except``. A
  failure is appended to ``startup_errors`` and loading continues. This is the
  existing "an optional plugin must not break the Core" mechanism.

Base classes and the hook protocol
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Verified** (``core/plugin_base.py``, ``core/ai_plugin_base.py``):

* ``PluginBase`` offers ``start``, ``stop``, ``is_enabled``, ``get_metadata``,
  ``get_display_name`` and ``get_history_contributions``. ``AIPluginBase`` is the
  engine base (``generate_response``, ``handle_incoming_message``, ...).
* Subclassing is **not required**: ``plugins/time_plugin/time_plugin.py`` is a
  plain class. 35 files subclass either base.
* The Core calls plugin hooks by duck typing over ``PLUGIN_REGISTRY.values()``:
  ``on_debrief``, ``get_recon_contributions``, ``get_recon_key``,
  ``is_recon_eligible``, ``detect_language``, ``detect_tone``,
  ``get_static_injection``, ``get_history_contributions`` and
  ``get_prompt_instructions``. Each call is wrapped in its own ``try``/``except``;
  a failure is logged and ignored. Recon and Debrief are gated by the
  ``ENABLE_RECON`` and ``ENABLE_DEBRIEF`` configuration keys.

Registries
~~~~~~~~~~

**Verified**: three module-level registries in ``core/core_initializer.py``:
``ACTION_REGISTRY`` (actions), ``PLUGIN_REGISTRY`` and ``INTERFACE_REGISTRY``.
``register_interface(name, obj)`` requires a non-empty ``display_name``, raises
``ValueError`` otherwise, **overwrites silently** on a name collision, and calls
``register_action`` for each key of ``get_supported_actions()`` **only when the
interface is enabled** (a disabled interface is registered without its actions);
a duplicate action name is overwritten by the last registration (the last
handler wins). These behaviours are pinned by
``tests/test_extension_baseline_contracts.py``.

A second interface registry, ``core.interfaces_registry.InterfaceRegistry``
(instance map, trainer ids, capabilities), is used by Matrix, Fluxer and part
of Discord. ``telegram_bot`` does not register there, and it removes itself from
``INTERFACE_REGISTRY`` only (``shutdown_interface``). **The two registries are
not kept in sync.**

Enable, disable, hot reload
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Verified**: the persistent keys are ``PLUGIN_ENABLED__<short_name>`` and
``INTERFACE_ENABLED__<name>``. A disabled plugin is not imported at all (it is
kept as a "ghost" component record). ``CoreInitializer.disable_plugin`` /
``enable_plugin`` (``enable_plugin`` reloads with ``importlib.reload``) and
``disable_interface`` / ``enable_interface`` work at runtime, exposed through
``POST /api/components/toggle``. ``CORE_PLUGIN_SHORT_NAMES`` contains only
``message_plugin`` and ``CORE_INTERFACE_NAMES`` only ``synth_webui``.
Plugin shutdown happens only on a WebUI disable and in ``cleanup_components``;
the application-level shutdown stops interfaces only.

Manifest, versions, compatibility
---------------------------------

**Verified**: there is **no** manifest, no ``plugin.json``, no version
constraint, no ``requires``, no Core-compatibility or API-version mechanism.
The nearest thing is ``get_metadata()`` (``core/plugin_base.py``), whose
``version``, ``author`` and ``type`` keys are documented as "informational
only"; only 16 files override it. ``guide.md`` and ``icon.png`` sidecar files
are found by convention. ``depends_on`` metadata is advisory: a warning is
logged if the target is not loaded, and loading is never blocked (used by 3
plugins). ``docs/plugins.rst`` states a plugin needs only a file in the right
directory.

Reaching Core services (no dependency injection)
------------------------------------------------

**Verified** (``grep`` over ``plugins/``): plugins import ``core.*`` directly. Of
146 Python files under ``plugins/`` (excluding SearXNG and ``node_modules``),
**89 import from ``core``**. The most used targets, by plugin import lines:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Core module
     - Import lines
   * - ``core.config_manager``
     - 81
   * - ``core.logging_utils``
     - 77
   * - ``core.core_initializer``
     - 69
   * - ``core.db``
     - 58
   * - ``from core import X`` (``message_queue`` 13, ``core_initializer`` 11,
       ``stats_registry`` 8)
     - 52
   * - ``core.variables_engine``
     - 30
   * - ``core.interface_path_utils``
     - 21
   * - ``core.config``, ``core.animation_handler``
     - 19 each
   * - ``core.vessel_registry``, ``core.ai_plugin_base``
     - 15 each

There are also 158 ``from plugins...`` imports **between** plugins, three from
``plugins`` into ``interface``, and **reverse** coupling from the Core into
plugins (``core/db.py`` hard-codes the plugin table initialisation list;
``plugins.emotion_manager`` is imported from 7 Core places).
``core/optional_plugins.py`` is a guarded-access helper that makes a missing
Vox plugin mean "feature off".

AGENTS.md section 4 states a layering rule for Rift Vessel adapters. **Unknown**
whether it is mechanically enforced: no enforcing test was found.

Configuration and database access
---------------------------------

**Verified** (``core/config_manager.py``, ``core/db.py``):

* ``config_registry`` declares keys with ``get_var(key, default, label=,
  group=, component=, sensitive=, hidden=, needs_component_reload=,
  allow_env_override=)``. Resolution order: getter, then an env var with the
  exact key name, then the database, then the default. Keys are **global**; the
  ``component`` label is metadata, not a namespace. Keys that are not registered
  before ``load_all_from_db`` never receive their stored value.
* Plugins call ``core.db.get_conn_ctx`` directly (46 plugin imports). Thirteen
  plugin files create tables inline with ``CREATE TABLE`` (for example
  ``ai_diary``, ``blocklist``, ``goals``, ``grillo_impl``); tables are **not
  namespaced**. ``init-db.sql`` has 18 ``CREATE TABLE`` statements and
  ``core/migrations.py`` holds the Core migrations. There is no per-plugin
  credential or ACL.

Agentic runtime and tools
-------------------------

**Verified**:

* Tools **are** actions. ``ToolRegistry`` (``core/tool_registry.py``) is rebuilt
  from the actions block. A plugin exposes a tool by returning it from
  ``get_supported_actions()``; there is no tool-specific plugin API.
* ``core/agent_router.py`` classifies a turn, ``core/agent_core.py`` runs the
  loop, and ``core/agent_tool_executor.py`` is the single execution gate.
* The MCP client (``core/mcp_bridge``, configured by ``config/synth_mcp.json``)
  supports stdio, SSE and HTTP and namespaces tools as ``mcp_<server>_<tool>``.
  ``connect_all()`` runs once and is non-fatal.
* ``core/mcp_bridge/server.py::build_server`` can expose every internal action
  as an MCP tool. **Unknown** whether any deployment runs it; nothing in
  ``main.py`` or ``core/`` starts it.
* Prompt scope (``core/prompt_engine.py``): an explicit ``scope`` wins;
  otherwise an action with ``external_effects`` goes to the ``agent`` scope;
  ``vessel_*`` and ``agent_*`` prefixes map to their scopes; the default is
  ``core``.

Security and permission mechanisms that exist
---------------------------------------------

**Verified**: there is **no plugin permission model**: no capability
declaration, no import restriction, no sandbox. A plugin runs in-process with
full access. What exists:

* ``core/action_safety.py::is_action_allowed_for_execution``: per-action
  ``security_level`` and ``external_effects`` (**self-declared by the plugin**),
  combined with ``SYNTH_AUTONOMY_MODE`` and allow-list keys. Human-triggered
  actions are always allowed.
* Narrow guards: ``core/outbound_file_utils.py`` (sandbox roots
  ``AGENT_FS_ROOTS``), ``core/url_safety.py``, ``core/delivery_guard.py``,
  ``core/webui_auth.py``, ``core/external_endpoints/crypto.py``. The gasmask
  plugin is prompt-level persona protection, not a technical control.
* ``plugins/agent_plugin`` has sandboxed file actions; ``agent_run_shell`` is
  refused on a bare host unless ``AGENT_SHELL_ALLOW_HOST`` is set.

Dependencies and execution
--------------------------

**Verified**: one global dependency list in ``pyproject.toml`` (about 55
packages, including every interface and engine library such as ``discord-py``,
``matrix-nio``, ``python-telegram-bot``, ``telethon``, ``google-genai``)
with extras ``local-voice``, ``vosk``, ``whisper``, ``tts-local``. ``uv.lock``
is committed and the Dockerfile runs ``uv sync --frozen``. No plugin carries its
own requirements, with one exception: ``plugins/rift_vessel/minecraft/mineflayer/
package.json`` (npm). Two runtime installation helpers exist:
``scripts/modules_registry.py`` + ``module_installer.py`` (install-time modules
for TTS/STT/vision) and ``core/package_installer.py`` (a fixed
``ALLOWED_REQUIREMENTS`` allow-list, installed into ``<data>/pylibs`` and
appended to ``sys.path``). SearXNG is vendored under
``plugins/web_search/searxng-runtime`` with its own venv in the Dockerfile.

Background work: 27 plugin files use ``asyncio.create_task``, two use
``threading.Thread``, nine use ``subprocess``. There is no per-plugin task
ownership, supervisor or resource limit.

Interface Path and the message path
-----------------------------------

Interface Path is an **existing virtual addressing mechanism**, not an HTTP
route, URL or filesystem path.

**Verified** (``core/interface_path_utils.py``):

* Format: ``interface_name/level1[/level2/...]`` split on ``/`` with no
  escaping, so a segment containing ``/`` breaks it. ``build_interface_path``,
  ``parse_interface_path`` (returns ``(name, [levels])``), ``extract_legacy_ids``
  (level 1 to ``chat_id``, level 2 to ``thread_id``) and
  ``is_valid_interface_path`` (only checks for empty segments).
* **The number of levels and their meaning belong to each interface**:

  .. list-table::
     :header-rows: 1
     :widths: 25 45 30

     * - Registry key
       - Path
       - Source
     * - ``telegram_bot``
       - ``telegram_bot/<chat_id>[/<thread_id>]``
       - ``interface/telegram_bot``
     * - ``telegram`` (userbot)
       - ``telegram/<chat_id or @user>[/<thread>]``
       - ``interface/telegram``
     * - ``discord_bot``
       - ``discord_bot/<guild>/<channel>[/<thread>]``, or
         ``discord_bot/<user_id>`` for a DM
       - ``interface/discord_interface``
     * - ``fluxer_bot``
       - ``fluxer_bot/<guild>/<channel>`` or ``fluxer_bot/<channel>``
       - ``interface/fluxer_interface``
     * - ``matrix_chat``
       - see below
       - ``interface/matrix_interface``
     * - ``ollama_serve``
       - ``ollama_serve/<conversation_id>``
       - ``interface/openai_api_server``
     * - ``synth_webui``
       - ``synth_webui/<session_id>``
       - ``core/webui.py``
     * - ``vessel``
       - ``vessel/<environment>/<world_token>``
       - ``interface/vessel_interface.py``

* **Matrix inconsistency (Verified, intent Unknown)**: the registry key is
  ``matrix_chat``, but inbound history writes use the literal ``matrix``, while
  the queue and ``resolve_and_touch`` use ``matrix_chat/...``. One room is
  stored under two prefixes. Whether this is intended is not established by the
  code.

Storage: ``chat_history_cache`` (``interface_path VARCHAR(512)``,
``created_at``; ``load_chat_history(path, match_chat_level=True)`` also matches
``chat_key/%``, the first two segments), ``interface_paths`` (primary key,
``last_used``, ``segment_labels``) and ``interface_aliases`` (``@username``
<-> numeric id; the numeric id is canonical in storage).

Inbound flow, for ``telegram_bot`` as the worked example:

#. ``handle_message`` builds the path, persists context
   (``add_message_to_context``), touches ``interface_paths``,
   ``resolve_and_touch`` and ``remember_message_alias``.
#. It calls ``core.message_queue.enqueue(bot, MessageWrapper,
   interface_id="telegram_bot", ...)``. The wrapper does not carry the path;
   the queue consumer rebuilds it with ``build_interface_path`` and sets
   ``context["interface_path"]``. Pending items with an identical path are
   coalesced.
#. The consumer dispatches ``plugin_instance.handle_incoming_message`` and the
   response runs through ``core.message_chain.handle_incoming_message``, which
   auto-injects a missing ``payload.interface_path`` into message actions and
   runs the actions through ``core.action_parser.run_actions``.

Outbound: ``core/action_parser.py::_dispatch_send_message`` resolves the
destination from ``payload.interface_path`` (parsed, then looked up in
``INTERFACE_REGISTRY``), else from ``original_message.interface_path``, else it
fails. It checks the capability gate and calls ``interface.send_message``.
``core/message_chain.py`` has hard-coded mappings
(``_INTERFACE_TO_MESSAGE_ACTION``: ``synth_webui`` to ``message_synth_webui``,
``radio_host`` to ``radio_speak``; ``vessel`` paths to ``vessel_<world>_say``),
and ``core/chat_context_manager.py`` has ``CONTEXT_LINK_MAP``, which is a user
configuration key (default ``{}``) that links several paths to one context, not a
list of interface names. An interface that is absent from
``_INTERFACE_TO_MESSAGE_ACTION`` (``fluxer_bot``, for one) is simply not
normalised: nothing breaks.

Implicit duck-typed interface contract (**Verified**, no formal base class):
required ``display_name``, ``get_interface_id()``, ``async send_message(payload,
original_message=None)``, ``get_supported_actions()`` and a module-level
``initialize_interface()``. Optional: ``start``/``stop``, ``is_enabled``,
``disabled_reason``, ``required_config_vars``, ``get_capabilities``,
``get_prompt_instructions``, ``validate_payload``, ``get_interface_path_hint``,
``send_activity``, ``set_avatar``, ``start_call``, ``on_generation_start`` /
``on_generation_end``, ``add_reaction``, plus module-level ``shutdown_interface``
and ``reload_interface``. Interface configuration is declared with
``register_exposed_var(..., component=<name>, needs_component_reload=True)``.

Karada State Server
-------------------

**Verified** (``core/animation_handler.py``, ``core/karada_transport.py``,
``core/karada_api.py``):

* ``KaradaStateServer`` is a lazily created singleton
  (``get_karada_state_server()``), in-memory only. State: the current animation
  state enum, the VRM model url/name/hash, face values, the active expression,
  the current audio (url, duration, lipsync, ``turn_id``) and presence.
  ``get_full_state()`` returns ``{vrm_model, animation, face_values, audio,
  [expression], [presence]}`` for late joiners.
* Writes: ``play_animation``, ``stop_animation``, ``transition_to``,
  ``set_vrm_model``, ``set_face_values``, ``push_face_expression``,
  ``set_presence``, ``broadcast_audio``, ``broadcast_sfx``, animation
  registration and search paths. Reads: ``get_full_state`` and the getters.
* In-process subscription: ``register_animation_state_changed_callback``.
  Transports implement the ``KaradaTransport`` ABC (``broadcast_*``,
  ``send_to_session``, ``preload_asset``, ``get_connected_sessions``): the
  WebUI ``WebSocketTransport``, ``KaradaApiTransport`` (``/api/karada/ws``) and
  ``RiftKaradaTransport`` (``plugins/rift_vessel/karada_bridge.py``).
* Public REST/WS API ``/api/karada``: ``GET /state``, ``/state/animation``,
  ``/state/model``, ``/state/face``, ``/state/audio``, ``/animations/...``,
  ``POST /action`` and ``WS /ws``. Authorisation is
  ``webui_auth.request_authorized``; **if no token is configured, access is
  open**, and there are no per-operation roles. In-process calls have no
  permission layer.
* **The wire protocol is VRM-shaped**: payload types ``vrm_animation_v2``,
  ``vrm_model``, ``vrm_face``, ``vrm_expression_set``/``_clear``,
  ``vrm_presence``, ``vrm_preload``, plus ``tts-play`` and ``sfx-play``. This
  matters for the decision that VRM becomes an extension.
* In-tree callers: ``plugins/vox_plugin`` and ``plugins/tts_lipsync``
  (``broadcast_audio``), ``plugins/emotion_manager`` (``set_face_values``),
  ``plugins/facial_expression_plugin`` (``push_face_expression``),
  ``core/persona_manager.py`` (animations), ``core/avatar_render/``,
  ``plugins/rift_vessel/retroxr`` (transport and presence), ``core/webui_call.py``.
* ``docs/``/code mismatch: the ``/api/karada/ws`` docstring lists message types
  such as ``state_full``, ``animation`` and ``audio``; the server actually pushes
  the raw payload types above plus ``state_full`` on connect.

Statistics
----------

**Verified** (``core/stats_registry.py``, ``core/stats_sections.py``,
``docs/statistics.rst``):

* ``StatsRegistry`` (lazily created by ``get_stats()``) takes **free-form dotted
  series names**, optionally labelled. There is no declaration step. Kinds:
  counter ``incr(name, n, **labels)``, timing ``observe(name, seconds, **labels)``,
  gauge ``gauge(name, v)`` and ``info(name, dict)``. A labelled call also writes
  the bare series. Nothing raises.
* Persistence: counters and timings are saved to ``state_path("stats.json")``
  (at most every 60 s and at exit), **not in the database**. Gauges and info
  are not persisted. Persistence is off under pytest or with
  ``SYNTH_STATS_PERSIST=0``.
* Sections: ``register_section(source, title, build(view) -> blocks,
  order=100)`` and ``unregister_section``; registering the same ``source``
  replaces the earlier section. The reference producer is
  ``core/vessel_stats.py`` (registered/unregistered by ``vessel_interface``).
* Query: ``StatsView`` (``count``, ``per_day``, ``timing``, ``gauge``, ...) and
  periods ``1h, 24h, 3d, 7d, 14d, all``. Consumers: ``GET /api/stats``
  (``core/webui.py::stats_overview``) and the Logs > Stats tab in
  ``res/synth_webui/js/main.js``.
* **No namespace or ownership is enforced**; the section ``source`` is the only
  ownership key.

Events and scheduling
---------------------

**Verified**: there is **no general event bus**. What exists:

* Scheduled events as database rows (``scheduled_events``) read through
  ``core/db.py::get_due_events`` and dispatched by
  ``core/event_dispatcher.py::dispatch_pending_events`` via
  ``message_queue.enqueue_event``. **Unknown**: which component actually calls
  ``dispatch_pending_events`` at runtime (no in-tree caller was found; the
  scheduler probably lives in ``plugins/event_plugin``).
* ``core/system_events.py``: a small database-backed log (boot, shutdown,
  component enabled/disabled, variable changed).
* Point callbacks: ``config_registry.add_listener``,
  ``register_reload_handler``, the Karada state callback,
  ``action_state_manager.register_state_changed_callback``,
  ``vad_service.register_handler``, optional ``on_generation_start``/``end`` on
  interfaces.

The calendar and event system are **Core** by maintainer decision; see
:doc:`component_classification`.

Zen, as implemented today
-------------------------

**Verified**: the Zen LLM Engine is an external container
(``xargonwan/zen-llm-engine``, ``docker-compose.yml`` service
``synth-zen-llm-engine``) that proxies a browser-driven chat UI to an
OpenAI-style API. There is **no** ``plugins/zen_*`` plugin that talks to it.
The SyntH side is:

* a preset ``providers/zen_llm_engine.json`` (protocol ``openai``, base URL
  ``http://synth-zen-llm-engine:8000``);
* ``core/external_endpoints/registry.py::ensure_default_zen_endpoint``, called
  from ``main.py``; it seeds the endpoint only when the registry is **empty**,
  and a user-deleted Zen stays deleted;
* seeds in ``init-db.sql`` (the ``external_endpoints`` row, plus
  ``BASE_CORTEX`` and ``ACTIVE_IRIS_ENGINE`` set to ``zen-llm-engine``,
  ``RESPONSE_TIMEOUT``) repeated in ``core/db.py``;
* Zen-specific logic in the Core: a 27000-character clamp in
  ``core/external_endpoints/bridges/cortex_bridge.py`` (Zen chunks above
  32000), ``core/db.py::_pick_preferred_cortex`` (sorts "zen" first),
  ``core/config.py`` / ``core/cortex_fallback.py`` (``CORTEX_LOCAL_ENGINES``
  default ``zen-llm-engine``), ``core/command_registry.py`` (``ZEN_DOUBLE_PROMPT``),
  migrations that rename ``SELENIUM_*`` keys to ``ZEN_*``
  (``core/migrations.py``), and comments/timeouts in ``agent_core.py`` and
  ``message_queue.py``.
* Cortex and the agent use Zen as an ordinary OpenAI-protocol endpoint wrapped
  by ``ExternalCortexEngine``.
* ``plugins_dev/zen_ttsfree.py`` and ``plugins_dev/zen_elevenlabs.py`` are
  different: they drive Selenium / ``undetected_chromedriver`` directly.
  ``selenium`` is **not** in ``pyproject.toml`` or ``uv.lock``.

When Zen is unreachable: ``timeout_breaker.py`` fails fast after two timeouts
(120 s cooldown), ``cortex_fallback.py`` runs primary, fallbacks, then a cached
response, and ``_heal_cortex_config`` repoints ``BASE_CORTEX`` when the endpoint
is disabled or removed. **Unknown**: the exact behaviour of a fresh install with
no endpoint at all.
