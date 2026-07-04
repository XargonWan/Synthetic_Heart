from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning


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
            "Determine if the user request requires a web search to provide up-to-date information. "
            "If YES, generate 1-3 specific search queries that would help answer the user's question. "
            'Return as an object: {"web_search": ["query1", "query2", ...]}. '
            'If NO web search is needed, return an empty list: {"web_search": []}. '
            "Examples of when to search: current events, news, weather, recent developments, factual information that may have changed. "
            "Examples of when NOT to search: general knowledge, opinions, hypothetical scenarios, internal system questions."
        )

    async def _execute_web_search(self, query: str, max_results: int = 5) -> str:
        """Execute a single web search query and return formatted results."""
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            web_search_plugin = None
            for plugin in PLUGIN_REGISTRY.values():
                if hasattr(plugin, "search_current_knowledge"):
                    web_search_plugin = plugin
                    break

            if not web_search_plugin:
                return "[Web search unavailable: plugin not found]"

            result = await web_search_plugin.search_current_knowledge(query=query)

            if not result or not isinstance(result, dict):
                return f"[No results for: {query}]"

            snippets = result.get("results", [])
            if not snippets:
                return f"[No results for: {query}]"

            formatted = f"Search: {query}\n"
            for i, snippet in enumerate(snippets[:max_results], 1):
                title = snippet.get("title", "Untitled")
                content = snippet.get("content", snippet.get("snippet", ""))
                url = snippet.get("url", "")
                formatted += f"{i}. {title}\n{content}\n{url}\n\n"

            return formatted.strip()

        except Exception as e:
            log_warning(f"[recon_web_search] Search failed for query '{query}': {e}")
            return f"[Search error for '{query}': {str(e)}]"

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
            return []

        log_info(f"[recon_web_search] Executing {len(queries)} web search(es)")

        search_tasks = [
            self._execute_web_search(query, max_results=max_results)
            for query in queries
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        combined_parts = []
        for query, result in zip(queries, search_results):
            if isinstance(result, Exception):
                combined_parts.append(f"[Search failed for '{query}': {result}]")
            elif isinstance(result, str) and result:
                combined_parts.append(result)

        if not combined_parts:
            return []

        combined_text = "\n\n---\n\n".join(combined_parts)

        return [
            {
                "type": "web_search_results",
                "content": combined_text,
                "source": "recon_web_search",
                "priority": int(self.recon_priority),
            }
        ]

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
