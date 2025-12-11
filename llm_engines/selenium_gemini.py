# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug
from selenium.webdriver.common.by import By
import time

# Gemini configuration - constants only
SERVICE_URL = "https://gemini.google.com"
MODEL_CONFIG_VAR = "GEMINI_MODEL"
DEFAULT_MODEL = "2.5-flash"

# Gemini-specific model limits (character context limits)
MODEL_LIMITS_MAP = {
    "2.5-flash": 32000,        # Gemini 2.5 Flash: 32k characters practical limit
    "2.0-flash": 32000,        # Gemini 2.0 Flash: 32k characters
    "1.5-flash": 100000,       # Gemini 1.5 Flash: ~100k characters
    "1.5-pro": 500000,         # Gemini 1.5 Pro: ~500k characters (2M tokens)
    "unlogged": 21500,         # Limited context for free tier
    "default": 32000           # Safe default for unknown models
}

class SeleniumGeminiPlugin(SeleniumLLMBase):
    display_name = "Selenium Gemini"
    
    def __init__(self, notify_fn=None):
        """Initialize the Gemini plugin - pass only configuration to base."""
        super().__init__(
            config={
                "service_url": SERVICE_URL,
                "interface_name": "gemini"
            },
            notify_fn=notify_fn
        )
        
        # Set model configuration - used by base class for auto-selection
        self.model_limits_map = MODEL_LIMITS_MAP
        self.model_config_var = MODEL_CONFIG_VAR
        self.default_model = DEFAULT_MODEL
        
        # Update interface limits based on current model
        self._update_interface_limits()
        
        # Set up Gemini-specific selectors - the base will use these for automation
        # IMPORTANT: Order matters - fast/working selectors first to avoid long timeouts
        self.selectors["prompt_area"] = [
            # Primary: Works on current Gemini UI
            "div[contenteditable='true'][data-placeholder]",
            "div[contenteditable='true'][aria-label*='Ask']",
            # Secondary: Fallback for older or alternative Gemini layouts
            "rich-textarea[data-placeholder]",
            "textarea[data-placeholder*='Ask me anything']",
            "textarea[data-placeholder*='Message Gemini']",
            "textarea[placeholder*='Ask me anything']",
            "textarea[placeholder*='Message Gemini']",
            # Generic fallbacks
            "div[role='textbox'][contenteditable='true']",
            "div.ql-editor.ql-blank",
            "div.ql-editor",
            "textarea",
            "div[contenteditable='true']",
        ]
        
        self.selectors["send_button"] = [
            "button[data-testid='send-button']",
            "button[type='submit']",
            "button[aria-label*='Send']",
            "button[aria-label*='send']",
            "button[title*='Send']",
            "button[title*='send']",
        ]
        
        self.selectors["response_text"] = [
            "div.assistant-message",
            "[data-author='assistant']",
            ".chat-message.ai",
            ".response-content",
            "article p",
            ".message-content",
            ".gemini-response",
        ]
        
        # Google login detection selectors
        self.login_detection_selectors = [
            (By.CSS_SELECTOR, "button[data-testid='login-button']"),
            (By.CSS_SELECTOR, "a[href*='signin']"),
            (By.XPATH, "//button[contains(text(), 'Sign in')]"),
            (By.XPATH, "//a[contains(text(), 'Sign in')]"),
            (By.CSS_SELECTOR, ".sign-in-button"),
        ]

    def _ensure_logged_in(self, driver) -> bool:
        """Ensure the user is logged in to Google Gemini."""
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        
        log_debug(f"[selenium_gemini] Checking login status at URL: {current_url}")

        if not current_url.startswith("https://gemini.google.com"):
            log_debug("[selenium_gemini] Not at Gemini, navigating to home")
            try:
                driver.get(SERVICE_URL)
                current_url = driver.current_url
                log_debug(f"[selenium_gemini] Navigated to {current_url}")
            except Exception as e:
                log_debug(f"[selenium_gemini] Failed to navigate to Gemini: {e}")
                return False

        if current_url and ("signin" in current_url or "login" in current_url):
            log_debug("[selenium_gemini] Login page detected, user needs to log in")
            if self._notify_fn:
                self._notify_fn("🔐 Login required for Google Gemini. Open UI to log in.")
            return False

        log_debug("[selenium_gemini] User is logged in")
        return True

    def get_supported_models(self) -> list:
        """Get list of supported Gemini models."""
        return list(MODEL_LIMITS_MAP.keys())

    def get_current_model(self) -> str:
        """Get the current Gemini model being used.
        
        If logged in, returns the configured model or default.
        If not logged in, returns 'unlogged' with reduced limits.
        """
        # Auto-detection: if not logged in, use unlogged model
        if not self.is_user_logged_in():
            log_debug("[selenium_gemini] User not logged in, using 'unlogged' model")
            return "unlogged"
        
        # User is logged in, return configured model
        return self._get_current_model_name()

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for Selenium Gemini interface."""
        self._update_interface_limits()
        return self.interface_limits

    def _get_response_choice_selectors(self) -> list:
        """Get CSS selectors for Gemini response choice buttons.
        
        Gemini may offer multiple response options in some cases.
        This returns selectors to find choice buttons so we can auto-select.
        """
        return [
            "button.gemini-choice-option",
            ".choice-buttons button:first-child",
            "button[data-testid*='choice']",
        ]

PLUGIN_CLASS = SeleniumGeminiPlugin
