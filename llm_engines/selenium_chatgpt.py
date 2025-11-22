# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug
from selenium.webdriver.common.by import By
import time

# ChatGPT configuration - constants only
SERVICE_URL = "https://chat.openai.com"
MODEL_CONFIG_VAR = "CHATGPT_MODEL"
DEFAULT_MODEL = "gpt-4o"

# ChatGPT-specific model limits - ALL VALUES ARE IN CHARACTERS, NOT TOKENS
# These are practical character limits for transmission via Selenium UI textarea
# The numbers represent the maximum number of characters we will send in a single prompt
# These are CONSERVATIVE limits to work reliably without ChatGPT Plus
MODEL_LIMITS_MAP = {
    "gpt-4o": 60000,         # 60,000 characters max
    "gpt-4o-mini": 60000,    # 60,000 characters max
    "gpt-4-turbo": 50000,    # 50,000 characters max
    "gpt-4": 40000,          # 40,000 characters max
    "gpt-3.5-turbo": 30000,  # 30,000 characters max
    "o1-preview": 50000,     # 50,000 characters max
    "o1-mini": 50000,        # 50,000 characters max
    "unlogged": 20000,       # 20,000 characters max (free tier limited)
    "default": 51000         # 51,000 characters max (safe default for unknown models)
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
        
        # Register the ChatGPT limits globally so selenium_llm_base can use them
        from core.selenium_llm_base import set_active_selenium_limits
        default_limit = MODEL_LIMITS_MAP.get("default", 51000)
        set_active_selenium_limits(default_limit, "chatgpt")
        
        # Set up ChatGPT-specific selectors - the base will use these for automation
        # IMPORTANT: Order matters - fast/working selectors first, general fallbacks last
        self.selectors["prompt_area"] = [
            # Primary: Most reliable and specific for current ChatGPT UI
            "textarea[data-testid='prompt-textarea']",
            "div[data-testid='prompt-textarea'][contenteditable='true']",
            "div.ProseMirror.ProseMirror-focused",
            "div.ProseMirror",
            # Secondary: ID-based selectors (very specific)
            "div[id='prompt-textarea']",
            "#prompt-textarea",
            # Tertiary: Placeholder-based (more reliable than generic)
            "p[data-placeholder='Ask anything']",
            "textarea[placeholder*='Message']",
            "textarea[placeholder*='Ask']",
            "textarea[placeholder*='Send a message']",
            # Quaternary: Role-based selectors (generic but useful)
            "div[contenteditable='true'][role='textbox']",
            "div[role='textbox'][contenteditable='true']",
            # Fallbacks: Generic (slowest, try last)
            "textarea",
            "div[contenteditable='true']",
        ]
        
        self.selectors["send_button"] = [
            # Primary: Most specific ChatGPT send button ID
            "#composer-submit-button",
            # Secondary: data-testid (very reliable)
            "button[data-testid='send-button']",
            "button[data-testid*='send']",
            # Tertiary: aria-label and title (more generic but still specific)
            "button[aria-label*='Send']",
            "button[aria-label*='send']",
            "button[title*='Send']",
            "button[title*='send']",
            # Fallback: Type-based (slowest, most likely to fail or find wrong button)
            "button[type='submit']",
        ]
        
        self.selectors["response_text"] = [
            # Primary: Most specific ChatGPT response selectors
            "[data-message-author-role='assistant']",
            "div.markdown.prose",
            # Secondary: Slightly less specific
            "[role='presentation'] .markdown",
            "[data-testid*='conversation-turn'] .markdown",
            # Tertiary: Generic class-based
            "div.markdown",
            ".prose",
            ".message-content .markdown",
        ]
        
        self.selectors["modal_dismissal"] = [
            # Primary: Specific modal dismissal buttons
            "button[aria-label*='close' i]",
            "button[aria-label*='Close']",
            # Secondary: Dialog-based
            "div[role='dialog'] button",
            "div[data-radix-dialog-overlay] button",
            # Tertiary: Fixed positioned overlays
            "div.fixed.inset-0.z-50 button",
            "div[data-modal-layer='overlay'] button",
            # Fallback: Radix-specific (long xpath-like selectors)
            "#radix-_r_3s_ > div > div.flex.flex-col.items-center.justify-center.self-center.px-6.py-6.md\\:w-\\[464px\\].md\\:py-8 > div > button.btn.relative.btn-secondary.btn-large.w-full",
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
        Also updates the global selenium limits when model changes.
        """
        # Auto-detection: if not logged in, use unlogged model
        if not self.is_user_logged_in():
            log_debug("[selenium_chatgpt] User not logged in, using 'unlogged' model")
            model = "unlogged"
        else:
            # User is logged in, return configured model
            model = self._get_current_model_name()
        
        # Update global limits for this model
        if model in self.model_limits_map:
            limit = self.model_limits_map[model]
            from core.selenium_llm_base import set_active_selenium_limits
            set_active_selenium_limits(limit, f"chatgpt_{model}")
            log_debug(f"[selenium_chatgpt] Updated global limits for model {model}: {limit} chars")
        
        return model

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