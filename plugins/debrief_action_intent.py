from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text


display_name = "Debrief Action Intent"


try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "ACTION_INTENT_DEBRIEF_ENABLED",
        label="Action Intent Debrief Enabled",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable Debrief action-intent plugin",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "ACTION_INTENT_MAX_ACTIONS",
        label="Action Intent Max Actions",
        default=3,
        value_type=int,
        ui_type="number",
        description="Maximum number of recovery actions returned by Debrief action intent",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "ACTION_INTENT_ALLOW_MESSAGE_ACTIONS",
        label="Action Intent Allow Message Actions",
        default=False,
        value_type=bool,
        ui_type="bool",
        description="Allow Debrief to propose message_* actions",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "ACTION_INTENT_PROACTIVE_ENABLED",
        label="Action Intent Proactive Enabled",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Allow Debrief to propose proactive reminder actions",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass


class DebriefActionIntentPlugin:
    display_name = display_name

    def get_supported_actions(self) -> dict:
        return {}

    def _is_enabled(self) -> bool:
        try:
            return bool(
                config_registry.get_value(
                    "ACTION_INTENT_DEBRIEF_ENABLED", True, value_type=bool
                )
            )
        except Exception:
            return True

    def _allow_message_actions(self) -> bool:
        try:
            return bool(
                config_registry.get_value(
                    "ACTION_INTENT_ALLOW_MESSAGE_ACTIONS", False, value_type=bool
                )
            )
        except Exception:
            return False

    def _get_max_actions(self) -> int:
        try:
            return int(
                config_registry.get_value(
                    "ACTION_INTENT_MAX_ACTIONS", 3, value_type=int
                )
                or 3
            )
        except Exception:
            return 3

    def _proactive_enabled(self) -> bool:
        try:
            return bool(
                config_registry.get_value(
                    "ACTION_INTENT_PROACTIVE_ENABLED", True, value_type=bool
                )
            )
        except Exception:
            return True

    @staticmethod
    def _normalize_assistant_response(text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            return ""
        parsed = None
        try:
            parsed = extract_json_from_text(text, return_metadata=False)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            message = parsed.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return text.strip()

    async def on_debrief(
        self,
        processed_actions: List[Dict],
        failed_actions: List[Dict],
        results: Dict,
        context: Dict,
        original_message: Any,
    ) -> Dict | None:
        if not self._is_enabled():
            return None

        if not isinstance(context, dict):
            context = {}

        llm_response = (
            context.get("llm_response_text")
            or (results or {}).get("llm_response_text")
            or ""
        )
        llm_response = self._normalize_assistant_response(llm_response)
        if not isinstance(llm_response, str) or not llm_response.strip():
            return None

        if not (
            context.get("from_llm") or getattr(original_message, "from_llm", False)
        ):
            return None

        user_message = (
            context.get("original_user_message")
            or getattr(original_message, "text", "")
            or ""
        )
        if not isinstance(user_message, str) or not user_message.strip():
            return None

        try:
            from core.core_initializer import core_initializer
            from core.action_schema_converter import (
                extract_for_llm_prompt,
                normalize_action_schema,
            )

            available_actions = core_initializer.actions_block.get(
                "available_actions", {}
            )
        except Exception as e:
            log_warning(f"[debrief_action_intent] Failed to load actions metadata: {e}")
            available_actions = {}

        allowed_action_types = context.get("allowed_action_types")
        if isinstance(allowed_action_types, list) and allowed_action_types:
            available_actions = {
                k: v for k, v in available_actions.items() if k in allowed_action_types
            }

        if not self._allow_message_actions():
            available_actions = {
                k: v
                for k, v in available_actions.items()
                if not (isinstance(k, str) and k.startswith("message_"))
            }

        if not available_actions:
            return None

        minified_actions: Dict[str, Dict] = {}
        for action_name, action_def in available_actions.items():
            try:
                normalized = normalize_action_schema(action_name, action_def)
                minified_actions[action_name] = extract_for_llm_prompt(
                    action_name, normalized
                )
            except Exception:
                continue

        if not minified_actions:
            return None

        processed_types = {
            (a.get("type") or a.get("action"))
            for a in (processed_actions or [])
            if isinstance(a, dict)
        }
        failed_types = {
            (
                a.get("action", {}).get("type")
                if isinstance(a.get("action"), dict)
                else a.get("type") or a.get("action")
            )
            for a in (failed_actions or [])
            if isinstance(a, dict)
        }

        now_iso = datetime.now(timezone.utc).isoformat()
        max_actions = self._get_max_actions()
        proactive_enabled = self._proactive_enabled()

        system_prompt = (
            "You are the Debrief Action-Intent analyzer. Your job is to detect\n"
            "promised, implied, or missing actions that the assistant should have\n"
            "performed based on the user message and the assistant response.\n\n"
            "Rules:\n"
            "- Only return actions from the available action schemas.\n"
            "- Do NOT repeat actions already processed or failed.\n"
            "- If no recovery actions are needed, return an empty list.\n"
            "- If proactive reminders are enabled, infer if the user mentioned\n"
            "  any future time/date or a task to remember and propose a suitable\n"
            "  reminder action (e.g. schedule_message or event) without inventing\n"
            "  missing details.\n"
            "- Output ONLY valid JSON with the exact schema below.\n\n"
            "Schema:\n"
            '{"recovery_actions":[{"action_type":str,"payload":object,"reason":str,"confidence":"low|medium|high"}]}'
        )

        user_prompt = json.dumps(
            {
                "now": now_iso,
                "user_message": user_message.strip(),
                "assistant_response": llm_response.strip(),
                "processed_action_types": sorted([t for t in processed_types if t]),
                "failed_action_types": sorted([t for t in failed_types if t]),
                "available_actions": minified_actions,
                "proactive_enabled": proactive_enabled,
                "preferred_action_types": ["schedule_message", "event"],
                "max_actions": max_actions,
            },
            ensure_ascii=False,
        )

        engine = None
        try:
            from core.config import get_active_llm
            from core.llm_registry import get_llm_registry

            active_llm = await get_active_llm()
            registry = get_llm_registry()
            engine = registry.get_engine(active_llm) or registry.load_engine(active_llm)
        except Exception as e:
            log_warning(f"[debrief_action_intent] Failed to load active LLM: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return None

        try:
            llm_text = await asyncio.wait_for(
                engine.generate_response(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ]
                ),
                timeout=10,
            )
        except Exception as e:
            log_warning(f"[debrief_action_intent] LLM generation failed: {e}")
            return None

        parsed = None
        try:
            parsed = extract_json_from_text(llm_text, return_metadata=False)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            return None

        raw_actions = parsed.get("recovery_actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            return None

        action_types = set(minified_actions.keys())
        recovered: List[Dict[str, Any]] = []
        seen = set()

        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            action_type = item.get("action_type") or item.get("type")
            if not action_type or action_type not in action_types:
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            dedup_key = (action_type, json.dumps(payload, sort_keys=True))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            recovered.append(
                {
                    "action_type": action_type,
                    "payload": payload,
                    "reason": item.get("reason") or "debrief_intent",
                    "confidence": item.get("confidence") or "medium",
                }
            )
            if len(recovered) >= max_actions:
                break

        if not recovered:
            return None

        log_info(
            f"[debrief_action_intent] Proposed {len(recovered)} recovery action(s)"
        )
        return {"recovery_actions": recovered}


PLUGIN_CLASS = DebriefActionIntentPlugin
