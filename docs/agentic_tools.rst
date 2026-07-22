Agentic Runtime 2.0 — Tools & MCP
==================================

Synthetic Heart (SyntH) can act as an **agent**: it can call *tools* — both its
own native actions and remote tools exposed over the **Model Context Protocol
(MCP)** — inside a bounded reasoning loop, re-injecting tool results into the
model until the goal is complete.

This document describes the runtime added in the ``feat/agentv2`` work. It is
compatible with standard coding-agent tool/MCP conventions while reusing
SyntH's existing single-message-chain architecture.

Design principles
-----------------

* **One chain, two lanes.** Every message still flows through the core message
  chain. A deterministic router decides between the **Fast Lane** (a single
  side-effect-free action, executed exactly as before) and the **Agent Lane**
  (tool calls / multi-step / external effects, executed inside a bounded loop).
* **Tools are actions.** Native SyntH actions and remote MCP tools are unified
  in a single :class:`core.tool_registry.ToolRegistry`. Every tool — internal or
  external — funnels through the same safety/audit gate
  (:func:`core.action_safety.is_action_allowed_for_execution`).
* **Dev MCP stays separate.** Synth's *own* MCP support lives exclusively in
  ``config/synth_mcp.json`` and ``core/mcp_bridge/``. The developer MCP servers
  (``.mcp.json``, ``mcp_servers/*.py``) are never touched by this runtime.

Components
----------

.. list-table::
   :header-rows: 1
   :widths: 8 34 58

   * - Phase
     - File
     - Purpose
   * - A
     - ``config/synth_mcp.json``
     - Synth-owned MCP server registry (separate from ``.mcp.json``). Top-level
       key ``synthMcpServers``.
   * - A
     - ``core/mcp_bridge/config.py``
     - Loads/validates the registry; fail-safe.
   * - B
     - ``core/tool_registry.py``
     - Unified registry of internal + MCP tools.
   * - C
     - ``core/mcp_bridge/client.py``
     - Connects to remote MCP servers, registers their tools, calls them.
   * - D
     - ``core/agent_tool_executor.py``
     - Executes a single tool call (internal via ``run_action``, external via
       the bridge).
   * - D
     - ``core/agent_core.py``
     - ``run_agentic_turn`` — bounded loop that re-injects observations.
   * - E
     - ``core/agent_router.py``
     - Deterministic Fast/Agent lane classifier.
   * - F
     - ``core/mcp_bridge/server.py``
     - Exposes selected Synth actions as an MCP server (FastMCP) for external
       clients.

The unified tool registry (Phase B)
-----------------------------------

:class:`core.tool_registry.ToolRegistry` aggregates:

* **Internal tools** — native SyntH actions loaded from the standard
  ``get_supported_actions()`` contract (via
  :meth:`ToolRegistry.load_internal_actions`).
* **MCP tools** — remote tools discovered by the client bridge, namespaced as
  ``mcp_<server>_<tool>`` via :meth:`ToolRegistry.add_mcp_tool`.

Each entry is a :class:`UnifiedToolManifest` carrying ``source``
(``internal`` or ``mcp:<server>``), ``security_level``, ``external_effects`` and
``server_name``. The manifest's :meth:`to_action_dict` renders it back into the
standard SyntH action shape so it flows through the identical dispatch path.

The MCP client bridge (Phase C)
-------------------------------

:class:`core.mcp_bridge.client.McpClientBridge` (singleton
``mcp_client_bridge``) connects to every enabled server in
``config/synth_mcp.json``. Supported transports: ``stdio``, ``sse``, ``http``,
``streamable_http``. For each server it calls ``list_tools`` and registers the
tools into the unified registry. :meth:`call_tool` resolves a namespaced tool
name, strips the ``mcp_<server>_`` prefix, and invokes the underlying
``ClientSession.call_tool``.

The agent tool executor (Phase D)
---------------------------------

:class:`core.agent_tool_executor.AgentToolExecutor` (singleton
``agent_tool_executor``) is the single execution gate for the Agent Lane:

* **Internal tool** → dispatched through ``core.action_parser.run_action``,
  inheriting validation + safety + audit unchanged.
* **External MCP tool** → invoked via ``mcp_client_bridge.call_tool`` and the
  result is normalized into a text observation.

The bounded loop
----------------

:class:`core.agent_core.AgentLoopManager.run_agentic_turn` runs the loop:

1. Builds the per-iteration prompt (goal + prior observations).
2. Asks the active engine for a response.
3. Parses tool calls out of the response.
4. Executes them through ``agent_tool_executor``.
5. Appends the results as observations and loops, until the model emits no more
   tool calls, hits ``AGENT_MAX_ITERATIONS`` (default 30), or exceeds
   ``AGENT_TURN_TIMEOUT_SEC`` (default 120).

When the turn ends (at either exit point — the deterministic
``preplanned_calls`` path or the main loop), ``run_agentic_turn`` **persists a
row into the ``agent_tasks`` table** via the source-agnostic
:meth:`core.agent_core.AgentLoopManager._persist_agentic_turn` helper. This is
the single persistence point for every entry to the agent loop, so an agentic
turn is recorded in the WebUI Agent panel **no matter which interface or API
call originated it** (Telegram, Discord, Matrix, the Ollama-compatible API, or
the WebUI ``POST /api/agent/run`` route). The row records the resolved engine,
a status derived from the ``stop_reason`` (``failed`` for ``timeout`` /
``engine_error`` / ``empty_response``, otherwise ``completed``), the per-turn
``iterations_meta``, the ``final_text`` / ``stop_reason`` output, the
originating ``trainer_id`` (from the message ``sender_id``), and a
``metadata.source`` label taken from the originating interface context. The
created task id is returned in the result dict as ``task_id`` so callers such as
the WebUI can surface it. Persistence is best-effort — a DB failure is logged
and swallowed and never breaks the turn itself. The WebUI route no longer
persists its own row; it reads ``task_id`` from the result to avoid duplicates.

Drones — ephemeral sub-agents
-----------------------------

A **Drone** is a short-lived, task-scoped sub-agent that the Agent can spawn to
handle a focused, self-contained piece of work (research, a scoped lookup, a
multi-step file inspection) without polluting the parent task. A Drone runs its
own bounded :meth:`core.agent_core.AgentLoopManager.run_agentic_turn` loop with
the full tool set and returns a concise result.

Entry point: :meth:`core.agent_core.AgentLoopManager.run_drone`. It is additive
over ``run_agentic_turn`` (no signature change to the existing loop) — it injects
a ``drone`` marker into the context, applies the tighter Drone budget, and
delegates to ``run_agentic_turn``.

**Spawning.** Drones are spawned **only** via the ``spawn_drone`` action
(handled in :mod:`plugins.agent_plugin`):

* ``required_fields``: ``["goal"]``
* ``optional_fields``: ``["engine", "max_iterations"]``
* ``security_level``: ``"medium"``

There is no direct user/interface spawn — a Drone is always requested by the
Agent from inside an agentic turn.

**Single-level delegation — Drones cannot spawn Drones.** This is enforced
twice:

1. :meth:`core.agent_core.AgentLoopManager._build_agent_prompt` omits the
   ``spawn_drone`` tool from a Drone's tool list when
   ``context["drone"]["is_drone"]`` is set, so the model never sees it.
2. The ``spawn_drone`` handler returns
   ``{"ok": False, "error": "drones_cannot_spawn_drones"}`` if it is invoked from
   within a Drone (defensive backstop).

**Engine inheritance.** When ``engine`` is omitted, a Drone resolves the same
agent-scope cortex as its parent (``get_active_cortex_engine(scope="agent")`` →
``AGENT_CORTEX`` → ``BASE_CORTEX``). An explicit ``engine`` in the payload wins.

**Budget.** Tighter than the parent Agent: ``DRONE_MAX_ITERATIONS`` (default 3)
and ``DRONE_TURN_TIMEOUT_SEC`` (default 90).

**Persistence.** Drone turns are recorded in the same ``agent_tasks`` table as
Agent turns, tagged with ``metadata.source = "drone"`` and
``metadata.drone.parent_task_id`` linking them to the spawning Agent task. No new
DB table is introduced.

Routing (Phase E)
-----------------

:func:`core.agent_router.classify` is a **pure, deterministic** function:

* Multiple actions → **Agent Lane**.
* A tool call (``mcp_*`` tool, or an internal action flagged with external
  effects) → **Agent Lane**.
* A single pure message (``message``/``tts_speak``/…) → **Fast Lane**.
* Unknown single action → **Fast Lane** (unchanged behaviour).

The router is gated by the ``AGENTIC_ROUTING_ENABLED`` config flag (default
``False``). When disabled, the message chain executes exactly as before.

Exposing Synth actions as MCP (Phase F)
---------------------------------------

:func:`core.mcp_bridge.server.build_server` builds a FastMCP server that
publishes **every** SyntH action as an MCP tool. By design there is no
whitelist and no developer opt-in: any action registered through a plugin or
interface ``get_supported_actions()`` (enumerated from
:data:`core.tool_registry.tool_registry`) automatically appears as a tool named
``synth_<action>``. Every invocation still passes through
:func:`core.action_safety.is_action_allowed_for_execution`, so the safety level
of each action and the existing policy/approval gates apply unchanged regardless
of how it is called.

The WebUI Agent panel
---------------------

Every agentic turn — regardless of the interface that originated it — is
persisted to the ``agent_tasks`` table (see *The bounded loop* above) and shown
in the **Agent panel** inside the WebUI *History* tab. Each task is rendered as a
compact card exposing its id, status, engine (or custom name), and timestamp.

Two per-card controls are available (both styled with the shared
``history-delete-btn`` class):

* **✏️ Rename** — prompts for a custom display name and stores it in the task's
  ``metadata`` JSON (key ``name``) via ``PATCH /api/agent/tasks/{task_id}``. The
  custom name replaces the engine label on the card. Submitting an empty name
  clears it. No database schema change is involved.
* **🗑 Delete** — permanently removes the task row via
  ``DELETE /api/agent/tasks/{task_id}``.

See :doc:`api_endpoints` for the full request/response shapes.

Connecting Synth as a coding agent
----------------------------------

Because Synth exposes its actions over MCP (Phase F), any MCP-capable coding
agent — **VS Code, VSCodium, Cursor, opencode, Claude Code, Windsurf**, etc. —
can drive Synth as a remote tool provider. The connection is the standard MCP
client/server handshake: the editor is the *client*, Synth is the *server*.

Step 1 — run the Synth MCP server
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``core.mcp_bridge.server`` provides the server factory; the deployment layer
decides the transport. The simplest is **stdio**, which every coding agent
supports. Create a tiny launcher (e.g. ``scripts/run_synth_mcp.py``)::

    from core.mcp_bridge.server import get_mcp_server

    if __name__ == "__main__":
        get_mcp_server("synth-actions").run(transport="stdio")

**Every** Synth action is published — there is no whitelist to configure. Each
call is still gated by
:func:`core.action_safety.is_action_allowed_for_execution`, so an action's
security level is enforced no matter how it is invoked.

Step 2 — register the server in your editor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each editor stores MCP servers in its own config file, but the shape is
identical. Point ``command``/``args`` at the launcher above (run it through
``uv`` so the project environment is used).

**VS Code / VSCodium** (``.vscode/mcp.json`` in the workspace, or the user
``mcp.json``):

.. code-block:: json

   {
     "servers": {
       "synth": {
         "type": "stdio",
         "command": "uv",
         "args": ["run", "python", "scripts/run_synth_mcp.py"]
       }
     }
   }

**Cursor** (``~/.cursor/mcp.json`` or ``.cursor/mcp.json`` in the project):

.. code-block:: json

   {
     "mcpServers": {
       "synth": {
         "command": "uv",
         "args": ["run", "python", "scripts/run_synth_mcp.py"]
       }
     }
   }

**opencode** (``opencode.json`` / ``~/.config/opencode/opencode.json``):

.. code-block:: json

   {
     "mcp": {
       "synth": {
         "type": "local",
         "command": ["uv", "run", "python", "scripts/run_synth_mcp.py"],
         "enabled": true
       }
     }
   }

**Claude Code / Claude Desktop** (``~/.claude.json`` /
``claude_desktop_config.json``) and **Windsurf**
(``~/.codeium/windsurf/mcp_config.json``) use the same ``mcpServers`` block as
Cursor.

Step 3 — use it
~~~~~~~~~~~~~~~

After restarting the editor, **all** Synth actions appear as MCP tools named
``synth_<action>`` (e.g. ``synth_tts_speak``, ``synth_message_synth_webui``,
``synth_message_telegram_bot``). The coding agent can now call them like any
other MCP tool, and every call is audited and policy-checked exactly as an
in-app action would be.

.. note::

   Remote (network) transports (``sse`` / ``streamable_http``) are also
   supported by MCP clients. If you prefer to reach a Synth container over the
   network instead of spawning it via stdio, run the FastMCP server with an HTTP
   transport in your launcher and register the resulting URL in the editor's MCP
   config (using the client's ``url`` field) instead of ``command``/``args``.

.. warning::

   Exposing Synth actions to an external coding agent grants that agent the same
   privileges as those actions. Because **every** action is published, only
   connect the Synth MCP server to a trusted client, and rely on
   :func:`core.action_safety.is_action_allowed_for_execution` (per-action
   security levels and the autonomy policy) to gate privileged or
   shell-executing actions.

Configuration reference
-----------------------

================================  ============================================
Key                               Meaning
================================  ============================================
``AGENTIC_ROUTING_ENABLED``       Enable the Fast/Agent router (default False).
``AGENT_MAX_ITERATIONS``          Hard cap on agent-loop iterations (default 30).
``AGENT_TURN_TIMEOUT_SEC``        Wall-clock budget per agent turn (default 120).
``SYNTH_MCP_CONFIG``              Override path to ``config/synth_mcp.json``.
``DRONE_MAX_ITERATIONS``          Hard cap on Drone sub-agent iterations (default 3).
``DRONE_TURN_TIMEOUT_SEC``        Wall-clock budget per Drone turn (default 90).
================================  ============================================

Testing
-------

``tests/test_agentic_runtime.py`` covers the registry, executor, bounded loop,
router classification, and MCP server exposure. Run it with::

    uv run pytest tests/test_agentic_runtime.py -q

``tests/test_drones.py`` covers Drone spawning, the no-recursion guard (handler
and prompt filter), engine override, and Drone metadata tagging. Run it with::

    uv run pytest tests/test_drones.py -q
