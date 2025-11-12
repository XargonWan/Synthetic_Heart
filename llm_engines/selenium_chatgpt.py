# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug
from selenium.webdriver.common.by import By
import time

# ChatGPT configuration - constants only
SERVICE_URL = "https://chat.openai.com"
MODEL_CONFIG_VAR = "CHATGPT_MODEL"
DEFAULT_MODEL = "gpt-4o"

# ChatGPT-specific model limits (character context limits)
MODEL_LIMITS_MAP = {
    "gpt-4o": 128000,        # 128k tokens context (~400k characters)
    "gpt-4o-mini": 128000,   # 128k tokens context (~400k characters)
    "gpt-4-turbo": 128000,   # 128k tokens context (~400k characters)
    "gpt-4": 128000,         # 8k tokens context (~24k characters)
    "gpt-3.5-turbo": 16000,  # 16k tokens context (~48k characters)
    "o1-preview": 128000,    # 128k tokens context (~400k characters)
    "o1-mini": 128000,       # 128k tokens context (~400k characters)
    "unlogged": 21500,       # Limited context for free tier
    "default": 128000        # Safe default for unknown models
}

class SeleniumChatGPTPlugin(SeleniumLLMBase):
    display_name = "Selenium ChatGPT"
    
    def __init__(self, notify_fn=None):
        """Initialize the ChatGPT plugin - pass only configuration to base."""
        super().__init__(
            config={
                "service_url": SERVICE_URL,
                "interface_name": "chatgpt"
            },
            notify_fn=notify_fn
        )
        
        # Set model configuration - used by base class for auto-selection
        self.model_limits_map = MODEL_LIMITS_MAP
        self.model_config_var = MODEL_CONFIG_VAR
        self.default_model = DEFAULT_MODEL
        
        # Update interface limits based on current model
        self._update_interface_limits()
        
        # Set up ChatGPT-specific selectors - the base will use these for automation
        self.selectors["prompt_area"] = [
            "div.ProseMirror.ProseMirror-focused",
            "div.ProseMirror",
            "div[id='prompt-textarea']",
            "#prompt-textarea",
            "p[data-placeholder='Ask anything']",
            "textarea[data-testid='prompt-textarea']",
            "div[data-testid='prompt-textarea'][contenteditable='true']",
            "textarea[placeholder*='Message']",
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='Send a message']",
            "div[contenteditable='true'][role='textbox']",
            "div[role='textbox'][contenteditable='true']",
            "textarea",
            "div[contenteditable='true']",
        ]
        
        self.selectors["send_button"] = [
            "#composer-submit-button",
            "button[data-testid='send-button']",
            "button[data-testid*='send']",
            "button[type='submit']",
            "button[aria-label*='Send']",
            "button[aria-label*='send']",
            "button[title*='Send']",
            "button[title*='send']",
        ]
        
        self.selectors["response_text"] = [
            "div.markdown.prose",
            "[data-message-author-role='assistant']",
            "div.markdown",
            "[role='presentation'] .markdown",
            ".prose",
            "[data-testid*='conversation-turn'] .markdown",
            ".message-content .markdown",
        ]
        
        self.selectors["modal_dismissal"] = [
            "#radix-_r_3s_ > div > div.flex.flex-col.items-center.justify-center.self-center.px-6.py-6.md\\:w-\\[464px\\].md\\:py-8 > div > button.btn.relative.btn-secondary.btn-large.w-full",
            "div[data-modal-layer='overlay'] button",
            "div.fixed.inset-0.z-50 button",
            "div[role='dialog'] button[aria-label*='close' i]",
            "div[data-radix-dialog-overlay] button",
            "button[aria-label*='close'], button[aria-label*='Close']",
        ]
        
        # ChatGPT-specific login detection selectors
        # These will be used by the centralized is_user_logged_in() method from SeleniumLLMBase
        self.login_detection_selectors = [
            (By.CSS_SELECTOR, "button[data-testid='login-button']"),
            (By.CSS_SELECTOR, "a[href*='login']"),
            (By.ID, "login-button"),
            (By.CLASS_NAME, "login-button"),
            (By.XPATH, "//button[contains(text(), 'Log in')]"),
            (By.XPATH, "//button[contains(text(), 'Sign in')]"),
            (By.XPATH, "//a[contains(text(), 'Log in')]"),
            (By.XPATH, "//a[contains(text(), 'Sign in')]"),
        ]

    def _ensure_logged_in(self, driver) -> bool:
        """Ensure the user is logged in to ChatGPT."""
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        
        log_debug(f"[selenium_chatgpt] Checking login status at URL: {current_url}")

        if not current_url.startswith("https://chat.openai.com") and not current_url.startswith("https://chatgpt.com"):
            log_debug("[selenium_chatgpt] Not at ChatGPT, navigating to home")
            try:
                driver.get(SERVICE_URL)
                current_url = driver.current_url
                log_debug(f"[selenium_chatgpt] Navigated to {current_url}")
            except Exception as e:
                log_debug(f"[selenium_chatgpt] Failed to navigate to ChatGPT: {e}")
                return False

        if current_url and ("login" in current_url or "auth0" in current_url):
            log_debug("[selenium_chatgpt] Login page detected, user needs to log in")
            if self._notify_fn:
                self._notify_fn("🔐 Login required for ChatGPT. Open UI to log in.")
            return False

        log_debug("[selenium_chatgpt] User is logged in")
        return True

    def get_supported_models(self) -> list:
        """Get list of supported ChatGPT models."""
        return list(MODEL_LIMITS_MAP.keys())

    def get_current_model(self) -> str:
        """Get the current ChatGPT model being used.
        
        If logged in, returns the configured model or default.
        If not logged in, returns 'unlogged' with reduced limits.
        """
        # Auto-detection: if not logged in, use unlogged model
        if not self.is_user_logged_in():
            log_debug("[selenium_chatgpt] User not logged in, using 'unlogged' model")
            return "unlogged"
        
        # User is logged in, return configured model
        return self._get_current_model_name()

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for Selenium ChatGPT interface."""
        self._update_interface_limits()
        return self.interface_limits

    def _get_response_choice_selectors(self) -> list:
        """Get CSS selectors for ChatGPT response choice buttons.
        
        ChatGPT sometimes offers users a choice between multiple responses.
        This returns selectors to find the choice buttons so we can auto-select the first one.
        """
        return [
            "div.basis-auto > div > div.flex.flex-col > article > div > div > div.overflow-x-auto > div > div:first-child > div > button",
            "article button[data-testid*='response']",
            "div.snap-x button",
            "article .flex > button:first-child",
            "article button",
        ]

PLUGIN_CLASS = SeleniumChatGPTPlugin