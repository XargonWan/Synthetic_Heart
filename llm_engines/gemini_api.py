# llm_engines/gemini_api.py
"""
Gemini API LLM Engine for Synthetic Heart.

This engine uses the Gemini REST API to communicate with Gemini models.
It follows the standard LLM engine architecture to ensure all plugins
(diary, emotions, bio_manager, etc.) work properly.
"""

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning, log_error
import json
import asyncio
import requests

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
        tags=["llm_engine", "sensitive"],
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
        tags=["llm_engine"],
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
    tags=["llm_engine"],
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
        tags=["llm_engine"],
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
    tags=["llm_engine"],
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
        """
        return False

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
            "model_name": self._current_model,
        }

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
            return '{"actions": [{"type": "system_message", "payload": {"text": "⚠️ Gemini API Key not configured. Please set GEMINI_API_KEY in settings or .env"}}]}'

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
                max_output_tokens=config_args.get("max_output_tokens", 8192),
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

        # Minimal system instruction - the prompt itself contains full action schemas
        # We just need to remind the model to output valid JSON
        system_instruction = (
            "You are part of the 'Synthetic Heart' AI system.\n"
            "\n"
            "CRITICAL OUTPUT FORMAT:\n"
            "1. Respond with ONLY valid JSON - nothing before or after\n"
            "2. Your response MUST start with { and end with }\n"
            '3. Use this structure: {"actions": [{"type": "action_name", "payload": {...}}]}\n'
            "4. NO markdown code blocks, NO explanations outside JSON\n"
            "\n"
            f"CURRENT INTERFACE: {interface}\n"
            f"TO SEND A MESSAGE TO THE USER: Use action type '{message_action}'\n"
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
        self, prompt_text: str, system_instruction: str, max_output_tokens: int
    ) -> str:
        """Generate content using the Gemini REST API."""
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

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt_text}],
                }
            ],
            "systemInstruction": {
                "role": "system",
                "parts": [{"text": system_instruction}],
            },
            "generationConfig": {
                "maxOutputTokens": int(max_output_tokens),
            },
        }

        def _do_request():
            return requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=30,
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

        return response_text

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

        # Extract interface from the prompt or system_message
        interface = (
            system_message.get("interface") or prompt.get("interface") or "synth_webui"
        )

        log_warning(f"[gemini_api] Handling correction prompt: {error_type}")

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
