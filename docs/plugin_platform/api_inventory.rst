D. API Inventory
================

Two parts: what exists today (**Verified**), and the public contract the
platform proposes (**Proposal**). The proposed contract is built **on top of**
the existing subsystems. It does not create parallel ones; in particular there is
no second statistics system.

How extensions reach the Core (Proposal)
----------------------------------------

#. The Core runs an **extension gateway**. Each extension runs in its own
   process (see :doc:`security_analysis`) and connects to it.
#. The gateway authenticates each extension instance with a token issued at
   launch, checks on **every call** that the capability is declared in the
   manifest *and* granted, validates arguments, and applies rate limits and
   quotas.
#. A thin ``synth_sdk`` package (no Core code) wraps the client and the types.
#. The wire protocol is not chosen here (the brief says to avoid premature
   technology choices). Precedents in the repository: ``core/mcp_bridge``
   (stdio, SSE and HTTP clients with namespaced tools) and ``/api/karada/ws``
   (WebSocket with token).
#. **Core to extension** calls (hooks, ``send_message``, ``execute_action``)
   are RPC too, with timeouts and per-call error isolation, extending the
   existing per-hook ``try``/``except`` pattern.

The API grows by proposal: a developer who needs a missing capability asks the
maintainers, who may add an API; there is no other way in. An official extension
that cannot be built on the public API is either given a new API or is a
candidate to join the Core.

Existing APIs and the proposed public contract
----------------------------------------------

Method names below are **illustrative**.

.. list-table::
   :header-rows: 1
   :widths: 14 40 46

   * - Namespace
     - Existing backing (Verified)
     - Proposed public surface
   * - ``interfaces``
     - ``register_interface`` (``core_initializer``), ``INTERFACE_REGISTRY`` and
       ``InterfaceRegistry``; ``message_queue.enqueue``;
       ``_dispatch_send_message``; ``core/interface_path_utils``;
       ``interface_paths``; ``interface_aliases``; ``set_name_resolver``.
     - ``register``/``unregister`` (collision-checked, one registry);
       ``declare_path_schema`` (levels and their meaning, declared by the
       interface); ``inbound`` (the gateway calls ``message_queue.enqueue``, so
       the queue and message chain cannot be skipped); outbound ``send_message``
       invoked **by** the Core on the extension; ``resolve_name``; aliases;
       capabilities. History, ``interface_paths`` and aliases stay Core-owned.
   * - ``karada``
     - ``KaradaStateServer`` (``play_animation``, ``set_face_values``,
       ``push_face_expression``, ``broadcast_audio``, ``set_presence``,
       ``get_full_state``, ``add_transport``); REST/WS ``/api/karada``;
       ``KaradaTransport`` ABC.
     - Logical writes (animation intent, expression, audio asset, presence),
       reads (``get_full_state`` snapshot) and ``subscribe``. Renderer-specific
       data (VRM model, blendshape weights) moves to a renderer-scoped part of
       the API. Audio is passed as an **asset in the extension's data
       directory**, not as a free file path. Per-operation permissions, which do
       not exist today.
   * - ``stats``
     - ``stats_registry``: ``incr``, ``observe``, ``gauge``, ``info``,
       ``register_section``/``unregister_section``, ``StatsView``.
     - The same operations, with the gateway **enforcing the prefix**
       ``<extension_id>.`` and the section ``source``. Because
       ``register_section`` takes a callback, a remote extension **publishes
       blocks/series** instead, or the Core queries the extension over RPC with a
       timeout. No second subsystem.
   * - ``config``
     - ``config_registry`` (``get_var``, ``register_exposed_var``, listeners,
       reload handlers).
     - Schema declared in the manifest; an extension reads only its own keys;
       secrets are delivered only to it; change notifications via ``subscribe``.
       The global key namespace gets a per-extension prefix.
   * - ``storage``
     - ``core/db.py`` (``get_conn_ctx``), inline ``CREATE TABLE`` in 13 plugin
       files, hard-coded init list.
     - Data lives in the Core database, in a space per extension, but the
       extension has **no database access**: only this API. The API shape (KV or
       documents, tables declared in the manifest with Core-managed schema and
       migrations, or a parameterised SQL subset with quotas) is open; see
       :doc:`manifest_proposal` and :doc:`open_questions`.
   * - ``events``
     - ``scheduled_events``, ``event_dispatcher``, ``system_events``, config and
       Karada callbacks.
     - Schedule and receive events through the Core calendar/event system (Core
       by decision); topic subscriptions with per-extension namespaces.
   * - ``cortex`` / ``agents`` / ``tools``
     - ``ToolRegistry``, ``agent_core``, ``agent_tool_executor``, ``mcp_bridge``,
       ``action_safety``. Tools **are** actions.
     - Register actions with a schema; ``security_level`` and
       ``external_effects`` remain declarative but are **validated against the
       manifest permissions** instead of being trusted. Explicit tool API only if
       the function-level matrix shows the need.
   * - ``engines``
     - Four overlapping mechanisms (see :doc:`dependency_analysis`).
     - One engine-registration capability (LLM, Vox, Auris, Iris, Live) in place
       of module scanning and hard-coded lists. The Core keeps the endpoint
       mechanism; where exactly the line falls is open.
   * - ``vessel``
     - ``core/vessel_*``, ``vessel_interface``, ``vessel_registry``.
     - Vessel API for adapters (verbs, world state, body). Core.
   * - ``ui``
     - ``register_plugin_js/api_route/router/section_tab``
       (``core/webui.py``) which load code **into the Core process**.
     - Static assets declared in the manifest, served in an isolated context,
       placed only in **fixed slots**; they talk to the extension only through
       the API. See "WebUI slots" below.
   * - ``goals`` / ``soul`` / ``grillo`` / ``cortex`` internals
     - Core subsystems (``core/soul``, ``plugins/goals``, ``plugins/grillo``).
     - **Unknown** which, if any, need a dedicated API. The brief lists them as
       investigation targets, not predetermined APIs. The function-level matrix
       answers it.

WebUI slots (Proposal)
----------------------

The WebUI is Core. An extension (Dashboard, AzuraCast, Stage) may contribute UI
only to **fixed attachment points** the Core defines, so the main menu is not
cluttered. A starting set to validate with the maintainers:

* one entry point that lists extensions (a single menu item, with the
  extension pages as sub-sections);
* a panel on the extension's own page;
* widgets in predefined zones of existing pages (for example the dashboard
  grid, the stats page through ``stats`` sections);
* configuration form generated from the manifest schema.

The extension store lives in the existing **Plugins > Catalog** and **Plugins > Repositories** sub-tabs
(``core/webui_templates/sections/plugins.html``, today "Coming soon"). Repository
management is also available from a CLI.

Interface Path as a public API
------------------------------

Interface Path remains what it is: a virtual addressing mechanism. Proposal:

* The **shape is fixed** for the API, as now: ``name/level1[/level2/...]``,
  using ``build``, ``parse`` and ``resolve`` helpers.
* **Each interface declares its own paths**: how many levels and what they mean.
  The Core never interprets the levels. The Matrix prefix question is a problem
  of the Matrix interface and its data migration, not a Core decision.
* Existing paths in ``chat_history_cache``, ``interface_paths`` and
  ``interface_aliases`` must keep resolving. An interface that changes its path
  scheme ships its own data migration (precedent: ``core/migrations.py``).
* The escaping gap (no escaping of ``/``) is a known limit to document, not to
  silently redesign.
* The Core never needs to know that an interface is "Telegram". Verified in M3:
  ``message_chain._INTERFACE_TO_MESSAGE_ACTION`` lists a few legacy names, but an
  interface that is **not** in it already works (``fluxer_bot`` is not), so remote
  interfaces need no entry. The ``telegram`` entries are removed when Telegram is
  extracted (M5). ``CONTEXT_LINK_MAP`` is **user configuration** (default ``{}``),
  not a hard-coded name, and stays.

What the brief asked to investigate first
-----------------------------------------

The brief names three APIs to ground in existing code. Summary of where each
stands:

Karada State Server API
   Exists (in-process singleton, REST/WS, transport ABC). Gaps: no per-operation
   permission, open access when no token is configured, VRM-shaped wire format,
   no documented snapshot-plus-subscribe contract for third parties, WS
   docstring mismatch.
Statistics API
   Exists and is usable almost as is. Gap: no namespace ownership.
Interface API
   Does **not** exist as a formal contract. It exists as a duck-typed convention
   plus two registries. The platform must formalise it.

First work item of the implementation phase: done (M0)
-------------------------------------------------------

The **function-level matrix** is generated by
``scripts/plugin_platform_api_matrix.py`` and published as :doc:`api_matrix`: for
each extension candidate, the ``core`` symbols it imports or calls, grouped into
the proposed API namespaces. It is static analysis, so real usage can be larger.

What the first pass shows (**Verified**, from that page):

* 47 of 55 units import ``core``; 110 of 181 files.
* The cross-cutting namespaces are small: ``log`` (46 units, 13 symbols),
  ``config`` (36 units, 21 symbols) and ``storage`` (17 units, 13 symbols). A
  first API covering these serves most extensions.
* ``interfaces`` is the widest surface: 27 units and 78 distinct symbols (path
  helpers, message queue, chat history and context, transport). Telegram,
  the Telegram userbot and Discord alone use 70, 72 and 50 symbols across 30, 35
  and 32 Core modules. The interface API is therefore the largest design item.
* **Reaching into Core internals** (no API at all): 13 units read
  ``INTERFACE_REGISTRY`` directly, 13 read ``PLUGIN_REGISTRY``, 7 use the
  ``core_initializer`` singleton and 4 use the WebUI interface object. 11 units
  call the private ``core.db._get_db_type`` to write dialect-aware SQL, which the
  ``storage`` API makes unnecessary. A few units call other private functions
  (``message_queue._clear_item_in_flight``,
  ``chat_context_manager._resolve_context_path``, ...). Each needs a real API
  ("list the active interfaces", "look up a plugin") or must go away.
* ``soul`` (49 symbols) and ``vessel`` (52 symbols) are each used by only five
  units. They are Core subsystems used by their own satellites, not
  cross-cutting needs; whether they get a public API is decided per extension.

Next steps: triage the ``unmapped`` entries (none at the time of writing), and
turn the namespaces with the broadest reach (``interfaces``, ``registration``)
into concrete contract proposals.
