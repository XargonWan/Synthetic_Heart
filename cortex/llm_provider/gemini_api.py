# cortex/llm_provider/gemini_api.py
"""
Gemini API LLM Engine for Synthetic Heart.

This engine uses the Gemini REST API to communicate with Gemini models.
It also supports the Gemini Live API (WebSockets) for real-time voice interactions.
It follows the standard LLM engine architecture to ensure all plugins
(diary, emotions, bio_manager, etc.) work properly.

Supports multimodal inputs:
- Images: JPEG, PNG, GIF, WebP
- Audio: MP3, WAV, OGG, FLAC, AAC, M4A
- Documents: PDF, TXT, HTML, CSS, JS, Python, Markdown, JSON, XML, CSV
"""

from __future__ import annotations

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.cortex_api_logger import log_cortex_request, log_cortex_response
from core.logging_utils import log_debug, log_info, log_warning, log_error
import json
import asyncio
import time as _time
import requests
import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.live_session_manager import LiveSessionManager
import tempfile
import subprocess
import os

# Try importing the Google GenAI SDK for Live API support
try:
    from google import genai
    from google.genai import types

    _HAS_GENAI_SDK = True
except ImportError:
    _HAS_GENAI_SDK = False
    log_warning(
        "[gemini_api] google-genai SDK not found. Live API features will be disabled."
    )


# Register Gemini API Key configuration (always visible so it can be set before activation)
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "GEMINI_API_KEY",
        label="Gemini API Key",
        default="",
        value_type=str,
        ui_type="password",
        description="API key for Google Gemini models.",
        scope="llm",
        component="gemini_api",
        tags=["cortex_engine", "sensitive"],
        needs_component_reload=True,
    )
    register_exposed_var(
        "GEMINI_API_BASE_URL",
        label="Gemini API Base URL",
        default="https://generativelanguage.googleapis.com",
        value_type=str,
        ui_type="string",
        description="Base URL for the Gemini REST API.",
        scope="llm",
        component="gemini_api",
        tags=["cortex_engine"],
        advanced=True,
        needs_component_reload=True,
    )
except Exception:
    # Fail silently during import-time if variables engine isn't ready
    pass

GEMINI_API_KEY = config_registry.get_var(
    "GEMINI_API_KEY",
    "",
    label="Gemini API Key",
    description="API key for Google Gemini models.",
    group="llm",
    component="gemini_api",
    sensitive=True,
)

GEMINI_API_BASE_URL = config_registry.get_var(
    "GEMINI_API_BASE_URL",
    "https://generativelanguage.googleapis.com",
    label="Gemini API Base URL",
    description="Base URL for the Gemini REST API.",
    value_type=str,
    group="llm",
    component="gemini_api",
    tags=["cortex_engine"],
    advanced=True,
)

# Model configuration
# As of early 2025, Gemini 2.0 Flash is the latest efficient model.
MODEL_CONFIGS = {
    "gemini-3-flash-preview": {
        "description": "Gemini 3 Flash (Preview)",
        "thinking": True,
        "max_output_tokens": 8192,
        "max_prompt_chars": 1000000,
    },
    "gemini-3-pro-preview": {
        "description": "Gemini 3 Pro (Preview)",
        "thinking": True,
        "max_output_tokens": 8192,
        "max_prompt_chars": 1000000,
    },
    "gemini-2.0-flash-thinking-exp-01-21": {
        "description": "Gemini 2.0 Flash Thinking (Experimental)",
        "thinking": True,
        "max_output_tokens": 8192,
        "max_prompt_chars": 500000,
    },
    "gemini-2.0-flash": {
        "description": "Gemini 2.0 Flash",
        "thinking": False,
        "max_output_tokens": 8192,
        "max_prompt_chars": 500000,
    },
}

DEFAULT_MODEL = "gemini-3-flash-preview"

# Token budget for video multimodal content (frames + audio + markers).
# The decomposition function dynamically adjusts extraction FPS so that the
# total image-token cost stays just within this budget.
# At ~1120 tokens/frame (Gemini 3 default), 32768 ≈ 28 frames.
VIDEO_TOKEN_BUDGET: int = 32768


def _get_gemini_model() -> str:
    from core.config import get_current_model

    current = get_current_model()
    if current in MODEL_CONFIGS:
        return current
    return DEFAULT_MODEL


def _set_gemini_model(value: str) -> None:
    from core.config import set_current_model

    model = str(value).strip()
    if model not in MODEL_CONFIGS:
        model = DEFAULT_MODEL
    set_current_model(model)
    try:
        from core.plugin_instance import plugin as active_plugin

        if active_plugin and hasattr(active_plugin, "set_current_model"):
            active_plugin.set_current_model(model)
    except Exception:
        pass


try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "GEMINI_MODEL",
        label="Gemini Model",
        default=DEFAULT_MODEL,
        value_type=str,
        ui_type="select",
        options=list(MODEL_CONFIGS.keys()),
        description="Active Gemini model used by gemini_api.",
        scope="llm",
        component="gemini_api",
        tags=["cortex_engine"],
        needs_component_reload=False,
    )
except Exception:
    pass

GEMINI_MODEL = config_registry.get_var(
    "GEMINI_MODEL",
    DEFAULT_MODEL,
    label="Gemini Model",
    description="Active Gemini model used by gemini_api.",
    value_type=str,
    group="llm",
    component="gemini_api",
    tags=["cortex_engine"],
    constraints={"choices": list(MODEL_CONFIGS.keys())},
    getter=_get_gemini_model,
    setter=_set_gemini_model,
)

# Model limits map for plugin_instance.py compatibility
# This maps model names to their max character limits
MODEL_LIMITS_MAP = {
    "gemini-3-flash-preview": 1000000,
    "gemini-3-pro-preview": 1000000,
    "gemini-2.0-flash-thinking-exp-01-21": 500000,
    "gemini-2.0-flash": 500000,
    "default": 500000,
}


class GeminiAPIPlugin(AIPluginBase):
    """Gemini API LLM Engine using REST API only.

    This engine follows the standard Synthetic Heart LLM architecture:
    1. handle_incoming_message() receives the prompt and generates a response
    2. The response is RETURNED (not sent directly) so the message_chain can:
       - Parse JSON actions
       - Extract emotions for emotion_manager
       - Create diary entries via ai_diary
       - Update bio_manager with user info
       - Execute any other plugin actions
    3. The message_chain then routes the response to the appropriate interface
    """

    display_name = "Gemini API"

    def __init__(self, notify_fn=None):
        from core.notifier import set_notifier
        from core.config import get_current_model

        if notify_fn:
            set_notifier(notify_fn)
            self._notify_fn = notify_fn
        else:
            self._notify_fn = lambda chat_id, message: log_info(
                f"[NOTIFY fallback] {message}"
            )
            set_notifier(self._notify_fn)

        self._current_model = str(GEMINI_MODEL) or get_current_model() or DEFAULT_MODEL
        if self._current_model not in MODEL_CONFIGS:
            self._current_model = DEFAULT_MODEL

        # Track current request metadata for error handling
        self._current_request_meta = None

        # Model limits map for plugin_instance.py compatibility
        self.model_limits_map = MODEL_LIMITS_MAP

        # Initialize Google GenAI Client if available
        self.client = None
        if _HAS_GENAI_SDK and GEMINI_API_KEY:
            try:
                self.client = genai.Client(
                    api_key=str(GEMINI_API_KEY).strip(),
                    http_options={"api_version": "v1alpha"},
                )
                log_info(
                    "[gemini_api] Google GenAI Client initialized for Live API (v1alpha)"
                )
            except Exception as e:
                log_error(f"[gemini_api] Failed to initialize Google GenAI Client: {e}")

        log_info(f"[gemini_api] Initialized with model: {self._current_model}")

    def get_health_status(self):
        """Return (ok, error_message) indicating whether the engine is ready."""
        if not GEMINI_API_KEY or not str(GEMINI_API_KEY).strip():
            return False, "GEMINI_API_KEY not configured"
        return True, ""

    def get_supported_models(self) -> list[str]:
        """Return available model names."""
        return list(MODEL_CONFIGS.keys())

    def get_current_model(self) -> str:
        """Return the currently active model."""

    # --- Agentic hooks (optional) ---
    def supports_agent(self) -> bool:
        """Return True if this engine provides optional agentic extensions.

        Default: False. Engines that implement richer agentic behavior should
        override this and implement `attach_agent`, `detach_agent` and
        `agent_execute` as appropriate.
        This implementation advertises support and provides a minimal adapter
        that forwards agent execution requests to the central Agent plugin
        when available (best-effort, non-fatal).
        """
        return True

    def agent_execute(self, action_dict: dict, context: dict | None = None) -> dict:
        """Engine-level execution helper that attempts to delegate to the Agent plugin.

        This is a best-effort adapter: if the Agent plugin is loaded in the
        core (`core.core_initializer.PLUGIN_REGISTRY['agent']`) it will call
        its `execute_action` method synchronously if available.
        """
        try:
            # Lazy import to avoid cycles
            from core.core_initializer import PLUGIN_REGISTRY

            agent_plugin = (
                PLUGIN_REGISTRY.get("agent")
                if isinstance(PLUGIN_REGISTRY, dict)
                else None
            )
            if agent_plugin and hasattr(agent_plugin, "execute_action"):
                # execute_action may be async; try to call safely
                res = agent_plugin.execute_action(
                    action_dict, context or {}, None, None
                )
                # If coroutine, return a placeholder since engine API is sync in some callers
                if hasattr(res, "__await__"):
                    # Can't await here; indicate async and let callers handle it
                    return {
                        "status": "pending_async",
                        "note": "Agent plugin returned coroutine",
                    }
                return res or {"status": "ok"}
        except Exception as e:
            log_warning(f"[gemini_api] agent_execute adapter failed: {e}")
        return {
            "status": "unsupported",
            "reason": "agent plugin not available or execution failed",
        }

    def attach_agent(self, agent_plugin) -> None:
        """Attach an Agent plugin instance to the engine.

        Default behavior: store reference and set an attribute. Engines with
        more complex integration can override this method.
        """
        try:
            setattr(self, "_agent_plugin", agent_plugin)
            setattr(self, "agent_enabled", True)
            log_info("[gemini_api] Agent attached (no-op adapter)")
        except Exception as e:
            log_warning(f"[gemini_api] attach_agent failed: {e}")

    def detach_agent(self, agent_plugin) -> None:
        """Detach previously attached Agent plugin instance."""
        try:
            if hasattr(self, "_agent_plugin"):
                delattr(self, "_agent_plugin")
            setattr(self, "agent_enabled", False)
            log_info("[gemini_api] Agent detached (no-op adapter)")
        except Exception as e:
            log_warning(f"[gemini_api] detach_agent failed: {e}")

    def agent_execute(self, action_dict: dict, context: dict | None = None) -> dict:
        """Optional engine-level execution helper for agentic actions.

        Default implementation returns a not-supported dict so callers can fall back.
        Engines that can safely perform tool-calls should implement this.
        """
        log_debug(
            "[gemini_api] agent_execute called but not implemented for this engine"
        )
        return {
            "status": "unsupported",
            "reason": "engine does not implement agent_execute",
        }
        return self._current_model

    def set_current_model(self, name: str):
        """Set the active model."""
        if name not in self.get_supported_models():
            raise ValueError(f"Unsupported model: {name}")
        self._current_model = name
        log_info(f"[gemini_api] Active model updated: {name}")

    def get_rate_limit(self):
        """Return rate limiting parameters.

        Returns:
            tuple: (requests_per_window, window_seconds, burst_limit)
        """
        # Gemini API has generous limits, but we still apply reasonable limits
        # trainer_fraction must be between 0 and 1
        return (60, 60, 0.5)  # 60 requests per minute, 50% reserved for trainers

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for this LLM interface."""
        model_config = MODEL_CONFIGS.get(
            self._current_model, MODEL_CONFIGS[DEFAULT_MODEL]
        )
        return {
            "max_prompt_chars": model_config.get("max_prompt_chars", 1000000),
            "max_response_chars": model_config.get("max_output_tokens", 8192),
            "supports_images": True,
            "supports_functions": True,
            "supports_voice_interaction": _HAS_GENAI_SDK,
            "model_name": self._current_model,
        }

    async def handle_live_processing(
        self, file_path: str, mime_type_hint: str = None
    ) -> str | None:
        """
        Process 'live' media (Voice/Video notes) using standard GenerateContent API.

        Note: We switched from WebSocket (Live API) to Standard API because:
        1. WebSocket requires 'gemini-live-*' models which are preview/unstable.
        2. WebSocket requires complex manual PCM/Frame chunking.
        3. Standard API handles .oga/.mp4 files natively and robustly.
        4. For file-based 'live' messages, standard API is strictly superior/stable.
        """
        try:
            if not self.client:
                log_error("[gemini_api] Client not initialized")
                return None

            # 1. Read File Data
            if not os.path.exists(file_path):
                log_error(f"[gemini_api] File not found: {file_path}")
                return None

            with open(file_path, "rb") as f:
                file_data = f.read()

            # 2. Prepare Part
            # Map common mime types
            mime_type = mime_type_hint or "audio/ogg"
            if file_path.endswith(".mp4"):
                mime_type = "video/mp4"
            elif file_path.endswith(".ogg") or file_path.endswith(".oga"):
                mime_type = "audio/ogg"

            log_debug(
                f"[gemini_api] Processing Live Media: {len(file_data)} bytes, mime={mime_type}"
            )

            # 3. Call Standard API
            # Use current model (usually gemini-2.0-flash-exp or gemini-3-flash which supports audio/video)
            model_id = self._current_model

            # Correction: gemini-3-flash-preview supports audio/video in generate_content
            # If it fails, we can fallback, but it should work.

            prompt = (
                "Transcribe the following audio/video message exactly as spoken. "
                "Output ONLY the transcribed text — no commentary, no timestamps, "
                "no formatting. If the audio is unclear or empty, output an empty string."
            )

            from google.genai import types

            response = await self.client.aio.models.generate_content(
                model=model_id,
                contents=[
                    types.Content(
                        parts=[
                            types.Part(text=prompt),
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type=mime_type, data=file_data
                                )
                            ),
                        ]
                    )
                ],
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are a speech-to-text transcription system. "
                        "Output ONLY the exact words spoken. Do not add any "
                        "interpretation, response, or JSON."
                    ),
                    response_mime_type="text/plain",
                ),
            )

            if response and response.text:
                return response.text.strip()

            return None

        except Exception as e:
            log_error(f"[gemini_api] Error in handle_live_processing (standard): {e}")
            return None

    # ------------------------------------------------------------------
    # Gemini Live API (WebSocket) — Discord voice integration
    # ------------------------------------------------------------------

    def get_live_session_manager(self) -> "LiveSessionManager | None":
        """Return the LiveSessionManager instance, creating it lazily.

        Returns None if the google-genai SDK is unavailable or no API key is set.
        """
        if not _HAS_GENAI_SDK or not GEMINI_API_KEY:
            return None

        if not hasattr(self, "_live_session_manager"):
            from core.live_session_manager import LiveSessionManager

            self._live_session_manager = LiveSessionManager(
                api_key=str(GEMINI_API_KEY).strip()
            )
            log_info("[gemini_api] LiveSessionManager created")

        return self._live_session_manager

    async def start_live_voice_session(
        self,
        guild_id: int,
        channel_id: int,
        system_instruction: str | None = None,
    ) -> bool:
        """Start a Live API session for Discord voice.

        If no system_instruction is provided, one is built automatically
        from the current persona via prompt_engine.

        Args:
            guild_id: Discord guild ID.
            channel_id: Discord voice channel ID.
            system_instruction: Optional pre-built system instruction text.

        Returns:
            True if the session was started successfully.
        """
        manager = self.get_live_session_manager()
        if not manager:
            log_error("[gemini_api] Cannot start live session — SDK or API key missing")
            return False

        if not system_instruction:
            from core.prompt_engine import build_live_system_instruction

            system_instruction = await build_live_system_instruction()

        return await manager.start_session(
            guild_id=guild_id,
            channel_id=channel_id,
            system_instruction=system_instruction,
        )

    async def stop_live_voice_session(self, guild_id: int) -> None:
        """Stop the Live API session for a guild."""
        manager = self.get_live_session_manager()
        if manager:
            await manager.stop_session(guild_id)

    async def _extract_frames(self, video_path: str) -> list[bytes]:
        """Extract frames from video at ~1fps as JPEGs."""
        frames = []
        try:
            # Output to pipe as series of JPEGs is tricky to parse.
            # Easier to output to temp files or use updating buffer.
            # actually, using sending a video file as 'video/mp4' data is NOT supported by WebSocket directly usually,
            # but 'genai' SDK might handle it?
            # Re-reading docs: "realtimeInput ... video ... Blob".
            # If we just read the file bytes?
            # Let's try sending the whole video file as one blob if it's small, OR just use frames.
            # Frames are safer for "Live" understanding.

            # Use ffmpeg to output frames to a temp directory
            with tempfile.TemporaryDirectory() as temp_dir:
                # Extract 1 frame per second
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    video_path,
                    "-vf",
                    "fps=1,scale=640:-1",  # Resize for speed
                    "-q:v",
                    "5",  # JPEG quality
                    os.path.join(temp_dir, "frame_%04d.jpg"),
                ]

                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                await process.communicate()

                # Read frames
                for filename in sorted(os.listdir(temp_dir)):
                    if filename.endswith(".jpg"):
                        with open(os.path.join(temp_dir, filename), "rb") as f:
                            frames.append(f.read())

            log_debug(f"[gemini_api] Extracted {len(frames)} frames from video")
            return frames
        except Exception as e:
            log_error(f"[gemini_api] Frame extraction failed: {e}")
            return []

    async def _convert_audio_to_pcm(self, input_path: str) -> bytes | None:
        """Convert input audio to 16kHz mono PCM s16le using ffmpeg."""
        try:
            # ffmpeg -i input.ogg -f s16le -acodec pcm_s16le -ar 16000 -ac 1 pipe:1
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "pipe:1",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                log_error(f"[gemini_api] ffmpeg conversion failed: {stderr.decode()}")
                return None

            return stdout
        except Exception as e:
            log_error(f"[gemini_api] Error running ffmpeg: {e}")
            return None

    async def _convert_pcm_to_ogg(
        self, pcm_data: bytes, output_path: str
    ) -> str | None:
        """Convert 24kHz (Gemini Default) PCM s16le to OGG Opus."""
        # Note: This is now largely unused if we are text-only, but kept for future fallback
        try:
            # ... (Same as before)
            # ffmpeg -f s16le -ar 24000 -ac 1 -i pipe:0 -c:a libopus output.ogg
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                output_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await process.communicate(input=pcm_data)

            if process.returncode != 0:
                log_error(
                    f"[gemini_api] ffmpeg output conversion failed: {stderr.decode()}"
                )
                return None

            return output_path
        except Exception as e:
            log_error(f"[gemini_api] Error running ffmpeg for output: {e}")
            return None

    async def handle_incoming_message(self, bot, message, prompt):
        """Process a message using a pre-built prompt.

        CRITICAL: This method RETURNS the response text, it does NOT send it directly.
        The response is then processed by the message_chain which:
        1. Parses JSON actions
        2. Extracts emotions for emotion_manager
        3. Creates diary entries
        4. Executes plugin actions
        5. Routes the response to the appropriate interface

        This is the key difference from the previous implementation - we follow
        the standard LLM engine pattern that other engines use.
        """
        from core.notifier import notify_trainer

        try:
            # Store request metadata for error handling
            self._current_request_meta = {
                "bot": bot,
                "message": message,
                "interface": getattr(message, "interface", None)
                or getattr(message, "interface_path", None),
                "chat_id": getattr(message, "chat_id", None),
                "interface_path": getattr(message, "interface_path", None),
            }

            log_debug(
                f"[gemini_api] Processing message from chat_id={getattr(message, 'chat_id', 'unknown')}"
            )

            # Generate response using the Gemini API
            response = await self.generate_response(prompt)

            # Log the response for debugging
            if response:
                preview = response[:200] + "..." if len(response) > 200 else response
                log_info(f"[gemini_api] 📤 Generated response: {preview}")

            # IMPORTANT: Return the response, don't send it directly!
            # The message_chain/plugin_instance will handle:
            # - JSON parsing and action execution
            # - Emotion extraction
            # - Diary entry creation
            # - Bio updates
            # - Interface routing
            return response

        except Exception as e:
            log_error(f"[gemini_api] Error in handle_incoming_message: {repr(e)}")
            notify_trainer(f"❌ Gemini API error:\n{e}")
            # Return error message so the system can handle it appropriately
            return f"⚠️ Error during response generation: {str(e)}"
        finally:
            self._current_request_meta = None

    async def generate_response(self, prompt):
        """Send prompt to Gemini API and receive the response.

        Args:
            prompt: Can be a dict (JSON prompt from prompt_engine) or string

        Returns:
            str: The LLM response text
        """
        if not GEMINI_API_KEY:
            # Return a plain, user-readable string instead of a `system_message` action
            # which the message_chain treats as a blocked/unsupported action type.
            log_warning(
                "[gemini_api] GEMINI_API_KEY not configured; returning plain error string"
            )
            return "⚠️ Gemini API Key not configured. Please set GEMINI_API_KEY in settings or .env"

        try:
            # Handle different prompt formats
            if isinstance(prompt, dict):
                # Check for system_message - but only trigger correction for ERROR types
                # "output" type system_messages are just action results and should be processed normally
                if "system_message" in prompt:
                    sm = prompt.get("system_message", {})
                    sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                    # Only handle as correction if it's an actual error/correction request
                    if sm_type in (
                        "error",
                        "correction",
                        "invalid_json",
                        "validation_error",
                    ):
                        return await self._handle_correction_prompt(prompt)
                    # Otherwise, process normally (e.g., "output" type with action_outputs)
                    log_debug(
                        f"[gemini_api] Processing system_message type '{sm_type}' as normal prompt"
                    )

                # Standard JSON prompt from prompt_engine
                prompt_text = json.dumps(prompt, indent=2, ensure_ascii=False)
            elif isinstance(prompt, str):
                # Try to parse as JSON first
                try:
                    parsed = json.loads(prompt)
                    if isinstance(parsed, dict) and "system_message" in parsed:
                        sm = parsed.get("system_message", {})
                        sm_type = sm.get("type", "") if isinstance(sm, dict) else ""
                        if sm_type in (
                            "error",
                            "correction",
                            "invalid_json",
                            "validation_error",
                        ):
                            return await self._handle_correction_prompt(parsed)
                        log_debug(
                            f"[gemini_api] Processing system_message type '{sm_type}' as normal prompt"
                        )
                    prompt_text = prompt
                except (json.JSONDecodeError, ValueError):
                    prompt_text = prompt
            else:
                prompt_text = str(prompt)

            # --- Multimodal Support: Extract parts and redact text prompt ---
            # Extract heavy multimodal parts (images, audio) to be sent as native Gemini parts
            multimodal_parts = await self._extract_multimodal_parts(prompt)

            # ALWAYS redact heavy base64 data from the text prompt, even if no parts were extracted.
            # This prevents unsupported mime-types or raw chunks from leaking into the text prompt
            # and confusing the model (or causing the '😵' error).
            prompt_to_redact = None
            if isinstance(prompt, dict):
                prompt_to_redact = prompt
            elif isinstance(prompt, str):
                try:
                    prompt_to_redact = json.loads(prompt)
                    if not isinstance(prompt_to_redact, dict):
                        prompt_to_redact = None
                except Exception:
                    prompt_to_redact = None

            if prompt_to_redact:
                prompt_redacted = self._copy_and_redact_data(prompt_to_redact)
                # Regenerate prompt_text from reduced version
                prompt_text = json.dumps(prompt_redacted, indent=2, ensure_ascii=False)
                if len(prompt_text) < len(str(prompt)):
                    log_debug(
                        f"[gemini_api] Redacted heavy data from prompt: {len(str(prompt))} -> {len(prompt_text)} chars"
                    )

            log_debug(
                f"[gemini_api] Sending prompt ({len(prompt_text)} chars) to {self._current_model}"
            )

            # Build generation config
            model_config = MODEL_CONFIGS.get(
                self._current_model, MODEL_CONFIGS[DEFAULT_MODEL]
            )
            # Note: thinking_enabled is configured via model config, not explicitly used here

            config_args = {
                "max_output_tokens": model_config.get("max_output_tokens", 8192),
            }

            # Configure thinking if enabled for this model
            # Build system instruction that enforces JSON output
            system_instruction = self._build_system_instruction(prompt)
            config_args["system_instruction"] = system_instruction

            response_text = await self._http_generate_content(
                prompt_text=prompt_text,
                system_instruction=system_instruction,
                max_output_tokens=int(config_args.get("max_output_tokens", 8192)),
                multimodal_parts=multimodal_parts if multimodal_parts else None,
            )

            log_debug(f"[gemini_api] Received response ({len(response_text)} chars)")

            return response_text

        except Exception as e:
            log_error(f"[gemini_api] Generation failed: {e}")
            # Return a JSON error so the system can handle it
            error_response = {
                "actions": [
                    {
                        "type": "system_message",
                        "payload": {"text": f"⚠️ Gemini API error: {str(e)}"},
                    }
                ]
            }
            return json.dumps(error_response)

    def _build_system_instruction(self, prompt) -> str:
        """Build the system instruction for Gemini based on the prompt context.

        NOTE: The prompt_engine already includes complete action schemas with descriptions,
        required fields, and examples. This system instruction just reinforces the JSON
        output format requirement. Don't duplicate action definitions here.
        """
        # Extract interface information from prompt if available
        # prompt_engine puts it at input.source.interface, but also check top-level
        interface = "unknown"
        verbose_instructions = None
        prompt_dict = None

        if isinstance(prompt, dict):
            prompt_dict = prompt
        elif isinstance(prompt, str):
            try:
                parsed = json.loads(prompt)
                if isinstance(parsed, dict):
                    prompt_dict = parsed
            except (json.JSONDecodeError, ValueError):
                prompt_dict = None

        if isinstance(prompt_dict, dict):
            # Check top-level first
            interface = prompt_dict.get("interface") or prompt_dict.get(
                "current_interface"
            )
            verbose_instructions = prompt_dict.get("instructions_verbose")

            # If not found, check input.source.interface (prompt_engine structure)
            if not interface or interface == "unknown":
                input_section = prompt_dict.get("input", {})
                if isinstance(input_section, dict):
                    source = input_section.get("source", {})
                    if isinstance(source, dict):
                        interface = source.get("interface") or interface

            # If still unknown, check input.interface
            if not interface or interface == "unknown":
                input_section = prompt_dict.get("input", {})
                if isinstance(input_section, dict):
                    interface = input_section.get("interface") or interface

        # Default fallback
        if not interface:
            interface = "unknown"

        # Map interface to the correct message action type
        interface_to_action = {
            "synth_webui": "message_synth_webui",
            "telegram_bot": "message_telegram_bot",
            "discord_bot": "message_discord_bot",
            "ollama_serve": "message_ollama_serve",
        }
        message_action = interface_to_action.get(interface, f"message_{interface}")

        is_grillo = interface == "grillo" or (
            isinstance(prompt_dict, dict) and prompt_dict.get("grillo_beat")
        )
        # Outreach beats are grillo-initiated but MUST produce message_*
        # actions to reach the target interface (e.g. telegram_bot).
        is_grillo_internal = is_grillo and (
            not isinstance(prompt_dict, dict)
            or prompt_dict.get("beat_type", "internal") != "outreach"
        )

        # Minimal system instruction - the prompt itself contains full action schemas
        # We just need to remind the model to output valid JSON
        if is_grillo_internal:
            # Internal beats (tag_elaboration, self_reflection, etc.)
            interface_hint = (
                "CURRENT INTERFACE: grillo (INTERNAL)\n"
                "This is an internal introspection beat. Do NOT output any message_* actions.\n"
                "Use ONLY internal actions like 'create_personal_diary_entry', 'set_emotion', etc."
            )
        else:
            interface_hint = (
                f"CURRENT INTERFACE: {interface}\n"
                f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'"
            )

        system_instruction = (
            "You are part of the 'Synthetic Heart' AI system.\n"
            "\n"
            "CRITICAL OUTPUT FORMAT:\n"
            "1. Respond with ONLY valid JSON - nothing before or after\n"
            "2. Your response MUST start with { and end with }\n"
            '3. Use this structure: {"actions": [{"type": "action_name", "payload": {...}}]}\n'
            "4. NO markdown code blocks, NO explanations outside JSON\n"
            "\n"
            f"{interface_hint}\n"
            "\n"
            "The prompt contains a complete action schema with available actions.\n"
            "Follow those instructions precisely.\n"
            "\n"
            "Remember: Output ONLY valid JSON. The system will parse your JSON and execute the actions."
        )

        # Include unminified chat instruction verbatim when provided
        if verbose_instructions:
            system_instruction = f"{verbose_instructions}\n\n{system_instruction}"

        return system_instruction

    async def _http_generate_content(
        self,
        prompt_text: str,
        system_instruction: str,
        max_output_tokens: int,
        multimodal_parts: list[dict] | None = None,
    ) -> str:
        """Generate content using the Gemini REST API.

        Args:
            prompt_text: The text prompt to send
            system_instruction: System instruction for the model
            max_output_tokens: Maximum tokens in response
            multimodal_parts: Optional list of multimodal inline_data parts
        """
        base_url = (
            str(GEMINI_API_BASE_URL).strip()
            or "https://generativelanguage.googleapis.com"
        )
        api_key = str(GEMINI_API_KEY).strip()
        if base_url.endswith("/v1") or base_url.endswith("/v1beta"):
            versioned_base = base_url
        else:
            versioned_base = f"{base_url}/v1beta"
        url = f"{versioned_base}/models/{self._current_model}:generateContent"

        # Build the parts list - multimodal content first, then text
        user_parts = []
        if multimodal_parts:
            user_parts.extend(multimodal_parts)
            log_debug(
                f"[gemini_api] Including {len(multimodal_parts)} multimodal parts in request"
            )
        user_parts.append({"text": prompt_text})

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": user_parts,
                }
            ],
            "systemInstruction": {
                "role": "system",
                "parts": [{"text": system_instruction}],
            },
            "generationConfig": {
                "maxOutputTokens": int(max_output_tokens),
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE",
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE",
                },
            ],
        }

        # ── Log the outgoing request ──────────────────────────────────
        log_cortex_request(
            "gemini_api",
            model=self._current_model,
            url=url,
            payload=payload,
        )
        _req_start = _time.monotonic()

        def _do_request():
            return requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=120,  # Increased timeout for multimodal requests
            )

        retryable_statuses = {429, 500, 503, 504}
        max_attempts = 3
        response = None

        for attempt in range(max_attempts):
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, _do_request)
            except Exception as e:
                if attempt < max_attempts - 1:
                    delay = min(8, 1 * (2**attempt))
                    log_warning(
                        f"[gemini_api] HTTP request failed (attempt {attempt + 1}/{max_attempts}): {e}. "
                        f"Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(f"[gemini_api] HTTP request failed: {e}")
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"⚠️ Gemini HTTP request failed: {str(e)}"
                                },
                            }
                        ]
                    }
                )

            if response.status_code >= 400:
                if (
                    response.status_code in retryable_statuses
                    and attempt < max_attempts - 1
                ):
                    delay = min(8, 1 * (2**attempt))
                    log_warning(
                        f"[gemini_api] HTTP error {response.status_code} (attempt {attempt + 1}/{max_attempts}). "
                        f"Retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_error(
                    f"[gemini_api] HTTP error {response.status_code}: {response.text}"
                )
                return json.dumps(
                    {
                        "actions": [
                            {
                                "type": "system_message",
                                "payload": {
                                    "text": f"⚠️ Gemini HTTP error {response.status_code}: {response.text}"
                                },
                            }
                        ]
                    }
                )
            break

        if response is None:
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "⚠️ Gemini HTTP request failed: no response"
                            },
                        }
                    ]
                }
            )

        try:
            data = response.json()
        except Exception as e:
            log_error(f"[gemini_api] HTTP response JSON parse failed: {e}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "⚠️ Gemini HTTP response was not valid JSON"
                            },
                        }
                    ]
                }
            )

        # Log error responses from the API
        if "error" in data:
            _elapsed = (_time.monotonic() - _req_start) * 1000
            err = data["error"]
            err_msg = (
                err.get("message", str(err)) if isinstance(err, dict) else str(err)
            )
            log_cortex_response(
                "gemini_api",
                model=self._current_model,
                status=response.status_code if response else None,
                error=err_msg,
                elapsed_ms=_elapsed,
            )

        candidates = data.get("candidates") or []
        if not candidates:
            log_error(f"[gemini_api] HTTP response missing candidates: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "⚠️ Gemini HTTP response missing candidates"
                            },
                        }
                    ]
                }
            )

        content = candidates[0].get("content", {})
        parts = content.get("parts") or []
        response_text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        ).strip()

        if not response_text:
            log_error(f"[gemini_api] HTTP response contained no text: {data}")
            return json.dumps(
                {
                    "actions": [
                        {
                            "type": "system_message",
                            "payload": {
                                "text": "⚠️ Gemini HTTP response contained no text"
                            },
                        }
                    ]
                }
            )

        # ── Log the response ────────────────────────────────────────────
        _elapsed = (_time.monotonic() - _req_start) * 1000
        usage_meta = data.get("usageMetadata") or {}
        log_cortex_response(
            "gemini_api",
            model=self._current_model,
            status=response.status_code if response else None,
            body=response_text,
            usage={
                "prompt_tokens": usage_meta.get("promptTokenCount"),
                "completion_tokens": usage_meta.get("candidatesTokenCount"),
            }
            if usage_meta
            else None,
            elapsed_ms=_elapsed,
        )

        return response_text

    # -------------------------------------------------------------------------
    # Multimodal Support Methods
    # -------------------------------------------------------------------------

    # Supported MIME types for multimodal inputs
    SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    SUPPORTED_AUDIO_TYPES = {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "audio/x-m4a",
    }
    SUPPORTED_VIDEO_TYPES = {
        "video/mp4",
        "video/mpeg",
        "video/mov",
        "video/quicktime",
        "video/avi",
        "video/x-msvideo",
        "video/x-flv",
        "video/mpg",
        "video/webm",
        "video/wmv",
        "video/x-ms-wmv",
        "video/3gpp",
    }
    SUPPORTED_DOCUMENT_TYPES = {
        "application/pdf",
        "text/plain",
        "text/html",
        "text/css",
        "text/javascript",
        "application/javascript",
        "text/x-python",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
        "text/csv",
    }

    def _get_mime_type(self, file_path: str | Path) -> str:
        """Determine MIME type from file path or extension."""
        path = Path(file_path) if isinstance(file_path, str) else file_path

        # Try mimetypes first
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type:
            return mime_type

        # Fallback mapping for common extensions
        ext_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".flac": "audio/flac",
            ".aac": "audio/aac",
            ".m4a": "audio/mp4",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".html": "text/html",
            ".htm": "text/html",
            ".css": "text/css",
            ".js": "text/javascript",
            ".py": "text/x-python",
            ".md": "text/markdown",
            ".json": "application/json",
            ".xml": "application/xml",
            ".csv": "text/csv",
            # Video formats
            ".mp4": "video/mp4",
            ".mpeg": "video/mpeg",
            ".mpg": "video/mpeg",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".flv": "video/x-flv",
            ".webm": "video/webm",
            ".wmv": "video/x-ms-wmv",
            ".3gp": "video/3gpp",
            ".3gpp": "video/3gpp",
        }

        suffix = path.suffix.lower()
        return ext_map.get(suffix, "application/octet-stream")

    def _encode_file_to_base64(self, file_path: str | Path) -> str | None:
        """Read a file and encode it to base64."""
        try:
            path = Path(file_path) if isinstance(file_path, str) else file_path
            if not path.exists():
                log_warning(f"[gemini_api] File not found: {path}")
                return None

            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            log_error(f"[gemini_api] Failed to encode file {file_path}: {e}")
            return None

    def _is_supported_multimodal_type(self, mime_type: str) -> bool:
        """Check if a MIME type is supported for multimodal input."""
        return (
            mime_type in self.SUPPORTED_IMAGE_TYPES
            or mime_type in self.SUPPORTED_AUDIO_TYPES
            or mime_type in self.SUPPORTED_VIDEO_TYPES
            or mime_type in self.SUPPORTED_DOCUMENT_TYPES
        )

    async def _extract_multimodal_parts(self, prompt: dict | str) -> list[dict]:
        """Extract multimodal parts from the prompt context recursively.

        Recursively searches for attachments in the prompt dict under keys like:
        - 'attachments': list of {path, mime_type, data} or {path, mime_type}
        - 'images': list of image paths or base64 data
        - 'audio': list of audio paths or base64 data
        - 'videos': list of video paths or base64 data
        - 'documents': list of document paths or base64 data

        These can appear at any nesting level in the prompt structure.

        Video attachments are decomposed into temporally-interleaved frames +
        audio chunks so the model perceives visual and audio content in parallel
        with explicit timestamp markers.

        Returns a list of Gemini API inline_data / text parts.
        """
        from core.multimodal_attachment import decompose_video_to_frames_and_audio

        parts: list[dict] = []

        if isinstance(prompt, str):
            try:
                prompt = json.loads(prompt)
            except (json.JSONDecodeError, ValueError):
                return parts

        if not isinstance(prompt, dict):
            return parts

        # Keys that can contain lists of multimodal attachments
        MULTIMODAL_KEYS = {"attachments", "images", "audio", "documents", "videos"}

        # Collect all attachments from all locations
        attachments: list[dict] = []

        # Keys whose subtrees are schema definitions (not multimodal data)
        # and should be skipped during recursive attachment collection.
        SCHEMA_ONLY_KEYS = {"actions", "available_actions", "schema", "payload"}

        def collect_attachments_recursive(container: dict | list | str) -> None:
            """Recursively collect multimodal attachments from any level."""
            if isinstance(container, dict):
                # Check for multimodal list keys at this level
                for key in MULTIMODAL_KEYS:
                    if key in container:
                        items = container[key]
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, dict):
                                    attachments.append(item)
                                elif isinstance(item, str):
                                    # Legacy: string path, infer type from key
                                    default_mime = {
                                        "images": "image/jpeg",
                                        "audio": "audio/mpeg",
                                        "videos": "video/mp4",
                                        "documents": "application/pdf",
                                    }.get(key, "application/octet-stream")
                                    attachments.append(
                                        {"path": item, "mime_type": default_mime}
                                    )
                        elif isinstance(items, dict):
                            # Single item rather than list
                            attachments.append(items)

                # Recurse into dict values, skipping action schema subtrees
                # which contain field definitions that look like attachments
                # but are not (e.g. {"type": "string", "description": "..."}).
                for key, value in container.items():
                    if key in SCHEMA_ONLY_KEYS:
                        continue
                    collect_attachments_recursive(value)

            elif isinstance(container, list):
                # Recurse into list items
                for item in container:
                    collect_attachments_recursive(item)

        # Start recursive collection from root
        collect_attachments_recursive(prompt)

        # Flag: set to True when at least one video was successfully decomposed.
        # When set, companion audio files (named *_audio.ogg by
        # _extract_audio_from_video) are skipped because their content is
        # already interleaved in the decomposed frame+audio parts.
        any_video_decomposed: bool = False

        # Process each collected attachment
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue

            # Get or determine MIME type
            mime_type = attachment.get("mime_type") or attachment.get("mimeType")
            file_path = attachment.get("path") or attachment.get("file_path")
            filename = attachment.get("filename", "")

            if file_path and not mime_type:
                mime_type = self._get_mime_type(file_path)

            if not mime_type:
                log_warning(
                    f"[gemini_api] Skipping attachment without MIME type: {attachment}"
                )
                continue

            if not self._is_supported_multimodal_type(mime_type):
                log_warning(f"[gemini_api] Unsupported MIME type: {mime_type}")
                continue

            # Get base64 data - either provided or read from file
            base64_data = attachment.get("data") or attachment.get("base64")

            if not base64_data and file_path:
                base64_data = self._encode_file_to_base64(file_path)

            if not base64_data:
                log_warning(f"[gemini_api] No data for attachment: {attachment}")
                continue

            # --- Skip companion audio for already-decomposed videos -------------
            # Companion audio files are created by _extract_audio_from_video and
            # always named "<something>_audio.ogg".  The video filename may use
            # the original file name (e.g. IMG_4830.MP4) while the companion
            # audio uses the Telegram file_unique_id, so stem matching is
            # unreliable.  Instead, skip any *_audio.ogg when a video in this
            # request was successfully decomposed.
            if (
                any_video_decomposed
                and mime_type in self.SUPPORTED_AUDIO_TYPES
                and filename
            ):
                stem = Path(filename).stem
                if stem.endswith("_audio"):
                    log_debug(
                        f"[gemini_api] Skipping companion audio {filename} "
                        f"(already interleaved in decomposed video)"
                    )
                    continue

            # --- Video decomposition: interleave frames + audio -----------------
            if mime_type in self.SUPPORTED_VIDEO_TYPES:
                video_bytes = base64.b64decode(base64_data)
                source_label = filename or "video"
                decomposed = await decompose_video_to_frames_and_audio(
                    video_bytes,
                    source_label,
                    token_budget=VIDEO_TOKEN_BUDGET,
                )
                if decomposed:
                    any_video_decomposed = True

                    total_frames = sum(len(s["frames_b64"]) for s in decomposed)
                    # Duration = last group's timestamp + 1 second
                    total_duration = decomposed[-1]["ts"] + 1.0 if decomposed else 0.0
                    has_any_audio = any(s["audio_b64"] is not None for s in decomposed)

                    # --- Video context preamble ---
                    # Give the model a holistic overview before the per-second
                    # interleaved data, mirroring the structured metadata that
                    # the image path provides.
                    caption = attachment.get("caption", "")
                    media_meta = attachment.get("media_metadata", {})
                    preamble_parts = [
                        f"[Video: {source_label}",
                        f"{total_duration:.1f}s duration",
                        f"{total_frames} frames",
                    ]
                    if has_any_audio:
                        preamble_parts.append("with audio")
                    else:
                        preamble_parts.append("no audio")
                    res_w = media_meta.get("width", 0)
                    res_h = media_meta.get("height", 0)
                    if res_w and res_h:
                        preamble_parts.append(f"{res_w}x{res_h}")
                    preamble = ", ".join(preamble_parts) + "]"
                    if caption:
                        preamble += f'\nUser caption: "{caption}"'
                    parts.append({"text": preamble})

                    for sec in decomposed:
                        ts = sec["ts"]
                        end_ts = ts + 1.0
                        has_audio = sec["audio_b64"] is not None
                        if has_audio:
                            marker = (
                                f"[Video frame at T={ts:.1f}s, "
                                f"audio spans {ts:.1f}\u2013{end_ts:.1f}s]"
                            )
                        else:
                            marker = f"[Video frame at T={ts:.1f}s, no audio]"
                        parts.append({"text": marker})

                        for frame_b64 in sec["frames_b64"]:
                            parts.append(
                                {
                                    "inline_data": {
                                        "mime_type": "image/jpeg",
                                        "data": frame_b64,
                                    }
                                }
                            )

                        if has_audio:
                            parts.append(
                                {
                                    "inline_data": {
                                        "mime_type": "audio/wav",
                                        "data": sec["audio_b64"],
                                    }
                                }
                            )

                    parts.append(
                        {
                            "text": (
                                f"[End of video \u2014 {total_frames} frames, "
                                f"{total_duration:.1f}s total duration]"
                            )
                        }
                    )
                    log_debug(
                        f"[gemini_api] Interleaved video {source_label}: "
                        f"{total_frames} frames + {sum(1 for s in decomposed if s['audio_b64'])} "
                        f"audio chunks over {total_duration:.1f}s"
                    )
                    continue  # Skip the default blob path
                else:
                    log_debug(
                        f"[gemini_api] Video decomposition failed for "
                        f"{source_label}, falling back to blob"
                    )
                    # Fall through to default inline_data handling below

            # --- Default: create inline_data part for Gemini API ----------------
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64_data,
                    }
                }
            )

            log_debug(f"[gemini_api] Added multimodal part: {mime_type}")

        return parts

    def _copy_and_redact_data(self, prompt: dict) -> dict:
        """Create a deep copy of the prompt and redact heavy binary data recursively.

        This ensures that when the prompt is serialized to JSON for the 'text'
        part of the Gemini request, it doesn't contain massive base64 strings
        that are already being sent as native multimodal 'inline_data' parts.

        This fixes the "duplicate data" issue where:
        - The plugin_instance adds base64-encoded multimodal data to the prompt
        - gemini_api.py extracts it and sends it as native inline_data parts
        - BUT the JSON text prompt ALSO contained the same base64 strings
        - This causes massive prompt sizes and confuses the model

        Handles:
        - 'attachments' key at any nesting level
        - Legacy keys: 'images', 'audio', 'documents', 'videos'
        - Both 'data' and 'base64' field names for binary content
        """
        import copy

        try:
            redacted = copy.deepcopy(prompt)

            # Keys that can contain lists of multimodal attachments
            MULTIMODAL_KEYS = {"attachments", "images", "audio", "documents", "videos"}
            # Fields within attachments that contain heavy base64 data
            DATA_FIELDS = {"data", "base64"}
            # Keys that suggest a dict is actually an attachment
            ATTACHMENT_FIELDS = {
                "mime_type",
                "mimeType",
                "path",
                "file_path",
                "data",
                "base64",
            }

            def is_likely_attachment(item: dict) -> bool:
                """Check if a dict contains keys typical of an attachment."""
                return bool(item.keys() & ATTACHMENT_FIELDS)

            def redact_multimodal_item(item: dict) -> None:
                """Redact base64 data fields within a single attachment dict."""
                if not isinstance(item, dict):
                    return
                # Verify it looks like an attachment before redacting
                if not is_likely_attachment(item):
                    return
                for field in DATA_FIELDS:
                    if field in item:
                        original_len = len(str(item[field]))
                        item[field] = f"<redacted: {original_len} chars>"

            def redact_recursive(container) -> None:
                """Recursively search and redact multimodal data at any level."""
                if isinstance(container, dict):
                    # Check for multimodal list keys at this level
                    for key in MULTIMODAL_KEYS:
                        if key in container:
                            items = container[key]
                            if isinstance(items, list):
                                for item in items:
                                    redact_multimodal_item(item)
                            elif isinstance(items, dict):
                                # Single item rather than list
                                redact_multimodal_item(items)

                    # Recurse into all dict values
                    for value in container.values():
                        redact_recursive(value)

                elif isinstance(container, list):
                    # Recurse into list items
                    for item in container:
                        redact_recursive(item)

            # Start recursive redaction from root
            redact_recursive(redacted)

            return redacted
        except Exception as e:
            log_warning(f"[gemini_api] Failed to redact prompt data: {e}")
            return prompt

    async def _handle_correction_prompt(self, prompt: dict) -> str:
        """Handle a correction/system_message prompt.

        When the system detects invalid JSON or failed actions, it sends a
        correction prompt. We need to understand what went wrong and fix it.
        """
        system_message = prompt.get("system_message", {})
        error_type = system_message.get("type", "error")
        error_message = system_message.get("message", "Unknown error")
        original_user_message = system_message.get("original_user_message", "")
        your_reply = system_message.get("your_reply", "")
        required_format = system_message.get("required_format", {})
        # action_full_schema available via system_message.get("action_full_schema", {}) if needed

        # Extract interface from the prompt or system_message - check multiple fields
        # Priority: target_interface > interface > infer from action_type_hint > fallback
        interface = (
            system_message.get("target_interface")
            or system_message.get("interface")
            or prompt.get("interface")
            or None
        )

        # If still no interface, try to extract from action_type_hint
        if not interface:
            action_hint = system_message.get("action_type_hint", "")
            if "message_telegram_bot" in action_hint:
                interface = "telegram_bot"
            elif "message_discord_bot" in action_hint:
                interface = "discord_bot"
            elif "message_synth_webui" in action_hint:
                interface = "synth_webui"
            else:
                interface = "synth_webui"  # Final fallback

        # Grillo is an internal beat system, not a real interface
        # When the interface is "grillo", use synth_webui or skip messaging entirely
        if interface == "grillo":
            log_debug(
                "[gemini_api] Grillo beat detected - internal messages don't need interface routing"
            )
            # For grillo beats, we don't need to send external messages
            # The LLM should only create diary entries, not try to send messages
            interface = None  # Signal that no message action is needed

        log_warning(f"[gemini_api] Handling correction prompt: {error_type}")
        log_debug(
            f"[gemini_api] Correction interface resolved: {interface}, target_interface={system_message.get('target_interface')}, action_type_hint={system_message.get('action_type_hint')}"
        )

        # For Grillo internal beats (interface=None after grillo detection), just fix JSON without message action
        if interface is None:
            # Tell the LLM to produce valid JSON without a message action
            correction_prompt = f"""CORRECTION REQUIRED - INTERNAL BEAT

Error: {error_message}

This is an internal Grillo beat. You should NOT output any message action.
Just output a valid JSON with internal actions like 'create_personal_diary_entry'.

Your previous (invalid) reply:
{your_reply[:500] if your_reply else "(none)"}...

Respond with ONLY valid JSON containing internal actions (like create_personal_diary_entry).
Do NOT include any message_* actions.
"""
            config_args = {
                "max_output_tokens": 4096,
                "system_instruction": (
                    "You are a JSON correction assistant for internal Grillo beats. "
                    "Output ONLY valid JSON with internal actions like 'create_personal_diary_entry'. "
                    "Do NOT output any message_* actions - this is an internal introspection beat."
                ),
            }
            return await self._http_generate_content(
                prompt_text=correction_prompt,
                system_instruction=config_args["system_instruction"],
                max_output_tokens=config_args.get("max_output_tokens", 4096),
            )

        # Map interface to the correct message action type
        interface_to_action = {
            "synth_webui": "message_synth_webui",
            "telegram_bot": "message_telegram_bot",
            "discord_bot": "message_discord_bot",
            "ollama_serve": "message_ollama_serve",
        }
        message_action = interface_to_action.get(interface, f"message_{interface}")

        # Build a focused correction prompt
        correction_prompt = f"CORRECTION REQUIRED\n\nError: {error_message}\n\n"

        if original_user_message:
            correction_prompt += f'Original user message you should respond to:\n"{original_user_message}"\n\n'

        if your_reply:
            correction_prompt += (
                f"Your previous (invalid) reply:\n{your_reply[:500]}...\n\n"
            )

        correction_prompt += (
            f"REQUIREMENTS:\n"
            f"1. Respond with ONLY valid JSON\n"
            f"2. Follow this exact structure:\n"
            f"{json.dumps(required_format, indent=2)}\n"
            f"\n"
            f"IMPORTANT: To send a message to the user, use action type '{message_action}'\n"
            f"\n"
            f"Respond NOW with valid JSON only."
        )

        # Generate corrected response
        config_args = {
            "max_output_tokens": 8192,
            "system_instruction": (
                "You are a JSON correction assistant. "
                "Your ONLY task is to output valid JSON following the exact structure shown. "
                f"CURRENT INTERFACE: {interface}. "
                f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'. "
                "NO explanations. NO markdown. ONLY valid JSON starting with { and ending with }."
            ),
        }

        return await self._http_generate_content(
            prompt_text=correction_prompt,
            system_instruction=config_args["system_instruction"],
            max_output_tokens=config_args.get("max_output_tokens", 8192),
        )


PLUGIN_CLASS = GeminiAPIPlugin
