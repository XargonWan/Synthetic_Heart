Agent integration
=================

Overview
--------
The Agent plugin provides a controlled, auditable way for the SyntH to perform external actions (shell commands, scheduled tasks, proposals). It is intentionally LLM-agnostic and uses the active LLM configured for the system.

Configuration
-------------
- `AGENT_ENABLED`: enable or disable the agent (default: enabled only when running in a container)
- `AGENT_APPROVAL_MODE`: approval mode for executing shell commands. Options:
  - `always_approve` — execute immediately (dangerous)
  - `whitelist` — only predefined safe commands (default)
  - `always_ask` — propose and wait for trainer approval
  - `disabled` — block execution
- `AGENT_SHELL_WHITELIST`: comma-separated commands allowed under `whitelist` mode
- `AGENT_CONTAINER_REQUIRED`: if true, shell execution is disabled when not running in a container

Safety
------
The plugin enforces a conservative default: shell execution is disabled by default outside containers and whitelist is used by default. Use `always_approve` only with caution.

- Proposals created via `propose_action` or generated when an unwhitelisted command is attempted are persisted in the `agent_activity_log` table (see `init-db.sql`).
- When configured to `always_ask` (or when a whitelist block occurs), proposals are sent to the trainer's private chat using the standard `notify_trainer`/trainer notification flow. The trainer can respond directly from any chat interface (Telegram, Discord, Matrix):

  - Approve with ``/agent approve <proposal_id>`` (or the `approve_action` action with the `proposal_id`).
  - Reject with ``/agent reject <proposal_id>`` (or the `reject_action` action with the `proposal_id`). Rejecting marks the proposal ``rejected`` in `agent_activity_log` without executing it.

  Both commands require trainer privileges: all interfaces route slash commands through ``handle_command_message``, which enforces the ``is_trainer`` check before dispatch. The WebUI Agent panel exposes the same approve/reject actions via ``POST /api/agent/proposals/{id}/approve`` and ``POST /api/agent/proposals/{id}/reject``.
- Approvals, rejections and execution results are recorded in `agent_activity_log` and `agent_action_execs` for auditability and WebUI inspection.

Usage
-----
The agent accepts actions such as `agent_execute` and `propose_action`. For template/example usage, see `plugins/agent_plugin.py` implementation and `tests/test_agent_plugin.py` for PoC test cases.

Testing the feature
-------------------
To run the agent-specific unit tests locally:

1. Create and activate a virtual environment (or use the project's `venv`):

.. code-block:: bash

   python3 -m venv venv
   source venv/bin/activate

2. Install runtime and test dependencies:

.. code-block:: bash

   # install dependencies via uv
   uv sync
   pip install pytest pytest-asyncio

3. Run the agent tests only:

.. code-block:: bash

   pytest -q tests/test_agent_core.py -q

Alternatively, use the convenience script at `dev/run_agent_tests.sh` which sets up the venv and runs the agent tests.
