from __future__ import annotations

from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text
from core.synth_core_memory import search_memories
from core.interface_path_utils import is_vessel_history_entry, is_vessel_interface_path
from core.vessel_focus import is_vessel_turn


display_name = "Recon Memory Recollector"

# UI-exposed switch to enable/disable this recon plugin
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_MEMORY_RECOLLECTOR_RECON_ENABLED",
        label="Enable Recon Memory Recollector",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Recon Memory Recollector plugin (search memories for Recon).",
        scope="recon",
        component="recon_memory_recollector",
        hidden=True,
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_MEMORY_RECOLLECTOR_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Memory Recollector",
        description="Enable the Recon Memory Recollector plugin (search memories for Recon).",
        group="recon",
        component="recon_memory_recollector",
        hidden=True,
    )


class ReconMemoryRecollectorPlugin:
    display_name = display_name
    recon_priority = 8

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "memory_search"

    def get_recon_instruction(self) -> str:
        return (
            "Extract 2-6 short tags and 2-6 short keywords for memory search. "
            'Return as an object: {"tags": ["tag"], "keywords": ["kw"]}.'
        )

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
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_MEMORY_RECOLLECTOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        raw_tags = []
        raw_keywords = []
        if isinstance(data, dict):
            raw_tags = data.get("tags") or []
            raw_keywords = data.get("keywords") or []
        else:
            raw_tags = tags or []
            raw_keywords = keywords or []

        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.split() if t.strip()]
        if isinstance(raw_keywords, str):
            raw_keywords = [k.strip() for k in raw_keywords.split() if k.strip()]

        tags = [str(t).strip() for t in raw_tags if str(t).strip()][:6]
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()][:6]

        if not tags and not keywords:
            return []

        # The message currently being answered is already persisted to
        # ``chat_history_cache``; excluding its own chat from the raw
        # chat-history tier prevents the live conversation (incl. stale
        # greetings) from being echoed back as "memories".
        _cur_path = getattr(message, "interface_path", None)
        if not _cur_path and isinstance(context_memory, dict):
            _cur_path = context_memory.get("interface_path")
        if _cur_path:
            try:
                from core.chat_context_manager import _resolve_context_path

                _cur_path = _resolve_context_path(str(_cur_path))
            except Exception:
                pass
        _excluded_paths = [str(_cur_path)] if _cur_path else None

        try:
            results = await search_memories(
                tags=tags,
                keywords=keywords,
                include_chat=True,
                limit=max_results,
                exclude_interface_paths=_excluded_paths,
            )
        except Exception as e:
            log_warning(f"[recon_memory] search_memories failed: {e}")
            return []

        contributions = []
        for row in results:
            if not isinstance(row, dict):
                continue
            contributions.append(
                {
                    "type": "memory",
                    "content": row,
                    "source": "memory_recollector",
                    "priority": int(self.recon_priority),
                }
            )

        log_info(
            f"[recon_memory] Collected {len(contributions)} memory contribution(s)"
        )
        return contributions

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
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_MEMORY_RECOLLECTOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        engine = None
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            active_cortex = await get_active_cortex_engine()
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(f"[recon_memory] Failed to load active Cortex engine: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return []

        # Build history context (local + global)
        local_lines: list[str] = []
        global_lines: list[str] = []

        interface_path = getattr(message, "interface_path", None)
        vessel_focus = is_vessel_turn(message, context_memory, interface_path)
        # Resolve the raw incoming path the same way messages are resolved when
        # persisted (alias/link map + Unified Lane), so the local-history lookup
        # keys line up with how the rows were stored.
        if interface_path:
            try:
                from core.chat_context_manager import _resolve_context_path

                interface_path = _resolve_context_path(interface_path)
            except Exception:
                pass
        try:
            if isinstance(context_memory, dict) and interface_path in context_memory:
                raw = list(context_memory.get(interface_path, []))
                for item in raw[-6:]:
                    if isinstance(item, dict):
                        sender = (
                            item.get("sender_name") or item.get("sender") or "unknown"
                        )
                        content = (
                            item.get("text")
                            or item.get("message_text")
                            or item.get("content")
                            or ""
                        )
                        if content:
                            local_lines.append(f"[{sender}] {content}")
        except Exception:
            pass

        try:
            from core.chat_history_cache import (
                load_chat_history,
                load_global_chat_history,
            )

            if interface_path:
                # match_chat_level=True: after a restart the in-memory context
                # is empty, so this DB read is the only source of local
                # history. An exact-path match silently drops thread-suffixed
                # turns of the same chat, leaving local history empty while
                # global history (unfiltered) survives.
                cached = await load_chat_history(
                    interface_path,
                    match_chat_level=(
                        not vessel_focus
                        and not is_vessel_interface_path(interface_path)
                    ),
                )
                for item in list(cached)[-6:]:
                    sender = item.get("sender_name") or "unknown"
                    content = item.get("text") or ""
                    if content:
                        local_lines.append(f"[{sender}] {content}")

            global_cached = await load_global_chat_history(limit=6)
            if vessel_focus:
                global_cached = []
            else:
                global_cached = [
                    item for item in global_cached if not is_vessel_history_entry(item)
                ]
            for item in list(global_cached)[-6:]:
                sender = item.get("sender_name") or "unknown"
                content = item.get("text") or ""
                if content:
                    global_lines.append(f"[{sender}] {content}")
        except Exception:
            pass

        local_text = "\n".join(local_lines) if local_lines else "(none)"
        global_text = "\n".join(global_lines) if global_lines else "(none)"

        system_prompt = (
            "This is a Recon prompt, please execute what is requested below:\n"
            "- Extract 2-6 short tags and 2-6 short keywords for memory search.\n"
            'Return ONLY valid JSON: {"tags": ["tag"], "keywords": ["kw"]}.\n'
            "Do not add any extra keys or commentary."
        )

        user_prompt = (
            f"User message:\n{text.strip()}\n\n"
            f"Recent local history:\n{local_text}\n\n"
            f"Recent global history:\n{global_text}\n"
        )

        try:
            llm_text = await engine.generate_response(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
        except Exception as e:
            log_warning(f"[recon_memory] LLM generate_response failed: {e}")
            return []

        parsed = None
        try:
            parsed = extract_json_from_text(llm_text, return_metadata=False)
        except Exception:
            parsed = None

        raw_tags = []
        raw_keywords = []
        if isinstance(parsed, dict):
            raw_tags = parsed.get("tags") or []
            raw_keywords = parsed.get("keywords") or []
        else:
            raw_tags = tags or []
            raw_keywords = keywords or []

        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.split() if t.strip()]
        if isinstance(raw_keywords, str):
            raw_keywords = [k.strip() for k in raw_keywords.split() if k.strip()]

        tags = [str(t).strip() for t in raw_tags if str(t).strip()][:6]
        keywords = [str(k).strip() for k in raw_keywords if str(k).strip()][:6]

        if not tags and not keywords:
            return []

        # The message currently being answered is already persisted to
        # ``chat_history_cache``; excluding its own chat from the raw
        # chat-history tier prevents the live conversation (incl. stale
        # greetings) from being echoed back as "memories".
        _excluded_paths = [str(interface_path)] if interface_path else None

        try:
            results = await search_memories(
                tags=tags,
                keywords=keywords,
                include_chat=True,
                limit=max_results,
                exclude_interface_paths=_excluded_paths,
            )
        except Exception as e:
            log_warning(f"[recon_memory] search_memories failed: {e}")
            return []

        contributions = []
        for row in results:
            if not isinstance(row, dict):
                continue
            contributions.append(
                {
                    "type": "memory",
                    "content": row,
                    "source": "memory_recollector",
                    "priority": int(self.recon_priority),
                }
            )

        log_info(
            f"[recon_memory] Collected {len(contributions)} memory contribution(s)"
        )
        return contributions


PLUGIN_CLASS = ReconMemoryRecollectorPlugin
