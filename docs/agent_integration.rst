Agent integration
=================

Overview
--------
The Agent plugin gives SyntH a controlled, auditable hand for external tasks.
It runs inside the **Agentic Runtime** — a bounded reasoning loop that calls
*tools* (native actions and remote MCP tools) through a single safety gate. It
is intentionally LLM-agnostic and uses the active agent-scope cortex configured
for the system. For the full runtime model (tools, routing, drones), see
:doc:`agentic_tools`.

Configuration
-------------
- ``AGENT_ENABLED``: user-facing toggle to enable or disable the agent
  (default: enabled). This is the **single authoritative gate** for the router:
  when off, SyntH stays on the Fast Lane; when on, the router is active and
  escalates agentic turns to the Agent Lane.
- ``AGENT_CORTEX``: agent-scope cortex engine (falls back to ``BASE_CORTEX``).
- ``AGENT_MAX_ITERATIONS`` / ``AGENT_TURN_TIMEOUT_SEC``: bounds for the agent
  loop.
- ``DRONE_MAX_ITERATIONS`` / ``DRONE_TURN_TIMEOUT_SEC``: tighter bounds for
  ephemeral drone sub-agents.

Tools & safety
--------------
Internal actions and remote MCP tools are unified in the tool registry. Every
tool — internal or external — funnels through
``core.action_safety.is_action_allowed_for_execution`` before it runs. Internal
tools dispatch via ``run_action``; external MCP tools via
``mcp_client_bridge.call_tool``. Tool names are namespaced ``mcp_<server>_<tool>``.

The agent plugin exposes a small set of native actions:

- ``agent_list_files`` — list files under an allowed path.
- ``agent_read_file`` — read a file under an allowed path.
- ``spawn_drone`` — delegate a focused sub-task to an ephemeral drone
  (``required_fields: ["goal"]``). Drones cannot spawn drones.

Persistence
-----------
Each agentic turn and its iterations are persisted in the ``agent_tasks`` table
(see ``init-db.sql``) for auditability and WebUI inspection. Drone turns are
recorded in the same table with ``metadata.source = "drone"`` linking them to
the spawning task.

Usage
-----
For implementation and example usage, see ``plugins/agent_plugin.py`` and the
runtime entry points in ``core/agent_core.py`` (``run_agentic_turn``,
``run_drone``).

Inspecting and resuming tasks
-----------------------------
Persisted tasks can be inspected and resumed in two ways:

- **From chat** (trainer only): the ``/task`` and ``/task resume <id>`` slash
  commands (see :doc:`commands`). Resuming from chat runs the agent turn
  asynchronously and delivers the final reply back to the originating chat.
- **Over REST**: the ``/api/agent/*`` endpoints (see :doc:`api_endpoints`),
  gated by ``AGENT_ENABLED``. ``POST /api/agent/tasks/{task_id}/continue`` is
  the REST resume and runs synchronously.

Testing the feature
-------------------
To run the agent-specific unit tests locally:

.. code-block:: bash

   uv sync
   uv run pytest tests/test_agentic_runtime.py tests/test_drones.py tests/test_agent_plugin.py
