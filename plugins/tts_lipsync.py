import asyncio
import os
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
    default=30,
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
                    "text": {"type": "string", "description": "The text content to speak."},
                    "emo": {"type": "string", "description": "Optional emotion style.", "optional": True}
                }
            }
        return {}
    
    def __init__(self):
        super().__init__()
        register_plugin("tts_lipsync", self)
        log_info("[tts_lipsync] Plugin initialized")

        self.endpoints = self._load_endpoints()
        self.timeout_s = config_registry.get_value(
            "TTS_TIMEOUT_SECONDS",
            30,
            value_type=int,
            group="plugins",
            component="tts_lipsync",
        )
        self.output_dir = Path(
            config_registry.get_value(
                "TTS_OUTPUT_DIR",
                "res/synth_webui/static/audio/tts",
                value_type=str,
                group="plugins",
                component="tts_lipsync",
            )
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Execute TTS action and optionally dispatch to interface."""
        # 1. Generate Audio via standard handler
        payload = action.get("payload", {})
        result = await self.handle_custom_action(action.get("type"), payload)
        
        if result.get("status") != "success":
            return result
            
        # 2. Extract info
        audio_url = result.get("audio_url")
        if not audio_url:
            return result
            
        filename = audio_url.split('/')[-1]
        local_path = self.output_dir / filename
        
        if not local_path.exists():
            log_warning(f"[tts_lipsync] Generated audio file not found at {local_path}")
            return result

        # 3. Dispatch to Interface
        interface_path = context.get("interface_path")
        
        # Fallback: check original_message if context is missing path
        if not interface_path and original_message:
            interface_path = getattr(original_message, "interface_path", None)
            if interface_path:
                log_info(f"[tts_lipsync] Recovered interface_path from original_message: {interface_path}")

        log_debug(f"[tts_lipsync] execute_action context keys: {list(context.keys())}, interface_path={interface_path}")
        
        if interface_path:
            try:
                from core.interface_path_utils import parse_interface_path
                iface_name, _ = parse_interface_path(interface_path)
                
                target_iface = INTERFACE_REGISTRY.get(iface_name)
                if target_iface:
                    log_info(f"[tts_lipsync] Dispatching audio to {iface_name} ({interface_path})")
                    
                    if iface_name == 'discord_bot' and hasattr(target_iface, 'send_message'):
                        # Send audio only (text sent by separate message_discord_bot action)
                        msg_payload = {
                            "interface_path": interface_path,
                            "audio": str(local_path)
                        }
                        
                        # Use merged text if available (from message_chain merger)
                        # This ensures we send text+audio in one message ONLY if we merged the original text action.
                        text_caption = payload.get("__merged_text")
                        if text_caption:
                            msg_payload["text"] = text_caption

                        await target_iface.send_message(msg_payload)

                    elif iface_name == 'telegram_bot':
                        # Try to find a send_audio type method or use audio_telegram_bot logic
                        if hasattr(target_iface, 'execute_action'):
                             # We need to parse chat_id from interface_path
                             # interface_path = telegram_bot/chat_id/...
                             _, levels = parse_interface_path(interface_path)
                             chat_id = levels[0] if levels else None
                             
                             if chat_id:
                                 await target_iface.execute_action({
                                     "type": "audio_telegram_bot",
                                     "payload": {
                                         "interface_path": interface_path,
                                         "audio": str(local_path),
                                         "caption": payload.get("__merged_text")
                                     }
                                 }, context, bot, original_message)
            except Exception as e:
                log_error(f"[tts_lipsync] Auto-dispatch failed callback: {e}")
        else:
             log_warning("[tts_lipsync] No interface_path in context or original_message; cannot dispatch audio automatically.")
                
        return result

    def _load_endpoints(self) -> List[str]:
        raw = config_registry.get_value(
            "TTS_ENDPOINTS",
            "",
            value_type=str,
            group="plugins",
            component="tts_lipsync",
        )
        if not raw:
            # Default to user-provided IPs if config is empty
            return [
                "http://127.0.0.1:8001/tts_stream",
                "http://localhost:8001/tts_stream",
                "http://192.168.1.6:8001/tts_stream",
                "http://192.168.1.69:8001/tts_stream"
            ]
        return [e.strip() for e in str(raw).split(",") if e.strip()]

    def _get_voice_ref(self, endpoint: str) -> str:
        """Get the appropriate voice reference path for a specific endpoint."""
        if "192.168.1.69" in endpoint:
            # Secondary server (EVO/Documents)
            return r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav"
        # Primary server (Default/F: dict)
        return r"F:\0synth\0synth\reference\2b_ref.wav"

    async def _generate_audio(self, text: str, emotion: str | None = None) -> str | None:
        endpoints = self._load_endpoints()
        if not endpoints:
            log_warning("[tts_lipsync] No TTS endpoints configured")
            return None

        # Clean emojis and extra symbols for TTS only
        # This regex removes most unicode emojis and symbols range
        clean_text = re.sub(r'[^\w\s,.!?;:\'\-\"”’]+', '', text)
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
                "use_emo_text": False
            }
            
            # Dynamic timeout: tight timeout for primary .6 server to failover fast
            current_timeout = self.timeout_s
            if "192.168.1.6:" in endpoint:
                current_timeout = 2  # 2 seconds max for primary
            
            try:
                log_debug(f"[tts_lipsync] Requesting TTS from {endpoint} (timeout={current_timeout}s)")
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
            if audio_bytes.startswith(b'RIFF'):
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
    resp = requests.post(endpoint, json=payload, timeout=timeout_s)
    if resp.status_code != 200:
        return None
    return resp.content or None


PLUGIN_CLASS = TTSLipSyncPlugin