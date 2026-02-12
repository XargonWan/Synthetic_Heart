# cortex/llm_engine/selenium_gemini.py

# Migration shim: attempt to reuse the original implementation from
# `llm_engines.selenium_gemini` when available. This allows projects that
# haven't fully migrated to the new cortex layout to keep working without
# causing fatal registry errors.
from core.logging_utils import log_debug, log_info, log_warning, log_error

PLUGIN_CLASS = None

try:
    import importlib
    orig_mod = importlib.import_module('llm_engines.selenium_gemini')
    if hasattr(orig_mod, 'PLUGIN_CLASS') and orig_mod.PLUGIN_CLASS:
        PLUGIN_CLASS = orig_mod.PLUGIN_CLASS
        # Ensure there is a human-friendly display_name for the WebUI
        if not getattr(PLUGIN_CLASS, 'display_name', None):
            try:
                PLUGIN_CLASS.display_name = 'Selenium Gemini'
                log_debug('[cortex.llm_engine.selenium_gemini] Set fallback display_name for PLUGIN_CLASS')
            except Exception:
                log_warning('[cortex.llm_engine.selenium_gemini] Failed to set fallback display_name')
    else:
        log_warning('[cortex.llm_engine.selenium_gemini] Original module found but PLUGIN_CLASS missing or None')
except ModuleNotFoundError:
    log_warning('[cortex.llm_engine.selenium_gemini] Original llm_engines.selenium_gemini module not found - this is a lightweight migration shim')
except Exception as e:
    log_error(f'[cortex.llm_engine.selenium_gemini] Unexpected error importing original engine: {e}')
