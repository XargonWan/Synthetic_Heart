import os
import sys
import types

# Ensure logs are written inside the repository
os.environ.setdefault("LOG_DIR", "./logs")

# Provide lightweight stubs for optional third party modules so that test
# imports succeed even when the dependencies are missing.


def _ensure_module(name, attrs=None):
    """Create a stub module and register it in ``sys.modules``.

    When ``name`` contains dots, parent packages are created as needed so that
    ``import pkg.sub`` works as expected.
    """
    if name in sys.modules:
        module = sys.modules[name]
    else:
        module = types.ModuleType(name)
        sys.modules[name] = module

    if attrs:
        for key, value in attrs.items():
            setattr(module, key, value)

    if "." in name:
        parent_name, child = name.rsplit(".", 1)
        parent = _ensure_module(parent_name)
        setattr(parent, child, module)

    return module


# telegram stubs ------------------------------------------------------------
telegram_error = _ensure_module(
    "telegram.error", {"TimedOut": type("TimedOut", (Exception,), {})}
)
_ensure_module(
    "telegram",
    {
        "Update": type("Update", (), {}),
        "Bot": type("Bot", (), {}),
        "error": telegram_error,
    },
)
_ensure_module(
    "telegram.ext",
    {
        "ApplicationBuilder": type("ApplicationBuilder", (), {}),
        "MessageHandler": type("MessageHandler", (), {}),
        "ContextTypes": types.SimpleNamespace(DEFAULT_TYPE=object),
        "CommandHandler": type("CommandHandler", (), {}),
        "filters": types.SimpleNamespace(TEXT=None),
    },
)

# dotenv stub --------------------------------------------------------------
# Only when python-dotenv is genuinely absent. The stub replaces the package for
# the whole process, and an incomplete stub breaks anything importing more than
# ``load_dotenv``: inside pytest the MCP client's ``mcp``/``pydantic_settings``
# import of ``dotenv_values`` fails, every stdio server then looks unreachable
# ("cannot import name 'dotenv_values' from 'dotenv' (unknown location)") and the
# runtime logs an ERROR per server per connect attempt (2026-09-28). With the
# package installed, ``load_dotenv`` stays a no-op so a real ``.env`` still cannot
# leak into test configuration, and every other attribute is the real one.
try:
    import dotenv as _dotenv_real
except Exception:  # pragma: no cover - only when the dependency is missing
    _ensure_module(
        "dotenv",
        {
            "load_dotenv": lambda *args, **kwargs: False,
            "dotenv_values": lambda *args, **kwargs: {},
            "find_dotenv": lambda *args, **kwargs: "",
        },
    )
else:
    _dotenv_real.load_dotenv = lambda *args, **kwargs: False


# aiomysql stub ------------------------------------------------------------
async def _missing_connect(*args, **kwargs):
    raise RuntimeError("aiomysql is not installed")


_ensure_module(
    "aiomysql",
    {
        "connect": _missing_connect,
        "create_pool": _missing_connect,
        "Connection": type("Connection", (), {}),
    },
)
