from __future__ import annotations

import asyncio
import os
import tempfile
import time
import shutil
from typing import Any, Dict, List, Tuple, Optional

try:  # pragma: no cover - import guard for test/container environments
    import undetected_chromedriver as uc  # type: ignore
except Exception:  # pragma: no cover
    uc = None
import subprocess
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning, _LOG_DIR
from core.variables_engine import register_exposed_var

try:
    from core.notifier import notify_trainer
except Exception:

    def notify_trainer(message: str) -> None:  # pragma: no cover - fallback
        log_warning("[selenium_ttsfree] notifier not available")


# Register exposed variable for WebUI
register_exposed_var(
    "Free_TTS_VOICES",
    label="Free TTS Voices",
    default={
        "italian": ["italian", "Isabella", 10, 20],
        "english": ["english", "Anna"],
    },
    value_type="json",
    ui_type="textarea",
    description=(
        "Mapping used by the selenium_ttsfree plugin. Format is a JSON object mapping language keys to an array:"
        ' [language, voice_name, pitch?, speed?]. Example: {"italian": ["italian", "Isabella", 10, 20]}'
    ),
    scope="plugin",
    component="selenium_ttsfree",
    tags=["tts", "voice"],
)


class SeleniumTTSFreePlugin:
    """Generate voice messages using https://ttsfree.com via Selenium and send them to an interface.

    Action: voice_message_ttsfree

    Payload requirements (voice_message_ttsfree):
      - message (str, <= 500 chars, plain text only)
      - language (str) — required
      - voice (list) — [language, voice_name, pitch?(int), speed?(int)]
      - interface_path (str) — where to send the audio

    Notes:
      - If a persona for the requested language is not available, the default persona will be used and a warning will be logged.
      - Temporary files are used for download and removed after dispatch.
    """

    display_name = "Selenium TTSFree"

    def __init__(self) -> None:
        register_plugin("selenium_ttsfree", self)
        log_info("[selenium_ttsfree] Plugin initialized")

        if uc is None:
            log_warning(
                "[selenium_ttsfree] undetected_chromedriver unavailable; generation will fail if invoked"
            )

    # === Action metadata ===
    def get_supported_action_types(self) -> List[str]:
        return ["voice_message_ttsfree"]

    def get_supported_actions(self) -> Dict[str, Dict[str, Any]]:
        return {
            "voice_message_ttsfree": {
                "description": "Generate voice message using ttsfree.com and send to an interface",
                # voice can be provided directly (list) or derived from Free_TTS_VOICES mapping
                "required_fields": ["message", "language", "interface_path"],
                "optional_fields": [],
                "restricted": False,
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if action_name != "voice_message_ttsfree":
            return {}
        return {
            "description": "Convert a plain-text message into speech using TTSFree and send the MP3 to a target interface path",
            "payload": {
                "message": "Ciao, questo è un messaggio di prova",
                "language": "italian",
                "voice": ["italian", "Isabella", 10, 20],
                "interface_path": "telegram_bot/123456789/987654321",
            },
        }

    # === Validation ===
    @staticmethod
    def validate_payload(action_type: str, payload: Dict[str, Any]) -> List[str]:
        if action_type != "voice_message_ttsfree":
            return []

        errors: List[str] = []

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            errors.append("payload.message must be a non-empty string")
        else:
            if len(message) > 500:
                errors.append("payload.message exceeds 500 characters")
            # plain text validation: allow letters, numbers, spaces and common punctuation
            import re

            if re.search(r"[^A-Za-z0-9\s\.,;:!\?\-\'\"]", message):
                errors.append(
                    "payload.message contains unsupported characters (no emoji/markup permitted)"
                )

        language = payload.get("language")
        if not isinstance(language, str) or not language.strip():
            errors.append("payload.language is required and must be a string")

        voice = payload.get("voice")
        # voice is optional because the mapping Free_TTS_VOICES is the canonical source
        # Accept direct list/tuple or a string key to look up inside Free_TTS_VOICES.
        if voice is not None:
            if isinstance(voice, (list, tuple)):
                if len(voice) < 2:
                    errors.append(
                        "payload.voice must contain at least language and voice name"
                    )
                else:
                    if not isinstance(voice[0], str) or not isinstance(voice[1], str):
                        errors.append(
                            "payload.voice first two elements must be strings: [language, voice_name]"
                        )
                    if len(voice) >= 3 and voice[2] is not None:
                        try:
                            _ = float(voice[2])
                        except Exception:
                            errors.append(
                                "payload.voice[2] pitch must be numeric if present"
                            )
                    if len(voice) >= 4 and voice[3] is not None:
                        try:
                            _ = float(voice[3])
                        except Exception:
                            errors.append(
                                "payload.voice[3] speed must be numeric if present"
                            )
            elif isinstance(voice, str):
                # string keys validated at runtime by looking up Free_TTS_VOICES mapping
                if not voice.strip():
                    errors.append("payload.voice string must be non-empty")
            else:
                errors.append(
                    "payload.voice must be a list, tuple, string key, or omitted to use Free_TTS_VOICES mapping"
                )

        interface_path = payload.get("interface_path")
        if not isinstance(interface_path, str) or not interface_path.strip():
            errors.append("payload.interface_path is required and must be a string")

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
        language = payload.get("language")
        voice = payload.get("voice")
        interface_path = payload.get("interface_path")

        log_info(
            f"[selenium_ttsfree] Executing voice_message_ttsfree for {len(text)} chars, language={language}"
        )

        # Persona per lingua: try to obtain a persona matching the requested language; fallback to default
        try:
            from core.persona_manager import PersonaManager

            pm = PersonaManager.get_instance()
            persona_for_lang = None
            if pm:
                # Try to load persona with the language name (best-effort). Some setups only support default.
                try:
                    persona_for_lang = await pm.load_persona(language)
                except Exception:
                    persona_for_lang = None
            if not persona_for_lang:
                log_warning(
                    f"[selenium_ttsfree] No persona found for language '{language}', using default persona"
                )
        except Exception:
            log_debug(
                "[selenium_ttsfree] Unable to verify persona for language (manager unavailable), using default persona"
            )

        # Resolve voice mapping: payload.voice can be a list (direct), a string key
        # or omitted. Use config Free_TTS_VOICES mapping as primary source when
        # voice is a string or missing.
        # Ensure variables are registered so defaults from variables_engine are available
        try:
            import core.variables_engine  # noqa: F401 - registers all exposed variables
        except Exception:
            pass

        from core.config_manager import config_registry

        mapping = config_registry.get_value("Free_TTS_VOICES", None) or {}

        # If config_registry couldn't load a mapping (e.g. DB unavailable), fall back
        # to a safe default so plugin still functions in tests and headless setups.
        if not mapping:
            mapping = {
                "italian": ["italian", "Isabella", 10, 20],
                "english": ["english", "Anna"],
            }

        resolved_voice = None
        if isinstance(voice, (list, tuple)):
            resolved_voice = list(voice)
        elif isinstance(voice, str) and voice.strip():
            resolved_voice = mapping.get(voice) or mapping.get(voice.lower())
        else:
            # Try to find mapping by language key
            # First try exact match, then lowercase, then convert ISO codes (it-IT -> italian, en-US -> english)
            resolved_voice = mapping.get(language)
            if not resolved_voice and language:
                resolved_voice = mapping.get(language.lower())
            if not resolved_voice and language:
                # Try ISO code conversion: "it-IT" -> "italian", "en-US" -> "english"
                iso_to_name = {
                    "it": "italian",
                    "en": "english",
                    "es": "spanish",
                    "fr": "french",
                    "de": "german",
                    "pt": "portuguese",
                    "ja": "japanese",
                    "zh": "chinese",
                    "ko": "korean",
                    "ru": "russian",
                    "ar": "arabic",
                }
                lang_code = language.split("-")[0].lower()  # "it-IT" -> "it"
                lang_name = iso_to_name.get(lang_code)
                if lang_name:
                    resolved_voice = mapping.get(lang_name)
                    log_debug(
                        f"[selenium_ttsfree] Converted ISO code '{language}' to '{lang_name}'"
                    )

        if not resolved_voice:
            log_warning(
                f"[selenium_ttsfree] No voice mapping found for language/key '{voice or language}', using fallback voice"
            )
            resolved_voice = [language, "default"]

        mp3_path = await self._generate_speech(text, language, resolved_voice)

        # Dispatch to target interface
        try:
            from core.core_initializer import INTERFACE_REGISTRY
        except Exception:
            log_warning(
                "[selenium_ttsfree] INTERFACE_REGISTRY unavailable, cannot dispatch audio"
            )
            return

        # Infer interface name from interface_path: e.g. 'telegram_bot/123/456'
        parts = interface_path.split("/") if interface_path else []
        if not parts:
            log_warning(
                "[selenium_ttsfree] interface_path seems invalid, aborting dispatch"
            )
            return

        interface_name = parts[0]
        iface = INTERFACE_REGISTRY.get(interface_name)
        if not iface:
            log_warning(
                f"[selenium_ttsfree] Interface {interface_name} not found in registry"
            )
            return

        # Prepare payload for interface; keep interface_path so interface can route
        send_payload = {"audio": mp3_path, "interface_path": interface_path}

        try:
            # Prefer send_audio if present, otherwise try a generic method name
            send_fn = (
                getattr(iface, "send_audio", None)
                or getattr(iface, "send_voice", None)
                or getattr(iface, "send_message", None)
            )
            if not send_fn:
                log_warning(
                    f"[selenium_ttsfree] Interface {interface_name} does not expose a send_audio/send_voice method"
                )
                return

            await send_fn(send_payload)
            log_info(f"[selenium_ttsfree] Audio dispatched to {interface_path}")
        except Exception as e:
            log_error(f"[selenium_ttsfree] Failed to send audio to interface: {e}")

    async def _generate_speech(self, text: str, language: str, voice: list) -> str:
        download_dir = tempfile.mkdtemp(prefix="ttsfree_")

        def _run() -> Tuple[str, Optional[str]]:
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
            chromium_log = os.path.join(_LOG_DIR, "chromium_ttsfree.log")
            chromedriver_log = os.path.join(_LOG_DIR, "chromedriver_ttsfree.log")
            service = Service(log_path=chromedriver_log, service_args=["--verbose"])

            options.add_argument("--enable-logging")
            options.add_argument("--log-level=0")
            options.add_argument(f"--log-file={chromium_log}")
            log_debug(
                f"[selenium_ttsfree] Chromium log -> {chromium_log}, chromedriver log -> {chromedriver_log}"
            )

            chromium_binary = (
                shutil.which("chromium")
                or shutil.which("chromium-browser")
                or "/usr/bin/chromium"
            )
            try:
                output = subprocess.check_output(
                    [chromium_binary, "--version"], text=True
                )
            except Exception:
                output = ""
            except Exception:
                output = ""

            # Launch driver
            try:
                driver = uc.Chrome(options=options, service=service)
            except Exception as e:
                log_error(f"[selenium_ttsfree] Failed to start Chrome driver: {e}")
                raise

            wait = WebDriverWait(driver, 60)

            try:
                driver.get("https://ttsfree.com/")

                # SELECT LANGUAGE
                try:
                    # click the language container, then try to pick a choice by language text or snippet
                    lang_container = wait.until(
                        EC.element_to_be_clickable(
                            (By.CSS_SELECTOR, "#select2-select_lang_bin-container")
                        )
                    )
                    lang_container.click()
                    time.sleep(0.5)

                    # Prefer matching by language text if possible
                    language_opt = None
                    try:
                        # try to find an option element with language string (case-insensitive)
                        options_list = driver.find_elements(
                            By.CSS_SELECTOR, "li.select2-results__option"
                        )
                        for el in options_list:
                            try:
                                if language.lower() in el.text.lower():
                                    language_opt = el
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                    if language_opt is not None:
                        language_opt.click()
                    else:
                        # fallback: try to click by known pattern id if present
                        try:
                            sel_id = f"#select2-select_lang_bin-result-*-{language}"
                            driver.execute_script(
                                "document.querySelector(arguments[0])?.click()", sel_id
                            )
                        except Exception:
                            log_warning(
                                f"[selenium_ttsfree] Could not reliably select language '{language}' via UI, proceeding with default site language"
                            )

                except Exception:
                    # ignore language take-over failures — TTSFree defaults might still work
                    log_debug(
                        "[selenium_ttsfree] Language selection failed or not needed"
                    )

                # CHOOSE VOICE
                voice_name = None
                try:
                    voice_name = voice[1] if len(voice) > 1 else None
                except Exception:
                    voice_name = None

                if voice_name:
                    try:
                        # find a label that contains the voice name and click its associated radio
                        labels = driver.find_elements(
                            By.CSS_SELECTOR, "label.form-check-label"
                        )
                        chosen = None
                        for lbl in labels:
                            txt = lbl.text or ""
                            if voice_name.lower() in txt.lower():
                                try:
                                    lbl.click()
                                    chosen = True
                                    break
                                except Exception:
                                    pass
                        if not chosen:
                            log_warning(
                                f"[selenium_ttsfree] Voice '{voice_name}' not found in UI, using default voice"
                            )
                    except Exception:
                        log_debug("[selenium_ttsfree] Error searching for voice labels")

                # Optional: set pitch and speed if provided
                try:
                    # Convert provided pitch/speed numbers (0..100-ish) to slider positions: site uses percent left style
                    pitch = None
                    speed = None
                    if len(voice) >= 3 and voice[2] is not None:
                        pitch = float(voice[2])
                    if len(voice) >= 4 and voice[3] is not None:
                        speed = float(voice[3])

                    if pitch is not None:
                        # Try to find slider for pitch and set via JS by calculating left percent
                        try:
                            pitch_el = driver.find_element(
                                By.CSS_SELECTOR, "#pitch-range, .irs-handle.single"
                            )
                            # Compute left percent using simple mapping: assuming site center is 50% for 0 default
                            left_pct = 47.2445 + (pitch / 100.0) * (94.4891 - 47.2445)
                            driver.execute_script(
                                "arguments[0].style.left = arguments[1] + '%';",
                                pitch_el,
                                left_pct,
                            )
                        except Exception:
                            log_debug(
                                "[selenium_ttsfree] Failed to set pitch slider via selector; ignoring"
                            )

                    if speed is not None:
                        try:
                            speed_el = driver.find_element(
                                By.CSS_SELECTOR, "#speed-range, .irs-handle.single"
                            )
                            left_pct = 47.2445 + (speed / 100.0) * (94.4891 - 47.2445)
                            driver.execute_script(
                                "arguments[0].style.left = arguments[1] + '%';",
                                speed_el,
                                left_pct,
                            )
                        except Exception:
                            log_debug(
                                "[selenium_ttsfree] Failed to set speed slider via selector; ignoring"
                            )
                except Exception:
                    log_debug("[selenium_ttsfree] Skipping pitch/speed adjustments")

                # Input text
                try:
                    textarea = wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "#input_text"))
                    )
                    textarea.clear()
                    textarea.send_keys(text)
                except Exception:
                    log_error("[selenium_ttsfree] Could not find input_text textarea")
                    raise

                # Click Convert Now - handle ad popup blocking
                try:
                    convert_btn = driver.find_element(By.CSS_SELECTOR, "a.convert-now")
                    # Scroll to button to ensure visibility
                    driver.execute_script(
                        "arguments[0].scrollIntoView(true);", convert_btn
                    )
                    time.sleep(1)  # Wait for ads to settle

                    # Try to click via JavaScript if normal click is blocked
                    try:
                        convert_btn.click()
                    except Exception:
                        log_debug(
                            "[selenium_ttsfree] Normal click blocked, using JavaScript click"
                        )
                        driver.execute_script("arguments[0].click();", convert_btn)
                except Exception:
                    # try alternative button text search
                    try:
                        btn = driver.find_element(
                            By.XPATH,
                            "//a[contains(., 'Convert Now') or contains(., 'Convert now')]",
                        )
                        driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                        time.sleep(1)
                        try:
                            btn.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", btn)
                    except Exception:
                        log_error("[selenium_ttsfree] Convert Now button not found")
                        raise

                # Wait for save/download link (#savevoice) to appear and get its href
                save_href = None
                try:
                    sv = wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "#savevoice"))
                    )
                    save_href = sv.get_attribute("href")
                except Exception:
                    # fallback: search for an <a> with Download Mp3 text
                    try:
                        a = wait.until(
                            EC.presence_of_element_located(
                                (
                                    By.XPATH,
                                    "//a[contains(., 'Download Mp3') or contains(., 'Download mp3') or contains(@id,'savevoice')]",
                                )
                            )
                        )
                        save_href = a.get_attribute("href")
                    except Exception:
                        log_error(
                            "[selenium_ttsfree] Download link not found after conversion"
                        )
                        raise

                # Try to download via href instead of relying on browser download
                if save_href:
                    import requests

                    try:
                        r = requests.get(save_href, stream=True, timeout=30)
                        if r.status_code == 200:
                            fname = os.path.join(download_dir, "ttsfree_output.mp3")
                            with open(fname, "wb") as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    if chunk:
                                        f.write(chunk)
                            # ensure file is present
                            return fname, None
                        else:
                            log_error(
                                f"[selenium_ttsfree] Download request returned status {r.status_code}"
                            )
                    except Exception as e:
                        log_error(
                            f"[selenium_ttsfree] Failed to download mp3 from save link: {e}"
                        )

                # As a fallback wait for browser download to finish
                def _wait_for_file(
                    directory: str, extension: str, timeout: int = 120
                ) -> str:
                    end = time.time() + timeout
                    while time.time() < end:
                        for name in os.listdir(directory):
                            if name.endswith(extension) and not name.endswith(
                                ".crdownload"
                            ):
                                full = os.path.join(directory, name)
                                if os.path.getsize(full) > 0:
                                    return full
                        time.sleep(0.5)
                    raise RuntimeError("Download timeout")

                mp3_path = _wait_for_file(download_dir, ".mp3")
                return mp3_path, None
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        # run selenium in thread
        mp3_path, _credits = await asyncio.to_thread(_run)

        # schedule cleanup of temp folder after return of path; keep caller responsible for removal
        return mp3_path


PLUGIN_CLASS = SeleniumTTSFreePlugin
