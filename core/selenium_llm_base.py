# core/selenium_llm_base.py

"""
Base library for Selenium-based LLM engines.
Provides common functionality for ChatGPT, Gemini, Grok and other browser-based LLMs.
"""

try:  # pragma: no cover - import guard for test environments/containers
    import undetected_chromedriver as uc  # type: ignore
except Exception:  # pragma: no cover
    uc = None
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
import random

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
    ElementClickInterceptedException,
    SessionNotCreatedException,
    WebDriverException,
    StaleElementReferenceException,
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib3.exceptions import ReadTimeoutError
from core.transport_layer import llm_to_interface
from core.variables_engine import exposed_vars

# Local functions and classes
from core.logging_utils import log_debug, log_error, log_warning, log_info, _LOG_DIR
from core.notifier import set_notifier
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var
import core.recent_chats as recent_chats
from core.ai_plugin_base import AIPluginBase
from core.action_parser import CORRECTOR_RETRIES
from core.message_chain import RESPONSE_TIMEOUT
from core.prompt_engine import reduce_json_text_for_transmission

# Register exposed variables for WebUI (shared by all Selenium-based LLM engines)
register_exposed_var(
    "CHROMIUM_HEADLESS",
    label="Chromium Headless Mode",
    default=0,
    value_type=int,
    ui_type="bool",
    description="Set to 1 for headless mode (no browser window), 0 for non-headless mode (visible browser window)",
    scope="llm",
    advanced=True,
    component="selenium_llm_base",
)

register_exposed_var(
    "SELENIUM_MAX_RETRIES",
    label="Selenium Max Retries",
    default=3,
    value_type=int,
    ui_type="number",
    description="Maximum number of retries for Selenium driver initialization",
    scope="llm",
    advanced=True,
    component="selenium_llm_base",
)

register_exposed_var(
    "AWAIT_RESPONSE_TIMEOUT",
    label="Response Timeout (Selenium)",
    default=240,
    value_type=int,
    ui_type="number",
    description="Seconds to wait for ChatGPT response before timing out",
    scope="llm",
    component="selenium_chatgpt_legacy",
    tags=["llm_engine"],
)

register_exposed_var(
    "CORRECTOR_RETRIES",
    label="Corrector Retries",
    default=2,
    value_type=int,
    ui_type="number",
    description="Maximum number of retries for the response corrector",
    scope="llm",
    component="selenium_chatgpt_legacy",
    tags=["llm_engine"],
)

register_exposed_var(
    "SELENIUM_PART1_PROCESSING_TIMEOUT",
    label="Selenium PART1 Processing Timeout (sec)",
    default=8,
    value_type=int,
    ui_type="number",
    description="Seconds to wait for ChatGPT to start responding for PART1 (smaller than AWAIT_RESPONSE_TIMEOUT)",
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
)

register_exposed_var(
    "SELENIUM_POST_SEND_CONFIRM_TIMEOUT",
    label="Selenium Post-Send Confirm Timeout (sec)",
    default=4.0,
    value_type=float,
    ui_type="number",
    description="Seconds to wait for UI confirmation after sending a prompt (textarea cleared or sending indicator).",
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
    advanced=True,
)

register_exposed_var(
    "SELENIUM_RESPONSE_POLL_INTERVAL",
    label="Selenium Response Poll Interval (sec)",
    default=0.5,
    value_type=float,
    ui_type="number",
    description="Polling interval while waiting for the response to stabilize.",
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
    advanced=True,
)

register_exposed_var(
    "SELENIUM_RESPONSE_STABLE_GRACE",
    label="Selenium Response Stable Grace (sec)",
    default=3.5,
    value_type=float,
    ui_type="number",
    description="How many seconds the response length must remain unchanged before we consider it complete.",
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
    advanced=True,
)

register_exposed_var(
    "SELENIUM_PART1_RESPONSE_STABLE_GRACE",
    label="Selenium PART1 Stable Grace (sec)",
    default=0.4,
    value_type=float,
    ui_type="number",
    description="Shorter stable-grace for PART1 (context-only) to reduce latency.",
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
    advanced=True,
)

# Driver recovery defaults must be defined BEFORE the config variables are registered
DEFAULT_DRIVER_RESPONSIVE_TIMEOUT = 10  # seconds to wait for a trivial driver operation
DEFAULT_DRIVER_RECOVERY_RETRIES = 2  # how many times to try restarting the driver and retrying the workflow

register_exposed_var(
    "SELENIUM_DRIVER_RESPONSIVE_TIMEOUT",
    label="Selenium Driver Responsiveness Timeout (sec)",
    default=DEFAULT_DRIVER_RESPONSIVE_TIMEOUT,
    value_type=int,
    ui_type="number",
    description="Seconds to wait for a trivial driver operation (window handles / current_url) before considering it frozen and attempting a restart.",
    scope="llm",
    component="selenium_llm_base",
    advanced=True,
)

register_exposed_var(
    "SELENIUM_DRIVER_RECOVERY_RETRIES",
    label="Selenium Driver Recovery Retries",
    default=DEFAULT_DRIVER_RECOVERY_RETRIES,
    value_type=int,
    ui_type="number",
    description="Number of attempts to restart the browser and retry the workflow when the driver appears frozen.",
    scope="llm",
    component="selenium_llm_base",
    advanced=True,
)

register_exposed_var(
    "SELENIUM_SPLIT_PROMPT_PARTS",
    label="Selenium Split Prompt Parts",
    default=3,
    value_type=int,
    ui_type="number",
    description=(
        "Maximum number of parts when splitting oversized prompts (before any minification/reduction). "
        "If set to 3 but everything fits in 2, only 2 parts are used."
    ),
    scope="llm",
    component="selenium_llm_base",
    tags=["llm_engine"],
    advanced=True,
)

# Use global timeout
AWAIT_RESPONSE_TIMEOUT = RESPONSE_TIMEOUT

# Load environment variables
load_dotenv()

# Constants
GRACE_PERIOD_SECONDS = 3
MAX_WAIT_TIMEOUT_SECONDS = 5 * 60  # hard ceiling

# Faster intermediate prompt handling (e.g., PART1 of double-prompt)
DEFAULT_RESPONSE_POLL_INTERVAL = 0.5
DEFAULT_RESPONSE_STABLE_GRACE = 3.5
DEFAULT_PART1_RESPONSE_STABLE_GRACE = 0.4

DEFAULT_SPLIT_PROMPT_PARTS = 3

class FrozenDriverError(Exception):
    """Raised when the Selenium driver appears unresponsive or frozen."""
    pass

# Cache the last response per chat to avoid duplicates
previous_responses: Dict[str, str] = {}
response_cache_lock = threading.Lock()

# Global driver manager for shared browser instance
_shared_driver = None
_shared_driver_lock = threading.Lock()
_shared_driver_ref_count = 0

# Global prompt character limit from the active Selenium LLM engine
# Updated when an engine is loaded or model is switched
# Default value comes from ChatGPT's MODEL_LIMITS_MAP["default"] = 51,000
_active_selenium_max_prompt_chars = 51000  # Safe default (ChatGPT default), will be updated when engine loads
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


def _llm_name_for_logs() -> str:
    """Return a human-friendly name for the active Selenium LLM to use in logs.

    Falls back to a generic label when the active name is not set.
    """
    return _active_selenium_llm_name or "LLM"


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
    def _driver_is_usable(cls, driver: Optional[webdriver.Remote]) -> bool:
        """Best-effort check to detect when the user closed the visible browser.

        In webtop setups the Chromium window can be closed manually; Selenium will then
        often keep a stale driver object that raises on access or reports no windows.
        """
        if driver is None:
            return False
        try:
            handles = driver.window_handles
            if not handles:
                return False
            # Touch a cheap property to detect invalid session
            _ = driver.current_url
            return True
        except Exception:
            return False

    @classmethod
    async def _get_global_shared_driver(cls) -> webdriver.Remote:
        """Get the single global shared driver instance."""
        async with cls._global_driver_lock:
            if cls._global_shared_driver is None:
                log_info("[selenium] 🌍 Creating global shared driver instance with 120s timeout")
                try:
                    # Add timeout to prevent infinite blocking on driver creation
                    cls._global_shared_driver = await asyncio.wait_for(
                        asyncio.to_thread(cls._create_shared_driver),
                        timeout=120  # 120 seconds max for driver creation
                    )
                    cls._global_ref_count = 1
                    log_info(f"[selenium] 🌍 Global driver created with {len(cls._global_shared_driver.window_handles)} window(s)")
                except asyncio.TimeoutError:
                    log_error("[selenium] 🔴 Driver creation timed out after 120s - browser failed to start")
                    raise Exception("Selenium driver creation timeout - browser failed to initialize")
                except Exception as e:
                    log_error(f"[selenium] 🔴 Failed to create global driver: {e}")
                    raise
            else:
                cls._global_ref_count += 1
                log_debug(f"[selenium] 🌍 Reusing global driver (ref count: {cls._global_ref_count})")

                # If the user closed the visible browser window, the driver becomes stale.
                # Detect it and recreate automatically so the system can recover.
                usable = await asyncio.to_thread(cls._driver_is_usable, cls._global_shared_driver)
                if not usable:
                    log_warning("[selenium] 🪟 Global driver is not usable (window closed / invalid session). Recreating...")
                    try:
                        cls._global_shared_driver.quit()
                    except Exception as e:
                        log_warning(f"[selenium] Error quitting stale global driver: {e}")
                    cls._global_shared_driver = None

                    try:
                        cls._global_shared_driver = await asyncio.wait_for(
                            asyncio.to_thread(cls._create_shared_driver),
                            timeout=120,
                        )
                        cls._global_ref_count = 1
                        log_info(f"[selenium] 🌍 Global driver recreated with {len(cls._global_shared_driver.window_handles)} window(s)")
                    except asyncio.TimeoutError:
                        log_error("[selenium] 🔴 Driver recreation timed out after 120s")
                        raise Exception("Selenium driver recreation timeout - browser failed to initialize")
                else:
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

    async def _ensure_driver_responsive_or_restart(self, allow_recreate: bool = True) -> None:
        """Ensure the current driver responds to a trivial check, otherwise restart it.

        Tries a quick window_handles/current_url check with a short timeout. If the check
        times out or raises, attempts to quit the existing shared driver and recreate a fresh one.
        Raises FrozenDriverError if recovery fails.
        """
        try:
            timeout = int(config_registry.get_value("SELENIUM_DRIVER_RESPONSIVE_TIMEOUT", DEFAULT_DRIVER_RESPONSIVE_TIMEOUT))
        except Exception:
            timeout = DEFAULT_DRIVER_RESPONSIVE_TIMEOUT

        # Do the check in a separate thread and enforce timeout
        try:
            def _health_check():
                if self.driver is None:
                    raise FrozenDriverError("No driver assigned")
                # Access two cheap properties to detect frozen/stale sessions
                _ = len(self.driver.window_handles)
                _ = self.driver.current_url
                return True

            await asyncio.wait_for(asyncio.to_thread(_health_check), timeout=timeout)
            # If success, return quietly
            return
        except asyncio.TimeoutError:
            log_warning(f"[selenium] 🔴 Driver did not respond within {timeout}s - considered frozen")
        except Exception as e:
            log_warning(f"[selenium] 🔴 Driver health check failed: {e}")

        # Attempt to recover by killing and recreating the shared driver
        if not allow_recreate:
            raise FrozenDriverError("Driver unresponsive and recreation is disabled")

        try:
            log_info("[selenium] 🛠️ Attempting to recover the frozen driver: quitting and recreating shared driver")
            # Quit current driver if possible
            try:
                await asyncio.to_thread(lambda: self.driver.quit() if self.driver is not None else None)
            except Exception as e:
                log_warning(f"[selenium] Error quitting frozen driver: {e}")

            # Reset global shared driver state so next _get_shared_driver() will recreate
            SeleniumLLMBase._global_shared_driver = None
            SeleniumLLMBase._global_ref_count = 0

            # Try to get a fresh shared driver (this can raise)
            new_driver = await self._get_shared_driver()
            self.driver = new_driver
            log_info("[selenium] ✅ Recovered and replaced frozen driver successfully")
            return
        except Exception as e:
            log_error(f"[selenium] ❌ Driver recovery attempt failed: {e}")
            raise FrozenDriverError(f"Driver recovery failed: {e}")

    def _ensure_driver_responsive_or_restart_sync(self, allow_recreate: bool = True) -> None:
        """Synchronous wrapper used by sync workflows to ensure driver is responsive or recreate it.

        Uses a blocking thread pool health check with timeout and falls back to synchronous recreation
        using the class-level _create_shared_driver method.
        """
        try:
            timeout = int(config_registry.get_value("SELENIUM_DRIVER_RESPONSIVE_TIMEOUT", DEFAULT_DRIVER_RESPONSIVE_TIMEOUT))
        except Exception:
            timeout = DEFAULT_DRIVER_RESPONSIVE_TIMEOUT

        def _health_check():
            if self.driver is None:
                raise FrozenDriverError("No driver assigned")
            _ = len(self.driver.window_handles)
            _ = self.driver.current_url
            return True

        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_health_check)
                fut.result(timeout=timeout)
            return
        except concurrent.futures.TimeoutError:
            log_warning(f"[selenium] 🔴 (sync) Driver did not respond within {timeout}s - considered frozen")
        except Exception as e:
            log_warning(f"[selenium] 🔴 (sync) Driver health check failed: {e}")

        if not allow_recreate:
            raise FrozenDriverError("Driver unresponsive and recreation is disabled")

        try:
            log_info("[selenium] 🛠️ (sync) Attempting to recover the frozen driver: quitting and recreating shared driver")
            try:
                if self.driver is not None:
                    try:
                        self.driver.quit()
                    except Exception as e:
                        log_warning(f"[selenium] Error quitting driver during sync recovery: {e}")
            except Exception:
                pass

            try:
                self._cleanup_chromium_remnants()
            except Exception:
                pass

            # Create a new driver synchronously
            new_driver = type(self)._create_shared_driver()
            SeleniumLLMBase._global_shared_driver = new_driver
            SeleniumLLMBase._global_ref_count = 1
            self.driver = new_driver
            log_info("[selenium] ✅ (sync) Recovered and replaced frozen driver successfully")
            return
        except Exception as e:
            log_error(f"[selenium] ❌ (sync) Driver recovery attempt failed: {e}")
            raise FrozenDriverError(f"Sync driver recovery failed: {e}")

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

        # Add headless if configured (use class attribute when creating a shared driver)
        # Note: this method is a @classmethod, so use 'cls' rather than 'self'.
        if getattr(cls, 'CHROMIUM_HEADLESS', False):
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

        # Model management - to be set by subclasses
        # model_limits_map: Dict[model_name] -> max_chars
        # Example: {"gpt-4o": 128001, "gpt-4": 8001, "default": 128001}
        self.model_limits_map: dict = {}
        
        # model_config_var: name of the config variable that holds the current model name
        # Example: "CHATGPT_MODEL" or "GEMINI_MODEL"
        self.model_config_var: str = ""
        
        # default_model: fallback model to use if config var not set
        # Example: "gpt-4o" or "gemini-1.5-pro"
        self.default_model: str = ""

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

        # Selenium automation selectors - to be set by subclasses
        # Each selector set is a list of CSS selector strings tried in order
        self.selectors = {
            # Prompt input area - where user types the message
            "prompt_area": [],
            # Send button - to submit the prompt
            "send_button": [],
            # Response text area - where the LLM response appears
            "response_text": [],
            # Modal dismissal buttons - for closing any modal dialogs
            "modal_dismissal": [],
        }
        
        # Interface limits - to be set by subclasses
        # Default values can be overridden by subclasses via set_interface_limits()
        self.interface_limits = {
            "max_prompt_chars": 10000,
            "max_response_chars": 4000,
            "supports_images": True,
            "supports_functions": False,
            "model_name": "default"
        }

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
        # Read CHROMIUM_HEADLESS from the config registry (supports ENV override)
        from core.config_manager import config_registry
        # Use 0 as default; config_registry will populate from ENV if present
        self.CHROMIUM_HEADLESS_VAR = config_registry.get_var(
            "CHROMIUM_HEADLESS",
            0,
            label="Chromium Headless Mode",
            description="Set to 1 for headless mode (no browser window), 0 for non-headless mode (visible browser window)",
            value_type=int,
            group="llm",
            component="selenium",
            advanced=True,
        )

        # Local boolean flag (kept in sync via listener)
        try:
            self.CHROMIUM_HEADLESS = bool(int(self.CHROMIUM_HEADLESS_VAR))
        except Exception:
            self.CHROMIUM_HEADLESS = False

        # Keep the instance flag in sync when the config changes at runtime
        def _on_chromium_headless_change(new_value):
            try:
                self.CHROMIUM_HEADLESS = bool(int(new_value))
            except Exception:
                # Ignore misformatted values; keep previous state
                pass

        config_registry.add_listener("CHROMIUM_HEADLESS", _on_chromium_headless_change)

        # AWAIT_RESPONSE_TIMEOUT as a dynamic instance-backed config var
        self.AWAIT_RESPONSE_TIMEOUT_VAR = config_registry.get_var(
            "AWAIT_RESPONSE_TIMEOUT",
            RESPONSE_TIMEOUT,
            label="Response Timeout (Selenium)",
            description="Seconds to wait for ChatGPT response before timing out",
            value_type=int,
            group="llm",
            component="selenium",
        )

        try:
            self.AWAIT_RESPONSE_TIMEOUT = int(self.AWAIT_RESPONSE_TIMEOUT_VAR)
        except Exception:
            self.AWAIT_RESPONSE_TIMEOUT = RESPONSE_TIMEOUT

        def _on_await_response_timeout_change(new_value):
            try:
                self.AWAIT_RESPONSE_TIMEOUT = int(new_value)
            except Exception:
                pass

        config_registry.add_listener("AWAIT_RESPONSE_TIMEOUT", _on_await_response_timeout_change)

        # Max retries for driver initialization
        self.MAX_RETRIES_VAR = config_registry.get_var(
            "SELENIUM_MAX_RETRIES",
            3,
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

        # Internal flag to avoid re-splitting or recursive behaviour for PART2
        self._skip_double_prompt_for_this_send: bool = False

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

            if uc is None:
                raise RuntimeError(
                    "undetected_chromedriver is unavailable in this environment (import failed). "
                    "Selenium LLM engines cannot start here."
                )

            # Clean up any existing remnants
            self._cleanup_chromium_remnants()

            # Initialize undetected-chromedriver with retry logic
            log_debug("[selenium] Creating undetected Chrome driver...")
            try:
                log_debug(f"[selenium] undetected-chromedriver version: {uc.__version__}")
            except Exception:
                pass
            
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
            driver.set_page_load_timeout(self.AWAIT_RESPONSE_TIMEOUT)
            driver.set_script_timeout(self.AWAIT_RESPONSE_TIMEOUT)
            log_debug(f"[selenium] Driver timeouts set to {self.AWAIT_RESPONSE_TIMEOUT}s")
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

    def wait_until_response_stabilizes(
        self,
        driver,
        max_total_wait: int | None = None,
        no_change_grace: float = DEFAULT_RESPONSE_STABLE_GRACE,
        poll_interval: float | None = None,
    ) -> str:
        """Return the last response text once its length stops growing."""
        # Use instance default if not provided and convert to int in case it's a ConfigVar
        if max_total_wait is None:
            max_total_wait = self.AWAIT_RESPONSE_TIMEOUT
        max_total_wait = int(max_total_wait)

        try:
            if poll_interval is None:
                poll_interval = float(config_registry.get_value("SELENIUM_RESPONSE_POLL_INTERVAL", DEFAULT_RESPONSE_POLL_INTERVAL))
        except Exception:
            poll_interval = DEFAULT_RESPONSE_POLL_INTERVAL
        
        start = time.time()
        last_len = -1
        last_change = start
        final_text = ""

        consecutive_errors = 0
        max_consecutive_errors = 2

        while True:
            if time.time() - start >= max_total_wait:
                log_warning("[selenium] Timeout while waiting for response")
                return final_text

            # Dismiss any modal that might have appeared during response waiting
            # Uses the engine-specific selectors set in selectors["modal_dismissal"]
            modal_selectors = self.selectors.get("modal_dismissal", [])
            if modal_selectors:
                try:
                    self._dismiss_modal_with_selectors(driver, modal_selectors)
                except Exception:
                    # Non-fatal
                    pass

            try:
                text = self._extract_response_text(driver)
                # If extraction succeeded, reset consecutive error counter
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                log_warning(f"[selenium] Error extracting response text (attempt {consecutive_errors}): {e}")
                # If we hit several consecutive extraction errors, consider driver frozen and attempt recovery
                if consecutive_errors >= max_consecutive_errors:
                    log_warning("[selenium] 🔴 Multiple consecutive errors extracting response - driver may be frozen")
                    # Best-effort synchronous recovery: try to quit and recreate the shared driver
                    try:
                        try:
                            if self.driver is not None:
                                try:
                                    self.driver.quit()
                                except Exception as e:
                                    log_warning(f"[selenium] Error quitting driver during recovery: {e}")
                        except Exception:
                            pass

                        # Cleanup remnants before recreating
                        try:
                            self._cleanup_chromium_remnants()
                        except Exception:
                            pass

                        # Recreate driver synchronously (this may block for a while)
                        try:
                            new_driver = type(self)._create_shared_driver()
                            SeleniumLLMBase._global_shared_driver = new_driver
                            SeleniumLLMBase._global_ref_count = 1
                            self.driver = new_driver
                            log_info("[selenium] ✅ Synchronous recovery: replaced frozen driver; request should be retried")
                            # Signal to caller that the driver state changed so they can retry
                            raise FrozenDriverError("Driver recovered synchronously; please retry the workflow")
                        except Exception as e2:
                            log_error(f"[selenium] ❌ Synchronous driver recreation failed: {e2}")
                            # If recreation fails, bubble up a FrozenDriverError
                            raise FrozenDriverError(f"Driver recreation failed: {e2}")

                    except FrozenDriverError:
                        # Propagate FrozenDriverError up to caller for retry handling
                        raise
                    except Exception as e3:
                        log_error(f"[selenium] Unexpected error during driver recovery: {e3}")
                        raise FrozenDriverError(f"Unexpected recovery error: {e3}")

                # Sleep a short bit before trying again
                try:
                    time.sleep(max(0.05, float(poll_interval)))
                except Exception:
                    time.sleep(DEFAULT_RESPONSE_POLL_INTERVAL)
                continue

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

            try:
                time.sleep(max(0.05, float(poll_interval)))
            except Exception:
                time.sleep(DEFAULT_RESPONSE_POLL_INTERVAL)
    def _dismiss_modal_with_selectors(self, driver, modal_selectors: list) -> bool:
        """Dismiss any modal/dialog overlay using provided selectors.
        
        This is a generic method that can be called from any step of the Selenium flow.
        Each LLM engine (ChatGPT, Grok, Gemini, etc.) provides its own list of selectors.
        
        Only logs if a modal was actually dismissed.
        
        Args:
            driver: Selenium WebDriver instance
            modal_selectors: List of CSS selector strings to try for dismissing modals
        
        Returns:
            True if a modal was dismissed, False if no modal was found
        """
        try:
            for selector in modal_selectors:
                try:
                    buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                    if buttons:
                        for button in buttons:
                            try:
                                # Try to click the button if it's visible
                                if button.is_displayed():
                                    driver.execute_script("arguments[0].click();", button)
                                    time.sleep(0.5)
                                    log_debug(f"[selenium] Modal dismissed with selector: {selector}")
                                    return True
                            except Exception:
                                continue
                except Exception:
                    continue
            
            return False
        except Exception as e:
            log_debug(f"[selenium] Modal dismissal check failed: {e}")
            return False

    def _resolve_click_interception(self, driver, target_element) -> bool:
        """Try to resolve common causes of ElementClickInterceptedException.

        Strategies:
        - Use plugin-provided modal dismissal selectors
        - Locate common overlay elements and try to close them or remove them via JS
        - Send ESC key to dismiss dialogs
        - As last resort, remove overlay nodes via JS
        - If any strategy succeeds and the target becomes clickable, return True
        """
        try:
            # 1) Try plugin-provided modal dismissal
            try:
                modal_selectors = self.selectors.get('modal_dismissal', []) or []
                if modal_selectors:
                    dismissed = self._dismiss_modal_with_selectors(driver, modal_selectors)
                    if dismissed:
                        time.sleep(0.2)
                        return True
            except Exception:
                pass

            # 2) Look for common overlay/modal elements and attempt close
            overlay_selectors = ["[role='dialog']", ".modal", ".overlay", ".modal-backdrop", ".backdrop", "[data-testid='modal']"]
            for sel in overlay_selectors:
                try:
                    overlays = driver.find_elements(By.CSS_SELECTOR, sel)
                    for overlay in overlays:
                        try:
                            if not overlay.is_displayed():
                                continue
                            # Try finding a close button inside
                            try:
                                close_btns = overlay.find_elements(By.CSS_SELECTOR, "button[aria-label*='Close'], button.close, .close, button[aria-label*='Chiudi']")
                                for cb in close_btns:
                                    try:
                                        driver.execute_script("arguments[0].click();", cb)
                                        time.sleep(0.2)
                                        log_debug(f"[selenium] Clicked overlay close button for selector {sel}")
                                        return True
                                    except Exception:
                                        continue
                            except Exception:
                                pass

                            # Send ESC key to body
                            try:
                                body = driver.find_element(By.TAG_NAME, 'body')
                                body.send_keys(Keys.ESCAPE)
                                time.sleep(0.2)
                                log_debug("[selenium] Sent ESC key to dismiss overlay")
                                # If overlay is gone, return True
                                if not overlay.is_displayed():
                                    return True
                            except Exception:
                                pass

                            # As a last resort try to remove the node via JS
                            try:
                                driver.execute_script("arguments[0].parentNode.removeChild(arguments[0]);", overlay)
                                time.sleep(0.2)
                                log_debug(f"[selenium] Removed overlay element via JS for selector {sel}")
                                return True
                            except Exception:
                                pass

                        except Exception:
                            continue
                except Exception:
                    continue

            # 3) If still blocked, try clicking via ActionChains at element center
            try:
                actions = ActionChains(driver)
                actions.move_to_element(target_element).click().perform()
                time.sleep(0.1)
                log_debug("[selenium] Clicked target element via ActionChains as last resort")
                return True
            except Exception:
                log_debug("[selenium] ActionChains click failed")

            return False
        except Exception as e:
            log_debug(f"[selenium] resolve_click_interception failed: {e}")
            return False
    
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

    def wait_for_response_completion(self, driver, timeout: int | None = None) -> bool:
        """Wait until the current response finishes streaming."""
        # Use instance default if not provided and convert to int in case it's a ConfigVar
        if timeout is None:
            timeout = self.AWAIT_RESPONSE_TIMEOUT
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

    # === GENERIC SELECTOR-BASED METHODS (using selectors from plugin config) ===

    def _locate_prompt_area(self, driver, timeout: int = None):
        """Locate the prompt area using CSS selectors from self.selectors["prompt_area"].
        
        The plugin must set self.selectors["prompt_area"] with CSS selectors to try.
        This method tries each selector in order until one works.
        Uses random timeout (1-5 seconds per selector) to avoid timing issues.
        """
        if timeout is None:
            timeout = random.uniform(1, 5)  # Random timeout between 1-5 seconds
        
        selectors = self.selectors.get("prompt_area", [])
        if not selectors:
            log_error("[selenium] No prompt area selectors configured in plugin")
            return None
        
        for selector in selectors:
            try:
                random_timeout = random.uniform(1, 5)  # Fresh random timeout per selector
                log_debug(f"[selenium] Trying prompt area selector: {selector} (timeout: {random_timeout:.2f}s)")
                
                # First try: element_to_be_clickable (strict, requires visible + enabled)
                try:
                    element = WebDriverWait(driver, random_timeout * 0.4).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    log_debug(f"[selenium] Found prompt area (clickable) with selector: {selector}")
                    return element
                except Exception as e1:
                    log_debug(f"[selenium] Clickable check failed for {selector}: {e1}")
                    
                    # Fallback: just check presence (less strict)
                    element = WebDriverWait(driver, random_timeout * 0.6).until(
                        EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
                    )
                    if element and len(element) > 0:
                        log_debug(f"[selenium] Found prompt area (present) with selector: {selector}")
                        return element[0]  # Return first matching element
                        
            except Exception as e:
                log_debug(f"[selenium] Prompt area selector failed: {selector} - {e}")
                continue
        
        log_error("[selenium] Could not locate prompt input area with any selector")
        return None

    def _find_send_button(self, driver, timeout=5):
        """Find the send/submit button using CSS selectors from self.selectors["send_button"].
        
        The plugin must set self.selectors["send_button"] with CSS selectors to try.
        This method tries each selector in order until one works.
        Also includes fallback checks for visible and enabled state.
        """
        selectors = self.selectors.get("send_button", [])
        if not selectors:
            log_debug("[selenium] No send button selectors configured in plugin")
            return None
        
        for selector in selectors:
            try:
                log_debug(f"[selenium] Trying send button selector: {selector}")
                # First, try to wait for it to be clickable (which means visible + enabled)
                button = WebDriverWait(driver, timeout).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                log_debug(f"[selenium] Found clickable send button with selector: {selector}")
                return button
            except TimeoutException:
                # Element exists but isn't clickable yet, try to find it anyway
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        button = elements[0]
                        # Check if it's visible at least
                        if button.is_displayed():
                            log_debug(f"[selenium] Found visible send button (but not clickable) with selector: {selector}")
                            return button
                        else:
                            log_debug(f"[selenium] Send button found but not visible: {selector}")
                except Exception as e:
                    log_debug(f"[selenium] Error checking visibility for selector {selector}: {e}")
            except Exception as e:
                log_debug(f"[selenium] Send button selector failed: {selector} - {e}")
                continue
        
        log_debug("[selenium] No send button found with any selector")
        return None
    
    def _get_response_selectors(self) -> list:
        """Get CSS selectors for extracting response text.
        
        Returns selectors from self.selectors["response_text"] that the plugin configured.
        Can be overridden by subclass if needed.
        """
        return self.selectors.get("response_text", [])

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

    def _send_prompt_with_confirmation(
        self,
        textarea,
        prompt_text: str,
        processing_max_wait: int | None = None,
        post_send_confirm_timeout: float | None = None,
    ) -> bool:
        """Send the prompt to the LLM service and wait for confirmation.
        
        This generic implementation:
        1. Clears any existing text in the textarea
        2. Sends the prompt text using send_keys or JavaScript
        3. Finds and clicks the send button (using selectors from plugin config)
        4. Returns True on success
        
        The plugin must have configured:
        - self.selectors["send_button"]: CSS selectors for the send button
        """
        def _get_part1_max_wait():
            try:
                return int(config_registry.get_value("SELENIUM_PART1_PROCESSING_TIMEOUT", 8))
            except Exception:
                return 8

        def _get_post_send_confirm_timeout() -> float:
            try:
                return float(config_registry.get_value("SELENIUM_POST_SEND_CONFIRM_TIMEOUT", 4.0))
            except Exception:
                return 4.0

        try:
            if not textarea:
                log_error("[selenium] No textarea provided")
                return False
            
            # Dismiss any modal/dialog that might be blocking interaction
            modal_selectors = self.selectors.get("modal_dismissal", [])
            if modal_selectors:
                self._dismiss_modal_with_selectors(self.driver, modal_selectors)
            
            # Filter out non-BMP characters that ChromeDriver can't handle
            # NOTE: Modern ChromeDriver supports full Unicode, so this should rarely happen
            # Only filter if the prompt becomes suspiciously small after filtering
            def filter_bmp_chars(text):
                """Filter out characters outside the Basic Multilingual Plane (BMP)."""
                return ''.join(char for char in text if ord(char) <= 0xFFFF)
            
            # First, try to use the full prompt as-is
            filtered_prompt = prompt_text
            
            # Only apply BMP filtering if the prompt seems to have problematic characters
            # (This is a safety measure for older ChromeDriver versions)
            try:
                # Try to encode as UTF-8 to check for issues
                prompt_text.encode('utf-8')
                # If encoding works, use the original prompt
                log_debug(f"[selenium] Prompt encoded successfully as UTF-8, no filtering needed")
            except Exception as e:
                log_warning(f"[selenium] Prompt encoding issue: {e}, applying BMP filter")
                filtered_prompt = filter_bmp_chars(prompt_text)
            
            if len(filtered_prompt) != len(prompt_text):
                removed_chars = len(prompt_text) - len(filtered_prompt)
                log_warning(f"[selenium] Filtered {removed_chars} characters from prompt")
            
            # Try intelligent reduction for JSON prompts (removes only oldest memories)
            # The goal is to fit within the MODEL's actual character/token limits
            # 
            # Get the active model's limit dynamically - this is the SOURCE OF TRUTH
            # not arbitrary UI limits or fallbacks
            global _active_selenium_max_prompt_chars
            model_limit = _active_selenium_max_prompt_chars  # Fallback to global limit
            try:
                if hasattr(self, 'llm_registry') and self.llm_registry:
                    active_llm = getattr(self.llm_registry, 'active_llm', None)
                    if active_llm and hasattr(active_llm, 'get_model_context_length'):
                        try:
                            model_limit = active_llm.get_model_context_length()
                            log_debug(f"[selenium] Got model limit from registry: {model_limit}")
                        except Exception:
                            log_debug(f"[selenium] Could not fetch model limit from active_llm")
            except Exception as e:
                log_debug(f"[selenium] Could not fetch model limit: {e}")
            
            # Apply intelligent reduction if prompt exceeds model limit
            if len(filtered_prompt) > model_limit:
                try:
                    # Try to reduce as JSON (intelligently removes oldest memories)
                    reduced_json = reduce_json_text_for_transmission(filtered_prompt, model_limit)
                    if len(reduced_json) < len(filtered_prompt):
                        filtered_prompt = reduced_json
                        log_info(f"[selenium] Applied intelligent JSON reduction: {len(prompt_text)} → {len(filtered_prompt)} chars (model limit: {model_limit})")
                    else:
                        # Fallback: dumb truncation to model limit
                        original_length = len(filtered_prompt)
                        filtered_prompt = filtered_prompt[:model_limit]
                        log_warning(f"[selenium] Dumb truncation: {original_length} → {len(filtered_prompt)} chars (model limit: {model_limit})")
                except Exception as e:
                    log_debug(f"[selenium] Intelligent reduction failed: {e}, falling back to emergency truncation")
                    original_length = len(filtered_prompt)
                    
                    # Emergency truncation: try to preserve minimum valid JSON structure
                    truncated = filtered_prompt[:model_limit]
                    
                    # If it looks like a JSON object was being cut off, try to close it gracefully
                    if truncated.count('{') > truncated.count('}'):
                        # Find the last complete object/array and close it
                        last_bracket = max(truncated.rfind('}'), truncated.rfind(']'))
                        if last_bracket > 0:
                            truncated = truncated[:last_bracket+1]
                            log_debug(f"[selenium] Closed unclosed JSON at position {last_bracket}")
                    
                    # If truncation resulted in invalid JSON structure, use safe minimal version
                    if '{"' not in truncated[:50]:  # Likely not valid JSON
                        truncated = '{"status": "event_reminder_truncated", "message": "Context too large, using fallback"}'
                        log_warning(f"[selenium] Extreme emergency: JSON was too corrupted, using minimal fallback structure")
                    
                    filtered_prompt = truncated
                    log_warning(f"[selenium] Emergency fallback truncation: {original_length} → {len(filtered_prompt)} chars (model limit: {model_limit})")
            
            log_debug(f"[selenium] About to clear textarea and send prompt (size: {len(filtered_prompt)}, model limit: {model_limit})")
            
            # Wait for textarea to be ready for input
            WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(textarea)
            )
            log_debug(f"[selenium] Textarea is clickable")
            
            # Determine if it's a textarea or contenteditable div
            tag_name = textarea.tag_name.lower()
            is_textarea = tag_name == "textarea"
            is_contenteditable = textarea.get_attribute("contenteditable") == "true"
            
            log_debug(f"[selenium] Element type: tag={tag_name}, is_textarea={is_textarea}, is_contenteditable={is_contenteditable}")
            
            # Check current content before clearing
            if is_textarea:
                current_value = textarea.get_attribute("value") or ""
            else:
                current_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium] Current textarea value before clear: '{current_value[:100] if len(current_value) > 100 else current_value}' (length: {len(current_value)})")
            
            # Clear any existing text
            log_debug(f"[selenium] Clearing textarea")
            if is_textarea:
                textarea.clear()
            else:
                # For contenteditable divs, use JavaScript to clear
                self.driver.execute_script("arguments[0].textContent = '';", textarea)
            log_debug(f"[selenium] Textarea cleared")
            
            # Check content after clearing
            if is_textarea:
                after_clear_value = textarea.get_attribute("value") or ""
            else:
                after_clear_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium] Textarea value after clear: '{after_clear_value}' (length: {len(after_clear_value)})")
            
            # Paste the filtered prompt text
            log_debug(f"[selenium] Sending text to textarea: '{filtered_prompt[:100]}...' (length: {len(filtered_prompt)})")
            
            if is_textarea:
                # For textarea elements, use send_keys
                try:
                    textarea.send_keys(filtered_prompt)
                    log_debug(f"[selenium] Keys sent to textarea via send_keys")
                except Exception as send_keys_error:
                    log_debug(f"[selenium] send_keys failed: {send_keys_error}")
                    # Try alternative method: use JavaScript to set value
                    try:
                        self.driver.execute_script("arguments[0].value = arguments[1];", textarea, filtered_prompt)
                        # Trigger input event to make sure service detects the change
                        self.driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", textarea)
                        log_debug(f"[selenium] Text set via JavaScript")
                    except Exception as js_error:
                        log_debug(f"[selenium] JavaScript method also failed: {js_error}")
                        raise send_keys_error  # Re-raise original error
            else:
                # For contenteditable divs, use JavaScript to insert text
                log_debug(f"[selenium] Inserting text via JavaScript for contenteditable div")
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
                    log_debug(f"[selenium] Text injected via JavaScript for contenteditable")
                except Exception as js_error:
                    log_error(f"[selenium] Failed to inject text via JavaScript: {js_error}")
                    raise
            
            # Check final content
            if is_textarea:
                final_value = textarea.get_attribute("value") or ""
            else:
                final_value = textarea.get_attribute("textContent") or textarea.text or ""
            log_debug(f"[selenium] Final textarea value: '{final_value[:100] if len(final_value) > 100 else final_value}' (length: {len(final_value)})")
            
            # Ensure the textarea still has focus before sending
            log_debug("[selenium] Ensuring textarea has focus before send...")
            self.driver.execute_script("arguments[0].focus();", textarea)
            try:
                time.sleep(0.1)
            except Exception:
                pass
            
            log_debug(f"[selenium] Prompt pasted, now trying to send")
            
            # Try to find and click send button first
            send_button = self._find_send_button(self.driver, timeout=3)
            if send_button:
                log_debug(f"[selenium] Send button found: {send_button.tag_name} with text: '{send_button.text}'")
                
                # Check if button is enabled
                is_enabled = send_button.is_enabled()
                log_debug(f"[selenium] Send button enabled: {is_enabled}")
                
                if is_enabled:
                    log_debug("[selenium] Clicking send button")
                    try:
                        # Scroll button into view to ensure it's clickable
                        self.driver.execute_script("arguments[0].scrollIntoView(true);", send_button)
                        try:
                            time.sleep(0.1)
                        except Exception:
                            pass
                        
                        # Try direct click first
                        send_button.click()
                        log_debug("[selenium] Send button clicked successfully")
                    except Exception as click_error:
                        log_debug(f"[selenium] Send button click failed: {click_error}")
                        # If the click was intercepted, try to resolve common overlay/modal issues
                        intercepted = False
                        try:
                            if isinstance(click_error, ElementClickInterceptedException) or 'element click intercepted' in str(click_error).lower():
                                intercepted = True
                        except Exception:
                            pass

                        if intercepted:
                            log_warning("[selenium] Click intercepted - attempting to resolve overlays and retry")
                            try:
                                resolved = self._resolve_click_interception(self.driver, send_button)
                                if resolved:
                                    log_info("[selenium] Overlay resolution succeeded, retrying click")
                                    try:
                                        send_button.click()
                                        log_debug("[selenium] Send button clicked after overlay resolution")
                                    except Exception as final_click_error:
                                        log_debug(f"[selenium] Final click after resolution failed: {final_click_error}")
                                else:
                                    log_warning("[selenium] Overlay resolution did not succeed")
                            except Exception as resolve_err:
                                log_debug(f"[selenium] Overlay resolution raised an error: {resolve_err}")

                        log_debug("[selenium] Attempting fallback: JavaScript click on button")
                        try:
                            # Try JavaScript click
                            self.driver.execute_script("arguments[0].click();", send_button)
                            log_debug("[selenium] Send button clicked via JavaScript")
                        except Exception as js_click_error:
                            log_debug(f"[selenium] JavaScript click also failed: {js_click_error}")
                            # Final fallback to keyboard shortcut (Ctrl+Return or Cmd+Return) and Enter
                            log_debug("[selenium] Final fallback: using Ctrl+Return keyboard shortcut and Enter")
                            textarea.click()
                            try:
                                textarea.send_keys(Keys.CONTROL, Keys.RETURN)
                                log_debug("[selenium] Sent Ctrl+Return")
                            except Exception:
                                try:
                                    textarea.send_keys(Keys.RETURN)
                                    log_debug("[selenium] Sent Enter")
                                except Exception as e:
                                    log_debug(f"[selenium] Keyboard send also failed: {e}")
                else:
                    log_debug("[selenium] Send button is disabled, trying keyboard shortcut (Ctrl+Return)")
                    from selenium.webdriver.common.keys import Keys
                    textarea.click()
                    textarea.send_keys(Keys.CONTROL, Keys.RETURN)
            else:
                # Fallback: Send the message using Ctrl+Return (common shortcut for ChatGPT)
                log_debug("[selenium] No send button found, using Ctrl+Return keyboard shortcut")
                from selenium.webdriver.common.keys import Keys
                textarea.click()
                try:
                    textarea.send_keys(Keys.CONTROL, Keys.RETURN)
                    log_debug("[selenium] Sent Ctrl+Return as fallback")
                except Exception:
                    try:
                        textarea.send_keys(Keys.RETURN)
                        log_debug("[selenium] Sent Enter as fallback")
                    except Exception as e:
                        log_debug(f"[selenium] Keyboard fallback also failed: {e}")
            
            log_debug(f"[selenium] Send action completed, waiting for confirmation")

            if post_send_confirm_timeout is None:
                post_send_confirm_timeout = _get_post_send_confirm_timeout()
            
            # Wait for confirmation that the prompt was sent (textarea should clear or sending indicator appears)
            try:
                log_debug("[selenium] Checking if textarea cleared after send...")
                WebDriverWait(self.driver, max(0.5, float(post_send_confirm_timeout))).until(
                    lambda d: (
                        textarea.get_attribute("value") == "" or
                        textarea.text == "" or
                        len(d.find_elements(By.CSS_SELECTOR, "[data-testid*='sending'], [data-testid*='send'], .sending, .loading")) > 0
                    )
                )
                log_debug("[selenium] Prompt sent successfully - textarea cleared or sending indicator found")
            except Exception as wait_error:
                log_debug(f"[selenium] Wait for textarea clear timed out (may not have been sent): {wait_error}")
                # Check at least if text is still in textarea
                current_text = textarea.get_attribute("value") or textarea.text or ""
                if current_text:
                    log_error(f"[selenium] CRITICAL: Textarea still contains text after send attempt ({len(current_text)} chars). Send may have failed!")
                else:
                    log_debug("[selenium] Textarea is empty, send likely succeeded")
                # Continue anyway - the message might have been sent
            
            # CRITICAL: Verify the active Selenium LLM has started processing the request
            # Wait for signs that the active LLM is actually responding (streaming indicator, response area changes, etc.)
            log_debug(f"[selenium] Verifying {_llm_name_for_logs()} has started processing...")
            processing_started = False
            start_time = time.time()
            # Default waiting time for ChatGPT to start processing; can be overridden
            # by callers who may want shorter wait (e.g., PART1 of double-prompt)
            default_processing_max_wait = 30
            # Allow callers to pass a shorter processing timeout (e.g., PART1)
            max_wait = int(processing_max_wait) if processing_max_wait is not None else default_processing_max_wait
            
            while time.time() - start_time < max_wait:
                try:
                    # Check for response area containing text or streaming indicators
                    response_selectors = self.selectors.get("response_container", [])
                    for selector in response_selectors:
                        try:
                            elements = self.driver.find_elements(selector[0], selector[1])
                            for elem in elements:
                                # Check if element has any text content (ChatGPT started writing)
                                if elem.text and len(elem.text.strip()) > 0:
                                    processing_started = True
                                    log_debug(f"[selenium] {_llm_name_for_logs()} has started processing - found response text ({len(elem.text)} chars)")
                                    break
                        except:
                            pass
                    
                    if processing_started:
                        break
                    
                    # Also check for "stop generating" button which appears when ChatGPT is writing
                    try:
                        stop_buttons = self.driver.find_elements(By.CSS_SELECTOR, "[aria-label*='Stop'], button:has-text('Stop')")
                        if len(stop_buttons) > 0:
                            processing_started = True
                            log_debug(f"[selenium] {_llm_name_for_logs()} has started processing - found stop button")
                            break
                    except:
                        pass
                    
                    time.sleep(0.5)
                except Exception as e:
                    log_debug(f"[selenium] Error while checking for processing: {e}")
                    break
            
            if not processing_started:
                log_warning(f"[selenium] {_llm_name_for_logs()} did not start processing within {max_wait}s - prompt may not have been sent successfully")
                # Don't fail here - let wait_until_response_stabilizes handle the timeout
            else:
                log_debug(f"[selenium] {_llm_name_for_logs()} processing confirmed after {time.time() - start_time:.1f}s")
            
            return True  # Prompt was sent successfully
        
        except Exception as e:
            # Capture a screenshot for post-mortem analysis
            try:
                ts = int(time.time())
                path = f"/tmp/selenium_send_failure_{ts}.png"
                try:
                    self.driver.save_screenshot(path)
                    log_info(f"[selenium] Screenshot of failure saved: {path}")
                except Exception as sc_err:
                    log_debug(f"[selenium] Could not save screenshot: {sc_err}")
            except Exception:
                path = None

            log_error(f"[selenium] Failed to send prompt: {e}")

            # Notify user/monitoring that a send failure happened
            try:
                # Use configured fallback message (exposed variable) if available
                try:
                    failed_text = exposed_vars.get_value('FAILED_MESSAGE_TEXT') or "😵"
                except Exception:
                    failed_text = "😵"

                log_warning(f"[selenium][send_failure] Sending fallback message to originating interface: url={getattr(self.driver, 'current_url', 'unknown')} screenshot={path}")

                # Attempt to deliver the fallback message to the originating interface/chat (preferred)
                try:
                    meta = getattr(self, '_current_request_meta', None)
                    if meta and (meta.get('bot') or meta.get('chat_id') or meta.get('interface_path')):
                        bot = meta.get('bot')
                        chat_id = meta.get('chat_id')
                        interface = meta.get('interface')

                        # Schedule llm_to_interface asynchronously on the main loop
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                loop.create_task(llm_to_interface(bot, chat_id, text=failed_text, interface=interface, interface_path=meta.get('interface_path')))
                                log_debug("[selenium][send_failure] Scheduled fallback message via llm_to_interface (create_task)")
                            else:
                                asyncio.run_coroutine_threadsafe(llm_to_interface(bot, chat_id, text=failed_text, interface=interface, interface_path=meta.get('interface_path')), loop)
                                log_debug("[selenium][send_failure] Scheduled fallback message via run_coroutine_threadsafe")
                        except Exception as e_schedule:
                            log_debug(f"[selenium][send_failure] Could not schedule llm_to_interface: {e_schedule}")
                            # Fallback to trainer notification if scheduling fails
                            if getattr(self, '_notify_fn', None):
                                try:
                                    self._notify_fn(failed_text)
                                except Exception:
                                    pass
                    else:
                        # No originating interface metadata available - fall back to trainer notification
                        if getattr(self, '_notify_fn', None):
                            try:
                                self._notify_fn(failed_text)
                            except Exception:
                                pass
                except Exception as e_deliver:
                    log_debug(f"[selenium][send_failure] Error delivering fallback message: {e_deliver}")
                    if getattr(self, '_notify_fn', None):
                        try:
                            self._notify_fn(failed_text)
                        except Exception:
                            pass
            except Exception:
                pass

            # Try engine-specific UI reset hook to allow the engine to recover (e.g., open new chat)
            try:
                hook = getattr(self, '_engine_ui_reset_hook', None)
                if callable(hook):
                    try:
                        hook_res = hook(None)
                        if isinstance(hook_res, str) and hook_res.startswith('❌'):
                            # Propagate the hook's retry indicator up the chain
                            return hook_res
                    except Exception as hook_err:
                        log_debug(f"[selenium] _engine_ui_reset_hook call failed: {hook_err}")
            except Exception:
                pass

            # Re-raise to let upper layers handle retries/logic
            raise

    def get_supported_models(self):
        """Get supported models (to be overridden by subclasses)."""
        return []

    async def _send_error_message(self, bot, message):
        """Send a friendly fallback message to the originating interface/chat when the engine fails.

        This is called by the transport layer corrector when the LLM fails to produce
        a valid response after repeated attempts. It will use the `FAILED_MESSAGE_TEXT`
        exposed variable as the message content and route it to the originating interface
        or fall back to trainer notification if no interface info is available.
        """
        try:
            try:
                failed_text = exposed_vars.get_value('FAILED_MESSAGE_TEXT') or "😵"
            except Exception:
                failed_text = "😵"

            chat_id = getattr(message, 'chat_id', None)
            interface = getattr(message, 'interface', None) or getattr(message, 'interface_path', None)

            await llm_to_interface(bot, chat_id, text=failed_text, interface=interface, interface_path=getattr(message, 'interface_path', None))
            log_debug("[selenium] _send_error_message delivered fallback message to interface")
        except Exception as e:
            log_debug(f"[selenium] _send_error_message failed to deliver to interface: {e}")
            # Fallback to trainer notification
            try:
                if getattr(self, '_notify_fn', None):
                    self._notify_fn(failed_text)
            except Exception:
                pass

    def _get_model_char_limit(self, model_name: str) -> int:
        """Get character limit for a specific model.
        
        Uses the model_limits_map set by subclass. Returns default if model not found.
        
        Args:
            model_name: Name of the model (e.g., "gpt-4o", "gemini-1.5-pro")
            
        Returns:
            Integer character limit for the model
        """
        if not self.model_limits_map:
            return 10000  # Fallback default
            
        # Normalize model name (lowercase, strip)
        normalized = model_name.lower().strip()
        
        # Direct match
        if normalized in self.model_limits_map:
            return self.model_limits_map[normalized]
        
        # Try partial match
        for key in self.model_limits_map.keys():
            if key in normalized or normalized.endswith(key):
                return self.model_limits_map[key]
        
        # Return default if exists
        if "default" in self.model_limits_map:
            return self.model_limits_map["default"]
            
        # Last resort fallback
        return 10000

    def _get_current_model_name(self) -> str:
        """Get current model name from config or use default.
        
        Uses model_config_var to look up the config value, falls back to default_model.
        Subclasses can override this for custom logic.
        
        Returns:
            String name of the current model
        """
        if self.model_config_var:
            from core.config_manager import config_registry
            configured_model = config_registry.get_value(self.model_config_var, "")
            if configured_model:
                return configured_model
        
        return self.default_model or "default"

    def _update_interface_limits(self):
        """Update interface limits based on current model.
        
        Subclasses can override this for custom logic.
        This method is called automatically to sync limits when model changes.
        """
        model_name = self._get_current_model_name()
        max_chars = self._get_model_char_limit(model_name)
        
        # Update interface limits
        self.interface_limits["max_prompt_chars"] = max_chars
        self.interface_limits["model_name"] = model_name

    def get_current_model(self):
        """Get current model (to be overridden by subclasses)."""
        return None

    def get_interface_limits(self):
        """Get interface limits.
        
        Returns the interface_limits dict that was set by subclass.
        Subclasses should call _update_interface_limits() to sync with current model.
        """
        return self.interface_limits

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

    # === NEW: LOGIN FLOW HELPERS ===

    async def ensure_selkies_running(self) -> bool:
        """Check if Selkies CLI is available in PATH.

        This is a lightweight check used before attempting to orchestrate
        the login flow. It does not attempt to start Selkies services.
        """
        try:
            import shutil
            path = shutil.which("selkies")
            if path:
                log_debug(f"[selenium] Selkies binary found at: {path}")
                return True
            else:
                log_debug("[selenium] Selkies binary not found in PATH")
                return False
        except Exception as e:
            log_warning(f"[selenium] Error while checking Selkies availability: {e}")
            return False

    async def check_login_state(self) -> dict:
        """Check and return the current login state for this engine.

        Returns a dict: { 'logged_in': bool, 'login_state': 'logged'|'unlogged'|'unknown' }
        If the state changes, notifies via the notify function if set.
        """
        try:
            if self.driver is None:
                state = {"logged_in": False, "login_state": "unknown"}
            else:
                try:
                    logged = bool(self.is_user_logged_in())
                    state = {"logged_in": logged, "login_state": ("logged" if logged else "unlogged")}
                except Exception as e:
                    log_warning(f"[selenium] Error while evaluating login state: {e}")
                    state = {"logged_in": False, "login_state": "unknown"}

            # Notify if changed from previous known state
            prev = getattr(self, "_last_login_state", None)
            if prev != state:
                self._last_login_state = state
                msg = f"🔐 Login state for {getattr(self, 'component_name', 'selenium')} changed: {state['login_state']}"
                log_info(f"[selenium] {msg}")
                if getattr(self, "notify_fn", None):
                    try:
                        self.notify_fn(msg)
                    except Exception:
                        # Fallback: ignore notifier exceptions
                        pass

            return state
        except Exception as e:
            log_warning(f"[selenium] Unexpected error checking login state: {e}")
            return {"logged_in": False, "login_state": "unknown"}

    async def start_login_flow(self, timeout: int = 30) -> dict:
        """Start a non-blocking login flow for the engine.

        - Optionally ensures Selkies availability (best-effort).
        - Creates/obtains the shared driver and navigates to the service homepage.
        - Returns an initial state dict (see check_login_state()).
        """
        try:
            # Check Selkies availability but don't fail hard if missing
            selkies_ok = await self.ensure_selkies_running()
            if not selkies_ok:
                log_debug("[selenium] Selkies not available - continuing without it (Chromium may still be opened)")

            # Ensure driver is created (shared driver helper handles locking)
            try:
                driver = await asyncio.wait_for(self._get_shared_driver(), timeout=min(timeout, 60))
                self.driver = driver
            except Exception as e:
                log_error(f"[selenium] Failed to create/open browser for login flow: {e}")
                return {"logged_in": False, "login_state": "unknown", "error": str(e)}

            # Navigate to service URL if present
            try:
                if self.service_url:
                    log_debug(f"[selenium] Navigating to service URL for login: {self.service_url}")
                    try:
                        driver.get(self.service_url)
                    except Exception as e:
                        log_warning(f"[selenium] Navigation to {self.service_url} failed: {e}")
            except Exception:
                pass

            # Small delay to allow page to load
            try:
                await asyncio.sleep(1)
            except Exception:
                pass

            # Evaluate login state and return
            state = await self.check_login_state()
            return state

        except Exception as e:
            log_error(f"[selenium] start_login_flow failed: {e}")
            return {"logged_in": False, "login_state": "unknown", "error": str(e)}

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
        """Clean up resources when switching plugins."""
        try:
            log_info(f"[selenium] Cleaning up {self.component_name}")
            
            # Close the driver immediately (synchronously) to ensure it doesn't stay open
            # This is critical when switching between Selenium plugins
            if self.driver is not None:
                try:
                    self.driver.quit()
                    log_info(f"[selenium] ✅ Driver closed successfully during cleanup")
                except Exception as e:
                    log_warning(f"[selenium] ⚠️ Error quitting driver during cleanup: {e}")
                    # Still try to close windows
                    try:
                        # Force close any remaining windows
                        import subprocess
                        subprocess.run(["pkill", "-f", "chromium", "-15"], timeout=5)
                        log_debug(f"[selenium] Force killed chromium processes")
                    except Exception as pk_err:
                        log_debug(f"[selenium] Could not force kill chromium: {pk_err}")
            
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
        1. Sends to ChatGPT via generate_response() with proper role separation
        2. Returns the response to the caller (plugin_instance)
        
        The response will then be passed to message_chain.handle_incoming_message()
        for validation, correction, and action execution. This ensures all LLM responses
        are properly validated through the central message chain, not sent directly to interfaces.

        We also populate `_current_request_meta` for the duration of this request so
        lower-level error handlers (e.g., send failures) can deliver fallback
        messages to the originating interface/chat instead of notifying the trainer.
        """
        try:
            # Populate current request meta for error reporting / fallback messaging
            try:
                self._current_request_meta = {
                    'bot': bot,
                    'message': message,
                    'interface': getattr(message, 'interface', None) or getattr(message, 'interface_path', None),
                    'chat_id': getattr(message, 'chat_id', None),
                    'interface_path': getattr(message, 'interface_path', None),
                }
            except Exception:
                self._current_request_meta = None
            # Check if prompt contains a system_message (correction scenario)
            system_message_dict = None
            prompt_for_llm = prompt
            
            # Try to parse prompt if it's a string that looks like JSON
            if isinstance(prompt, str):
                try:
                    parsed = json.loads(prompt)
                    if isinstance(parsed, dict) and "system_message" in parsed:
                        system_message_dict = parsed.get("system_message", {})
                        # Extract the actual JSON instructions or format requirements
                        prompt_for_llm = parsed
                        log_debug(f"[selenium] Detected correction scenario with system_message type={system_message_dict.get('type')}")
                except (json.JSONDecodeError, ValueError):
                    # Not JSON, proceed normally
                    pass
            
            messages = []
            # If prompt was provided as a dict by plugin_instance, it may contain
            # a pre-reduction size metadata field that we should use for deciding
            # about double-prompt splitting. Extract and remove it so it is NOT
            # inadvertently sent to the LLM.
            pre_reduction_size = None
            if isinstance(prompt, dict):
                pre_reduction_size = prompt.pop("__pre_reduction_size", None)
            
            # Build system message to enforce JSON-only responses
            if system_message_dict:
                # This is a correction/error scenario - we MUST get JSON back
                error_msg = system_message_dict.get("message", "Invalid JSON")
                required_format = system_message_dict.get("required_format", {})
                strict_requirements = system_message_dict.get("strict_requirements", [])
                original_user_message = system_message_dict.get("original_user_message", "")
                
                # Build a comprehensive system prompt that forces JSON response
                system_prompt = (
                    "You are a JSON-only assistant. Your task is to respond with ONLY valid JSON.\n"
                    f"Error: {error_msg}\n"
                    "\nYou MUST respond with ONLY valid JSON, nothing else.\n"
                    "NO text outside JSON. NO markdown. NO explanations.\n"
                    "Strict requirements:\n"
                )
                for req in strict_requirements:
                    system_prompt += f"- {req}\n"
                
                system_prompt += (
                    "\nRespond with this exact structure:\n"
                    f"{json.dumps(required_format, indent=2)}\n"
                    "\nDo not deviate. Respond ONLY with valid JSON."
                )
                
                # Build user message with context
                # Include the ORIGINAL user message so LLM knows what to respond to
                user_content = (
                    "Please provide a valid JSON response following the structure shown above.\n\n"
                )
                
                if original_user_message:
                    user_content += (
                        f"ORIGINAL USER MESSAGE YOU SHOULD RESPOND TO:\n"
                        f"\"{original_user_message}\"\n\n"
                    )
                
                user_content += (
                    f"Your previous response was:\n{system_message_dict.get('your_reply', 'N/A')}\n\n"
                    "Now provide ONLY a valid JSON response following the structure shown above, "
                    "as if responding to the original user message. Respond ONLY with valid JSON, nothing else."
                )
                
                messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": user_content})
                
                log_info(f"[selenium] 🔧 Correction scenario: system prompt enforces JSON-only responses")
                log_debug(f"[selenium] System prompt length: {len(system_prompt)} chars")
                if original_user_message:
                    log_info(f"[selenium] 📝 Original user message in correction: \"{original_user_message}\"")
                else:
                    log_warning(f"[selenium] ⚠️ No original user message available in correction scenario!")
                
            else:
                # Normal scenario - convert prompt to messages
                if isinstance(prompt_for_llm, dict):
                    try:
                        # If a verbose (unminified) instruction for chats is present,
                        # include it as a system message so Selenium-driven UI flows
                        # embed it verbatim at the top of the prompt.
                        verbose = None
                        try:
                            verbose = prompt_for_llm.get("instructions_verbose")
                        except Exception:
                            verbose = None

                        if verbose:
                            messages.append({"role": "system", "content": verbose})

                        # Simply convert prompt to JSON for user message
                        prompt_text = json.dumps(prompt_for_llm)
                        messages.append({"role": "user", "content": prompt_text})
                        log_debug(f"[selenium] Built user message from prompt ({len(prompt_text)} chars)")
                    except Exception as e:
                        log_warning(f"[selenium] Error processing prompt: {e}")
                        prompt_text = str(prompt_for_llm)
                        messages.append({"role": "user", "content": prompt_text})
                    
                else:
                    # If prompt is already a list or string, convert to text format
                    if isinstance(prompt_for_llm, list):
                        # Convert message list to text
                        prompt_text = ""
                        for msg in prompt_for_llm:
                            if isinstance(msg, dict) and "content" in msg:
                                role = msg.get("role", "user")
                                content = msg.get("content", "")
                                prompt_text += f"{role}: {content}\n"
                            else:
                                prompt_text += str(msg) + "\n"
                    else:
                        prompt_text = str(prompt_for_llm)
                    
                    messages.append({"role": "user", "content": prompt_text})


            # The response will be returned to plugin_instance which passes it to message_chain
            try:
                response = await self.generate_response(messages, pre_reduction_size=pre_reduction_size)
                log_debug(f"[selenium] Response generated ({len(response) if response else 0} chars), returning to plugin_instance for message chain processing")
                return response
            except Exception as e:
                log_error(f"[selenium] Failed to handle incoming message: {e}", e)
                # Return error message instead of sending directly
                error_msg = f"❌ Error processing message: {e}"
                return error_msg
            finally:
                # Clear request meta to avoid accidental reuse by unrelated operations
                try:
                    self._current_request_meta = None
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[selenium] Unexpected error in handle_incoming_message: {e}", e)
            # Return error message instead of sending directly
            error_msg = f"❌ Error processing message: {e}"
            return error_msg
    async def generate_response(self, messages, pre_reduction_size: int | None = None):
        """Send messages to the LLM engine and receive the response."""
        try:
            # Check if engine was properly initialized
            if not getattr(self, '_initialized', False):
                return "❌ LLM engine not properly initialized"

            # Record metadata about the current incoming request so failure handlers
            # can send a fallback message back to the originating interface instead
            # of notifying the trainer. This is set by the plugin instance via
            # handle_incoming_message prior to calling generate_response.
            # Example keys: 'bot', 'message', 'interface', 'chat_id', 'interface_path'
            if not hasattr(self, '_current_request_meta'):
                self._current_request_meta = None

            log_debug(f"[selenium] generate_response called with {len(messages) if isinstance(messages, list) else 1} message(s)")
            
            # Log the system prompt if present (for debugging)
            if isinstance(messages, list) and len(messages) > 0:
                for msg in messages:
                    if isinstance(msg, dict) and msg.get("role") == "system":
                        system_content = msg.get("content", "")
                        log_info(f"[selenium] 📋 System prompt sent to LLM:\n{system_content[:500]}...")
                        log_debug(f"[selenium] Full system prompt ({len(system_content)} chars):\n{system_content}")
            
            # Convert messages to text for Selenium interaction
            # Selenium can't use API "system" roles directly, so we need to embed instructions
            if isinstance(messages, list):
                prompt_text = ""
                system_instructions = ""
                
                # Extract system messages and user messages separately
                for msg in messages:
                    if isinstance(msg, dict):
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        
                        if role == "system":
                            system_instructions += content + "\n"
                        else:
                            prompt_text += f"{role}: {content}\n"
                
                # Prepend system instructions to the final prompt
                if system_instructions:
                    prompt_text = system_instructions + "\n---\n" + prompt_text
            else:
                prompt_text = str(messages)

            log_debug(f"[selenium] About to call _execute_complete_workflow with prompt ({len(prompt_text)} chars)")
            log_debug(f"[selenium] 📤 FULL PROMPT SENT TO LLM ({len(prompt_text)} chars):\n{prompt_text}")

            # Lazy driver initialization - create only when first actual request comes in
            if self.driver is None:
                log_info(f"[selenium] 🚀 First use of {self.component_name} - creating shared driver")
                try:
                    shared_driver = await self._get_shared_driver()
                    self.driver = shared_driver  # Assign to instance for compatibility
                    log_info(f"[selenium] ✅ Driver ready for {self.component_name}")
                except Exception as driver_err:
                    # Notify user/admin and return an error early
                    try:
                        notify_msg = "Impossibile avviare il browser per il motore LLM (errore Selenium). Controlla i log e riprova."
                        log_warning(f"[selenium][driver_error] {notify_msg} err={driver_err}")
                        if getattr(self, '_notify_fn', None):
                            try:
                                self._notify_fn(notify_msg)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    raise driver_err

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
                try:
                    # Use the async recovery helper inside async context
                    await self._ensure_driver_responsive_or_restart()
                except Exception:
                    # Fallback to direct recreate if helper failed
                    shared_driver = await self._get_shared_driver()
                    self.driver = shared_driver
                    log_info(f"[selenium] ✅ Fresh driver ready for {self.component_name}")
                    window_count = len(self.driver.window_handles)
                    log_info(f"[selenium] Using fresh shared driver with {window_count} window(s)")


            # Check for double-prompt split condition (only when not explicitly skipping)
            try:
                # Determine whether we must split into PART1/PART2
                # Decide about double prompt using pre_reduction_size (if provided)
                if self._should_double_prompt(prompt_text, pre_reduction_size=pre_reduction_size):
                    log_info("[selenium] 🔀 Double-prompt condition met — executing PART1/PART2 workflow")
                    response = await asyncio.to_thread(self._execute_double_prompt_workflow, prompt_text)
                else:
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

    # === DOUBLE PROMPT HELPERS ===
    def _should_double_prompt(self, prompt_text: str, pre_reduction_size: int | None = None) -> bool:
        """Return True if we should split this prompt into PART1/PART2.

        Conditions:
        - Split feature enabled via `SELENIUM_SPLIT_PROMPT_PARTS` (> 1)
        - Not currently sending PART2 (internal flag)
        - prompt_text length > model limit (character limit)
        """
        try:
            max_parts = int(config_registry.get_value("SELENIUM_SPLIT_PROMPT_PARTS", DEFAULT_SPLIT_PROMPT_PARTS))
        except Exception:
            max_parts = DEFAULT_SPLIT_PROMPT_PARTS

        if max_parts <= 1:
            return False

        # If we're explicitly skipping (we are in PART2) do not split again
        if getattr(self, '_skip_double_prompt_for_this_send', False):
            log_debug('[selenium] skipping double-prompt because _skip_double_prompt_for_this_send=True')
            return False

        # Determine model limit
        try:
            # Prefer engine-provided limit
            lim = self._get_model_char_limit(self._get_current_model_name())
        except Exception:
            lim = _active_selenium_max_prompt_chars

        try:
            size = len(prompt_text)
        except Exception:
            size = 0

        # If a pre_reduction_size is provided, prefer it to decide splitting (ensures split happens before any minification)
        if pre_reduction_size is not None:
            try:
                pre_size_int = int(pre_reduction_size)
                if pre_size_int > 0:
                    size_to_check = pre_size_int
                else:
                    size_to_check = size
            except Exception:
                size_to_check = size
        else:
            size_to_check = size

        log_debug(f"[selenium] Double-prompt decision check: size_to_check={size_to_check}, model_limit={lim}")
        if lim and size_to_check > lim:
            # Only split when the prompt contains JSON context we can safely extract.
            # Prevent accidental splitting of plain text user messages into PART1.
            try:
                # Use the same parsing logic as _split_prompt_text_into_parts to detect JSON
                json_candidate = None
                if "\n---\n" in prompt_text:
                    idx = prompt_text.find("\n---\n")
                    remainder = prompt_text[idx + len("\n---\n"):].strip()
                    brace_idx = remainder.find("{")
                    json_candidate = remainder[brace_idx:] if brace_idx != -1 else remainder
                else:
                    first_brace = prompt_text.find("{")
                    json_candidate = prompt_text[first_brace:] if first_brace != -1 else None

                if json_candidate:
                    parsed = json.loads(json_candidate)
                    # Only enable double-prompt when we find explicit context keys
                    split_keys = (
                        'history_current_chat', 'history_recent', 'memories', 'thoughts', 'tags_placeholder',
                        # legacy keys (backward compatible)
                        'current_chat_history', 'chat_history', 'ai_diary', 'diary', 'diary_entries', 'recent_entries',
                    )
                    if isinstance(parsed, dict) and (
                        'context' in parsed or
                        any(k in parsed for k in split_keys)
                    ):
                        log_debug(f"[selenium] prompt length {size} exceeds model limit {lim} and contains context keys, enabling double-prompt")
                        return True
                    else:
                        log_debug(f"[selenium] prompt length > limit but no explicit context found in JSON; skipping double-prompt")
                        return False
                else:
                    log_debug("[selenium] prompt length > limit but no JSON found; skipping double-prompt")
                    return False
            except Exception as e:
                log_warning(f"[selenium] Error while checking for JSON context before split: {e}; skipping double-prompt")
                return False

        return False

    def _split_prompt_text_into_parts(self, prompt_text: str) -> tuple[str, str]:
        """Try to split prompt_text into PART1 (context) and PART2 (main prompt).

        Strategy:
        1. If prompt_text contains a JSON body (common with build_json_prompt), parse it and extract 'context' for PART1.
           - PART1 will be header + JSON of context only
           - PART2 will be the original prompt with 'context' replaced by an empty object (or minimal persona kept)
        2. If parsing fails, fallback to simple character-based split with PART1 = first half and PART2 = remainder.
        """
        try:
            # Try to find JSON part (after the canonical separator) and parse it
            json_start = None
            if "\n---\n" in prompt_text:
                idx = prompt_text.find("\n---\n")
                # JSON likely after the separator
                remainder = prompt_text[idx + len("\n---\n"):].strip()
                # Find first brace
                brace_idx = remainder.find("{")
                if brace_idx != -1:
                    json_candidate = remainder[brace_idx:]
                else:
                    json_candidate = remainder
            else:
                # No separator - try to find the first JSON object in the string
                first_brace = prompt_text.find("{")
                if first_brace != -1:
                    json_candidate = prompt_text[first_brace:]
                else:
                    json_candidate = None

            parsed = None
            if json_candidate:
                parsed = None
                # Try parsing raw candidate
                try:
                    parsed = json.loads(json_candidate)
                except Exception:
                    # Attempt to extract from first '{' to last '}'
                    try:
                        first = json_candidate.find('{')
                        last = json_candidate.rfind('}')
                        if first != -1 and last != -1 and last > first:
                            sub = json_candidate[first:last+1]
                            parsed = json.loads(sub)
                        else:
                            # Try wrapping the candidate with braces in case outer braces were omitted
                            wrapped = '{' + json_candidate.strip().strip('{}') + '}'
                            parsed = json.loads(wrapped)
                    except Exception:
                        parsed = None

            # If parsed JSON with context
            # parsed JSON may include context in different shapes. We detect:
            # - a 'context' key (preferred)
            # - or top-level keys like history/memories
            if isinstance(parsed, dict):
                # Extract only the known, safe-to-externalize context lists.
                context_obj = {}

                # Prefer the 'context' dict if present
                source = parsed.get('context') if isinstance(parsed.get('context'), dict) else parsed

                # Helper: copy keys if present
                def copy_if_present(dst, src, key):
                    if key in src:
                        dst[key] = src[key]

                # Canonical keys
                for k in ('history_current_chat', 'history_recent', 'memories', 'thoughts', 'tags_placeholder'):
                    copy_if_present(context_obj, source, k)
                # Legacy keys (backward compatible)
                for k in ('current_chat_history', 'chat_history', 'ai_diary', 'diary_entries', 'diary'):
                    copy_if_present(context_obj, source, k)

                # Only create PART1 if we actually found memory/chat keys
                if context_obj:
                    # Build PART1 header and payload
                    part1_header = (
                        "[INTERNAL-PART1] This message contains CONTEXT for memory persistence. "
                        "Read it carefully and keep it available for subsequent messages. "
                        "Do NOT act on this message. Reply ONLY with an empty JSON object: {}"
                    )

                    # Build PART1 payload containing ONLY extracted context lists
                    part1_payload = dict(context_obj)

                    part1_text = part1_header + "\n\n" + json.dumps(part1_payload)

                    # Build PART2 by removing the context keys from the original parsed prompt
                    parsed_part2 = dict(parsed)  # shallow copy

                    # If the original had a 'context' dict, only remove the keys we moved to PART1
                    if 'context' in parsed_part2 and isinstance(parsed_part2['context'], dict):
                        for k in context_obj.keys():
                            if k in parsed_part2['context']:
                                parsed_part2['context'][k] = []

                    # Also remove top-level occurrences of these keys if present
                    for k in context_obj.keys():
                        if k in parsed_part2:
                            parsed_part2[k] = []

                    part2_text = json.dumps(parsed_part2)

                    # Apply minification to PART2 so it fits model limits better
                    try:
                        model_limit = self._get_model_char_limit(self._get_current_model_name())
                        # Only attempt reduction if model limit is set and part2 exceeds it
                        if model_limit and len(part2_text) > model_limit:
                            reduced = reduce_json_text_for_transmission(part2_text, model_limit)
                            if isinstance(reduced, str) and len(reduced) < len(part2_text):
                                log_info(f"[selenium] PART2 minified from {len(part2_text)} to {len(reduced)} chars")
                                part2_text = reduced
                    except Exception as e:
                        log_warning(f"[selenium] Could not minify PART2: {e}")

                    return part1_text, part2_text

        except Exception as e:
            log_debug(f"[selenium] _split_prompt_text_into_parts JSON parse fallback: {e}")

        # Fallback: if we couldn't parse a JSON context, DO NOT attempt to naively split user text.
        # Returning an empty PART1 and the full prompt as PART2 avoids accidental inclusion of
        # user messages inside PART1. _should_double_prompt should normally prevent calling this.
        try:
            size = len(prompt_text)
            mid = max(1, size // 2)
            # Return an empty context PART1 and the full prompt as PART2
            empty_header = (
                "[INTERNAL-PART1] This message is intended for CONTEXT only. "
                "If present, read/store history/memories. Reply ONLY with {}."
            )
            # Empty context payload
            return empty_header + "\n\n{}", prompt_text
        except Exception:
            # As a last resort, return original as part2 and empty part1
            return "{}", prompt_text

    def _split_prompt_text_into_n_parts(self, prompt_text: str, max_parts: int) -> list[str]:
        """Split prompt into up to N parts.

        - 1..(N-1): context-only parts (expecting `{}`), used to persist memory/context
        - N: final prompt with context keys emptied

        `max_parts` is a hard cap; if everything fits in fewer parts, fewer parts are returned.
        """
        try:
            max_parts_int = int(max_parts)
        except Exception:
            max_parts_int = DEFAULT_SPLIT_PROMPT_PARTS

        if max_parts_int <= 1:
            return [prompt_text]

        # Keep compatibility for the classic 2-part behavior
        if max_parts_int == 2:
            p1, p2 = self._split_prompt_text_into_parts(prompt_text)
            return [p1, p2]

        try:
            # Parse prompt JSON body (same approach as _split_prompt_text_into_parts)
            if "\n---\n" in prompt_text:
                idx = prompt_text.find("\n---\n")
                remainder = prompt_text[idx + len("\n---\n"):].strip()
                brace_idx = remainder.find("{")
                json_candidate = remainder[brace_idx:] if brace_idx != -1 else remainder
            else:
                first_brace = prompt_text.find("{")
                json_candidate = prompt_text[first_brace:] if first_brace != -1 else None

            parsed = None
            if json_candidate:
                try:
                    parsed = json.loads(json_candidate)
                except Exception:
                    try:
                        first = json_candidate.find('{')
                        last = json_candidate.rfind('}')
                        if first != -1 and last != -1 and last > first:
                            parsed = json.loads(json_candidate[first:last + 1])
                        else:
                            wrapped = '{' + json_candidate.strip().strip('{}') + '}'
                            parsed = json.loads(wrapped)
                    except Exception:
                        parsed = None

            if not isinstance(parsed, dict):
                p1, p2 = self._split_prompt_text_into_parts(prompt_text)
                return [p1, p2]

            source = parsed.get('context') if isinstance(parsed.get('context'), dict) else parsed

            extracted: dict[str, list] = {}
            for k in (
                "history_current_chat", "history_recent", "memories", "thoughts", "tags_placeholder",
                # legacy
                "current_chat_history", "chat_history", "ai_diary", "diary_entries", "diary",
            ):
                v = source.get(k)
                if isinstance(v, list) and v:
                    extracted[k] = v

            if not extracted:
                p1, p2 = self._split_prompt_text_into_parts(prompt_text)
                return [p1, p2]

            # Build final prompt by clearing extracted keys (no minification here)
            parsed_final = dict(parsed)
            if 'context' in parsed_final and isinstance(parsed_final['context'], dict):
                for k in extracted.keys():
                    if k in parsed_final['context']:
                        parsed_final['context'][k] = []
            for k in extracted.keys():
                if k in parsed_final:
                    parsed_final[k] = []
            final_text = json.dumps(parsed_final)

            # Determine packing target based on model limit (before any reduction)
            try:
                model_limit = int(self._get_model_char_limit(self._get_current_model_name()))
            except Exception:
                model_limit = int(_active_selenium_max_prompt_chars)

            per_part_limit = max(2000, int(model_limit * 0.9))

            keys_order = [
                "history_current_chat", "history_recent", "memories", "thoughts", "tags_placeholder",
                # legacy
                "current_chat_history", "chat_history", "ai_diary", "diary_entries", "diary",
            ]
            stream: list[tuple[str, object]] = []
            for k in keys_order:
                for item in (extracted.get(k) or []):
                    stream.append((k, item))

            def payload_size(payload: dict[str, list]) -> int:
                try:
                    return len(json.dumps(payload))
                except Exception:
                    return 10**9

            context_payloads: list[dict[str, list]] = []
            current_payload: dict[str, list] = {}
            for k, item in stream:
                current_payload.setdefault(k, []).append(item)

                if payload_size(current_payload) > per_part_limit:
                    # If we have more than one item in this payload, move the last item into a new payload.
                    if len(current_payload.get(k, [])) > 1:
                        last = current_payload[k].pop()
                        context_payloads.append(current_payload)
                        current_payload = {k: [last]}
                    else:
                        # Single huge item; accept it as-is to avoid infinite splitting.
                        context_payloads.append(current_payload)
                        current_payload = {}

            if current_payload:
                context_payloads.append(current_payload)

            # Cap to max_parts-1 by merging overflow into the last payload
            max_context_parts = max_parts_int - 1
            if len(context_payloads) > max_context_parts:
                kept = context_payloads[:max_context_parts]
                for payload in context_payloads[max_context_parts:]:
                    for k, items in payload.items():
                        kept[-1].setdefault(k, []).extend(items)
                context_payloads = kept

            total_parts = len(context_payloads) + 1
            parts: list[str] = []
            for idx, payload in enumerate(context_payloads, start=1):
                header = (
                    f"[INTERNAL-PART{idx}/{total_parts}] This message contains CONTEXT for memory persistence. "
                    "Read it carefully and keep it available for subsequent messages. "
                    "Do NOT act on this message. Reply ONLY with an empty JSON object: {}"
                )
                parts.append(header + "\n\n" + json.dumps(payload))

            parts.append(final_text)
            return parts

        except Exception as e:
            log_debug(f"[selenium] _split_prompt_text_into_n_parts fallback: {e}")
            p1, p2 = self._split_prompt_text_into_parts(prompt_text)
            return [p1, p2]

    def _execute_double_prompt_workflow(self, prompt_text: str) -> str:
        """Execute PART1 then PART2 sequentially, ignoring PART1's content for actions.

        PART1: send only context/memories and instruct to reply with {}.
        PART2: send main prompt (without the context, relying on PART1) and return PART2's response.

        The internal flag _skip_double_prompt_for_this_send is used to avoid re-splitting PART2.
        """
        try:
            max_parts = int(config_registry.get_value("SELENIUM_SPLIT_PROMPT_PARTS", DEFAULT_SPLIT_PROMPT_PARTS))
        except Exception:
            max_parts = DEFAULT_SPLIT_PROMPT_PARTS

        parts = self._split_prompt_text_into_n_parts(prompt_text, max_parts=max_parts)
        if len(parts) < 2:
            p1, p2 = self._split_prompt_text_into_parts(prompt_text)
            parts = [p1, p2]

        context_parts = parts[:-1]
        final_part_text = parts[-1]

        # Send PART1 and wait for a response (we ignore content, but we log parsing/results)
        # Use a shorter processing wait for PART1 to avoid long delays before sending PART2
        try:
            part1_processing_timeout = int(config_registry.get_value("SELENIUM_PART1_PROCESSING_TIMEOUT", 8))
        except Exception:
            part1_processing_timeout = 8
        # Retry each context PART up to CORRECTOR_RETRIES; any response is enough to proceed.
        # CORRECTOR_RETRIES may be a ConfigVar; ensure int semantics
        max_retries = int(CORRECTOR_RETRIES) if 'CORRECTOR_RETRIES' in globals() else 3
        try:
            part1_stable_grace = float(config_registry.get_value("SELENIUM_PART1_RESPONSE_STABLE_GRACE", DEFAULT_PART1_RESPONSE_STABLE_GRACE))
        except Exception:
            part1_stable_grace = DEFAULT_PART1_RESPONSE_STABLE_GRACE

        for part_index, context_text in enumerate(context_parts, start=1):
            try:
                part_len = len(context_text) if isinstance(context_text, str) else None
            except Exception:
                part_len = None

            log_info(f"[selenium] PART{part_index}/{len(parts)} -> sending context part to LLM (expecting JSON {{}}) size={part_len}")
            part_resp = None

            for attempt in range(1, max_retries + 1):
                log_info(f"[selenium] PART{part_index} attempt {attempt}/{max_retries}")
                try:
                    # Pre-check and attempt sync recovery before each PART attempt
                    try:
                        self._ensure_driver_responsive_or_restart_sync()
                    except FrozenDriverError as e:
                        log_warning(f"[selenium] PART{part_index} pre-check recovery raised: {e}")

                    attempt_start = time.time()
                    part_resp = self._execute_complete_workflow(
                        context_text,
                        processing_max_wait=part1_processing_timeout,
                        stabilize_max_wait=part1_processing_timeout,
                        stabilize_no_change_grace=part1_stable_grace,
                        post_send_confirm_timeout=1.5,
                    )
                    attempt_elapsed = time.time() - attempt_start
                    log_debug(f"[selenium] PART{part_index} attempt {attempt} completed in {attempt_elapsed:.1f}s")
                except Exception as e:
                    log_warning(f"[selenium] PART{part_index} attempt {attempt} failed with error: {e}")
                    if attempt < max_retries:
                        time.sleep(1)
                        continue
                    part_resp = None
                    break

                if isinstance(part_resp, str) and part_resp.strip() != "":
                    try:
                        parsed = json.loads(part_resp)
                    except Exception:
                        parsed = None

                    if parsed == {}:
                        log_info(f"[selenium] PART{part_index} attempt {attempt} parsed as {{}} (OK)")
                    else:
                        log_warning(f"[selenium] PART{part_index} attempt {attempt} response did not strictly parse as {{}} - proceeding anyway")
                    break

                log_warning(f"[selenium] PART{part_index} attempt {attempt} returned empty response; retrying...")
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                log_warning(f"[selenium] PART{part_index} exhausted retries without valid response; proceeding")

        # Send PART2 - ensure we do not re-split
        try:
            part2_len = len(final_part_text) if isinstance(final_part_text, str) else None
        except Exception:
            part2_len = None
        log_info(f"[selenium] PART{len(parts)}/{len(parts)} -> sending main prompt (is_part2=True) size_before_minify={part2_len}")
        self._skip_double_prompt_for_this_send = True
        # Give the browser a very short grace to process previous message before sending PART2
        try:
            time.sleep(0.1)
        except Exception:
            pass

        # Execute workflow with driver recovery retries
        try:
            try:
                recovery_retries = int(config_registry.get_value("SELENIUM_DRIVER_RECOVERY_RETRIES", DEFAULT_DRIVER_RECOVERY_RETRIES))
            except Exception:
                recovery_retries = DEFAULT_DRIVER_RECOVERY_RETRIES

            last_exception = None
            for attempt in range(recovery_retries + 1):
                try:
                    # Quick health-check and optional recovery before attempting the workflow
                    try:
                        self._ensure_driver_responsive_or_restart_sync()
                    except FrozenDriverError as e:
                        log_warning(f"[selenium] Pre-workflow driver recovery raised: {e}")
                        # continue to attempt workflow with fresh driver

                    resp = self._execute_complete_workflow(final_part_text)
                    log_info("[selenium] PART2 response received — treating as final response for action parsing/corrector flow")
                    return resp
                except (FrozenDriverError, WebDriverException, ReadTimeoutError, TimeoutError) as e:
                    last_exception = e
                    log_warning(f"[selenium] Workflow attempt {attempt + 1} failed due to driver issue: {e}")
                    # Try to recover and then retry
                    try:
                        self._ensure_driver_responsive_or_restart_sync()
                    except Exception as rec_e:
                        log_error(f"[selenium] Failed to recover driver after workflow failure: {rec_e}")
                        # If we cannot recover, break and propagate
                        break
                    # small pause before retry
                    try:
                        time.sleep(0.2)
                    except Exception:
                        pass
                    continue

            # If we get here, all attempts failed
            if last_exception is not None:
                log_error(f"[selenium] All {recovery_retries + 1} workflow attempts failed: {last_exception}")
                raise last_exception

        finally:
            # Always reset the flag to avoid residual state
            self._skip_double_prompt_for_this_send = False

    def _execute_complete_workflow(
        self,
        prompt_text: str,
        processing_max_wait: int | None = None,
        stabilize_max_wait: int | None = None,
        stabilize_no_change_grace: float | None = None,
        post_send_confirm_timeout: float | None = None,
    ) -> str:
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
            send_result = self._send_prompt_with_confirmation(
                textarea,
                prompt_text,
                processing_max_wait=processing_max_wait,
                post_send_confirm_timeout=post_send_confirm_timeout,
            )
            if send_result is False:
                return "❌ Failed to send prompt"
            if isinstance(send_result, str) and send_result.startswith('❌'):
                return send_result

            # Handle response choice if applicable (e.g., ChatGPT offers two responses)
            self._handle_response_choice(self.driver)

            # Wait for response to stabilize (text stops growing for N seconds)
            # Use a shorter stabilization timeout for PART1 when requested
            if stabilize_max_wait is not None:
                grace = stabilize_no_change_grace if stabilize_no_change_grace is not None else DEFAULT_RESPONSE_STABLE_GRACE
                response = self.wait_until_response_stabilizes(
                    self.driver,
                    max_total_wait=stabilize_max_wait,
                    no_change_grace=grace,
                )
            else:
                try:
                    default_grace = float(config_registry.get_value("SELENIUM_RESPONSE_STABLE_GRACE", DEFAULT_RESPONSE_STABLE_GRACE))
                except Exception:
                    default_grace = DEFAULT_RESPONSE_STABLE_GRACE
                response = self.wait_until_response_stabilizes(self.driver, no_change_grace=default_grace)

            # Log the full response for debugging
            if response:
                log_debug(f"[selenium] 📨 FULL LLM RESPONSE ({len(response)} chars):\n{response}")
            else:
                log_warning("[selenium] Received empty response from LLM")

            # Engine-specific UI reset hook: allow engines to implement a UI-level reset
            # (e.g., opening a new chat in Gemini) when appropriate. The hook should return
            # a string starting with '❌' to indicate a retry and cause upper layers to consume
            # one retry attempt.
            try:
                hook_res = None
                try:
                    hook = getattr(self, '_engine_ui_reset_hook', None)
                    if callable(hook):
                        hook_res = hook(response)
                except Exception as e:
                    log_debug(f"[selenium] _engine_ui_reset_hook failed: {e}")

                if isinstance(hook_res, str) and hook_res.startswith('❌'):
                    return hook_res
            except Exception:
                pass

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