from __future__ import annotations

import json
from typing import List, cast

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
            "Decide whether the user request needs external web information. There "
            "are TWO independent tools you can trigger:\n"
            "1. queries: internet searches (SearXNG/Tavily) for current or "
            "verifiable facts. Bias toward searching whenever the answer depends "
            "on facts that can change over time or that you cannot state with "
            "confidence from static knowledge. Generate 1-3 specific, "
            "self-contained search queries.\n"
            "2. check_website: explicit links the user asked you to open/visit/"
            "read directly. Put here ONLY full http(s) URLs the user provided or "
            "clearly wants you to fetch directly. These are visited and scraped "
            "AS-IS; they do NOT replace or count against the search queries.\n"
            "Either list may be empty. Use check_website WITHOUT any queries when "
            "the user only wants specific links opened and no broader search is "
            "needed. The value for this key MUST be an object with exactly these "
            "two lists and nothing else: "
            '{"queries": ["query1"], "check_website": ["https://example.com"]}. '
            "When the request clearly needs no external information and no links "
            'to open, use {"queries": [], "check_website": []}.'
        )

    @staticmethod
    def _split_payload(raw: object) -> tuple[list[str], list[str]]:
        """Normalize a ``web_search`` value into ``(queries, urls)``.

        Accepts every historical and current shape:
        - new object form: ``{"queries": [...], "check_website": [...]}``
        - legacy bare list: ``["query1", "query2"]`` (all treated as queries)
        - nested legacy: ``{"web_search": [...]}``
        URLs are validated to be http(s); anything that is not a valid link is
        dropped from the url list. Query/URL classification is decided by the
        recon LLM's structure, never by keyword/regex matching of content.
        """
        queries: list[str] = []
        urls: list[str] = []

        if isinstance(raw, dict):
            raw_dict = cast("dict[object, object]", raw)
            # Defensive unwrap: some recon models echo the wrapper key and emit
            # {"web_search": {"queries": [...], "check_website": [...]}} as the
            # value. Peel one redundant layer so both shapes are accepted.
            nested = raw_dict.get("web_search")
            if (
                isinstance(nested, dict)
                and ("queries" in nested or "check_website" in nested)
                and "queries" not in raw_dict
                and "check_website" not in raw_dict
            ):
                raw_dict = cast("dict[object, object]", nested)
            q: object = raw_dict.get("queries")
            if not isinstance(q, list):
                # nested legacy {"web_search": [...]}
                q = raw_dict.get("web_search")
            if isinstance(q, list):
                queries = [str(x).strip() for x in q if str(x).strip()][:3]

            cw: object = raw_dict.get("check_website")
            if isinstance(cw, list):
                urls = [
                    str(x).strip()
                    for x in cw
                    if str(x).strip().lower().startswith(("http://", "https://"))
                ][:3]
        elif isinstance(raw, list):
            queries = [str(x).strip() for x in raw if str(x).strip()][:3]

        return queries, urls

    def _extract_payload_from_text(self, raw_text: str) -> tuple[list[str], list[str]]:
        """Loosely extract ``(queries, urls)`` from raw LLM text.

        Fallback when the central JSON parser could not extract structured data
        (truncation, formatting quirks). Handles both the new object form and
        the legacy bare-list form.
        """
        if not raw_text or not raw_text.strip():
            return [], []

        text = raw_text.strip()

        def _from_parsed(parsed: object) -> tuple[list[str], list[str]]:
            if isinstance(parsed, dict):
                parsed_dict = cast("dict[object, object]", parsed)
                return self._split_payload(parsed_dict.get("web_search"))
            if isinstance(parsed, list):
                return self._split_payload(parsed)
            return [], []

        # Try full JSON parse
        try:
            q, u = _from_parsed(json.loads(text))
            if q or u:
                return q, u
        except json.JSONDecodeError:
            pass

        # Try to locate a JSON block within the text using regex
        import re

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                q, u = _from_parsed(json.loads(json_match.group()))
                if q or u:
                    return q, u
            except json.JSONDecodeError:
                pass

        return [], []

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
        urls: list[str] = []

        # Phase 1: Use pre-parsed data from recon.py. ``data`` is already the
        # value under the "web_search" key, so pass it straight to _split_payload
        # (it handles the object form, the legacy bare list, and the nested form).
        if data is not None:
            queries, urls = self._split_payload(data)

        # Phase 2: Self-parse from raw LLM text when central parser failed
        if not queries and not urls and _raw_llm_text:
            queries, urls = self._extract_payload_from_text(_raw_llm_text)
            if queries or urls:
                log_info(
                    f"[recon_web_search] Extracted {len(queries)} queries and "
                    f"{len(urls)} link(s) from raw LLM text (central JSON parser "
                    f"missed these)"
                )

        if not queries and not urls:
            log_debug("[recon_web_search] No search queries or links generated by LLM")
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
                urls=urls or None,
            )
            log_info(
                f"[recon_web_search] Triggered background search task {task_id} "
                f"({len(queries)} queries, {len(urls)} link(s)) for "
                f"path={interface_path}"
            )
        except Exception as e:
            log_error(f"[recon_web_search] Failed to trigger background search: {e}")
            return []

        parts: list[str] = []
        if queries:
            parts.append(f"web searches for: {'; '.join(queries)}")
        if urls:
            parts.append(f"directly visiting the link(s): {', '.join(urls)}")
        work_desc = " and ".join(parts)
        instruction = (
            f"You are starting the following work in the background: {work_desc}. "
            "Tell the user, in their language and your own voice, that you are "
            "on it and will report the detailed results in a follow-up message. "
            "Answer the rest of their message normally now; do NOT fabricate "
            "search results or link contents — the real findings will arrive "
            "later as a separate turn."
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
