# llm_engines/gemini_api.py
"""
Gemini API LLM Engine for Synthetic Heart.

This engine uses the Google GenAI SDK to communicate with Gemini models.
It follows the standard LLM engine architecture to ensure all plugins
(diary, emotions, bio_manager, etc.) work properly.
"""

from google import genai
from google.genai import types
from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning, log_error
import json
import asyncio

def _is_gemini_active(value: str | None) -> bool:
    return str(value or "").strip().lower() == "gemini_api"


def _update_gemini_key_visibility(active_value: str | None = None) -> None:
    try:
        active = active_value if active_value is not None else config_registry.get_value(
            "ACTIVE_LLM",
            "selenium_chatgpt",
        )
        defn = config_registry._definitions.get("GEMINI_API_KEY")
        if defn is not None:
            defn.hidden = not _is_gemini_active(active)
    except Exception as e:
        log_debug(f"[gemini_api] Failed to update GEMINI_API_KEY visibility: {e}")


# Register Gemini API Key configuration (visible only when Gemini API is active)
_is_active = _is_gemini_active(config_registry.get_value("ACTIVE_LLM", "selenium_chatgpt"))
GEMINI_API_KEY = config_registry.get_value(
    "GEMINI_API_KEY",
    "",
    label="Gemini API Key",
    description="API key for Google Gemini models.",
    group="llm",
    component="gemini_api",
    sensitive=True,
    hidden=not _is_active,
)

config_registry.add_listener("GEMINI_API_KEY", lambda v: globals().update(GEMINI_API_KEY=v or ""))
config_registry.add_listener("ACTIVE_LLM", _update_gemini_key_visibility)

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
    """Gemini API LLM Engine using Google GenAI SDK.
    
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
    
    display_name = "Gemini API (GenAI SDK)"

    def __init__(self, notify_fn=None):
        from core.notifier import set_notifier
        from core.config import get_current_model

        if notify_fn:
            set_notifier(notify_fn)
            self._notify_fn = notify_fn
        else:
            self._notify_fn = lambda chat_id, message: log_info(f"[NOTIFY fallback] {message}")
            set_notifier(self._notify_fn)

        self._current_model = get_current_model() or DEFAULT_MODEL
        if self._current_model not in MODEL_CONFIGS:
            self._current_model = DEFAULT_MODEL
        
        self.client = None
        self._init_client()
        
        # Track current request metadata for error handling
        self._current_request_meta = None
        
        # Model limits map for plugin_instance.py compatibility
        self.model_limits_map = MODEL_LIMITS_MAP
        
        log_info(f"[gemini_api] Initialized with model: {self._current_model}")

    def _init_client(self):
        """Initialize the Gemini API client."""
        if GEMINI_API_KEY:
            try:
                self.client = genai.Client(api_key=GEMINI_API_KEY)
                log_info("[gemini_api] Client initialized successfully")
            except Exception as e:
                log_error(f"[gemini_api] Failed to initialize client: {e}")
                self.client = None
        else:
            log_warning("[gemini_api] No API key configured")

    def get_supported_models(self) -> list[str]:
        """Return available model names."""
        return list(MODEL_CONFIGS.keys())

    def get_current_model(self) -> str:
        """Return the currently active model."""
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
        return (60, 60, 10)  # 60 requests per minute with burst of 10

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for this LLM interface."""
        model_config = MODEL_CONFIGS.get(self._current_model, MODEL_CONFIGS[DEFAULT_MODEL])
        return {
            "max_prompt_chars": model_config.get("max_prompt_chars", 1000000),
            "max_response_chars": model_config.get("max_output_tokens", 8192),
            "supports_images": True,
            "supports_functions": True,
            "model_name": self._current_model
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
                'bot': bot,
                'message': message,
                'interface': getattr(message, 'interface', None) or getattr(message, 'interface_path', None),
                'chat_id': getattr(message, 'chat_id', None),
                'interface_path': getattr(message, 'interface_path', None),
            }
            
            log_debug(f"[gemini_api] Processing message from chat_id={getattr(message, 'chat_id', 'unknown')}")
            
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
        
        if not self.client:
            self._init_client()
            if not self.client:
                return '{"actions": [{"type": "system_message", "payload": {"text": "⚠️ Failed to initialize Gemini client"}}]}'

        try:
            # Handle different prompt formats
            if isinstance(prompt, dict):
                # Check for system_message (correction scenario)
                if "system_message" in prompt:
                    return await self._handle_correction_prompt(prompt)
                
                # Standard JSON prompt from prompt_engine
                prompt_text = json.dumps(prompt, indent=2, ensure_ascii=False)
            elif isinstance(prompt, str):
                # Try to parse as JSON first
                try:
                    parsed = json.loads(prompt)
                    if isinstance(parsed, dict) and "system_message" in parsed:
                        return await self._handle_correction_prompt(parsed)
                    prompt_text = prompt
                except (json.JSONDecodeError, ValueError):
                    prompt_text = prompt
            else:
                prompt_text = str(prompt)
            
            log_debug(f"[gemini_api] Sending prompt ({len(prompt_text)} chars) to {self._current_model}")
            
            # Build generation config
            model_config = MODEL_CONFIGS.get(self._current_model, MODEL_CONFIGS[DEFAULT_MODEL])
            thinking_enabled = model_config.get("thinking", False)
            
            config_args = {
                "max_output_tokens": model_config.get("max_output_tokens", 8192),
            }

            # Configure thinking if enabled for this model
            if thinking_enabled:
                if "gemini-3" in self._current_model:
                    # Default to low for responsiveness as per docs recommendations for chat/apps
                    config_args["thinking_config"] = types.ThinkingConfig(thinking_level="low")
                else:
                    # Legacy for 2.0 Flash Thinking
                    config_args["thinking_config"] = types.ThinkingConfig(include_thoughts=False)
            
            # Build system instruction that enforces JSON output
            system_instruction = self._build_system_instruction(prompt)
            config_args["system_instruction"] = system_instruction

            generation_config = types.GenerateContentConfig(**config_args)

            # Make the API call - run in executor since it may be blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self._current_model,
                    contents=prompt_text,
                    config=generation_config
                )
            )
            
            response_text = response.text if response else ""
            
            log_debug(f"[gemini_api] Received response ({len(response_text)} chars)")
            
            return response_text
            
        except Exception as e:
            log_error(f"[gemini_api] Generation failed: {e}")
            # Return a JSON error so the system can handle it
            error_response = {
                "actions": [{
                    "type": "system_message",
                    "payload": {"text": f"⚠️ Gemini API error: {str(e)}"}
                }]
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
            interface = prompt_dict.get("interface") or prompt_dict.get("current_interface")
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
        action_full_schema = system_message.get("action_full_schema", {})
        
        # Extract interface from the prompt or system_message
        interface = system_message.get("interface") or prompt.get("interface") or "synth_webui"
        
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
        correction_prompt = (
            f"CORRECTION REQUIRED\n"
            f"\n"
            f"Error: {error_message}\n"
            f"\n"
        )
        
        if original_user_message:
            correction_prompt += f"Original user message you should respond to:\n\"{original_user_message}\"\n\n"
        
        if your_reply:
            correction_prompt += f"Your previous (invalid) reply:\n{your_reply[:500]}...\n\n"
        
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
            )
        }
        
        generation_config = types.GenerateContentConfig(**config_args)
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.generate_content(
                    model=self._current_model,
                    contents=correction_prompt,
                    config=generation_config
                )
            )
            
            return response.text if response else ""
            
        except Exception as e:
            log_error(f"[gemini_api] Correction generation failed: {e}")
            raise


PLUGIN_CLASS = GeminiAPIPlugin