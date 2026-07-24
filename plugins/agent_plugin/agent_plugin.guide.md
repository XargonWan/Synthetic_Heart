# Agent

Exposes Synth's **agentic toolset** to the Agentic Runtime 2.0 — a bounded
reasoning loop that lets Synth read/write files, run shell commands, and
delegate focused sub-tasks to short-lived sub-agents (Drones). Every action is
sandboxed and gated by security levels.

The plugin is enabled by the `AGENT_ENABLED` toggle (re-read on every call);
agentic routing additionally requires `AGENTIC_ROUTING_ENABLED`.

## Actions

| Action | Purpose |
|--------|---------|
| `agent_list_files` | List files inside the sandbox. |
| `agent_read_file` | Read a text file inside the sandbox. |
| `agent_write_file` | Write/append a text file (≤ 2 MB). |
| `agent_edit_file` | Literal find-and-replace inside a sandboxed file. |
| `agent_search_files` | Read-only in-sandbox grep (substring or regex). |
| `agent_run_shell` | Run a shell command (container-only unless `AGENT_SHELL_ALLOW_HOST`). |
| `spawn_drone` | Delegate a scoped sub-task to a short-lived Drone sub-agent. |
| `resume_agent_task` | Continue a previously started agent task. |
| `note_to_self` | Persist a short note for later turns. |

All filesystem actions are confined to `AGENT_FS_ROOTS` (default `/app` and the
log dir). Drones cannot spawn Drones.

## Configuration

| Key | Purpose |
|-----|---------|
| `AGENT_ENABLED` | Master enable toggle. |
| `AGENT_CORTEX` | LLM engine used by the agent loop (falls back to `BASE_CORTEX`). |
| `AGENT_MAX_ITERATIONS` / `AGENT_TURN_TIMEOUT_SEC` | Agent loop budget. |
| `AGENT_SHELL_ALLOW_HOST` | Allow `agent_run_shell` outside a container (risky). |
| `DRONE_MAX_ITERATIONS` / `DRONE_TURN_TIMEOUT_SEC` | Drone budget. |
