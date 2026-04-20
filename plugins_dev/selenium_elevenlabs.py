from __future__ import annotations

import asyncio
import os
import tempfile
import time
import shutil
import re
import subprocess
from typing import Any, Dict, List, Tuple

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service

from core.core_initializer import register_plugin
from core.logging_utils import (
    log_debug,
    log_error,
    log_info,
    log_warning,
    _LOG_DIR,
)

# Optional imports with fallbacks for test environments
try:
    from core.notifier import notify_trainer
except Exception:

    def notify_trainer(message: str) -> None:  # pragma: no cover - fallback
        log_warning("[selenium_elevenlabs] notifier not available")


class SeleniumElevenLabsPlugin:
    """Generate speech via ElevenLabs using Selenium and dispatch audio."""

    def __init__(self) -> None:
        register_plugin("selenium_elevenlabs", self)
        log_info("[selenium_elevenlabs] Plugin initialized")

    # === Action metadata ===
    def get_supported_action_types(self) -> List[str]:
        return ["speech_selenium_elevenlabs"]

    def get_supported_actions(self) -> Dict[str, Dict[str, Any]]:
        return {
            "speech_selenium_elevenlabs": {
                "description": "Generate speech via ElevenLabs and send audio to destinations",
                "required_fields": ["message", "destinations"],
                "optional_fields": [],
                "restricted": True,
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if action_name != "speech_selenium_elevenlabs":
            return {}
        return {
            "description": "Convert text into speech using ElevenLabs and send it to one or more chats",
            "payload": {
                "message": "Hello world",
                "destinations": [
                    {
                        "interface": "telegram_bot",
                        "chat_id": 123456,
                        "thread_id": 7,
                    }
                ],
            },
        }

    # === Validation ===
    @staticmethod
    def validate_payload(action_type: str, payload: Dict[str, Any]) -> List[str]:
        if action_type != "speech_selenium_elevenlabs":
            return []

        errors: List[str] = []
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            errors.append("payload.message must be a non-empty string")
        elif len(message) > 3000:
            errors.append("payload.message exceeds 3000 characters")

        destinations = payload.get("destinations")
        if not isinstance(destinations, list) or not destinations:
            errors.append("payload.destinations must be a non-empty list")
        else:
            for idx, dest in enumerate(destinations):
                if not isinstance(dest, dict):
                    errors.append(f"destinations[{idx}] must be a dict")
                    continue
                if not dest.get("interface"):
                    errors.append(f"destinations[{idx}].interface is required")
                if dest.get("chat_id") is None:
                    errors.append(f"destinations[{idx}].chat_id is required")
        return errors

    # === Execution ===
    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> None:
        payload = action.get("payload", {})
        text = payload.get("message", "")
        destinations = payload.get("destinations", [])
        log_info(f"[selenium_elevenlabs] Executing speech action for {len(text)} chars")

        mp3_path = await self._generate_speech(text)

        for dest in destinations:
            await self._dispatch_audio(dest, mp3_path)

        credits = await self._get_remaining_credits()
        notify_trainer(f"ElevenLabs credits remaining: {credits}")

    # === Internal helpers ===
    async def _generate_speech(self, text: str) -> str:
        """Generate speech using ElevenLabs web interface via Selenium."""

        download_dir = tempfile.mkdtemp(prefix="elevenlabs_")

        def _run() -> Tuple[str, str]:
            options = uc.ChromeOptions()
            if os.getenv("synth_SELENIUM_HEADLESS", "1") == "1":
                options.add_argument("--headless=new")
            prefs = {
                "download.default_directory": download_dir,
                "download.prompt_for_download": False,
                "safebrowsing.enabled": True,
            }
            options.add_experimental_option("prefs", prefs)

            os.makedirs(_LOG_DIR, exist_ok=True)
            chromium_log = os.path.join(_LOG_DIR, "chromium.log")
            chromedriver_log = os.path.join(_LOG_DIR, "chromedriver.log")
            service = Service(log_path=chromedriver_log, service_args=["--verbose"])
            options.add_argument("--enable-logging")
            options.add_argument("--log-level=0")
            options.add_argument(f"--log-file={chromium_log}")
            log_debug(
                f"[selenium_elevenlabs] Chromium log -> {chromium_log}, chromedriver log -> {chromedriver_log}"
            )

            chromium_binary = (
                shutil.which("chromium")
                or shutil.which("chromium-browser")
                or "/usr/bin/chromium"
            )
            log_debug(f"[selenium_elevenlabs] Using Chromium binary: {chromium_binary}")
            try:
                output = subprocess.check_output(
                    [chromium_binary, "--version"], text=True
                )
                match = re.search(r"(\d+)\.", output)
                chromium_major = int(match.group(1)) if match else None
            except Exception:
                chromium_major = None
            driver = uc.Chrome(
                options=options,
                service=service,
                headless=False,
                use_subprocess=False,
                version_main=chromium_major,
            )
            wait = WebDriverWait(driver, 60)
            try:
                driver.get("https://elevenlabs.io/app/speech-synthesis/text-to-speech")

                # Login if necessary
                if "login" in driver.current_url.lower():
                    email = os.getenv("ELEVENLABS_EMAIL", "")
                    password = os.getenv("ELEVENLABS_PASSWORD", "")
                    wait.until(
                        EC.presence_of_element_located((By.NAME, "email"))
                    ).send_keys(email)
                    wait.until(
                        EC.presence_of_element_located((By.NAME, "password"))
                    ).send_keys(password)
                    driver.find_element(
                        By.XPATH, "//button[contains(., 'Sign in')]"
                    ).click()
                    time.sleep(2)

                # Skip possible onboarding dialogs
                for text_btn in ["Skip", "Creative Platform"]:
                    try:
                        btn = driver.find_element(
                            By.XPATH, f"//*[contains(text(), '{text_btn}')]"
                        )
                        btn.click()
                        time.sleep(1)
                    except Exception:
                        pass

                try:
                    tts_btn = wait.until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//p[text()='Text to Speech']")
                        )
                    )
                    tts_btn.click()
                except Exception:
                    pass

                textarea = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "textarea[data-testid='tts-editor']")
                    )
                )
                textarea.clear()
                textarea.send_keys(text)

                generate_btn = driver.find_element(
                    By.CSS_SELECTOR, "button[aria-label^='Generate speech']"
                )
                generate_btn.click()

                # Wait for loading indicator to disappear
                try:
                    wait.until(
                        EC.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                "button[aria-label='Loading'][data-loading='true']",
                            )
                        )
                    )
                    wait.until_not(
                        EC.presence_of_element_located(
                            (
                                By.CSS_SELECTOR,
                                "button[aria-label='Loading'][data-loading='true']",
                            )
                        )
                    )
                except Exception:
                    pass

                download_btn = wait.until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, "button[aria-label='Download']")
                    )
                )
                download_btn.click()

                mp3_path = _wait_for_file(download_dir, ".mp3")

                credits = "unknown"
                try:
                    credits_el = driver.find_element(
                        By.XPATH, "//span[contains(text(),'credits remaining')]"
                    )
                    credits = credits_el.text
                except Exception:
                    pass

                return mp3_path, credits
            finally:
                driver.quit()

        def _wait_for_file(directory: str, extension: str, timeout: int = 120) -> str:
            end = time.time() + timeout
            while time.time() < end:
                for name in os.listdir(directory):
                    if name.endswith(extension) and not name.endswith(".crdownload"):
                        full = os.path.join(directory, name)
                        if os.path.getsize(full) > 0:
                            return full
                time.sleep(0.5)
            raise RuntimeError("Download timeout")

        mp3_path, credits = await asyncio.to_thread(_run)
        self._credits = credits
        return mp3_path

    async def _dispatch_audio(self, dest: Dict[str, Any], file_path: str) -> None:
        interface_name = dest.get("interface")
        chat_id = dest.get("chat_id")
        thread_id = dest.get("thread_id")
        if not interface_name or chat_id is None:
            return
        try:
            from core.core_initializer import INTERFACE_REGISTRY
        except Exception:
            log_warning("[selenium_elevenlabs] INTERFACE_REGISTRY unavailable")
            return
        iface = INTERFACE_REGISTRY.get(interface_name)
        if not iface or not hasattr(iface, "send_audio"):
            log_warning(f"[selenium_elevenlabs] Interface {interface_name} unavailable")
            return
        payload = {"audio": file_path, "target": {"chat_id": chat_id}}
        if thread_id is not None:
            payload["target"]["thread_id"] = thread_id
        try:
            await iface.send_audio(payload)
        except Exception as e:
            log_error(f"[selenium_elevenlabs] Failed to send audio: {e}")

    async def _get_remaining_credits(self) -> str:
        return getattr(self, "_credits", "unknown")


PLUGIN_CLASS = SeleniumElevenLabsPlugin
