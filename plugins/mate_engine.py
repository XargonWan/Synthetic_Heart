"""Optional plugin to integrate Mate-Engine semantics and policies.

Provides:
- get_supported_actions(): actions for enqueuing messages to Mate outbox and for promoting uploads
- admin endpoints (registered via register_interface hooks) to perform promotions under policy
- helper get_prompt_instructions() for prompt override guidance

This plugin intentionally delegates core behaviour to `core.animation_uploads` and `core.webui` endpoints and focuses on policy, auditing and convenient actions for LLMs.
"""

from __future__ import annotations

import os
import json
from typing import Any, Dict, Optional
from fastapi import Request, HTTPException
from core.core_initializer import register_interface
from core.logging_utils import log_info, log_warning
from core.animation_uploads import promote_upload, list_uploads, delete_upload


class MateEnginePlugin:
    display_name = "Mate Engine Integration Plugin"
    interface_id = "mate_engine_plugin"

    @staticmethod
    def get_supported_actions() -> Dict[str, Dict[str, Any]]:
        return {
            "send_mate_message": {
                "description": "Send a text message to the Mate Engine outbox",
                "required_fields": ["text", "target"],
                "optional_fields": ["metadata"],
            },
            "promote_upload": {
                "description": "Promote a temporary animation upload into a target skin (admin only)",
                "required_fields": ["upload_id", "target_skin"],
                "optional_fields": ["target_state", "overwrite", "rename"],
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if action_name == "send_mate_message":
            return {
                "description": "Send a message to Mate Engine integration (will appear in outbox for the client to poll)",
                "payload": {"text": {"type": "string"}, "target": {"type": "string"}, "metadata": {"type": "object", "optional": True}},
            }
        if action_name == "promote_upload":
            return {
                "description": "Promote an existing upload into a skin (admin-only)",
                "payload": {"upload_id": {"type": "string"}, "target_skin": {"type": "string"}, "target_state": {"type": "string", "optional": True}},
            }
        return {}

    async def execute_action(self, action: Dict[str, Any], context: Dict[str, Any], bot, original_message) -> None:
        """Execute LLM-triggered actions that operate on the Mate integration or uploads."""
        typ = action.get("type") or action.get("action")
        payload = action.get("payload") or {}

        if typ == "send_mate_message":
            text = payload.get("text")
            target = payload.get("target")
            if not text:
                raise ValueError("text is required for send_mate_message")
            # Use webui's enqueue helper by importing the interface if available
            try:
                from core.webui import synth_webui_interface, SynthWebUIInterface
                webui_instance = synth_webui_interface if getattr(synth_webui_interface, 'enqueue_outbox', None) else None
                if webui_instance is None:
                    # Fallback to creating a temporary interface for enqueueing in tests or headless contexts
                    webui_instance = SynthWebUIInterface(autostart=False)
                await webui_instance.enqueue_outbox("mate", text=text, target=target)
                log_info(f"[mate_plugin] Enqueued mate message to target={target}")
            except Exception as exc:
                log_warning(f"[mate_plugin] Failed to enqueue mate message: {exc}")

        elif typ == "promote_upload":
            # This action is admin-sensitive. Respect environment guard.
            if os.getenv("SYNTH_MATEENGINE_PROMOTE_ENABLED", "0") != "1":
                raise PermissionError("Promote uploads is disabled (SYNTH_MATEENGINE_PROMOTE_ENABLED != 1)")
            upload_id = payload.get("upload_id")
            target_skin = payload.get("target_skin")
            if not upload_id or not target_skin:
                raise ValueError("upload_id and target_skin are required for promote_upload")
            try:
                promoted = promote_upload(upload_id, target_skin=target_skin, target_state=payload.get("target_state"), overwrite=bool(payload.get("overwrite", False)), rename=payload.get("rename"))
                log_info(f"[mate_plugin] Promoted upload {upload_id} into skin {target_skin}: {promoted}")
            except Exception as exc:
                log_warning(f"[mate_plugin] Promotion failed: {exc}")
                raise


# Register plugin entry for the loader
PLUGIN_CLASS = MateEnginePlugin
