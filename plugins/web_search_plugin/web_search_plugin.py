# plugins/web_search_plugin.py
"""Web Search Plugin - Provides real-time internet search capability."""

from __future__ import annotations

import inspect
from typing import Any

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning

from plugins.web_search.search_engine import run_search

# Register exposed variable for WebUI / env loading
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "TAVILY_API_KEY",
        label="Tavily API Key",
        default="",
        value_type=str,
        ui_type="password",
        description="API key for Tavily Web Search. (Optional - fallback to DuckDuckGo if empty)",
        scope="plugins",
        component="web_search",
        tags=["plugin", "sensitive"],
    )
    register_exposed_var(
        "SEARXNG_URL",
        label="SearXNG URL",
        default="http://127.0.0.1:8888",
        value_type=str,
        description=(
            "Base URL of the SearXNG instance used as the preferred search "
            "backend. (Optional - falls back to Tavily/DuckDuckGo if empty or "
            "unreachable)"
        ),
        scope="plugins",
        component="web_search",
        tags=["plugin"],
    )
except Exception:
    pass

TAVILY_API_KEY = config_registry.get_var(
    "TAVILY_API_KEY",
    "",
    label="Tavily API Key",
    description="API key for Tavily Web Search.",
    value_type=str,
    group="plugins",
    component="web_search",
    sensitive=True,
)

SEARXNG_URL = config_registry.get_var(
    "SEARXNG_URL",
    "http://127.0.0.1:8888",
    label="SearXNG URL",
    description="Base URL of the self-hosted SearXNG search backend.",
    value_type=str,
    group="plugins",
    component="web_search",
)


class WebSearchPlugin:
    """Plugin to perform web searches for factual grounding."""

    display_name = "Web Search"

    def __init__(self) -> None:
        register_plugin("web_search", self)
        log_info("[web_search] WebSearchPlugin initialized and registered")

    def get_supported_action_types(self) -> list[str]:
        """Return the supported action types."""
        return ["search_current_knowledge"]

    def get_supported_actions(self) -> dict[str, Any]:
        """Return actions metadata and schemas."""
        return {
            "search_current_knowledge": {
                "description": "Perform a web search to retrieve up-to-date factual information about current events, people, places, or technology post-dating your knowledge cutoff.",
                "required_fields": ["query"],
                "optional_fields": [],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query to retrieve real-time facts about.",
                        }
                    },
                    "required": ["query"],
                },
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Provide detailed instructions for when to use the web search action."""
        if action_name == "search_current_knowledge":
            return {
                "usage": (
                    "Use ONLY for factual queries about external real-time information "
                    "(news, weather, current events, people, places, technology, etc.). "
                    "Do NOT use this action to answer questions about Synthetic Heart's "
                    "own capabilities, features, tools, or plugins. Those should be "
                    "answered from the 'available_actions' section in your system prompt."
                ),
                "examples": [
                    {
                        "query": "latest artificial intelligence news",
                        "when": "user asks about recent external events",
                    },
                    {
                        "query": "weather in Tokyo",
                        "when": "user asks about current weather",
                    },
                ],
                "avoid": (
                    "Avoid using this action when the user asks about what you can do "
                    "or what tools you have. Answer capability questions directly from "
                    "the 'available_actions' block in your system prompt — you already "
                    "know your own capabilities."
                ),
            }
        return {}

    async def execute_action(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any] | None:
        """Execute the search action and send the results back to the prompt chain."""
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type != "search_current_knowledge":
            return None

        query = str(payload.get("query", "")).strip()
        if not query:
            return {"error": "Empty query"}

        log_info(f"[web_search] Performing web search for: '{query}'")

        # Prevent loop: check if we are already in delivery mode
        is_action_result_delivery = (
            context.get("prompt_request_mode") == "delivery"
            or context.get("mode") == "delivery"
        )

        results: list[dict[str, str]] = []
        try:
            results = await run_search(query, max_results=5)
        except Exception as e:
            log_error(f"[web_search] Search error: {e}")

        if results:
            action_outputs = [
                {
                    "type": "web_search_result",
                    "result": {
                        "title": r.get("title", ""),
                        "snippet": r.get("snippet", ""),
                        "url": r.get("url", ""),
                    },
                }
                for r in results
            ]
        else:
            action_outputs = [
                {
                    "type": "web_search_result",
                    "result": {
                        "found": 0,
                        "message": f"No web search results found for query '{query}'. You should acknowledge this fact to the user.",
                    },
                }
            ]

        if not is_action_result_delivery:
            delivery_ok = False
            try:
                from core.auto_response import request_llm_delivery

                # Build a delivery context the auto-response system can act on.
                # The raw action context only carries interface_path/chat_id (no
                # interface_name), and request_llm_response bails out silently
                # without it — the legacy memory_search plugin solves this the
                # same way. interface_name is derived structurally from the
                # interface_path prefix ("telegram_bot/123" -> "telegram_bot").
                raw_interface_name = context.get("interface_name") or context.get(
                    "interface"
                )
                interface_path = context.get("interface_path") or getattr(
                    original_message, "interface_path", None
                )
                if (
                    not raw_interface_name
                    and interface_path
                    and "/" in str(interface_path)
                ):
                    raw_interface_name = str(interface_path).split("/", 1)[0]
                original_context = {
                    "interface_name": raw_interface_name,
                    "interface_path": interface_path,
                    "chat_id": context.get("chat_id")
                    or getattr(original_message, "chat_id", None),
                    "message_id": context.get("message_id")
                    or getattr(original_message, "message_id", None),
                }

                delivered = await request_llm_delivery(
                    action_outputs=action_outputs,
                    original_context=original_context,
                    action_type="search_current_knowledge",
                )
                # The legacy path returns True once the delivery turn has been
                # enqueued; treat a falsy return as a failed delivery.
                delivery_ok = bool(delivered)
                log_info(
                    f"[web_search] Requested LLM delivery; success={bool(delivered)}"
                )
            except Exception as e:
                log_warning(f"[web_search] Failed to request LLM delivery: {e}")

            # Fallback: if LLM delivery was not attempted or failed, send the
            # outcome directly to the user so they get useful information (or a
            # clear "no results" note) instead of silence.
            if not delivery_ok:
                await self._send_results_directly(
                    results, query, context, original_message
                )

        return {"status": "ok", "results_count": len(results)}

    async def _send_results_directly(
        self,
        results: list[dict[str, str]],
        query: str,
        context: dict[str, Any],
        original_message: Any,
    ) -> None:
        """Send search results directly as a text message when LLM delivery fails."""
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            interface_name = (
                context.get("interface_name")
                if context
                else getattr(original_message, "interface_name", None)
            )
            interface_path = (
                context.get("interface_path")
                if context
                else getattr(original_message, "interface_path", None)
            )
            thread_id = (
                context.get("thread_id")
                if context
                else getattr(original_message, "thread_id", None)
            )

            # The action context never carries interface_name; derive it
            # structurally from the interface_path prefix.
            if not interface_name and interface_path and "/" in str(interface_path):
                interface_name = str(interface_path).split("/", 1)[0]

            # interface_path is what the interface needs to resolve the target
            # chat; chat_id alone is not required by every interface.
            if not interface_name or not interface_path:
                log_debug(
                    "[web_search] Cannot send direct fallback — missing "
                    "interface_name/interface_path in context"
                )
                return

            iface = INTERFACE_REGISTRY.get(interface_name)
            if not iface:
                log_debug(
                    f"[web_search] Cannot send direct fallback — interface "
                    f"'{interface_name}' not in registry"
                )
                return

            # Build a concise text summary of results
            lines = [f"🔍 Search results for: {query}"]
            if not results:
                lines.append(
                    "\nNo web search results found for this query (search backend "
                    "unavailable or returned nothing)."
                )
            for i, r in enumerate(results[:5], 1):
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                url = r.get("url", "")
                # Truncate snippet to stay within message limits
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                lines.append(f"\n{i}. {title}")
                if snippet:
                    lines.append(f"   {snippet}")
                if url:
                    lines.append(f"   {url}")

            fallback_text = "\n".join(lines)

            # Send through the interface's canonical payload-dict path — the same
            # call shape the message_* actions use (see action_parser message
            # dispatch and message_plugin). universal_send is for LLM-response
            # flows, and hand-rolled chat_id/text kwargs break payload-style
            # interfaces (Telegram's send_message takes a single payload dict).
            try:
                send_payload: dict[str, Any] = {
                    "text": fallback_text,
                    "interface_path": interface_path,
                }
                if thread_id is not None:
                    send_payload["thread_id"] = thread_id
                result = iface.send_message(
                    send_payload, original_message=original_message
                )
                if inspect.iscoroutine(result):
                    result = await result
                if result is False:
                    log_warning(
                        "[web_search] Direct fallback reported delivery failure "
                        f"({len(results)} results, {len(fallback_text)} chars)"
                    )
                    return
                log_info(
                    "[web_search] Direct fallback sent via interface.send_message "
                    f"({len(results)} results, {len(fallback_text)} chars)"
                )
            except Exception as se:
                log_warning(f"[web_search] Failed to send direct fallback: {se}")
        except Exception as e:
            log_warning(f"[web_search] Direct fallback error: {e}")


PLUGIN_CLASS = WebSearchPlugin
