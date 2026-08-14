from __future__ import annotations

from typing import Any, List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info


display_name = "Recon Agent Intent"

# UI-exposed switch for enabling agent-intent recon contributions
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_AGENT_INTENT_RECON_ENABLED",
        label="Enable Recon Agent Intent",
        default=True,
        value_type=bool,
        ui_type="bool",
        description=(
            "Enable the Recon Agent Intent plugin. During the preflight Recon "
            "call it lets the model decide whether the incoming request needs "
            "the agentic (tool-using) lane, and injects an instruction nudging "
            "the main model to emit agentic tool calls so the deterministic "
            "router escalates the turn to the Agent lane."
        ),
        scope="recon",
        component="recon_agent_intent",
        advanced=True,
        hidden=True,
    )
except Exception:
    config_registry.get_var(
        "RECON_AGENT_INTENT_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Agent Intent",
        description="Enable the Recon Agent Intent plugin.",
        group="recon",
        component="recon_agent_intent",
        advanced=True,
        hidden=True,
    )


class ReconAgentIntentPlugin:
    """Recon contributor that detects — semantically, via the shared Recon LLM
    call — whether the user's request requires the agentic (tool-using) lane.

    The SyntH router (:mod:`core.agent_router`) is intentionally deterministic:
    it picks the FAST or AGENT lane purely from the *shape* of the actions the
    main model already emitted. A single ``message`` action always routes to
    FAST, even when the user clearly asked for multi-step, tool-driven work.

    This plugin closes that gap upstream. It rides the single combined Recon
    prompt (no extra LLM call) and asks the model to judge intent. When agentic
    work is warranted it returns an ``instruction`` contribution that the prompt
    engine prepends to the main prompt, steering the main model to emit an
    agentic tool call — which the deterministic router then escalates to the
    Agent lane.

    No keyword/regex matching is used: the judgement is made by the Recon model
    itself, keeping the behaviour language-agnostic.
    """

    display_name = display_name
    recon_priority = 9

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "agent_intent"

    def get_recon_instruction(self, *, message=None, context_memory=None) -> str:
        instruction = (
            "Judge whether fulfilling the user's request requires acting as an "
            "agent that uses tools and performs work, as opposed to simply "
            "replying with knowledge or conversation. DEFAULT TO "
            "agent_needed=false: most messages are conversational and a single "
            "direct reply fully satisfies them. Only set agent_needed=true when "
            "the request CLEARLY and UNAMBIGUOUSLY needs tool-driven work. "
            "Decide by answering these questions about the request: (1) is it "
            "articulated enough that it clearly must be carried out in several "
            "concrete steps? (2) does it explicitly require reading or modifying "
            "files, inspecting the codebase, or running commands? (3) does it "
            "require producing a substantial deliverable or an operational "
            "result that cannot be given as a direct reply? Set agent_needed=true "
            "only if the answer to one of these is a clear yes. When in doubt, "
            "prefer agent_needed=false. It is NOT needed for greetings, small "
            "talk, opinions, questions answerable from general knowledge, or any "
            "request a single direct reply fully satisfies. "
            "Messages that merely TALK ABOUT the agent, tools, or the system "
            "are NOT agentic requests: complaining that something is broken, "
            "asking how the agent works, joking or roleplaying about it, or "
            "reporting its state is ordinary conversation and must be "
            "agent_needed=false. A request is agentic only when the human is "
            "actually asking you TO DO concrete work with your tools right "
            "now, and a single reply cannot satisfy it. "
            "Base the judgement on the meaning of the request in any language, "
            "never on specific words. Also provide a very short human-readable "
            "title (max ~6 words) that names the task, written in the same "
            "language as the user's request and suitable to display as the task "
            "name; when no agentic work is needed the title may be empty. "
            'Return as an object: {"agent_needed": true|false, "reason": '
            '"short justification", "task_title": "short task name"}.'
        )
        if isinstance(context_memory, dict) and context_memory.get("attachment_paths"):
            instruction += (
                " The user attached file(s) to this message; the attached "
                "content is already provided to the main model in this turn. "
                "Reading, quoting, or summarising an attached file is ordinary "
                "conversation and does NOT require tools. Only escalate when "
                "the request needs tool work beyond the attached file itself "
                "(modifying files, running commands, searching the codebase)."
            )
        return instruction

    def _enabled(self) -> bool:
        try:
            return bool(
                config_registry.get_value(
                    "RECON_AGENT_INTENT_RECON_ENABLED", True, value_type=bool
                )
            )
        except Exception:
            return True

    def _agentic_routing_on(self) -> bool:
        # No point nudging toward tool calls when the router would send the turn
        # to FAST regardless — the agentic lane is gated by this flag.
        try:
            return bool(
                config_registry.get_var(
                    "AGENTIC_ROUTING_ENABLED", False, value_type=bool
                )
            )
        except Exception:
            return False

    def _agent_toggle_on(self) -> bool:
        # The user-facing agent on/off toggle. When the agent is OFF the router
        # forces the Fast Lane regardless, so the recon must not even spend a
        # decision here: it no-ops and the turn stays on the classic path.
        try:
            return bool(config_registry.get_var("AGENT_ENABLED", True, value_type=bool))
        except Exception:
            return True

    async def parse_recon_response(
        self,
        data: Any,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        if not text or not isinstance(text, str) or not text.strip():
            return []
        if not self._enabled():
            return []
        if not self._agentic_routing_on():
            log_debug(
                "[recon_agent_intent] AGENTIC_ROUTING_ENABLED off — skipping hint"
            )
            return []
        if not self._agent_toggle_on():
            log_debug("[recon_agent_intent] AGENT_ENABLED off — skipping hint")
            return []
        if not isinstance(data, dict):
            return []

        # New contract field is ``agent_needed``; keep reading the legacy
        # ``needs_agent`` for backward compatibility during the transition.
        agent_needed = bool(data.get("agent_needed", data.get("needs_agent")))
        if not agent_needed:
            return []

        # Set the authoritative routing decision on the shared context. The
        # recon ``context_memory`` dict is inherited by the router ctx after
        # this hook runs (``llm_context.update(context_memory)`` in
        # core/plugin_instance.py), so a top-level key set here reaches
        # ``core.agent_router.classify``'s ``context`` argument and
        # deterministically forces the Agent lane — instead of relying on the
        # main model emitting a tool-shaped action. This is what keeps a plain
        # greeting on the Fast Lane and only escalates genuine agentic work.
        if isinstance(context_memory, dict):
            context_memory["agent_needed"] = True

        # Propagate the model-provided task title onto the shared context so the
        # Agent lane can name the persisted task in the WebUI, via the same
        # inheritance mechanism.
        task_title = str(data.get("task_title") or "").strip()
        if task_title and isinstance(context_memory, dict):
            # Keep it compact — the WebUI displays it as the task name.
            context_memory["agent_task_title"] = task_title[:120]
            log_debug(f"[recon_agent_intent] Task title captured: {task_title[:120]!r}")

        reason = str(data.get("reason") or "").strip()
        instruction = (
            "This request requires agentic work: to fulfil it you must use your "
            "agentic tools rather than answering from memory alone. Emit an "
            "agentic tool action (for example agent_list_files or "
            "agent_read_file to inspect the codebase, spawn_drone to delegate a "
            "self-contained sub-task, or an available mcp_* tool) as your first "
            "action so the task runs in the Agent lane. Do not reply with only a "
            "plain message when tool use is expected."
        )
        if reason:
            instruction += f" Rationale: {reason}"

        log_info("[recon_agent_intent] Emitting agentic-intent instruction")
        return [
            {
                "type": "instruction",
                "content": instruction,
                "source": "agent_intent",
                "priority": int(self.recon_priority),
            }
        ]


# Auto-register this plugin
PLUGIN_CLASS = ReconAgentIntentPlugin
