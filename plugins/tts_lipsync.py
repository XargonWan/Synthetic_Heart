import os
import re
import aiohttp
import base64
import time
import json
import wave
import struct
import numpy as np
import asyncio
from pathlib import Path
from core.ai_plugin_base import AIPluginBase
from core.core_initializer import core_initializer, register_plugin, INTERFACE_REGISTRY
from core.logging_utils import log_info, log_error, log_warning, log_debug

class TTSLipSyncPlugin(AIPluginBase):
    display_name = "TTS Lip Sync"
    version = "1.1.0"

    def __init__(self):
        super().__init__()
        
        log_info("[tts_lipsync] 🟢 Plugin initialized")
        # Register the plugin handles component tracking
        register_plugin("tts_lipsync", self)
        
        # Configuration - Primary and Fallback Servers with separate reference paths
        # Server 1: Primary (Remote machine)
        # Server 2: Fallback (Local/Secondary machine)
        self.tts_servers = [
            {
                "url": "http://192.168.1.6:8001/tts_stream",
                "ref_path": r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav"
            },
            {
                "url": "http://192.168.1.69:8001/tts_stream",
                "ref_path": r"F:\0synth\0synth\reference\2b_ref.wav"
            }
        ]
        
        # Ensure static directory exists for saving audio (relative to project root)
        self.static_dir = Path("res/synth_webui/static/audio/tts")
        self.static_dir.mkdir(parents=True, exist_ok=True)

        # Initialize deduplication state
        self.last_tts_text = None
        self.last_tts_time = 0
        
        # Log configuration
        log_info(f"[tts_lipsync] Configured {len(self.tts_servers)} TTS servers.")
        # We only verify the local reference path if it matches the current environment to avoid confusion
        local_ref_candidate = r"F:\0synth\0synth\reference\2b_ref.wav"
        if os.path.exists(local_ref_candidate):
             log_info(f"[tts_lipsync] Local reference audio found at {local_ref_candidate}")
        else:
             # Just a debug log, as it might be running on the other machine? 
             # Actually user said "the 2nd server path should be F:..." which implies this IS the 2nd server environment 
             # if we are editing files there.
             log_warning(f"[tts_lipsync] Local reference audio NOT FOUND at {local_ref_candidate}")

    async def start(self):
        """Startup hook to test TTS connection."""
        log_info("[tts_lipsync] 🚀 Start method called")
        await asyncio.sleep(1)  # Reduce wait
        log_info("[tts_lipsync] Performing startup TTS test...")
        try:
            # We don't broadcast to WebUI here as it might not be connected yet,
            # but we verify the audio generation and server connection.
            result = await self._generate_audio("Synthetic Heart TTS system online.", emotion="happy")
            if result:
                log_info(f"[tts_lipsync] Startup test successful: generated {result}")
            else:
                log_error("[tts_lipsync] Startup test failed: No audio generated")
        except Exception as e:
            log_error(f"[tts_lipsync] Startup test exception: {e}")


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
                        }
                    },
                    "required": ["text"]
                },
                "brief": "Generate speech from text using the local TTS server. REQUIRED for the user to hear you.",
                "examples": {
                    "description": "Call this action to speak to the user. You should use this alongside any text reply if you want to be heard.",
                    "examples": [
                        {
                            "scenario": "Speak with emotion",
                            "payload": {"text": "Hello, how are you?", "emotion": "happy"}
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
        log_info(f"[tts_lipsync] Handling custom action: {action_type} with payload: {payload}")
        if action_type == "tts_speak":
            text = payload.get("text")
            emotion = payload.get("emotion")
            
            # Deduplication Check
            now = time.time()
            if self.last_tts_text == text and (now - self.last_tts_time) < 2.0:
                log_warning(f"[tts_lipsync] Ignoring duplicate TTS request for: {text[:30]}...")
                return {"status": "skipped", "message": "Duplicate request ignored"}
            
            self.last_tts_text = text
            self.last_tts_time = now

            # 1. Check TTS Status / Generate
            try:
                log_info(f"[tts_lipsync] Generating audio for text: '{text[:50]}...' (emotion: {emotion})")
                audio_path = await self._generate_audio(text, emotion)
                if audio_path:
                    # Success: Broadcast event to frontend
                    audio_url = f"/static/audio/tts/{audio_path}"
                    
                    # Find WebUI interface
                    webui = INTERFACE_REGISTRY.get("synth_webui") or INTERFACE_REGISTRY.get("Web UI")
                    if webui and hasattr(webui, "broadcast_event"):
                        await webui.broadcast_event("synth:tts-play", {
                            "audio_url": audio_url,
                            "text": text
                        })
                        log_info(f"[tts_lipsync] Broadcasted synth:tts-play event for {audio_url}")
                    else:
                        log_warning(f"[tts_lipsync] WebUI interface not found or broadcast_event missing. Audio generated but not played. Registry keys: {list(INTERFACE_REGISTRY.keys())}")

                    return {
                        "status": "success",
                        "audio_url": audio_url,
                        "text": text,
                        "action": "play_audio"
                    }
                else:
                    log_warning("[tts_lipsync] TTS Generation failed (returned None). Continuing without audio.")
                    return {"status": "warning", "message": "TTS failed", "text": text}
            
            except Exception as e:
                log_error(f"[tts_lipsync] TTS Plugin Error: {e}", e)
                return {"status": "error", "error": str(e), "text": text}

        log_error(f"[tts_lipsync] Unknown action received: {action_type}")
        return {"error": "Unknown action"}

    # Emotion Vector Mapping for IndexTTS2
    # Order: [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
    EMOTION_VECTORS = {
        "happy":        [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "angry":        [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "sad":          [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "afraid":       [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        "disgusted":    [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "melancholic":  [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        "surprised":    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        "curious":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0], # Map curious to surprised/alert
        "calm":         [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        "neutral":      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]  # Map neutral to calm
    }

    @staticmethod
    def _sanitize_text_for_tts(text: str) -> str:
        """
        Sanitize text before sending to TTS server.
        Removes emojis, special unicode characters, and cleans up whitespace.
        The TTS model works best with clean ASCII-like text.
        """
        if not text:
            return ""
        
        original_len = len(text)
        
        # Remove emoji characters (covers most emoji ranges)
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags
            "\U00002702-\U000027B0"  # dingbats
            "\U000024C2-\U0001F251"  # enclosed chars
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U00002600-\U000026FF"  # misc symbols (sun, umbrella, etc)
            "\U00002700-\U000027BF"  # dingbats
            "\U0000FE00-\U0000FE0F"  # variation selectors
            "\U0001FA00-\U0001FA6F"  # chess, extended-A
            "\U0001FA70-\U0001FAFF"  # symbols extended-A
            "]+",
            flags=re.UNICODE
        )
        text = emoji_pattern.sub('', text)
        
        # Replace some unicode characters with ASCII equivalents
        replacements = {
            '\u00b0': ' degrees ',  # degree symbol
            '\u2013': '-',  # en dash
            '\u2014': '-',  # em dash
            '\u2018': "'",  # left single quote
            '\u2019': "'",  # right single quote
            '\u201c': '"',  # left double quote
            '\u201d': '"',  # right double quote
            '\u2026': '...',  # ellipsis
            '\u00a0': ' ',  # non-breaking space
        }
        for char, replacement in replacements.items():
            text = text.replace(char, replacement)
        
        # Clean up multiple spaces
        text = re.sub(r'\s+', ' ', text).strip()
        
        if len(text) != original_len:
            log_debug(f"[tts_lipsync] Sanitized TTS text: {original_len} -> {len(text)} chars")
        
        return text

    async def _generate_audio(self, text, emotion=None):
        """
        Calls TTS (Primary or Fallback) -> Saves WAV.
        """
        
        # Sanitize text to remove emojis and special chars that can break TTS
        clean_text = self._sanitize_text_for_tts(text)
        if not clean_text:
            log_warning("[tts_lipsync] Text became empty after sanitization, skipping TTS.")
            return None
        
        log_debug(f"[tts_lipsync] Generating audio for: {clean_text[:80]}...")
        
        # Determine emotion vector
        requested_emotion = emotion.lower() if emotion else "neutral"
        emo_vector = self.EMOTION_VECTORS.get(requested_emotion)
        
        # Fallback to neutral if unknown
        if not emo_vector:
            log_warning(f"[tts_lipsync] Unknown emotion '{requested_emotion}', falling back to neutral vector.")
            emo_vector = self.EMOTION_VECTORS["neutral"]
            
        base_payload = {
            "text": clean_text,  # Use sanitized text
            # Server-side Qwen emotion model is disabled, so we pass explicit vectors instead of text
            "use_emo_text": False,
            "emo_text": "",
            # Pass the 8-float vector directly
            "emo_vector": emo_vector,
            # Standard alpha (intensity) - can be tuned later if needed
            # User recommended 0.6 for text modes, but vectors work well at higher intensity (0.9-1.0)
            # usually, but let's stick to safe 0.8 to check stability.
            "emo_alpha": 0.8
        }

        raw_audio = b""
        
        # 1. Call TTS - Try configured servers in order
        success = False
        
        for server_config in self.tts_servers:
            url = server_config["url"]
            ref_path = server_config["ref_path"]
            
            try:
                # Prepare payload for this specific URL using its specific reference path
                payload = base_payload.copy()
                payload["voice_wav"] = ref_path

                log_info(f"[tts_lipsync] Requesting TTS audio from {url} with ref={ref_path}...")
                
                # Use reasonable connect timeout (5s) to allow for local network latency
                # Total timeout 300s (5m) for longer audio generation (weather reports, etc)
                timeout = aiohttp.ClientTimeout(total=300, connect=5.0)
                
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            raw_audio = await resp.read() # Read all into memory
                            if raw_audio:
                                log_info(f"[tts_lipsync] TTS request successful from {url}. Received {len(raw_audio)} bytes.")
                                success = True
                                break # Stop trying URLs
                            else:
                                log_error(f"[tts_lipsync] TTS returned empty audio from {url}")
                        else:
                            log_error(f"[tts_lipsync] TTS Server {url} returned status {resp.status}: {await resp.text()}")
            
            except asyncio.TimeoutError:
                log_warning(f"[tts_lipsync] Connection timed out for {url} (server likely down)")
            except Exception as e:
                # Log detailed error but continue to next URL
                log_warning(f"[tts_lipsync] TTS Request failed for {url}: {e}")

        if not success or not raw_audio:
            log_error("[tts_lipsync] All TTS servers failed.")
            return None

        # 3. Save as valid WAV (offload to thread to avoid blocking event loop)
        try:
            timestamp = int(time.time())
            filename = f"tts_{timestamp}.wav"
            filepath = self.static_dir / filename
            
            def _write_wav():
                with wave.open(str(filepath), 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2) # 16-bit
                    wf.setframerate(22050)
                    wf.writeframes(raw_audio)
            
            await asyncio.to_thread(_write_wav)
            
            log_info(f"Audio saved to {filepath} (Size: {len(raw_audio)} bytes)")
            return filename
        except Exception as e:
            log_error(f"Failed to save WAV: {e}")
            return None

PLUGIN_CLASS = TTSLipSyncPlugin

