Component Development Guide - Configuration Management
==========================================================

Overview
--------

This guide explains how to properly use the configuration system when developing new components (interfaces, plugins, Cortex engines) for Synthetic Heart.

.. warning::
   **IMPORTANT: Use ConfigVar for Global Variables**

   When declaring configuration variables at module level (global variables), **always use ``config_registry.get_var()``** instead of ``config_registry.get_value()``.

Why ConfigVar?
~~~~~~~~~~~~~~

- **Auto-updating**: ConfigVar automatically reflects database changes without manual listeners
- **No boilerplate**: No need to write listener functions or update globals manually
- **Developer-friendly**: Simpler code, less error-prone
- **Consistent behavior**: Works the same way across all components

The Standard Pattern
--------------------

.. _correct-pattern:

✅ CORRECT: Using ConfigVar
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   from core.config_manager import config_registry

   # Declare configuration using get_var() - this returns a ConfigVar object
   MY_TOKEN = config_registry.get_var(
       "MY_TOKEN",
       "",  # default value
       label="My Token",
       description="Authentication token for the service",
       group="interface",  # or "plugin", "llm", etc.
       component="my_component",
       sensitive=True,  # hide value in UI
   )

   # Use the variable naturally - it auto-updates when DB changes
   def start_service():
       if MY_TOKEN:  # ConfigVar supports __bool__
           client = MyClient(token=str(MY_TOKEN))  # Convert to string when needed
       else:
           print("Token not configured")

   # Fallback pattern
   TOKEN_A = config_registry.get_var("TOKEN_A", "", label="Token A", ...)
   TOKEN_B = config_registry.get_var("TOKEN_B", "", label="Token B", ...)

   def get_token():
       """Get token with fallback logic."""
       return str(TOKEN_A or TOKEN_B)  # ConfigVar supports __or__

.. _wrong-pattern:

❌ WRONG: Old pattern with get_value() and listeners
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # DON'T DO THIS - Old pattern, deprecated
   MY_TOKEN = config_registry.get_value("MY_TOKEN", "", label="My Token", ...)

   def _update_token(value):
       global MY_TOKEN
       MY_TOKEN = value

   config_registry.add_listener("MY_TOKEN", _update_token)

ConfigVar API
-------------

ConfigVar objects support natural Python operations:

.. code-block:: python

   TOKEN = config_registry.get_var("TOKEN", "", ...)

   # Boolean check
   if TOKEN:  # True if value exists and is not empty
       ...

   # String conversion
   token_str = str(TOKEN)

   # Equality
   if TOKEN == "expected_value":
       ...

   # Fallback with or
   active_token = TOKEN_A or TOKEN_B or "default"

   # Access raw value (same as str())
   token_value = TOKEN.value

When to Use get_value() vs get_var()
------------------------------------

Use ``get_var()`` for:
~~~~~~~~~~~~~~~~~~~~~~~

- **Module-level global variables** (most common case)
- Any variable that needs to stay updated when DB changes
- Interface tokens, bot names, feature flags, etc.

Use ``get_value()`` for:
~~~~~~~~~~~~~~~~~~~~~~~~

- **Inside class constructors** (when you want to capture value at init time)
- One-time configuration reads
- Values that shouldn't change after initialization

.. code-block:: python

   class MyPlugin:
       def __init__(self):
           # get_value() is OK here - reads current value once during init
           self.cache_dir = config_registry.get_value(
               "CACHE_DIR",
               "/tmp/cache",
               label="Cache Directory",
               ...
           )

Complete Example: Telegram Bot Interface
----------------------------------------

.. code-block:: python

   from core.config_manager import config_registry
   from core.logging_utils import log_warning

   # Configuration - use get_var() for module-level variables
   BOTFATHER_TOKEN = config_registry.get_var(
       "BOTFATHER_TOKEN",
       "",
       label="Telegram Bot Token",
       description="Token provided by BotFather to access the Telegram Bot API.",
       group="interface",
       component="telegram_bot",
       sensitive=True,
   )

   TELEGRAM_TOKEN = config_registry.get_var(
       "TELEGRAM_TOKEN",
       "",
       label="Telegram Token (Alternative)",
       description="Optional alternative Telegram bot token (fallback for BOTFATHER_TOKEN).",
       group="interface",
       component="telegram_bot",
       sensitive=True,
   )

   def get_telegram_token() -> str:
       """
       Get the active Telegram token with fallback logic.
       Returns BOTFATHER_TOKEN if set, otherwise TELEGRAM_TOKEN.
       """
       token = BOTFATHER_TOKEN or TELEGRAM_TOKEN
       return str(token) if token else ""

   async def start_bot():
       token = get_telegram_token()
       if not token:
           log_warning("[telegram_bot] Token not configured - skipping startup")
           return

       # Token is always current from DB
       app = ApplicationBuilder().token(token).build()
       await app.run_polling()

Configuration Options
---------------------

All configuration methods accept these parameters:

.. code-block:: python

   config_registry.get_var(
       "CONFIG_KEY",              # Unique identifier (UPPERCASE_WITH_UNDERSCORES)
       "default_value",           # Default if not in ENV or DB
       label="Human Readable",    # Display name in Web UI
       description="...",         # Help text in Web UI
       value_type=str,            # str, int, bool, float, or custom converter
       group="core",              # Grouping: "core", "interface", "plugin", "llm"
       component="my_component",  # Component name for attribution
       advanced=False,            # True to hide in basic settings view
       sensitive=True,            # True to hide value in UI (passwords, tokens)
       tags=["bootstrap"],        # Special tags (usually not needed)
       constraints={"min": 0},    # Validation constraints (optional)
   )

Configuration Precedence
------------------------

The system follows this priority order:

1. **Environment variable** (highest priority, read-only in UI)
2. **Database value** (persisted user changes via Web UI)
3. **Default value** (fallback if not set anywhere)

When an ENV variable exists, it:

- Overrides the database value
- Is marked as read-only in the Web UI
- Shows an "override" indicator
- Still gets persisted to DB for visibility

Testing Your Component
----------------------

After implementing configuration:

1. **Test with ENV variable**:

   .. code-block:: bash

      export MY_TOKEN="test_value"
      python main.py

   → Variable should be read-only in UI

2. **Test with DB value**:

   - Remove from ENV
   - Set value in Web UI
   - Restart application
   → Value should persist

3. **Test default**:

   - Remove from ENV and DB
   → Should use default value

Common Patterns
---------------

Feature Flags
~~~~~~~~~~~~~

.. code-block:: python

   ENABLE_FEATURE = config_registry.get_var(
       "ENABLE_FEATURE",
       False,
       value_type="bool",
       label="Enable Feature",
       ...
   )

   if ENABLE_FEATURE:
       # Feature code
       pass

Numeric Settings
~~~~~~~~~~~~~~~~

.. code-block:: python

   TIMEOUT = config_registry.get_var(
       "TIMEOUT",
       30,
       value_type=int,
       label="Timeout (seconds)",
       constraints={"min": 1, "max": 300},
       ...
   )

   await asyncio.wait_for(operation(), timeout=int(TIMEOUT))

List/Set Settings
~~~~~~~~~~~~~~~~~

.. code-block:: python

   ALLOWED_IDS = config_registry.get_var(
       "ALLOWED_IDS",
       "",
       label="Allowed IDs",
       description="Comma-separated list of allowed user IDs",
       ...
   )

   def get_allowed_ids() -> set[str]:
       value = str(ALLOWED_IDS).strip()
       return set(x.strip() for x in value.split(",") if x.strip())

Migration from Old Pattern
--------------------------

If you have existing code using the old pattern:

.. code-block:: python

   # Old
   VAR = config_registry.get_value("VAR", "default", ...)
   def _update_var(value):
       global VAR
       VAR = value
   config_registry.add_listener("VAR", _update_var)

Convert to:

.. code-block:: python

   # New
   VAR = config_registry.get_var("VAR", "default", ...)

That's it! Remove the listener function and ``add_listener`` call.

Need Help?
----------

- Check existing interfaces: ``interface/telegram_bot.py``, ``interface/discord_interface.py``
- Check core modules: ``core/persona_manager.py``
- Ask in the development channel

Summary
-------

✅ **DO**:

- Use ``get_var()`` for module-level configuration variables
- Use ConfigVar objects naturally (they support bool, str, or, eq)
- Create helper functions for complex value processing

❌ **DON'T**:

- Use ``get_value()`` + manual listeners for global variables
- Update globals manually in listener functions
- Assume values stay constant (they update automatically)

.. note::
   **Remember**: If you declare a configuration variable at module level, use ``get_var()``. The system handles everything else automatically!