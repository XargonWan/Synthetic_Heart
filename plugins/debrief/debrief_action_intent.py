from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.transport_layer import extract_json_from_text, run_corrector_middleware


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
        advanced=True,
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
        advanced=True,
        needs_component_reload=False,
    )
    register_exposed_var(
        "ACTION_INTENT_ALLOW_MESSAGE_ACTIONS",
        label="Action Intent Allow Message Actions",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Allow Debrief to propose message_* actions",
        scope="agent",
        component="agent",
        advanced=True,
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

    @staticmethod
    def _extract_action_candidates(parsed: Any) -> List[Dict[str, Any]]:
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

        if not isinstance(parsed, dict):
            return []

        if isinstance(parsed.get("actions"), list):
            return [item for item in parsed["actions"] if isinstance(item, dict)]

        if isinstance(parsed.get("recovery_actions"), list):
            normalized: List[Dict[str, Any]] = []
            for item in parsed["recovery_actions"]:
                if not isinstance(item, dict):
                    continue
                action_type = item.get("action_type") or item.get("type")
                payload = item.get("payload")
                if not action_type or not isinstance(payload, dict):
                    continue
                normalized.append(
                    {
                        "type": action_type,
                        "payload": payload,
                        "reason": item.get("reason"),
                        "confidence": item.get("confidence"),
                    }
                )
            return normalized

        action_type = parsed.get("type") or parsed.get("action_type")
        payload = parsed.get("payload")
        if action_type and isinstance(payload, dict):
            return [parsed]

        return []

    @staticmethod
    def _build_correction_context(
        *,
        context: Dict[str, Any],
        original_message: Any,
        user_message: str,
        allowed_action_types: List[str],
    ) -> Dict[str, Any]:
        correction_message = SimpleNamespace()
        correction_message.chat_id = getattr(original_message, "chat_id", None)
        correction_message.thread_id = getattr(original_message, "thread_id", None)
        correction_message.text = user_message
        correction_message.interface_path = getattr(
            original_message, "interface_path", None
        ) or context.get("interface_path")

        corrected_context = dict(context or {})
        corrected_context["message"] = correction_message
        corrected_context["from_cortex"] = True
        corrected_context["original_user_message"] = user_message
        corrected_context["allowed_action_types"] = list(allowed_action_types)
        return corrected_context

    async def _parse_recovery_output(
        self,
        *,
        llm_text: str,
        context: Dict[str, Any],
        original_message: Any,
        user_message: str,
        allowed_action_types: List[str],
    ) -> List[Dict[str, Any]]:
        parsed: Any = None
        metadata: Dict[str, Any] = {}

        try:
            parsed, metadata = extract_json_from_text(llm_text, return_metadata=True)
        except Exception as exc:
            log_debug(
                f"[debrief_action_intent] Initial JSON extraction failed, will ask corrector: {exc}"
            )

        candidates = self._extract_action_candidates(parsed)
        if candidates:
            return candidates

        corrected_context = self._build_correction_context(
            context=context,
            original_message=original_message,
            user_message=user_message,
            allowed_action_types=allowed_action_types,
        )
        corrected_text = await run_corrector_middleware(
            text=llm_text,
            bot=None,
            context=corrected_context,
            chat_id=getattr(original_message, "chat_id", None),
            thread_id=getattr(original_message, "thread_id", None),
        )
        if not isinstance(corrected_text, str) or not corrected_text.strip():
            if metadata.get("had_errors") or metadata.get("had_extra_text"):
                log_info(
                    "[debrief_action_intent] Corrector did not recover valid JSON for debrief output"
                )
            return []

        try:
            corrected_json = extract_json_from_text(
                corrected_text, return_metadata=False
            )
        except Exception as exc:
            log_warning(
                f"[debrief_action_intent] Corrected debrief output still failed JSON extraction: {exc}"
            )
            return []

        return self._extract_action_candidates(corrected_json)

    @staticmethod
    def _normalize_recovery_actions(
        *,
        candidates: List[Dict[str, Any]],
        allowed_action_types: set[str],
        processed_types: set[str],
        failed_types: set[str],
        max_actions: int,
    ) -> List[Dict[str, Any]]:
        recovered: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for item in candidates:
            action_type = item.get("action_type") or item.get("type")
            if (
                not isinstance(action_type, str)
                or action_type not in allowed_action_types
            ):
                continue
            if action_type in processed_types or action_type in failed_types:
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

        return recovered

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
                    "ACTION_INTENT_ALLOW_MESSAGE_ACTIONS", True, value_type=bool
                )
            )
        except Exception:
            return True

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
            context.get("from_cortex")
            or getattr(original_message, "from_cortex", False)
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

        synth_name = "SyntH"
        try:
            synth_name = str(
                config_registry.get_value("SYNTH_NAME", "SyntH", value_type=str)
                or "SyntH"
            ).strip()
        except Exception:
            pass

        system_prompt = (
            f"You are the Debrief Action-Intent analyzer. Your job is to identify\n"
            f"actions {synth_name} PROMISED or IMPLIED in its response but did NOT\n"
            f"actually execute as formal actions.\n\n"
            f"CRITICAL RULES:\n"
            f"1. ALWAYS propose at least one recovery action when {synth_name} said\n"
            f"   things like 'ok, i will do it', 'ok, i'll check', 'I will reply',\n"
            f"   'I will send', 'i will do it tomorrow', or any other commitment.\n"
            f"2. Convert conversational promises into concrete executable actions\n"
            f"   using ONLY the available action schemas below.\n"
            f"3. If {synth_name} promised to send a message, you MUST propose a\n"
            f"   message_* action with the appropriate interface_path and text.\n"
            f"4. If {synth_name} promised to schedule/remind something but did NOT\n"
            f"   create a schedule_message or event action, you MUST propose it.\n"
            f"   This is the most common failure mode: {synth_name} says 'ok, i will do it',\n"
            f"   'ok, i'll check', 'I will reply', 'I will send', 'i will do it tomorrow'\n"
            f"   but forgets to actually schedule it.\n"
            f"5. Do NOT return an empty list unless {synth_name} explicitly refused\n"
            f"   or said it cannot do something.\n"
            f"6. Do NOT repeat actions already in processed_action_types or failed_action_types.\n"
            f"7. Output ONLY valid JSON with the exact schema below.\n\n"
            "EXAMPLES:\n"
            'User: "Remind me tomorrow"\n'
            f'{synth_name}: "Ok, I will write it tomorrow."\n'
            '→ [{"type": "schedule_message", "payload": {"text": "...", "send_in": "1 day"}}]\n\n'
            'User: "Check and let me know"\n'
            f'{synth_name}: "Ok, I will check and let you know."\n'
            '→ [{"type": "message_telegram_bot", "payload": {"text": "...", "interface_path": "..."}}]\n\n'
            'User: "I need a reminder for the meeting"\n'
            f'{synth_name}: "Perfect, I have set the reminder for the meeting."\n'
            '→ [{"type": "schedule_message", "payload": {"text": "Meeting reminder", "send_in": "..."}}]\n\n'
            "Schema:\n"
            '{"actions":[{"type":str,"payload":object,"reason":str,"confidence":"low|medium|high"}]}'
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
            from core.config import derive_cortex_scope, get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            scope = derive_cortex_scope(context if isinstance(context, dict) else None)
            active_cortex = await get_active_cortex_engine(scope=scope)
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(
                f"[debrief_action_intent] Failed to load active Cortex engine: {e}"
            )
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
                timeout=120,
            )
        except Exception as e:
            log_warning(f"[debrief_action_intent] LLM generation failed: {e}")
            return None

        action_types = set(minified_actions.keys())
        candidates = await self._parse_recovery_output(
            llm_text=llm_text,
            context=context,
            original_message=original_message,
            user_message=user_message.strip(),
            allowed_action_types=sorted(action_types),
        )

        recovered = self._normalize_recovery_actions(
            candidates=candidates,
            allowed_action_types=action_types,
            processed_types={t for t in processed_types if isinstance(t, str)},
            failed_types={t for t in failed_types if isinstance(t, str)},
            max_actions=max_actions,
        )

        if not recovered:
            return None

        log_info(
            f"[debrief_action_intent] Proposed {len(recovered)} recovery action(s)"
        )
        return {"recovery_actions": recovered}


PLUGIN_CLASS = DebriefActionIntentPlugin
