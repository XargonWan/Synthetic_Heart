# Import the base Selenium LLM library
from core.selenium_llm_base import SeleniumLLMBase

# Selenium Grok-specific configuration
# Model-specific character limits (based on official documentation and testing)
GROK_MODEL_LIMITS = {
    "grok-beta": 128000,        # Grok: 128k tokens context (~400k characters)
    "grok-vision-beta": 128000,  # Grok Vision: 128k tokens context (~400k characters)
    "unlogged": 1000,           # Unlogged state: very limited context
    "default": 128000        # Safe default for unknown models
}

SELENIUM_CONFIG = {
    "max_prompt_chars": 128000,  # Default to grok-beta limit
    "max_response_chars": 4000,
    "supports_images": True,
    "supports_functions": False,  # Browser-based doesn't support functions
    "model_name": "grok-beta",
    "default_model": "grok-beta",
    "browser_timeout": 30,
    "page_load_timeout": 60,
    "element_wait_timeout": 10,
    "retry_attempts": 3,
    "retry_delay": 2
}

def get_model_char_limit(model_name: str) -> int:
    """Get the character limit for a specific Grok model."""
    # Normalize model name (lowercase, strip)
    normalized = model_name.lower().strip()
    
    # Check direct match first
    if normalized in GROK_MODEL_LIMITS:
        return GROK_MODEL_LIMITS[normalized]
    
    # Try to match partial names (e.g., "grok-beta" -> "grok-beta")
    for key in GROK_MODEL_LIMITS.keys():
        if key in normalized or normalized.endswith(key):
            return GROK_MODEL_LIMITS[key]
    
    # Special case: check for model variants
    if "vision" in normalized:
        return GROK_MODEL_LIMITS["grok-vision-beta"]
    elif "beta" in normalized:
        return GROK_MODEL_LIMITS["grok-beta"]
    
    # Return default if no match found
    from core.logging_utils import log_warning
    log_warning(f"[selenium_grok] Unknown model '{model_name}', using default limit of {GROK_MODEL_LIMITS['default']} chars")
    return GROK_MODEL_LIMITS["default"]

def get_interface_limits() -> dict:
    """Get the limits and capabilities for Selenium Grok interface."""
    # Get current model and its specific limit
    from core.config_manager import config_registry
    GROK_MODEL = config_registry.get_value("GROK_MODEL", "")
    model_name = GROK_MODEL or SELENIUM_CONFIG.get("default_model", "grok-beta")
    max_chars = get_model_char_limit(model_name)
    
    from core.logging_utils import log_info
    log_info(f"[selenium_grok] Interface limits for model '{model_name}': max_prompt_chars={max_chars}, supports_images={SELENIUM_CONFIG['supports_images']}")
    return {
        "max_prompt_chars": max_chars,
        "max_response_chars": SELENIUM_CONFIG["max_response_chars"],
        "supports_images": SELENIUM_CONFIG["supports_images"],
        "supports_functions": SELENIUM_CONFIG["supports_functions"],
        "model_name": model_name
    }

class SeleniumGrokPlugin(SeleniumLLMBase):
    display_name = "Selenium Grok"
    
    def __init__(self, notify_fn=None):
        """Initialize the Grok plugin."""
        # Get current model from config
        from core.config_manager import config_registry
        GROK_MODEL = config_registry.get_value("GROK_MODEL", "")
        
        # Grok-specific configuration
        grok_config = SELENIUM_CONFIG.copy()
        grok_config.update({
            "service_url": "https://grok.x.ai",
            "model": GROK_MODEL or "grok-beta",
            "interface_name": "grok"
        })
        
        super().__init__(config=grok_config, notify_fn=notify_fn)

    def _locate_prompt_area(self):
        """Locate the Grok prompt input area."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        # Try multiple selectors for Grok's input area (current interface)
        selectors = [
            # Primary Grok selectors
            (By.CSS_SELECTOR, "textarea[placeholder*='Message Grok']"),
            (By.CSS_SELECTOR, "textarea[placeholder*='Ask Grok']"),
            (By.CSS_SELECTOR, "textarea[placeholder*='What would you like to know?']"),
            (By.CSS_SELECTOR, "textarea[data-testid='prompt-textarea']"),
            # Contenteditable divs used by Grok
            (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder*='Message']"),
            (By.CSS_SELECTOR, "div[contenteditable='true'][aria-label*='Message']"),
            (By.CSS_SELECTOR, "div[role='textbox'][contenteditable='true']"),
            # Fallback selectors
            (By.CSS_SELECTOR, "div.ql-editor.ql-blank"),
            (By.CSS_SELECTOR, "div.ql-editor"),
            (By.TAG_NAME, "textarea"),
        ]
        
        for by, selector in selectors:
            try:
                element = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((by, selector))
                )
                return element
            except:
                continue
        
        raise Exception("Could not locate Grok prompt input area")

    def _send_prompt_with_confirmation(self, prompt_text: str, image_path: str = None) -> bool:
        """Send the prompt to Grok and confirm it was sent successfully."""
        try:
            # Locate the prompt input area
            prompt_area = self._locate_prompt_area()
            if not prompt_area:
                from core.logging_utils import log_error
                log_error("[selenium] Could not locate prompt input area")
                return False
            
            # Clear any existing text
            prompt_area.clear()
            
            # Paste the prompt text
            prompt_area.send_keys(prompt_text)
            
            # If there's an image, handle it (Grok supports image uploads)
            if image_path and os.path.exists(image_path):
                # Find and click the image upload button
                try:
                    from selenium.webdriver.common.by import By
                    from selenium.webdriver.support.ui import WebDriverWait
                    from selenium.webdriver.support import expected_conditions as EC
                    
                    upload_button = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-testid='file-upload-button'], .upload-button, [aria-label*='upload'], [aria-label*='image']"))
                    )
                    upload_button.click()
                    
                    # Wait for file input and upload
                    file_input = WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
                    )
                    file_input.send_keys(image_path)
                    
                    # Wait for upload to complete
                    WebDriverWait(self.driver, 10).until(
                        lambda d: len(d.find_elements(By.CSS_SELECTOR, ".uploaded-image, [data-testid*='uploaded']")) > 0
                    )
                    from core.logging_utils import log_debug
                    log_debug("[selenium] Image uploaded successfully")
                except Exception as e:
                    from core.logging_utils import log_warning
                    log_warning(f"[selenium] Failed to upload image: {e}")
            
            # Send the message
            from selenium.webdriver.common.keys import Keys
            prompt_area.send_keys(Keys.RETURN)
            
            # Wait for confirmation that the prompt was sent
            # Check that the input area is cleared or that a sending indicator appears
            WebDriverWait(self.driver, 10).until(
                lambda d: (
                    prompt_area.get_attribute("textContent") == "" or
                    len(d.find_elements(By.CSS_SELECTOR, "[data-testid*='sending'], .sending, .loading")) > 0
                )
            )
            
            from core.logging_utils import log_debug
            log_debug("[selenium] Prompt sent successfully")
            return True
            
        except Exception as e:
            from core.logging_utils import log_error
            log_error(f"[selenium] Failed to send prompt: {e}")
            return False

    def _extract_response_text(self) -> str:
        """Extract the latest response from Grok."""
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            
            # Wait for response to appear
            response_element = WebDriverWait(self.driver, self.config.get("response_timeout", 60)).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".response-content, [data-testid*='response'], .message-content"))
            )
            
            # Get the text content
            response_text = response_element.text
            
            # Wait for response to stabilize (no more typing indicators)
            self.wait_until_response_stabilizes()
            
            # Get final text after stabilization
            final_response = response_element.text
            
            from core.logging_utils import log_debug
            log_debug(f"[selenium] Extracted response: {len(final_response)} characters")
            return final_response
            
        except Exception as e:
            from core.logging_utils import log_error
            log_error(f"[selenium] Failed to extract response: {e}")
            return ""

    def get_supported_models(self) -> list:
        """Get list of supported Grok models."""
        return list(GROK_MODEL_LIMITS.keys())

    def get_current_model(self) -> str:
        """Get the current Grok model being used."""
        from core.config_manager import config_registry
        GROK_MODEL = config_registry.get_value("GROK_MODEL", "")
        configured_model = GROK_MODEL or self.config.get("default_model", "grok-beta")
        
        # Check if user is logged in, if not return "unlogged" model
        if not self._is_user_logged_in():
            from core.logging_utils import log_warning
            log_warning(f"[selenium_grok] ⚠️ User not logged in to Grok, using 'unlogged' model with limited context (1000 chars)")
            return "unlogged"
        
        return configured_model

    def _is_user_logged_in(self) -> bool:
        """Check if user is logged in to Grok without initializing driver if not needed."""
        # If driver is not initialized, assume not logged in
        if self.driver is None:
            return False
            
        try:
            current_url = self.driver.current_url
            # If we're on a Twitter/X login page, user is not logged in
            if current_url and ("twitter.com" in current_url and ("login" in current_url or "signin" in current_url)):
                return False
            return True
        except Exception:
            # If we can't get the URL, assume not logged in
            return False

PLUGIN_CLASS = SeleniumGrokPlugin