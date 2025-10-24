import undetected_chromedriver as uc
from selenium import webdriver
import os
import re
import time
import json
import glob
import shutil
import tempfile
import threading
import asyncio
import logging
import requests
import base64
import traceback
from collections import defaultdict
from typing import Optional, Dict
from pathlib import Path
import subprocess
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - fallback if python-dotenv not installed
    def load_dotenv(*args, **kwargs):
        return False
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementNotInteractableException,
    SessionNotCreatedException,
    WebDriverException,
    StaleElementReferenceException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib3.exceptions import ReadTimeoutError
from core.transport_layer import llm_to_interface


# Local functions and classes
from core.logging_utils import log_debug, log_error, log_warning, log_info, _LOG_DIR
from core.notifier import set_notifier
from core.config_manager import config_registry
import core.recent_chats as recent_chats
from core.ai_plugin_base import AIPluginBase

# === Register CHROMIUM_HEADLESS in config_registry (lazy init) ===
CHROMIUM_HEADLESS = 0
_chromium_headless_registered = False

def _ensure_chromium_headless_registered():
    global CHROMIUM_HEADLESS, _chromium_headless_registered
    if _chromium_headless_registered:
        return
    _chromium_headless_registered = True
    
    def _update_chromium_headless(new_value):
        global CHROMIUM_HEADLESS
        CHROMIUM_HEADLESS = 1 if new_value else 0
        log_debug(f"[selenium_chatgpt_legacy] CHROMIUM_HEADLESS updated to {CHROMIUM_HEADLESS}")

    CHROMIUM_HEADLESS = config_registry.get_value(
        "CHROMIUM_HEADLESS",
        0,
        label="Chromium Headless Mode",
        description="Enable headless mode for Chromium browser (no GUI). WARNING: Selenium-based LLM engines require non-headless mode for initial login to services. Set to 0 (off) when logging in, can enable afterwards.",
        group="llm",
        component="selenium",
        value_type=bool,
        advanced=True,
    )
    config_registry.add_listener("CHROMIUM_HEADLESS", _update_chromium_headless)

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
    """Get the character limit for a specific ChatGPT model.
    
    Args:
        model_name: The model name (e.g., "gpt-4o", "gpt-4-turbo")
        
    Returns:
        Maximum characters allowed for the model, or default if unknown
    """
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
    log_warning(f"[selenium_chatgpt_legacy] Unknown model '{model_name}', using default limit of {CHATGPT_MODEL_LIMITS['default']} chars")
    return CHATGPT_MODEL_LIMITS["default"]

def get_selenium_config() -> dict:
    """Get Selenium ChatGPT-specific configuration."""
    return SELENIUM_CONFIG.copy()

def get_max_prompt_chars() -> int:
    """Get maximum prompt characters for the current ChatGPT model.
    
    Checks CHATGPT_MODEL configuration variable or uses default model,
    then returns the model-specific character limit.
    """
    # Get current model from config or use default
    model_name = CHATGPT_MODEL or SELENIUM_CONFIG.get("default_model", "gpt-4o")
    
    # Return model-specific limit
    return get_model_char_limit(model_name)

def get_max_response_chars() -> int:
    """Get maximum response characters for Selenium ChatGPT."""
    return SELENIUM_CONFIG["max_response_chars"]

def supports_images() -> bool:
    """Check if Selenium ChatGPT supports images."""
    return SELENIUM_CONFIG["supports_images"]

def supports_functions() -> bool:
    """Check if Selenium ChatGPT supports functions."""
    return SELENIUM_CONFIG["supports_functions"]

def get_interface_limits() -> dict:
    """Get the limits and capabilities for Selenium ChatGPT interface.
    
    Returns model-specific character limits based on the current model.
    """
    # Get current model and its specific limit
    model_name = CHATGPT_MODEL or SELENIUM_CONFIG.get("default_model", "gpt-4o")
    max_chars = get_model_char_limit(model_name)
    
    log_info(f"[selenium_chatgpt_legacy] Interface limits for model '{model_name}': max_prompt_chars={max_chars}, supports_images={SELENIUM_CONFIG['supports_images']}")
    return {
        "max_prompt_chars": max_chars,
        "max_response_chars": SELENIUM_CONFIG["max_response_chars"],
        "supports_images": SELENIUM_CONFIG["supports_images"],
        "supports_functions": SELENIUM_CONFIG["supports_functions"],
        "model_name": model_name
    }

# Load environment variables for root password and other settings
load_dotenv()

# ChatLinkStore: manages mapping between interface chats and ChatGPT conversations
from plugins.chat_link import ChatLinkStore
from interface.telegram_utils import safe_send
from core.db import get_conn

# Fallback for notify_trainer when core.notifier module is unavailable
def notify_trainer(message: str) -> None:
    """Best-effort trainer notification used during tests.

    The real ``notify_trainer`` utility accepts a single message argument, so
    the fallback must mirror that signature to avoid ``TypeError`` when the
    caller provides just the message text.
    """
    log_warning(f"[notify_trainer fallback] {message}")

# ---------------------------------------------------------------------------
# Constants

GRACE_PERIOD_SECONDS = 3
MAX_WAIT_TIMEOUT_SECONDS = 5 * 60  # hard ceiling

# Cache the last response per chat to avoid duplicates
previous_responses: Dict[str, str] = {}
response_cache_lock = threading.Lock()

# Extended ChatLinkStore for ChatGPT-specific functionality
class ChatGPTLinkStore(ChatLinkStore):
    """Extends ChatLinkStore to handle ChatGPT-specific link management."""
    
    def __init__(self):
        super().__init__()
        self.chatgpt_link_initialized = False
    
    async def ensure_chatgpt_link_column(self):
        """Ensure the chatgpt_link column exists in the chatlink table."""
        if self.chatgpt_link_initialized:
            return
            
        try:
            connection = await get_conn()
            async with connection.cursor() as cursor:
                # Check if chatgpt_link column exists
                await cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.COLUMNS 
                    WHERE TABLE_SCHEMA = DATABASE() 
                    AND TABLE_NAME = 'chatlink' 
                    AND COLUMN_NAME = 'chatgpt_link'
                """)
                result = await cursor.fetchone()
                
                if result[0] == 0:
                    # Add chatgpt_link column
                    await cursor.execute("""
                        ALTER TABLE chatlink 
                        ADD COLUMN chatgpt_link VARCHAR(255) NULL
                    """)
                    await connection.commit()
                    log_info("[selenium_chatgpt_legacy] Added chatgpt_link column to chatlink table")
                
            await connection.ensure_closed()
            self.chatgpt_link_initialized = True
        except Exception as e:
            log_error(f"[selenium_chatgpt_legacy] Failed to ensure chatgpt_link column: {e}")
    
    async def get_chatgpt_link(self, chat_id, thread_id=None, interface="unknown"):
        """Get ChatGPT link for a chat, creating chat record if needed."""
        await self.ensure_chatgpt_link_column()
        
        # Ensure chat exists first
        await self.ensure_chat_exists(chat_id, thread_id, interface)
        
        try:
            connection = await get_conn()
            async with connection.cursor() as cursor:
                if thread_id:
                    await cursor.execute("""
                        SELECT chatgpt_link 
                        FROM chatlink 
                        WHERE chat_id = %s AND thread_id = %s AND interface = %s
                    """, (str(chat_id), str(thread_id), interface))
                else:
                    await cursor.execute("""
                        SELECT chatgpt_link 
                        FROM chatlink 
                        WHERE chat_id = %s AND thread_id IS NULL AND interface = %s
                    """, (str(chat_id), interface))
                
                result = await cursor.fetchone()
                await connection.ensure_closed()
                
                chatgpt_link = result[0] if result and result[0] else None
                log_debug(f"[selenium_chatgpt_legacy] get_chatgpt_link: chat_id={chat_id}, thread_id={thread_id}, interface={interface} -> link={chatgpt_link}")
                return chatgpt_link
        except Exception as e:
            log_error(f"[selenium_chatgpt_legacy] Failed to get ChatGPT link: {e}")
            return None
    
    async def store_chatgpt_link(self, chat_id, chatgpt_link, thread_id=None, interface="unknown", chat_name=None):
        """Store ChatGPT link for a chat."""
        await self.ensure_chatgpt_link_column()
        
        # Ensure chat exists first
        await self.ensure_chat_exists(chat_id, thread_id, interface, chat_name=chat_name)
        
        try:
            connection = await get_conn()
            async with connection.cursor() as cursor:
                if thread_id:
                    await cursor.execute("""
                        UPDATE chatlink 
                        SET chatgpt_link = %s 
                        WHERE chat_id = %s AND thread_id = %s AND interface = %s
                    """, (chatgpt_link, str(chat_id), str(thread_id), interface))
                else:
                    await cursor.execute("""
                        UPDATE chatlink 
                        SET chatgpt_link = %s 
                        WHERE chat_id = %s AND thread_id IS NULL AND interface = %s
                    """, (chatgpt_link, str(chat_id), interface))
                
                await connection.commit()
                await connection.ensure_closed()
                log_debug(f"[selenium_chatgpt_legacy] Stored ChatGPT link: {chatgpt_link} for chat {chat_id}")
                return True
        except Exception as e:
            log_error(f"[selenium_chatgpt_legacy] Failed to store ChatGPT link: {e}")
            return False
    
    async def remove_chatgpt_link(self, chat_id, thread_id=None, interface="unknown"):
        """Remove ChatGPT link for a chat (sets to NULL)."""
        await self.ensure_chatgpt_link_column()
        
        try:
            connection = await get_conn()
            async with connection.cursor() as cursor:
                if thread_id:
                    await cursor.execute("""
                        UPDATE chatlink 
                        SET chatgpt_link = NULL 
                        WHERE chat_id = %s AND thread_id = %s AND interface = %s
                    """, (str(chat_id), str(thread_id), interface))
                else:
                    await cursor.execute("""
                        UPDATE chatlink 
                        SET chatgpt_link = NULL 
                        WHERE chat_id = %s AND thread_id IS NULL AND interface = %s
                    """, (str(chat_id), interface))
                
                await connection.commit()
                await connection.ensure_closed()
                log_debug(f"[selenium_chatgpt_legacy] Removed ChatGPT link for chat {chat_id}")
                return True
        except Exception as e:
            log_error(f"[selenium_chatgpt_legacy] Failed to remove ChatGPT link: {e}")
            return False

# Persistent mapping between interface chats and ChatGPT conversations
chat_link_store = ChatGPTLinkStore()
queue_paused = False


def get_previous_response(chat_id: str) -> str:
    """Return the cached response for the given chat."""
    with response_cache_lock:
        return previous_responses.get(chat_id, "")


def update_previous_response(chat_id: str, new_text: str) -> None:
    """Store ``new_text`` for ``chat_id`` inside the cache."""
    with response_cache_lock:
        previous_responses[chat_id] = new_text


def has_response_changed(chat_id: str, new_text: str) -> bool:
    """Return True if ``new_text`` is different from the cached value."""
    with response_cache_lock:
        old = previous_responses.get(chat_id)
    return old != new_text


def strip_non_bmp(text: str) -> str:
    """Return ``text`` with characters above the BMP removed."""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


async def _download_telegram_image(bot, file_id: str, temp_dir: str) -> Optional[str]:
    """Download an image from Telegram and return the local file path."""
    try:
        # Get file info from Telegram
        file_info = await bot.get_file(file_id)
        
        # CORREZIONE: Costruisci l'URL corretto senza duplicare "bot"
        # Il token è già nel formato "botTOKEN", quindi dobbiamo solo usare bot.token
        file_url = f"https://api.telegram.org/file/{bot.token}/{file_info.file_path}"
        
        log_debug(f"[selenium] Downloading from URL: {file_url}")
        
        # Download the file
        response = requests.get(file_url, timeout=30)
        response.raise_for_status()
        
        # Save to temp file
        file_extension = Path(file_info.file_path).suffix or '.jpg'
        temp_file = os.path.join(temp_dir, f"image_{int(time.time())}{file_extension}")
        
        with open(temp_file, 'wb') as f:
            f.write(response.content)
        
        log_debug(f"[selenium] Downloaded Telegram image to: {temp_file}")
        return temp_file
        
    except Exception as e:
        log_error(f"[selenium] Failed to download Telegram image: {e}")
        return None


def _locate_prompt_area(driver, timeout: int = 10):
    """Try multiple strategies to locate the ChatGPT prompt area.

    This function first attempts to find the element with ID 'prompt-textarea'
    (the standard ChatGPT textarea). If that fails it will try a fallback
    for contenteditable ProseMirror-style divs that some UIs use. Returns a
    WebElement or raises the original exception.
    """
    try:
        if timeout and timeout > 0:
            return WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.ID, "prompt-textarea"))
            )
        # no wait requested: try direct find
        return driver.find_element(By.ID, "prompt-textarea")
    except Exception as primary_exc:
        # fallback: look for a contenteditable ProseMirror div with the same id
        try:
            xpath = "//div[@contenteditable='true' and @id='prompt-textarea']"
            if timeout and timeout > 0:
                return WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
            return driver.find_element(By.XPATH, xpath)
        except Exception:
            # re-raise the original exception to preserve caller behavior
            raise primary_exc


def _paste_image_to_chatgpt(driver, image_path: str) -> bool:
    """Paste an image to ChatGPT input using JavaScript injection (Docker-compatible)."""
    try:
        # Find the input area (supports textarea or contenteditable fallback)
        textarea = _locate_prompt_area(driver, timeout=10)

        # Click on the textarea to focus it
        textarea.click()
        time.sleep(0.5)

        # Method 1: Try to find and use the image upload button
        try:
            # Look for image upload button (common selectors for ChatGPT)
            upload_selectors = [
                "button[data-testid*='image']",
                "button[aria-label*='image']",
                "button[aria-label*='upload']",
                "input[type='file'][accept*='image']",
                ".image-upload-button",
                "[data-testid='file-upload-button']"
            ]

            upload_element = None
            for selector in upload_selectors:
                try:
                    if selector.startswith("input"):
                        upload_element = WebDriverWait(driver, 2).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                        )
                    else:
                        upload_element = WebDriverWait(driver, 2).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                        )
                    break
                except TimeoutException:
                    continue

            if upload_element:
                log_debug(f"[selenium] Found image upload button: {upload_element.tag_name}")
                # For file input, we can set the file directly
                if upload_element.tag_name.lower() == "input":
                    driver.execute_script("arguments[0].style.display = 'block';", upload_element)
                    upload_element.send_keys(image_path)
                    log_info("[selenium] Image uploaded via file input")
                    return True
                else:
                    # Click the upload button
                    upload_element.click()
                    time.sleep(1)
                    # This might open a file dialog, but in headless mode we need a different approach
                    log_debug("[selenium] Clicked image upload button")

        except Exception as e:
            log_debug(f"[selenium] Image upload button method failed: {e}")

        # Method 2: Convert image to base64 and inject via JavaScript
        try:
            import base64

            # Read and encode the image
            with open(image_path, 'rb') as f:
                image_data = f.read()

            # Get image format from file extension
            import mimetypes
            mime_type, _ = mimetypes.guess_type(image_path)
            if not mime_type:
                mime_type = 'image/jpeg'  # fallback

            # Create data URL
            encoded_image = base64.b64encode(image_data).decode('utf-8')
            data_url = f"data:{mime_type};base64,{encoded_image}"

            # JavaScript to create and upload the image
            js_script = f"""
            // Create a temporary file input
            var input = document.createElement('input');
            input.type = 'file';
            input.accept = 'image/*';
            input.style.display = 'none';

            // Create a blob from the data URL
            fetch('{data_url}')
                .then(res => res.blob())
                .then(blob => {{
                    var file = new File([blob], 'uploaded_image.jpg', {{type: '{mime_type}'}});
                    var dt = new DataTransfer();
                    dt.items.add(file);
                    input.files = dt.files;

                    // Find the actual file input in ChatGPT's interface
                    var chatgptInputs = document.querySelectorAll('input[type="file"]');
                    if (chatgptInputs.length > 0) {{
                        chatgptInputs[0].files = dt.files;
                        chatgptInputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}

                    // Alternative: try to paste into textarea as data URL
                    var textarea = document.getElementById('prompt-textarea');
                    if (textarea) {{
                        textarea.focus();
                        // Insert image marker (ChatGPT might handle this)
                        var imageMarker = '[Image uploaded: ' + file.name + ']';
                        textarea.value += imageMarker;
                        textarea.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return true;
                    }}

                    return false;
                }})
                .catch(err => console.error('Image upload failed:', err));
            """

            result = driver.execute_script(js_script)
            if result:
                log_info("[selenium] Image injected via JavaScript")
                time.sleep(2)  # Wait for processing
                return True

        except Exception as e:
            log_warning(f"[selenium] JavaScript injection method failed: {e}")

        # Method 3: Fallback to clipboard method (original implementation)
        import platform
        system = platform.system().lower()

        if system == "linux":
            try:
                subprocess.run([
                    "xclip", "-selection", "clipboard", "-t", "image/png", "-i", image_path
                ], check=True, capture_output=True)
                log_debug(f"[selenium] Copied image to clipboard using xclip: {image_path}")
            except (subprocess.CalledProcessError, FileNotFoundError):
                log_warning("[selenium] xclip not available, trying alternative method")
                return False

        elif system == "darwin":  # macOS
            try:
                subprocess.run([
                    "osascript", "-e", f'set the clipboard to (read file POSIX file "{image_path}" as JPEG picture)'
                ], check=True, capture_output=True)
                log_debug(f"[selenium] Copied image to clipboard using osascript: {image_path}")
            except subprocess.CalledProcessError:
                log_warning("[selenium] osascript failed, trying alternative method")
                return False

        elif system == "windows":
            try:
                ps_script = f"""
                Add-Type -AssemblyName System.Windows.Forms
                $img = [System.Drawing.Image]::FromFile('{image_path}')
                [System.Windows.Forms.Clipboard]::SetImage($img)
                """
                subprocess.run([
                    "powershell", "-Command", ps_script
                ], check=True, capture_output=True)
                log_debug(f"[selenium] Copied image to clipboard using PowerShell: {image_path}")
            except subprocess.CalledProcessError:
                log_warning("[selenium] PowerShell failed, trying alternative method")
                return False

        # Paste the image using Ctrl+V
        textarea.send_keys(Keys.CONTROL, 'v')
        time.sleep(2)  # Wait for the image to be processed

        # Check if the image was pasted successfully
        try:
            # Look for image indicators in the UI
            WebDriverWait(driver, 5).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "[data-testid*='image']") or
                         d.find_elements(By.CSS_SELECTOR, "img") or
                         d.find_elements(By.CSS_SELECTOR, "[title*='image']") or
                         d.find_elements(By.CSS_SELECTOR, ".image-preview") or
                         d.find_elements(By.CSS_SELECTOR, "[data-testid*='attachment']")
            )
            log_info("[selenium] Image successfully pasted to ChatGPT")
            return True
        except TimeoutException:
            log_warning("[selenium] Could not verify if image was pasted successfully")
            # Still return True as the paste operation was attempted
            return True

    except Exception as e:
        log_error(f"[selenium] Failed to paste image to ChatGPT: {e}")
        return False


def _extract_image_info_from_prompt(prompt_text: str) -> Optional[Dict]:
    """Extract image information from JSON prompt if present."""
    try:
        # Handle both string and dict inputs
        if isinstance(prompt_text, str):
            # Parse the JSON prompt
            prompt_data = json.loads(prompt_text)
        elif isinstance(prompt_text, dict):
            # Already parsed
            prompt_data = prompt_text
        else:
            log_debug(f"[selenium] Unexpected prompt type: {type(prompt_text)}")
            return None
        
        # Look for image data in the input payload
        input_payload = prompt_data.get("input", {}).get("payload", {})
        image_data = input_payload.get("image")
        
        if image_data:
            log_debug(f"[selenium] Found image data in prompt: {image_data.get('type', 'unknown')}")
            return image_data
            
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        log_debug(f"[selenium] No image data found in prompt: {e}")
        
    return None


def _send_text_to_textarea(driver, textarea, text: str) -> None:
    """Inject ``text`` into the ChatGPT prompt area via JavaScript."""
    try:
        clean_text = strip_non_bmp(text)
        log_debug(f"[DEBUG] Length before sending: {len(clean_text)}")
        # Log full text content for debugging (truncated for safety)
        preview = clean_text[:500] + "..." if len(clean_text) > 500 else clean_text
        log_debug(f"[DEBUG] Text to send ({len(clean_text)} chars): {preview}")

        tag = (textarea.tag_name or "").lower()
        prop = "value" if tag in {"textarea", "input"} else "textContent"
        script = (
            "arguments[0].focus();"
            f"arguments[0].{prop} = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
        )
        
        try:
            driver.execute_script(script, textarea, clean_text)
            log_debug("[DEBUG] JavaScript injection completed successfully")
        except Exception as js_error:
            log_error(f"[selenium] JavaScript injection failed: {js_error}")
            raise

        try:
            actual = driver.execute_script(f"return arguments[0].{prop};", textarea) or ""
            log_debug(f"[DEBUG] Length actually present in textarea: {len(actual)}")
            # Only warn if the textarea differs noticeably from the injected text
            if abs(len(clean_text) - len(actual)) > 5:
                log_warning(
                    f"[selenium] textarea mismatch: expected {len(clean_text)} chars, found {len(actual)}"
                )
        except Exception as check_error:
            log_warning(f"[selenium] Failed to verify textarea content: {check_error}")
            # Don't fail the operation, just continue
            
    except Exception as e:
        log_error(f"[selenium] Critical error in _send_text_to_textarea: {e}")
        import traceback
        log_error(f"[selenium] Full traceback: {traceback.format_exc()}")
        raise


def paste_and_send(textarea, prompt_text: str) -> None:
    """Insert ``prompt_text`` into ``textarea`` ensuring full content is present.

    Tries JavaScript injection first (for performance and reliability), then
    verifies the length.  If the content does not match, falls back to a
    chunked ``send_keys`` approach which mimics manual typing.
    """
    try:
        driver = textarea._parent
        clean = strip_non_bmp(prompt_text)

        # Try JavaScript injection first
        try:
            _send_text_to_textarea(driver, textarea, clean)
            tag = (textarea.tag_name or "").lower()
            prop = "value" if tag in {"textarea", "input"} else "textContent"
            actual = driver.execute_script(f"return arguments[0].{prop};", textarea) or ""
            if len(actual) >= len(clean) * 0.9:  # Allow some tolerance
                log_debug(f"[selenium] JS injection successful: {len(actual)}/{len(clean)} chars")
                return
        except StaleElementReferenceException:
            log_warning("[selenium] Textarea became stale during JS paste, retrying with send_keys")
        except Exception as e:
            log_warning(f"[selenium] JS injection failed: {e}, falling back to send_keys")
    except Exception as critical_error:
        log_error(f"[selenium] Critical error in paste_and_send initialization: {critical_error}")
        import traceback
        log_error(f"[selenium] paste_and_send init traceback: {traceback.format_exc()}")
        raise

    log_warning(f"[selenium] JS paste failed, falling back to send_keys")

    # Fallback to send_keys with improved logic
    import textwrap
    chunk_size = 1000
    final_val = ""
    for attempt in range(3):
        if attempt:
            log_warning(f"[selenium] send_keys retry {attempt}/3")
        try:
            # Re-locate textarea if it became stale
            try:
                textarea.clear()
                time.sleep(0.1)  # Brief pause after clear
            except StaleElementReferenceException:
                log_warning("[selenium] Textarea stale, attempting to re-locate")
                textarea = _locate_prompt_area(driver, timeout=0)
                textarea.clear()
                time.sleep(0.1)
            
            # Send chunks with better validation
            accumulated_text = ""
            chunks_sent = 0
            total_chunks = len(list(textwrap.wrap(clean, chunk_size)))
            content_was_sent = False
            
            for idx, chunk in enumerate(textwrap.wrap(clean, chunk_size), start=1):
                log_debug(f"[selenium] sending chunk {idx}/{total_chunks} len={len(chunk)}")
                textarea.send_keys(chunk)
                accumulated_text += chunk
                chunks_sent = idx
                time.sleep(0.05)
                
                # Validate every few chunks to catch issues early
                if idx % 5 == 0:
                    current_val = textarea.get_attribute("value") or ""
                    if len(current_val) < len(accumulated_text) * 0.5:  # More lenient threshold
                        log_warning(f"[selenium] Content mismatch detected at chunk {idx}, retrying")
                        break
            
            # If we sent all chunks, consider it potentially successful
            if chunks_sent == total_chunks:
                content_was_sent = True
                log_debug(f"[selenium] All {chunks_sent} chunks sent successfully")
            
            final_val = textarea.get_attribute("value") or ""
            log_debug(f"[selenium] value after send_keys: {len(final_val)} chars")
            
            # Check success conditions with better logic
            if len(final_val) >= len(clean) * 0.9:
                log_debug(f"[selenium] Content successfully inserted ({len(final_val)}/{len(clean)} chars)")
                return
            elif len(final_val) == 0 and content_was_sent:
                log_debug("[selenium] Textarea is empty but all chunks were sent - likely cleared by ChatGPT JS")
                # This is actually success - ChatGPT cleared the textarea after accepting input
                return
            elif len(final_val) == 0:
                log_warning("[selenium] Textarea is empty after sending, possible JS interference")
                # Try alternative approach for next attempt
                chunk_size = max(100, chunk_size // 3)
            elif len(final_val) >= len(clean) * 0.5:
                log_debug(f"[selenium] Accepting partial content as sufficient ({len(final_val)}/{len(clean)} chars)")
                return
            else:
                log_warning(f"[selenium] Partial content inserted ({len(final_val)}/{len(clean)} chars)")
                
        except StaleElementReferenceException as e:
            log_warning(f"[selenium] Stale element on send_keys attempt {attempt}: {e}")
            try:
                textarea = _locate_prompt_area(driver, timeout=0)
            except NoSuchElementException:
                log_error("[selenium] Could not re-locate textarea element")
                break
        except Exception as e:
            log_warning(f"[selenium] send_keys attempt {attempt} failed: {e}")
        
        chunk_size = max(200, chunk_size // 2)
    
    # If we still failed, try one more approach with smaller chunks
    if len(final_val) < len(clean) * 0.5:
        log_warning("[selenium] Attempting emergency fallback with very small chunks")
        try:
            textarea.clear()
            for char in clean[:500]:  # Limit to first 500 chars as emergency
                textarea.send_keys(char)
                time.sleep(0.01)
            final_val = textarea.get_attribute("value") or ""
            log_warning(f"[selenium] Emergency fallback result: {len(final_val)} chars")
        except Exception as e:
            log_error(f"[selenium] Emergency fallback failed: {e}")
    
    log_warning(
        f"[selenium] Failed to insert full prompt: expected {len(clean)} chars, got {len(final_val)}"
    )


# ---------------------------------------------------------------------------
# Queue utilities for sequential prompt processing

_prompt_queue: asyncio.Queue = asyncio.Queue()
_queue_lock = asyncio.Lock()
_queue_worker: asyncio.Task | None = None


def wait_for_markdown_block_to_appear(driver, prev_count: int, timeout: int = 10) -> bool:
    """Return ``True`` once a new markdown block appears."""
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, "div.markdown"))
            if count > prev_count:
                log_debug(f"[selenium] Markdown count {prev_count} -> {count}")
                return True
        except Exception as e:  # pragma: no cover - best effort
            log_warning(f"[selenium] Markdown wait error: {e}")
        time.sleep(0.5)
    log_warning("[selenium] Timeout waiting for response start")
    return False


# Register timeout and retry configurations
AWAIT_RESPONSE_TIMEOUT = config_registry.get_value(
    "AWAIT_RESPONSE_TIMEOUT",
    240,
    value_type="int",
    label="Response Timeout",
    description="Seconds to wait for ChatGPT response before timing out",
    group="llm",
    component="selenium_chatgpt_legacy",
)

CORRECTOR_RETRIES = config_registry.get_value(
    "CORRECTOR_RETRIES",
    2,
    value_type="int",
    label="Corrector Retries",
    description="Number of times the corrector retries invalid JSON responses",
    group="llm",
    component="selenium_chatgpt_legacy",
)

def _update_await_timeout(value: int | None) -> None:
    """Update global AWAIT_RESPONSE_TIMEOUT variable."""
    global AWAIT_RESPONSE_TIMEOUT
    AWAIT_RESPONSE_TIMEOUT = int(value) if value is not None else 240

def _update_corrector_retries(value: int | None) -> None:
    """Update global CORRECTOR_RETRIES variable."""
    global CORRECTOR_RETRIES
    CORRECTOR_RETRIES = int(value) if value is not None else 2

config_registry.add_listener("AWAIT_RESPONSE_TIMEOUT", _update_await_timeout)
config_registry.add_listener("CORRECTOR_RETRIES", _update_corrector_retries)


def wait_until_response_stabilizes(
    driver: webdriver.Remote,
    max_total_wait: int = AWAIT_RESPONSE_TIMEOUT,
    no_change_grace: float = 3.5,
) -> str:
    """Return the last markdown text once its length stops growing."""
    selector = "div.markdown.prose"
    start = time.time()
    last_len = -1
    last_change = start
    final_text = ""

    while True:
        # Some UI experiments may display a "Which response do you prefer?" dialog
        # that blocks further interaction. If present, automatically click the first
        # "I prefer this response" button so ChatGPT can finalize the output.
        try:
            buttons = driver.find_elements(
                By.CSS_SELECTOR, "[data-testid='paragen-prefer-response-button']"
            )
            if buttons:
                try:
                    buttons[0].click()
                    time.sleep(1)
                    log_debug(
                        "[selenium] Dismissed prefer-response dialog"
                    )
                except StaleElementReferenceException:
                    log_debug("[selenium] Prefer-response button became stale")
                except Exception as e:  # pragma: no cover - best effort
                    log_warning(
                        f"[selenium] Failed to click prefer-response button: {e}"
                    )
        except Exception:
            pass

        if time.time() - start >= max_total_wait:
            log_warning("[WARNING] Timeout while waiting for new response")
            return final_text

        try:
            elems = driver.find_elements(By.CSS_SELECTOR, selector)
            if not elems:
                time.sleep(0.5)
                continue
            text = elems[-1].text or ""
        except StaleElementReferenceException:
            log_debug("[selenium] Response element became stale, retrying...")
            time.sleep(0.5)
            continue
        except Exception as e:  # pragma: no cover - best effort
            log_warning(f"[selenium] Response wait error: {e}")
            time.sleep(0.5)
            continue

        current_len = len(text)
        changed = current_len != last_len
        log_debug(f"[DEBUG] len={current_len} changed={changed}")

        if current_len > 0 and changed:
            last_len = current_len
            last_change = time.time()
            final_text = text
        elif current_len > 0 and time.time() - last_change >= no_change_grace:
            elapsed = time.time() - start
            log_debug(
                f"[DEBUG] Response stabilized with length {current_len} after {elapsed:.1f}s"
            )
            return text

        time.sleep(0.5)


def _wait_for_button_state(driver, state: str, timeout: int) -> bool:
    """Wait until the submit button matches the desired ``state``."""
    locator = (By.ID, "composer-submit-button")
    max_retries = 3
    
    for retry in range(max_retries):
        try:
            def check_button_state(d):
                try:
                    element = d.find_element(*locator)
                    return element.get_attribute("data-testid") == state
                except StaleElementReferenceException:
                    # Element is stale, return False to trigger retry in outer loop
                    log_debug(f"[selenium] Stale element in check_button_state, retry {retry + 1}")
                    return False
                except NoSuchElementException:
                    log_debug(f"[selenium] Button element not found for state '{state}'")
                    return False
                except Exception as e:
                    log_debug(f"[selenium] Unexpected error in check_button_state: {e}")
                    return False
            
            WebDriverWait(driver, timeout).until(check_button_state)
            return True
        except (TimeoutException, StaleElementReferenceException) as e:
            if retry < max_retries - 1:
                log_debug(f"[selenium] Retry {retry + 1}/{max_retries} for button state '{state}': {str(e)}")
                time.sleep(1)  # Brief pause before retry
                continue
            else:
                log_warning(f"[selenium] Failed to wait for button state '{state}' after {max_retries} retries")
                return False
        except Exception as e:
            log_warning(f"[selenium] Unexpected error waiting for button state '{state}': {str(e)}")
            return False
    
    return False


def wait_for_chatgpt_idle(driver, timeout: int = AWAIT_RESPONSE_TIMEOUT) -> bool:
    """Wait until ChatGPT is ready for a new prompt (textarea is available and no stop button)."""
    if not wait_for_response_completion(driver, timeout):
        log_warning("[selenium] ChatGPT may still be generating during idle wait")

    try:
        _locate_prompt_area(driver, timeout=10)
        log_debug("[selenium] Textarea found, ChatGPT is ready for input")
        return True
    except TimeoutException:
        log_warning("[selenium] Timeout waiting for textarea")
        return False


def wait_for_response_completion(driver, timeout: int = AWAIT_RESPONSE_TIMEOUT) -> bool:
    """Wait until the current response finishes streaming."""
    start_time = time.time()
    end_time = start_time + timeout

    try:
        driver.find_element(By.CSS_SELECTOR, "button[data-testid='stop-button']")
        log_debug(
            f"[selenium] Stop button found, waiting for response to complete with timeout {timeout} seconds"
        )
        try:
            driver.command_executor.set_timeout(timeout)
        except Exception as e:
            log_warning(f"[selenium] Could not apply command timeout: {e}")
    except NoSuchElementException:
        log_debug("[selenium] No stop button found, assuming idle")
        return True

    last_report = 0
    while time.time() < end_time:
        try:
            driver.find_element(By.CSS_SELECTOR, "button[data-testid='stop-button']")
            elapsed = int(time.time() - start_time)
            if elapsed // 10 > last_report // 10:
                log_debug(
                    f"[selenium] {elapsed} seconds passed, stop button still present"
                )
                last_report = elapsed
            time.sleep(1)
            continue
        except NoSuchElementException:
            elapsed = int(time.time() - start_time)
            log_debug(
                f"[selenium] Stop button disappeared after {elapsed} seconds, response completed"
            )
            return True
        except (ReadTimeoutError, WebDriverException) as e:
            log_warning(f"[selenium] Polling error while waiting for completion: {e}")
            time.sleep(1)

    log_warning("[selenium] Timeout waiting for response completion")
    return False



def _send_prompt_with_confirmation(textarea, prompt_text: str) -> None:
    """Send text and wait for ChatGPT's reply to finish."""
    driver = textarea._parent
    wait_for_chatgpt_idle(driver)
    for attempt in range(1, 4):
        try:
            log_debug(f"[selenium][STEP] Attempt {attempt} to send prompt")
            # Re-find textarea element in case it became stale
            try:
                textarea = _locate_prompt_area(driver, timeout=5)
            except (TimeoutException, StaleElementReferenceException) as e:
                log_warning(f"[selenium] Could not find textarea on attempt {attempt}: {e}")
                continue
            
            paste_and_send(textarea, prompt_text)
            
            # Try multiple selectors for the send button
            send_button_selectors = [
                (By.CSS_SELECTOR, "button[data-testid='send-button']"),
                (By.CSS_SELECTOR, "button[aria-label='Send prompt']"),
                (By.CSS_SELECTOR, "button.absolute.rounded-full"),
                (By.XPATH, "//button[@data-testid='send-button']"),
                (By.XPATH, "//button[contains(@class, 'bottom') and not(@disabled)]")
            ]
            
            button_clicked = False
            for selector in send_button_selectors:
                try:
                    send_btn = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable(selector)
                    )
                    driver.execute_script("arguments[0].click();", send_btn)
                    log_debug(f"[selenium][STEP] Clicked send button with selector: {selector}")
                    button_clicked = True
                    break
                except (StaleElementReferenceException, TimeoutException, NoSuchElementException):
                    continue
            
            # Fallback to ENTER key if button click failed
            if not button_clicked:
                log_warning("[selenium] All send button selectors failed, using ENTER key")
                try:
                    textarea.send_keys(Keys.ENTER)
                    log_debug("[selenium][STEP] Sent ENTER key as fallback")
                except StaleElementReferenceException:
                    log_warning("[selenium] Textarea became stale, retrying...")
                    continue
            log_debug("[selenium][STEP] Prompt sent, waiting for completion")
            if wait_for_response_completion(driver):
                wait_until_response_stabilizes(driver)
                log_debug("[selenium][STEP] Response stabilized")
                return
            log_warning(f"[selenium] No response after attempt {attempt}")
        except StaleElementReferenceException as e:
            log_warning(f"[selenium] Stale element on attempt {attempt}: {e}")
            continue
        except Exception as e:  # pragma: no cover - best effort
            log_warning(f"[selenium] Send attempt {attempt} failed: {e}")
    log_warning("[selenium] Fallback via ActionChains")
    try:
        ActionChains(driver).click(textarea).send_keys(prompt_text).send_keys(Keys.ENTER).perform()
        log_debug("[selenium][STEP] Fallback ActionChains used to send prompt")
        if wait_for_response_completion(driver):
            wait_until_response_stabilizes(driver)
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[selenium] Fallback send failed: {e}")


async def _queue_worker_loop() -> None:
    """Background worker that processes queued prompts sequentially."""
    global _queue_worker
    while not _prompt_queue.empty():
        textarea, text = await _prompt_queue.get()
        log_debug("[selenium] Dequeued prompt")
        async with _queue_lock:
            log_debug("[selenium] Send lock acquired")
            await asyncio.to_thread(_send_prompt_with_confirmation, textarea, text)
            log_debug("[selenium] Prompt completed")
        _prompt_queue.task_done()
        log_debug("[selenium] Task done")
    _queue_worker = None


async def enqueue_prompt(textarea, prompt_text: str) -> None:
    """Enqueue ``prompt_text`` for sequential sending to ChatGPT."""
    await _prompt_queue.put((textarea, prompt_text))
    log_debug(f"[selenium] Prompt enqueued (size={_prompt_queue.qsize()})")
    global _queue_worker
    # Ensure we always create a fresh coroutine object for the queue worker.
    try:
        if _queue_worker is None or _queue_worker.done():
            _queue_worker = asyncio.create_task(_queue_worker_loop())
    except RuntimeError:
        # If no running loop is available, schedule creation when loop is running.
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_queue_worker_loop()))


def _build_vnc_url() -> str:
    """Return the URL to access the noVNC interface."""
    port = os.getenv("WEBVIEW_PORT", "5005")
    host = os.getenv("WEBVIEW_HOST")
    try:
        host = subprocess.check_output(
            "ip route | awk '/default/ {print $3}'",
            shell=True,
        ).decode().strip()
    except Exception as e:
        log_warning(f"[selenium] Unable to determine host: {e}")
        if not host:
            host = "localhost"
    url = f"http://{host}:{port}/vnc.html"
    log_debug(f"[selenium] VNC URL built: {url}")
    return url

# [FIX] helper to avoid message length limits on certain interfaces
def _safe_notify(text: str) -> None:
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        log_debug(f"[selenium] Notifying chunk length {len(chunk)}")
        try:
            from core.notifier import notify_trainer
            notify_trainer(chunk)
        except Exception as e:  # pragma: no cover - best effort
            log_error(f"[selenium] notify_trainer failed: {repr(e)}", e)

def _notify_gui(message: str = ""):
    """Send a notification with the VNC URL, optionally prefixed."""
    url = _build_vnc_url()
    text = f"{message} {url}".strip()
    log_debug(f"[selenium] Sending VNC notification: {text}")
    _safe_notify(text)


def _extract_chat_id(url: str) -> Optional[str]:
    """Extracts the chat ID from the ChatGPT URL."""
    log_debug(f"[selenium][DEBUG] Extracting chat ID from URL: {url}")

    if not url or not isinstance(url, str):
        log_error("[selenium][ERROR] Invalid URL provided for chat ID extraction.")
        return None

    # More flexible patterns for different ChatGPT URL formats
    patterns = [
        r"/chat/([^/?#]+)",           # Standard format: /chat/uuid
        r"/c/([^/?#]+)",              # Alternative format: /c/uuid  
        r"chat\\.openai\\.com/chat/([^/?#]+)",  # Full URL
        r"chat\\.openai\\.com/c/([^/?#]+)"      # Alternative full URL
    ]

    for pattern in patterns:
        log_debug(f"[selenium][DEBUG] Trying pattern: {pattern}")
        match = re.search(pattern, url)
        if match:
            chat_id = match.group(1)
            log_debug(f"[selenium][DEBUG] Extracted chat ID: {chat_id}")
            return chat_id

    log_error("[selenium][ERROR] No chat ID could be extracted from the URL.")
    return None


def _check_conversation_full(driver) -> bool:
    try:
        elems = driver.find_elements(By.CSS_SELECTOR, "div.text-token-text-error")
        for el in elems:
            text = (el.get_attribute("innerText") or "").strip()
            if "maximum length for this conversation" in text:
                return True
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[selenium] overflow check failed: {e}")
    return False


def _open_new_chat(driver) -> None:
    """Navigate to ChatGPT home to create a new chat with retries."""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            log_debug(f"[selenium] Attempt {attempt}/{max_retries} to navigate to ChatGPT home")
            driver.get("https://chat.openai.com")
            log_debug("[selenium] Successfully navigated to ChatGPT home")
            return
        except Exception as e:
            log_warning(f"[selenium] Attempt {attempt} failed: {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)  # Exponential backoff
            else:
                log_error("[selenium] All attempts to navigate to ChatGPT home failed")
                raise


def is_chat_archived(driver, chat_id: str) -> bool:
    """Check if a ChatGPT chat is archived."""
    try:
        chat_url = f"https://chat.openai.com/chat/{chat_id}"
        driver.get(chat_url)
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(text(), 'This conversation is archived')]"))
        )
        log_warning("[selenium] Chat is archived.")
        return True
    except TimeoutException:
        log_debug("[selenium] Chat is not archived.")
        return False
    except Exception as e:
        log_error(f"[selenium] Error checking if chat is archived: {repr(e)}")
        return False

# Update process_prompt_in_chat to use the new functions
def process_prompt_in_chat(
    driver, chat_id: str | None, prompt_text: str, previous_text: str, image_path: str | None = None
) -> Optional[str]:
    """Send a prompt to a ChatGPT chat and return the newly generated text."""
    if chat_id and is_chat_archived(driver, chat_id):
        chat_id = None  # Mark chat as invalid

    if not chat_id:
        log_debug("[selenium] Creating a new chat")
        _open_new_chat(driver)
        # Chat ID will be extracted later from the URL after sending the prompt

    # Handle image upload first if present
    if image_path and os.path.exists(image_path):
        log_info(f"[selenium] Uploading image to ChatGPT: {image_path}")
        if not _paste_image_to_chatgpt(driver, image_path):
            log_warning("[selenium] Failed to upload image, proceeding with text only")

    # Some UI experiments may block the textarea with a "I prefer this response"
    # dialog. Dismiss it if present before looking for the textarea.

    if not ensure_chatgpt_model(driver):
        log_warning("[chatgpt_model] Failed to ensure model")

    try:
        prefer_btn = WebDriverWait(driver, 2).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "[data-testid='paragen-prefer-response-button']")
            )
        )
        prefer_btn.click()
        time.sleep(2)
    except TimeoutException:
        pass
    except Exception as e:  # pragma: no cover - best effort
        log_warning(f"[selenium] Failed to click prefer-response button: {e}")

    try:
        textarea = _locate_prompt_area(driver, timeout=10)
    except TimeoutException:
        log_error("[selenium] prompt textarea not found")
        return None

    start = time.time()
    attempt = 0
    repeat_failures = 0
    last_response: Optional[str] = None
    while attempt < CORRECTOR_RETRIES and time.time() - start < AWAIT_RESPONSE_TIMEOUT:
        attempt += 1
        try:
            wait_for_chatgpt_idle(driver)
            
            try:
                paste_and_send(textarea, prompt_text)
                log_debug("[selenium] paste_and_send completed successfully")
            except Exception as paste_error:
                log_error(f"[selenium] Critical error in paste_and_send: {paste_error}")
                import traceback
                log_error(f"[selenium] paste_and_send traceback: {traceback.format_exc()}")
                return None
            
            try:
                tag = (textarea.tag_name or "").lower()
                prop = "value" if tag in {"textarea", "input"} else "textContent"
                final_value = driver.execute_script(
                    f"return arguments[0].{prop};", textarea
                ) or ""
                expected_len = len(strip_non_bmp(prompt_text))
                # Allow small discrepancies (e.g., trailing spaces trimmed by ChatGPT)
                if abs(expected_len - len(final_value)) > 5:
                    log_warning(
                        f"[selenium] Prompt mismatch after paste: expected {expected_len} chars, got {len(final_value)}"
                    )
            except Exception as check_error:
                log_error(f"[selenium] Error checking textarea content: {check_error}")
                # Continue anyway, the paste might have worked
                time.sleep(1)
                continue

            # Skip JSON validation for normal prompts (they now include persona identity before JSON)
            # JSON validation is only needed for correction/system messages which are still pure JSON
            
            # Try multiple selectors for the send button
            send_button_selectors = [
                (By.CSS_SELECTOR, "button[data-testid='send-button']"),
                (By.CSS_SELECTOR, "button[aria-label='Send prompt']"),
                (By.CSS_SELECTOR, "button.absolute.rounded-full"),
                (By.XPATH, "//button[@data-testid='send-button']"),
                (By.XPATH, "//button[contains(@class, 'bottom') and not(@disabled)]")
            ]
            
            button_clicked = False
            for selector in send_button_selectors:
                try:
                    send_btn = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable(selector)
                    )
                    driver.execute_script("arguments[0].click();", send_btn)
                    log_debug(f"[selenium][STEP] Clicked send button with selector: {selector}")
                    button_clicked = True
                    break
                except (StaleElementReferenceException, TimeoutException, NoSuchElementException):
                    continue
            
            # Fallback to ENTER key if button click failed
            if not button_clicked:
                log_warning("[selenium] All send button selectors failed, using ENTER key")
                try:
                    textarea.send_keys(Keys.ENTER)
                    log_debug("[selenium][STEP] Sent ENTER key as fallback")
                except Exception as e:
                    log_error(f"[selenium] Even ENTER key failed: {e}")
                    return None
        except ElementNotInteractableException as e:
            log_warning(f"[selenium][retry] Element not interactable: {e}")
            time.sleep(2)
            continue
        except Exception as e:
            log_error(f"[selenium][ERROR] Failed to send prompt: {repr(e)}")
            return None

        log_debug("🔍 Waiting for response...")
        if not wait_for_response_completion(driver):
            repeat_failures += 1
            log_warning("[selenium][retry] Response did not complete")
        else:
            try:
                response_text = wait_until_response_stabilizes(driver, max_total_wait=5)
            except TimeoutException:
                log_warning("[selenium][WARN] Timeout while waiting for response")
                response_text = None
            else:
                if response_text and response_text != previous_text:
                    if not chat_id:
                        new_chat_id = _extract_chat_id(driver.current_url)
                        if new_chat_id:
                            log_debug(
                                f"[selenium] New chat ID extracted after response: {new_chat_id}"
                            )
                    return response_text.strip()

            if response_text == last_response:
                repeat_failures += 1
            else:
                repeat_failures = 0
            last_response = response_text

        if repeat_failures >= 3:
            log_warning("[selenium] Aborting after repeated empty responses")
            break

        log_warning(f"[selenium][retry] Empty response attempt {attempt}")
        remaining = AWAIT_RESPONSE_TIMEOUT - (time.time() - start)
        if remaining > 0:
            time.sleep(min(5, remaining))

    log_warning(f"[selenium] Aborting after {attempt} attempts")
    screenshots_dir = os.path.join(_LOG_DIR, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    fname = os.path.join(screenshots_dir, f"chat_{chat_id or 'unknown'}_no_response.png")
    try:
        driver.save_screenshot(fname)
        log_warning(f"[selenium] Saved screenshot to {fname}")
    except Exception as e:
        log_warning(f"[selenium] Failed to save screenshot: {e}")
    notify_trainer(
        f"\u26A0\uFE0F No response received for chat_id={chat_id}. Screenshot: {fname}"
    )
    return None


# TODO: Chat renaming logic - currently commented out due to unreliable ChatGPT UI changes
# This functionality needs to be reimplemented when ChatGPT's interface stabilizes
# def rename_and_send_prompt(driver, chat_info, prompt_text: str) -> Optional[str]:
#     """Rename the active chat and send ``prompt_text``. Return the new response."""
#     try:
#         chat_name = (
#             chat_info.chat.title
#             or getattr(chat_info.chat, "full_name", "")
#             or str(chat_info.chat_id)
#         )
#         is_group = chat_info.chat.type in ("group", "supergroup")
#         emoji = "💬" if is_group else "💌"
#         thread = (
#             f"/Thread {chat_info.thread_id}" if getattr(chat_info, "thread_id", None) else ""
#         )
#         new_title = f"⚙️{emoji} Chat/{chat_name}{thread} - 1"
#         log_debug(f"[selenium][STEP] renaming chat to: {new_title}")

#         options_btn = WebDriverWait(driver, 5).until(
#             EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-testid='history-item-0-options']"))
#         )
#         options_btn.click()
#         script = (
#             "const buttons = Array.from(document.querySelectorAll('[data-testid=\"share-chat-menu-item\"]'));"
#             " const rename = buttons.find(b => b.innerText.trim() === 'Rename');"
#             " if (rename) rename.click();"
#         )
#         driver.execute_script(script)
#         rename_input = WebDriverWait(driver, 5).until(
    #         EC.element_to_be_clickable((By.CSS_SELECTOR, "[role='textbox']"))
    #     )
    #     rename_input.clear()
    #     rename_input.send_keys(strip_non_bmp(new_title))

    #     rename_input.send_keys(Keys.ENTER)
    #     log_debug("[DEBUG] Rename field found and edited")
    #     recent_chats.set_chat_path(chat_info.chat_id, new_title)
    # except Exception as e:
    #     log_warning(f"[selenium][ERROR] rename failed: {e}")

    # try:
    #     textarea = WebDriverWait(driver, 10).until(
    #         EC.element_to_be_clickable((By.ID, "prompt-textarea"))
    #     )
    # except TimeoutException:
    #     log_error("[selenium][ERROR] prompt textarea not found")
    #     return None

    # try:
    #     paste_and_send(textarea, prompt_text)
    #     textarea.send_keys(Keys.ENTER)
    # except Exception as e:
    #     log_error(f"[selenium][ERROR] failed to send prompt: {repr(e)}")
    #     return None

    # previous_text = get_previous_response(chat_info.chat_id)
    # log_debug("🔍 Waiting for response block...")
    # try:
    #     response_text = wait_until_response_stabilizes(driver)
    # except Exception as e:
    #     log_error(f"[selenium][ERROR] waiting for response failed: {repr(e)}")
    #     return None

    # if not response_text or response_text == previous_text:
    #     log_debug("🟡 No new response, skipping")
    #     return None
    # update_previous_response(chat_info.chat_id, response_text)
    # log_debug("📝 New response text extracted")
    # return response_text.strip()


# Funzione di selezione modello ChatGPT
CHATGPT_MODEL = config_registry.get_value(
    "CHATGPT_MODEL",
    "",
    label="ChatGPT Model",
    description="ChatGPT model to use (e.g., 'gpt-4o', 'gpt-4', 'o1-preview'). Leave empty to use default model.",
    group="llm",
    component="selenium_chatgpt_legacy",
)

def _update_chatgpt_model(value: str | None) -> None:
    """Update global CHATGPT_MODEL variable."""
    global CHATGPT_MODEL
    CHATGPT_MODEL = value or ""

config_registry.add_listener("CHATGPT_MODEL", _update_chatgpt_model)


def select_chatgpt_model(driver):
    """Seleziona il modello di ChatGPT in base alla variabile di ambiente CHATGPT_MODEL."""
    # Usa la variabile già definita che ha il default
    target_model = CHATGPT_MODEL
    if not target_model or target_model.strip() == "":
        log_info("[chatgpt_model] CHATGPT_MODEL not set, using default model")
        return

    # 1. Clicca sul pulsante del selettore in alto
    selector = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='Model picker'],button[data-qa='model-switcher']"))
    )
    selector.click()

    # 2. Attendi che il menu sia visibile
    WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "div[role='menu']"))
    )
    time.sleep(0.5)  # piccola pausa per permettere il rendering completo

    # 3. Cerca il modello nel menu principale
    model_elements = driver.find_elements(By.XPATH, f"//div[@role='menu']//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{target_model.lower()}')]")
    if model_elements:
        model_elements[0].click()
        return

    # 4. Se non trovato, espandi il gruppo "Legacy models" (freccia/voce con etichetta simile)
    legacy_toggle = driver.find_element(By.XPATH, "//div[@role='menu']//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'legacy')]")
    legacy_toggle.click()

    # 5. Attendi che compaiano le opzioni legacy e seleziona il modello
    WebDriverWait(driver, 5).until(
        EC.visibility_of_element_located((By.XPATH, "//div[@role='menu']//div[contains(., 'Legacy')]//span"))
    )
    legacy_models = driver.find_elements(By.XPATH, f"//div[@role='menu']//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{target_model.lower()}')]")
    if legacy_models:
        legacy_models[0].click()
        return

    raise RuntimeError(f"Il modello '{target_model}' non è stato trovato nel menu.")


def ensure_chatgpt_model(driver):
    """Ensure the desired ChatGPT model is active before sending a prompt."""
    # Check if CHATGPT_MODEL is set and not empty
    if not CHATGPT_MODEL or CHATGPT_MODEL.strip() == "" or CHATGPT_MODEL.upper() == "NONE":
        log_info("[chatgpt_model] CHATGPT_MODEL not set or disabled, skipping model selection")
        return True
        
    try:
        log_info(f"[chatgpt_model] Ensuring model {CHATGPT_MODEL} is active")
        select_chatgpt_model(driver)
        log_info(f"[chatgpt_model] Model {CHATGPT_MODEL} configured successfully")
        return True
    except Exception as e:
        log_warning(f"[chatgpt_model] Could not configure model {CHATGPT_MODEL}: {e}")
        log_info("[chatgpt_model] Continuing with current/default model")
        try:
            screenshots_dir = os.path.join(_LOG_DIR, "screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)
            screenshot_path = os.path.join(screenshots_dir, "model_switch_error.png")
            driver.save_screenshot(screenshot_path)
            log_info(f"[chatgpt_model] Saved screenshot {screenshot_path}")
        except Exception as ss:
            log_warning(f"[chatgpt_model] Screenshot failed: {ss}")
        return True  # Non bloccare l'esecuzione, continua con il modello corrente

class SeleniumChatGPTLegacyPlugin(AIPluginBase):
    display_name = "Selenium ChatGPT (Legacy)"
    
    # [FIX] shared locks per chat
    chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    def __init__(self, notify_fn=None):
        """Initialize the plugin without starting Selenium yet."""
        # Ensure CHROMIUM_HEADLESS is registered
        _ensure_chromium_headless_registered()
        
        self.driver = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task = None
        self._restarting = False  # Flag to prevent concurrent restart attempts
        self._notify_fn = notify_fn or notify_trainer
        log_debug(f"[selenium] notify_fn passed: {bool(notify_fn)}")
        set_notifier(self._notify_fn)

        # Unique identifier for this instance to isolate Chromium resources
        self.instance_id = os.getenv("SyntH_INSTANCE_ID", str(os.getpid()))
        self.profile_dir: Optional[str] = None

    def cleanup(self):
        """Clean up resources when the plugin is stopped."""
        log_debug("[selenium] Starting cleanup...")
        
        # Stop the worker task (best-effort)
        task = getattr(self, "_worker_task", None)
        if task and not task.done():
            try:
                task.cancel()
                log_debug("[selenium] Worker task cancelled (cleanup)")
            except Exception as e:
                log_warning(f"[selenium] Failed to cancel worker task during cleanup: {e}")
        
        # Clear the queue to prevent pending tasks
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
            log_debug("[selenium] Queue cleared")
        except Exception as e:
            log_warning(f"[selenium] Failed to clear queue: {e}")
        
        # Close the driver
        if self.driver:
            try:
                self.driver.quit()
                log_debug("[selenium] Chromium driver closed")
            except Exception as e:
                log_warning(f"[selenium] Failed to close driver: {e}")
            finally:
                self.driver = None
        
        # Remove any remaining Chromium processes and locks
        self._cleanup_chromium_remnants()

        log_debug("[selenium] Cleanup completed")

    async def stop(self):
        """Cancel worker task and run cleanup."""  # [FIX]
        task = getattr(self, "_worker_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                log_debug("[selenium] Worker task cancelled cleanly (stop)")
            except Exception as e:
                log_warning(f"[selenium] Exception while awaiting worker task during stop: {e}")
        self._worker_task = None
        # Run synchronous cleanup
        self.cleanup()

    async def start(self):
        """Start the background worker loop."""
        log_debug("[selenium] \U0001F7E2 start() called")
        if self.is_worker_running():
            log_debug("[selenium] Worker already running")
            return

        # Clear completed task reference if present
        if getattr(self, "_worker_task", None) is not None and self._worker_task.done():
            log_warning("[selenium] Previous worker task ended, restarting")
            self._worker_task = None

        # Create a fresh coroutine object and task
        try:
            self._worker_task = asyncio.create_task(self._worker_loop(), name="selenium_worker")
            self._worker_task.add_done_callback(self._handle_worker_done)
            log_debug("[selenium] Worker task created")
        except RuntimeError as e:
            # No running loop in this thread; schedule creation on the running loop
            log_warning(f"[selenium] Could not create worker task directly: {e}; scheduling on running loop")
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._worker_loop()))

    def is_worker_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    def _get_interface_name(self, bot) -> str:
        """Determine the interface name from the bot object."""
        module_name = getattr(bot.__class__, "__module__", "")
        if module_name.startswith("telegram"):
            return "telegram"
        elif hasattr(bot, "get_interface_id"):
            return bot.get_interface_id()
        else:
            return "generic"

    def _handle_worker_done(self, fut: asyncio.Future):
        """Handle worker task completion and attempt restart if needed."""
        if fut.cancelled():
            log_warning("[selenium] Worker task cancelled")
        else:
            exc = None
            try:
                exc = fut.exception()
            except Exception:
                exc = None
            if exc:
                log_warning(f"[selenium] Worker task crashed: {repr(exc)}")
                try:
                    tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    log_warning(f"[selenium] Worker traceback:\n{tb}")
                except Exception:
                    pass
        
        # Attempt restart if needed, but prevent concurrent restart attempts
        if self._restarting:
            log_debug("[selenium] Restart already in progress, skipping")
            return

        self._restarting = True
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                # Schedule restart as a new task
                async def restart_worker():
                    try:
                        await asyncio.sleep(0.1)  # Brief delay before restart
                        await self.start()
                    finally:
                        self._restarting = False
                
                loop.create_task(restart_worker())
            else:
                self._restarting = False
        except RuntimeError:
            self._restarting = False

    async def handle_incoming_message(self, bot, message, prompt):
        """Handle incoming messages by queuing them for processing."""
        try:
            # Check if this is a correction request by examining the prompt structure
            is_correction = (
                isinstance(prompt, str) and 
                ("correction" in prompt.lower() or "corrected" in prompt.lower() or 
                 "failed actions" in prompt.lower() or "valid JSON" in prompt.lower())
            ) or (
                isinstance(prompt, dict) and 
                prompt.get("system_message", {}).get("type") == "error"
            )
            
            # Check if this is a system message with output (from terminal plugin, etc.)
            is_system_output = (
                isinstance(prompt, dict) and 
                prompt.get("system_message", {}).get("type") == "output"
            )
            
            if is_correction or is_system_output:
                # For correction requests and system output, process synchronously and return the response
                request_type = "correction" if is_correction else "system output"
                log_debug(f"[selenium] Processing {request_type} request synchronously: chat_id={message.chat_id}")
                
                # Initialize driver if needed
                if not self.driver:
                    self._init_driver()
                
                # Process the message directly
                if is_correction:
                    response_text = await self._process_correction_message(bot, message, prompt)
                else:
                    response_text = await self._process_system_output_message(bot, message, prompt)
                return response_text
            else:
                # For normal messages, use the queue system
                await self._queue.put((bot, message, prompt))
                log_debug(f"[selenium] Message queued for processing: chat_id={message.chat_id}")
                
                # Ensure worker is running
                if not self.is_worker_running():
                    await self.start()
                    
        except Exception as e:
            log_error(f"[selenium] Failed to queue message: {repr(e)}", e)
            # Send error message if queuing fails
            await self._send_error_message(bot, message, error_text=f"Failed to queue message: {e}")
            
    async def _process_correction_message(self, bot, message, prompt):
        """Process a correction message synchronously and return the response."""
        try:
            log_debug(f"[selenium] Processing correction prompt: {prompt}")
            
            # Handle both dict and string inputs
            if isinstance(prompt, dict):
                prompt_text = json.dumps(prompt, ensure_ascii=False)
            else:
                prompt_text = prompt
            
            # Disable driver initialization retries for correction processing
            max_attempts = 1
            response_text = None
            
            for attempt in range(max_attempts):
                try:
                    if not self.driver:
                        self._init_driver()
                    
                    # Get chat ID for ChatGPT conversation
                    thread_id_for_link = getattr(message, "thread_id", None)
                    log_debug(f"[selenium] _process_correction_message: Getting ChatGPT link with chat_id={message.chat_id}, thread_id={thread_id_for_link}")
                    
                    chat_id = await chat_link_store.get_chatgpt_link(
                        message.chat_id, 
                        thread_id_for_link,
                        interface=self._get_interface_name(bot)
                    )
                    
                    # Process the prompt in ChatGPT with timeout
                    previous_text = get_previous_response(str(message.chat_id))
                    import asyncio
                    timeout_seconds = 300  # 5 minutes timeout
                    response_text = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: process_prompt_in_chat(self.driver, chat_id, prompt_text, previous_text)
                        ),
                        timeout=timeout_seconds
                    )
                    
                    if response_text:
                        # Update response cache
                        update_previous_response(str(message.chat_id), response_text)
                        log_debug(f"[selenium] Correction response generated: {len(response_text)} chars")
                        return response_text.strip()
                    else:
                        log_warning("[selenium] No response from ChatGPT for correction")
                        return None
                        
                except Exception as e:
                    log_error(f"[selenium] Error processing correction message: {e}")
                    if attempt == max_attempts - 1:
                        return None
                        
        except Exception as e:
            log_error(f"[selenium] Failed to process correction message: {e}")
            return None

    async def _process_system_output_message(self, bot, message, prompt):
        """Process a system output message (e.g., from terminal plugin) synchronously and return the response."""
        try:
            log_debug(f"[selenium] Processing system output prompt: {prompt}")
            
            # Handle dict input (system_message structure)
            if isinstance(prompt, dict):
                prompt_text = json.dumps(prompt, ensure_ascii=False)
            else:
                prompt_text = prompt
            
            # Disable driver initialization retries for system output processing
            max_attempts = 1
            response_text = None
            
            for attempt in range(max_attempts):
                try:
                    if not self.driver:
                        self._init_driver()
                    
                    # Get chat ID for ChatGPT conversation
                    thread_id_for_link = getattr(message, "thread_id", None)
                    log_debug(f"[selenium] _process_system_output_message: Getting ChatGPT link with chat_id={message.chat_id}, thread_id={thread_id_for_link}")
                    
                    chat_id = await chat_link_store.get_chatgpt_link(
                        message.chat_id, 
                        thread_id_for_link,
                        interface=self._get_interface_name(bot)
                    )
                    
                    # Process the prompt in ChatGPT with timeout
                    previous_text = get_previous_response(str(message.chat_id))
                    import asyncio
                    timeout_seconds = 300  # 5 minutes timeout
                    response_text = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: process_prompt_in_chat(self.driver, chat_id, prompt_text, previous_text)
                        ),
                        timeout=timeout_seconds
                    )
                    
                    if response_text:
                        # Update response cache
                        update_previous_response(str(message.chat_id), response_text)
                        log_debug(f"[selenium] System output response generated: {len(response_text)} chars")
                        return response_text.strip()
                    else:
                        log_warning("[selenium] No response from ChatGPT for system output")
                        return None
                        
                except Exception as e:
                    log_error(f"[selenium] Error processing system output message: {e}")
                    if attempt == max_attempts - 1:
                        return None
                        
        except Exception as e:
            log_error(f"[selenium] Failed to process system output message: {e}")
            return None

    async def _worker_loop(self):
        """Process messages from the queue sequentially."""
        log_debug("[selenium] Worker loop started")
        try:
            while True:
                try:
                    # Get message from queue
                    bot, message, prompt = await self._queue.get()
                    log_debug(f"[selenium] Processing message from queue: chat_id={message.chat_id}")
                    
                    # Process the message
                    await self._process_message(bot, message, prompt)
                    
                    # Mark task as done
                    self._queue.task_done()
                    
                except asyncio.CancelledError:
                    log_debug("[selenium] Worker loop cancelled")
                    break
                except RuntimeError as e:
                    # During shutdown the queue.get() or internal cancel may raise
                    # RuntimeError('Event loop is closed') or similar. Treat these as
                    # a signal to stop the worker loop instead of crashing.
                    msg = str(e)
                    if "bound to a different event loop" in msg or "Event loop is closed" in msg:
                        log_warning(f"[selenium] Event loop problem, stopping worker: {e}")
                        break
                    else:
                        raise
                except Exception as e:
                    log_error(f"[selenium] Error in worker loop: {repr(e)}", e)
                    # Continue processing other messages even if one fails
                    continue
                    
        except Exception as e:
            log_error(f"[selenium] Worker loop crashed: {repr(e)}", e)
        finally:
            log_debug("[selenium] Worker loop ended")

    def _apply_driver_timeouts(self) -> None:
        """Apply environment-based timeouts to the Selenium driver."""
        if not self.driver:
            return
        try:
            self.driver.command_executor.set_timeout(AWAIT_RESPONSE_TIMEOUT)
            self.driver.set_page_load_timeout(AWAIT_RESPONSE_TIMEOUT)
            self.driver.set_script_timeout(AWAIT_RESPONSE_TIMEOUT)
            log_debug(
                f"[selenium] Driver timeouts set to {AWAIT_RESPONSE_TIMEOUT}s"
            )
        except Exception as e:
            log_warning(f"[selenium] Failed to set driver timeouts: {e}")

    def _locate_chromium_binary(self) -> str:
        """Return path to the Chromium executable, checking common locations."""
        chromium_binary = (
            shutil.which("chromium")
            or shutil.which("chromium-browser")
            or "/usr/bin/chromium"
        )
        log_debug(f"[selenium] Using Chromium binary: {chromium_binary}")
        return chromium_binary

    def _get_chromium_major_version(self, binary: str) -> Optional[int]:
        """Return the major version of the given Chromium binary."""
        try:
            output = subprocess.check_output([binary, "--version"], text=True)
            match = re.search(r"(\d+)\.", output)
            if match:
                return int(match.group(1))
        except Exception as e:
            log_warning(f"[selenium] Unable to determine Chromium version: {e}")
        return None

    def _init_driver(self):
        if self.driver is None:
            log_debug("[selenium] [STEP] Initializing Chromium driver with undetected-chromedriver")

            # Clean up any leftover processes and files from previous runs
            self._cleanup_chromium_remnants()

            # Ensure DISPLAY is set
            if not os.environ.get("DISPLAY"):
                os.environ["DISPLAY"] = ":0"
                log_debug("[selenium] DISPLAY not set, defaulting to :0")

            # Precompute logging and service configuration so they remain available
            chromium_level = os.environ.get("CHROMIUM_LOG_LEVEL", "1")
            log_dir = "/app/logs"
            os.makedirs(log_dir, exist_ok=True)
            chromium_log_path = os.path.join(log_dir, "chromium.log")
            uc_log_path = os.path.join(log_dir, "undetected_chromedriver.log")
            selenium_log_path = os.path.join(log_dir, "selenium.log")
            service = Service(log_path=uc_log_path)

            # Ensure Chromium writes verbose logs to the desired location
            os.environ["CHROME_LOG_FILE"] = chromium_log_path
            log_debug(
                f"[selenium] Chromium logs directed to {chromium_log_path} with verbosity {chromium_level}"
            )

            # Configure Python logging for selenium and undetected-chromedriver modules
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            for name, path in (
                ("selenium", selenium_log_path),
                ("undetected_chromedriver", uc_log_path),
            ):
                logger = logging.getLogger(name)
                if not any(
                    isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == path
                    for h in logger.handlers
                ):
                    fh = logging.FileHandler(path)
                    fh.setFormatter(formatter)
                    logger.addHandler(fh)
                logger.setLevel(logging.DEBUG)

            # Essential Chromium arguments reused across attempts
            essential_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-web-security",
                "--start-maximized",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-popup-blocking",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--memory-pressure-off",
                "--disable-features=VizDisplayCompositor",
                "--enable-logging",
                f"--v={chromium_level}",
                "--remote-debugging-port=0",
                "--disable-background-mode",
                "--disable-default-browser-check",
                "--disable-hang-monitor",
                "--disable-prompt-on-repost",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-default-browser-check",
                "--safebrowsing-disable-auto-update",
                "--disable-client-side-phishing-detection",
            ]

            # Use a shared profile directory to maintain login sessions
            config_home = os.getenv(
                "XDG_CONFIG_HOME",
                os.path.join(os.path.expanduser("~"), ".config"),
            )
            profile_dir = os.path.join(config_home, "chromium-synth")
            self.profile_dir = profile_dir

            chromium_binary = self._locate_chromium_binary()
            chromium_major = self._get_chromium_major_version(chromium_binary)
            if chromium_major:
                log_debug(f"[selenium] Detected Chromium major version {chromium_major}")
            else:
                log_warning("[selenium] Could not detect Chromium version; using default driver")

            # Try multiple times with increasing delays
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    log_debug(f"[selenium] Initialization attempt {attempt + 1}/{max_retries}")

                    # Create Chromium options optimized for container environments
                    options = uc.ChromeOptions()

                    # Configure Chromium/chromedriver logging based on LOGGING_LEVEL
                    os.makedirs(_LOG_DIR, exist_ok=True)
                    log_path = os.path.join(_LOG_DIR, "chromium.log")
                    service_log_path = os.path.join(_LOG_DIR, "chromedriver.log")
                    service = Service(log_path=service_log_path, service_args=["--verbose"])
                    log_debug(
                        f"[selenium] Chromium log -> {log_path}, chromedriver log -> {service_log_path}"
                    )

                    logging_level = os.getenv("LOGGING_LEVEL", "ERROR").upper()
                    level_map = {"DEBUG": 0, "INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 2}
                    chromium_level = level_map.get(logging_level, 2)

                    # Essential options for Docker containers
                    essential_args = [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-setuid-sandbox",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-web-security",
                        "--start-maximized",
                        "--no-first-run",
                        "--disable-default-apps",
                        "--disable-popup-blocking",
                        "--disable-infobars",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--memory-pressure-off",
                        "--disable-features=VizDisplayCompositor",
                        "--enable-logging",
                        f"--log-level={chromium_level}",
                        f"--log-file={log_path}",
                        "--remote-debugging-port=0",
                        "--disable-background-mode",
                        "--disable-default-browser-check",
                        "--disable-hang-monitor",
                        "--disable-prompt-on-repost",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-default-browser-check",
                        "--safebrowsing-disable-auto-update",
                        "--disable-client-side-phishing-detection",
                    ]
                    for arg in essential_args:
                        options.add_argument(arg)
                    options.add_argument(f"--user-data-dir={profile_dir}")

                    # Clear any existing driver cache
                    import tempfile
                    import shutil
                    uc_cache_dir = os.path.join(tempfile.gettempdir(), 'undetected_chromedriver')
                    if os.path.exists(uc_cache_dir):
                        shutil.rmtree(uc_cache_dir, ignore_errors=True)
                        log_debug("[selenium] Cleared undetected-chromedriver cache")

                    # Try with explicit Chromium binary
                    log_debug(
                        f"[selenium] Calling {chromium_binary} {' '.join(options.arguments)}"
                    )
                    headless = bool(CHROMIUM_HEADLESS)
                    log_debug(
                        f"[selenium] Headless mode {'enabled' if headless else 'disabled'}"
                    )
                    self.driver = uc.Chrome(
                        options=options,
                        service=service,
                        headless=headless,
                        use_subprocess=True,
                        version_main=chromium_major,
                        suppress_welcome=True,
                        log_level=int(chromium_level),
                        driver_executable_path=None,  # Let UC handle chromedriver
                        browser_executable_path=chromium_binary,
                        user_data_dir=profile_dir
                    )
                    self._apply_driver_timeouts()
                    log_debug(
                        "[selenium] ✅ Chromium successfully initialized with undetected-chromedriver"
                    )
                    return  # Success, exit

                except Exception as e:
                    log_warning(f"[selenium] Attempt {attempt + 1} failed: {e}")

                    # Handle specific Python shutdown error
                    if "sys.meta_path is None" in str(e) or "Python is likely shutting down" in str(e):
                        log_warning("[selenium] Python shutdown detected, skipping Chromium initialization")
                        return None

                    # Clean up before next attempt
                    if self.driver:
                        try:
                            self.driver.quit()
                        except:
                            pass
                        self.driver = None

                    self._cleanup_chromium_remnants()

                    if attempt < max_retries - 1:
                        delay = (attempt + 1) * 2  # 2, 4, 6 seconds
                        log_debug(f"[selenium] Waiting {delay}s before next attempt...")
                        time.sleep(delay)
                    else:
                        # Final attempt with explicit Chromium binary
                        log_debug("[selenium] Final attempt with explicit Chromium binary path...")
                        try:
                            if os.path.exists(chromium_binary):
                                # Create fresh ChromiumOptions for fallback attempt
                                fallback_options = uc.ChromeOptions()
                                for arg in essential_args:
                                    fallback_options.add_argument(arg)
                                fallback_options.add_argument(f"--user-data-dir={profile_dir}")
                                log_debug(
                                    f"[selenium] Calling chromium with command: {chromium_binary} {' '.join(fallback_options.arguments)}"
                                )
                                self.driver = uc.Chrome(
                                    options=fallback_options,
                                    service=service,
                                    headless=False,
                                    use_subprocess=True,
                                    version_main=chromium_major,
                                    suppress_welcome=True,
                                    log_level=int(chromium_level),
                                    browser_executable_path=chromium_binary,
                                    user_data_dir=profile_dir
                                )
                                self._apply_driver_timeouts()
                                log_debug(
                                    "[selenium] ✅ Chromium initialized with explicit binary path"
                                )
                                return
                            else:
                                raise Exception("Chromium binary not found")

                        except Exception as e2:
                            log_warning("[selenium] Chromium lock suspected - attempting forced lock cleanup...")
                            self._cleanup_chromium_remnants()
                            try:
                                if os.path.exists(chromium_binary):
                                    fallback_options = uc.ChromeOptions()
                                    for arg in essential_args:
                                        fallback_options.add_argument(arg)
                                    fallback_options.add_argument(f"--user-data-dir={profile_dir}")
                                    log_debug(
                                        f"[selenium] Calling chromium with command: {chromium_binary} {' '.join(fallback_options.arguments)}"
                                    )
                                    self.driver = uc.Chrome(
                                        options=fallback_options,
                                        service=service,
                                        headless=False,
                                        use_subprocess=True,
                                        version_main=chromium_major,
                                        suppress_welcome=True,
                                        log_level=int(chromium_level),
                                        browser_executable_path=chromium_binary,
                                        user_data_dir=profile_dir
                                    )
                                    self._apply_driver_timeouts()
                                    log_debug(
                                        "[selenium] ✅ Chromium initialized after forced lock cleanup"
                                    )
                                    return
                                else:
                                    raise Exception("Chromium binary not found")
                            except Exception as e3:
                                log_error(
                                    f"[selenium] ❌ All initialization attempts failed: {e3}"
                                )
                                _notify_gui(
                                    f"❌ Selenium error: {e3}. Check graphics environment."
                                )
                                # Propagate error without shutting down the whole process
                                raise RuntimeError(
                                    f"Chromium initialization failed after retries: {e3}"
                                )

    def _cleanup_chromium_remnants(self):
        """Clean up Chromium processes and leftover lock files."""
        try:
            parent_pid = str(os.getpid())
            subprocess.run(
                ["pkill", "-P", parent_pid, "-f", "chromium"],
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["pkill", "-P", parent_pid, "-f", "chromedriver"],
                capture_output=True,
                text=True,
            )
            log_debug("[selenium] Issued pkill for chromium and chromedriver owned by this process")
            time.sleep(1)
        except Exception as e:
            log_debug(f"[selenium] Failed to kill chromium processes: {e}")

        try:
            patterns = []
            if self.instance_id:
                patterns.append(f"/tmp/.org.chromium.*{self.instance_id}*")
                patterns.append(f"/tmp/chromium_{self.instance_id}*")

            for pattern in patterns:
                log_debug(f"[selenium] Scanning {pattern}")
                for prof_dir in glob.glob(pattern):
                    for name in [
                        "SingletonLock",
                        "lockfile",
                        "SingletonSocket",
                        "SingletonCookie",
                    ]:
                        path = os.path.join(prof_dir, name)
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                                log_debug(f"[selenium] Removed lock file: {path}")
                            except Exception as e:
                                log_debug(f"[selenium] Failed to remove {path}: {e}")
        except Exception as e:
            log_debug(f"[selenium] Lock file cleanup failed: {e}")

        log_debug("[selenium] Chromium lock cleanup complete")
        try:
            root_pwd = os.getenv("ROOT_PASSWORD")
            if not root_pwd:
                log_debug("[selenium] ROOT_PASSWORD not set; skipping /config permission reset")
            else:
                cmds = [
                    ["chown", "-R", "abc:abc", "/config"],
                    ["chmod", "ug+rwx", "-R", "/config"],
                ]
                for cmd in cmds:
                    subprocess.run(
                        ["sudo", "-S", *cmd],
                        input=f"{root_pwd}\n",
                        text=True,
                        check=False,
                    )
        except Exception as e:
            log_debug(f"[selenium] Failed to reset /config permissions: {e}")

    # [FIX] ensure the WebDriver session is alive before use
    def _get_driver(self):
        """Return a valid WebDriver, recreating it if the session is dead."""
        if self.driver is None:
            try:
                self._init_driver()
            except Exception as e:
                log_error(f"[selenium] Failed to initialize driver: {e}")
                return None
        else:
            try:
                # simple command to verify the session is still alive
                self.driver.execute_script("return 1")
            except Exception as e:
                log_warning(f"[selenium] WebDriver session error: {e}. Restarting")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
                try:
                    self._init_driver()
                except Exception as e:
                    log_error(f"[selenium] Failed to reinitialize driver: {e}")
                    return None
        return self.driver

    def _ensure_logged_in(self):
        try:
            current_url = self.driver.current_url
        except Exception:
            current_url = ""
        log_debug(f"[selenium] [STEP] Checking login state at {current_url}")

        if not current_url.startswith("https://chat.openai.com") and not current_url.startswith("https://chatgpt.com"):
            try:
                self.driver.get("https://chat.openai.com")
                current_url = self.driver.current_url
            except Exception as e:
                log_warning(f"[selenium] Failed to navigate to ChatGPT home: {e}")
            if not current_url.startswith(("https://chat.openai.com", "https://chatgpt.com")):
                _notify_gui("🔐 Login or challenge detected. Open UI")
                return False

        if current_url and ("login" in current_url or "auth0" in current_url):
            log_debug("[selenium] Login required, notifying user")
            _notify_gui("🔐 Login required. Open UI")
            return False

        log_debug("[selenium] Logged in and ready")
        return True

    async def _send_error_message(self, bot, message, error_text="😵‍💫"):
        """Send an error message to the chat."""
        send_params = {"chat_id": message.chat_id, "text": error_text}
        reply_id = getattr(message, "message_id", None)
        if reply_id is not None:
            send_params["reply_to_message_id"] = reply_id
        thread_id = getattr(message, "thread_id", None)
        if thread_id is not None:
            send_params["thread_id"] = thread_id

        # Send system messages through the interface_to_llm entry so they follow
        # the interface-origin path (no corrector middleware run).
        try:
            from core.transport_layer import llm_to_interface
        except Exception:
            llm_to_interface = None

        try:
            if llm_to_interface is None:
                # Fallback: call directly if transport wrapper unavailable
                await bot.send_message(**send_params)
            else:
                # Use uniform llm_to_interface for all interfaces
                await llm_to_interface(bot.send_message, **send_params)
        except Exception as e:
            # Preserve previous logging behavior
            log_warning(f"[selenium][STEP] error response forwarding failed: {e}")

        log_debug(
            f"[selenium][STEP] error response forwarded to {message.chat_id}"
        )

    async def _process_message(self, bot, message, prompt):
        """Send the prompt to ChatGPT and forward the response."""
        log_debug(f"[selenium][STEP] processing prompt: {prompt}")

        # Check if prompt contains image data
        # Handle both dict and string inputs
        if isinstance(prompt, dict):
            prompt_text = json.dumps(prompt, ensure_ascii=False)
            image_info = _extract_image_info_from_prompt(prompt)
        else:
            # prompt is already a string
            prompt_text = prompt
            image_info = _extract_image_info_from_prompt(prompt_text)
        
        temp_image_path = None
        
        # Download image if present
        if image_info:
            log_info(f"[selenium] Processing message with image: {image_info.get('type', 'unknown')}")
            temp_dir = tempfile.mkdtemp(prefix="synth_images_")
            
            try:
                # Handle different image sources
                image_data = image_info.get('image_data', {})
                
                if image_data.get('type') == 'photo' and 'file_id' in image_data:
                    # Telegram photo
                    temp_image_path = await _download_telegram_image(
                        bot, image_data['file_id'], temp_dir
                    )
                elif image_data.get('type') == 'document' and 'file_id' in image_data:
                    # Telegram document (image)
                    temp_image_path = await _download_telegram_image(
                        bot, image_data['file_id'], temp_dir
                    )
                elif image_data.get('type') == 'attachment' and 'url' in image_data:
                    # Discord attachment or other URL-based image
                    try:
                        response = requests.get(image_data['url'], timeout=30)
                        response.raise_for_status()
                        
                        filename = image_data.get('filename', 'image.jpg')
                        temp_image_path = os.path.join(temp_dir, filename)
                        
                        with open(temp_image_path, 'wb') as f:
                            f.write(response.content)
                        
                        log_debug(f"[selenium] Downloaded image from URL to: {temp_image_path}")
                    except Exception as e:
                        log_error(f"[selenium] Failed to download image from URL: {e}")
                
                if not temp_image_path:
                    log_warning("[selenium] Could not download image, proceeding with text only")
                    
            except Exception as e:
                log_error(f"[selenium] Error processing image: {e}")
                temp_image_path = None

        max_attempts = 3
        for attempt in range(max_attempts):
            driver = self._get_driver()
            if not driver:
                log_error("[selenium] WebDriver unavailable, aborting")
                _notify_gui("\u274c Selenium driver not available. Open UI")
                await self._send_error_message(bot, message)
                return
            if (
                not driver.service
                or not getattr(driver.service, "process", None)
                or driver.service.process.poll() is not None
            ):
                log_warning("[selenium] Driver process not running, restarting")
                driver = self._get_driver()
                if not driver:
                    log_error("[selenium] Failed to restart WebDriver")
                    _notify_gui("\u274c Selenium driver not available. Open UI")
                    await self._send_error_message(bot, message)
                    return

            if not self._ensure_logged_in():
                if attempt == max_attempts - 1:
                    await self._send_error_message(bot, message)
                    return
                time.sleep(2 * (attempt + 1))
                continue

            log_debug("[selenium][STEP] ensuring ChatGPT is accessible")

            module_name = getattr(bot.__class__, "__module__", "")
            if module_name.startswith("telegram"):
                interface_name = "telegram"
            elif hasattr(bot, "get_interface_id"):
                interface_name = bot.get_interface_id()
            else:
                interface_name = "generic"
            thread_id = getattr(message, "thread_id", None)
            chat_id = await chat_link_store.get_chatgpt_link(
                message.chat_id, thread_id, interface=interface_name
            )
            
            # Format the prompt with proper instructions
            # For system messages (errors, terminal output), wrap in code block
            if isinstance(prompt, dict) and "system_message" in prompt:
                prompt_text = json.dumps(prompt, ensure_ascii=False)
                prompt_text = f"```json\n{prompt_text}\n```"
            else:
                # For normal prompts, extract persona identity and prepend as clear instructions
                # Then remove it from the JSON to avoid duplication
                persona_identity = ""
                if isinstance(prompt, dict):
                    context = prompt.get("context", {})
                    persona_data = context.get("persona", "")
                    
                    if persona_data:
                        # Extract the persona identity to use as role instructions
                        persona_identity = persona_data
                        # Remove persona from context to avoid duplication
                        prompt_copy = json.loads(json.dumps(prompt))  # Deep copy
                        if "persona" in prompt_copy.get("context", {}):
                            del prompt_copy["context"]["persona"]
                        prompt_text = json.dumps(prompt_copy, ensure_ascii=False)
                    else:
                        prompt_text = json.dumps(prompt, ensure_ascii=False)
                else:
                    prompt_text = json.dumps(prompt, ensure_ascii=False)
                
                # Build the final prompt with identity first, then JSON context
                if persona_identity:
                    prompt_text = f"{persona_identity}\n\nYou must respond as this persona using the following context and instructions. Read the JSON below and respond accordingly:\n\n```json\n{prompt_text}\n```"
                else:
                    # Fallback: at least tell ChatGPT what to do with the JSON
                    prompt_text = f"Use the following JSON context to respond. Read carefully and respond as instructed:\n\n```json\n{prompt_text}\n```"
            if not chat_id:
                path = recent_chats.get_chat_path(message.chat_id)
                if path and go_to_chat_by_path_with_retries(driver, path):
                    chat_id = _extract_chat_id(driver.current_url)
                    if chat_id:
                        await chat_link_store.store_chatgpt_link(
                            message.chat_id,
                            chat_id,
                            thread_id,
                            interface=interface_name,
                        )
                        _safe_notify(
                            f"\u26a0\ufe0f Couldn't find ChatGPT conversation for chat_id={message.chat_id}, thread_id={thread_id}.\n"
                            f"A new ChatGPT chat has been created: {chat_id}"
                        )
                else:
                    if path:
                        log_warning(f"[selenium] Chat path {path} no longer accessible (archived/deleted), creating new chat")
                        recent_chats.clear_chat_path(message.chat_id)
                    _open_new_chat(driver)
            else:
                chat_url = f"https://chat.openai.com/c/{chat_id}"
                try:
                    driver.get(chat_url)
                    WebDriverWait(driver, 120).until(
                        EC.presence_of_element_located((By.TAG_NAME, "textarea"))
                    )
                    log_debug(f"[selenium] Successfully accessed existing chat: {chat_id}")
                except TimeoutException:
                    log_warning("[selenium] ChatGPT UI not ready after loading existing chat")
                    if attempt == max_attempts - 1:
                        _notify_gui("\u274c ChatGPT UI not ready. Open UI")
                        await self._send_error_message(bot, message)
                        return
                    time.sleep(2 * (attempt + 1))
                    continue
                except Exception as e:
                    log_warning(f"[selenium] Existing chat {chat_id} no longer accessible: {e}")
                    log_info(f"[selenium] Creating new chat to replace inaccessible chat {chat_id}")
                    await chat_link_store.remove_chatgpt_link(
                        message.chat_id, thread_id, interface=interface_name
                    )
                    recent_chats.clear_chat_path(message.chat_id)
                    _open_new_chat(driver)
                    chat_id = None

            log_debug(f"[selenium][DEBUG] Chat ID from store: {chat_id}")
            log_debug(
                f"[selenium][DEBUG] source chat_id: {message.chat_id}, thread_id: {thread_id}"
            )

            if not chat_id:
                try:
                    driver.get("https://chat.openai.com")
                    WebDriverWait(driver, 180).until(
                        EC.presence_of_element_located((By.TAG_NAME, "textarea"))
                    )
                except TimeoutException:
                    log_warning("[selenium][ERROR] ChatGPT UI failed to become ready")
                    if attempt == max_attempts - 1:
                        _notify_gui("\u274c Selenium error: ChatGPT UI not ready. Open UI")
                        await self._send_error_message(bot, message)
                        return
                    time.sleep(2 * (attempt + 1))
                    continue
                except Exception:
                    log_warning("[selenium][ERROR] ChatGPT UI failed to load")
                    if attempt == max_attempts - 1:
                        _notify_gui("\u274c Selenium error: ChatGPT UI not ready. Open UI")
                        await self._send_error_message(bot, message)
                        return
                    time.sleep(2 * (attempt + 1))
                    continue

            try:
                # Critical section: process prompt with robust error handling
                response_text = None
                try:
                    import asyncio
                    # Add timeout to prevent blocking the entire system
                    timeout_seconds = 300  # 5 minutes timeout
                    
                    if chat_id:
                        previous = get_previous_response(message.chat_id)
                        response_text = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, 
                                lambda: process_prompt_in_chat(driver, chat_id, prompt_text, previous, temp_image_path)
                            ),
                            timeout=timeout_seconds
                        )
                        if response_text:
                            update_previous_response(message.chat_id, response_text)
                    else:
                        previous = get_previous_response(message.chat_id)
                        response_text = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: process_prompt_in_chat(driver, None, prompt_text, previous, temp_image_path)
                            ),
                            timeout=timeout_seconds
                        )
                        if response_text:
                            update_previous_response(message.chat_id, response_text)
                            new_chat_id = _extract_chat_id(driver.current_url)
                            log_debug(f"[selenium][DEBUG] New chat created, extracted ID: {new_chat_id}")
                            log_debug(f"[selenium][DEBUG] Current URL: {driver.current_url}")
                            if new_chat_id:
                                await chat_link_store.store_chatgpt_link(
                                    message.chat_id,
                                    new_chat_id,
                                    thread_id,
                                    interface=interface_name,
                                )
                                log_debug(
                                    f"[selenium][DEBUG] Saved link: {message.chat_id}/{thread_id} -> {new_chat_id}"
                                )
                                _safe_notify(
                                    f"\u26a0\ufe0f Couldn't find ChatGPT conversation for chat_id={message.chat_id}, thread_id={thread_id}.\n"
                                    f"A new ChatGPT chat has been created: {new_chat_id}"
                                )
                            else:
                                log_warning("[selenium][WARN] Failed to extract chat ID from URL")
                
                except asyncio.TimeoutError:
                    log_error(f"[selenium] TIMEOUT: process_prompt_in_chat took longer than {timeout_seconds} seconds")
                    _notify_gui("\u23f3 ChatGPT request timed out. Try again")
                    await self._send_error_message(bot, message)
                    return
                except Exception as prompt_error:
                    # Critical error in process_prompt_in_chat
                    log_error(f"[selenium] CRITICAL ERROR in process_prompt_in_chat: {repr(prompt_error)}")
                    log_error(f"[selenium] Chat: {message.chat_id}, ChatGPT ID: {chat_id}")
                    log_error(f"[selenium] Full traceback: {traceback.format_exc()}")
                    response_text = None  # Ensure it's None for fallback handling

                if _check_conversation_full(driver):
                    current_id = chat_id or _extract_chat_id(driver.current_url)
                    global queue_paused
                    queue_paused = True
                    _open_new_chat(driver)
                    # Process prompt in new chat with timeout
                    import asyncio
                    timeout_seconds = 300  # 5 minutes timeout
                    response_text = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: process_prompt_in_chat(driver, None, prompt_text, "", temp_image_path)
                        ),
                        timeout=timeout_seconds
                    )
                    new_chat_id = _extract_chat_id(driver.current_url)
                    if new_chat_id:
                        await chat_link_store.store_chatgpt_link(
                            message.chat_id,
                            new_chat_id,
                            thread_id,
                            interface=interface_name,
                        )
                        log_debug(
                            f"[selenium][SUCCESS] New chat created for full conversation. Chat ID: {new_chat_id}"
                        )
                    queue_paused = False

                # Handle case where ChatGPT completely failed to generate a response
                if response_text is None:
                    try:
                        from core.message_chain import get_failed_message_text
                        fallback_text = str(get_failed_message_text())
                    except Exception:
                        import os
                        fallback_text = os.getenv('FAILED_MESSAGE_TEXT', 'LLM failed')
                    log_error(f"[selenium] LLM FAILURE - Chat: {message.chat_id}, Reason: ChatGPT returned None (complete failure)")
                    log_error(f"[selenium] Sending fallback message: '{fallback_text}'")
                    response_text = fallback_text

                # Send response through llm_to_interface
                if response_text and response_text.strip():
                    await llm_to_interface(
                        bot.send_message,
                        chat_id=message.chat_id,
                        text=response_text,
                        reply_to_message_id=getattr(message, "message_id", None),
                        thread_id=thread_id,
                    )
                else:
                    # Send fallback message when LLM fails to generate response
                    try:
                        from core.message_chain import get_failed_message_text
                        fallback_text = str(get_failed_message_text())
                    except Exception:
                        import os
                        fallback_text = os.getenv('FAILED_MESSAGE_TEXT', 'LLM failed')
                    log_error(f"[selenium] LLM FAILURE - Chat: {message.chat_id}, Reason: Empty response from ChatGPT")
                    log_error(f"[selenium] Sending fallback message: '{fallback_text}'")
                    await llm_to_interface(
                        bot.send_message,
                        chat_id=message.chat_id,
                        text=fallback_text,
                        reply_to_message_id=getattr(message, "message_id", None),
                        thread_id=thread_id,
                    )

                log_debug(
                    f"[selenium][STEP] response forwarded to {message.chat_id}"
                )
                break  # Success, exit the retry loop

            except Exception as e:
                log_error(f"[selenium][ERROR] failed to process message: {repr(e)}", e)
                log_error(f"[selenium] Full exception traceback: {traceback.format_exc()}")
                _notify_gui(f"\u274c Selenium error: {e}. Open UI")
                if attempt == max_attempts - 1:
                    # Final fallback: send fallback message when all attempts failed
                    try:
                        try:
                            from core.message_chain import get_failed_message_text
                            fallback_text = str(get_failed_message_text())
                        except Exception:
                            import os
                            fallback_text = os.getenv('FAILED_MESSAGE_TEXT', 'LLM failed')
                        log_error(f"[selenium] LLM FAILURE - Chat: {message.chat_id}, Reason: All {max_attempts} attempts failed with exception")
                        log_error(f"[selenium] Sending final fallback message: '{fallback_text}'")
                        
                        await llm_to_interface(
                            bot.send_message,
                            chat_id=message.chat_id,
                            text=fallback_text,
                            reply_to_message_id=getattr(message, "message_id", None),
                            thread_id=thread_id,
                        )
                    except Exception as fallback_error:
                        log_error(f"[selenium] CRITICAL: Even fallback message failed: {repr(fallback_error)}")
                    break  # Max attempts reached, exit
                
        # Clean up temporary image file
        if temp_image_path and os.path.exists(temp_image_path):
            try:
                os.remove(temp_image_path)
                log_debug(f"[selenium] Cleaned up temporary image: {temp_image_path}")
            except Exception as e:
                log_warning(f"[selenium] Failed to clean up temporary image: {e}")

    async def clean_chat_link(chat_id: int, interface: str) -> str:
        """Remove the association between a chat and a ChatGPT conversation.
        If no link exists for the current chat, creates a new one."""
        try:
            if await chat_link_store.remove_chatgpt_link(chat_id, None, interface=interface):
                log_debug(f"[clean_chat_link] Chat link removed for chat_id={chat_id}")
                return f"✅ Link for chat_id={chat_id} successfully removed."
            else:
                new_chat_id = f"new_chat_{chat_id}"
                await chat_link_store.store_chatgpt_link(
                    chat_id, new_chat_id, None, interface=interface
                )
                log_debug(f"[clean_chat_link] No link found. Created new link: {new_chat_id}")
                return f"⚠️ No link found for chat_id={chat_id}. Created new link: {new_chat_id}."
        except Exception as e:
            log_error(f"[clean_chat_link] Error while removing or creating the link: {repr(e)}", e)
            return f"❌ Error while removing or creating the link: {e}"

    @staticmethod
    async def handle_clear_chat_link_command(bot, message):
        """Handles the /clear_chat_link command."""
        chat_id = message.chat_id
        text = message.text.strip()

        interface_name = (
            bot.get_interface_id() if hasattr(bot, "get_interface_id") else "generic"
        )

        if text == "/clear_chat_link":
            confirmation_message = (
                f"⚠️ Do you really want to reset the link for this chat (ID: {chat_id})?\n"
                "Reply with 'yes' to confirm or use /cancel to cancel."
            )
            # Use interface_to_llm for system-originated confirmation
            try:
                from core.transport_layer import interface_to_llm
            except Exception:
                interface_to_llm = None

            if interface_to_llm is None:
                await bot.send_message(chat_id=chat_id, text=confirmation_message)
            else:
                await interface_to_llm(bot.send_message, chat_id=chat_id, text=confirmation_message)

            def check_response(response):
                return response.chat_id == chat_id and response.text.lower() in ["yes", "/cancel"]

            try:
                response = await bot.wait_for("message", timeout=60, check=check_response)
                if response.text.lower() == "yes":
                    result = await SeleniumChatGPTLegacyPlugin.clean_chat_link(chat_id, interface_name)
                    # Send result via interface_to_llm
                    if interface_to_llm is None:
                        await bot.send_message(chat_id=chat_id, text=result)
                    else:
                        await interface_to_llm(bot.send_message, chat_id=chat_id, text=result)
                else:
                    if interface_to_llm is None:
                        await bot.send_message(chat_id=chat_id, text="❌ Operation canceled.")
                    else:
                        await interface_to_llm(bot.send_message, chat_id=chat_id, text="❌ Operation canceled.")
            except asyncio.TimeoutError:
                if interface_to_llm is None:
                    await bot.send_message(chat_id=chat_id, text="⏳ Timeout. Operation canceled.")
                else:
                    await interface_to_llm(bot.send_message, chat_id=chat_id, text="⏳ Timeout. Operation canceled.")
        else:
            result = await SeleniumChatGPTLegacyPlugin.clean_chat_link(chat_id, interface_name)
            try:
                from core.transport_layer import interface_to_llm
            except Exception:
                interface_to_llm = None

            if interface_to_llm is None:
                await bot.send_message(chat_id=chat_id, text=result)
            else:
                await interface_to_llm(bot.send_message, chat_id=chat_id, text=result)


    def get_interface_limits(self):
        """Get the limits and capabilities for Selenium ChatGPT interface.
        
        Returns model-specific character limits based on the current model.
        """
        return get_interface_limits()


PLUGIN_CLASS = SeleniumChatGPTLegacyPlugin