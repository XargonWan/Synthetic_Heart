import asyncio
import re
import time
from pathlib import Path
from typing import List

import requests

from core.ai_plugin_base import AIPluginBase
from core.core_initializer import register_plugin, INTERFACE_REGISTRY
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
from core.logging_utils import log_info, log_warning, log_error, log_debug


register_exposed_var(
    "TTS_ENDPOINTS",
    label="TTS Endpoints",
    default="",
    value_type=str,
    ui_type="string",
    description="Comma-separated list of TTS HTTP endpoints.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)

register_exposed_var(
    "TTS_TIMEOUT_SECONDS",
    label="TTS Timeout (s)",
    default=300,
    value_type=int,
    ui_type="number",
    description="Timeout for TTS HTTP requests in seconds.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)

register_exposed_var(
    "TTS_OUTPUT_DIR",
    label="TTS Output Directory",
    default="res/synth_webui/static/audio/tts",
    value_type=str,
    ui_type="string",
    description="Directory for generated TTS audio files.",
    scope="plugins",
    component="tts_lipsync",
    advanced=True,
)

# Expose an enable switch for WebUI configuration
register_exposed_var(
    "TTS_ENABLED",
    label="TTS Enabled",
    default=False,
    value_type=bool,
    ui_type="boolean",
    description="Enable or disable TTS feature from the WebUI.",
    scope="plugins",
    component="tts_lipsync",
    advanced=False,
)

# Expose fallback behaviour for text-only fallback when TTS generation fails
register_exposed_var(
    "TTS_FALLBACK_TO_TEXT",
    label="TTS Fallback to Text",
    default=True,
    value_type=bool,
    ui_type="boolean",
    description="If true, on TTS generation failure the system will send a text-only fallback.",
    scope="plugins",
    component="tts_lipsync",
    advanced=False,
)


class TTSLipSyncPlugin(AIPluginBase):
    display_name = "TTS Lip Sync"

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle custom actions like tts_speak."""
        if action_type == "tts_speak":
            text = payload.get("text")
            emo = payload.get("emo")  # Optional emotion
            filename = await self._generate_audio(text, emo)
            if filename:
                # Return success with a dummy URL that contains the filename
                # execute_action will extract the filename using split('/')[-1]
                return {"status": "success", "audio_url": f"http://internal/{filename}"}
            return {"status": "error", "message": "Failed to generate audio"}
        return {"status": "error", "message": f"Unsupported action type: {action_type}"}

    @staticmethod
    def get_supported_actions() -> dict:
        """Register supported actions."""
        return {
            "tts_speak": {
                "description": "Generate speech from text and dispatch to the active interface.",
                "required_fields": ["text"],
                "optional_fields": ["emo"],
            }
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Provide detailed instructions for the LLM."""
        if action_name == "tts_speak":
            return {
                "description": "Generate speech audio from text. The audio will be automatically sent to the chat.",
                "payload": {
                    "text": {
                        "type": "string",
                        "description": "The text content to speak.",
                    },
                    "emo": {
                        "type": "string",
                        "description": "Optional emotion style.",
                        "optional": True,
                    },
                },
            }
        return {}

    def __init__(self):
        super().__init__()
        register_plugin("tts_lipsync", self)
        log_info("[tts_lipsync] Plugin initialized")

        # Load runtime config and set internal attributes
        self.refresh_config()

    def refresh_config(self):
        """Read exposed variables from the config registry and update internal state.
        This allows WebUI changes to take effect without plugin reloads (best-effort).
        """
        try:
            # Read enabled flag first
            enabled = config_registry.get_value(
                "TTS_ENABLED",
                False,
                value_type=bool,
                group="plugins",
                component="tts_lipsync",
            )

            # Read endpoints and parse
            raw_endpoints = config_registry.get_value(
                "TTS_ENDPOINTS",
                "",
                value_type=str,
                group="plugins",
                component="tts_lipsync",
            )
            endpoints = (
                [e.strip() for e in str(raw_endpoints).split(",") if e.strip()]
                if raw_endpoints
                else []
            )

            # Read timeout and output dir
            timeout_s = config_registry.get_value(
                "TTS_TIMEOUT_SECONDS",
                60,
                value_type=int,
                group="plugins",
                component="tts_lipsync",
            )
            output_dir = Path(
                config_registry.get_value(
                    "TTS_OUTPUT_DIR",
                    "res/synth_webui/static/audio/tts",
                    value_type=str,
                    group="plugins",
                    component="tts_lipsync",
                )
            )

            fallback_to_text = config_registry.get_value(
                "TTS_FALLBACK_TO_TEXT",
                True,
                value_type=bool,
                group="plugins",
                component="tts_lipsync",
            )

            self.endpoints = endpoints
            # Plugin considered enabled only when user enabled AND endpoints configured
            self.enabled = bool(enabled) and bool(endpoints)
            self.timeout_s = int(timeout_s)
            self.output_dir = output_dir
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.fallback_to_text = bool(fallback_to_text)

            if not self.endpoints:
                log_info("[tts_lipsync] No TTS endpoints configured")

            if not bool(enabled):
                log_info("[tts_lipsync] TTS disabled via WebUI (TTS_ENABLED=False)")

        except Exception as e:
            log_warning(f"[tts_lipsync] Failed to refresh config: {e}")

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute TTS action and optionally dispatch to interface."""
        # Check if this is a Grillo internal beat (not outreach) - skip TTS
        is_grillo_internal = context.get("grillo_beat", False) and context.get(
            "beat_type"
        ) not in ("outreach", None)
        if is_grillo_internal:
            log_info(
                f"[tts_lipsync] Skipping TTS for Grillo internal beat: {context.get('beat_type')}"
            )
            return {"status": "skipped", "reason": "grillo_internal_beat"}

        # Check if message text is an internal confirmation (plugin result feedback)
        # These should not trigger TTS as they are system confirmations
        payload = action.get("payload", {})
        msg_text = payload.get("text", "") if isinstance(payload, dict) else ""
        if isinstance(msg_text, str) and msg_text:
            internal_patterns = [
                "I have successfully recorded",
                "diary entry",
                "System check complete",
                "system initialized",
                "action executed",
                "successfully completed",
            ]
            if any(
                pattern.lower() in msg_text.lower() for pattern in internal_patterns
            ):
                log_info(
                    f"[tts_lipsync] Skipping TTS for internal confirmation: '{msg_text[:50]}...'"
                )
                return {"status": "skipped", "reason": "internal_confirmation"}

        # Refresh configuration (in case WebUI changed settings)
        self.refresh_config()

        # Defer to VoxPlugin when the new Vox TTS subsystem is active.
        # tts_lipsync is the legacy handler; VoxPlugin is canonical when an
        # active engine is configured (i.e. ACTIVE_VOX_ENGINE != "disabled").
        try:
            active = str(
                config_registry.get_value(
                    "ACTIVE_VOX_ENGINE",
                    "",
                    value_type=str,
                    group="plugins",
                    component="vox_plugin",
                )
            )
            if active and active != "disabled":
                log_info(
                    "[tts_lipsync] Active Vox engine present — deferring tts_speak to VoxPlugin"
                )
                return {"status": "skipped", "reason": "vox_active"}
        except Exception:
            pass

        # If plugin is not enabled (either user disabled or no endpoints configured), skip TTS entirely
        if not getattr(self, "enabled", False):
            log_info(
                "[tts_lipsync] TTS is disabled (TTS_ENABLED=False or no endpoints); skipping TTS execution"
            )
            return {"status": "skipped", "reason": "tts_disabled"}

        # 1. Generate Audio via standard handler
        result = await self.handle_custom_action(action.get("type"), payload)

        if result.get("status") != "success":
            log_info(
                f"[tts_lipsync] TTS generation failed ({result.get('message')}); checking for fallback text message."
            )

            # If we have merged text from a message action, send it as text-only
            merged_text = payload.get("__merged_text")
            if merged_text and getattr(self, "fallback_to_text", True):
                log_info(
                    f"[tts_lipsync] Sending text-only fallback (TTS failed): '{merged_text[:50]}...'"
                )

                # Get interface_path from context or original_message
                interface_path = context.get("interface_path")
                if not interface_path and original_message:
                    interface_path = getattr(original_message, "interface_path", None)

                if interface_path:
                    try:
                        from core.interface_path_utils import parse_interface_path

                        iface_name, _ = parse_interface_path(interface_path)
                        target_iface = INTERFACE_REGISTRY.get(iface_name)

                        if target_iface and hasattr(target_iface, "send_message"):
                            await target_iface.send_message(
                                {
                                    "interface_path": interface_path,
                                    "text": merged_text,
                                }
                            )
                            log_info(
                                "[tts_lipsync] ✅ Text-only fallback sent successfully"
                            )
                        else:
                            log_warning(
                                f"[tts_lipsync] Could not find interface {iface_name} for text fallback"
                            )
                    except Exception as e:
                        log_error(f"[tts_lipsync] Failed to send text fallback: {e}")
                else:
                    log_warning(
                        "[tts_lipsync] No interface_path available for text fallback"
                    )

            return result

        # 2. Extract info
        audio_url = result.get("audio_url")
        if not audio_url:
            return result

        filename = audio_url.split("/")[-1]
        local_path = self.output_dir / filename

        if not local_path.exists():
            log_warning(f"[tts_lipsync] Generated audio file not found at {local_path}")

            # Send text-only fallback if merged text is present
            merged_text = payload.get("__merged_text")
            if merged_text:
                log_info(
                    "[tts_lipsync] Audio file not found, sending text-only fallback"
                )
                interface_path = context.get("interface_path")
                if not interface_path and original_message:
                    interface_path = getattr(original_message, "interface_path", None)

                if interface_path:
                    try:
                        from core.interface_path_utils import parse_interface_path

                        iface_name, _ = parse_interface_path(interface_path)
                        target_iface = INTERFACE_REGISTRY.get(iface_name)

                        if target_iface and hasattr(target_iface, "send_message"):
                            await target_iface.send_message(
                                {
                                    "interface_path": interface_path,
                                    "text": merged_text,
                                }
                            )
                            log_info(
                                "[tts_lipsync] ✅ Text-only fallback sent (audio file missing)"
                            )
                    except Exception:
                        pass

            return result

        # 3. Dispatch to Interface
        interface_path = context.get("interface_path")

        # Fallback: check original_message if context is missing path
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)
            if interface_path:
                log_info(
                    f"[tts_lipsync] Recovered interface_path from original_message: {interface_path}"
                )

        log_debug(
            f"[tts_lipsync] execute_action context keys: {list(context.keys())}, interface_path={interface_path}"
        )

        if interface_path:
            try:
                from core.interface_path_utils import parse_interface_path

                iface_name, _ = parse_interface_path(interface_path)

                target_iface = INTERFACE_REGISTRY.get(iface_name)
                if target_iface:
                    log_info(
                        f"[tts_lipsync] Dispatching audio to {iface_name} ({interface_path})"
                    )

                    if iface_name == "discord_bot" and hasattr(
                        target_iface, "send_message"
                    ):
                        # Send audio with text (merged from message action if present)
                        msg_payload = {
                            "interface_path": interface_path,
                            "audio": str(local_path),
                        }

                        # Use merged text if available, fallback to original text
                        text_caption = payload.get("__merged_text") or payload.get(
                            "text"
                        )
                        if text_caption:
                            msg_payload["text"] = text_caption

                        await target_iface.send_message(msg_payload)

                    elif iface_name == "synth_webui" and hasattr(
                        target_iface, "send_tts_audio"
                    ):
                        # WebUI: send text message + TTS audio for lip-sync playback
                        _, levels = parse_interface_path(interface_path)
                        session_id = levels[0] if levels else None

                        if session_id:
                            text_caption = payload.get("__merged_text") or payload.get(
                                "text"
                            )
                            success = await target_iface.send_tts_audio(
                                session_id=session_id,
                                audio_path=str(local_path),
                                text=text_caption,
                            )
                            if success:
                                log_info(
                                    f"[tts_lipsync] TTS audio dispatched to WebUI session {session_id}"
                                )
                            else:
                                log_warning(
                                    f"[tts_lipsync] Failed to dispatch TTS audio to WebUI session {session_id}"
                                )
                        else:
                            log_warning(
                                f"[tts_lipsync] Could not extract session_id from interface_path: {interface_path}"
                            )

                    elif iface_name == "telegram_bot":
                        # Use audio_telegram_bot which maps to send_voice
                        if hasattr(target_iface, "execute_action"):
                            # We need to parse chat_id from interface_path
                            _, levels = parse_interface_path(interface_path)
                            chat_id = levels[0] if levels else None

                            if chat_id:
                                # Use text as caption
                                text_caption = payload.get(
                                    "__merged_text"
                                ) or payload.get("text")
                                await target_iface.execute_action(
                                    {
                                        "type": "audio_telegram_bot",
                                        "payload": {
                                            "interface_path": interface_path,
                                            "audio": str(local_path),
                                            "caption": text_caption,
                                        },
                                    },
                                    context,
                                    bot,
                                    original_message,
                                )
            except Exception as e:
                log_error(f"[tts_lipsync] Auto-dispatch failed callback: {e}")
        else:
            log_warning(
                "[tts_lipsync] No interface_path in context or original_message; cannot dispatch audio automatically."
            )

        return result

    def _load_endpoints(self) -> List[str]:
        raw = config_registry.get_value(
            "TTS_ENDPOINTS",
            "",
            value_type=str,
            group="plugins",
            component="tts_lipsync",
        )

        log_debug(f"[tts_lipsync] Loading endpoints. Raw config: '{raw}'")

        # Parse first
        endpoints = []
        if raw:
            endpoints = [e.strip() for e in str(raw).split(",") if e.strip()]

        # If empty (either raw was None/empty OR it parsed to empty list), do NOT use defaults
        if not endpoints:
            log_info("[tts_lipsync] No custom endpoints configured")
            return []

        return endpoints

    def _get_voice_ref(self, endpoint: str) -> str:
        """Get the appropriate voice reference path for a specific endpoint."""
        if "192.168.1.69" in endpoint:
            # Server VM (n2)
            return r"F:\0synth\0synth\reference\2b_ref.wav"
        # Dev Machine (n1)
        return r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav"

    async def _generate_audio(
        self, text: str, emotion: str | None = None
    ) -> str | None:
        endpoints = self._load_endpoints()
        if not endpoints:
            log_warning("[tts_lipsync] No TTS endpoints configured")
            return None

        # Clean emojis and extra symbols for TTS only
        # This regex removes most unicode emojis and symbols range
        clean_text = re.sub(r"[^\w\s,.!?;:\'\-\"”’]+", "", text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        if not clean_text:
            return None

        audio_bytes = None

        for endpoint in endpoints:
            # Construct payload dynamically for each endpoint
            voice_wav = self._get_voice_ref(endpoint)
            payload = {
                "text": clean_text,
                "voice_wav": voice_wav,
                "use_emo_text": False,
            }

            # Dynamic timeout: tight timeout for primary .6 server to failover fast
            current_timeout = self.timeout_s
            if "192.168.1.6:" in endpoint:
                current_timeout = 2  # 2 seconds max for primary

            try:
                log_debug(
                    f"[tts_lipsync] Requesting TTS from {endpoint} (timeout={current_timeout}s)"
                )
                audio_bytes = await asyncio.to_thread(
                    _post_tts,
                    endpoint,
                    payload,
                    current_timeout,
                )
                if audio_bytes:
                    log_debug(f"[tts_lipsync] Success from {endpoint}")
                    break
            except Exception as e:
                log_warning(f"[tts_lipsync] TTS request failed for {endpoint}: {e}")

        if not audio_bytes:
            log_error("[tts_lipsync] All TTS endpoints failed")
            return None

        filename = f"tts_{int(time.time())}.wav"
        out_path = self.output_dir / filename
        try:
            # Check if RIFF header is present (already WAV)
            if audio_bytes.startswith(b"RIFF"):
                with open(out_path, "wb") as f:
                    f.write(audio_bytes)
            else:
                # Server returns raw PCM 16-bit 22050Hz Mono. Wrap in WAV.
                import wave

                with wave.open(str(out_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(22050)
                    wav_file.writeframes(audio_bytes)
        except Exception as e:
            log_error(f"[tts_lipsync] Failed to write audio file: {e}")
            return None

        return filename


def _post_tts(endpoint: str, payload: dict, timeout_s: int) -> bytes | None:
    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout_s)
        if resp.status_code != 200:
            log_warning(
                f"[tts_lipsync] Request to {endpoint} failed (Status: {resp.status_code}). Response: {resp.text[:200]}"
            )
            return None
        return resp.content or None
    except Exception as e:
        log_warning(f"[tts_lipsync] _post_tts connection error: {e}")
        return None


PLUGIN_CLASS = TTSLipSyncPlugin
