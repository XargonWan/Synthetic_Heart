# core/selenium_llm_base.py

"""
Base library for Selenium-based LLM engines.
Provides common functionality for ChatGPT, Gemini, Grok and other browser-based LLMs.
"""

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

try:
    from selenium_stealth import stealth
    SELENIUM_STEALTH_AVAILABLE = True
except ImportError:
    SELENIUM_STEALTH_AVAILABLE = False
import base64
import traceback
from collections import defaultdict
from typing import Optional, Dict, Callable, Any
from pathlib import Path
import subprocess
import textwrap
import mimetypes
import platform
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
from core.action_parser import CORRECTOR_RETRIES
from core.message_chain import RESPONSE_TIMEOUT

# Use global timeout
AWAIT_RESPONSE_TIMEOUT = RESPONSE_TIMEOUT

# Load environment variables
load_dotenv()

# Constants
GRACE_PERIOD_SECONDS = 3
MAX_WAIT_TIMEOUT_SECONDS = 5 * 60  # hard ceiling

# Cache the last response per chat to avoid duplicates
previous_responses: Dict[str, str] = {}
response_cache_lock = threading.Lock()

# Global driver manager for shared browser instance
_shared_driver = None
_shared_driver_lock = threading.Lock()
_shared_driver_ref_count = 0

# Global prompt character limit from the active Selenium LLM engine
# Updated when an engine is loaded or model is switched
_active_selenium_max_prompt_chars = 128000  # Default safe value
_active_selenium_llm_name = "unknown"  # Track which Selenium engine is active


def set_active_selenium_limits(max_prompt_chars: int, llm_name: str = "") -> None:
    """Update the global prompt character limit for the active Selenium LLM engine.
    
    This is called by Selenium engine implementations (ChatGPT, Grok, etc.)
    when they are initialized or when the model is switched.
    
    Parameters
    ----------
    max_prompt_chars : int
        Maximum prompt characters supported by the active LLM engine
    llm_name : str
        Name of the active LLM engine (e.g., "gpt-4o", "grok-beta")
    """
    global _active_selenium_max_prompt_chars, _active_selenium_llm_name
    _active_selenium_max_prompt_chars = max_prompt_chars
    _active_selenium_llm_name = llm_name
    from core.logging_utils import log_info
    log_info(f"[selenium_llm_base] Active Selenium LLM limits updated: {llm_name} max_prompt_chars={max_prompt_chars}")


def get_active_selenium_limits() -> dict:
    """Get the current limits from the active Selenium LLM engine.
    
    Returns
    -------
    dict
        Dictionary with keys: max_prompt_chars, llm_name
    """
    return {
        "max_prompt_chars": _active_selenium_max_prompt_chars,
        "llm_name": _active_selenium_llm_name
    }


class SeleniumLLMBase(AIPluginBase):
    """
    Base class for Selenium-based LLM engines.
    Provides common functionality that can be customized via parameters.
    """

    # Global driver registry to ensure only ONE driver instance across ALL classes
    _global_shared_driver: Optional[webdriver.Remote] = None
    _global_driver_lock = asyncio.Lock()
    _global_ref_count = 0

    @classmethod
    async def _get_global_shared_driver(cls) -> webdriver.Remote:
        """Get the single global shared driver instance."""
        async with cls._global_driver_lock:
            if cls._global_shared_driver is None:
                log_info("[selenium] 🌍 CREATING GLOBAL shared driver instance")
                import traceback
                creation_stack = "".join(traceback.format_stack()[-5:-1])
                log_debug(f"[selenium] Global driver creation from:\n{creation_stack}")
                cls._global_shared_driver = await asyncio.to_thread(cls._create_shared_driver)
                cls._global_ref_count = 1
                log_info(f"[selenium] 🌍 GLOBAL driver CREATED, windows: {len(cls._global_shared_driver.window_handles)}")
            else:
                cls._global_ref_count += 1
                log_debug(f"[selenium] 🌍 Reusing GLOBAL driver (ref count: {cls._global_ref_count})")

                # Always ensure single window for global driver
                await asyncio.to_thread(cls._ensure_single_window, cls._global_shared_driver)

            return cls._global_shared_driver

    @classmethod
    async def _release_global_shared_driver(cls) -> None:
        """Release reference to global driver."""
        async with cls._global_driver_lock:
            cls._global_ref_count -= 1
            log_debug(f"[selenium] 🌍 Released GLOBAL driver reference (ref count: {cls._global_ref_count})")
            if cls._global_ref_count <= 0:
                log_info("[selenium] 🌍 Cleaning up GLOBAL driver")
                if cls._global_shared_driver:
                    try:
                        cls._global_shared_driver.quit()
                    except Exception as e:
                        log_warning(f"[selenium] Error quitting global driver: {e}")
                cls._global_shared_driver = None
                cls._global_ref_count = 0

    @classmethod
    async def _ensure_single_window(cls, driver) -> None:
        """Ensure the driver has only one window open."""
        try:
            window_count = len(driver.window_handles)
            if window_count > 1:
                log_warning(f"[selenium] 🚨 DRIVER HAS {window_count} WINDOWS, CLEANING UP!")
                import traceback
                cleanup_stack = "".join(traceback.format_stack()[-4:-1])
                log_debug(f"[selenium] Window cleanup triggered from:\n{cleanup_stack}")

                # Log current URLs for debugging
                for i, handle in enumerate(driver.window_handles):
                    try:
                        driver.switch_to.window(handle)
                        current_url = driver.current_url
                        log_debug(f"[selenium] Window {i}: {current_url}")
                    except Exception as e:
                        log_debug(f"[selenium] Could not get URL for window {i}: {e}")

                # Keep only the first window
                driver.switch_to.window(driver.window_handles[0])
                # Close all other windows
                for handle in driver.window_handles[1:]:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                        log_debug(f"[selenium] ✅ Closed extra window: {handle}")
                    except Exception as e:
                        log_debug(f"[selenium] ❌ Could not close window {handle}: {e}")
                # Switch back to first window
                driver.switch_to.window(driver.window_handles[0])
                final_count = len(driver.window_handles)
                log_info(f"[selenium] 🧹 Window cleanup complete, now has {final_count} window(s)")
            else:
                log_debug(f"[selenium] ✅ Driver already has correct number of windows: {window_count}")
        except Exception as e:
            log_warning(f"[selenium] ❌ Failed to ensure single window: {e}")

    @classmethod
    async def _get_shared_driver(cls) -> webdriver.Remote:
        """Get or create the shared driver instance (now uses global driver)."""
        return await cls._get_global_shared_driver()

    @classmethod
    async def _release_shared_driver(cls) -> None:
        """Release reference to shared driver (now uses global driver)."""
        await cls._release_global_shared_driver()

    @classmethod
    def _create_shared_driver(cls) -> webdriver.Remote:
        """Create a new shared driver instance."""
        # Use the same logic as _init_driver but without instance-specific config
        import os
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        import undetected_chromedriver as uc

        # Get chromium binary
        chromium_binary = cls._locate_chromium_binary_static()
        if not chromium_binary:
            raise Exception("Chromium binary not found")

        # Get version for compatibility
        version = cls._get_chromium_major_version_static(chromium_binary)

        # Configure options
        options = Options()
        options.binary_location = chromium_binary

        # Essential arguments for shared driver
        essential_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-plugins",
            "--disable-images",
            "--disable-javascript",  # Will be enabled per service
            "--disable-web-security",
            "--allow-running-insecure-content",
            "--disable-features=VizDisplayCompositor",
            "--user-data-dir=/config/.config/chromium-synth",
            "--profile-directory=Default",
            "--remote-debugging-port=0",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]

        # Add headless if configured
        if os.getenv("CHROMIUM_HEADLESS", "0") == "1":
            essential_args.append("--headless")

        for arg in essential_args:
            options.add_argument(arg)

        # Create undetected driver
        try:
            driver = uc.Chrome(
                options=options,
                version_main=version,
                service=Service(executable_path="/usr/bin/chromedriver")
            )

            # Ensure we start with only one window
            if len(driver.window_handles) > 1:
                log_warning(f"[selenium] Shared driver created with {len(driver.window_handles)} windows, cleaning up...")
                # Keep only the first window
                for handle in driver.window_handles[1:]:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception as e:
                        log_debug(f"[selenium] Could not close extra window {handle}: {e}")
                # Switch back to first window
                driver.switch_to.window(driver.window_handles[0])

            log_info(f"[selenium] Shared driver created with {len(driver.window_handles)} window(s)")
            return driver
        except Exception as e:
            log_error(f"[selenium] Failed to create shared driver: {e}")
            raise

    @classmethod
    def _locate_chromium_binary_static(cls) -> Optional[str]:
        """Static version of _locate_chromium_binary for shared driver creation."""
        possible_paths = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ]

        for path in possible_paths:
            if os.path.exists(path):
                return path
        return None

    @classmethod
    def _get_chromium_major_version_static(cls, binary: str) -> Optional[int]:
        """Static version of _get_chromium_major_version for shared driver creation."""
        try:
            import subprocess
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                version_str = result.stdout.strip()
                # Extract version number (e.g., "Chromium 120.0.6099.109" -> 120)
                import re
                match = re.search(r'(\d+)\.', version_str)
                if match:
                    return int(match.group(1))
        except Exception as e:
            log_warning(f"[selenium] Could not get chromium version: {e}")
        return None

    def __init__(self, notify_fn=None, config=None):
        """
        Initialize the Selenium LLM base.

        Args:
            notify_fn: Notification function
            config: Configuration dictionary with engine-specific parameters
        """
        super().__init__()
        if notify_fn:
            self.set_notify_fn(notify_fn)

        # Default configuration - can be overridden by subclasses
        self.config = config or {}
        self.service_url = self.config.get('service_url', '')
        self.model_limits = self.config.get('model_limits', {})
        self.model_var = self.config.get('model_var', '')
        self.link_column = self.config.get('link_column', '')
        self.component_name = self.config.get('component_name', 'selenium_llm')

        # Driver and state
        self.driver: Optional[webdriver.Remote] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._driver_lock = asyncio.Lock()
        self._initialized = False

        # Login detection configuration - to be set by subclasses
        # Format: list of tuples (By, selector) for detecting login buttons
        self.login_detection_selectors: list = []
        # Fallback common login button selectors used if no specific ones provided
        self.common_login_selectors = [
            (By.CSS_SELECTOR, "button:contains('Log in')"),
            (By.CSS_SELECTOR, "button:contains('Sign in')"),
            (By.CSS_SELECTOR, "button:contains('Login')"),
            (By.CSS_SELECTOR, "a:contains('Log in')"),
            (By.CSS_SELECTOR, "a:contains('Sign in')"),
            (By.CSS_SELECTOR, "a:contains('Login')"),
            (By.CSS_SELECTOR, "[data-testid*='login']"),
            (By.CSS_SELECTOR, "[data-testid*='signin']"),
            (By.CLASS_NAME, "login"),
            (By.CLASS_NAME, "signin"),
        ]

        # Initialize components
        self._init_components()

    def __del__(self):
        """Cleanup when instance is destroyed."""
        try:
            # Release shared driver reference
            if hasattr(self, '_shared_driver_lock') and asyncio.iscoroutinefunction(self._release_shared_driver):
                # Can't call async method from __del__, so we schedule it
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if not loop.is_closed():
                        loop.create_task(self._release_shared_driver())
                except RuntimeError:
                    # No event loop, can't release async
                    pass
        except Exception as e:
            # Don't raise exceptions in __del__
            pass

    def _init_components(self):
        """Initialize common components."""
        # Import CHROMIUM_HEADLESS from environment variable directly
        import os
        self.CHROMIUM_HEADLESS = os.getenv("CHROMIUM_HEADLESS", "0") == "1"

        # Also register with config registry for UI visibility
        from core.config_manager import config_registry
        self.CHROMIUM_HEADLESS_VAR = config_registry.get_var(
            "CHROMIUM_HEADLESS",
            int(os.getenv("CHROMIUM_HEADLESS", "0")),
            label="Chromium Headless Mode",
            description="Set to 1 for headless mode (no browser window), 0 for non-headless mode (visible browser window)",
            value_type=int,
            group="llm",
            component="selenium",
            advanced=True,
        )

        # Max retries for driver initialization
        self.MAX_RETRIES_VAR = config_registry.get_var(
            "SELENIUM_MAX_RETRIES",
            int(os.getenv("SELENIUM_MAX_RETRIES", "3")),
            label="Selenium Max Retries",
            description="Maximum number of retries for Selenium driver initialization",
            value_type=int,
            group="llm",
            component="selenium",
            advanced=True,
        )

        # Queue for sequential processing
        self._prompt_queue: asyncio.Queue = asyncio.Queue()
        self._queue_lock = asyncio.Lock()
        self._queue_worker: asyncio.Task | None = None

    # === DRIVER MANAGEMENT ===

    def _locate_chromium_binary(self) -> Optional[str]:
        """Locate Chromium binary in common locations."""
        possible_paths = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ]

        for path in possible_paths:
            if os.path.exists(path):
                log_debug(f"[selenium] Found Chromium at: {path}")
                return path

        log_warning("[selenium] Chromium binary not found in common locations")
        return None

    def _locate_chromium_binary(self) -> Optional[str]:
        """Locate Chromium binary in common locations."""
        possible_paths = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ]

        for path in possible_paths:
            if os.path.exists(path):
                log_debug(f"[selenium] Found Chromium binary: {path}")
                return path

        log_warning("[selenium] Chromium binary not found in common locations")
        return None

    def _get_chromium_major_version(self, binary: str) -> Optional[int]:
        """Return the major version of the given Chromium binary."""
        try:
            import subprocess
            import re
            output = subprocess.check_output([binary, "--version"], text=True, stderr=subprocess.STDOUT)
            match = re.search(r"(\d+)\.", output)
            if match:
                version = int(match.group(1))
                log_debug(f"[selenium] Detected Chromium major version: {version}")
                return version
        except Exception as e:
            log_warning(f"[selenium] Unable to determine Chromium version: {e}")
        return None



    def _init_driver(self) -> webdriver.Remote:
        """Initialize the Chrome driver with common settings."""
        try:
            log_info("[selenium] Initializing Chrome driver...")

            # Clean up any existing remnants
            self._cleanup_chromium_remnants()

            # Initialize undetected-chromedriver with retry logic
            log_debug("[selenium] Creating undetected Chrome driver...")
            log_debug(f"[selenium] undetected-chromedriver version: {uc.__version__}")
            
            max_retries = self.MAX_RETRIES_VAR.value
            retry_delay = 2
            
            # Detect Chromium version for optimal undetected-chromedriver compatibility
            chromium_binary = self._locate_chromium_binary() or "/usr/bin/chromium"
            chromium_major = self._get_chromium_major_version(chromium_binary)
            if chromium_major:
                log_debug(f"[selenium] Detected Chromium major version {chromium_major}")
            else:
                log_warning("[selenium] Could not detect Chromium version; using default driver")
            
            for attempt in range(max_retries):
                try:
                    log_debug(f"[selenium] Driver initialization attempt {attempt + 1}/{max_retries}")
                    
                    # Use uc.ChromeOptions() for better Cloudflare bypass compatibility
                    options = uc.ChromeOptions()
                    
                    # Essential Chromium arguments for Cloudflare bypass (CRITICAL)
                    essential_args = [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-setuid-sandbox",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-web-security",
                        # Removed: "--start-maximized",  # This can cause new windows to open
                        "--no-first-run",
                        "--disable-default-apps",
                        "--disable-popup-blocking",
                        "--disable-infobars",
                        "--disable-background-timer-throttling",
                        "--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--memory-pressure-off",
                        "--disable-features=VizDisplayCompositor,VizHitTestSurfaceLayer",
                        "--enable-logging",
                        "--remote-debugging-port=0",
                        "--disable-background-mode",
                        "--disable-default-browser-check",
                        "--disable-hang-monitor",
                        "--disable-prompt-on-repost",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-default-browser-check",
                        "--safebrowsing-disable-auto-update",
                        # Removed: "--disable-client-side-phishing-detection" (too suspicious)
                        # Removed: "--disable-blink-features=AutomationControlled" (too suspicious)
                    ]
                    
                    # Add all essential arguments
                    for arg in essential_args:
                        options.add_argument(arg)
                    
                    # Set user agent for better compatibility
                    user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    options.add_argument(f'--user-agent="{user_agent}"')
                    options.add_argument("--window-size=1280,720")

                    if self.CHROMIUM_HEADLESS:
                        options.add_argument("--headless")
                        log_info("[selenium] Running in headless mode")
                    else:
                        log_info("[selenium] Running in non-headless mode")

                    # Use EXACTLY the same profile directory as desktop entry
                    profile_dir = "/config/.config/chromium-synth"
                    os.makedirs(profile_dir, exist_ok=True)
                    
                    # Clean up lock files
                    for lock_pattern in ["SingletonLock", "SingletonCookie", ".org.chromium.Chromium.*"]:
                        for lock_file in glob.glob(os.path.join(profile_dir, lock_pattern)):
                            try:
                                os.remove(lock_file)
                            except:
                                pass
                    
                    options.add_argument(f'--user-data-dir="{profile_dir}"')
                    log_debug(f"[selenium] Using shared profile directory: {profile_dir}")

                    # Clear undetected-chromedriver cache before creating driver (CRITICAL for stability)
                    import tempfile
                    import shutil
                    uc_cache_dir = os.path.join(tempfile.gettempdir(), 'undetected_chromedriver')
                    if os.path.exists(uc_cache_dir):
                        shutil.rmtree(uc_cache_dir, ignore_errors=True)
                        log_debug("[selenium] Cleared undetected-chromedriver cache")

                    # Create driver with undetected-chromedriver specific parameters (CRITICAL for Cloudflare bypass)
                    driver = uc.Chrome(
                        options=options,
                        headless=bool(self.CHROMIUM_HEADLESS),
                        use_subprocess=True,
                        version_main=chromium_major,
                        suppress_welcome=True,
                        browser_executable_path=chromium_binary,
                        user_data_dir=profile_dir
                    )
                    
                    # Apply stealth settings (removed to match legacy behavior)
                    # if SELENIUM_STEALTH_AVAILABLE:
                    #     try:
                    #         stealth(driver,
                    #               languages=["en-US", "en"],
                    #               vendor="Google Inc.",
                    #               platform="Win32",
                    #               webgl_vendor="Intel Inc.",
                    #               renderer="Intel Iris OpenGL Engine",
                    #               fix_hairline=True)
                    #         log_debug("[selenium] Applied selenium-stealth")
                    #     except Exception as e:
                    #         log_warning(f"[selenium] Failed to apply selenium-stealth: {e}")
                    
                    # Remove webdriver property (basic only, like legacy)
                    try:
                        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                        log_debug("[selenium] Applied webdriver property removal")
                    except Exception as e:
                        log_warning(f"[selenium] Failed to remove webdriver property: {e}")
                    
                    # No additional anti-detection measures to match legacy behavior
                    
                    # Verify driver is working
                    if driver and hasattr(driver, 'current_url'):
                        log_debug("[selenium] Driver created successfully")
                        break
                    else:
                        raise Exception("Driver object is invalid")
                        
                except Exception as init_error:
                    log_warning(f"[selenium] Driver initialization attempt {attempt + 1} failed: {init_error}")
                    if attempt < max_retries - 1:
                        log_debug(f"[selenium] Waiting {retry_delay}s before retry...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        log_error(f"[selenium] All {max_retries} driver initialization attempts failed")
                        raise init_error
            
            # Apply timeouts
            self._apply_driver_timeouts(driver)
            log_info("[selenium] Chrome driver initialized successfully")
            return driver

        except Exception as e:
            log_error(f"[selenium] Failed to initialize driver: {e}", e)
            raise
    def _apply_driver_timeouts(self, driver: webdriver.Remote) -> None:
        """Apply common timeouts to the driver."""
        try:
            # Note: command_executor.set_timeout() is not supported by undetected-chromedriver
            # Only apply timeouts that are supported by local WebDriver instances
            driver.set_page_load_timeout(AWAIT_RESPONSE_TIMEOUT)
            driver.set_script_timeout(AWAIT_RESPONSE_TIMEOUT)
            log_debug(f"[selenium] Driver timeouts set to {AWAIT_RESPONSE_TIMEOUT}s")
        except Exception as e:
            log_warning(f"[selenium] Could not apply driver timeouts: {e}")

    def _cleanup_chromium_remnants(self) -> None:
        """Clean up Chromium lock files and processes."""
        try:
            log_debug("[selenium] Cleaning up Chromium remnants...")
            
            # Kill any existing chromium processes more aggressively
            try:
                # Kill processes by name - be more aggressive
                subprocess.run(["pkill", "-9", "-f", "chromium"], check=False, capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "chrome"], check=False, capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "chromedriver"], check=False, capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "undetected_chromedriver"], check=False, capture_output=True)
                
                # Wait longer for processes to terminate
                time.sleep(5)
                log_debug("[selenium] Chromium processes killed")
            except Exception as e:
                log_warning(f"[selenium] Error killing Chromium processes: {e}")

            # Clean up lock files
            temp_dir = tempfile.gettempdir()
            lock_patterns = [
                os.path.join(temp_dir, ".org.chromium.Chromium.*"),
                os.path.join(temp_dir, "selenium_*_profile", "SingletonLock"),
                os.path.join(temp_dir, "selenium_*_profile", "SingletonCookie"),
                os.path.join(temp_dir, "selenium_*_profile", ".org.chromium.Chromium.*"),
            ]

            for pattern in lock_patterns:
                for lock_file in glob.glob(pattern):
                    try:
                        os.remove(lock_file)
                        log_debug(f"[selenium] Removed lock file: {lock_file}")
                    except Exception as e:
                        log_debug(f"[selenium] Could not remove lock file {lock_file}: {e}")
                        
            # Also clean up profile directory lock files
            profile_dir = "/config/.config/chromium-synth"
            if os.path.exists(profile_dir):
                for lock_pattern in ["SingletonLock", "SingletonCookie", ".org.chromium.Chromium.*"]:
                    for lock_file in glob.glob(os.path.join(profile_dir, lock_pattern)):
                        try:
                            os.remove(lock_file)
                            log_debug(f"[selenium] Removed profile lock file: {lock_file}")
                        except Exception as e:
                            log_debug(f"[selenium] Could not remove profile lock file {lock_file}: {e}")
            
            # Additional wait after cleanup
            time.sleep(2)
            log_debug("[selenium] Chromium cleanup completed")
            
        except Exception as e:
            log_warning(f"[selenium] Error during Chromium cleanup: {e}")

    # === UTILITY FUNCTIONS ===

    def get_previous_response(self, chat_id: str) -> str:
        """Return the cached response for the given chat."""
        with response_cache_lock:
            return previous_responses.get(chat_id, "")

    def update_previous_response(self, chat_id: str, new_text: str) -> None:
        """Store new_text for chat_id inside the cache."""
        with response_cache_lock:
            previous_responses[chat_id] = new_text

    def has_response_changed(self, chat_id: str, new_text: str) -> bool:
        """Return True if new_text is different from the cached value."""
        with response_cache_lock:
            old = previous_responses.get(chat_id)
        return old != new_text

    def strip_non_bmp(self, text: str) -> str:
        """Return text with characters above the BMP removed."""
        return "".join(ch for ch in text if ord(ch) <= 0xFFFF)

    # === IMAGE HANDLING ===

    async def _download_telegram_image(self, bot, file_id: str, temp_dir: str) -> Optional[str]:
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

    def _paste_image_to_service(self, driver, image_path: str, image_selectors: list) -> bool:
        """Paste an image to the LLM service using various methods."""
        try:
            # Find the input area
            textarea = self._locate_prompt_area(driver, timeout=10)

            # Click on the textarea to focus it
            textarea.click()
            time.sleep(0.5)

            # Method 1: Try to find and use the image upload button
            try:
                upload_element = None
                for selector in image_selectors:
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
                        log_debug("[selenium] Clicked image upload button")

            except Exception as e:
                log_debug(f"[selenium] Image upload button method failed: {e}")

            # Method 2: Convert image to base64 and inject via JavaScript
            try:
                with open(image_path, 'rb') as f:
                    image_data = f.read()

                # Get image format from file extension
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

                        // Find the actual file input in the service interface
                        var inputs = document.querySelectorAll('input[type="file"]');
                        if (inputs.length > 0) {{
                            inputs[0].files = dt.files;
                            inputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));
                            return true;
                        }}

                        return false;
                    }})
                    .catch(err => console.error('Image upload failed:', err));
                """

                result = driver.execute_script(js_script)
                if result:
                    log_info("[selenium] Image injected via JavaScript")
                    time.sleep(2)
                    return True

            except Exception as e:
                log_warning(f"[selenium] JavaScript injection method failed: {e}")

            # Method 3: Fallback to clipboard method
            return self._paste_image_via_clipboard(driver, textarea, image_path)

        except Exception as e:
            log_error(f"[selenium] Failed to paste image: {e}")
            return False

    def _paste_image_via_clipboard(self, driver, textarea, image_path: str) -> bool:
        """Paste image via clipboard (system-dependent)."""
        try:
            system = platform.system().lower()

            if system == "linux":
                try:
                    subprocess.run([
                        "xclip", "-selection", "clipboard", "-t", "image/png", "-i", image_path
                    ], check=True, capture_output=True)
                    log_debug(f"[selenium] Copied image to clipboard using xclip: {image_path}")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    log_warning("[selenium] xclip not available")
                    return False

            elif system == "darwin":  # macOS
                try:
                    subprocess.run([
                        "osascript", "-e", f'set the clipboard to (read file POSIX file "{image_path}" as JPEG picture)'
                    ], check=True, capture_output=True)
                    log_debug(f"[selenium] Copied image to clipboard using osascript: {image_path}")
                except subprocess.CalledProcessError:
                    log_warning("[selenium] osascript failed")
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
                    log_warning("[selenium] PowerShell failed")
                    return False

            # Paste the image using Ctrl+V
            textarea.send_keys(Keys.CONTROL, 'v')
            time.sleep(2)

            # Check if the image was pasted successfully
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "[data-testid*='image']") or
                             d.find_elements(By.CSS_SELECTOR, "img") or
                             d.find_elements(By.CSS_SELECTOR, "[title*='image']") or
                             d.find_elements(By.CSS_SELECTOR, ".image-preview") or
                             d.find_elements(By.CSS_SELECTOR, "[data-testid*='attachment']")
                )
                log_info("[selenium] Image successfully pasted")
                return True
            except TimeoutException:
                log_warning("[selenium] Could not verify if image was pasted")
                return True  # Still return True as the paste was attempted

        except Exception as e:
            log_error(f"[selenium] Failed to paste image via clipboard: {e}")
            return False

    # === TEXT INPUT AND RESPONSE HANDLING ===

    def _send_text_to_textarea(self, driver, textarea, text: str) -> None:
        """Inject text into the LLM prompt area via JavaScript."""
        try:
            clean_text = self.strip_non_bmp(text)
            log_debug(f"[DEBUG] Length before sending: {len(clean_text)}")

            # Service-specific textarea handling
            script = self._get_textarea_injection_script(textarea, clean_text)
            driver.execute_script(script, textarea, clean_text)
            log_debug("[DEBUG] JavaScript injection completed successfully")

            # Verify content
            actual = driver.execute_script(
                self._get_textarea_content_script(),
                textarea
            ) or ""
            log_debug(f"[DEBUG] Length actually present: {len(actual)}")

            if abs(len(clean_text) - len(actual)) > 5:
                log_warning(
                    f"[selenium] textarea mismatch: expected {len(clean_text)} chars, found {len(actual)}"
                )

        except Exception as e:
            log_error(f"[selenium] Critical error in _send_text_to_textarea: {e}")
            raise

    def _get_textarea_injection_script(self, textarea, text: str) -> str:
        """Return the JavaScript for injecting text into textarea (can be overridden)."""
        return (
            "var editor = arguments[0].querySelector('div.ql-editor') || arguments[0];"
            "editor.focus();"
            "editor.textContent = arguments[1];"
            "editor.dispatchEvent(new Event('input', {bubbles: true}));"
        )

    def _get_textarea_content_script(self) -> str:
        """Return the JavaScript for getting textarea content (can be overridden)."""
        return (
            "return (arguments[0].querySelector('div.ql-editor') || arguments[0]).textContent;"
        )

    def paste_and_send(self, textarea, prompt_text: str) -> None:
        """Insert prompt_text into textarea ensuring full content is present."""
        try:
            driver = textarea._parent
            clean = self.strip_non_bmp(prompt_text)

            # Try JavaScript injection first
            try:
                self._send_text_to_textarea(driver, textarea, clean)
                actual = driver.execute_script(
                    self._get_textarea_content_script(),
                    textarea
                ) or ""
                if len(actual) >= len(clean) * 0.9:
                    log_debug(f"[selenium] JS injection successful: {len(actual)}/{len(clean)} chars")
                    return
            except StaleElementReferenceException:
                log_warning("[selenium] Textarea became stale during JS paste, retrying")
            except Exception as e:
                log_warning(f"[selenium] JS injection failed: {e}, falling back to send_keys")

        except Exception as critical_error:
            log_error(f"[selenium] Critical error in paste_and_send: {critical_error}")
            raise

        log_warning("[selenium] JS paste failed, falling back to send_keys")
        self._paste_via_send_keys(driver, textarea, clean)

    def _paste_via_send_keys(self, driver, textarea, text: str) -> None:
        """Fallback method using send_keys with chunking."""
        chunk_size = 1000
        final_val = ""
        for attempt in range(3):
            if attempt:
                log_warning(f"[selenium] send_keys retry {attempt}/3")
            try:
                textarea.clear()
                time.sleep(0.1)

                accumulated_text = ""
                chunks_sent = 0
                total_chunks = len(list(textwrap.wrap(text, chunk_size)))

                for idx, chunk in enumerate(textwrap.wrap(text, chunk_size), start=1):
                    log_debug(f"[selenium] sending chunk {idx}/{total_chunks} len={len(chunk)}")
                    textarea.send_keys(chunk)
                    accumulated_text += chunk
                    chunks_sent = idx
                    time.sleep(0.05)

                    if idx % 5 == 0:
                        current_val = textarea.get_attribute("value") or ""
                        if len(current_val) < len(accumulated_text) * 0.5:
                            log_warning(f"[selenium] Content mismatch detected at chunk {idx}")
                            break

                if chunks_sent == total_chunks:
                    log_debug(f"[selenium] All {chunks_sent} chunks sent successfully")
                    return

                final_val = textarea.get_attribute("value") or ""
                log_debug(f"[selenium] value after send_keys: {len(final_val)} chars")

                if len(final_val) >= len(text) * 0.9:
                    log_debug(f"[selenium] Content successfully inserted ({len(final_val)}/{len(text)} chars)")
                    return
                elif len(final_val) == 0:
                    chunk_size = max(100, chunk_size // 3)
                else:
                    log_warning(f"[selenium] Partial content inserted ({len(final_val)}/{len(text)} chars)")

            except StaleElementReferenceException as e:
                log_warning(f"[selenium] Stale element on attempt {attempt}: {e}")
                try:
                    textarea = self._locate_prompt_area(driver, timeout=0)
                except Exception:
                    break
            except Exception as e:
                log_warning(f"[selenium] send_keys attempt {attempt} failed: {e}")

        if len(final_val) < len(text) * 0.5:
            log_warning("[selenium] Attempting emergency fallback")
            try:
                textarea.clear()
                for char in text[:500]:
                    textarea.send_keys(char)
                    time.sleep(0.01)
                final_val = textarea.get_attribute("value") or ""
                log_warning(f"[selenium] Emergency fallback result: {len(final_val)} chars")
            except Exception as e:
                log_error(f"[selenium] Emergency fallback failed: {e}")

        log_warning(
            f"[selenium] Failed to insert full prompt: expected {len(text)} chars, got {len(final_val)}"
        )

    # === RESPONSE WAITING ===

    def wait_until_response_stabilizes(self, driver, max_total_wait: int = AWAIT_RESPONSE_TIMEOUT,
                                      no_change_grace: float = 3.5) -> str:
        """Return the last response text once its length stops growing."""
        # Convert max_total_wait to int in case it's a ConfigVar
        max_total_wait = int(max_total_wait)
        
        start = time.time()
        last_len = -1
        last_change = start
        final_text = ""

        while True:
            if time.time() - start >= max_total_wait:
                log_warning("[selenium] Timeout while waiting for response")
                return final_text

            text = self._extract_response_text(driver)
            current_len = len(text)
            changed = current_len != last_len

            if changed:
                log_debug(f"[DEBUG] len={current_len} changed={changed}")
            else:
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

    def _get_response_selectors(self) -> list:
        """Get the CSS selectors for extracting response text.
        
        Subclasses should override this to provide service-specific selectors.
        Returns a list of CSS selector strings (tried in order).
        """
        # Default generic selectors - subclasses should override with specific ones
        return [
            "div.markdown",
            "[data-message-author-role='model']",
            "div.model-response-text",
            ".response-content"
        ]

    def _extract_response_text(self, driver) -> str:
        """Extract response text from the page using service-specific selectors.
        
        This standardized method:
        1. Gets selectors from _get_response_selectors() (can be overridden by subclass)
        2. Tries each selector in order
        3. Returns text from the LAST matching element (most recent response)
        4. Handles both .text and .textContent attributes
        5. Returns empty string if no response found
        
        This approach works for ChatGPT, Grok, Gemini, etc. - just override
        _get_response_selectors() in subclass to provide the right selectors.
        """
        try:
            selectors = self._get_response_selectors()
            
            for selector in selectors:
                try:
                    log_debug(f"[selenium] Trying response selector: {selector}")
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    
                    if elements:
                        # Get the LAST element (most recent response in chat)
                        last_element = elements[-1]
                        
                        # Try .text first (Selenium's smart getter)
                        text = last_element.text or ""
                        
                        # Fallback to textContent if .text is empty
                        if not text:
                            text = last_element.get_attribute("textContent") or ""
                        
                        # Clean up whitespace
                        text = text.strip()
                        
                        if text:
                            log_debug(f"[selenium] Found response with selector '{selector}': {len(text)} chars")
                            return text
                            
                except Exception as e:
                    log_debug(f"[selenium] Selector '{selector}' failed: {e}")
                    continue
            
            log_debug("[selenium] No response found with any selector")
            return ""
            
        except Exception as e:
            log_warning(f"[selenium] Error extracting response text: {e}")
            return ""

    def wait_for_response_completion(self, driver, timeout: int = AWAIT_RESPONSE_TIMEOUT) -> bool:
        """Wait until the current response finishes streaming."""
        # Convert timeout to int in case it's a ConfigVar
        timeout = int(timeout)
        
        start_time = time.time()
        end_time = start_time + timeout

        try:
            driver.command_executor.set_timeout(timeout)
        except Exception as e:
            log_warning(f"[selenium] Could not apply command timeout: {e}")

        if not self._has_visible_stop_button(driver):
            log_debug("[selenium] No visible stop button found, assuming idle")
            return True

        log_debug(f"[selenium] Visible stop button found, waiting up to {timeout} seconds")

        last_report = 0
        while time.time() < end_time:
            try:
                if not self._has_visible_stop_button(driver):
                    elapsed = int(time.time() - start_time)
                    log_debug(
                        f"[selenium] Stop button disappeared after {elapsed} seconds, response completed"
                    )
                    return True
            except (ReadTimeoutError, WebDriverException) as e:
                log_warning(f"[selenium] Polling error: {e}")
            time.sleep(0.5)
            elapsed = int(time.time() - start_time)
            if elapsed // 10 > last_report // 10:
                log_debug(f"[selenium] {elapsed} seconds passed, stop button still visible")
                last_report = elapsed

        log_warning("[selenium] Timeout waiting for response completion")
        return False

    def _has_visible_stop_button(self, driver) -> bool:
        """Return True when the service renders a visible stop button."""
        selectors = [
            "button.send-button.stop",
            "button[data-testid='stop-button']",
            "button[aria-label='Stop']",
        ]
        for selector in selectors:
            try:
                candidates = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for candidate in candidates:
                try:
                    if not candidate.is_displayed():
                        continue
                    disabled_attr = candidate.get_attribute("disabled")
                    if disabled_attr and disabled_attr.lower() not in ("false", "0"):
                        continue
                    aria_disabled = candidate.get_attribute("aria-disabled")
                    if aria_disabled and aria_disabled.lower() not in ("false", "0", ""):
                        continue
                    return True
                except StaleElementReferenceException:
                    continue
                except WebDriverException:
                    continue
        return False

    # === RESPONSE CHOICE HANDLING ===

    def _get_response_choice_selectors(self) -> list:
        """Get CSS selectors for response choice buttons (when LLM offers multiple options).
        
        Subclasses should override this to provide service-specific selectors for choice buttons.
        Returns a list of CSS selector strings (tried in order).
        Default returns empty list (no choice handling).
        """
        return []

    def _handle_response_choice(self, driver) -> bool:
        """Handle response choice selection (when LLM offers multiple response options).
        
        Some LLMs like ChatGPT offer users to choose between multiple response versions.
        This method detects and automatically selects the first option.
        
        Args:
            driver: Selenium WebDriver instance
            
        Returns:
            bool: True if a choice was found and selected, False otherwise
        """
        from selenium.webdriver.common.by import By
        from core.logging_utils import log_debug, log_warning
        
        selectors = self._get_response_choice_selectors()
        if not selectors:
            log_debug("[selenium] No response choice selectors configured")
            return False
        
        log_debug(f"[selenium] Checking for response choice buttons with {len(selectors)} selector(s)")
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    log_debug(f"[selenium] Found {len(elements)} choice button(s) with selector: {selector}")
                    # Click the first button (first response option)
                    try:
                        first_button = elements[0]
                        if first_button.is_displayed():
                            log_debug("[selenium] Clicking first response choice button")
                            first_button.click()
                            return True
                        else:
                            log_debug("[selenium] First choice button not visible, skipping")
                    except Exception as click_err:
                        log_warning(f"[selenium] Failed to click choice button: {click_err}")
            except Exception as e:
                log_debug(f"[selenium] Error checking choice selector '{selector}': {e}")
                continue
        
        log_debug("[selenium] No response choice buttons found")
        return False

    # === QUEUE MANAGEMENT ===

    async def _queue_worker_loop(self) -> None:
        """Background worker that processes queued prompts sequentially."""
        while not self._prompt_queue.empty():
            textarea, text = await self._prompt_queue.get()
            log_debug("[selenium] Dequeued prompt")
            async with self._queue_lock:
                log_debug("[selenium] Send lock acquired")
                await asyncio.to_thread(self._send_prompt_with_confirmation, textarea, text)
                log_debug("[selenium] Prompt completed")
            self._prompt_queue.task_done()
            log_debug("[selenium] Task done")

    async def enqueue_prompt(self, textarea, prompt_text: str) -> None:
        """Enqueue prompt_text for sequential sending."""
        await self._prompt_queue.put((textarea, prompt_text))
        log_debug(f"[selenium] Prompt enqueued (size={self._prompt_queue.qsize()})")
        # Ensure we always create a fresh coroutine object for the queue worker.
        try:
            if self._queue_worker is None or self._queue_worker.done():
                self._queue_worker = asyncio.create_task(self._queue_worker_loop())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._queue_worker_loop()))

    # === ABSTRACT METHODS (to be implemented by subclasses) ===

    def _locate_prompt_area(self, driver, timeout: int = 10):
        """Locate the prompt area (to be overridden by subclasses)."""
        raise NotImplementedError

    def _navigate_to_service_url(self, driver, service_url: str) -> None:
        """Navigate to service URL safely without opening new tabs/windows."""
        try:
            current_url = driver.current_url
            log_debug(f"[selenium] Current URL: {current_url}, Target URL: {service_url}")

            # Check if we're already on the correct domain
            if current_url and (service_url in current_url or current_url.startswith(service_url)):
                log_debug(f"[selenium] Already on {service_url}, no navigation needed")
                return

            # Ensure we have only one window before navigating
            if len(driver.window_handles) > 1:
                log_warning(f"[selenium] ⚠️ Multiple windows detected ({len(driver.window_handles)}) before navigation, cleaning up...")
                # Keep only the first window
                driver.switch_to.window(driver.window_handles[0])
                # Close all other windows
                for handle in driver.window_handles[1:]:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                    except Exception as e:
                        log_debug(f"[selenium] Could not close window {handle}: {e}")
                # Switch back to first window
                driver.switch_to.window(driver.window_handles[0])

            # Navigate to the service URL in the current window
            log_debug(f"[selenium] Navigating to {service_url}")
            driver.get(service_url)

        except Exception as e:
            log_error(f"[selenium] Failed to navigate to {service_url}: {e}")
            raise

    def _send_prompt_with_confirmation(self, textarea, prompt_text: str) -> None:
        """Send prompt and wait for response (to be overridden by subclasses)."""
        raise NotImplementedError

    def get_supported_models(self):
        """Get supported models (to be overridden by subclasses)."""
        return []

    def get_current_model(self):
        """Get current model (to be overridden by subclasses)."""
        return None

    def get_interface_limits(self):
        """Get interface limits (to be overridden by subclasses)."""
        return {
            "max_prompt_chars": 1000,
            "max_response_chars": 1000,
            "supports_images": False,
            "supports_functions": False,
            "model_name": "default"
        }

    def is_user_logged_in(self) -> bool:
        """
        Centralized login state detection.
        
        Checks if user is logged in by looking for login/signin buttons.
        Uses selectors provided by subclass or common fallbacks.
        
        Returns True if user is LOGGED IN, False if login button found (NOT logged in).
        """
        # If driver is not initialized, assume not logged in
        if self.driver is None:
            log_debug(f"[{self.component_name}] Driver not initialized, assuming not logged in")
            return False
        
        try:
            # Combine specific selectors with common fallbacks
            all_selectors = self.login_detection_selectors + self.common_login_selectors
            
            # Check for login buttons - if we find ANY, user is NOT logged in
            for by, selector in all_selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                    if elements:
                        log_debug(f"[{self.component_name}] Found login button with selector {selector}, user NOT logged in")
                        return False
                except Exception:
                    # This selector doesn't exist, try next one
                    pass
            
            # If we didn't find any login button, assume user IS logged in
            log_debug(f"[{self.component_name}] No login buttons found, user appears to be logged in")
            return True
            
        except Exception as e:
            log_warning(f"[{self.component_name}] Error checking login status: {e}, assuming not logged in")
            return False

    # === COMMON LIFECYCLE METHODS ===

    async def start(self):
        """Start the LLM engine (lazy initialization - driver created only when needed)."""
        # Don't create driver here - wait for actual usage in generate_response
        log_info(f"[selenium] {self.component_name} initialized (driver will be created on first use)")

        # Mark as ready without actually creating the driver
        self._initialized = True

    async def stop(self):
        """Stop the LLM engine."""
        try:
            # Cancel worker task
            if self._worker_task and not self._worker_task.done():
                self._worker_task.cancel()
                try:
                    await asyncio.wait_for(self._worker_task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass

            # Release shared driver reference instead of closing directly
            await self._release_shared_driver()
            self.driver = None

            log_info(f"[selenium] {self.component_name} stopped")

        except Exception as e:
            log_error(f"[selenium] Error stopping {self.component_name}: {e}", e)

    def cleanup(self):
        """Clean up resources."""
        try:
            # Release shared driver reference instead of closing directly
            # Note: This is synchronous, so we can't await _release_shared_driver
            # The __del__ method will handle cleanup when the instance is destroyed
            self.driver = None

            # Note: We no longer clean up the shared profile directory to maintain login sessions
            # The profile is now stored in ~/.config/chromium-synth and should persist between runs

        except Exception as e:
            log_error(f"[selenium] Error in cleanup: {e}")

    # === COMMON NOTIFICATION ===

    def _notify_gui(self, message: str = ""):
        """Send a notification with the VNC URL."""
        url = self._build_vnc_url()
        text = f"{message} {url}".strip()
        log_debug(f"[selenium] Sending VNC notification: {text}")
        self._safe_notify(text)

    def _build_vnc_url(self) -> str:
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

    def _safe_notify(self, text: str) -> None:
        """Send notification with length limits."""
        for i in range(0, len(text), 4000):
            chunk = text[i : i + 4000]
            log_debug(f"[selenium] Notifying chunk length {len(chunk)}")
            try:
                from core.notifier import notify_trainer
                notify_trainer(chunk)
            except Exception as e:
                log_error(f"[selenium] notify_trainer failed: {repr(e)}", e)

    # === AI PLUGIN BASE INTERFACE IMPLEMENTATION ===

    async def handle_incoming_message(self, bot, message, prompt):
        """Process a message using a pre-built prompt.
        
        This method:
        1. Converts the prompt to text
        2. Sends to ChatGPT via generate_response()
        3. Returns the response to the caller (plugin_instance)
        
        The response will then be passed to message_chain.handle_incoming_message()
        for validation, correction, and action execution. This ensures all LLM responses
        are properly validated through the central message chain, not sent directly to interfaces.
        """
        try:
            # Convert prompt to text format for LLM
            if isinstance(prompt, list):
                # Convert message list to text
                prompt_text = ""
                for msg in prompt:
                    if isinstance(msg, dict) and "content" in msg:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        prompt_text += f"{role}: {content}\n"
                    else:
                        prompt_text += str(msg) + "\n"
            else:
                prompt_text = str(prompt)

            # Send to LLM and get response
            # The response will be returned to plugin_instance which passes it to message_chain
            response = await self.generate_response([{"role": "user", "content": prompt_text}])
            
            # Simply return the response - don't send it directly!
            # plugin_instance will handle passing it to message_chain for proper validation
            log_debug(f"[selenium] Response generated ({len(response) if response else 0} chars), returning to plugin_instance for message chain processing")
            return response

        except Exception as e:
            log_error(f"[selenium] Failed to handle incoming message: {e}", e)
            # Return error message instead of sending directly
            error_msg = f"❌ Error processing message: {e}"
            return error_msg

    async def generate_response(self, messages):
        """Send messages to the LLM engine and receive the response."""
        try:
            # Check if engine was properly initialized
            if not getattr(self, '_initialized', False):
                return "❌ LLM engine not properly initialized"

            # Log who's calling this (to debug 30 tabs issue)
            import traceback
            caller_info = "".join(traceback.format_stack()[-3:-1])
            log_warning(f"[selenium] ⚠️ generate_response called! Caller:\n{caller_info}")
            
            log_debug(f"[selenium] generate_response called with {len(messages) if isinstance(messages, list) else 1} message(s)")
            
            # Convert messages to text
            if isinstance(messages, list):
                prompt_text = ""
                for msg in messages:
                    if isinstance(msg, dict) and "content" in msg:
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        prompt_text += f"{role}: {content}\n"
                    else:
                        prompt_text += str(msg) + "\n"
            else:
                prompt_text = str(messages)

            log_warning(f"[selenium] ⚠️ About to call _execute_complete_workflow with prompt: {prompt_text}")

            # Lazy driver initialization - create only when first actual request comes in
            if self.driver is None:
                log_info(f"[selenium] 🚀 First use of {self.component_name} - creating shared driver")
                shared_driver = await self._get_shared_driver()
                self.driver = shared_driver  # Assign to instance for compatibility
                log_info(f"[selenium] ✅ Driver ready for {self.component_name}")

            # Health check: verify driver is still alive
            driver_is_dead = False
            try:
                window_count = len(self.driver.window_handles)
                log_info(f"[selenium] Using shared driver with {window_count} window(s)")
            except Exception as health_check_error:
                log_warning(f"[selenium] ⚠️ Driver health check failed: {health_check_error}")
                driver_is_dead = True

            # If driver is dead, reset and recreate it
            if driver_is_dead:
                log_warning(f"[selenium] 🔴 Driver is dead, resetting global driver")
                # Reset global driver so it gets recreated
                SeleniumLLMBase._global_shared_driver = None
                SeleniumLLMBase._global_ref_count = 0
                # Get a fresh driver
                shared_driver = await self._get_shared_driver()
                self.driver = shared_driver
                log_info(f"[selenium] ✅ Fresh driver ready for {self.component_name}")
                window_count = len(self.driver.window_handles)
                log_info(f"[selenium] Using fresh shared driver with {window_count} window(s)")

            try:
                # Execute interaction in a single thread (driver is now guaranteed to be ready)
                response = await asyncio.to_thread(self._execute_complete_workflow, prompt_text)
                
                # Simply return the response as-is from ChatGPT
                # Validation and correction will be handled by the message chain / transport layer
                # NOT by this LLM engine itself (to avoid recursive loops)
                return response or "No response received from LLM"
            finally:
                # Don't release the shared driver here - let it persist for other requests
                # The driver will be released when the last instance is destroyed
                pass

        except Exception as e:
            log_error(f"[selenium] Failed to generate response: {e}", e)
            return f"❌ Error generating response: {e}"

    def _execute_complete_workflow(self, prompt_text: str) -> str:
        """Execute the complete workflow (interaction only) in a single thread."""
        try:
            log_debug(f"[selenium] _execute_complete_workflow called with driver: {self.driver is not None}")
            
            # Driver is guaranteed to be initialized by generate_response
            # Just verify it's still alive
            try:
                self.driver.current_url
            except Exception as e:
                log_error(f"[selenium] Driver is dead: {e}")
                raise Exception(f"Driver is not available: {e}")

            # Check login status (may navigate if needed, but don't block execution)
            # The old plugin would notify but continue anyway
            logged_in = self._ensure_logged_in(self.driver)
            if not logged_in:
                log_warning("[selenium] Not logged in, but continuing anyway")

            # Locate prompt area
            textarea = self._locate_prompt_area(self.driver)

            # Send prompt and wait for response
            if not self._send_prompt_with_confirmation(textarea, prompt_text):
                return "❌ Failed to send prompt"

            # Handle response choice if applicable (e.g., ChatGPT offers two responses)
            self._handle_response_choice(self.driver)

            # Wait for response to stabilize (text stops growing for N seconds)
            response = self.wait_until_response_stabilizes(self.driver)

            return response

        except Exception as e:
            log_error(f"[selenium] Workflow execution failed: {e}", e)
            return f"❌ Error: {e}"

    def check_login_status(self, driver, login_button_selectors=None, login_texts=None):
        """Check login status using provided selectors and warn if not logged in.
        
        This method should be called by LLM subclasses to check if the user appears to be
        logged in to the service. It uses a two-strategy approach:
        1. Check for login button selectors (CSS selectors, IDs, etc.)
        2. Check for login/signup text on the page
        
        LLM subclasses should define their login detection parameters in __init__ and call
        this method during startup checks.
        
        Example usage in LLM subclass:
            self.login_button_selectors = [
                (By.CSS_SELECTOR, "button[data-testid='login-button']"),
                (By.ID, "login-button"),
            ]
            self.login_texts = ["log in", "sign in", "login"]
            
            # Then call during startup:
            self.check_login_status(driver, self.login_button_selectors, self.login_texts)
        
        Args:
            driver: WebDriver instance
            login_button_selectors: List of (By, selector) tuples to check for login buttons
            login_texts: List of text strings to search for on the page
            
        Returns:
            bool: True if appears logged in, False if login indicators found
        """
        try:
            # Strategy 1: Check for login button selectors (if provided)
            login_button_found = False
            if login_button_selectors:
                for by, selector in login_button_selectors:
                    try:
                        elements = driver.find_elements(by, selector)
                        if elements:
                            login_button_found = True
                            log_debug(f"[selenium] Found login button with selector: {by} = '{selector}'")
                            break
                    except Exception:
                        continue
            
            # Strategy 2: Check for login/signup text on page (if provided)
            if not login_button_found and login_texts:
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    
                    for text in login_texts:
                        if text.lower() in page_text:
                            login_button_found = True
                            log_debug(f"[selenium] Found login text '{text}' on page")
                            break
                except Exception as e:
                    log_debug(f"[selenium] Could not check page text: {e}")
            
            # If login indicators found, warn user about unlogged limitations
            if login_button_found:
                log_warning(f"[selenium] ⚠️  User appears to be unlogged on {self.component_name}. Unlogged sessions have very limited token usage and may be restricted by the service. Please log in through the UI for full functionality.")
                if self._notify_fn:
                    self._notify_fn(f"⚠️  {self.component_name.title()}: Unlogged session detected. Limited token usage - please log in for full functionality.")
                return False
                    
        except Exception as e:
            log_debug(f"[selenium] Error checking login status: {e}")
            return True  # Assume logged in if check fails
            
        return not login_button_found