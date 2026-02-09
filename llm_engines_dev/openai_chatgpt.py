# llm_engines/openai_chatgpt.py

from core.ai_plugin_base import AIPluginBase
import json
import os
import openai  # Assicurati che sia installato
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.transport_layer import llm_to_interface

# Register OpenAI API Key configuration
OPENAI_API_KEY = config_registry.get_value(
    "OPENAI_API_KEY",
    "",
    label="OpenAI API Key",
    description="API key for OpenAI ChatGPT models.",
    group="llm",
    component="openai_chatgpt",
    sensitive=True,
)

def _update_openai_key(value: str | None) -> None:
    global OPENAI_API_KEY
    OPENAI_API_KEY = value or ""
    if OPENAI_API_KEY:
        openai.api_key = OPENAI_API_KEY

config_registry.add_listener("OPENAI_API_KEY", _update_openai_key)

def get_user_api_key():
    """Get OpenAI API key from config or environment."""
    return OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")

# Model-specific configurations with appropriate prompt limits
MODEL_CONFIGS = {
    "gpt-3.5-turbo": {
        "max_prompt_chars": 12000,  # 4k context, ~12k chars
        "max_tokens": 4000,
        "supports_images": False,
        "supports_functions": True
    },
    "gpt-3.5-turbo-16k": {
        "max_prompt_chars": 48001,  # 16k context, ~48k chars
        "max_tokens": 4000,
        "supports_images": False,
        "supports_functions": True
    },
    "gpt-4": {
        "max_prompt_chars": 24000,  # 8k context, ~24k chars
        "max_tokens": 4000,
        "supports_images": False,
        "supports_functions": True
    },
    "gpt-4o": {
        "max_prompt_chars": 400000,  # 128k context, ~400k chars
        "max_tokens": 4000,
        "supports_images": True,
        "supports_functions": True
    }
}

# Base OpenAI configuration
OPENAI_CONFIG = {
    "max_response_chars": 4000,
    "default_model": "gpt-3.5-turbo",
    "temperature": 0.7,
    "api_timeout": 30
}

def get_openai_config() -> dict:
    """Get OpenAI-specific configuration."""
    return OPENAI_CONFIG.copy()

def get_max_prompt_chars(model_name: str = None) -> int:
    """Get maximum prompt characters for the specified model."""
    if model_name and model_name in MODEL_CONFIGS:
        return MODEL_CONFIGS[model_name]["max_prompt_chars"]
    # Fallback to default model
    default_model = OPENAI_CONFIG.get("default_model", "gpt-3.5-turbo")
    return MODEL_CONFIGS.get(default_model, {}).get("max_prompt_chars", 12000)

def get_max_response_chars() -> int:
    """Get maximum response characters for OpenAI."""
    return OPENAI_CONFIG["max_response_chars"]

def supports_images(model_name: str = None) -> bool:
    """Check if the specified model supports images."""
    if model_name and model_name in MODEL_CONFIGS:
        return MODEL_CONFIGS[model_name]["supports_images"]
    return False

def supports_functions(model_name: str = None) -> bool:
    """Check if the specified model supports functions."""
    if model_name and model_name in MODEL_CONFIGS:
        return MODEL_CONFIGS[model_name]["supports_functions"]
    return True

def get_interface_limits() -> dict:
    """Get the limits and capabilities for OpenAI ChatGPT interface."""
    # Get current model from active LLM or use default
    try:
        from core.config import get_active_cortex_engine
        active_engine = get_active_cortex_engine()
        model_name = active_llm.get("model_name", OPENAI_CONFIG["default_model"]) if active_llm else OPENAI_CONFIG["default_model"]
    except:
        model_name = OPENAI_CONFIG["default_model"]
    
    limits = {
        "max_prompt_chars": get_max_prompt_chars(model_name),
        "max_response_chars": OPENAI_CONFIG["max_response_chars"],
        "supports_images": supports_images(model_name),
        "supports_functions": supports_functions(model_name),
        "model_name": model_name
    }
    log_info(f"[openai_chatgpt] Interface limits for {model_name}: max_prompt_chars={limits['max_prompt_chars']}")
    return limits

class OpenAIPlugin(AIPluginBase):

    def __init__(self, notify_fn=None):
        from core.notifier import set_notifier
        from core.config import get_current_model

        self.reply_map = {}

        if notify_fn:
            log_debug("[openai] Uso funzione di notifica personalizzata.")
            set_notifier(notify_fn)
        else:
            log_debug("[openai] Nessuna funzione di notifica fornita, uso fallback.")
            set_notifier(lambda chat_id, message: log_info(f"[NOTIFY fallback] {message}"))

        self._current_model = get_current_model() or "gpt-3.5-turbo"

    def get_supported_models(self):
        return list(MODEL_CONFIGS.keys())

    def get_current_model(self):
        return self._current_model

    def get_max_prompt_chars_for_current_model(self):
        """Get max prompt chars for the currently selected model."""
        return get_max_prompt_chars(self._current_model)

    def set_current_model(self, name):
        if name not in self.get_supported_models():
            raise ValueError(f"Unsupported model: {name}")
        self._current_model = name
        max_chars = self.get_max_prompt_chars_for_current_model()
        log_debug(f"[openai] Active model updated: {name} (max_prompt_chars: {max_chars})")

    def get_target(self, trainer_message_id):
        return self.reply_map.get(trainer_message_id)

    def clear(self, trainer_message_id):
        self.reply_map.pop(trainer_message_id, None)

    async def handle_incoming_message(self, bot, message, prompt):
        from core.notifier import notify_trainer
        notify_trainer("🚨 Generating the reply...")

        try:
            response = await self.generate_response(prompt)

            if bot and message:
                log_debug(f"[openai] Invio risposta a chat_id={message.chat_id}")
                await llm_to_interface(
                    bot.send_message,
                    chat_id=message.chat_id,
                    text=response,
                    reply_to_message_id=getattr(message, 'message_id', None),
                    interface='telegram' if getattr(bot.__class__, '__module__', '').startswith('telegram') else 'generic',
                )

            return response

        except Exception as e:
            log_error(f"[OpenAI] Error while responding: {repr(e)}", e)
            notify_trainer(f"❌ OpenAI error:\n```\n{e}\n```")

            if bot and message:
                try:
                    from core.transport_layer import interface_to_llm
                except Exception:
                    interface_to_llm = None

                if interface_to_llm is None:
                    await bot.send_message(
                        chat_id=message.chat_id,
                        text="⚠️ LLM response error."
                    )
                else:
                    await interface_to_llm(bot.send_message, chat_id=message.chat_id, text="⚠️ LLM response error.")
            return "⚠️ Error during response generation."

    async def generate_response(self, prompt):
        # Use the module-level function that accesses OPENAI_API_KEY
        openai.api_key = get_user_api_key()

        messages = []

        # If there's an unminified chat instruction, send it first as a system message
        verbose = prompt.get("instructions_verbose")
        if verbose:
            messages.append({"role": "system", "content": verbose})

        # Include interface information in system message
        interface = prompt.get("input", {}).get("interface", "unknown")
        messages.append({
            "role": "system",
            "content": f"The message comes from the {interface} interface."
        })

        for entry in prompt.get("context", []):
            messages.append({
                "role": "user",
                "content": entry["text"]
            })

        if prompt.get("memories"):
            memory_text = "\n".join(f"- {m}" for m in prompt["memories"])
            messages.append({
                "role": "system",
                "content": f"[RELEVANT MEMORIES]\n{memory_text}"
            })

        # Correct path to the message text
        message_text = prompt.get("input", {}).get("payload", {}).get("text", "")
        messages.append({
            "role": "user",
            "content": message_text
        })

        log_debug(f"[openai] Invio a OpenAI con modello: {self._current_model}")
        response = openai.ChatCompletion.create(
            model=self._current_model,
            messages=messages
        )
        return response.choices[0].message.content.strip()


PLUGIN_CLASS = OpenAIPlugin
