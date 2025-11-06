# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug, log_info, log_warning, log_error
from selenium.webdriver.common.by import By
import time

# Selenium ChatGPT-specific configuration
# Model-specific character limits (based on official documentation and testing)
CHATGPT_MODEL_LIMITS = {
    "gpt-4o": 128000,        # GPT-4o: 128k tokens context (~400k characters)
    "gpt-4o-mini": 128000,   # GPT-4o-mini: 128k tokens context (~400k characters)
    "gpt-4-turbo": 128000,   # GPT-4 Turbo: 128k tokens context (~400k characters)
    "gpt-4": 8000,           # GPT-4: 8k tokens context (~24k characters)
    "gpt-3.5-turbo": 16000,  # GPT-3.5 Turbo: 16k tokens context (~48k characters)
    "o1-preview": 128000,    # o1-preview: 128k tokens context (~400k characters)
    "o1-mini": 128000,       # o1-mini: 128k tokens context (~400k characters)
    "unlogged": 21500,       # Unlogged state: limited context for free tier
    "default": 128000        # Safe default for unknown models (assume newer models)
}

SELENIUM_CONFIG = {
    "max_prompt_chars": 128000,  # Default to gpt-4o limit
    "max_response_chars": 4000,
    "supports_images": True,
    "supports_functions": False,  # Browser-based doesn't support functions
    "model_name": "gpt-4o",
    "default_model": "gpt-4o",
    "browser_timeout": 30,
    "page_load_timeout": 60,
    "element_wait_timeout": 10,
    "retry_attempts": 3,
    "retry_delay": 2
}

def get_model_char_limit(model_name: str) -> int:
    """Get the character limit for a specific ChatGPT model."""
    # Normalize model name (lowercase, strip)
    normalized = model_name.lower().strip()
    
    # Check direct match first
    if normalized in CHATGPT_MODEL_LIMITS:
        return CHATGPT_MODEL_LIMITS[normalized]
    
    # Try to match partial names (e.g., "chatgpt-4o" -> "gpt-4o")
    for key in CHATGPT_MODEL_LIMITS.keys():
        if key in normalized or normalized.endswith(key):
            return CHATGPT_MODEL_LIMITS[key]
    
    # Special case: check for model variants
    if "4o" in normalized:
        if "mini" in normalized:
            return CHATGPT_MODEL_LIMITS["gpt-4o-mini"]
        return CHATGPT_MODEL_LIMITS["gpt-4o"]
    elif "turbo" in normalized:
        if "3.5" in normalized or "3-5" in normalized:
            return CHATGPT_MODEL_LIMITS["gpt-3.5-turbo"]
        return CHATGPT_MODEL_LIMITS["gpt-4-turbo"]
    elif "o1" in normalized:
        if "mini" in normalized:
            return CHATGPT_MODEL_LIMITS["o1-mini"]
        return CHATGPT_MODEL_LIMITS["o1-preview"]
    elif "gpt-4" in normalized:
        return CHATGPT_MODEL_LIMITS["gpt-4"]
    
    # Return default if no match found
    from core.logging_utils import log_warning
    log_warning(f"[selenium_chatgpt] Unknown model '{model_name}', using default limit of {CHATGPT_MODEL_LIMITS['default']} chars")
    return CHATGPT_MODEL_LIMITS["default"]

def get_interface_limits() -> dict:
    """Get the limits and capabilities for Selenium ChatGPT interface."""
    # Get current model and its specific limit
    from core.config_manager import config_registry
    CHATGPT_MODEL = config_registry.get_value("CHATGPT_MODEL", "")
    model_name = CHATGPT_MODEL or SELENIUM_CONFIG.get("default_model", "gpt-4o")
    max_chars = get_model_char_limit(model_name)
    
    from core.logging_utils import log_info, log_debug
    log_info(f"[selenium_chatgpt] Interface limits for model '{model_name}': max_prompt_chars={max_chars}, supports_images={SELENIUM_CONFIG['supports_images']}")
    log_debug(f"[selenium_chatgpt] CHATGPT_MODEL config: '{CHATGPT_MODEL}', SELENIUM_CONFIG default_model: '{SELENIUM_CONFIG.get('default_model', 'gpt-4o')}'")
    return {
        "max_prompt_chars": max_chars,
        "max_response_chars": SELENIUM_CONFIG["max_response_chars"],
        "supports_images": SELENIUM_CONFIG["supports_images"],
        "supports_functions": SELENIUM_CONFIG["supports_functions"],
        "model_name": model_name
    }

# Global model configuration
from core.config_manager import config_registry
CHATGPT_MODEL = config_registry.get_value("CHATGPT_MODEL", "")

class SeleniumChatGPTPlugin(SeleniumLLMBase):
    display_name = "Selenium ChatGPT"
    
    def __init__(self, notify_fn=None):
        """Initialize the ChatGPT plugin."""
        # ChatGPT-specific configuration
        chatgpt_config = SELENIUM_CONFIG.copy()
        chatgpt_config.update({
            "service_url": "https://chat.openai.com",
            "model": CHATGPT_MODEL or "gpt-4o",
            "interface_name": "chatgpt"
        })
        
        super().__init__(config=chatgpt_config, notify_fn=notify_fn)
        
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
        
        # Track if we've already checked login status on startup
        self._login_status_checked = False

    def _ensure_logged_in(self, driver) -> bool:
        """Ensure the user is logged in to ChatGPT."""
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        log_debug(f"[selenium_chatgpt] _ensure_logged_in called, current URL: {current_url}")

        if not current_url.startswith("https://chat.openai.com") and not current_url.startswith("https://chatgpt.com"):
            try:
                log_debug("[selenium_chatgpt] Navigating to ChatGPT home")
                driver.get("https://chat.openai.com")
                current_url = driver.current_url
                log_debug(f"[selenium_chatgpt] Navigated to {current_url}")
            except Exception as e:
                log_warning(f"[selenium_chatgpt] Failed to navigate to ChatGPT home: {e}")
                return False

        if current_url and ("login" in current_url or "auth0" in current_url):
            log_debug("[selenium_chatgpt] Login required, notifying user")
            # Notify user to log in
            if self._notify_fn:
                self._notify_fn("🔐 Login required for ChatGPT. Open UI to log in.")
            return False

        log_debug("[selenium_chatgpt] Logged in and ready")
        return True

    def _locate_prompt_area(self, driver, timeout: int = 10):
        """Locate the ChatGPT prompt input area.
        
        Based on the legacy version that worked, prioritizing the main selectors.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        # Primary selectors from legacy version that worked
        primary_selectors = [
            (By.ID, "prompt-textarea"),  # Main ChatGPT textarea ID
            (By.XPATH, "//div[@contenteditable='true' and @id='prompt-textarea']"),  # Fallback for contenteditable div
        ]
        
        # Try primary selectors first (from legacy)
        for by, selector in primary_selectors:
            try:
                log_debug(f"[selenium_chatgpt] Trying primary selector: {by} = '{selector}'")
                element = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((by, selector))
                )
                log_debug(f"[selenium_chatgpt] Found element with primary selector: {by} = '{selector}'")
                return element
            except Exception as e:
                log_debug(f"[selenium_chatgpt] Primary selector failed: {by} = '{selector}' - {e}")
                continue
        
        # Additional selectors as fallback (from current version)
        fallback_selectors = [
            # ProseMirror editor selectors
            (By.CSS_SELECTOR, "div.ProseMirror.ProseMirror-focused"),
            (By.CSS_SELECTOR, "div.ProseMirror"),
            (By.CSS_SELECTOR, "div[id='prompt-textarea']"),
            (By.CSS_SELECTOR, "p[data-placeholder='Ask anything']"),
            (By.CSS_SELECTOR, "div.ProseMirror p[data-placeholder]"),
            # Additional ChatGPT-specific selectors
            (By.CSS_SELECTOR, "textarea[data-id='prompt-textarea']"),
            (By.CSS_SELECTOR, "#prompt-textarea"),  # Fallback to ID selector
            (By.CSS_SELECTOR, "textarea[data-testid='prompt-textarea']"),
            (By.CSS_SELECTOR, "div[data-testid='prompt-textarea'][contenteditable='true']"),
            # Rich text editor selectors
            (By.CSS_SELECTOR, "div[data-contents='true']"),
            (By.CSS_SELECTOR, "div.ql-editor"),
            (By.CSS_SELECTOR, "div.ql-editor.ql-blank"),
            # Placeholder-based selectors
            (By.CSS_SELECTOR, "textarea[placeholder*='Message']"),
            (By.CSS_SELECTOR, "textarea[placeholder*='Ask']"),
            (By.CSS_SELECTOR, "textarea[placeholder*='Send a message']"),
            # Contenteditable divs
            (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
            (By.CSS_SELECTOR, "div[role='textbox'][contenteditable='true']"),
            # Generic fallbacks
            (By.TAG_NAME, "textarea"),
            (By.CSS_SELECTOR, "div[contenteditable='true']"),
        ]
        
        for by, selector in fallback_selectors:
            try:
                log_debug(f"[selenium_chatgpt] Trying fallback selector: {by} = '{selector}'")
                element = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((by, selector))
                )
                log_debug(f"[selenium_chatgpt] Found element with fallback selector: {by} = '{selector}'")
                return element
            except Exception as e:
                log_debug(f"[selenium_chatgpt] Fallback selector failed: {by} = '{selector}' - {e}")
                continue
        
        log_error("[selenium_chatgpt] Could not locate ChatGPT prompt input area with any selector")
        return None

    def _find_send_button(self, driver, timeout=5):
        """Find the send/submit button in ChatGPT interface."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from core.logging_utils import log_debug
        
        # Try multiple selectors for send button (most specific first)
        send_button_selectors = [
            (By.CSS_SELECTOR, "#composer-submit-button"),  # User provided specific selector
            (By.CSS_SELECTOR, "button[data-testid='send-button']"),
            (By.CSS_SELECTOR, "button[data-testid*='send']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "button[aria-label*='Send']"),
            (By.CSS_SELECTOR, "button[aria-label*='send']"),
            (By.CSS_SELECTOR, "button[title*='Send']"),
            (By.CSS_SELECTOR, "button[title*='send']"),
            (By.CSS_SELECTOR, "svg[data-testid*='send']"),
            (By.CSS_SELECTOR, "div[data-testid*='send']"),
            # Generic button with send icon
            (By.CSS_SELECTOR, "button svg path[d*='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z']"),
            # Fallback: any button near the textarea
            (By.XPATH, "//textarea/ancestor::div[1]//button"),
        ]
        
        for by, selector in send_button_selectors:
            try:
                log_debug(f"[selenium_chatgpt] Trying send button selector: {by} = '{selector}'")
                button = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((by, selector))
                )
                log_debug(f"[selenium_chatgpt] Found send button with selector: {by} = '{selector}'")
                return button
            except Exception as e:
                log_debug(f"[selenium_chatgpt] Send button selector failed: {by} = '{selector}' - {e}")
                continue
        
        log_debug("[selenium_chatgpt] No send button found with any selector")
        return None

    def _send_prompt_with_confirmation(self, textarea, prompt_text: str) -> None:
        """Send the prompt to ChatGPT and confirm it was sent successfully."""
        try:
            # Use the provided textarea instead of locating it again
            if not textarea:
                from core.logging_utils import log_error
                log_error("[selenium] No textarea provided")
                return
            
            # Filter out non-BMP characters that ChromeDriver can't handle
            def filter_bmp_chars(text):
                """Filter out characters outside the Basic Multilingual Plane (BMP) that ChromeDriver can't handle."""
                return ''.join(char for char in text if ord(char) <= 0xFFFF)
            
            filtered_prompt = filter_bmp_chars(prompt_text)
            if len(filtered_prompt) != len(prompt_text):
                from core.logging_utils import log_warning
                removed_chars = len(prompt_text) - len(filtered_prompt)
                log_warning(f"[selenium_chatgpt] Filtered {removed_chars} non-BMP characters from prompt")
            
            # Check prompt length and truncate if too long
            # ChatGPT's web interface can handle much larger prompts than 10000 chars
            # We use 100000 as a more realistic limit for the textarea content
            max_prompt_length = 100000  # Realistic limit for ChatGPT web textarea
            if len(filtered_prompt) > max_prompt_length:
                original_length = len(filtered_prompt)
                filtered_prompt = filtered_prompt[:max_prompt_length]
                log_warning(f"[selenium_chatgpt] Prompt truncated from {original_length} to {max_prompt_length} characters")
            
            from core.logging_utils import log_debug
            log_debug(f"[selenium_chatgpt] About to clear textarea and send prompt")
            
            # Wait for textarea to be ready for input
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from core.logging_utils import log_debug
            
            log_debug(f"[selenium_chatgpt] Waiting for textarea to be clickable")
            WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(textarea)
            )
            log_debug(f"[selenium_chatgpt] Textarea is clickable")
            
            # Determine if it's a textarea or contenteditable div
            tag_name = textarea.tag_name.lower()
            is_textarea = tag_name == "textarea"
            is_contenteditable = textarea.get_attribute("contenteditable") == "true"
            
            log_debug(f"[selenium_chatgpt] Element type: tag={tag_name}, is_textarea={is_textarea}, is_contenteditable={is_contenteditable}")
            
            # Check current content before clearing
            if is_textarea:
                current_value = textarea.get_attribute("value") or ""
            else:
                current_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium_chatgpt] Current textarea value before clear: '{current_value[:100] if len(current_value) > 100 else current_value}' (length: {len(current_value)})")
            
            # Clear any existing text
            log_debug(f"[selenium_chatgpt] Clearing textarea")
            if is_textarea:
                textarea.clear()
            else:
                # For contenteditable divs, use JavaScript to clear
                self.driver.execute_script("arguments[0].textContent = '';", textarea)
            log_debug(f"[selenium_chatgpt] Textarea cleared")
            
            # Check content after clearing
            if is_textarea:
                after_clear_value = textarea.get_attribute("value") or ""
            else:
                after_clear_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium_chatgpt] Textarea value after clear: '{after_clear_value}' (length: {len(after_clear_value)})")
            
            # Paste the filtered prompt text
            log_debug(f"[selenium_chatgpt] Sending text to textarea: '{filtered_prompt[:100]}...' (length: {len(filtered_prompt)})")
            
            if is_textarea:
                # For textarea elements, use send_keys
                try:
                    textarea.send_keys(filtered_prompt)
                    log_debug(f"[selenium_chatgpt] Keys sent to textarea via send_keys")
                except Exception as send_keys_error:
                    log_debug(f"[selenium_chatgpt] send_keys failed: {send_keys_error}")
                    # Try alternative method: use JavaScript to set value
                    try:
                        self.driver.execute_script("arguments[0].value = arguments[1];", textarea, filtered_prompt)
                        # Trigger input event to make sure ChatGPT detects the change
                        self.driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", textarea)
                        log_debug(f"[selenium_chatgpt] Text set via JavaScript")
                    except Exception as js_error:
                        log_debug(f"[selenium_chatgpt] JavaScript method also failed: {js_error}")
                        raise send_keys_error  # Re-raise original error
            else:
                # For contenteditable divs, use JavaScript to insert text
                log_debug(f"[selenium_chatgpt] Inserting text via JavaScript for contenteditable div")
                try:
                    # Focus the element first
                    self.driver.execute_script("arguments[0].focus();", textarea)
                    # Set the text content
                    self.driver.execute_script("arguments[0].textContent = arguments[1];", textarea, filtered_prompt)
                    # Trigger input and change events
                    self.driver.execute_script("""
                        const elem = arguments[0];
                        elem.dispatchEvent(new Event('input', { bubbles: true }));
                        elem.dispatchEvent(new Event('change', { bubbles: true }));
                    """, textarea)
                    log_debug(f"[selenium_chatgpt] Text injected via JavaScript for contenteditable")
                except Exception as js_error:
                    log_error(f"[selenium_chatgpt] Failed to inject text via JavaScript: {js_error}")
                    raise
            
            # Check final content
            if is_textarea:
                final_value = textarea.get_attribute("value") or ""
            else:
                final_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium_chatgpt] Final textarea value: '{final_value[:100] if len(final_value) > 100 else final_value}' (length: {len(final_value)})")
            
            # Wait a bit for ChatGPT to process the input before trying to send
            import time
            log_debug("[selenium_chatgpt] Waiting 2 seconds for ChatGPT to process input...")
            time.sleep(2)
            log_debug("[selenium_chatgpt] Wait completed, now trying to send")
            
            log_debug(f"[selenium_chatgpt] Prompt pasted, now trying to send")
            
            # Try to find and click send button first
            send_button = self._find_send_button(self.driver, timeout=3)
            if send_button:
                from core.logging_utils import log_debug
                log_debug(f"[selenium_chatgpt] Send button found: {send_button.tag_name} with text: '{send_button.text}'")
                
                # Check if button is enabled
                is_enabled = send_button.is_enabled()
                log_debug(f"[selenium_chatgpt] Send button enabled: {is_enabled}")
                
                if is_enabled:
                    log_debug("[selenium_chatgpt] Clicking send button")
                    try:
                        send_button.click()
                        log_debug("[selenium_chatgpt] Send button clicked successfully")
                    except Exception as click_error:
                        log_debug(f"[selenium_chatgpt] Send button click failed: {click_error}")
                        # Fallback to RETURN
                        log_debug("[selenium_chatgpt] Falling back to RETURN key")
                        from selenium.webdriver.common.keys import Keys
                        textarea.click()
                        textarea.send_keys(Keys.RETURN)
                else:
                    log_debug("[selenium_chatgpt] Send button is disabled, using RETURN key")
                    from selenium.webdriver.common.keys import Keys
                    textarea.click()
                    textarea.send_keys(Keys.RETURN)
            else:
                # Fallback: Send the message using RETURN key
                from core.logging_utils import log_debug
                log_debug("[selenium_chatgpt] No send button found, using RETURN key")
                from selenium.webdriver.common.keys import Keys
                textarea.click()
                textarea.send_keys(Keys.RETURN)
            
            log_debug(f"[selenium_chatgpt] Send action completed, waiting for confirmation")
            # Wait for confirmation that the prompt was sent
            # Check that the input area is cleared or that a sending indicator appears
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from core.logging_utils import log_debug
            
            log_debug("[selenium_chatgpt] Waiting for prompt confirmation...")
            
            # Add timeout handling for the wait
            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: (
                        textarea.get_attribute("value") == "" or
                        textarea.text == "" or
                        len(d.find_elements(By.CSS_SELECTOR, "[data-testid*='sending'], [data-testid*='send'], .sending, .loading, button[data-testid*='send-button'], button[type='submit']")) > 0
                    )
                )
                log_debug("[selenium_chatgpt] Prompt sent successfully")
            except Exception as wait_error:
                log_debug(f"[selenium_chatgpt] Wait for confirmation timed out or failed: {wait_error}")
                # Continue anyway - the message might have been sent
            
            return True  # Prompt was sent successfully
        
        except Exception as e:
            from core.logging_utils import log_error
            log_error(f"[selenium_chatgpt] Failed to send prompt: {e}")
            raise

    def _get_response_selectors(self) -> list:
        """Get CSS selectors for extracting ChatGPT responses.
        
        These selectors are tried in order until a response is found.
        Returns the LAST matching element (most recent response).
        """
        return [
            "div.markdown.prose",  # Primary selector (from legacy version that works)
            "[data-message-author-role='assistant']",  # Alternative for assistant messages
            "div.markdown",  # Fallback
            "[role='presentation'] .markdown",  # Generic markdown content
            ".prose",  # Fallback prose content
            "[data-testid*='conversation-turn'] .markdown",  # New ChatGPT message structure
            ".message-content .markdown",
        ]

    def _extract_response_text(self, driver) -> str:
        """Extract response from ChatGPT with prefer-response dialog dismissal.
        
        Extends the base method to handle ChatGPT-specific dialogs.
        """
        # Dismiss the "prefer-response" dialog if it appears
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, "[data-testid='paragen-prefer-response-button']")
            if buttons:
                try:
                    buttons[0].click()
                    time.sleep(1)
                    log_debug("[selenium_chatgpt] Dismissed prefer-response dialog")
                except Exception as e:
                    log_debug(f"[selenium_chatgpt] Could not click prefer-response button: {e}")
        except Exception:
            pass
        
        # Use the base class method which tries all selectors
        return super()._extract_response_text(driver)
    
    def _get_response_choice_selectors(self) -> list:
        """Get CSS selectors for ChatGPT response choice buttons.
        
        ChatGPT sometimes offers users a choice between multiple responses.
        This returns selectors to find the choice buttons so we can auto-select the first one.
        """
        return [
            # The complex selector provided by user for first choice button
            "div.basis-auto > div > div.flex.flex-col > article > div > div > div.overflow-x-auto > div > div:first-child > div > button",
            # Simplified selectors for response choice buttons
            "article button[data-testid*='response']",
            "div.snap-x button",
            "article .flex > button:first-child",
            # Fallback: any button near the response area
            "article button",
        ]
    
    def get_supported_models(self) -> list:
        """Get list of supported ChatGPT models."""
        return list(CHATGPT_MODEL_LIMITS.keys())

    def get_current_model(self) -> str:
        """Get the current ChatGPT model being used.
        
        Automatically returns 'unlogged' model if user is not logged in,
        otherwise returns configured model.
        """
        from core.config_manager import config_registry
        from core.logging_utils import log_debug
        
        # Check if user is logged in using centralized method
        if not self.is_user_logged_in():
            log_debug("[selenium_chatgpt] User not logged in, returning 'unlogged' model (21500 chars limit)")
            return "unlogged"
        
        # User is logged in, return configured model
        CHATGPT_MODEL = config_registry.get_value("CHATGPT_MODEL", "")
        configured_model = CHATGPT_MODEL or self.config.get("default_model", "gpt-4o")
        
        log_debug(f"[selenium_chatgpt] get_current_model returning: '{configured_model}' (CHATGPT_MODEL config: '{CHATGPT_MODEL}')")
        return configured_model

    def get_interface_limits(self) -> dict:
        """Get the limits and capabilities for Selenium ChatGPT interface."""
        return get_interface_limits()

PLUGIN_CLASS = SeleniumChatGPTPlugin