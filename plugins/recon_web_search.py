from __future__ import annotations

import json
from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info


display_name = "Recon Web Search"

# UI-exposed switch to enable/disable this recon plugin
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_WEB_SEARCH_RECON_ENABLED",
        label="Enable Recon Web Search",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Recon Web Search plugin (perform web searches based on LLM-generated queries).",
        scope="agent",
        component="agent",
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_WEB_SEARCH_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Web Search",
        description="Enable the Recon Web Search plugin (perform web searches based on LLM-generated queries).",
        group="agent",
        component="agent",
    )


class ReconWebSearchPlugin:
    display_name = display_name
    recon_priority = 6

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "web_search"

    def get_recon_instruction(self) -> str:
        return (
            "Determine whether the user request needs a web search for current or "
            "verifiable information. Bias toward searching whenever the answer "
            "depends on facts that can change over time or that you cannot state "
            "with confidence from static knowledge (for example anything the user "
            "frames as up-to-date, latest, current, today's, or recent). When in "
            "doubt, prefer generating a query rather than returning an empty list. "
            "If a search is warranted, generate 1-3 specific, self-contained search "
            "queries. Return STRICTLY this JSON object and nothing else: "
            '{"web_search": ["query1", "query2"]}. '
            "Only when the request clearly needs no external information "
            "(pure general knowledge, opinions, hypotheticals, or internal system "
            'questions) return {"web_search": []}.'
        )

    def _extract_queries_from_text(self, raw_text: str) -> list[str]:
        """Attempt to extract web_search queries from raw LLM text.

        The Recon LLM may produce valid JSON that the central parser failed to
        extract (e.g. due to truncation or formatting quirks).  This helper
        tries loose extraction as a fallback.
        """
        if not raw_text or not raw_text.strip():
            return []

        text = raw_text.strip()

        # Try full JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                raw = parsed.get("web_search")
                if isinstance(raw, list):
                    return [str(q).strip() for q in raw if str(q).strip()][:3]
                if isinstance(raw, dict):
                    nested = raw.get("web_search")
                    if isinstance(nested, list):
                        return [str(q).strip() for q in nested if str(q).strip()][:3]
        except json.JSONDecodeError:
            pass

        # Try to locate a JSON block within the text using regex
        import re

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if isinstance(parsed, dict):
                    raw = parsed.get("web_search")
                    if isinstance(raw, list):
                        return [str(q).strip() for q in raw if str(q).strip()][:3]
                    if isinstance(raw, dict):
                        nested = raw.get("web_search")
                        if isinstance(nested, list):
                            return [str(q).strip() for q in nested if str(q).strip()][
                                :3
                            ]
            except json.JSONDecodeError:
                pass

        return []

    async def parse_recon_response(
        self,
        data,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        """Extract search queries from the Recon LLM response and execute them.

        Two-phase extraction:
        1. Use the pre-parsed ``data`` dict if non-None.
        2. Fall back to ``_raw_llm_text`` for self-parsing when the central
           JSON parser could not extract structured data.
        """
        enabled = bool(
            config_registry.get_value(
                "RECON_WEB_SEARCH_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        # Guard: the second turn that delivers already-completed search results
        # ("web_search_result" beat) must NEVER trigger another search. Its
        # prompt still talks about a search, so the recon LLM would generate
        # fresh queries and this plugin would spawn a new background task,
        # producing an infinite loop of "searching..." announcements while the
        # real findings (already in the prompt) are never reported. Detect the
        # beat via structured context, not text — language-agnostic.
        if isinstance(context_memory, dict) and (
            context_memory.get("beat_type") == "web_search_result"
        ):
            log_debug(
                "[recon_web_search] Skipping: this is a web_search_result "
                "delivery turn; results are already in the prompt."
            )
            return []

        queries: list[str] = []

        # Phase 1: Use pre-parsed data from recon.py
        if data is not None:
            if isinstance(data, dict):
                raw_queries = data.get("web_search")
                if isinstance(raw_queries, list):
                    queries = [str(q).strip() for q in raw_queries if str(q).strip()][
                        :3
                    ]
                elif isinstance(raw_queries, dict):
                    nested = raw_queries.get("web_search")
                    if isinstance(nested, list):
                        queries = [str(q).strip() for q in nested if str(q).strip()][:3]
            elif isinstance(data, list):
                queries = [str(q).strip() for q in data if str(q).strip()][:3]

        # Phase 2: Self-parse from raw LLM text when central parser failed
        if not queries and _raw_llm_text:
            queries = self._extract_queries_from_text(_raw_llm_text)
            if queries:
                log_info(
                    f"[recon_web_search] Extracted {len(queries)} queries from raw "
                    f"LLM text (central JSON parser missed these)"
                )

        if not queries:
            log_debug("[recon_web_search] No search queries generated by LLM")
            # No background search was triggered for this turn. Emit a guard
            # instruction so the persona model does not falsely announce that a
            # search is underway (a hallucinated "searching..." message would
            # promise results that will never arrive). Behavioural instruction
            # only — no keyword/regex matching, language-agnostic.
            return [
                {
                    "type": "instruction",
                    "content": (
                        "No background web search has been started for this turn. "
                        "Answer the user directly using what you already know. "
                        "Do NOT claim that you are searching the web, sending "
                        "agents/drones/modules to gather data, or that results "
                        "will follow in a later message — no search is running."
                    ),
                    "source": "recon_web_search",
                    "priority": int(self.recon_priority),
                }
            ]

        # Recon is a PURE TRIGGER: it never runs the searches inline (that would
        # block the whole recon dispatch). It fires a decoupled background task
        # and immediately returns an instruction so Synth can tell the user a
        # search is underway and the detailed results will follow in a second
        # turn.
        #
        # Resolve the originating interface_path robustly. The `message` object
        # does not always carry a populated `interface_path` at recon time, so
        # fall back to the message dict form and finally to the context dict —
        # otherwise the second-turn delivery lands on the wrong path (or the
        # generic `web_search/-1` fallback) and Synth replies in the wrong chat.
        interface_path = self._resolve_interface_path(
            message=message, context_memory=context_memory
        )
        search_context = self._build_search_context(
            message=message, text=text, context_memory=context_memory
        )

        try:
            from plugins.web_search.search_orchestrator import get_search_orchestrator

            orchestrator = get_search_orchestrator()
            task_id = await orchestrator.submit(
                interface_path=interface_path,
                queries=queries,
                search_context=search_context,
                context_memory=context_memory
                if isinstance(context_memory, dict)
                else None,
            )
            log_info(
                f"[recon_web_search] Triggered background search task {task_id} "
                f"({len(queries)} queries) for path={interface_path}"
            )
        except Exception as e:
            log_error(f"[recon_web_search] Failed to trigger background search: {e}")
            return []

        queries_str = "; ".join(queries)
        instruction = (
            "You are starting a web search in the background for the following "
            f"queries: {queries_str}. Tell the user, in their language and your "
            "own voice, that you are searching and will report the detailed "
            "results in a follow-up message. Answer the rest of their message "
            "normally now; do NOT fabricate search results — the real findings "
            "will arrive later as a separate turn."
        )

        return [
            {
                "type": "instruction",
                "content": instruction,
                "source": "recon_web_search",
                "priority": int(self.recon_priority),
            }
        ]

    def _resolve_interface_path(
        self,
        *,
        message=None,
        context_memory=None,
    ) -> str | None:
        """Resolve the originating interface_path from the available inputs.

        The recon `message` object is not guaranteed to expose a populated
        ``interface_path`` attribute, so try, in order: the message attribute,
        the message dict form, and finally the context dict (which the message
        queue always populates with ``interface_path`` for incoming turns).
        """
        candidate = getattr(message, "interface_path", None)
        if not candidate and isinstance(message, dict):
            candidate = message.get("interface_path")
        if not candidate and isinstance(context_memory, dict):
            candidate = context_memory.get("interface_path")
        if candidate:
            candidate = str(candidate).strip()
        return candidate or None

    def _build_search_context(
        self,
        *,
        message=None,
        text: str | None = None,
        context_memory=None,
    ) -> str:
        """Snapshot the intent behind the search for later synthesis."""
        parts: list[str] = []
        msg_text = getattr(message, "text", None) if message is not None else None
        if msg_text:
            parts.append(str(msg_text).strip())
        if text and text.strip() and text.strip() not in parts:
            parts.append(text.strip())
        return "\n".join(parts).strip()

    async def get_recon_contributions(
        self,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
    ) -> list[dict]:
        """Legacy interface — delegate to parse_recon_response."""
        return await self.parse_recon_response(
            data=None,
            message=message,
            context_memory=context_memory,
            text=text,
            tags=tags,
            keywords=keywords,
            max_results=max_results,
            _raw_llm_text=None,
        )


# Auto-register this plugin
PLUGIN_CLASS = ReconWebSearchPlugin
