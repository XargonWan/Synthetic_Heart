# cortex/llm_provider/dev/gemini_cli.py

import subprocess
from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_debug, log_info, log_error
from core.notifier import set_notifier
from core.config_manager import config_registry
from core.transport_layer import llm_to_interface

# Register Gemini API Key configuration
GEMINI_API_KEY = config_registry.get_value(
    "GEMINI_API_KEY",
    "",
    label="Gemini API Key",
    description="API key used by the Gemini CLI LLM engine.",
    group="llm",
    component="gemini_cli",
    sensitive=True,
)


def _update_gemini_key(value: str | None) -> None:
    global GEMINI_API_KEY
    GEMINI_API_KEY = value or ""


config_registry.add_listener("GEMINI_API_KEY", _update_gemini_key)

# Gemini CLI-specific configuration
GEMINI_CLI_CONFIG = {
    "max_prompt_chars": 1000000,  # Gemini 1.5 Pro has 1M token context
    "max_response_chars": 3000,
    "supports_images": True,
    "supports_functions": True,
    "model_name": "2.5-flash",
    "default_model": "gemini-cli",
    "timeout": 30,
    "max_retries": 3,
    "temperature": 0.7,
}


def get_GEMINI_CLI_config() -> dict:
    """Get Gemini CLI-specific configuration."""
    return GEMINI_CLI_CONFIG.copy()


def get_max_prompt_chars() -> int:
    """Get maximum prompt characters for Gemini CLI."""
    return GEMINI_CLI_CONFIG["max_prompt_chars"]


def get_max_response_chars() -> int:
    """Get maximum response characters for Gemini CLI."""
    return GEMINI_CLI_CONFIG["max_response_chars"]


def supports_images() -> bool:
    """Check if Gemini CLI supports images."""
    return GEMINI_CLI_CONFIG["supports_images"]


def supports_functions() -> bool:
    """Check if Gemini CLI supports functions."""
    return GEMINI_CLI_CONFIG["supports_functions"]


def get_interface_limits() -> dict:
    """Get the limits and capabilities for Gemini CLI interface."""
    log_info(
        f"[GEMINI_CLI] Interface limits: max_prompt_chars={GEMINI_CLI_CONFIG['max_prompt_chars']}, supports_images={GEMINI_CLI_CONFIG['supports_images']}"
    )
    return {
        "max_prompt_chars": GEMINI_CLI_CONFIG["max_prompt_chars"],
        "max_response_chars": GEMINI_CLI_CONFIG["max_response_chars"],
        "supports_images": GEMINI_CLI_CONFIG["supports_images"],
        "supports_functions": GEMINI_CLI_CONFIG["supports_functions"],
        "model_name": GEMINI_CLI_CONFIG["model_name"],
    }


class GeminiCLIPlugin(AIPluginBase):
    """
    LLM plugin to use gemini-cli as backend.
    """

    def __init__(self, notify_fn=None):
        if notify_fn:
            log_debug("[gemini-cli] Using custom notification function.")
            set_notifier(notify_fn)
        else:
            log_debug("[gemini-cli] No notification function provided, using fallback.")
            set_notifier(
                lambda chat_id, message: log_info(f"[NOTIFY fallback] {message}")
            )

    async def generate_response(self, messages):
        """
        Sends the request to gemini-cli and returns the response.
        """
        prompt = messages[-1]["content"] if messages else ""
        log_debug(f"[gemini] Sent prompt: {prompt}")
        try:
            result = subprocess.run(
                ["gemini", "--token", GEMINI_API_KEY, prompt],
                capture_output=True,
                text=True,
                timeout=30,
            )
            log_debug(f"[gemini] stdout: {result.stdout.strip()}")
            log_debug(f"[gemini] stderr: {result.stderr.strip()}")
            if result.returncode != 0:
                log_error(f"gemini error: {result.stderr}")
                return "gemini error: " + result.stderr
            return result.stdout.strip()
        except Exception as e:
            log_error(f"Exception in gemini: {e}")
            return f"Error: {e}"

    def get_supported_models(self):
        return ["gemini-cli"]

    def get_current_model(self):
        return "gemini-cli"

    def set_current_model(self, name):
        if name != "gemini-cli":
            raise ValueError(f"Unsupported model: {name}")

    async def handle_incoming_message(self, bot, message, prompt: dict):
        """
        Handles an incoming message using gemini-cli.
        """
        # Correct path to the message text
        query = prompt.get("input", {}).get("payload", {}).get("text", "")
        if not query:
            try:
                from core.transport_layer import interface_to_llm
            except Exception:
                interface_to_llm = None
            if interface_to_llm is None:
                await bot.send_message(message.chat_id, "⚠️ No query provided.")
            else:
                await interface_to_llm(
                    bot.send_message,
                    chat_id=message.chat_id,
                    text="⚠️ No query provided.",
                )
            return

        # Include unminified chat instruction as a system message when present
        system_messages = []
        verbose = prompt.get("instructions_verbose")
        if verbose:
            system_messages.append({"role": "system", "content": verbose})

        # Include interface in the query for the LLM
        interface = prompt.get("input", {}).get("interface", "unknown")
        full_query = f"Message from {interface} interface: {query}"

        # Build messages list with optional system message
        messages = system_messages + [{"role": "user", "content": full_query}]

        response = await self.generate_response(messages)
        # Forward model output through the centralized LLM->interface path
        await llm_to_interface(
            bot.send_message,
            chat_id=message.chat_id,
            text=response,
            interface="telegram"
            if getattr(bot.__class__, "__module__", "").startswith("telegram")
            else "generic",
        )


# Ensure the plugin loader can locate the plugin class
PLUGIN_CLASS = GeminiCLIPlugin
