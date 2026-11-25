B. Component Classification
===========================

Components are classified by **what they do and what they are coupled to**, not
by directory. Classes used:

* **Core**: stays in the main repository.
* **Extension**: becomes an independently installable extension.
* **External service**: runs outside SyntH; an extension only integrates it.
* **Uncertain**: needs a function-level coupling analysis first.

Where a classification is a maintainer decision it is tagged **Decision**.
Where it is inferred from names only (the component was not inspected in depth)
it is tagged **Uncertain** and stays that way until the function-level matrix
exists (see :doc:`api_inventory`).

Classification table
--------------------

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Component (location)
     - Class
     - Basis
   * - GRILLO (``plugins/grillo``, ``plugins/grillo_plugin.py``)
     - Core
     - **Decision**.
   * - SOUL (``core/soul``, ``plugins/soul_plugin``)
     - Core
     - **Decision**.
   * - Cortex and cognitive infrastructure (``core/cortex_*``, ``core/prompt_*``,
       ``core/message_chain.py``, ``core/message_queue.py``, ``core/action_parser.py``)
     - Core
     - **Decision** ("Cortex and essential cognitive infrastructure").
   * - Goals (``plugins/goals``)
     - Core
     - **Decision** ("goal infrastructure"). Lives in ``plugins/`` today and
       creates its own tables inline.
   * - Rift Vessel (``interface/vessel_interface.py``, ``core/vessel_*``,
       ``interface/message_send_utils.py``)
     - Core
     - **Decision**.
   * - Vessel adapters (``plugins/rift_vessel/minecraft``, ``retroxr``,
       ``rift_vessel/skyrim``, ``rift_vessel/vrchat``)
     - Extension
     - **Decision**. Today ``plugins/rift_vessel/`` mixes Vessel code and
       adapters; the split is non-trivial.
   * - Karada State Server (``core/animation_handler.py``,
       ``core/karada_*.py``)
     - Core
     - **Decision**. Stays active with no renderer installed.
   * - VRM renderer / avatar model (``core/avatar_render``, ``vrm_*`` payloads)
     - Extension
     - **Decision**. Installed by default. Future renderers (live2d, inochi2d)
       follow the same shape.
   * - Statistics (``core/stats_registry.py``, ``core/stats_sections.py``)
     - Core
     - **Decision**.
   * - Calendar and event system (``scheduled_events``,
       ``core/event_dispatcher.py``, ``plugins/event_plugin``)
     - Core
     - **Decision**. ``event_plugin`` lives in ``plugins/`` today and must be
       reclassified as Core code.
   * - WebUI (``core/webui.py``, ``core/webui_templates``,
       ``res/synth_webui``)
     - Core
     - **Decision**. Extensions plug into fixed WebUI slots.
   * - Dashboard (``core/webui_templates/sections/dashboard.html``,
       ``res/synth_webui/js/dashboard.js``)
     - Extension
     - **Decision**.
   * - Synth Stage (``frontend/``, mounted at ``/stage``)
     - Extension
     - **Decision**.
   * - AzuraCast / radio (``plugins/radio_host``)
     - Extension
     - **Decision**.
   * - Harmony (``core/external_endpoints/adapters/harmony_ai_adapter.py``)
     - Extension
     - **Decision**.
   * - Zen integration
     - Extension
     - **Decision**. Provided by SyntH, ``default_install``, removable.
   * - Zen LLM Engine container
     - External service
     - **Verified**: a separate image
       (``docker-compose.yml``). Shipped because it is free and needs no key.
   * - Every LLM engine (``engines/external_engines``: ``anthropic``,
       ``gemini_api``, ``openapi``, ``openrouter``, ``xai_grok``; dev engines)
     - Extension
     - **Decision**: no native engines remain in the Core.
   * - Vox / Auris / Iris / Live engines (``plugins/vox_engines``,
       ``auris_engines``, ``live_engines``, ``engines/live``)
     - Extension
     - **Decision**. The engine *hosts* (``vox_plugin``, ``auris_plugin``,
       ``iris_plugin``, ``media_dispatcher``) are **Uncertain**.
   * - Every interface: ``telegram_bot``, ``telegram`` (userbot),
       ``discord_interface``, ``matrix_interface``, ``fluxer_interface``,
       ``openai_api_server``
     - Extension
     - **Decision**. ``synth_webui`` is part of the WebUI and stays Core.
   * - Remaining plugins: ``agent_plugin``, ``agpeer``, ``ai_diary``,
       ``bio_manager``, ``blocklist``, ``debrief``, ``document_skill``,
       ``emotion_manager``, ``facial_expression_plugin``, ``home_assistant``,
       ``mate_engine``, ``memory_search``, ``message_map``, ``message_plugin``,
       ``pdf_voice``, ``recent_chats``, ``recon``, ``time_plugin``,
       ``tts_lipsync``, ``weather_plugin``, ``web_search``,
       ``web_search_plugin``
     - Uncertain
     - Not inspected one by one. Known: ``message_plugin`` is the only member
       of ``CORE_PLUGIN_SHORT_NAMES`` (**Verified**); ``emotion_manager`` is
       imported from the Core in 7 places and ``recon``/``debrief`` are driven
       by Core hooks (**Verified**), so they behave like Core;
       ``tts_lipsync`` and ``facial_expression_plugin`` are Karada clients;
       ``web_search`` vendors SearXNG (an external service). The function-level
       matrix decides each one.
   * - ``plugins_dev/`` and ``interface_dev/``
     - Extension (dev)
     - ``example_*``, ``reddit_*``, ``x_interface``, ``terminal``, ``gasmask``
       and the two Zen/Selenium plugins. **Decision**: they leave the Core
       repository (see below).

Per-component cards
-------------------

Each card covers the fields the brief requires. Numbers are **Verified** from
this investigation unless marked.

Telegram: ``telegram_bot`` and ``telegram`` (userbot)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Current implementation
   ``interface/telegram_bot`` (3,470 LOC) and ``interface/telegram`` (about
   4,230 LOC plus a JS file), duck-typed interfaces with the path
   ``telegram_bot/<chat>[/<thread>]``.
Current dependencies
   30 distinct ``core`` modules for ``telegram_bot`` (35 for the userbot,
   including ``core.webui`` and ``webui_auth``), ``python-telegram-bot`` and
   ``telethon`` (both base dependencies of the Core), the plugin tables
   ``message_map`` and ``blocklist``, and ``py-tgcalls`` installed at runtime by
   ``core/package_installer.py``.
Core coupling
   Very high. 123 string references to ``telegram_bot`` in 37 files of
   ``core`` and ``plugins`` (excluding ``webui.py``), including
   ``interface_paths``, ``message_registry``, ``prompt_engine``,
   ``transport_layer``, ``config_manager`` (reload handler) and the WebUI
   JavaScript (``main.js``, ``history.js``). ``"telegram_bot"`` appears as a
   fixture string in 122 test files. ``main.py`` references the userbot.
Migration complexity
   **High**, for both. This is accumulated hard-coding, not an inherent need.
What must remain in Core
   Nothing Telegram-specific. The generic interface contract, Interface Path
   storage, aliases and the message chain remain in the Core.
What can become an extension
   Both interfaces, their commands, calls and their private tables.
Potential regressions
   History and alias lookups (``match_chat_level`` matching ``chat/%``), thread
   handling, trainer ids (``TRAINER_IDS`` format ``"telegram_bot:123,..."``),
   WebUI display of Telegram chats, call stack for the userbot.
Unresolved questions
   Who owns ``message_map`` and ``blocklist`` (Telegram-only, or shared)?
   How do calls (``CallProvider`` registration in ``core/call_manager.py``)
   cross the process boundary? See :doc:`open_questions`.

Fluxer, ``openai_api_server``, Matrix, Discord
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Current implementation
   Fluxer 1,270 LOC; ``openai_api_server`` 1,343 LOC (+47 JS); Matrix 1,568
   LOC; Discord 4,392 LOC (+49 JS).
Core coupling
   Distinct ``core`` modules: Fluxer 15, ``openai_api_server`` 13, Matrix 15,
   Discord 32. References from outside: Fluxer 3 (``message_chain``,
   ``transport_layer``, ``vox_plugin``), Matrix 31 in 10 files, Discord 65 in
   26 files (including ``core.live_*`` and ``call_manager``), and Discord calls
   ``openrouter._openai_chat_completion`` directly (``engines``).
Migration complexity
   Fluxer **low**; ``openai_api_server`` low to medium; Matrix medium; Discord
   **high** (live/voice).
Potential regressions
   ``openai_api_server`` keeps the legacy ``ollama_serve`` identifier, the
   ``OLLAMA_*`` keys and ``/api/tags`` routes (see :doc:`migration_plan`, M7).
   Matrix stores one room under two prefixes (see
   :doc:`current_architecture`).
Unresolved questions
   New identifier for ``ollama_serve``; destination of the legacy routes;
   whether the default port 11435 stays.

Zen
~~~

Current implementation
   See :doc:`current_architecture` (section "Zen, as implemented today").
Core coupling
   About 150 scattered lines: seeds, preset registration, ``_pick_preferred_cortex``,
   clamp and timeout logic, ``CORTEX_LOCAL_ENGINES``, ``ZEN_DOUBLE_PROMPT``
   and migrations. Plus 935 LOC of Selenium-driven TTS plugins in
   ``plugins_dev/``.
Migration complexity
   Medium. The container is already external; the work is generalising or
   deleting the Zen-specific logic in the Core.
What must remain in Core
   The generic endpoint mechanism (``external_endpoints``) and the explicit
   "no Cortex" fallback message.
What can become an extension
   The endpoint registration/preset and the compose service. **It is only an
   OpenAI-compatible endpoint**: it does not bring Iris or vision pieces; if the
   engine offers vision, that is Zen's concern and the Core attaches to it like
   to any other endpoint. The Selenium plugins move to the Zen ecosystem (not
   the Core).
Potential regressions
   Fresh installs lose their default ``BASE_CORTEX``; the explicit fallback
   message replaces it. Tests that use ``zen-llm-engine`` as a fixture
   (``test_zen_endpoint_no_resurrect``, ``test_db_auto_heal``,
   ``test_setup_page``, ``test_stats_server``).
Unresolved questions
   Exact current behaviour with no endpoint (**Unknown**, run
   ``cortex_fallback``/``_heal_cortex_config`` paths); fate of the legacy
   ``selenium-llm-engine`` rename code.

Karada State Server and the VRM renderer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Current implementation
   See :doc:`current_architecture`. State and transports are generic in shape
   but the **wire protocol and part of the state are VRM-specific**.
Core coupling
   ``core/avatar_render/`` and the ``vrm_*`` payload types; in-tree callers in
   ``vox_plugin``, ``tts_lipsync``, ``emotion_manager``,
   ``facial_expression_plugin``, ``persona_manager``, ``retroxr``.
Migration complexity
   **High**: requires splitting renderer-independent logical state (animation
   intent, expressions, audio, presence) from renderer-specific data.
What must remain in Core
   The state server, the transport ABC and the public API, active with zero
   renderers. Anyone may attach to it; in principle a CLI renderer is possible.
   With no renderer the chat page simply shows no 3D environment.
What can become an extension
   The VRM renderer (installed by default); later live2d and inochi2d.
Potential regressions
   Synth Stage and the classic WebUI depend on ``vrm_*`` messages; RetroXR
   needs a model and should declare the VRM renderer as a **dependency**;
   Minecraft does not need one.
Unresolved questions
   Which Vessel adapters really need the model (check each).

Rift Vessel and its adapters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Current implementation
   ``interface/vessel_interface.py`` (4,276 LOC),
   ``interface/minecraft_provisioner.py`` (886 LOC),
   ``interface/message_send_utils.py`` (1,355 LOC) and
   ``plugins/rift_vessel/``. ``karada_bridge.py`` adds a Karada transport,
   ``retroxr.py`` registers a stats section; Minecraft uses ``mineflayer``
   (npm) and ``subprocess``.
Core coupling
   Deep, by design: the Vessel is Core. Adapter layering is documented in
   ``AGENTS.md`` section 4 but no enforcing test was found (**Unknown**).
Migration complexity
   Medium to high, mostly because Vessel and adapter code share a directory.
What must remain in Core / become an extension
   Core: the Vessel and its API. Extensions: every adapter.
Unresolved questions
   Exact boundary files; how adapters that spawn Node processes fit the
   process-sandbox model.

Dashboard, Synth Stage, Harmony
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Dashboard
   359 + 1,054 LOC of HTML and JS, **no** Python ``core`` imports; it calls
   core REST endpoints (``/api/config``, ``/api/components``, ``/api/about``,
   ``/api/emotion-state``, ``/api/agent/run``). Reverse coupling is hard-coded
   navigation: ``core/webui_templates/synth_webui_shell.html`` (a nav button and
   panel) and two ``allowed_sections`` whitelists in ``core/webui.py``. The test
   ``tests/test_dashboard_section_presence.py`` string-asserts those. The
   existing ``register_plugin_*`` hooks (``core/webui.py``) cover JS, API routes,
   routers and sub-tabs, **not** top-level tabs. Complexity: low to medium.
Synth Stage
   ``frontend/`` (Vue 3, pnpm; about 5.5k LOC), a pure client of ``/ws``,
   ``/api/karada/*``, ``/api/audio/stream``, ``/skins``, ``/uploads`` and
   ``/avatars``. The Core mounts ``frontend/dist`` at ``/stage`` if present
   (``core/webui.py``) and sets ``STAGE_AVAILABLE``; the ``Dockerfile`` has a
   Node build stage. The backend already runs without it. Complexity: low; the
   work is the Docker build, non-Docker install and documentation. No pytest
   covers it, only Node scripts under ``frontend/scripts/``.
Harmony
   ``harmony_ai_adapter.py`` (403 LOC) plus ``providers/harmonyai.json``,
   isolated, but ``EndpointProtocol.HARMONY`` is an enum member and
   ``probe.get_adapter_for_endpoint`` is a hard-coded ``if`` chain. Harmony
   workarounds also live inside ``openai_compat.py``. A stored ``harmony``
   protocol silently degrades to ``CUSTOM`` if the adapter is missing.
   Complexity: low to medium.

Engines (LLM, Vox, Auris, Live)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

LLM engines
   7,128 LOC across five modules, 18 distinct ``core`` imports
   (``prompt_engine``, ``live_session_manager``, ``cortex_registry`` ...). Core
   and Discord hard-code engine names; ``cortex_registry`` has a fallback import
   of ``engines.external_engines.<name>``. They overlap with
   ``external_endpoints`` adapters and presets. **Decision**: no native engines
   remain. **Unknown**: where the line falls between the Core endpoint/Cortex
   mechanism and an engine extension (``external_endpoints``,
   ``EndpointProtocol``, ``probe.get_adapter_for_endpoint``).
Vox / Auris
   ``vox_engines`` 1,103 LOC, ``auris_engines`` 722 LOC; host lists hard-coded
   in ``vox_plugin`` and ``auris_plugin``. Install-time extras and
   ``modules_registry`` already exist. Complexity: medium.
Live
   ``engines/live`` and ``plugins/live_engines`` (419 + 518 LOC). Discord and
   ``core/live_session_manager.py`` import the base types, so the base must
   become a public API. Complexity: high.

``plugins_dev/`` and ``interface_dev/``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Decision**: development extensions live in a separate repository. A mountable
``plugins_dev`` root remains for convenience so developers need not create a
repository to test a plugin that will land elsewhere. The two Selenium plugins
move to the Zen ecosystem; ``tests/plugins/test_zen_ttsfree.py`` goes with them.
A manifest flag ``experimental`` marks such extensions with a tag and a
pre-configured disclaimer in the extension store.
