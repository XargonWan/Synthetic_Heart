# plugins/web_search_plugin.py
"""Web Search Plugin - Provides real-time internet search capability."""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import requests
from bs4 import BeautifulSoup

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning

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
            tavily_key = str(TAVILY_API_KEY).strip()
            if tavily_key:
                results = await self._search_tavily(tavily_key, query)
            else:
                results = await self._search_duckduckgo(query)
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

                delivered = await request_llm_delivery(
                    action_outputs=action_outputs,
                    original_context=context,
                    action_type="search_current_knowledge",
                )
                # delivered is None in the legacy path, so treat non-exception as success
                delivery_ok = True
                log_info(
                    f"[web_search] Requested LLM delivery; success={bool(delivered)}"
                )
            except Exception as e:
                log_warning(f"[web_search] Failed to request LLM delivery: {e}")

            # Fallback: if LLM delivery was not attempted or failed, send results
            # directly to the user so they get useful information instead of "😵"
            if not delivery_ok and results:
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

            interface_name = context.get("interface_name") if context else None
            interface_path = (
                context.get("interface_path")
                if context
                else getattr(original_message, "interface_path", None)
            )
            chat_id = (
                context.get("chat_id")
                if context
                else getattr(original_message, "chat_id", None)
            )
            thread_id = (
                context.get("thread_id")
                if context
                else getattr(original_message, "thread_id", None)
            )

            if not interface_name or not interface_path or not chat_id:
                log_debug(
                    "[web_search] Cannot send direct fallback — missing "
                    "interface_name/interface_path/chat_id in context"
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

            # Try universal_send first (handles thread_id, etc.)
            try:
                from core.transport_layer import universal_send

                await universal_send(
                    iface,
                    chat_id=chat_id,
                    text=fallback_text,
                    interface_path=interface_path,
                    thread_id=thread_id,
                    is_llm_response=True,
                    skip_history=True,
                )
                log_info(
                    "[web_search] Direct fallback sent via universal_send "
                    f"({len(results)} results, {len(fallback_text)} chars)"
                )
                return
            except Exception as ue:
                log_debug(
                    f"[web_search] universal_send fallback failed, "
                    f"trying direct send: {ue}"
                )

            # Fallback to send_message on the interface
            if hasattr(iface, "send_message"):
                try:
                    kwargs = {"chat_id": chat_id, "text": fallback_text}
                    if thread_id is not None:
                        kwargs["message_thread_id"] = thread_id
                    await iface.send_message(**kwargs)
                    log_info(
                        "[web_search] Direct fallback sent via interface.send_message"
                    )
                except Exception as se:
                    log_warning(f"[web_search] Failed to send direct fallback: {se}")
        except Exception as e:
            log_warning(f"[web_search] Direct fallback error: {e}")

    async def _search_tavily(self, api_key: str, query: str) -> list[dict[str, str]]:
        """Perform search using the Tavily API."""

        def _do_post() -> dict[str, Any]:
            headers = {"Content-Type": "application/json"}
            payload = {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "include_answer": False,
                "max_results": 5,
            }
            response = requests.post(
                "https://api.tavily.com/search",
                headers=headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            return response.json()

        try:
            data = await asyncio.to_thread(_do_post)
            results = data.get("results", [])
            log_info(f"[web_search] Tavily returned {len(results)} results")
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("content", ""),
                    "url": r.get("url", ""),
                }
                for r in results
            ]
        except Exception as e:
            log_warning(
                f"[web_search] Tavily API request failed: {e}. Falling back to DuckDuckGo..."
            )
            return await self._search_duckduckgo(query)

    async def _search_duckduckgo(self, query: str) -> list[dict[str, str]]:
        """Perform search using DuckDuckGo HTML scraping."""

        def _do_get() -> str:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.text

        try:
            html = await asyncio.to_thread(_do_get)
            soup = BeautifulSoup(html, "html.parser")
            results: list[dict[str, str]] = []

            elements = soup.select(".result")
            if not elements:
                elements = soup.select(".web-result")

            for el in elements:
                title = ""
                snippet = ""
                url = ""

                title_el = el.select_one(".result__title, .result__a, h2")
                snippet_el = el.select_one(".result__snippet, .snippet, .result__body")
                url_el = el.select_one(".result__url, .url")

                if title_el:
                    title = title_el.get_text(strip=True)
                    link = (
                        title_el if title_el.name == "a" else title_el.select_one("a")
                    )
                    if link:
                        href = link.get("href")
                        if href:
                            url = str(href)
                if snippet_el:
                    snippet = snippet_el.get_text(strip=True)
                if not url and url_el:
                    url = url_el.get_text(strip=True)

                if title and snippet:
                    if url.startswith("/l/?") or "uddg=" in url:
                        try:
                            parsed_url = urllib.parse.urlparse(url)
                            qs = urllib.parse.parse_qs(parsed_url.query)
                            if "uddg" in qs:
                                url = qs["uddg"][0]
                        except Exception:
                            pass
                    results.append(
                        {
                            "title": title,
                            "snippet": snippet,
                            "url": url,
                        }
                    )
                    if len(results) >= 5:
                        break

            log_info(f"[web_search] DuckDuckGo returned {len(results)} results")
            return results
        except Exception as e:
            log_error(f"[web_search] DuckDuckGo scraping failed: {e}")
            return []


PLUGIN_CLASS = WebSearchPlugin
