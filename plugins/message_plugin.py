# plugins/message_plugin.py
"""Message plugin for handling text message actions."""

from difflib import SequenceMatcher

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.core_initializer import INTERFACE_REGISTRY
from core.config_manager import config_registry
from core.beat_utils import is_outbound_beat


class MessagePlugin:
    """Plugin to handle message-type actions across multiple interfaces."""

    display_name = "Message Handler"

    def __init__(self):
        """Initialize the plugin."""
        # Populate supported interfaces from the registry if available
        if INTERFACE_REGISTRY:
            self.supported_interfaces = list(INTERFACE_REGISTRY.keys())
        else:
            # Default to telegram_bot if no interfaces registered yet
            self.supported_interfaces = ["telegram_bot"]
        log_debug("[message_plugin] MessagePlugin initialized")

    @property
    def description(self):
        """Return a description of this plugin."""
        return "Handles text message sending across different interfaces (Telegram, Discord, etc.)"

    def get_supported_action_types(self):
        """Return the action types this plugin supports."""
        return [
            "message_telegram_bot",
            "message_reddit",
            "message_discord",
            "message_discord_bot",
            "message_x",
            "message_synth_webui",
            "message_ollama_serve",
            "message_matrix_chat",
        ]

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this plugin interface."""
        return "message"  # Generic message plugin - works with any interface

    def get_supported_actions(self) -> dict:
        """
        Claim support for standard message actions to intercept them
        and ensure context (like TTS audio) is passed to interface execution.
        """
        return {
            "message_telegram_bot": {},
            "message_discord_bot": {},
            "message_synth_webui": {},
            "message_reddit": {},
            "message_x": {},
            "message_matrix_chat": {},
            "message_ollama_serve": {},
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Prompt instructions for supported actions."""
        # No longer provides instructions - interfaces handle this
        return {}

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute a message action."""
        try:
            await self._handle_message_action(action, context, bot, original_message)

        except Exception as e:
            log_error(f"[message_plugin] Error executing message action: {repr(e)}")

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle custom message actions."""
        if action_type.startswith("message_"):
            log_info(
                f"[message_plugin] Handling {action_type} action with payload: "
                + str(payload)
            )

    async def _should_mirror_origin_path(self, context, original_message) -> bool:
        """Whether a reply's route should be forced to the originating chat.

        Scoped to **local-model** openai endpoints — those with ``disable_tools``
        or ``force_action_grammar`` set. Such models frequently hallucinate
        ``interface_path`` (e.g. ``/channels/main``), so a direct reply silently
        fails to deliver ("Chat not found"). Cloud openai endpoints (xai,
        openrouter) route reliably and are left untouched, as are non-openai and
        grillo/outreach/internal turns (the latter target a system-chosen chat).
        """
        try:
            if isinstance(context, dict) and (
                is_outbound_beat(context.get("beat_type"))
                or context.get("grillo_beat")
                or context.get("activity_log_id")
                or context.get("grillo_activity_log_id")
            ):
                return False
            if getattr(original_message, "chat_id", None) in (None, "", -1, "-1"):
                return False

            from core.config import derive_cortex_scope, get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry
            from core.external_endpoints.bridges.cortex_bridge import (
                ExternalCortexEngine,
            )
            from core.external_endpoints.models import EndpointProtocol

            # Resolve the engine for THIS turn's scope (base/trainer), not just the
            # global base. Scopes are routinely split (e.g. local 1070ti base +
            # xai trainer for image recognition); only the local openai_compat
            # engine needs path mirroring — an xai trainer turn must be left alone.
            scope = derive_cortex_scope(context if isinstance(context, dict) else None)
            engine_name = await get_active_cortex_engine(scope=scope)
            if not engine_name:
                return False
            instance = get_cortex_registry().get_engine(engine_name)
            if not (
                isinstance(instance, ExternalCortexEngine)
                and getattr(instance._endpoint, "protocol", None)
                is EndpointProtocol.OPENAI
            ):
                return False
            # All endpoints here are openai-protocol, so gate on the local-model
            # marker (the same flags as disable_tools / force_action_grammar).
            # Cloud openai endpoints (xai, openrouter) route reliably and must NOT
            # be mirrored.
            extra = getattr(instance._endpoint, "extra_config", None) or {}
            return bool(extra.get("disable_tools") or extra.get("force_action_grammar"))
        except Exception:
            return False

    async def _handle_message_action(
        self, action: dict, context: dict, bot, original_message
    ):
        """Handle message action execution using the interface registry."""

        payload = action.get("payload", {})
        text = payload.get("text") or payload.get("content") or ""
        interface_path = payload.get("interface_path")
        target = payload.get("target")
        thread_id = payload.get("thread_id")

        # Decompose interface_path into components
        if interface_path:
            parts = interface_path.split("/")
            if len(parts) >= 2:
                # interface_name_from_path = parts[0]
                target = parts[1]  # chat_id or user_id
                if len(parts) >= 3:
                    # Treat an empty third segment (trailing slash) as no thread
                    thread_id = parts[2].strip() or None
            else:
                log_warning(
                    f"[message_plugin] Invalid interface_path format: {interface_path}"
                )

        # Map action types to interface names
        action_type = action.get("type", "")
        interface_map = {
            "message_telegram_bot": "telegram_bot",
            "message_reddit": "reddit",
            "message_discord": "discord",
            "message_discord_bot": "discord_bot",
            "message_x": "x",
            "message_synth_webui": "synth_webui",
            "message_ollama_serve": "ollama_serve",
            "message_matrix_chat": "matrix_chat",
        }

        interface_name = interface_map.get(action_type)

        # interface_path is validated upstream against real routable targets
        # (e.g. Grillo's ELIGIBLE TARGETS list); the action `type` is free-form
        # LLM output and can name the wrong platform — models sometimes copy an
        # example action type verbatim regardless of which interface_path they
        # were actually given. When the two disagree, trust interface_path: it's
        # the one guaranteed to point at a real chat, not the target id from a
        # mismatched interface (e.g. sending a webui session id to Telegram).
        interface_name_from_path = (
            interface_path.split("/")[0] if interface_path else None
        )
        if (
            interface_name_from_path
            and interface_name_from_path in INTERFACE_REGISTRY
            and interface_name_from_path != interface_name
        ):
            if interface_name is not None:
                log_warning(
                    f"[message_plugin] action type '{action_type}' implies interface "
                    f"'{interface_name}' but interface_path='{interface_path}' points at "
                    f"'{interface_name_from_path}'; trusting interface_path"
                )
            interface_name = interface_name_from_path

        if not interface_name:
            interface_name = action.get("interface")

        if not interface_name:
            interface_name = (
                self.supported_interfaces[0]
                if self.supported_interfaces
                else "telegram_bot"
            )

        # openai_compat cortex routing safety: these small local models routinely
        # hallucinate the interface_path (e.g. "/channels/main") so a direct reply
        # never reaches the user. For these engines only — and only on normal user
        # turns (grillo/outreach keep their system-chosen target) — mirror the
        # reply back to the originating chat instead of trusting the model.
        if await self._should_mirror_origin_path(context, original_message):
            origin_interface = (
                (context.get("interface") if isinstance(context, dict) else None)
                or getattr(original_message, "interface", None)
                or interface_name
            )
            origin_chat = getattr(original_message, "chat_id", None)
            origin_thread = getattr(original_message, "thread_id", None) or getattr(
                original_message, "message_thread_id", None
            )
            if origin_chat is not None and (
                str(interface_name) != str(origin_interface)
                or str(target) != str(origin_chat)
            ):
                log_info(
                    "[message_plugin] openai_compat reply path mirror: "
                    f"{interface_name}/{target} -> {origin_interface}/{origin_chat}"
                )
                interface_name = origin_interface
                target = origin_chat
                thread_id = origin_thread

        log_debug(
            f"[message_plugin] Handling {action_type} via {interface_name}: {str(text)[:50]}..."
        )

        interface = INTERFACE_REGISTRY.get(interface_name)
        if not interface:
            log_warning(
                f"[message_plugin] Interface '{interface_name}' not found in registry"
            )
            return

        if not target:
            target = getattr(original_message, "chat_id", None)

        if not thread_id:
            # Accept both normalized `thread_id` and Telegram's native `message_thread_id`
            thread_id = getattr(original_message, "thread_id", None) or getattr(
                original_message, "message_thread_id", None
            )

        rebuilt_interface_path = None
        if interface_name and target:
            if thread_id:
                rebuilt_interface_path = f"{interface_name}/{target}/{thread_id}"
            else:
                rebuilt_interface_path = f"{interface_name}/{target}"

        # --- Grillo Suppression Logic ---
        # Outbound beats (observer) are EXEMPT from suppression: their purpose is
        # to initiate conversation, so silencing them defeats the feature.
        is_outbound = isinstance(context, dict) and is_outbound_beat(
            context.get("beat_type")
        )
        activity_log_id = None
        if isinstance(context, dict):
            activity_log_id = context.get("activity_log_id") or context.get(
                "grillo_activity_log_id"
            )

        if (
            not is_outbound
            and isinstance(context, dict)
            and (
                context.get("grillo_beat")
                or context.get("activity_log_id")
                or context.get("grillo_activity_log_id")
            )
        ):
            try:
                suppress_enabled = config_registry.get_value(
                    "GRILLO_SUPPRESS_INACTIVE",
                    True,
                    label="Suppress Grillo outbound messages when last message is from synth",
                    description=(
                        "When enabled, Grillo will skip outbound messages if the most recent message in the target chat was sent by the synth."
                    ),
                    group="grillo",
                    component="grillo",
                )
            except Exception:
                suppress_enabled = True

            try:
                from core.chat_history_cache import get_last_message, load_chat_history

                target_interface_path = (
                    rebuilt_interface_path
                    or interface_path
                    or f"{interface_name}/{target}"
                )

                # Check exemptions (WebUI)
                bypass = False
                if "webui" in (interface_name or "").lower():
                    bypass = True

                # Exemption: Trainer chats (simplified check without registry dependency to avoid circles)
                # If we assume trainer is handled elsewhere or Grillo won't spam trainer chats excessively

                if not bypass:
                    # Check if public chat logic
                    is_public = False
                    # Simple heuristic: Telegram negative IDs, or Discord/Reddit/Matrix channels
                    if interface_name == "telegram_bot" and target:
                        try:
                            if int(target) < 0:
                                is_public = True
                        except Exception:
                            pass
                    elif (
                        interface_name
                        in ["discord", "discord_bot", "reddit", "matrix", "matrix_chat"]
                        and target
                    ):
                        is_public = True

                    if is_public:
                        last = await get_last_message(target_interface_path)
                        if last:
                            sender_name = (
                                last.get("sender_name") or last.get("username") or ""
                            ).lower()
                            sender_id = str(
                                last.get("sender_id") or last.get("user_id") or ""
                            )

                            is_synth = sender_id == "self" or any(
                                k in sender_name for k in ["rekku", "synth", "bot"]
                            )

                            if is_synth and suppress_enabled:
                                log_info(
                                    f"[message_plugin] Suppressing Grillo message to {target_interface_path} (last msg from synth)"
                                )
                                try:
                                    from plugins.grillo.grillo_impl import GrilloPlugin

                                    await GrilloPlugin.record_suppressed_event(
                                        activity_log_id=activity_log_id,
                                        reason="last msg from synth",
                                    )
                                except Exception as suppression_error:
                                    log_debug(
                                        f"[message_plugin] Failed to record Grillo suppression event: {suppression_error}"
                                    )
                                return

                        similarity_threshold = float(
                            config_registry.get_value(
                                "GRILLO_DUP_SIMILARITY_THRESHOLD",
                                0.85,
                                label="Grillo Duplicate Similarity Threshold",
                                description=(
                                    "Similarity threshold above which Grillo suppresses outbound messages that are too close to recent public-chat text."
                                ),
                                value_type=float,
                                group="grillo",
                                component="grillo",
                            )
                        )
                        candidate_text = str(text or "").strip().lower()
                        if candidate_text:
                            recent_history = await load_chat_history(
                                target_interface_path
                            )
                            for entry in recent_history or []:
                                previous_text = (
                                    str(entry.get("text") or "").strip().lower()
                                )
                                if not previous_text:
                                    continue
                                similarity = SequenceMatcher(
                                    None, candidate_text, previous_text
                                ).ratio()
                                if similarity >= similarity_threshold:
                                    log_info(
                                        f"[message_plugin] Suppressing Grillo message to {target_interface_path} (similarity={similarity:.2f})"
                                    )
                                    try:
                                        from plugins.grillo.grillo_impl import (
                                            GrilloPlugin,
                                        )

                                        await GrilloPlugin.record_suppressed_event(
                                            activity_log_id=activity_log_id,
                                            reason=(
                                                f"duplicate similarity={similarity:.2f}"
                                            ),
                                        )
                                    except Exception as suppression_error:
                                        log_debug(
                                            f"[message_plugin] Failed to record Grillo suppression event: {suppression_error}"
                                        )
                                    return

            except Exception as e:
                log_debug(f"[message_plugin] Grillo suppression check failed: {e}")

        # --- Dispatch ---
        handler = INTERFACE_REGISTRY.get(interface_name)
        if not handler:
            log_warning(
                f"[message_plugin] Interface '{interface_name}' not found in registry"
            )
            return

        if not target:
            target = getattr(original_message, "chat_id", None)

        if not thread_id:
            # Accept both normalized `thread_id` and Telegram's native `message_thread_id`
            thread_id = getattr(original_message, "thread_id", None) or getattr(
                original_message, "message_thread_id", None
            )

        reply_to = None
        if (
            original_message
            and hasattr(original_message, "chat_id")
            and hasattr(original_message, "message_id")
            and target == getattr(original_message, "chat_id")
        ):
            reply_to = original_message.message_id

        # === Voice input forces a spoken reply ===
        # Historically, when the incoming message was an audio/voice note the reply
        # was always delivered as voice. Preserve that behaviour: if this turn was
        # voice-originated (is_voice_input / request_tts) and Vox is enabled, force
        # send_as_voice=true regardless of whether the model set it. This also
        # applies to the WebUI: Vox dispatches audio + lip-sync to the WebUI
        # client, so a voice-originated turn gets a spoken reply there too.
        try:
            _voice_input = isinstance(context, dict) and (
                context.get("is_voice_input") or context.get("request_tts")
            )
            if _voice_input and not payload.get("send_as_voice"):
                from plugins.vox_plugin import is_vox_enabled

                if is_vox_enabled():
                    log_info(
                        "[message_plugin] Voice input detected — forcing "
                        "send_as_voice=true for spoken reply."
                    )
                    payload["send_as_voice"] = True
        except Exception as e:
            log_debug(f"[message_plugin] Voice-input force check failed: {e}")

        # === send_as_voice: deliver this reply as a spoken voice note ===
        # message_* actions are routed through this plugin (not the interface's
        # own send_message branch in action_parser), so the send_as_voice routing
        # must live here too. When set, hand the text to Vox (TTS): Vox synthesises
        # the audio AND dispatches both the audio and caption to the interface, so
        # we must NOT also call handler.send_message() (that would duplicate the
        # text). If Vox is disabled or fails, Vox.speak() falls back to sending the
        # text itself, so a plain reply is still delivered.
        if bool(payload.get("send_as_voice")):
            try:
                from core.core_initializer import PLUGIN_REGISTRY
                from plugins.vox_plugin import VoxPlugin

                vox_plugin = None
                if isinstance(PLUGIN_REGISTRY, dict):
                    for p in PLUGIN_REGISTRY.values():
                        if isinstance(p, VoxPlugin):
                            vox_plugin = p
                            break

                if vox_plugin is not None:
                    voice_ip = (
                        rebuilt_interface_path
                        or interface_path
                        or getattr(original_message, "interface_path", None)
                    )
                    log_info(
                        f"[message_plugin] 🎙️ send_as_voice=true — routing '{action_type}' "
                        f"to Vox for interface '{interface_name}'"
                    )
                    await vox_plugin.speak(
                        text=text or "",
                        interface_path=voice_ip,
                        context=context,
                        original_message=original_message,
                    )
                    return
                log_warning(
                    "[message_plugin] send_as_voice=true but no Vox plugin loaded "
                    "— falling back to plain text send."
                )
            except Exception as e:
                log_error(
                    f"[message_plugin] send_as_voice routing failed: {repr(e)} "
                    "— falling back to plain text send."
                )

        send_payload = {"text": text, "target": target}
        if thread_id is not None:
            send_payload["thread_id"] = thread_id
        if rebuilt_interface_path:
            send_payload["interface_path"] = rebuilt_interface_path

        try:
            send_result = await handler.send_message(
                send_payload, original_message=original_message
            )
            if send_result is False:
                raise RuntimeError(
                    f"{interface_name} send_message() reported delivery failure"
                )
            log_info(
                f"[message_plugin] Message successfully sent to {target} (thread: {thread_id}, reply_to: {reply_to})"
            )

        except Exception as e:
            log_error(
                f"[message_plugin] Failed to send message via {interface_name}: {e}"
            )
            raise


# Export the plugin class
__all__ = ["MessagePlugin"]

# Define the plugin class for automatic loading
PLUGIN_CLASS = MessagePlugin
