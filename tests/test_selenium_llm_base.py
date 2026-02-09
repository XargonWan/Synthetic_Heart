import importlib
import sys
import types


def _import_sandboxed_selenium_llm_base():
    # Insert lightweight dummy modules to avoid requiring heavy selenium/uc deps during unit tests
    if 'undetected_chromedriver' not in sys.modules:
        sys.modules['undetected_chromedriver'] = types.ModuleType('undetected_chromedriver')
    if 'selenium' not in sys.modules:
        selenium_mod = types.ModuleType('selenium')
        selenium_mod.webdriver = types.ModuleType('selenium.webdriver')
        # Provide a dummy Remote class used in type annotations
        class Remote:
            pass
        selenium_mod.webdriver.Remote = Remote
        # Create chrome subpackage and expected modules/classes
        chrome_mod = types.ModuleType('selenium.webdriver.chrome')
        chrome_service = types.ModuleType('selenium.webdriver.chrome.service')
        chrome_options = types.ModuleType('selenium.webdriver.chrome.options')
        # Minimal Service and Options classes
        class Service:
            def __init__(self, *args, **kwargs):
                pass

        class Options:
            def __init__(self, *args, **kwargs):
                pass

        chrome_service.Service = Service
        chrome_options.Options = Options

        selenium_mod.webdriver.chrome = chrome_mod
        sys.modules['selenium'] = selenium_mod
        sys.modules['selenium.webdriver'] = selenium_mod.webdriver
        sys.modules['selenium.webdriver.chrome'] = chrome_mod
        sys.modules['selenium.webdriver.chrome.service'] = chrome_service
        sys.modules['selenium.webdriver.chrome.options'] = chrome_options

        # Common submodules and exceptions
        common_by = types.ModuleType('selenium.webdriver.common.by')
        common_keys = types.ModuleType('selenium.webdriver.common.keys')
        action_chains = types.ModuleType('selenium.webdriver.common.action_chains')
        common_exceptions = types.ModuleType('selenium.common.exceptions')

        # Dummy exceptions used by the module
        class NoSuchElementException(Exception):
            pass

        class TimeoutException(Exception):
            pass

        class ElementNotInteractableException(Exception):
            pass

        class SessionNotCreatedException(Exception):
            pass

        class WebDriverException(Exception):
            pass

        class StaleElementReferenceException(Exception):
            pass

        common_exceptions.NoSuchElementException = NoSuchElementException
        common_exceptions.TimeoutException = TimeoutException
        common_exceptions.ElementNotInteractableException = ElementNotInteractableException
        common_exceptions.SessionNotCreatedException = SessionNotCreatedException
        common_exceptions.WebDriverException = WebDriverException
        common_exceptions.StaleElementReferenceException = StaleElementReferenceException

        # Minimal By class used in the code (e.g., By.CSS_SELECTOR)
        class By:
            CSS_SELECTOR = 'css selector'
            ID = 'id'
            XPATH = 'xpath'

        common_by.By = By
        sys.modules['selenium.webdriver.common.by'] = common_by
        # Minimal Keys class
        class Keys:
            ENTER = 'ENTER'
            CONTROL = 'CONTROL'

        common_keys.Keys = Keys
        sys.modules['selenium.webdriver.common.keys'] = common_keys
        class ActionChains:
            def __init__(self, *args, **kwargs):
                pass

        action_chains.ActionChains = ActionChains
        sys.modules['selenium.webdriver.common.action_chains'] = action_chains
        sys.modules['selenium.common.exceptions'] = common_exceptions

        # Support.ui and expected_conditions minimal stubs
        support_ui = types.ModuleType('selenium.webdriver.support.ui')
        expected_conditions_mod = types.ModuleType('selenium.webdriver.support.expected_conditions')

        class WebDriverWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, func):
                return True

        support_ui.WebDriverWait = WebDriverWait

        # expected_conditions can be a simple module/object; some code may treat it as callable
        def dummy_condition(*args, **kwargs):
            return True

        expected_conditions_mod.dummy = dummy_condition

        support_pkg = types.ModuleType('selenium.webdriver.support')
        support_pkg.expected_conditions = expected_conditions_mod
        support_pkg.ui = support_ui

        sys.modules['selenium.webdriver.support'] = support_pkg
        sys.modules['selenium.webdriver.support.ui'] = support_ui
        sys.modules['selenium.webdriver.support.expected_conditions'] = expected_conditions_mod

    # Now import the module under test
    import core.selenium_llm_base as slb
    importlib.reload(slb)
    return slb


def test_llm_name_for_logs_defaults_to_LLM():
    slb = _import_sandboxed_selenium_llm_base()
    # Ensure default fallback is used when no name set
    # Force the module's variable to empty
    slb._active_selenium_llm_name = ""
    assert slb._llm_name_for_logs() == "LLM"


def test_set_active_selenium_limits_updates_name():
    slb = _import_sandboxed_selenium_llm_base()
    slb.set_active_selenium_limits(12345, "grok-beta")
    assert slb.get_active_selenium_limits()["llm_name"] == "grok-beta"
    assert slb._llm_name_for_logs() == "grok-beta"


def test_set_active_selenium_limits_with_empty_name():
    slb = _import_sandboxed_selenium_llm_base()
    slb.set_active_selenium_limits(1000, "")
    assert slb.get_active_selenium_limits()["llm_name"] == ""
    assert slb._llm_name_for_logs() == "LLM"
