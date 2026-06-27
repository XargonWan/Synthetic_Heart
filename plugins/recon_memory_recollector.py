from __future__ import annotations

from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text
from core.synth_core_memory import search_memories


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
        scope="agent",
        component="agent",
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_MEMORY_RECOLLECTOR_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Memory Recollector",
        description="Enable the Recon Memory Recollector plugin (search memories for Recon).",
        group="agent",
        component="agent",
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

        try:
            results = await search_memories(
                tags=tags, keywords=keywords, include_chat=True, limit=max_results
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
                cached = await load_chat_history(interface_path)
                for item in list(cached)[-6:]:
                    sender = item.get("sender_name") or "unknown"
                    content = item.get("text") or ""
                    if content:
                        local_lines.append(f"[{sender}] {content}")

            global_cached = await load_global_chat_history(limit=6)
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

        try:
            results = await search_memories(
                tags=tags, keywords=keywords, include_chat=True, limit=max_results
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
