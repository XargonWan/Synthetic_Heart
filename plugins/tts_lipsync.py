import os
import aiohttp
import logging
import base64
import time
import json
import wave
import struct
import numpy as np
from pathlib import Path
from core.ai_plugin_base import AIPluginBase
from core.core_initializer import core_initializer, register_plugin, INTERFACE_REGISTRY

# Configure Logging
logger = logging.getLogger("tts_lipsync_plugin")

class TTSLipSyncPlugin(AIPluginBase):
    display_name = "TTS Lip Sync"
    version = "1.1.0"

    def __init__(self):
        super().__init__()
        # Register the plugin
        register_plugin("tts_lipsync", self)
        core_initializer.register_plugin("tts_lipsync")
        
        # Configuration
        self.tts_url = "http://127.0.0.1:8001/tts_stream"
        self.rvc_url = "http://127.0.0.1:8002/convert_stream"
        
        # Ensure static directory exists for saving audio (relative to project root)
        self.static_dir = Path("res/synth_webui/static/tts_cache")
        self.static_dir.mkdir(parents=True, exist_ok=True)

        # Initialize deduplication state
        self.last_tts_text = None
        self.last_tts_time = 0

    @staticmethod
    def get_supported_action_types() -> list[str]:
        return ["tts_speak"]

    def get_supported_actions(self) -> dict:
        return {
            "tts_speak": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The text to speak"
                        },
                        "emotion": {
                            "type": "string",
                            "description": "Optional emotion for the speech (e.g., happy, sad, angry)",
                            "enum": ["happy", "sad", "angry", "curious", "neutral"]
                        },
                        "use_rvc": {
                            "type": "boolean",
                            "description": "Whether to pass the audio through RVC voice conversion",
                            "default": True
                        }
                    },
                    "required": ["text"]
                },
                "brief": "Generate speech from text using the local TTS and RVC servers.",
                "examples": {
                    "description": "Generates audio from text and triggers the frontend to play it with lip sync.",
                    "examples": [
                        {
                            "scenario": "Speak with RVC voice",
                            "payload": {"text": "Hello, how are you?", "emotion": "happy", "use_rvc": True}
                        }
                    ]
                }
            }
        }

    def validate_payload(self, action_type: str, payload: dict) -> list[str]:
        """Validate action payload before execution."""
        errors = []
        if action_type == "tts_speak" and not payload.get("text"):
            errors.append("payload.text is required")
        return errors

    async def handle_custom_action(self, action_type: str, payload: dict):
        if action_type == "tts_speak":
            text = payload.get("text")
            emotion = payload.get("emotion")
            use_rvc = payload.get("use_rvc", True)

            # Deduplication Check
            now = time.time()
            if self.last_tts_text == text and (now - self.last_tts_time) < 2.0:
                logger.warning(f"Ignoring duplicate TTS request for: {text[:30]}...")
                return {"status": "skipped", "message": "Duplicate request ignored"}
            
            self.last_tts_text = text
            self.last_tts_time = now

            # 1. Check TTS Status / Generate
            try:
                audio_path = await self._generate_audio(text, emotion, use_rvc)
                if audio_path:
                    # Success: Broadcast event to frontend
                    audio_url = f"/static/tts_cache/{audio_path}"
                    
                    # Find WebUI interface
                    webui = INTERFACE_REGISTRY.get("synth_webui") or INTERFACE_REGISTRY.get("Web UI")
                    if webui and hasattr(webui, "broadcast_event"):
                        await webui.broadcast_event("synth:tts-play", {
                            "audio_url": audio_url,
                            "text": text
                        })
                        logger.info(f"Broadcasted synth:tts-play event for {audio_url}")
                    else:
                        logger.warning("WebUI interface not found or broadcast_event missing. Audio generated but not played.")

                    return {
                        "status": "success",
                        "audio_url": audio_url,
                        "text": text,
                        "action": "play_audio"
                    }
                else:
                    logger.warning("TTS Generation failed (returned None). Continuing without audio.")
                    return {"status": "warning", "message": "TTS failed", "text": text}
            
            except Exception as e:
                logger.error(f"TTS Plugin Error: {e}", exc_info=True)
                return {"status": "error", "error": str(e), "text": text}

        return {"error": "Unknown action"}

    async def _generate_audio(self, text, emotion=None, use_rvc=True):
        """
        Calls TTS -> RVC (optional) -> Saves WAV.
        """
        payload = {
            "text": text,
            # Use the specific reference audio requested by the user
            "voice_wav": r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav", 
            # Default to neutral if no emotion is provided to avoid "excited/panic" spam
            "use_emo_text": bool(emotion),
            "emo_text": emotion if emotion else "neutral"
        }

        raw_audio = b""
        
        # 1. Call TTS
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.tts_url, json=payload, timeout=30) as resp:
                    if resp.status == 200:
                        raw_audio = await resp.read() # Read all into memory
                        if not raw_audio:
                            logger.error("TTS returned empty audio")
                            return None
                    else:
                        logger.error(f"TTS Server returned status {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"TTS Request Error: {e}")
            return None

        # 2. Call RVC (If enabled)
        if use_rvc:
            try:
                async with aiohttp.ClientSession() as session:
                    # RVC expects raw Int16 PCM in body
                    async with session.post(self.rvc_url, data=raw_audio, timeout=60) as resp:
                        if resp.status == 200:
                            rvc_audio = await resp.read()
                            if rvc_audio:
                                # RVC returns Float32 bytes, convert back to Int16 for WAV
                                audio_f32 = np.frombuffer(rvc_audio, dtype=np.float32)
                                
                                # --- Normalization ---
                                max_val = np.max(np.abs(audio_f32))
                                if max_val > 0:
                                    # Normalize to 50% volume (reduced from 90% due to user feedback)
                                    audio_f32 = (audio_f32 / max_val) * 0.5
                                
                                audio_i16 = (audio_f32 * 32767).astype(np.int16)
                                raw_audio = audio_i16.tobytes()
                                logger.info(f"RVC conversion successful. Normalized peak: {max_val:.2f}")
                            else:
                                logger.warning("RVC returned empty audio. Using TTS audio.")
                        else:
                            logger.warning(f"RVC Server returned status {resp.status}. Using TTS audio.")
            except Exception as e:
                logger.warning(f"RVC Request Error (skipping RVC): {e}")

        # 3. Save as valid WAV
        try:
            timestamp = int(time.time())
            filename = f"tts_{timestamp}.wav"
            filepath = self.static_dir / filename
            
            with wave.open(str(filepath), 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2) # 16-bit
                wf.setframerate(22050)
                wf.writeframes(raw_audio)
                
            logger.info(f"Audio saved to {filepath} (Size: {len(raw_audio)} bytes)")
            return filename
        except Exception as e:
            logger.error(f"Failed to save WAV: {e}")
            return None

PLUGIN_CLASS = TTSLipSyncPlugin
