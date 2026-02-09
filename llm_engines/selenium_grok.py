# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug
from core.variables_engine import register_exposed_var
from selenium.webdriver.common.by import By
import time

# Register exposed variables for WebUI
register_exposed_var(
    "GROK_MODEL",
    label="Grok Model",
    default="",
    value_type=str,
    ui_type="string",
    description="Model name for Grok API calls. Leave empty to use default.",
    scope="llm",
    component="selenium_grok",
    tags=["llm_engine"],
    hidden=True,  # Hide model selection until model-selection UX is improved
)

# Grok configuration - constants only
SERVICE_URL = "https://grok.x.ai"
MODEL_CONFIG_VAR = "GROK_MODEL"
DEFAULT_MODEL = "grok-beta"

# Grok-specific model limits (character context limits)
MODEL_LIMITS_MAP = {
    "grok-beta": 128001,           # Grok: 128k tokens context (~400k characters)
    "grok-vision-beta": 128001,    # Grok Vision: 128k tokens context (~400k characters)
    "unlogged": 21500,             # Limited context for free tier
    "default": 128001              # Safe default for unknown models
}

class SeleniumGrokPlugin(SeleniumLLMBase):
    display_name = "Selenium Grok"
    
    def __init__(self, notify_fn=None):
        """Initialize the Grok plugin - pass only configuration to base."""
        super().__init__(
            config={
                "service_url": SERVICE_URL,
                "interface_name": "grok"
            },
            notify_fn=notify_fn
        )
        
        # Set model configuration - used by base class for auto-selection
        self.model_limits_map = MODEL_LIMITS_MAP
        self.model_config_var = MODEL_CONFIG_VAR
        self.default_model = DEFAULT_MODEL
        
        # Update interface limits based on current model
        self._update_interface_limits()
        
        # Set up Grok-specific selectors - the base will use these for automation
        # IMPORTANT: Order matters - fast/working selectors first to avoid long timeouts
        self.selectors["prompt_area"] = [
            # Primary: Most specific Grok selector (found via browser inspection)
            "form textarea",
            "body > div.border-border.relative.h-svh.w-full.overflow-hidden.border-b.pb-px.md\\:overflow-x-hidden > div > div.mx-auto.w-full.px-4.lg\\:px-6.xl\\:max-w-7xl.flex.h-full.flex-col > div.relative.z-20.mt-20.flex.h-full.w-full.items-center > div > div > div > form > textarea",
            # Secondary: Slightly less specific
            "textarea[placeholder*='Message Grok']",
            "textarea[placeholder*='Ask Grok']",
            "textarea[data-testid='prompt-textarea']",
            # Tertiary: Alternative placeholders
            "textarea[placeholder*='What would you like to know?']",
            # Quaternary: Contenteditable divs
            "div[contenteditable='true'][data-placeholder*='Message']",
            "div[contenteditable='true'][aria-label*='Message']",
            # Generic fallbacks
            "div[role='textbox'][contenteditable='true']",
            "div.ql-editor.ql-blank",
            "div.ql-editor",
            "textarea",
            "div[contenteditable='true']",
        ]
        
        self.selectors["send_button"] = [
            # Primary: Most specific Grok send button selector (found via browser inspection)
            "form > div > button",
            "body > div.border-border.relative.h-svh.w-full.overflow-hidden.border-b.pb-px.md\\:overflow-x-hidden > div > div.mx-auto.w-full.px-4.lg\\:px-6.xl\\:max-w-7xl.flex.h-full.flex-col > div.relative.z-20.mt-20.flex.h-full.w-full.items-center > div > div > div > form > div > button",
            # Secondary: Less specific but common patterns
            "button[data-testid='send-button']",
            "button[type='submit']",
            "button[aria-label*='Send']",
            "button[aria-label*='send']",
            "button[title*='Send']",
            "button[title*='send']",
        ]
        
        self.selectors["response_text"] = [
            "div.grok-response",
            "[data-testid='grok-message']",
            ".response-text",
            ".response-content",
            "[data-role='assistant-message']",
            "div.message-response",
            ".chat-message.assistant",
        ]
        
        # Grok (X/Twitter) login detection selectors
        self.login_detection_selectors = [
            (By.CSS_SELECTOR, "a[href*='login']"),
            (By.CSS_SELECTOR, "button[data-testid='login-button']"),
            (By.XPATH, "//button[contains(text(), 'Log in')]"),
            (By.XPATH, "//button[contains(text(), 'Sign in')]"),
            (By.XPATH, "//a[contains(text(), 'Log in')]"),
            (By.XPATH, "//a[contains(text(), 'Sign in')]"),
        ]

    def _ensure_logged_in(self, driver) -> bool:
        """Ensure the user is logged in to Grok."""
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        
        log_debug(f"[selenium_grok] Checking login status at URL: {current_url}")

        if not current_url.startswith("https://grok.x.ai"):
            log_debug("[selenium_grok] Not at Grok, navigating to home")
            try:
                driver.get(SERVICE_URL)
                current_url = driver.current_url
                log_debug(f"[selenium_grok] Navigated to {current_url}")
            except Exception as e:
                log_debug(f"[selenium_grok] Failed to navigate to Grok: {e}")
                return False

        if current_url and ("login" in current_url or "auth" in current_url):
            log_debug("[selenium_grok] Login page detected, user needs to log in")
            if self._notify_fn:
                self._notify_fn("🔐 Login required for Grok. Open UI to log in.")
            return False

        log_debug("[selenium_grok] User is logged in")
        return True

    def get_supported_models(self) -> list:
        """Get list of supported Grok models."""
        return list(MODEL_LIMITS_MAP.keys())

    def get_current_model(self) -> str:
        """Get the current Grok model being used.
        
        If logged in, returns the configured model or default.
        If not logged in, returns 'unlogged' with reduced limits.
        """
        # Auto-detection: if not logged in, use unlogged model
        if not self.is_user_logged_in():
            log_debug("[selenium_grok] User not logged in, using 'unlogged' model")
            return "unlogged"
        
        # User is logged in, return configured model
        return self._get_current_model_name()

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for Selenium Grok interface."""
        self._update_interface_limits()
        return self.interface_limits

    def _get_response_choice_selectors(self) -> list:
        """Get CSS selectors for Grok response choice buttons.
        
        Grok may offer multiple response options in some cases.
        This returns selectors to find choice buttons so we can auto-select.
        """
        return [
            "button.grok-choice-option",
            ".choice-buttons button:first-child",
            "button[data-testid*='choice']",
        ]

PLUGIN_CLASS = SeleniumGrokPlugin
