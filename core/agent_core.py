"""core/agent_core.py

Agent loop manager and persistence helpers.
This file provides a lightweight orchestrator used by the Agent plugin to
run multi-iteration tasks, persist them to the DB for WebUI inspection, and
expose pause/resume/cancel control.

Note: this is a conservative, test-friendly scaffold. Concrete LLM/engine
invocation and action parsing are left as TODOs and should call existing
core utilities (plugin_instance, action_parser, etc.).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from typing import Any, Coroutine, Dict, Optional

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
from core.agent_router import _is_pure_message

# Expose agent configuration variable for max iterations
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "AGENT_MAX_ITERATIONS",
        label="Agent max iterations",
        default=30,
        value_type=int,
        ui_type="number",
        description="Maximum iterations allowed for Agent loops before automatic stop.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Interim (mid-loop) outbound user-facing messages per agentic turn. The
    # final answer (attempt_completion summary / pause-composer message) is
    # NOT counted — this caps only status updates sent while the loop keeps
    # working. Default 1: the user hears one interim update, then the final
    # answer — never a re-worded status message on every iteration (observed
    # live: three near-identical Telegram updates from one turn).
    register_exposed_var(
        "AGENT_MAX_INTERIM_MESSAGES",
        label="Agent max interim messages",
        default=1,
        value_type=int,
        ui_type="number",
        description=(
            "Maximum number of mid-task status messages a single agent turn "
            "may send to the user (the final answer is not counted). Raise "
            "for long-running tasks that benefit from milestone updates."
        ),
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Persona voiceover at agent delivery: the tool-calling loop deliberately
    # runs persona-free (a ~32k persona prompt on every iteration would bloat
    # each call and degrade tool discipline), so the agent's raw final text is
    # operationally correct but tonally flat. When on, the final message is
    # re-voiced ONCE by the Base Cortex with the full persona chat context
    # before delivery; any failure falls back to the original text.
    register_exposed_var(
        "AGENT_PERSONA_DELIVERY",
        label="Agent persona delivery",
        default=True,
        value_type=bool,
        ui_type="bool",
        description=(
            "Re-voice the agent's final result through the persona chat "
            "engine (Base Cortex + full persona context) before it is "
            "delivered to the interface. Tool-calling iterations stay "
            "persona-free either way; on any restyle failure the original "
            "agent text is delivered unchanged."
        ),
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Drones are ephemeral, task-scoped sub-agents spawned by the Agent via the
    # `spawn_drone` tool. They run through the same bounded agent loop but with a
    # tighter budget and cannot spawn further Drones (single-level delegation).
    register_exposed_var(
        "DRONE_MAX_ITERATIONS",
        label="Drone max iterations",
        default=3,
        value_type=int,
        ui_type="number",
        description="Maximum iterations allowed for a Drone (sub-agent) loop before automatic stop.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "DRONE_TURN_TIMEOUT_SEC",
        label="Drone turn timeout (seconds)",
        default=90,
        value_type=int,
        ui_type="number",
        description="Wall-clock budget in seconds for a single Drone (sub-agent) turn.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Explicit-completion contract: when enabled (default), the agentic loop does
    # NOT treat a bare natural-language response (no tool calls) as "task done".
    # The model must either send a real user message or call the dedicated
    # ``attempt_completion`` tool to end the turn; otherwise the loop re-injects
    # a nudge and keeps working until the goal is finished or iterations run out.
    # This prevents premature final answers (the model announcing intent — e.g.
    # "I'll check the codebase now..." — and stopping without doing the work).
    register_exposed_var(
        "AGENT_REQUIRE_EXPLICIT_COMPLETION",
        label="Agent requires explicit completion",
        default=True,
        value_type=bool,
        ui_type="toggle",
        description=(
            "When on, an agentic turn ends only via a user message or the "
            "attempt_completion tool — plain text with no tool calls does not "
            "stop the loop, preventing premature final answers."
        ),
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Auto-resume freshness window: a parked (``pending``) task is only
    # auto-resumed when it was parked recently (default 900s = 15 min). A
    # stale parked task must never hijack a later conversational turn — the
    # user's "keep going" reply arrives within seconds/minutes of the pause
    # message, so an old pending row is abandoned work, not a continuation
    # (Langfuse 00:49 chain: a 3.5h-old task absorbed a plain chat message).
    register_exposed_var(
        "AGENT_RESUME_MAX_AGE_SEC",
        label="Agent auto-resume max age (s)",
        default=900,
        value_type=int,
        ui_type="number",
        description="Maximum age (seconds) of a parked agent task that may still be auto-resumed by the next message from the same interface. Older pending tasks are left for explicit resume only.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Scoped agent-route engine profile: the Agent Lane calls its engine with
    # thinking ENABLED and NATIVE tool calls so engines that support them
    # (e.g. Venice deepseek) return structured tool_calls instead of ad-hoc
    # text protocols (the "format whack-a-mole": Tool Call: JSON, <function>
    # XML, bare name(args), ...). Ordinary chat keeps the in-prompt JSON
    # protocol — these keys apply ONLY to the agentic loop route.
    register_exposed_var(
        "AGENT_ENABLE_THINKING",
        label="Agent route: enable thinking",
        default=True,
        value_type=bool,
        ui_type="toggle",
        description="Enable model thinking on Agent Lane engine calls (Venice disable_thinking: false). Ordinary chat keeps its configured default; only the agentic loop turns thinking on.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "AGENT_NATIVE_TOOLS",
        label="Agent route: native tool calls",
        default=True,
        value_type=bool,
        ui_type="toggle",
        description="Pass OpenAI function schemas (tools + tool_choice=auto) on Agent Lane engine calls so capable engines return structured tool_calls instead of text-protocol tool calls.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "AGENT_PARALLEL_TOOL_CALLS",
        label="Agent route: parallel tool calls",
        default=True,
        value_type=bool,
        ui_type="toggle",
        description="Allow multiple tool calls per Agent Lane response (parallel_tool_calls).",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass

# Sentinel tool the model calls to explicitly declare the task finished. It is
# not a real executable action — the loop intercepts it, extracts its summary as
# the final answer, and stops. Recognised in the prompt's AVAILABLE TOOLS block.
_COMPLETION_TOOL = "attempt_completion"

# Delivery actions whose failure the loop tries to fix *programmatically* once,
# before ever surfacing the error back to the model. Only cheap, safe repairs
# are attempted (text sanitisation, deriving a missing target from the
# already-present interface_path). The interface_path itself is NEVER modified —
# doing so could deliver the message to a chat Synth never intended.
_DELIVERY_ACTIONS = ("send_message", "message_")


def _is_delivery_action(name: str) -> bool:
    """True for the unified send_message or any legacy message_* action."""
    return name == "send_message" or name.startswith("message_")


def _context_allowed_tools(context: dict[str, Any] | None) -> set[str] | None:
    """Return a Drone's tool allow-list from context, or ``None`` if unrestricted.

    A task-scoped Drone (e.g. the vessel goal-expander/planner) is spawned via
    :meth:`AgentLoopManager.run_drone` with an explicit ``allowed_tools`` set,
    which lands under ``context["drone"]["allowed_tools"]``. When present, the
    agent loop restricts BOTH the prompt's AVAILABLE TOOLS block and the tool
    executor gate to that set (plus the always-implicit completion sentinel), so
    the Drone can never emit an out-of-scope action even if a
    broken/hallucinating cortex proposes one. Purely structural — reads the
    context marker only, no keyword/language logic. Returns ``None`` (no
    restriction) for a normal Agent turn or an unrestricted Drone.
    """
    if not isinstance(context, dict):
        return None
    drone = context.get("drone")
    if not isinstance(drone, dict):
        return None
    allowed = drone.get("allowed_tools")
    if not isinstance(allowed, (list, tuple, set)):
        return None
    names = {str(t) for t in allowed if t}
    return names or None


def _tool_allowed_by_context(tool_name: str, context: dict[str, Any] | None) -> bool:
    """True when ``tool_name`` is permitted under the context's Drone allow-list.

    The completion sentinel is always permitted (it ends the turn and is never a
    real action). When no allow-list is set the tool is unrestricted. Defence in
    depth for the executor gate — mirrors the prompt-side filter so an
    out-of-scope tool a hallucinating cortex slips past the prompt is still
    refused before execution.
    """
    allowed = _context_allowed_tools(context)
    if allowed is None:
        return True
    if tool_name == _COMPLETION_TOOL:
        return True
    return tool_name in allowed


def _programmatic_delivery_fix(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Attempt cheap, safe repairs to a failed delivery action's payload.

    Returns ``(new_args, changed)``. ``interface_path`` is treated as
    immutable — never rewritten — so a repaired retry can only ever reach the
    exact same destination the model already chose.
    """
    if not isinstance(args, dict):
        return args, False

    fixed = dict(args)
    changed = False

    # 1) Normalise / strip the outbound text. Weak models sometimes emit text
    #    wrapped in stray whitespace or with mojibake that the interface layer
    #    would reject or mangle.
    text = fixed.get("text") or fixed.get("content")
    if isinstance(text, str):
        stripped = text.strip()
        if stripped and stripped != text:
            if "text" in fixed:
                fixed["text"] = stripped
            else:
                fixed["content"] = stripped
            changed = True

    # 2) Derive a missing `target` from the interface_path the model already
    #    provided. This does NOT change the destination — it only fills in the
    #    redundant field the action validator requires, matching what the
    #    interface would compute itself.
    interface_path = fixed.get("interface_path")
    if interface_path and not fixed.get("target"):
        parts = str(interface_path).split("/")
        if len(parts) >= 2 and parts[1].strip():
            fixed["target"] = parts[1].strip()
            if len(parts) >= 3 and parts[2].strip() and not fixed.get("thread_id"):
                fixed["thread_id"] = parts[2].strip()
            changed = True

    return fixed, changed


def _looks_like_internal_monologue(text: str) -> list[str]:
    """Detect STRUCTURAL signs that internal task-log/monologue leaked into a
    user-facing message.

    Language-agnostic by design (see AGENTS.md hard rules: no keyword/phrase
    matching). It only flags machine-structural artefacts that should never
    appear in natural conversation with a user — raw action/tool JSON, tool-log
    prefixes emitted by the agent loop, and internal tool names used as call
    syntax. Returns a list of matched signal names (empty ⇒ looks clean). This
    is a NON-blocking observability aid: the caller only logs a WARNING.
    """
    if not isinstance(text, str) or not text.strip():
        return []

    signals: list[str] = []

    # 1) Raw action/tool-call JSON structure leaking through (the shape the
    #    model is supposed to emit as tool calls, not as prose).
    for marker in ('"type":', '"payload":', '"actions":', '"arguments":'):
        if marker in text:
            signals.append(f"action_json:{marker}")

    # 2) The tool-log prefix the agent loop itself writes into observations.
    if "[tool:" in text:
        signals.append("tool_log_prefix")

    # 3) An internal tool/action name written as a call (name followed by "(" or
    #    JSON-ish braces) — natural user replies don't invoke tools by name.
    try:
        from core.tool_registry import tool_registry

        for tool in tool_registry.all_tools():
            tname = tool.name
            if not tname:
                continue
            if f"{tname}(" in text or f'"{tname}"' in text:
                signals.append(f"tool_name_as_call:{tname}")
                break
    except Exception:
        # Registry not available (very early / tests): skip this signal only.
        pass

    return signals


def _match_balanced_json_object(text: str, start: int) -> int | None:
    """Return the index of the ``}`` closing the JSON object opened at
    ``text[start]`` (which must be ``{``), or ``None`` when unbalanced.

    Tracks nesting depth and skips string literals (with backslash escapes)
    so braces inside string values never confuse the matcher.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _match_balanced_call_paren(text: str, open_idx: int) -> int | None:
    """Return the index of the ``)`` closing the call paren at ``text[open_idx]``
    (which must be ``(``), or ``None`` when unbalanced.

    Tracks nesting depth and skips string literals (with backslash escapes) so
    parentheses and quotes inside argument values never confuse the matcher.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _system_only_action_names() -> frozenset[str]:
    """Action names the main prompt keeps OUT of the model-visible catalog.

    The agent loop mirrors the same exclusion so the model cannot reach for
    avatar-only speech (``tts_speak`` — voice replies must use
    ``send_as_voice`` on a ``message_*`` action) or system-internal actions
    (``static_inject``). Lazy import keeps ``prompt_engine`` detachable.
    """
    try:
        from core.prompt_engine import _SYSTEM_ONLY_ACTION_NAMES

        return _SYSTEM_ONLY_ACTION_NAMES
    except Exception:
        return frozenset({"tts_speak"})


def _repeatable_action_names() -> frozenset[str]:
    """Action names that legitimately re-run identically within one turn.

    Poll/refresh actions (e.g. ``agpeer_transfer``) declare ``"repeatable":
    true`` in their schema so the loop's identical-call dedup does not serve
    them a stale cached first result. Read fresh from the live action catalog
    (a tiny walk — the catalog is a few hundred entries) so runtime plugin
    enable/disable is picked up immediately; fail-safe to an empty set (dedup
    then behaves exactly as before).
    """
    try:
        from core.core_initializer import core_initializer

        actions = (core_initializer.actions_block or {}).get("available_actions", {})
        return frozenset(
            name
            for name, defn in (actions or {}).items()
            if isinstance(defn, dict) and defn.get("repeatable")
        )
    except Exception:
        return frozenset()


def _build_openai_tool_manifests() -> list[dict[str, Any]]:
    """OpenAI function schemas for every registered agent tool.

    Mirrors the loop's AVAILABLE TOOLS enumeration (internal + MCP tools,
    minus the ``_SYSTEM_ONLY_ACTION_NAMES`` actions) plus the
    ``attempt_completion`` sentinel, so engines that support native
    tool-calling (e.g. Venice deepseek) receive the same tool set they see in
    the prompt and return structured ``tool_calls`` instead of ad-hoc text
    protocols. The executor's safety gate and the drone/allow-list
    restrictions still apply at execution time regardless of what the engine
    sees here.
    """
    from core.tool_registry import tool_registry

    excluded = _system_only_action_names()
    manifests: list[dict[str, Any]] = []
    for tool in tool_registry.all_tools():
        if tool.name in excluded:
            continue
        action = tool.to_action_dict()
        schema = action.get("schema") or {}
        manifests.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            }
        )
    manifests.append(
        {
            "type": "function",
            "function": {
                "name": _COMPLETION_TOOL,
                "description": (
                    "Signal that the goal is genuinely accomplished (or cannot "
                    "be), ending the agent turn with a short summary."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": ("Short summary of what was accomplished."),
                        }
                    },
                    "required": ["summary"],
                },
            },
        }
    )
    return manifests


async def _call_with_hard_timeout(
    coro: Coroutine[Any, Any, str], timeout: float
) -> str:
    """Await ``coro`` with a hard deadline WITHOUT waiting for its cancellation.

    ``asyncio.wait_for`` (with or without ``asyncio.shield``) cancels the inner
    task and then *awaits* it to finish cancelling — when the engine call is
    stuck in non-cancellable code (e.g. a wedged socket read inside an engine
    adapter), ``wait_for`` never returns and the whole agentic/Drone loop hangs
    silently (observed live: goal-expansion Drones went silent mid-loop, leaving
    their ``agent_tasks`` rows ``pending`` forever and goals stepless — 30 stuck
    rows in one day). ``asyncio.wait`` never cancels anything: on expiry the
    task is still in the pending set, we cancel it best-effort WITHOUT awaiting
    it (the stuck call drains in the background, one bounded orphan per
    genuinely hung call) and raise ``TimeoutError`` immediately — the caller
    always proceeds. Callers treat ``TimeoutError`` exactly like the previous
    ``wait_for`` timeout path.
    """
    task = asyncio.create_task(coro)
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()
    task.cancel()
    raise asyncio.TimeoutError()


# --------------------------------------------------------------------------
# Engine-failure classification
#
# The agent loop must report WHY an engine call produced nothing (endpoint
# offline / rejected credentials / empty body / timeout) instead of guessing a
# single cause. Markers are matched against transport/provider ERROR strings
# only (never message content), mirroring the bridge's own
# ``_is_retryable_exception`` — this is diagnostics, not product routing.
# --------------------------------------------------------------------------

_ENGINE_FAILURE_AUTH_MARKERS = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "incorrect api key",
    "api key",
    "authentication",
    "error code: 401",
    "error code: 403",
)
_ENGINE_FAILURE_CONNECTION_MARKERS = (
    "connection",
    "refused",
    "reset by peer",
    "unreachable",
    "dns",
    "getaddrinfo",
    "name or service not known",
    "no route to host",
    "network",
    "ssl",
)
_ENGINE_FAILURE_BAD_REQUEST_MARKERS = (
    "error code: 400",
    "invalid request",
    "tool definitions",
    "context length",
    "model not found",
    "does not exist",
)
_ENGINE_FAILURE_RATE_LIMIT_MARKERS = (
    "error code: 429",
    "rate limit",
    "too many requests",
    "quota",
    "resource exhausted",
    "overloaded",
)
_ENGINE_FAILURE_SERVER_MARKERS = (
    "error code: 5",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "server error",
)
# Our own engine/adapter code raising while handling the call or its response
# (e.g. ``TypeError: 'NoneType' object is not subscriptable`` on a non-standard
# HTTP-200 body). The bridge records these verbatim as
# ``f"{type(exc).__name__}: {exc}"``, so the Python type name leads the string;
# match by prefix so provider text merely *mentioning* a type name still wins.
_ENGINE_FAILURE_INTERNAL_MARKERS = (
    "TypeError:",
    "AttributeError:",
    "KeyError:",
    "IndexError:",
    "NameError:",
    "UnboundLocalError:",
    "JSONDecodeError:",
)

# Failure kinds that cannot self-heal within a turn: retrying the same engine
# (or burning the turn budget iterating) is pure waste, so the loop fails fast
# with ``engine_error`` once the base-cortex safety net is exhausted.
_STRUCTURAL_ENGINE_FAILURES = frozenset({"connection", "auth", "bad_request"})

_ENGINE_FAILURE_HINTS = {
    "auth": (
        "the endpoint rejected the credentials — check that engine's API key "
        "in the Engines settings"
    ),
    "connection": (
        "the endpoint is unreachable — it looks offline, or its URL/port is wrong"
    ),
    "bad_request": (
        "the endpoint rejected the request itself — e.g. an unsupported model, "
        "a request-size/tool limit, or a malformed parameter"
    ),
    "rate_limited": (
        "the endpoint is rate-limiting or out of quota — retry later or raise "
        "its limits"
    ),
    "server_error": (
        "the endpoint reported a server-side error — it may be overloaded or broken"
    ),
    "timeout": (
        "no response within the time budget — the endpoint may be offline, "
        "overloaded, or too slow for this agent turn"
    ),
    "empty": (
        "the endpoint answered but returned an empty body — often a broken "
        "engine config (e.g. an invalid API key or model name)"
    ),
    "internal": (
        "our engine code hit an unexpected Python exception handling this "
        "endpoint's response — often a non-standard reply shape; check the "
        "logs and report it if reproducible"
    ),
    "unknown": (
        "the engine failed without a specific error — check the endpoint "
        "config and logs"
    ),
}


def classify_engine_failure(
    error_text: str | None = None,
    *,
    timed_out: bool = False,
    empty_body: bool = False,
) -> tuple[str, str]:
    """Classify an engine/cortex failure into ``(kind, operator_hint)``.

    ``kind`` is one of ``auth``, ``connection``, ``bad_request``,
    ``rate_limited``, ``server_error``, ``timeout``, ``empty``, ``internal``
    (our own code crashed handling the call/response), ``unknown``.
    A recorded provider error wins over the timeout/empty flags: an offline
    endpoint typically surfaces to the loop as a hard timeout (the bridge's
    connection retries outlive the turn budget) while the bridge has already
    recorded the underlying ``Connection error`` — the truthful kind is then
    ``connection``, not ``timeout``.
    """
    if error_text:
        lowered = error_text.lower()
        for marker in _ENGINE_FAILURE_AUTH_MARKERS:
            if marker in lowered:
                return "auth", _ENGINE_FAILURE_HINTS["auth"]
        for marker in _ENGINE_FAILURE_CONNECTION_MARKERS:
            if marker in lowered:
                return "connection", _ENGINE_FAILURE_HINTS["connection"]
        for marker in _ENGINE_FAILURE_BAD_REQUEST_MARKERS:
            if marker in lowered:
                return "bad_request", _ENGINE_FAILURE_HINTS["bad_request"]
        for marker in _ENGINE_FAILURE_RATE_LIMIT_MARKERS:
            if marker in lowered:
                return "rate_limited", _ENGINE_FAILURE_HINTS["rate_limited"]
        for marker in _ENGINE_FAILURE_SERVER_MARKERS:
            if marker in lowered:
                return "server_error", _ENGINE_FAILURE_HINTS["server_error"]
        # Our own code crashing (recorded as "TypeName: message") — never
        # misread as a provider-side problem.
        for marker in _ENGINE_FAILURE_INTERNAL_MARKERS:
            if lowered.startswith(marker.lower()):
                return "internal", _ENGINE_FAILURE_HINTS["internal"]
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout", _ENGINE_FAILURE_HINTS["timeout"]
    if timed_out:
        return "timeout", _ENGINE_FAILURE_HINTS["timeout"]
    if empty_body:
        return "empty", _ENGINE_FAILURE_HINTS["empty"]
    return "unknown", _ENGINE_FAILURE_HINTS["unknown"]


def _peek_engine_diagnostics(engine_name: str | None) -> dict[str, Any] | None:
    """Fail-safe read of a cortex engine's last-call diagnostics.

    External-endpoint bridges expose ``_last_attempt_error`` (last transport/
    provider error inside ``generate_response``) and ``_last_response_metadata``
    (with ``empty_response`` for 200-but-empty bodies). Reading them after a
    failed/cancelled call recovers the real cause — e.g. a hard agent timeout
    that was actually the endpoint being offline. Purely diagnostic: any error
    (unknown engine, non-bridge engine, registry failure) returns ``None``.
    """
    if not engine_name:
        return None
    try:
        from core.cortex_registry import get_cortex_registry

        engine_obj = get_cortex_registry().get_engine(engine_name)
        if engine_obj is None:
            return None
        error_text = getattr(engine_obj, "_last_attempt_error", None)
        metadata = getattr(engine_obj, "_last_response_metadata", None)
        return {
            "error": str(error_text) if error_text else None,
            "empty_body": bool(
                isinstance(metadata, dict) and metadata.get("empty_response")
            ),
        }
    except Exception:
        return None


def _engine_failure_from_diagnostics(engine_name: str | None) -> dict[str, Any] | None:
    """Build a classified failure record from an engine's bridge diagnostics.

    Returns ``None`` when the engine carries no failure diagnostics (plain
    plugin engines, or a call that genuinely never reached the bridge).
    """
    diag = _peek_engine_diagnostics(engine_name)
    if not diag:
        return None
    if diag.get("error"):
        kind, hint = classify_engine_failure(diag["error"])
        return {
            "kind": kind,
            "hint": hint,
            "detail": diag["error"],
            "engine": engine_name,
        }
    if diag.get("empty_body"):
        kind, hint = classify_engine_failure(empty_body=True)
        return {
            "kind": kind,
            "hint": hint,
            "detail": "endpoint returned an empty body (HTTP 200, no content)",
            "engine": engine_name,
        }
    return None


def _describe_engine_failure(failure: dict[str, Any] | None, engine: str | None) -> str:
    """Render a classified failure for logs/notifications, truthfully.

    When no diagnostics exist (e.g. the generic plugin path) the description
    stays at the honest symptom ("empty response") instead of guessing a cause.
    """
    engine_label = engine or "active cortex"
    if not failure:
        return (
            f"empty response from '{engine_label}' — no error detail was "
            f"recorded by the engine"
        )
    kind = str(failure.get("kind") or "unknown")
    detail = str(failure.get("detail") or "").strip()
    hint = str(failure.get("hint") or "").strip()
    parts = [f"{kind} from '{failure.get('engine') or engine_label}'"]
    if detail:
        parts.append(detail)
    if hint:
        parts.append(hint)
    return "; ".join(parts)


# DB helper is imported lazily inside methods for testability/mocking


class AgentLoopManager:
    """Manage bounded agentic turns (Agentic Runtime 2.0).

    Orchestrates ``run_agentic_turn`` and ``run_drone``: assembling the tool
    manifest, running the bounded reasoning loop, and persisting each turn to
    the ``agent_tasks`` table via ``_persist_agentic_turn``.
    """

    def __init__(self) -> None:
        pass

    # --- DB helpers ---
    async def _maybe_commit(self, conn) -> None:
        """Safely call commit on connection (if available) and await if coroutine."""
        try:
            commit_fn = getattr(conn, "commit", None)
            if commit_fn and callable(commit_fn):
                res = commit_fn()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            # Best-effort; ignore commit failures
            pass

    async def _get_conn_ctx(self) -> Any:
        try:
            import core as core_package

            db_module = getattr(core_package, "db", None)
        except Exception:
            db_module = None

        if db_module is None:
            db_module = sys.modules.get("core.db")
        if db_module is None:
            db_module = importlib.import_module("core.db")

        get_conn_ctx = getattr(db_module, "get_conn_ctx")
        conn_ctx = get_conn_ctx()
        if asyncio.iscoroutine(conn_ctx):
            conn_ctx = await conn_ctx
        return conn_ctx

    async def _begin_agentic_turn(
        self,
        *,
        engine: str | None,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        original_message: Any = None,
        preplanned_calls: Optional[list[Dict[str, Any]]] = None,
    ) -> Optional[int]:
        """Insert a ``running`` row for an in-flight agentic turn.

        Called BEFORE the reasoning loop starts so the turn is durable from the
        moment it begins. This is what makes a detached turn survivable across a
        container restart: an interrupted turn is left as a ``running`` row in
        ``agent_tasks`` that the startup recovery sweep can detect and mark as
        interrupted (rather than the turn silently vanishing with the process).

        Returns the new row id, or ``None`` on any DB failure (best-effort: a
        persistence problem must never prevent the turn from running).
        """
        try:
            trainer_id: str | None = None
            if isinstance(original_message, dict):
                raw_id = original_message.get("sender_id") or original_message.get(
                    "user_id"
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)
            elif original_message is not None:
                raw_id = getattr(original_message, "sender_id", None) or getattr(
                    original_message, "user_id", None
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)

            source = "agentic_turn"
            interface_path = None
            if isinstance(context, dict):
                source = str(
                    context.get("interface_name") or context.get("interface") or source
                )
                interface_path = context.get("interface_path")

            task_name: str | None = None
            if isinstance(context, dict):
                raw_title = context.get("agent_task_title")
                if isinstance(raw_title, str) and raw_title.strip():
                    task_name = raw_title.strip()[:120]
            if not task_name and isinstance(goal, str) and goal.strip():
                task_name = goal.strip()[:120]

            input_payload = {
                "goal": goal,
                "planned_actions": preplanned_calls
                if isinstance(preplanned_calls, list)
                else None,
            }
            metadata = {
                "source": source,
                "interface_path": interface_path,
                "has_preplanned_calls": bool(isinstance(preplanned_calls, list)),
                "name": task_name,
            }
            if isinstance(context, dict) and isinstance(context.get("drone"), dict):
                drone_meta = context["drone"]
                if drone_meta.get("is_drone"):
                    metadata["source"] = "drone"
                    metadata["drone"] = {
                        "parent_task_id": drone_meta.get("parent_task_id"),
                    }

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    params = (
                        str(engine or "default"),
                        "running",
                        json.dumps(input_payload),
                        trainer_id,
                        json.dumps(metadata),
                    )
                    new_id: Optional[int] = None
                    try:
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            params,
                        )
                        row = await cur.fetchone()
                        if row is not None:
                            new_id = int(row[0])
                    except Exception:
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            params,
                        )
                        last = getattr(cur, "lastrowid", None)
                        new_id = int(last) if last else None
                    await self._maybe_commit(conn)
                    return new_id
        except Exception as e:
            log_warning(f"[agent_core] _begin_agentic_turn failed: {e}")
            return None

    async def find_resumable_task_for_interface(
        self, interface_path: str | None
    ) -> Optional[Dict[str, Any]]:
        """Find a paused (``pending``) agentic task for the given interface.

        A single interface (a Telegram chat, a Discord channel, a WebUI
        session) must never own two parallel pending agentic tasks. When a
        turn exhausts its budget without an explicit completion it is parked as
        ``pending``; the very next message from that same ``interface_path``
        should RESUME that task, not spawn a brand-new one. This is what lets a
        user reply "yes, keep going" (in any language) in chat and have Synth
        continue the same task — without any keyword/language detection, purely
        by matching the originating interface.

        Returns a dict
        ``{"task_id", "goal", "engine", "prior_observations", "updated_at"}``
        for the most recent pending task on that interface, or ``None`` when
        there is none (or on any DB error — best-effort, never blocks a turn).
        """
        if not interface_path or not isinstance(interface_path, str):
            return None
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, input, iterations_meta, updated_at "
                        "FROM agent_tasks "
                        "WHERE status='pending' "
                        "AND metadata::json->>'interface_path' = %s "
                        "ORDER BY id DESC LIMIT 1",
                        (interface_path,),
                    )
                    row = await cur.fetchone()
            if not row:
                return None
            task_id = int(row[0])
            engine = row[1]
            input_raw = row[2]
            iterations_raw = row[3]
            updated_at = row[4]

            input_payload = json.loads(input_raw) if input_raw else {}
            if not isinstance(input_payload, dict):
                input_payload = {}
            goal = str(input_payload.get("goal") or "").strip()
            if not goal:
                return None

            prior_observations: list[Dict[str, Any]] = []
            iterations_meta = json.loads(iterations_raw) if iterations_raw else []
            if isinstance(iterations_meta, list):
                for entry in iterations_meta:
                    if not isinstance(entry, dict):
                        continue
                    prior_observations.append(
                        {
                            "iteration": entry.get("iteration"),
                            "role": entry.get("role") or "observation",
                            "content": entry.get("result"),
                        }
                    )
            return {
                "task_id": task_id,
                "goal": goal,
                "engine": engine if engine and engine != "default" else None,
                "prior_observations": prior_observations,
                "updated_at": updated_at,
            }
        except Exception as e:
            log_warning(f"[agent_core] find_resumable_task_for_interface failed: {e}")
            return None

    async def find_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Load a specific paused (``pending``) agentic task by its id.

        Unlike :meth:`find_resumable_task_for_interface` this does NOT match on
        the originating interface: the user may refer to a task created on a
        different interface (e.g. a Grillo-originated task referenced from a
        Telegram chat). The task id is chosen by the model — this method only
        loads and validates it, it never parses user text.

        Returns a dict ``{"task_id", "goal", "engine", "prior_observations"}``
        when the task exists and is ``pending``; otherwise ``None`` (unknown id,
        wrong status, or any DB error — best-effort, never blocks a turn).
        """
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return None
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, input, iterations_meta, status "
                        "FROM agent_tasks WHERE id=%s LIMIT 1",
                        (tid,),
                    )
                    row = await cur.fetchone()
            if not row:
                return None
            status = row[4]
            if status != "pending":
                log_info(
                    f"[agent_core] find_task_by_id: task {tid} is '{status}', "
                    "not resumable"
                )
                return None
            engine = row[1]
            input_raw = row[2]
            iterations_raw = row[3]

            input_payload = json.loads(input_raw) if input_raw else {}
            if not isinstance(input_payload, dict):
                input_payload = {}
            goal = str(input_payload.get("goal") or "").strip()
            if not goal:
                return None

            prior_observations: list[Dict[str, Any]] = []
            iterations_meta = json.loads(iterations_raw) if iterations_raw else []
            if isinstance(iterations_meta, list):
                for entry in iterations_meta:
                    if not isinstance(entry, dict):
                        continue
                    prior_observations.append(
                        {
                            "iteration": entry.get("iteration"),
                            "role": entry.get("role") or "observation",
                            "content": entry.get("result"),
                        }
                    )
            return {
                "task_id": int(row[0]),
                "goal": goal,
                "engine": engine if engine and engine != "default" else None,
                "prior_observations": prior_observations,
            }
        except Exception as e:
            log_warning(f"[agent_core] find_task_by_id failed: {e}")
            return None

    @staticmethod
    def _clean_goal_text(goal: str) -> str:
        """Return ``goal`` unless it is a raw agent-action JSON blob.

        Some tasks store the LLM's raw ``{"actions": [...]}`` payload as their
        goal, which is meaningless to a human reading ``/task``. Detect that
        structurally (parse + ``actions`` key) and drop it so a better display
        text (task name / final_text) can take over. Returns ``""`` when the
        goal is such a blob.
        """
        text = (goal or "").strip()
        if not text:
            return ""
        if text[0] in "{[":
            try:
                parsed = json.loads(text)
            except Exception:
                return text
            if isinstance(parsed, dict) and "actions" in parsed:
                return ""
            if isinstance(parsed, list):
                return ""
        return text

    async def list_recent_tasks(self, limit: int = 15) -> list[Dict[str, Any]]:
        """Return the most recent agent tasks for display (newest first).

        Each entry is ``{"task_id", "status", "engine", "resumable", "name",
        "summary", "stop_reason", "actions_executed", "iterations"}``. ``name``
        is the best human-readable label (task name → cleaned goal →
        final_text). ``resumable`` mirrors :meth:`find_task_by_id` semantics —
        only ``pending`` tasks can be resumed. Best-effort: returns ``[]`` on
        any DB error so a display command never raises.
        """
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 15
        if lim <= 0:
            lim = 15
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, status, engine, input, metadata, output "
                        "FROM agent_tasks ORDER BY id DESC LIMIT %s",
                        (lim,),
                    )
                    rows = await cur.fetchall()
        except Exception as e:
            log_warning(f"[agent_core] list_recent_tasks failed: {e}")
            return []

        tasks: list[Dict[str, Any]] = []
        for row in rows or []:
            try:
                input_payload = json.loads(row[3]) if row[3] else {}
            except Exception:
                input_payload = {}
            try:
                metadata = json.loads(row[4]) if row[4] else {}
            except Exception:
                metadata = {}
            try:
                output_payload = json.loads(row[5]) if row[5] else {}
            except Exception:
                output_payload = {}
            if not isinstance(input_payload, dict):
                input_payload = {}
            if not isinstance(metadata, dict):
                metadata = {}
            if not isinstance(output_payload, dict):
                output_payload = {}

            goal = self._clean_goal_text(str(input_payload.get("goal") or ""))
            task_name = str(metadata.get("name") or "").strip()
            final_text = str(output_payload.get("final_text") or "").strip()
            stop_reason = str(output_payload.get("stop_reason") or "").strip()

            # Prefer an explicit task name, then a real (non-JSON) goal, then
            # the final reply text so the label is always meaningful.
            name = task_name or goal or final_text

            status = row[1]
            tasks.append(
                {
                    "task_id": int(row[0]),
                    "status": status,
                    "engine": row[2],
                    "resumable": status == "pending",
                    "name": name,
                    "summary": final_text,
                    "stop_reason": stop_reason,
                    "actions_executed": output_payload.get("actions_executed"),
                    "iterations": output_payload.get("iterations"),
                }
            )
        return tasks

    async def _mark_task_running(self, task_id: int) -> None:
        """Flip an existing ``agent_tasks`` row back to ``running``.

        Used when resuming a paused task so the UI/state reflects the in-flight
        resume immediately. Best-effort: a failure here must never abort the
        turn (the finalising persist will set the real terminal status anyway).
        """
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status='running', "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                        (int(task_id),),
                    )
                await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _mark_task_running failed: {e}")

    async def supersede_pending_task(self, task_id: int) -> None:
        """Best-effort cancel of a parked task that can never be resumed.

        Used when the auto-resume gate refuses a task whose stored goal is a
        self-referential LLM artifact (pre-fix data): leaving it ``pending``
        would let it keep hijacking future turns from the same interface.
        Best-effort — a failure never blocks the turn.
        """
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status='cancelled', "
                        "updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=%s AND status='pending'",
                        (int(task_id),),
                    )
                await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] supersede_pending_task failed: {e}")

    async def _persist_agentic_turn(
        self,
        *,
        engine: str | None,
        goal: str,
        result: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        original_message: Any = None,
        preplanned_calls: Optional[list[Dict[str, Any]]] = None,
        task_id: Optional[int] = None,
    ) -> Optional[int]:
        """Persist a completed agentic turn into ``agent_tasks``.

        This is the single, source-agnostic persistence point for
        :meth:`run_agentic_turn`. Every caller — the message-chain Agent Lane
        (Telegram / Discord / API), the WebUI ``/api/agent/run`` route, or any
        future entry point — produces a row visible in the WebUI Agent panel,
        regardless of which interface originated the turn.

        Best-effort: DB failures are logged and swallowed so an audit-write
        problem never breaks the agent turn itself.
        """
        try:
            observations = result.get("observations") or []
            iterations_meta: list[Dict[str, Any]] = []
            if isinstance(observations, list):
                for idx, obs in enumerate(observations, start=1):
                    if not isinstance(obs, dict):
                        continue
                    iterations_meta.append(
                        {
                            "iteration": obs.get("iteration") or idx,
                            "role": obs.get("role") or "observation",
                            "result": obs.get("content"),
                        }
                    )

            stop_reason = str(result.get("stop_reason") or "")
            if stop_reason in {
                "timeout",
                "engine_error",
                "empty_response",
                "malformed_response",
                "delivery_failed",
            }:
                status = "failed"
            elif stop_reason in ("paused_max_iterations", "paused_timeout"):
                # Iteration or TIME budget exhausted without an explicit
                # completion: the goal is not finished. Park the task as
                # ``pending`` so the user can grant more iterations via
                # "Continue" (WebUI) or a chat reply instead of it being
                # falsely reported as ``completed``.
                status = "pending"
            else:
                status = "completed"

            # Count the tool actions actually executed across the turn so the
            # proactive "I've done X actions, continue?" message and the WebUI
            # can show a concrete number.
            actions_executed = 0
            if isinstance(observations, list):
                for obs in observations:
                    if isinstance(obs, dict) and obs.get("role") == "tool_results":
                        content = obs.get("content")
                        if isinstance(content, list):
                            actions_executed += len(content)

            # Derive a trainer/originator id and a source label from the
            # originating context/message so the WebUI can attribute the task.
            trainer_id: str | None = None
            if isinstance(original_message, dict):
                raw_id = original_message.get("sender_id") or original_message.get(
                    "user_id"
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)
            elif original_message is not None:
                raw_id = getattr(original_message, "sender_id", None) or getattr(
                    original_message, "user_id", None
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)

            source = "agentic_turn"
            interface_path = None
            if isinstance(context, dict):
                source = str(
                    context.get("interface_name") or context.get("interface") or source
                )
                interface_path = context.get("interface_path")

            input_payload = {
                "goal": goal,
                "planned_actions": preplanned_calls
                if isinstance(preplanned_calls, list)
                else None,
            }
            output_payload = {
                "iterations": int(result.get("iterations") or len(iterations_meta)),
                "final_text": result.get("final_text") or "",
                "stop_reason": stop_reason,
                "actions_executed": actions_executed,
                "paused": status == "pending",
            }
            # Task name shown in the WebUI Agents panel. Prefer the recon-derived
            # title (set on the shared context by the agent-intent recon hook);
            # fall back to a truncated goal so the task is never nameless.
            task_name: str | None = None
            if isinstance(context, dict):
                raw_title = context.get("agent_task_title")
                if isinstance(raw_title, str) and raw_title.strip():
                    task_name = raw_title.strip()[:120]
            if not task_name and isinstance(goal, str) and goal.strip():
                task_name = goal.strip()[:120]

            metadata = {
                "source": source,
                "interface_path": interface_path,
                "has_preplanned_calls": bool(isinstance(preplanned_calls, list)),
                "name": task_name,
            }
            # Tag Drone (sub-agent) turns so the WebUI/audit can distinguish them
            # from top-level Agent turns and link them to their parent task.
            if isinstance(context, dict) and isinstance(context.get("drone"), dict):
                drone_meta = context["drone"]
                if drone_meta.get("is_drone"):
                    metadata["source"] = "drone"
                    metadata["drone"] = {
                        "parent_task_id": drone_meta.get("parent_task_id"),
                    }

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    # When ``task_id`` is provided, a ``running`` row already
                    # exists (opened by ``_begin_agentic_turn`` so the turn is
                    # durable from the start). Finalise it in place with the
                    # loop results instead of inserting a duplicate row.
                    if task_id is not None:
                        await cur.execute(
                            """
                            UPDATE agent_tasks
                            SET engine = %s,
                                status = %s,
                                iterations_meta = %s,
                                output = %s,
                                trainer_id = %s,
                                metadata = %s
                            WHERE id = %s
                            """,
                            (
                                str(engine or "default"),
                                status,
                                json.dumps(iterations_meta),
                                json.dumps(output_payload),
                                trainer_id,
                                json.dumps(metadata),
                                int(task_id),
                            ),
                        )
                        await self._maybe_commit(conn)
                        return int(task_id)

                    params = (
                        str(engine or "default"),
                        status,
                        json.dumps(input_payload),
                        json.dumps(iterations_meta),
                        json.dumps(output_payload),
                        trainer_id,
                        json.dumps(metadata),
                    )
                    new_id: Optional[int] = None
                    try:
                        # Postgres path: RETURNING id yields the new row id.
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            params,
                        )
                        row = await cur.fetchone()
                        if row is not None:
                            new_id = int(row[0])
                    except Exception:
                        # MariaDB / drivers without RETURNING support.
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            params,
                        )
                        last = getattr(cur, "lastrowid", None)
                        new_id = int(last) if last else None
                    await self._maybe_commit(conn)
                    return new_id
        except Exception as e:
            log_warning(f"[agent_core] _persist_agentic_turn failed: {e}")
            return None

    async def run_agentic_turn(
        self,
        *,
        goal: str,
        engine: str | None = None,
        context: Dict[str, Any] | None = None,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        original_message: Any = None,
        preplanned_calls: list[Dict[str, Any]] | None = None,
        task_id: int | None = None,
        prior_observations: list[Dict[str, Any]] | None = None,
        cortex_scope: str = "agent",
        preplanned_then_loop: bool = False,
    ) -> Dict[str, Any]:
        """Run a bounded agentic turn that re-injects tool results into the model.

        Each iteration:

        1. Asks the active engine for a response, including the accumulated
           observation history from previous tool calls.
        2. Parses any tool calls out of the response (via the standard
           message-chain normalization).
        3. Executes them through :class:`core.agent_tool_executor.AgentToolExecutor`
           — which funnels internal actions AND external MCP tools through the
           same safety/audit gate.
        4. Appends the tool results as observations and loops, until the model
           emits no more tool calls, hits ``max_iterations``, or ``timeout``.

        Args:
            goal: The user/agent objective for this turn.
            engine: Optional cortex engine name (defaults to active cortex).
            context: Optional extra context dict forwarded to the engine.
            max_iterations: Hard cap on iterations (defaults to AGENT_MAX_ITERATIONS).
            timeout_seconds: Optional wall-clock budget for the whole turn.
            original_message: Optional originating message (for audit/safety).
            task_id: Optional existing ``agent_tasks`` row id to resume/finalise
                in place (used by "Continue" to re-run a paused task on the same
                record instead of creating a new one).
            prior_observations: Optional observation history from a previous
                (paused) turn, re-injected so the model continues with the
                context it already built instead of starting from scratch.
            preplanned_then_loop: when True (used by the Agent Lane
                under-emission seeding), execute ``preplanned_calls`` first
                and then CONTINUE into the model loop instead of returning
                after the plan (default False keeps the current terminal-plan
                behaviour for WebUI/vessel callers).

        Returns:
            A dict with ``iterations``, ``observations``, ``final_text`` and
            ``stop_reason``.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 30))
        if timeout_seconds is None:
            timeout_seconds = float(
                config_registry.get_var("AGENT_TURN_TIMEOUT_SEC", 3600)
            )

        # Resolve the Cortex engine for the agentic loop. When the caller did
        # not pin an explicit engine, honour the scope override (default
        # "agent"): "Default" reuses the active Base Cortex, otherwise the turn
        # runs on a dedicated LLM better suited for tool-calling work. Callers
        # may pin another scope (e.g. "vessel" for the goal-expansion Drone) so
        # its engine matches VESSEL_CORTEX rather than the generic AGENT_CORTEX.
        if not engine:
            try:
                from core.config import get_active_cortex_engine

                engine = await get_active_cortex_engine(scope=cortex_scope)
                log_debug(
                    f"[agent_core] Agentic loop engine resolved "
                    f"(scope={cortex_scope}): {engine}"
                )
            except Exception as exc:
                log_warning(
                    f"[agent_core] Could not resolve agent-scope engine, "
                    f"falling back to active cortex: {exc}"
                )
                engine = None

        from core.agent_tool_executor import agent_tool_executor
        from core.transport_layer import extract_json_from_text

        # Derive the interface name from the originating interface_path when the
        # caller did not already supply it. The Agent Lane router only sets
        # ``interface_path`` on the context, but internal actions run by the tool
        # executor read ``context["interface"]`` (e.g. create_personal_diary_entry
        # would otherwise persist the entry as interface="unknown"). Enriching the
        # shared context once here means both the prompt and every executed tool
        # see the correct interface. Best-effort and purely structural.
        if isinstance(context, dict) and not context.get("interface"):
            src_path = context.get("interface_path")
            if isinstance(src_path, str) and src_path:
                try:
                    from core.interface_path_utils import get_interface_from_path

                    derived_interface = get_interface_from_path(src_path)
                    if derived_interface:
                        context["interface"] = derived_interface
                except Exception as exc:
                    log_debug(
                        f"[agent_core] Could not derive interface from "
                        f"interface_path {src_path!r}: {exc}"
                    )

        # Diagnostic: the agent prompt surfaces materialized attachment paths
        # (the USER ATTACHMENTS block) so the model can read uploaded files
        # directly. Log once per turn whether the context carried them, so a
        # missing block can be attributed without a full trace dump (Langfuse
        # 600052d5 showed the block absent even though plugin_instance
        # persisted the file at the same timestamp).
        _att_paths = (
            context.get("attachment_paths") if isinstance(context, dict) else None
        )
        log_info(
            f"[agent_core] Agent turn context: attachment_paths present="
            f"{bool(isinstance(_att_paths, list) and _att_paths)}; "
            f"context_keys={sorted((context or {}).keys())}"
        )

        # Open a durable ``running`` row BEFORE the loop starts. A detached turn
        # (message-chain Agent Lane) can be interrupted by a container restart
        # mid-flight; persisting up-front means the startup recovery sweep can
        # detect the orphaned ``running`` row and surface it instead of the turn
        # vanishing silently. Best-effort: a None id just means the finalising
        # persist will INSERT a fresh row as before.
        #
        # When ``task_id`` is supplied (resume/"Continue" of a paused task) we
        # reuse that existing row instead of opening a new one, so the whole
        # multi-batch effort stays a single ``agent_tasks`` record.
        if task_id is None:
            task_id = await self._begin_agentic_turn(
                engine=engine,
                goal=goal,
                context=context,
                original_message=original_message,
                preplanned_calls=preplanned_calls,
            )
        else:
            # Resuming an existing (paused) row: flip it back to ``running`` so
            # the UI reflects the in-flight resume instead of showing it as
            # ``pending`` for the whole batch. Best-effort — never block the
            # turn on a status update failure.
            await self._mark_task_running(task_id)

        observations: list[Dict[str, Any]] = list(prior_observations or [])
        final_text = ""
        stop_reason = "max_iterations"

        # Delivery-integrity tracking (see completion-integrity gate below).
        # ``delivered_message_ok`` becomes True as soon as any outbound message_*
        # action is genuinely delivered; ``delivery_failures`` collects the names
        # of outbound messages that failed so the turn never falsely claims to
        # have replied when nothing reached the interface.
        delivered_message_ok = False
        delivery_failures: list[str] = []

        # Cross-iteration delivery dedup: a weak loop engine re-emits the SAME
        # outbound text (or the same voice note via send_as_voice) on later
        # iterations instead of calling attempt_completion, which delivered the
        # reply multiple times (Langfuse 00:48-00:50 chain: the PDF voice note
        # was sent 3x). Any text already delivered once is not re-sent; the
        # observation records "already delivered" so the model sees it went out.
        delivered_texts: set[str] = set()

        # Interim-message damper: even NON-identical re-worded status messages
        # must not spam the user on every iteration (observed live: one turn
        # sent three near-identical Telegram updates because each iteration
        # re-narrated the same status slightly differently — exact-text dedup
        # can not catch that). At most ``AGENT_MAX_INTERIM_MESSAGES``
        # successful mid-loop user-facing deliveries per turn; anything beyond
        # is suppressed with an observation steering the model to finish and
        # use attempt_completion for the final answer. Failed deliveries do
        # not count against the cap (a retry must remain possible).
        interim_messages_delivered = 0
        try:
            max_interim_messages = max(
                0,
                min(int(config_registry.get_var("AGENT_MAX_INTERIM_MESSAGES", 1)), 10),
            )
        except (TypeError, ValueError):
            max_interim_messages = 1

        # Cross-iteration tool-call dedup: a weak loop engine re-issues the SAME
        # tool call on later iterations (observed live: the same
        # ``agent_read_file`` call repeated 7x because the engine thought the
        # file content was truncated). An identical (name, args) call in the
        # same turn is never re-executed — the first result is returned with a
        # structural note so the model sees it already has the answer.
        executed_calls: dict[tuple[str, str], Dict[str, Any]] = {}

        # Two consecutive message-only iterations (no tool calls in either) mean
        # the loop engine has no further tool intent: the second delivered
        # message is treated as the final answer instead of nagging the model
        # into inventing tool calls it will not make (observed live: the agent
        # answered, then re-issued the same read_file 6 more times and sent a
        # second duplicate reply).
        prev_iteration_message_only = False

        # LogChat is warned at most once per turn when the agent-scope engine
        # fails (empty response, offline endpoint, rejected credentials — see
        # the classified engine-failure handling inside the iteration loop
        # below), so a persistently broken AGENT_CORTEX does not spam the
        # operator every iteration.
        agent_engine_failure_notified = False

        # Responses that contained JSON-looking (or error-page) content but
        # could not be parsed into any action — surfaced in the final result so
        # a turn that produced nothing usable is auditable as malformed output
        # rather than silent no-op.
        malformed_response_count = 0

        # Diary discipline: a single agentic turn is ONE moment, not many. The
        # model must not write a diary entry on every iteration — at most one at
        # the start and one at the end. We enforce this deterministically (the
        # prompt guidance alone is not trusted with weak cortex engines) by
        # allowing diary tool calls only on the first iteration (start) and the
        # last allowed iteration (end), and suppressing them in between.
        _DIARY_TOOLS = {"create_personal_diary_entry", "update_diary_entry"}

        import time

        start = time.monotonic()

        # Deterministic path: execute a user-provided tool plan without relying
        # on model tool-call generation. Still uses the same tool executor/safety
        # gate and returns observations in the standard agent format.
        if preplanned_calls:
            from core.agent_tool_executor import agent_tool_executor

            capped_calls = preplanned_calls[: max(1, max_iterations)]
            for i, call in enumerate(capped_calls, start=1):
                if (time.monotonic() - start) > timeout_seconds:
                    stop_reason = "timeout"
                    break

                if not isinstance(call, dict):
                    observations.append(
                        {
                            "iteration": i,
                            "role": "tool_results",
                            "content": [
                                {
                                    "tool": "",
                                    "ok": False,
                                    "result": "",
                                    "error": "invalid_planned_call",
                                }
                            ],
                        }
                    )
                    continue

                name = str(call.get("name") or call.get("type") or "").strip()
                args = call.get("arguments", call.get("payload", {}))
                if not isinstance(args, dict):
                    args = {}

                exec_result = await agent_tool_executor.execute(
                    name,
                    args,
                    context=context or {"from_cortex": True, "agent_tool": True},
                    original_message=original_message,
                )
                observations.append(
                    {
                        "iteration": i,
                        "role": "tool_results",
                        "content": [
                            {
                                "tool": name,
                                "ok": exec_result.get("ok", False),
                                "result": exec_result.get("result", ""),
                                "error": exec_result.get("error"),
                            }
                        ],
                    }
                )

            if not preplanned_then_loop:
                if not stop_reason or stop_reason == "max_iterations":
                    stop_reason = "planned_calls_done"
                result: Dict[str, Any] = {
                    "iterations": len(observations),
                    "observations": observations,
                    "final_text": "",
                    "stop_reason": stop_reason,
                }
                result["task_id"] = await self._persist_agentic_turn(
                    engine=engine,
                    goal=goal,
                    result=result,
                    context=context,
                    original_message=original_message,
                    preplanned_calls=preplanned_calls,
                    task_id=task_id,
                )
                return result
            # Under-emission seeding: continue into the model loop with the
            # real goal after the bookkeeping plan executed. Reset the stop
            # reason so the loop's own end-of-turn logic (budget/pause/etc.)
            # applies instead of the terminal "planned_calls_done".
            if stop_reason == "timeout":
                stop_reason = "max_iterations"

        for i in range(1, max_iterations + 1):
            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                stop_reason = "timeout"
                break

            remaining_budget = max(1.0, timeout_seconds - elapsed)
            # Per-call timeout must respect the real engine budget, not an
            # arbitrary hardcoded cap. Browser-backed cortex engines (e.g.
            # selenium-llm-engine) routinely need far longer than a few seconds
            # to produce a response; capping each call at 8s made every
            # iteration time out with an empty reply, so the whole agentic turn
            # returned nothing. Bound the per-call wait by the engine's own
            # response timeout (AWAIT_RESPONSE_TIMEOUT) and the remaining turn
            # budget, whichever is smaller.
            engine_timeout = float(
                config_registry.get_var("AWAIT_RESPONSE_TIMEOUT", 600)
            )
            per_call_timeout = max(2.0, min(engine_timeout, remaining_budget))

            # Build the iteration prompt: goal + prior observations.
            prompt = self._build_agent_prompt(goal, observations, engine, context)

            # Classified failure of THIS iteration's engine call, populated by
            # the handlers below / diagnostics peek. ``None`` means the engine
            # answered (or no diagnostics exist).
            engine_failure: dict[str, Any] | None = None

            try:
                if engine:
                    # An engine is pinned (via caller or the AGENT_CORTEX
                    # override) — call it directly through the Cortex registry so
                    # the agentic loop actually runs on the selected engine
                    # instead of the generic active plugin.
                    raw_response = await _call_with_hard_timeout(
                        self._call_engine_direct(prompt, engine, cortex_scope),
                        timeout=per_call_timeout,
                    )
                else:
                    from core import plugin_instance

                    raw_response = await _call_with_hard_timeout(
                        plugin_instance.handle_incoming_message(
                            bot=None, message=None, context_memory_or_prompt=prompt
                        ),
                        timeout=per_call_timeout,
                    )
            except asyncio.TimeoutError:
                # Recover the REAL cause before reporting a bare timeout: an
                # offline endpoint makes each bridge connection attempt hang
                # until the turn budget dies, so the loop sees a timeout while
                # the bridge has already recorded the underlying connection
                # error on its earlier attempts (peek survives the cancellation
                # because the bridge writes it before its retry sleep).
                bridge_error = (_peek_engine_diagnostics(engine) or {}).get("error")
                timeout_detail = (
                    f"engine call timed out at iteration {i} "
                    f"after {per_call_timeout:.1f}s"
                )
                if bridge_error:
                    timeout_detail = (
                        f"{timeout_detail}; last engine error: {bridge_error}"
                    )
                kind, hint = classify_engine_failure(bridge_error, timed_out=True)
                engine_failure = {
                    "kind": kind,
                    "hint": hint,
                    "detail": timeout_detail,
                    "engine": engine,
                }
                log_warning(f"[agent_core] {timeout_detail}")
                raw_response = ""
            except Exception as exc:
                kind, hint = classify_engine_failure(str(exc))
                engine_failure = {
                    "kind": kind,
                    "hint": hint,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "engine": engine,
                }
                log_error(
                    f"[agent_core] Engine call failed at iteration {i} ({kind}): {exc}"
                )
                observations.append(
                    {
                        "iteration": i,
                        "role": "error",
                        "content": f"engine_error ({kind}): {exc}",
                    }
                )
                if not agent_engine_failure_notified:
                    agent_engine_failure_notified = True
                    try:
                        from core.notifier import notifier

                        notifier(
                            f"⚠️ Agent cortex '{engine or 'active cortex'}' "
                            f"failed ({engine_failure['detail'][:300]}). "
                            f"{hint}"
                        )
                    except Exception as notify_exc:
                        log_debug(
                            f"[agent_core] Could not notify LogChat about the "
                            f"engine failure: {notify_exc}"
                        )
                stop_reason = "engine_error"
                break

            raw_text = (
                raw_response
                if isinstance(raw_response, str)
                else (str(raw_response) if raw_response is not None else "")
            )

            if not raw_text.strip():
                # The engine produced nothing. Recover the classified cause from
                # the bridge diagnostics (``_call_engine_direct`` swallows the
                # raised error, but the bridge records it) so the rest of this
                # block can report and act on the truthful failure kind.
                if engine_failure is None:
                    engine_failure = _engine_failure_from_diagnostics(engine)

                # Same-engine retry is pointless for structural failures (the
                # endpoint is offline or rejected the credentials and the bridge
                # already retried internally) — skip straight to the safety net.
                structural = bool(
                    engine_failure
                    and engine_failure.get("kind") in _STRUCTURAL_ENGINE_FAILURES
                )
                if not structural:
                    try:
                        remaining_after_primary = max(
                            1.0, timeout_seconds - (time.monotonic() - start)
                        )
                        fallback_text = await _call_with_hard_timeout(
                            self._call_engine_direct(prompt, engine, cortex_scope),
                            timeout=max(
                                2.0, min(engine_timeout, remaining_after_primary)
                            ),
                        )
                    except asyncio.TimeoutError:
                        fallback_text = ""
                    if fallback_text:
                        raw_text = fallback_text
                        engine_failure = None

            # Base-cortex safety net. The agent-scope engine (AGENT_CORTEX /
            # scope="agent") can be a registered endpoint that passes the
            # startup probe yet fails at call time — an offline endpoint
            # (connection errors outliving the turn budget), an expired/invalid
            # API key answering HTTP 401, or a 200 with an empty body. The
            # bridge-level retries and the same-engine retry above then hit the
            # same broken engine and the whole turn would end
            # ``empty_response`` — silently starving the out-of-band Drones
            # (goal expander / planner) so goals never gain sub-steps and never
            # advance. When the response is still empty and the agent engine is
            # NOT already the Base Cortex, retry once on the Base Cortex so a
            # misconfigured AGENT_CORTEX degrades gracefully instead of
            # blocking autonomy entirely. The warning carries the CLASSIFIED
            # cause (offline / auth / empty / timeout) instead of a single
            # guessed reason, so the operator is told what actually happened.
            if not raw_text.strip():
                base_engine: str | None = None
                try:
                    from core.config import get_active_cortex_engine

                    base_engine = await get_active_cortex_engine()
                except Exception as exc:
                    log_debug(
                        f"[agent_core] Could not resolve Base Cortex for the "
                        f"empty-response safety net: {exc}"
                    )
                if base_engine and base_engine != engine:
                    failure_desc = _describe_engine_failure(engine_failure, engine)
                    warn_msg = (
                        f"[agent_core] Agent engine {engine!r} failed "
                        f"({failure_desc}); falling back to Base Cortex "
                        f"{base_engine!r}."
                    )
                    log_warning(warn_msg)
                    if not agent_engine_failure_notified:
                        agent_engine_failure_notified = True
                        try:
                            from core.notifier import notifier

                            notifier(
                                f"⚠️ Agent cortex '{engine}' failed "
                                f"({failure_desc}). Falling back to Base Cortex "
                                f"'{base_engine}'."
                            )
                        except Exception as notify_exc:
                            log_debug(
                                f"[agent_core] Could not notify LogChat about the "
                                f"agent-engine fallback: {notify_exc}"
                            )
                    try:
                        remaining_after_base = max(
                            1.0, timeout_seconds - (time.monotonic() - start)
                        )
                        base_text = await asyncio.wait_for(
                            self._call_engine_direct(prompt, base_engine),
                            timeout=max(2.0, min(engine_timeout, remaining_after_base)),
                        )
                    except asyncio.TimeoutError:
                        base_text = ""
                    except Exception as base_exc:
                        # The safety net must never kill the turn itself.
                        log_warning(
                            f"[agent_core] Base Cortex fallback call raised: {base_exc}"
                        )
                        base_text = ""
                    if base_text and base_text.strip():
                        raw_text = base_text
                        engine_failure = None
                    else:
                        base_failure = _engine_failure_from_diagnostics(base_engine)
                        observations.append(
                            {
                                "iteration": i,
                                "role": "error",
                                "content": (
                                    f"base_cortex_fallback_failed "
                                    f"({_describe_engine_failure(base_failure, base_engine)}); "
                                    f"primary failure: {_describe_engine_failure(engine_failure, engine)}"
                                ),
                            }
                        )
                elif engine_failure is not None and not agent_engine_failure_notified:
                    # No distinct Base Cortex to fall back to (unresolvable, or
                    # identical to the broken agent engine). Still surface the
                    # classified failure once so the operator is not left with
                    # an unexplained silent turn.
                    agent_engine_failure_notified = True
                    try:
                        from core.notifier import notifier

                        notifier(
                            f"⚠️ Agent cortex '{engine or 'active cortex'}' failed "
                            f"({_describe_engine_failure(engine_failure, engine)}) "
                            f"and no separate Base Cortex fallback is available."
                        )
                    except Exception as notify_exc:
                        log_debug(
                            f"[agent_core] Could not notify LogChat about the "
                            f"engine failure: {notify_exc}"
                        )

            # Fail fast on structural engine failures (offline endpoint /
            # rejected credentials / rejected request): both the same-engine
            # retry and the Base Cortex safety net are exhausted, and further
            # iterations would just re-walk the same dead path until the turn
            # budget drains. End the turn promptly with the true cause instead
            # of grinding to ``timeout``/``empty_response``.
            if (
                not raw_text.strip()
                and engine_failure is not None
                and engine_failure.get("kind") in _STRUCTURAL_ENGINE_FAILURES
            ):
                observations.append(
                    {
                        "iteration": i,
                        "role": "error",
                        "content": (
                            f"engine_failure ({engine_failure['kind']}): "
                            f"{engine_failure['detail']}. {engine_failure['hint']}"
                        ),
                    }
                )
                log_error(
                    f"[agent_core] Ending agent turn at iteration {i}: engine "
                    f"failure is structural "
                    f"({engine_failure['kind']}) — {_describe_engine_failure(engine_failure, engine)}"
                )
                stop_reason = "engine_error"
                break

            parsed, _meta = extract_json_from_text(raw_text, return_metadata=True)
            tool_calls = self._extract_tool_calls(parsed)
            if not tool_calls:
                # Text-protocol fallback: engines that ignore the JSON-only
                # instruction and emit "Tool Call: <name>" + JSON blocks must
                # still have their tool intent executed (Langfuse 07ba4a27).
                tool_calls = self._extract_tool_calls_from_text(raw_text)

            # Defence in depth: enforce the Drone tool allow-list before ANY
            # execution. A restricted Drone (e.g. the vessel goal-expander,
            # limited to lookup_knowledge + update_goal) must never run an
            # out-of-scope tool such as an in-world ``vessel_<world>_say`` even
            # if a broken/hallucinating cortex proposes one that slipped past the
            # prompt filter. Refused calls become an observation the model sees,
            # never an execution. Structural (reads the context allow-list only,
            # no keyword/language logic); a no-op for unrestricted turns.
            if _context_allowed_tools(context) is not None:
                permitted: list[Any] = []
                for c in tool_calls:
                    c_name = str(c.get("name") or c.get("type") or "").strip()
                    if _tool_allowed_by_context(c_name, context):
                        permitted.append(c)
                    else:
                        log_warning(
                            f"[agent_core] Iteration {i}: refusing out-of-scope "
                            f"tool '{c_name}' (not in Drone allow-list)"
                        )
                        observations.append(
                            {
                                "iteration": i,
                                "role": "tool",
                                "tool": c_name,
                                "content": (
                                    f"Tool '{c_name}' is not available for this "
                                    "task. Use only the listed AVAILABLE TOOLS."
                                ),
                            }
                        )
                tool_calls = permitted

            # Explicit completion signal. The model ends the turn by calling the
            # dedicated ``attempt_completion`` tool (a sentinel, not a real
            # action). Its ``summary``/``text`` becomes the final answer. This is
            # the language-agnostic, structural end-of-turn marker used by robust
            # agents — absence of tool calls alone concludes nothing.
            completion_calls = [
                c
                for c in tool_calls
                if str(c.get("name") or c.get("type") or "").strip() == _COMPLETION_TOOL
            ]
            if completion_calls:
                tool_calls = [c for c in tool_calls if c not in completion_calls]
                summary_parts: list[str] = []
                for cc in completion_calls:
                    args = cc.get("arguments") or cc.get("payload") or {}
                    if isinstance(args, dict):
                        text = str(
                            args.get("summary")
                            or args.get("text")
                            or args.get("content")
                            or ""
                        ).strip()
                        if text:
                            summary_parts.append(text)
                if summary_parts:
                    final_text = "\n\n".join(summary_parts)
                    # The completion summary becomes BOTH the user-facing final
                    # message AND the single "final result" the agent harness
                    # persists to conversation context for the next trainer turn
                    # (start + final result only — mid-loop deliveries are not
                    # kept, by design). Prefix it structurally so a later turn
                    # reads the task as FINISHED instead of misreading the
                    # past-tense summary as a promise to still do it (Langfuse
                    # f1684175: after "good job baby" the model replied "I'll
                    # get that all read for you right away" although the task
                    # was already done).
                    final_text = f"Task complete: {final_text}"
                observations.append(
                    {
                        "iteration": i,
                        "role": "assistant",
                        "content": final_text or "(completed)",
                    }
                )
                stop_reason = "completed"
                break

            # Synth actions vs tools. A plain outbound message action (e.g.
            # ``message_telegram_bot``) is Synth talking to the user, NOT a tool
            # the agent should feed back into its loop. Recognise those synth
            # actions and split them out: the message text becomes the turn's
            # reply, while the tool executor keeps handling real tools. This
            # stops message actions from driving further iterations.
            #
            # CRITICAL: a pure-message call must still be ACTUALLY DELIVERED to
            # the originating interface via the tool executor. Historically the
            # text was only captured into ``final_text`` and never executed, so
            # on an asynchronous interface (Telegram/Discord) the message was
            # silently never sent while the turn still claimed to have replied.
            # We now execute every pure-message through ``agent_tool_executor``
            # exactly like any other action, recording the delivery outcome.
            message_calls = [
                c
                for c in tool_calls
                if _is_pure_message(str(c.get("name") or c.get("type") or ""))
            ]
            if message_calls:
                tool_calls = [c for c in tool_calls if c not in message_calls]
                collected: list[str] = []
                delivered_message_texts: list[str] = []
                for mc in message_calls:
                    mc_name = str(mc.get("name") or mc.get("type") or "")
                    args = mc.get("arguments") or mc.get("payload") or {}
                    if not isinstance(args, dict):
                        args = {}
                    text = str(args.get("text") or args.get("content") or "").strip()
                    is_delivery = _is_delivery_action(mc_name)
                    # Non-delivery speech actions are only captured; delivery
                    # actions are collected once they pass the dedup + interim
                    # cap below (a suppressed message must not be hoisted into
                    # ``final_text`` — the closing message belongs to
                    # attempt_completion / the pause composer).
                    if text and not is_delivery:
                        collected.append(text)

                    # Deliver the message to the interface. Only actual outbound
                    # interface messages (message_* delivery actions) are routed
                    # through the executor here. Voice replies go through
                    # ``send_as_voice`` on the message_* action itself (tts_speak
                    # is deprecated and is excluded from the loop's tool set);
                    # any stray non-message speech action (radio_speak) stays
                    # captured as text only.
                    if _is_delivery_action(mc_name):
                        # Cross-iteration dedup: never re-send text already
                        # delivered earlier in THIS turn (the model re-emitting
                        # the same reply instead of completing → 3x voice notes).
                        if text and text in delivered_texts:
                            delivered_ok = True
                            observations.append(
                                {
                                    "iteration": i,
                                    "role": "tool_results",
                                    "content": [
                                        {
                                            "tool": mc_name,
                                            "ok": True,
                                            "result": "already delivered",
                                            "error": None,
                                        }
                                    ],
                                }
                            )
                            continue
                        # Interim-message damper: the cap for THIS turn is
                        # already spent — suppress the re-worded update and
                        # steer the model toward completing properly. Only a
                        # MIXED iteration (message + further tool calls) is an
                        # interim update; a message-ONLY iteration is the
                        # model's answer channel (two consecutive message-only
                        # iterations end the turn as model_done) and must stay
                        # deliverable. The suppressed text is NOT hoisted into
                        # final_text (the pause composer or attempt_completion
                        # own the closing message).
                        if (
                            interim_messages_delivered >= max_interim_messages
                            and tool_calls
                        ):
                            log_info(
                                f"[agent_core] Iteration {i}: suppressing interim "
                                f"message '{mc_name}' — cap of "
                                f"{max_interim_messages} mid-task message(s) "
                                "already delivered this turn"
                            )
                            observations.append(
                                {
                                    "iteration": i,
                                    "role": "tool_results",
                                    "content": [
                                        {
                                            "tool": mc_name,
                                            "ok": True,
                                            "result": (
                                                "suppressed: the user was "
                                                f"already updated {interim_messages_delivered} "
                                                "time(s) this turn — do not send "
                                                "more interim status messages. "
                                                "Keep working silently and "
                                                "deliver the final answer via "
                                                f"{_COMPLETION_TOOL} (or tell "
                                                "the user you need more time "
                                                "in that final message)."
                                            ),
                                            "error": None,
                                        }
                                    ],
                                }
                            )
                            continue
                        if text:
                            collected.append(text)
                        exec_result = await agent_tool_executor.execute(
                            mc_name,
                            args,
                            context=context
                            or {"from_cortex": True, "agent_tool": True},
                            original_message=original_message,
                        )
                        if not exec_result.get("ok"):
                            fixed_args, changed = _programmatic_delivery_fix(args)
                            if changed:
                                log_info(
                                    f"[agent_core] Iteration {i}: message "
                                    f"'{mc_name}' failed; retrying once with "
                                    f"programmatic fix (interface_path preserved)"
                                )
                                exec_result = await agent_tool_executor.execute(
                                    mc_name,
                                    fixed_args,
                                    context=context
                                    or {"from_cortex": True, "agent_tool": True},
                                    original_message=original_message,
                                )
                        delivered_ok = bool(exec_result.get("ok"))
                        if delivered_ok:
                            delivered_message_ok = True
                            interim_messages_delivered += 1
                            delivered_text = str(
                                args.get("text") or args.get("content") or ""
                            ).strip()
                            if delivered_text:
                                delivered_message_texts.append(delivered_text)
                                delivered_texts.add(delivered_text)
                        else:
                            delivery_failures.append(mc_name)
                            log_warning(
                                f"[agent_core] Iteration {i}: outbound message "
                                f"'{mc_name}' FAILED to deliver: "
                                f"{exec_result.get('error')}"
                            )
                        observations.append(
                            {
                                "iteration": i,
                                "role": "tool_results",
                                "content": [
                                    {
                                        "tool": mc_name,
                                        "ok": delivered_ok,
                                        "result": exec_result.get("result", ""),
                                        "error": exec_result.get("error"),
                                    }
                                ],
                            }
                        )
                        log_info(
                            f"[agent_core] Iteration {i}: outbound message "
                            f"'{mc_name}' delivered ok={delivered_ok}"
                        )
                if collected:
                    delivered_set = {
                        t for t in delivered_message_texts
                    } | delivered_texts
                    # Keep only texts that were NOT already delivered through
                    # the executor (the Agent Lane router re-sends final_text
                    # as a fresh outbound message — a delivered message must
                    # never be sent a second time) plus non-delivery speech
                    # actions that were only captured.
                    remaining = [t for t in collected if t.strip() not in delivered_set]
                    if remaining:
                        final_text = "\n\n".join(remaining)

            # Under the explicit-completion contract, the ONLY structural
            # end-of-turn signal is ``attempt_completion`` (handled above) or
            # exhausting the iteration budget. A plain outbound message — even on
            # a synchronous interface like Ollama — is NOT proof the goal is
            # done: weak models routinely emit an intent statement ("I'll check
            # the codebase now...") as a message and would otherwise stop there.
            require_explicit_completion = bool(
                config_registry.get_var("AGENT_REQUIRE_EXPLICIT_COMPLETION", True)
            )

            if not tool_calls:
                # Iteration-1 self-correction for router over-routing. If the
                # VERY FIRST model turn produces only a user-facing message and
                # NO real tool call, the request was effectively conversational
                # and never needed the Agent Lane — the router 2.0 over-routed a
                # simple reply. Deliver that message as the final answer and end
                # the turn cleanly instead of nagging the model to invent tool
                # calls it does not need. Structural only (message vs tool), no
                # keyword/language logic.
                if (
                    i == 1
                    and message_calls
                    and (final_text.strip() or delivered_message_texts)
                ):
                    log_info(
                        "[agent_core] Iteration 1 produced only a message and no "
                        "tool calls; treating as a conversational reply "
                        "(router over-routing) and ending the turn"
                    )
                    observations.append(
                        {
                            "iteration": i,
                            "role": "assistant",
                            "content": final_text or "(message delivered)",
                        }
                    )
                    stop_reason = "no_tools_required"
                    break

                # Only synth message actions this iteration (no real tools left).
                if message_calls and (final_text.strip() or delivered_message_texts):
                    if require_explicit_completion and i < max_iterations:
                        if prev_iteration_message_only:
                            # Two consecutive message-only iterations: the loop
                            # engine has no further tool intent. Treat the
                            # delivered message as the final answer instead of
                            # nagging it into inventing tool calls (observed
                            # live: the agent answered, then re-issued the same
                            # read_file 6 more times and sent a second reply).
                            log_info(
                                "[agent_core] Two consecutive message-only "
                                "iterations — ending the turn with the "
                                "delivered reply"
                            )
                            observations.append(
                                {
                                    "iteration": i,
                                    "role": "assistant",
                                    "content": final_text or "(message delivered)",
                                }
                            )
                            stop_reason = "model_done"
                            break
                        prev_iteration_message_only = True
                        # Deliver the message as an intermediate reply but keep
                        # working: the model must still call attempt_completion
                        # (or run out of iterations) to genuinely end the turn.
                        observations.append(
                            {
                                "iteration": i,
                                "role": "assistant",
                                "content": final_text,
                            }
                        )
                        observations.append(
                            {
                                "iteration": i,
                                "role": "system",
                                "content": (
                                    "You sent a message but the goal is NOT "
                                    "finished yet. A message is not a completion "
                                    "signal. Do not stop at an intent statement — "
                                    "take the next tool action to make real "
                                    "progress. When (and only when) the goal is "
                                    "genuinely accomplished, call the "
                                    f"{_COMPLETION_TOOL} tool with a short summary."
                                ),
                            }
                        )
                        stop_reason = "max_iterations"
                        continue

                    # Explicit-completion ON but iterations exhausted: the goal
                    # was never explicitly completed. Do NOT declare the task
                    # done — pause it so the user can grant more iterations via
                    # "Continue". The message is kept as the latest reply.
                    if require_explicit_completion:
                        observations.append(
                            {
                                "iteration": i,
                                "role": "assistant",
                                "content": final_text or "(message delivered)",
                            }
                        )
                        stop_reason = "paused_max_iterations"
                        break

                    # Explicit-completion disabled: the message is the final
                    # reply; end the turn.
                    observations.append(
                        {
                            "iteration": i,
                            "role": "assistant",
                            "content": final_text or "(message delivered)",
                        }
                    )
                    stop_reason = "model_done"
                    break

                # A non-message-only iteration resets the consecutive-message
                # detector: the loop engine still has tool intent.
                prev_iteration_message_only = False

                if not raw_text.strip():
                    # Carry the classified cause into the audit observation so a
                    # failed task explains itself (offline endpoint vs bad key
                    # vs plain empty body) instead of a bare marker.
                    empty_detail = (
                        f" ({engine_failure['kind']}: {engine_failure['detail']})"
                        if engine_failure
                        else ""
                    )
                    observations.append(
                        {
                            "iteration": i,
                            "role": "error",
                            "content": f"empty_model_response{empty_detail}",
                        }
                    )
                    stop_reason = "empty_response"
                    continue

                # Malformed protocol response: the model emitted JSON-looking
                # content that failed to parse (or an HTML error page from a
                # broken proxy/gateway) and no tool call, completion, or message
                # could be extracted. This is NOT a valid answer — the raw text
                # must never become ``final_text``. Record it so the task audit
                # shows why the turn produced nothing usable, and nudge the
                # model to re-emit valid JSON. Structural (parse metadata +
                # response shape only, never content keywords).
                _malformed = (
                    parsed is None
                    and bool(raw_text.strip())
                    and (
                        int(_meta.get("error_count") or 0) > 0
                        or raw_text.lstrip().lower().startswith(("<html", "<!doctype"))
                    )
                )
                if _malformed:
                    malformed_response_count += 1
                    _malformed_head = raw_text.strip()[:120].replace("\n", " ")
                    observations.append(
                        {
                            "iteration": i,
                            "role": "error",
                            "content": (
                                f"malformed_response: output could not be parsed "
                                f"({_meta.get('error_count', 0)} broken JSON "
                                f"fragment(s)); starts with: {_malformed_head!r}"
                            ),
                        }
                    )
                    if i < max_iterations:
                        observations.append(
                            {
                                "iteration": i,
                                "role": "system",
                                "content": (
                                    "Your previous response contained malformed "
                                    "or unparseable JSON, so no action could be "
                                    "executed. Re-emit ONE valid JSON object with "
                                    "your next tool calls (or a user message if "
                                    "you genuinely mean to reply in text)."
                                ),
                            }
                        )
                        stop_reason = "max_iterations"
                        continue
                    stop_reason = "malformed_response"
                    break

                # Bare text, no tool calls, no user-facing message. Under the
                # explicit-completion contract this is NOT "done": weak models
                # often stop here with an intent statement ("I'll check the
                # codebase now...") instead of actually finishing the goal. Keep
                # the text as an intermediate observation, re-inject a structural
                # nudge, and continue the loop. The turn only ends via a user
                # message, ``attempt_completion``, or exhausting iterations.
                if require_explicit_completion and i < max_iterations:
                    observations.append(
                        {"iteration": i, "role": "assistant", "content": raw_text}
                    )
                    observations.append(
                        {
                            "iteration": i,
                            "role": "system",
                            "content": (
                                "You responded with text but no tool calls and no "
                                "user message. The goal is NOT finished yet. Do not "
                                "stop at an intent statement — take the next tool "
                                "action to make real progress. When (and only when) "
                                "the goal is genuinely accomplished, call the "
                                f"{_COMPLETION_TOOL} tool with a short summary, or "
                                "send the final answer as a user message."
                            ),
                        }
                    )
                    final_text = raw_text
                    stop_reason = "max_iterations"
                    continue

                # Explicit-completion ON but iterations exhausted with only a
                # bare intent statement: the goal was never explicitly finished.
                # Pause the task instead of faking completion, so the user can
                # grant more iterations via "Continue".
                if require_explicit_completion:
                    final_text = raw_text
                    observations.append(
                        {"iteration": i, "role": "assistant", "content": raw_text}
                    )
                    stop_reason = "paused_max_iterations"
                    break

                # Explicit-completion disabled: fall back to the legacy
                # behaviour and keep the text as final.
                final_text = raw_text
                observations.append(
                    {"iteration": i, "role": "assistant", "content": raw_text}
                )
                stop_reason = "model_done"
                break

            # Execute each tool call and collect observations.
            # Diary entries are allowed only on the first (start) and last (end)
            # iteration; suppress them on the intermediate working iterations so
            # a single task produces at most one opening and one closing entry.
            prev_iteration_message_only = False
            diary_allowed_this_iteration = i == 1 or i == max_iterations
            iteration_results: list[Dict[str, Any]] = []
            for call in tool_calls:
                name = call.get("name") or call.get("type") or ""
                args = call.get("arguments") or call.get("payload") or {}
                if not isinstance(args, dict):
                    args = {}
                if name in _DIARY_TOOLS and not diary_allowed_this_iteration:
                    log_info(
                        f"[agent_core] Iteration {i}: suppressing mid-task diary "
                        f"tool '{name}' (diary allowed only at start/end of turn)"
                    )
                    iteration_results.append(
                        {
                            "tool": name,
                            "ok": False,
                            "result": "",
                            "error": (
                                "diary_suppressed_mid_task: write a diary entry "
                                "only at the start or the end of the task, not on "
                                "intermediate iterations"
                            ),
                        }
                    )
                    continue

                # Cross-iteration identical-call dedup: never re-execute the
                # same (name, args) tool call within one turn. Weak engines
                # re-issue the identical call when they think a result was
                # truncated (observed live: the same agent_read_file call
                # repeated 7x). Return the first result with a structural note.
                # Two structural exemptions:
                # * Diary tools: their start/end slots are enforced
                #   deterministically above, and the closing entry is expected
                #   to run even when its content resembles the opening one.
                # * Repeatable (poll/refresh) actions: re-invoking them is the
                #   POINT — serving a cached first result made every poll of a
                #   slow external operation see stale state forever, so the
                #   model could never observe progress and looped until the
                #   turn budget died (observed live: a Soulseek transfer stuck
                #   "queued" polled ~15x, each poll returning the cached first
                #   snapshot). Actions opt in via ``"repeatable": true`` in
                #   their get_supported_actions() schema.
                _call_key = (name, json.dumps(args, sort_keys=True, default=str))
                cached = (
                    executed_calls.get(_call_key)
                    if name not in _DIARY_TOOLS
                    and name not in _repeatable_action_names()
                    else None
                )
                if cached is not None:
                    log_info(
                        f"[agent_core] Iteration {i}: identical tool call "
                        f"'{name}' already executed this turn — returning "
                        "cached result"
                    )
                    iteration_results.append(
                        {
                            **cached,
                            "note": (
                                "identical call already executed earlier this "
                                "turn; result unchanged — do not repeat it"
                            ),
                        }
                    )
                    continue
                exec_result = await agent_tool_executor.execute(
                    name,
                    args,
                    context=context or {"from_cortex": True, "agent_tool": True},
                    original_message=original_message,
                )

                # Programmatic delivery self-repair: if a message_* action
                # failed, try one cheap, safe fix (text sanitisation, deriving a
                # missing target) and re-execute BEFORE bothering the model. The
                # interface_path is never touched, so the retry can only reach
                # the exact destination the model already chose.
                if not exec_result.get("ok") and _is_delivery_action(str(name)):
                    fixed_args, changed = _programmatic_delivery_fix(args)
                    if changed:
                        log_info(
                            f"[agent_core] Iteration {i}: delivery tool '{name}' "
                            f"failed; retrying once with programmatic fix "
                            f"(interface_path preserved)"
                        )
                        retry_result = await agent_tool_executor.execute(
                            name,
                            fixed_args,
                            context=context
                            or {"from_cortex": True, "agent_tool": True},
                            original_message=original_message,
                        )
                        if retry_result.get("ok"):
                            log_info(
                                f"[agent_core] Iteration {i}: delivery tool "
                                f"'{name}' succeeded after programmatic fix"
                            )
                            exec_result = retry_result
                        else:
                            log_warning(
                                f"[agent_core] Iteration {i}: delivery tool "
                                f"'{name}' still failing after programmatic fix; "
                                f"surfacing error to model"
                            )
                            exec_result = retry_result

                # Safety-net observability: flag when internal task-log /
                # monologue structure leaked into a user-facing message. This is
                # non-blocking — the message is already delivered — but a WARNING
                # makes the leak visible in logs for later prompt tuning.
                if _is_delivery_action(str(name)):
                    delivered_text = args.get("text") or args.get("content")
                    leak_signals = _looks_like_internal_monologue(
                        delivered_text if isinstance(delivered_text, str) else ""
                    )
                    if leak_signals:
                        log_warning(
                            f"[agent_core] Iteration {i}: delivery tool '{name}' "
                            f"message appears to contain internal monologue / "
                            f"task-log structure (signals={leak_signals}). It was "
                            f"still delivered; consider using note_to_self for "
                            f"private reasoning."
                        )

                iteration_results.append(
                    {
                        "tool": name,
                        "ok": exec_result.get("ok", False),
                        "result": exec_result.get("result", ""),
                        "error": exec_result.get("error"),
                    }
                )
                # Cache the final result (after any programmatic retry) for the
                # cross-iteration identical-call dedup above.
                executed_calls[_call_key] = {
                    "tool": name,
                    "ok": exec_result.get("ok", False),
                    "result": exec_result.get("result", ""),
                    "error": exec_result.get("error"),
                }
                log_info(
                    f"[agent_core] Iteration {i}: tool '{name}' "
                    f"ok={exec_result.get('ok')}"
                )

            observations.append(
                {
                    "iteration": i,
                    "role": "tool_results",
                    "content": iteration_results,
                }
            )

        # Loop fell through the iteration budget while still executing tool
        # calls (never called attempt_completion). Under the explicit-completion
        # contract this is NOT a completion — pause the task so the user can
        # grant more iterations via "Continue" instead of marking it done.
        if stop_reason == "max_iterations" and bool(
            config_registry.get_var("AGENT_REQUIRE_EXPLICIT_COMPLETION", True)
        ):
            stop_reason = "paused_max_iterations"

        # Paused (budget exhausted without explicit completion): let Synth
        # author the "I'm not done, shall I continue?" message itself, in the
        # conversation's own language/tone, instead of shipping a hardcoded
        # English string. Overwrite ``final_text`` with the composed message so
        # every delivery path (Telegram/Discord/API) shows Synth's own words.
        if stop_reason == "paused_max_iterations":
            actions_executed = 0
            for obs in observations:
                if isinstance(obs, dict) and obs.get("role") == "tool_results":
                    content = obs.get("content")
                    if isinstance(content, list):
                        actions_executed += len(content)
            composed = await self._compose_pause_message(
                goal=goal,
                observations=observations,
                actions_executed=actions_executed,
                engine=engine,
                context=context,
            )
            if composed:
                final_text = composed

        # Timeout with real work done: the turn ran out of TIME (not
        # iterations) while still mid-task. Without this, the loop ends with
        # an empty ``final_text`` and NOTHING is delivered — the user sees
        # silence even though the agent diagnosed the situation mid-loop
        # (observed live: a stuck-queued download was analysed in every
        # iteration's reasoning, yet Telegram never received a word because
        # the turn died at the budget with no message). Compose the same
        # model-authored "here's where I am, shall I continue?" pause message
        # and re-classify the stop reason so the task parks as resumable
        # ``pending`` (a raw ``timeout`` persists as ``failed``) and the
        # router's garbage-output guard (which only suppresses ``timeout``
        # with ZERO actions) lets it through.
        if stop_reason == "timeout" and not str(final_text or "").strip():
            actions_executed = 0
            for obs in observations:
                if isinstance(obs, dict) and obs.get("role") == "tool_results":
                    content = obs.get("content")
                    if isinstance(content, list):
                        actions_executed += len(content)
            if actions_executed > 0:
                composed = await self._compose_pause_message(
                    goal=goal,
                    observations=observations,
                    actions_executed=actions_executed,
                    engine=engine,
                    context=context,
                )
                if composed:
                    final_text = composed
                    stop_reason = "paused_timeout"
                    log_info(
                        "[agent_core] Turn hit the time budget mid-task after "
                        f"{actions_executed} action(s); composed a pause message "
                        "for delivery (task parks as resumable 'pending')."
                    )

        # Completion-integrity gate. A turn must never report "completed" while
        # an outbound message it claimed to send actually failed to reach the
        # interface. If every attempted delivery failed and none succeeded,
        # surface the discrepancy instead of faking a successful reply: record
        # the failure as an observation and downgrade the stop reason so the
        # persistence layer does not mark the task ``completed``.
        if (
            stop_reason == "completed"
            and delivery_failures
            and not delivered_message_ok
        ):
            log_warning(
                "[agent_core] Completion-integrity gate: turn signalled "
                f"completion but {len(delivery_failures)} outbound message(s) "
                f"failed to deliver ({', '.join(delivery_failures)}) and none "
                "succeeded; downgrading from 'completed' to 'delivery_failed'"
            )
            observations.append(
                {
                    "iteration": len(observations),
                    "role": "error",
                    "content": (
                        "delivery_failed: the turn claimed completion but the "
                        "outbound message(s) "
                        f"{', '.join(delivery_failures)} never reached the "
                        "interface. The reply was NOT delivered."
                    ),
                }
            )
            stop_reason = "delivery_failed"

        result: Dict[str, Any] = {
            "iterations": len(observations),
            "observations": observations,
            "final_text": final_text,
            "stop_reason": stop_reason,
        }
        if malformed_response_count:
            result["malformed_responses"] = malformed_response_count
        result["task_id"] = await self._persist_agentic_turn(
            engine=engine,
            goal=goal,
            result=result,
            context=context,
            original_message=original_message,
            preplanned_calls=preplanned_calls,
            task_id=task_id,
        )
        return result

    async def run_drone(
        self,
        *,
        goal: str,
        engine: str | None = None,
        context: Dict[str, Any] | None = None,
        parent_task_id: int | None = None,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        original_message: Any = None,
        allowed_tools: set[str] | None = None,
        cortex_scope: str = "agent",
    ) -> Dict[str, Any]:
        """Run an ephemeral, task-scoped sub-agent ("Drone").

        A Drone is a single-level delegation: it runs through the same bounded
        :meth:`run_agentic_turn` loop but with a tighter budget
        (``DRONE_MAX_ITERATIONS`` / ``DRONE_TURN_TIMEOUT_SEC``) and is flagged so
        it cannot spawn further Drones. The flag is enforced both by the
        ``spawn_drone`` handler (recursion guard) and by
        :meth:`_build_agent_prompt`, which hides the ``spawn_drone`` tool from a
        Drone's tool list.

        Args:
            goal: The focused sub-task objective for the Drone.
            engine: Optional cortex engine name. When ``None`` the Drone inherits
                the ``cortex_scope`` engine (same resolution as the parent Agent).
            cortex_scope: Cortex scope used to resolve the engine/model when
                ``engine`` is ``None`` (default ``"agent"``). Out-of-band vessel
                Drones (the goal expander/planner) pass ``"vessel"`` so they run
                on VESSEL_CORTEX instead of the generic AGENT_CORTEX — which may
                be a slow browser-driven engine that cannot complete a
                multi-step tool-calling turn within the Drone budget.
            context: Optional context dict; a ``drone`` marker is injected.
            parent_task_id: DB id of the Agent task that spawned this Drone.
            max_iterations: Hard cap (defaults to ``DRONE_MAX_ITERATIONS``).
            timeout_seconds: Wall-clock budget (defaults to ``DRONE_TURN_TIMEOUT_SEC``).
            original_message: Optional originating message (for audit/safety).
            allowed_tools: Optional allow-list of tool names this Drone may use.
                When provided, the Drone's prompt only lists these tools AND the
                executor refuses any tool outside the set (defence in depth), so
                a task-scoped Drone (e.g. the vessel goal-expander/planner) can
                never emit an out-of-scope action — such as an in-world
                ``vessel_<world>_say`` — even if a broken/hallucinating cortex
                proposes one. The completion sentinel is always implicitly
                allowed. Structural, keyword-free.

        Returns:
            The standard :meth:`run_agentic_turn` result dict (``iterations``,
            ``observations``, ``final_text``, ``stop_reason``, ``task_id``).
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("DRONE_MAX_ITERATIONS", 3))
        if timeout_seconds is None:
            timeout_seconds = float(
                config_registry.get_var("DRONE_TURN_TIMEOUT_SEC", 90)
            )

        drone_context: Dict[str, Any] = dict(context or {})
        drone_meta: Dict[str, Any] = {
            "is_drone": True,
            "parent_task_id": parent_task_id,
        }
        if allowed_tools:
            drone_meta["allowed_tools"] = sorted(str(t) for t in allowed_tools)
        drone_context["drone"] = drone_meta

        log_info(
            f"[agent_core] Spawning Drone (parent_task_id={parent_task_id}, "
            f"max_iterations={max_iterations}, timeout={timeout_seconds}s)"
        )

        return await self.run_agentic_turn(
            goal=goal,
            engine=engine,
            context=drone_context,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
            original_message=original_message,
            cortex_scope=cortex_scope,
        )

    async def run_agent_drone(
        self,
        *,
        goal: str,
        engine: str | None = None,
        context: Dict[str, Any] | None = None,
        parent_task_id: int | None = None,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        original_message: Any = None,
        allowed_tools: set[str] | None = None,
        cortex_scope: str = "agent",
    ) -> Dict[str, Any]:
        """Run a task-scoped sub-agent with the full **Agent** budget.

        An *agent Drone* is the same single-level, task-scoped delegation as an
        ordinary :meth:`run_drone` (it cannot spawn further Drones, and it honours
        the ``allowed_tools`` allow-list), but it is deliberately given the
        Agent's iteration/time budget (``AGENT_MAX_ITERATIONS`` /
        ``AGENT_TURN_TIMEOUT_SEC``) instead of the tight Drone budget
        (``DRONE_MAX_ITERATIONS`` / ``DRONE_TURN_TIMEOUT_SEC``). This lets it
        genuinely *reason*: ask itself questions, consult the knowledge base over
        several iterations and refine before committing — work that a 3-iteration
        Drone cannot complete. It is meant for open-ended reasoning sub-tasks
        such as breaking a self-authored goal into an ordered plan.

        The larger budget is the ONLY difference from :meth:`run_drone`; every
        safety property (single-level delegation, tool allow-list, cortex scope,
        no vessel ``interface_path`` so it is never attributed to an embodiment
        turn) is identical.

        Args:
            goal: The focused reasoning objective for the agent Drone.
            engine: Optional cortex engine name. When ``None`` the Drone inherits
                the ``cortex_scope`` engine (same resolution as the parent Agent).
            cortex_scope: Cortex scope used to resolve the engine/model when
                ``engine`` is ``None`` (default ``"agent"``). Out-of-band vessel
                agent Drones (the goal expander/planner) pass ``"vessel"``.
            context: Optional context dict; a ``drone`` marker is injected.
            parent_task_id: DB id of the Agent task that spawned this Drone.
            max_iterations: Hard cap (defaults to ``AGENT_MAX_ITERATIONS``).
            timeout_seconds: Wall-clock budget (defaults to ``AGENT_TURN_TIMEOUT_SEC``).
            original_message: Optional originating message (for audit/safety).
            allowed_tools: Optional allow-list of tool names this Drone may use.

        Returns:
            The standard :meth:`run_agentic_turn` result dict.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 30))
        if timeout_seconds is None:
            timeout_seconds = float(
                config_registry.get_var("AGENT_TURN_TIMEOUT_SEC", 3600)
            )

        drone_context: Dict[str, Any] = dict(context or {})
        drone_meta: Dict[str, Any] = {
            "is_drone": True,
            "is_agent_drone": True,
            "parent_task_id": parent_task_id,
        }
        if allowed_tools:
            drone_meta["allowed_tools"] = sorted(str(t) for t in allowed_tools)
        drone_context["drone"] = drone_meta

        log_info(
            f"[agent_core] Spawning agent Drone (parent_task_id={parent_task_id}, "
            f"max_iterations={max_iterations}, timeout={timeout_seconds}s)"
        )

        return await self.run_agentic_turn(
            goal=goal,
            engine=engine,
            context=drone_context,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
            original_message=original_message,
            cortex_scope=cortex_scope,
        )

    async def _call_engine_direct(
        self,
        prompt: Dict[str, Any],
        engine_name: str | None,
        cortex_scope: str = "agent",
        *,
        native_tools: bool = True,
    ) -> str:
        """Fallback direct call to the active cortex engine.

        Some runtime paths can return an empty string through ``plugin_instance``
        even when the model produced output. This fallback talks to the engine
        directly and returns its raw text response.

        ``cortex_scope`` selects which cortex scope's per-scope model override to
        honour (default ``"agent"``). Out-of-band vessel Drones (the goal
        expander/planner) pass ``"vessel"`` so the model matches VESSEL_CORTEX
        rather than the generic AGENT_CORTEX.
        """
        try:
            from core.config import (
                get_active_cortex_engine,
                get_active_cortex_scope,
                scope_model_override,
            )
            from core.cortex_registry import get_cortex_registry

            # Resolve the per-scope model so a scope-specific model override is
            # honoured even on this direct fallback path. The scope defaults to
            # "agent" (ordinary agentic turns) but callers may pin another scope
            # (e.g. "vessel" for the goal-expansion Drone).
            scope_model: str | None = None
            if engine_name:
                resolved_engine = engine_name
                try:
                    _, scope_model = await get_active_cortex_scope(scope=cortex_scope)
                except Exception:
                    scope_model = None
            else:
                try:
                    resolved_engine, scope_model = await get_active_cortex_scope(
                        scope=cortex_scope
                    )
                except Exception:
                    resolved_engine = await get_active_cortex_engine()
            if not resolved_engine:
                return ""

            registry = get_cortex_registry()
            engine = registry.get_engine(resolved_engine) or registry.load_engine(
                resolved_engine
            )
            if engine is None:
                return ""

            # Agentic turns MUST go through the role-separated message path.
            # The agent prompt carries an ``agent_turn`` block under the
            # ``system_message`` key; ``handle_incoming_message`` -> _build_messages
            # would mistake that for a corrector payload (it keys off
            # ``system_message``), discarding the real GOAL/TOOLS/system text and
            # emitting an almost-empty prompt. External web-driven engines (e.g.
            # selenium-llm-engine) then pad that empty prompt with their own
            # canvas/JSON boilerplate. Passing explicit role-separated messages to
            # ``generate_response`` bypasses _build_messages entirely and delivers
            # the actual agentic prompt.
            payload = prompt.get("input", {}).get("payload", {})
            text = str(payload.get("text", ""))
            system = str(payload.get("system", ""))
            if prompt.get("agent_mode") and hasattr(engine, "generate_response"):
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ]
                # Agent-route engine profile: thinking on + native tool calls
                # (AGENT_ENABLE_THINKING / AGENT_NATIVE_TOOLS /
                # AGENT_PARALLEL_TOOL_CALLS). Only external endpoint bridges
                # (identifiable by ``_adapter``) accept these request kwargs;
                # plugin engines keep the plain positional call. The
                # pause-composer passes ``native_tools=False`` to write prose
                # without offering tools/thinking.
                agent_call_kwargs: dict[str, Any] = {}
                if native_tools:
                    if bool(config_registry.get_var("AGENT_ENABLE_THINKING", True)):
                        agent_call_kwargs["enable_thinking"] = True
                    if bool(config_registry.get_var("AGENT_NATIVE_TOOLS", True)):
                        manifests = _build_openai_tool_manifests()
                        if manifests:
                            agent_call_kwargs["tools"] = manifests
                            agent_call_kwargs["tool_choice"] = "auto"
                            agent_call_kwargs["parallel_tool_calls"] = bool(
                                config_registry.get_var(
                                    "AGENT_PARALLEL_TOOL_CALLS", True
                                )
                            )
                with scope_model_override(engine, scope_model):
                    if agent_call_kwargs and hasattr(engine, "_adapter"):
                        res = await engine.generate_response(
                            messages, **agent_call_kwargs
                        )
                    else:
                        res = await engine.generate_response(messages)
                return res if isinstance(res, str) else (str(res) if res else "")

            if hasattr(engine, "handle_incoming_message"):
                try:
                    # Common signature used by many Cortex engines
                    with scope_model_override(engine, scope_model):
                        res = await engine.handle_incoming_message(
                            bot=None,
                            message=None,
                            context_memory_or_prompt=prompt,
                        )
                except TypeError:
                    # Fallback for engines expecting positional prompt arg.
                    with scope_model_override(engine, scope_model):
                        res = await engine.handle_incoming_message(None, None, prompt)
                return res if isinstance(res, str) else (str(res) if res else "")

            if hasattr(engine, "generate_response"):
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ]
                with scope_model_override(engine, scope_model):
                    res = await engine.generate_response(messages)
                return res if isinstance(res, str) else (str(res) if res else "")
        except Exception as exc:
            log_warning(
                f"[agent_core] Direct engine call to {engine_name!r} failed: "
                f"{type(exc).__name__}: {exc}"
            )
        return ""

    async def persona_voiceover(
        self,
        final_text: str,
        *,
        goal: str = "",
        context: Dict[str, Any] | None = None,
        message: Any = None,
    ) -> str:
        """Re-voice an agent turn's final result through the persona.

        The agentic loop deliberately runs persona-free (the ~32k persona
        prompt on every tool iteration would bloat each call and degrade
        tool discipline), so its final text is operationally correct but
        tonally flat. This asks the **Base Cortex** — the engine that speaks
        as Synth in ordinary chat — with the FULL persona chat context
        (persona, memories, conversation, language) to deliver the result in
        Synth's own voice, keeping every fact intact.

        Returns the re-voiced text, or ``""`` on any failure — the caller
        then delivers the original agent text unchanged. A styling hiccup
        must never lose the result.
        """
        if not str(final_text or "").strip():
            return ""
        try:
            from types import SimpleNamespace

            from core.prompt_engine import build_prompt_request

            # Build the SAME persona prompt an ordinary chat reply on this
            # interface would get: the synthetic user message carries the
            # restyle instruction, and the history/persona machinery keys on
            # the real interface_path from the original message / context.
            interface_path = None
            if isinstance(context, dict):
                interface_path = context.get("interface_path")
            if not interface_path:
                interface_path = getattr(message, "interface_path", None)
            interface_name = None
            if isinstance(interface_path, str) and "/" in interface_path:
                interface_name = interface_path.split("/", 1)[0]

            instruction = (
                "Your tool-using agent side just finished working on a task"
                + (f" ('{goal.strip()}')" if str(goal or "").strip() else "")
                + " — it does the mechanical work, you do the talking. It "
                "produced this result to report to the user:\n\n"
                f"{final_text.strip()}\n\n"
                "Deliver this result to the user now, in YOUR own voice — "
                "same language and tone as this conversation. Keep every "
                "fact and outcome intact (paths, names, numbers, successes, "
                "failures); change only the voice, and keep it about as "
                "short as the original. Reply ONLY with the message text."
            )
            synth_message = SimpleNamespace(
                interface_path=interface_path,
                text=instruction,
                sender_id=getattr(message, "sender_id", None),
                is_from_self=False,
            )
            prompt = await build_prompt_request(
                synth_message,
                {"interface_path": interface_path} if interface_path else {},
                interface_name,
            )
            payload = (prompt.get("input") or {}).get("payload") or {}
            persona_system = str(payload.get("system") or "")

            # Route through the Base Cortex (the persona chat engine), prose
            # only — no thinking, no native tools. Same bounded/detachable
            # call shape as the pause composer.
            from core.config import get_active_cortex_engine

            base_engine = await get_active_cortex_engine()
            if not base_engine:
                return ""
            voice_prompt: Dict[str, Any] = {
                "input": {
                    "payload": {
                        "text": instruction,
                        "system": persona_system,
                    }
                },
                "system_message": {
                    "type": "agent_turn",
                    "engine": base_engine,
                    "goal": goal,
                },
                "agent_mode": True,
                "observation_history": [],
            }
            if context:
                voice_prompt["context"] = context
            text = await _call_with_hard_timeout(
                self._call_engine_direct(voice_prompt, base_engine, native_tools=False),
                timeout=30.0,
            )
            if not isinstance(text, str):
                return ""
            text = text.strip()
            if not text:
                return ""
            # The chat-shaped persona prompt invites an action JSON reply —
            # a message action's text IS the styled reply, so unwrap it.
            if text.startswith("{") or text.startswith("```"):
                try:
                    from core.transport_layer import extract_json_from_text

                    parsed = extract_json_from_text(text)
                    if isinstance(parsed, dict):
                        actions = parsed.get("actions")
                        if isinstance(actions, list):
                            for act in actions:
                                if not isinstance(act, dict):
                                    continue
                                a_text = str(
                                    (act.get("payload") or {}).get("text") or ""
                                ).strip()
                                if a_text:
                                    return a_text
                except Exception:
                    pass
                # Unparseable JSON wrapper — do not ship raw JSON to the user.
                return ""
            return text
        except Exception as exc:
            log_debug(f"[agent_core] persona_voiceover failed: {exc}")
            return ""

    async def _compose_pause_message(
        self,
        *,
        goal: str,
        observations: list[Dict[str, Any]],
        actions_executed: int,
        engine: str | None,
        context: Dict[str, Any] | None,
    ) -> str:
        """Have Synth write the "I need more turns, shall I continue?" message.

        When an agentic turn exhausts its iteration budget without an explicit
        completion, the user must be told — but the text must be authored BY the
        model in the language and tone of the ongoing conversation, never a
        hardcoded English string (which would also wrongly reference a WebUI-only
        "Continue" button on chat interfaces where none exists).

        This asks the active cortex to produce a short, natural, plain-text
        message summarising what was done so far and asking whether to keep
        going. It returns the generated text, or an empty string on any failure
        (the caller then falls back to the model's last real reply).
        """
        try:
            instruction = (
                "You are Synth. You have been working on a task for a user but "
                "have reached your action budget for this turn WITHOUT finishing "
                f"it. So far you have carried out {actions_executed} action(s). "
                "Write a SHORT, natural message to the user, in the SAME language "
                "and tone as the ongoing conversation, that: (1) briefly says what "
                "you have been doing, (2) makes clear the task is not finished "
                "yet, and (3) asks whether they want you to continue. Do NOT "
                "mention any button, UI element or technical detail — the user "
                "may just reply in chat to tell you to keep going. Reply with the "
                "message text ONLY, no JSON, no tool call, no quotes."
            )
            history_lines: list[str] = []
            for obs in observations[-12:]:
                role = obs.get("role", "system")
                content = obs.get("content", "")
                if isinstance(content, list):
                    for item in content:
                        status = (
                            "OK" if item.get("ok") else f"ERROR: {item.get('error')}"
                        )
                        history_lines.append(f"[tool:{item.get('tool')}] {status}")
                else:
                    history_lines.append(f"[{role}] {content}")
            history_block = "\n".join(history_lines)
            prompt: Dict[str, Any] = {
                "input": {
                    "payload": {
                        "text": (
                            f"GOAL: {goal}\n\nWHAT YOU DID SO FAR:\n{history_block}\n"
                        ),
                        "system": instruction,
                    }
                },
                "system_message": {
                    "type": "agent_turn",
                    "engine": engine,
                    "goal": goal,
                },
                "agent_mode": True,
                "observation_history": observations,
            }
            if context:
                prompt["context"] = context
            # Bounded (and detachable): a hung engine must not freeze the turn
            # that is trying to tell the user it needs more turns. Write prose
            # only — the agent profile (thinking/native tools) is disabled so
            # the engine answers with plain text instead of a JSON action.
            text = await _call_with_hard_timeout(
                self._call_engine_direct(prompt, engine, native_tools=False),
                timeout=30.0,
            )
            if not isinstance(text, str):
                return ""
            text = text.strip()
            # The model may still return a JSON action / code-fence wrapper
            # despite the "text ONLY" instruction. Delivering raw JSON to the
            # user is worse than falling back to the last real reply.
            if text.startswith("{") or text.startswith("```"):
                log_debug(
                    "[agent_core] _compose_pause_message got a non-prose "
                    "response; falling back to the last reply"
                )
                return ""
            return text
        except Exception as exc:
            log_debug(f"[agent_core] _compose_pause_message failed: {exc}")
            return ""

    @staticmethod
    def _build_agent_prompt(
        goal: str,
        observations: list[Dict[str, Any]],
        engine: str | None,
        context: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Assemble the per-iteration prompt including observation history."""
        from core.tool_registry import tool_registry

        history_lines: list[str] = []
        for obs in observations:
            role = obs.get("role", "system")
            content = obs.get("content", "")
            if isinstance(content, list):
                # tool_results list
                for item in content:
                    status = "OK" if item.get("ok") else f"ERROR: {item.get('error')}"
                    history_lines.append(
                        f"[tool:{item.get('tool')}] {status}\n{item.get('result', '')}"
                    )
            else:
                history_lines.append(f"[{role}] {content}")

        observation_block = "\n".join(history_lines)

        # Expose the originating conversation to the model. Message-delivery
        # actions (e.g. message_telegram_bot / audio_telegram_bot) declare
        # ``interface_path`` as a REQUIRED field, but the agentic prompt only
        # carried GOAL + TOOLS + OBSERVATIONS — the model never saw the source
        # interface_path and therefore either omitted it (payload validation
        # failed with "interface_path or chat_name is required") or invented a
        # wrong one, so the message and the diary entry silently failed to land.
        # Surfacing it here lets the model reply in the same conversation and
        # gives diary actions the context they need. No keyword/language logic —
        # purely structural, driven by the interface_path already in context.
        source_block = ""
        if isinstance(context, dict):
            source_interface_path = context.get("interface_path")
            source_interface = context.get("interface")
            if source_interface_path:
                lines = [
                    "SOURCE CONVERSATION (use this to talk back to the user):",
                    f"- interface_path: {source_interface_path}",
                ]
                if source_interface:
                    lines.append(f"- interface: {source_interface}")
                lines.append(
                    "When you call a message/delivery action (e.g. "
                    "message_telegram_bot), you MUST set its 'interface_path' "
                    "field to EXACTLY the interface_path above so the reply "
                    "reaches this same conversation."
                )
                source_block = "\n".join(lines)

        # Inbound attachments materialized to sandbox paths (documents/audio
        # uploaded by the user in this conversation). The agent tools
        # (agent_read_file / stt_transcribe) can only read real files, so the
        # model must be told where the uploaded file actually lives instead of
        # guessing a bare filename (Langfuse 11feca6f: "File not found:
        # Untitled document.pdf"). Structural context, never keyword logic.
        attachment_block = ""
        if isinstance(context, dict):
            attachment_paths = context.get("attachment_paths")
            if isinstance(attachment_paths, list) and attachment_paths:
                lines = [
                    "USER ATTACHMENTS (files uploaded in this conversation; "
                    "read them with agent_read_file if the task needs their "
                    "contents):"
                ]
                for apath in attachment_paths:
                    lines.append(f"- {apath}")
                attachment_block = "\n".join(lines)

        # Drones cannot spawn Drones: hide the spawn_drone tool from a Drone's
        # available tool list (single-level delegation). The handler enforces the
        # same rule defensively, this just keeps the model from ever proposing it.
        is_drone = bool(
            isinstance(context, dict)
            and isinstance(context.get("drone"), dict)
            and context["drone"].get("is_drone")
        )

        # Task-scoped Drone allow-list: when the spawning caller restricted this
        # Drone to a specific tool set (e.g. the vessel goal-expander/planner is
        # limited to lookup_knowledge + update_goal), hide every other tool from
        # the prompt so the model never even sees — let alone proposes — an
        # out-of-scope action such as an in-world ``vessel_<world>_say``.
        allowed_tools = _context_allowed_tools(context)

        tool_lines: list[str] = []
        for tool in tool_registry.all_tools():
            if tool.name in _system_only_action_names():
                continue
            if is_drone and tool.name == "spawn_drone":
                continue
            if allowed_tools is not None and tool.name not in allowed_tools:
                continue
            params = []
            for p in tool.parameters:
                ptype = p.type or "string"
                req = "required" if p.required else "optional"
                params.append(f"{p.name}:{ptype}({req})")
            params_block = ", ".join(params) if params else "no-params"
            tool_lines.append(
                f"- {tool.name} | source={tool.source} | security={tool.security_level} | params=[{params_block}]"
            )
        # The completion sentinel is always available and is how the model ends
        # the turn. It is intercepted by the loop, not executed as a real action.
        tool_lines.append(
            f"- {_COMPLETION_TOOL} | source=agent | security=safe | "
            "params=[summary:string(required)]"
        )
        tools_block = "\n".join(tool_lines) if tool_lines else "- (no tools registered)"

        system_text = (
            "You are Synth operating in agentic mode. Achieve the goal using "
            "the available tools. When you need a tool, respond ONLY with the "
            "tool-call JSON actions. Use only tool names from the AVAILABLE TOOLS "
            "block.\n"
            "TOOL-CALL FORMAT: when you need a tool, emit EXACTLY this JSON "
            "shape — no markdown, no XML, no prose, nothing else:\n"
            '{"tool_calls": [{"function": {"name": "agent_read_file", '
            '"arguments": {"path": "C:/x.pdf"}}}]}\n'
            "You may list several entries inside one tool_calls array.\n"
            "VOICE/AUDIO REPLIES: to answer with your spoken voice, call "
            "send_message with send_as_voice=true and put the full spoken reply "
            "in 'text'.\n"
            "Work like a careful engineer: break the goal into steps and keep "
            "calling tools to gather information, verify assumptions and make "
            "progress until the goal is genuinely achieved. Do NOT stop and give "
            "a final answer while the goal is only partially done or still "
            "unverified — inspect results, and take the next tool action if more "
            "work remains.\n"
            "ENDING THE TURN: neither plain text NOR a message action ends the "
            "task on its own — the loop keeps going. Announcing what you are "
            'about to do ("I\'ll now check...") is never a completion, even if '
            f"phrased as a message. The ONLY way to finish is to call the "
            f"{_COMPLETION_TOOL} tool with a short summary of what you "
            "accomplished, once the goal is genuinely done (or you can clearly "
            "explain, based on tool results, why it cannot be). Send message "
            "actions to talk to the user while you work, but send at most ONE "
            "brief interim update per task — repeating status updates on later "
            "iterations is spam and will be suppressed; the complete answer "
            f"belongs in the {_COMPLETION_TOOL} summary. Keep emitting the "
            f"next tool call until you call {_COMPLETION_TOOL}.\n"
            "USER-FACING VOICE: any text you send to the user (interim update "
            "or completion summary) must already be written in your own voice "
            "and the conversation's language — natural and personal, never a "
            "dry technical log. Keep the facts exact; keep the tone yours.\n"
            "Each tool observation is already in PRIOR OBSERVATIONS — build on it "
            "rather than repeating an identical call.\n"
            "INTERNAL NOTES vs USER MESSAGES: keep your private reasoning "
            "separate from what you say to the user. A message/delivery action "
            "(e.g. message_telegram_bot) is a real message the user WILL read — "
            "it must contain ONLY what you want to tell them, in natural "
            "conversational language, never your step-by-step plan, tool logs, "
            "raw tool output, status chatter, or thinking-out-loud. If you want "
            "to jot down a plan, track progress, or record an intermediate "
            "thought for yourself, use the note_to_self tool (it is private and "
            "never shown to the user) — do NOT send it as a user message.\n"
            "Diary discipline: this is a single agentic task, not many separate "
            "moments. Do NOT write a diary entry on every iteration. At most, "
            "record one diary entry when you begin the task and one when it is "
            "finished. During the intermediate working iterations do NOT call any "
            "diary tool (create_personal_diary_entry / update_diary_entry) — just "
            "use the tools needed to make progress. If the prior observations "
            "already show a diary entry was written for this task, do not write "
            "another one until the task is complete."
        )
        source_prefix = f"{source_block}\n\n" if source_block else ""
        attachment_prefix = f"{attachment_block}\n\n" if attachment_block else ""
        prompt = {
            "input": {
                "payload": {
                    "text": (
                        f"{source_prefix}"
                        f"{attachment_prefix}"
                        f"GOAL: {goal}\n\n"
                        f"AVAILABLE TOOLS:\n{tools_block}\n\n"
                        f"PRIOR OBSERVATIONS:\n{observation_block}\n"
                        if observation_block
                        else (
                            f"{source_prefix}"
                            f"{attachment_prefix}"
                            f"GOAL: {goal}\n\nAVAILABLE TOOLS:\n{tools_block}\n"
                        )
                    ),
                }
            },
            "system_message": {
                "type": "agent_turn",
                "engine": engine,
                "goal": goal,
            },
            "agent_mode": True,
            "observation_history": observations,
        }
        if context:
            prompt["context"] = context
        # Attach the system instruction so engines that honor it will use it.
        prompt["input"]["payload"]["system"] = system_text
        return prompt

    @staticmethod
    def _extract_tool_calls(parsed: Any) -> list[Dict[str, Any]]:
        """Normalize parsed LLM JSON into a list of tool-call dicts."""

        def _normalize_args(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    parsed_value = json.loads(value)
                    return parsed_value if isinstance(parsed_value, dict) else {}
                except Exception:
                    return {}
            return {}

        if not isinstance(parsed, dict):
            return []
        # Standard SyntH shape: {"actions": [{"type":..., "payload":...}]}
        actions = parsed.get("actions")
        if isinstance(actions, list):
            calls: list[Dict[str, Any]] = []
            for a in actions:
                if isinstance(a, dict) and (
                    a.get("type") or a.get("name") or a.get("tool")
                ):
                    calls.append(
                        {
                            "name": str(
                                a.get("type") or a.get("name") or a.get("tool")
                            ),
                            "arguments": _normalize_args(
                                a.get(
                                    "payload",
                                    a.get(
                                        "params", a.get("arguments", a.get("args", {}))
                                    ),
                                )
                            ),
                        }
                    )
            return calls

        # OpenAI-ish shape: {"tool_calls": [{"function": {"name":..., "arguments": ...}}]}
        tool_calls = parsed.get("tool_calls")
        if isinstance(tool_calls, list):
            calls: list[Dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict) and fn.get("name"):
                    calls.append(
                        {
                            "name": str(fn.get("name")),
                            "arguments": _normalize_args(fn.get("arguments", {})),
                        }
                    )
            if calls:
                return calls

        # Generic shape: {"calls": [{"name":..., "arguments":...}]}
        generic_calls = parsed.get("calls")
        if isinstance(generic_calls, list):
            calls: list[Dict[str, Any]] = []
            for c in generic_calls:
                if isinstance(c, dict) and (
                    c.get("name") or c.get("type") or c.get("tool")
                ):
                    calls.append(
                        {
                            "name": str(
                                c.get("name") or c.get("type") or c.get("tool")
                            ),
                            "arguments": _normalize_args(
                                c.get(
                                    "arguments",
                                    c.get(
                                        "params", c.get("payload", c.get("args", {}))
                                    ),
                                )
                            ),
                        }
                    )
            if calls:
                return calls

        # Single action object. The tool/action name may be carried under any of
        # ``type`` / ``name`` / ``tool`` — different engines pick different keys
        # (e.g. logfare-claude emits ``{"tool": "attempt_completion", "params":
        # {...}}``). Likewise, the arguments may live under ``arguments`` /
        # ``payload`` / ``args`` / ``params``. Normalize all of them the same way
        # so the completion sentinel and single tool calls are never silently
        # dropped (a missing ``params`` key previously discarded every
        # logfare-claude tool call, including attempt_completion and outbound
        # messages).
        name_key = parsed.get("type") or parsed.get("name") or parsed.get("tool")
        if name_key and (
            "arguments" in parsed
            or "payload" in parsed
            or "args" in parsed
            or "params" in parsed
        ):
            return [
                {
                    "name": str(name_key),
                    "arguments": _normalize_args(
                        parsed.get(
                            "arguments",
                            parsed.get(
                                "payload",
                                parsed.get("params", parsed.get("args", {})),
                            ),
                        )
                    ),
                }
            ]
        if parsed.get("type"):
            return [
                {
                    "name": parsed["type"],
                    "arguments": _normalize_args(parsed.get("payload", {})),
                }
            ]
        return []

    @staticmethod
    def _extract_tool_calls_from_text(text: str) -> list[Dict[str, Any]]:
        """Recover tool calls from a text-protocol response.

        Some engines (e.g. flash-sized models served through openai_compat
        bridges) ignore the "respond ONLY with JSON" instruction and emit a
        text protocol instead. Several such protocols are recognized::

            I'll check the file now.

            Tool Call: agent_read_file
            {"path": "core/main.py"}

            <function>agent_read_file<path>core/main.py</path></function>

            <tool_calls><invoke name="agent_read_file">
              <parameter name="path">core/main.py</parameter>
            </invoke></tool_calls>

            agent_read_file(path="core/main.py")

            `attempt_completion({"summary": "done"})`

        ``extract_json_from_text`` then recovers only the inner payload JSON
        (which carries no tool name), so :meth:`_extract_tool_calls` returns
        nothing and the loop nags the model with "you responded with text but
        no tool calls" for the whole budget without executing anything
        (Langfuse 07ba4a27 / d59c10fc / fdf08aef / 5ea2ff8c / 85519e59). This
        parser recovers the name + arguments pairs so the loop executes the
        model's actual intent. A weak model may re-emit the SAME call in
        several formats within one response; identical (name, arguments) pairs
        are deduplicated so side effects never repeat. Purely structural (a
        declared call protocol), never keyword intent detection. Returns an
        empty list when nothing parseable is present.
        """
        if not isinstance(text, str) or not text.strip():
            return []
        calls: list[Dict[str, Any]] = []
        header = re.compile(
            r"tool[\s_]*call\s*[:：]\s*([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        for match in header.finditer(text):
            name = match.group(1).strip()
            # The header may be wrapped in markdown bold (**Tool Call: x**)
            # and the JSON may sit inside a ```json code fence — both appear
            # in real engine output (Langfuse c1e66673). Consume whitespace,
            # asterisks, then an optional fence opener before the object.
            cursor = match.end()
            while cursor < len(text) and text[cursor] in " \t\r\n*":
                cursor += 1
            if text.startswith("```", cursor):
                cursor += 3
                while cursor < len(text) and text[cursor] in " \t\r\n":
                    cursor += 1
                if text[cursor : cursor + 4].lower() == "json":
                    cursor += 4
                while cursor < len(text) and text[cursor] in " \t\r\n":
                    cursor += 1
            if cursor >= len(text) or text[cursor] != "{":
                continue
            end = _match_balanced_json_object(text, cursor)
            if end is None:
                continue
            try:
                args = json.loads(text[cursor : end + 1])
            except Exception:
                continue
            if not isinstance(args, dict):
                continue
            calls.append({"name": name, "arguments": args})

        # DeepSeek-native XML blocks (Langfuse ff1bbae0). Two variants:
        #   <function> name-line <arg>value</arg> ... </function>
        #   <function> <function_name>name</function_name>
        #              <parameters>{...json...}</parameters> </function>
        for block in re.finditer(
            r"<function>(.*?)</function>", text, re.DOTALL | re.IGNORECASE
        ):
            inner = block.group(1)
            name_match = re.search(
                r"<function_name>\s*([A-Za-z_][A-Za-z0-9_]*)\s*</function_name>",
                inner,
                re.DOTALL | re.IGNORECASE,
            )
            if name_match:
                name = name_match.group(1)
            else:
                stripped_inner = inner.strip()
                if not stripped_inner:
                    continue
                first_line = stripped_inner.splitlines()[0].strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", first_line):
                    continue
                name = first_line
            args: Dict[str, Any] = {}
            params_match = re.search(
                r"<parameters>\s*(.*?)\s*</parameters>",
                inner,
                re.DOTALL | re.IGNORECASE,
            )
            if params_match:
                try:
                    parsed_params = json.loads(params_match.group(1).strip())
                    if isinstance(parsed_params, dict):
                        args = parsed_params
                except Exception:
                    pass
            if not args:
                for arg_match in re.finditer(
                    r"<([A-Za-z_][A-Za-z0-9_]*)>\s*(.*?)\s*</\1>",
                    inner,
                    re.DOTALL,
                ):
                    key = arg_match.group(1)
                    if key in ("function_name", "parameters"):
                        continue
                    value = arg_match.group(2).strip()
                    try:
                        args[key] = json.loads(value)
                    except Exception:
                        args[key] = value
            calls.append({"name": name, "arguments": args})

        # Bare Python-style calls (Langfuse fdf08aef): engines that emit
        #   agent_read_file(path="D:\\app\\a.pdf", max_chars="5000")
        # directly, with no wrapper/prefix. Also covers backtick-wrapped
        # JSON-argument calls like `attempt_completion({"summary": "..."})`.
        # A call must carry at least one argument so prose is never mistaken.
        for m in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
            name = m.group(1)
            open_idx = m.end() - 1
            end = _match_balanced_call_paren(text, open_idx)
            if end is None:
                continue
            args_text = text[m.end() : end].strip()
            if not args_text:
                continue
            args: Dict[str, Any] = {}
            if args_text.startswith("{"):
                # JSON-object argument form: name({"path": "x"})
                try:
                    parsed_args = json.loads(args_text)
                    if isinstance(parsed_args, dict):
                        args = parsed_args
                except Exception:
                    pass
            if not args:
                # key="value" pairs (double-quoted values, escaped quotes ok)
                for am in re.finditer(
                    r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"',
                    args_text,
                ):
                    key = am.group(1)
                    value = am.group(2)
                    value = value.replace('\\"', '"').replace("\\\\", "\\")
                    try:
                        args[key] = json.loads(value)
                    except Exception:
                        args[key] = value
            if not args:
                continue
            calls.append({"name": name, "arguments": args})

        # Claude-style XML blocks (Langfuse 5ea2ff8c):
        #   <tool_calls>
        #   <invoke name="agent_run_shell">
        #   <parameter name="command">python3 -c "..."</parameter>
        #   </invoke>
        #   </tool_calls>
        for invoke in re.finditer(
            r"<invoke\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</invoke>",
            text,
            re.DOTALL | re.IGNORECASE,
        ):
            name = invoke.group(1).strip()
            inner = invoke.group(2)
            args: Dict[str, Any] = {}
            for pm in re.finditer(
                r"<parameter\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</parameter>",
                inner,
                re.DOTALL | re.IGNORECASE,
            ):
                key = pm.group(1)
                value = pm.group(2).strip()
                try:
                    args[key] = json.loads(value)
                except Exception:
                    args[key] = value
            calls.append({"name": name, "arguments": args})

        # A weak model may emit the SAME call in several formats within one
        # response (e.g. "Tool Call: x" + <function>x</function> + x(args)).
        # Executing duplicates would repeat side effects — keep the first only
        # (the "3 duplicate tool calls per step" symptom).
        seen: set[tuple] = set()
        deduped: list[Dict[str, Any]] = []
        for call in calls:
            key = (
                str(call.get("name")),
                json.dumps(call.get("arguments") or {}, sort_keys=True, default=str),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(call)
        return deduped


# Expose a convenient singleton manager
_agent_loop_manager = AgentLoopManager()


def get_agent_loop_manager() -> AgentLoopManager:
    return _agent_loop_manager
