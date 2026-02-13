from core.selenium_llm_base import SeleniumLLMBase
from core.logging_utils import log_debug
from selenium.webdriver.common.by import By

# StepFun configuration
SERVICE_URL = "https://stepfun.ai/"
MODEL_CONFIG_VAR = "STEPFUN_MODEL"
DEFAULT_MODEL = "stepfun-default"

# Practical character limits for StepFun web UI
MODEL_LIMITS_MAP = {
    "stepfun-default": 20000,
    "unlogged": 1000,  # free / unlogged prompt limit (strict)
    "default": 20000,
}


class SeleniumStepFunPlugin(SeleniumLLMBase):
    display_name = "Selenium StepFun"

    def __init__(self, notify_fn=None):
        super().__init__(
            config={"service_url": SERVICE_URL, "interface_name": "stepfun"},
            notify_fn=notify_fn,
        )

        # Model configuration used by the base class
        self.model_limits_map = MODEL_LIMITS_MAP
        self.model_config_var = MODEL_CONFIG_VAR
        self.default_model = DEFAULT_MODEL
        self._update_interface_limits()

        # UI selectors (primary first, fallbacks afterwards)
        # Prompt textarea (exact selector provided by user)
        self.selectors["prompt_area"] = [
            "#«rf» > div > div > div > div.relative.flex-1.flex.flex-col.overflow-hidden > div > div.flex.justify-center > div > div.sticky.top-0.z-\\[8\\].pt-8.sh\\:pt-4 > div.relative.w-full.transition-all.duration-300.ease-out.h-\\[120px\\] > div > div > div.w-full.flex.flex-col.relative > div.pt-4.px-4.pb-3 > div > div.flex.w-full.align-middle > textarea",
            "textarea.Publisher_textarea__pMX9t",
            "textarea[placeholder*='What do you want to know']",
            "textarea[placeholder*='What do you want to know?']",
            "textarea",
        ]

        # Send / action button (UI shares location with stop button sometimes)
        self.selectors["send_button"] = [
            # Primary (explicit user-provided selector for StepFun send button)
            "#«rc» > div > div > div > div.relative.flex-1.flex.flex-col.overflow-hidden > div > div.flex.justify-center > div > div.sticky.top-0.z-\\[8\\].pt-8.sh\\:pt-4 > div.relative.w-full.transition-all.duration-300.ease-out.h-\\[120px\\] > div > div > div.w-full.flex.flex-col.relative > div.flex.justify-between.items-center.pt-2.pb-4.px-4.overflow-auto > div.flex-shrink-0.flex.flex-row.items-center.gap-3 > button",
            # Fallback matching the icon/button class pattern provided by user
            "button.inline-flex.items-center.justify-center",
            # Generic fallbacks (kept for robustness)
            "button[data-testid='send-button']",
            "button[type='submit']",
            "button[aria-label*='Send']",
            "button[title*='Send']",
            "button",
        ]

        # Response text selector (exact provided selector + fallbacks)
        self.selectors["response_text"] = [
            "#contentContainer > div.flex.flex-1.w-full.min-h-0.relative > div > div > div:nth-child(1) > div > div > div.flex.flex-1.flex-col > div > div:nth-child(2) > div > p",
            "div.response, div.message, article p",
            "p",
        ]

        # Modal / popup dismissal (close popup when present on unlogged view)
        self.selectors["modal_dismissal"] = [
            "#radix-«r5» > div > button",
            "button[aria-label*='close']",
            "button[aria-label*='Close']",
            "div[role='dialog'] button",
        ]

        # Login-detection: presence of popup/cta indicates NOT logged in
        self.login_detection_selectors = [
            (By.CSS_SELECTOR, "#radix-«r5» > div > button"),
            (By.CSS_SELECTOR, "a[href*='login']"),
            (
                By.XPATH,
                "//button[contains(text(), 'Log in') or contains(text(), 'Sign in')]",
            ),
        ]

        # Register global limits for this engine
        from core.selenium_llm_base import set_active_selenium_limits

        default_limit = MODEL_LIMITS_MAP.get("default", 20000)
        set_active_selenium_limits(default_limit, "stepfun")

    def _on_selector_success(self, kind: str, selector: str) -> None:
        """Override the dynamic selector-promotion hook as a NO-OP.

        StepFun selectors are promoted statically in this plugin file —
        do not perform runtime reordering.
        """
        return None

    def _ensure_logged_in(self, driver) -> bool:
        """Ensure the user is at StepFun and dismiss the initial unlogged popup if present.

        If the popup (radix) is present we treat the session as "unlogged".
        """
        try:
            current_url = driver.current_url if driver else ""
        except Exception:
            current_url = ""

        log_debug(f"[selenium_stepfun] Checking login status at URL: {current_url}")

        if not current_url.startswith("https://stepfun.ai"):
            try:
                driver.get(SERVICE_URL)
            except Exception as e:
                log_debug(f"[selenium_stepfun] Failed to navigate to StepFun: {e}")
                return False

        # Try to close the unlogged popup if present (selector provided by user)
        try:
            els = driver.find_elements(By.CSS_SELECTOR, "#radix-«r5» > div > button")
            if els:
                try:
                    els[0].click()
                    log_debug("[selenium_stepfun] Closed radix popup (unlogged view)")
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", els[0])
                    except Exception:
                        log_debug(
                            "[selenium_stepfun] Failed to click radix popup close button"
                        )
                # still consider unlogged (popup presence implies unlogged)
                log_debug(
                    "[selenium_stepfun] Detected unlogged popup -> treating as NOT logged in"
                )
                if self._notify_fn:
                    self._notify_fn(
                        "🔐 StepFun appears unlogged (popup closed). Open UI to log in if you need a logged session."
                    )
                return False
        except Exception:
            # Ignore selector failures and proceed to other checks
            pass

        # If we reach here, assume logged in
        log_debug("[selenium_stepfun] No unlogged popup detected; assuming logged in")
        return True

    def get_supported_models(self) -> list:
        return list(MODEL_LIMITS_MAP.keys())

    def get_current_model(self) -> str:
        # If not logged in, use the strict unlogged limit
        if not self.is_user_logged_in():
            log_debug("[selenium_stepfun] User not logged in, using 'unlogged' model")
            return "unlogged"
        return self._get_current_model_name()


PLUGIN_CLASS = SeleniumStepFunPlugin
