=====================================# Component Development Guide - Configuration Management

Configuration Management Guide

=====================================## Overview



This guide explains how to properly use the configuration system when developing new components (interfaces, plugins, engines) for synth.This guide explains how to properly use the configuration system when developing new components (interfaces, plugins, LLM engines) for Synthetic Heart.



.. contents:: Table of Contents## ⚠️ IMPORTANT: Use ConfigVar for Global Variables

   :local:

   :depth: 2When declaring configuration variables at module level (global variables), **always use `config_registry.get_var()`** instead of `config_registry.get_value()`.



The Problem We Solved### Why ConfigVar?

=====================

- **Auto-updating**: ConfigVar automatically reflects database changes without manual listeners

Previously, developers had to manually register listeners to update global variables when configuration changed:- **No boilerplate**: No need to write listener functions or update globals manually  

- **Developer-friendly**: Simpler code, less error-prone

.. code-block:: python- **Consistent behavior**: Works the same way across all components



   # ❌ OLD WAY - DON'T DO THIS## The Standard Pattern

   MY_VAR = config_registry.get_value("MY_KEY", "default")

### ✅ CORRECT: Using ConfigVar

   def _update_my_var(new_value):

       global MY_VAR```python

       MY_VAR = new_valuefrom core.config_manager import config_registry



   config_registry.add_listener("MY_KEY", _update_my_var)# Declare configuration using get_var() - this returns a ConfigVar object

MY_TOKEN = config_registry.get_var(

**Problems with this approach:**    "MY_TOKEN",

    "",  # default value

- Easy to forget the listener    label="My Token",

- Boilerplate code in every component    description="Authentication token for the service",

- Variables wouldn't update when loaded from database    group="interface",  # or "plugin", "llm", etc.

- Error-prone for new developers    component="my_component",

    sensitive=True,  # hide value in UI

The Solution: ConfigVar)

========================

# Use the variable naturally - it auto-updates when DB changes

The core now provides **ConfigVar** - a self-updating proxy that automatically refreshes when the database loads.def start_service():

    if MY_TOKEN:  # ConfigVar supports __bool__

Correct Usage        client = MyClient(token=str(MY_TOKEN))  # Convert to string when needed

-------------    else:

        print("Token not configured")

.. code-block:: python

# Fallback pattern

   from core.config_manager import config_registryTOKEN_A = config_registry.get_var("TOKEN_A", "", label="Token A", ...)

TOKEN_B = config_registry.get_var("TOKEN_B", "", label="Token B", ...)

   # Global variable using ConfigVar

   MY_VAR = config_registry.get_var(def get_token():

       "MY_KEY",    """Get token with fallback logic."""

       "default_value",    return str(TOKEN_A or TOKEN_B)  # ConfigVar supports __or__

       label="My Variable",```

       description="Description for the WebUI",

       category="My Category",### ❌ WRONG: Old pattern with get_value() and listeners

       var_type="string"  # or "int", "bool", "json"

   )```python

# DON'T DO THIS - Old pattern, deprecated

   # Use it normally - it automatically updates!MY_TOKEN = config_registry.get_value("MY_TOKEN", "", label="My Token", ...)

   def some_function():

       if MY_VAR:  # Works with conditionalsdef _update_token(value):

           print(f"Value is: {MY_VAR}")  # Works with string formatting    global MY_TOKEN

           return str(MY_VAR)  # Or explicit conversion    MY_TOKEN = value



How It Worksconfig_registry.add_listener("MY_TOKEN", _update_token)

------------```



1. **At import time**: ``get_var()`` creates a ConfigVar proxy## ConfigVar API

2. **During initialization**: Core registers all variables

3. **After DB load**: Core calls ``notify_all_listeners()`` automaticallyConfigVar objects support natural Python operations:

4. **When accessed**: ConfigVar returns the current value transparently

```python

.. note::TOKEN = config_registry.get_var("TOKEN", "", ...)

   You don't need to do anything special - just use ``get_var()`` instead of ``get_value()``.

# Boolean check

Migration Guideif TOKEN:  # True if value exists and is not empty

===============    ...



Before (Manual Listener)# String conversion

------------------------token_str = str(TOKEN)



.. code-block:: python# Equality

if TOKEN == "expected_value":

   from core.config_manager import config_registry    ...



   # Old pattern# Fallback with or

   DISCORD_TOKEN = config_registry.get_value("DISCORD_BOT_TOKEN", "")active_token = TOKEN_A or TOKEN_B or "default"



   def _handle_token_update(new_token):# Access raw value (same as str())

       global DISCORD_TOKENtoken_value = TOKEN.value

       DISCORD_TOKEN = new_token```

       logger.info(f"Discord token updated: {new_token[:10]}...")

## When to Use get_value() vs get_var()

   config_registry.add_listener("DISCORD_BOT_TOKEN", _handle_token_update)

### Use `get_var()` for:

After (ConfigVar)- **Module-level global variables** (most common case)

------------------ Any variable that needs to stay updated when DB changes

- Interface tokens, bot names, feature flags, etc.

.. code-block:: python

### Use `get_value()` for:

   from core.config_manager import config_registry- **Inside class constructors** (when you want to capture value at init time)

- One-time configuration reads

   # New pattern - that's it!- Values that shouldn't change after initialization

   DISCORD_TOKEN = config_registry.get_var(

       "DISCORD_BOT_TOKEN",```python

       "",class MyPlugin:

       label="Discord Bot Token",    def __init__(self):

       description="Token for Discord bot authentication",        # get_value() is OK here - reads current value once during init

       category="Discord Interface"        self.cache_dir = config_registry.get_value(

   )            "CACHE_DIR", 

            "/tmp/cache",

   # Remove the listener function entirely            label="Cache Directory",

   # Remove the add_listener() call            ...

        )

Real-World Examples```

===================

## Complete Example: Telegram Bot Interface

Example 1: Telegram Bot

-----------------------```python

from core.config_manager import config_registry

.. code-block:: pythonfrom core.logging_utils import log_warning



   # Global variables# Configuration - use get_var() for module-level variables

   BOTFATHER_TOKEN = config_registry.get_var(BOTFATHER_TOKEN = config_registry.get_var(

       "BOTFATHER_TOKEN",    "BOTFATHER_TOKEN",

       "",    "",

       label="BotFather Token",    label="Telegram Bot Token",

       description="Token from @BotFather",    description="Token provided by BotFather to access the Telegram Bot API.",

       category="Telegram Interface"    group="interface",

   )    component="telegram_bot",

    sensitive=True,

   TELEGRAM_TOKEN = config_registry.get_var()

       "TELEGRAM_TOKEN",

       "",TELEGRAM_TOKEN = config_registry.get_var(

       label="Legacy Telegram Token",    "TELEGRAM_TOKEN",

       description="Fallback token",    "",

       category="Telegram Interface"    label="Telegram Token (Alternative)",

   )    description="Optional alternative Telegram bot token (fallback for BOTFATHER_TOKEN).",

    group="interface",

   # Helper function for fallback logic    component="telegram_bot",

   def get_effective_token():    sensitive=True,

       token = str(BOTFATHER_TOKEN).strip())

       if not token:

           token = str(TELEGRAM_TOKEN).strip()def get_telegram_token() -> str:

       return token    """

    Get the active Telegram token with fallback logic.

   # Use in your code    Returns BOTFATHER_TOKEN if set, otherwise TELEGRAM_TOKEN.

   async def start_bot():    """

       token = get_effective_token()    token = BOTFATHER_TOKEN or TELEGRAM_TOKEN

       if not token:    return str(token) if token else ""

           raise ValueError("No Telegram token configured")

       async def start_bot():

       app = ApplicationBuilder().token(token).build()    token = get_telegram_token()

       # ...    if not token:

        log_warning("[telegram_bot] Token not configured - skipping startup")

Example 2: Persona Manager        return

--------------------------    

    # Token is always current from DB

.. code-block:: python    app = ApplicationBuilder().token(token).build()

    await app.run_polling()

   # Multiple related variables```

   SYNTH_NAME = config_registry.get_var(

       "SYNTH_NAME",## Configuration Options

       "SyntH",

       label="Persona Name",All configuration methods accept these parameters:

       description="Default persona name",

       category="Persona",```python

       var_type="string"config_registry.get_var(

   )    "CONFIG_KEY",              # Unique identifier (UPPERCASE_WITH_UNDERSCORES)

    "default_value",           # Default if not in ENV or DB

   SYNTH_ALIASES_TRIGGER = config_registry.get_var(    label="Human Readable",    # Display name in Web UI

       "SYNTH_ALIASES_TRIGGER",    description="...",         # Help text in Web UI

       "//aliases",    value_type=str,            # str, int, bool, float, or custom converter

       label="Aliases Trigger",    group="core",              # Grouping: "core", "interface", "plugin", "llm"

       description="Command to list aliases",    component="my_component",  # Component name for attribution

       category="Persona"    advanced=False,            # True to hide in basic settings view

   )    sensitive=True,            # True to hide value in UI (passwords, tokens)

    tags=["bootstrap"],        # Special tags (usually not needed)

   # Use directly in functions    constraints={"min": 0},    # Validation constraints (optional)

   def get_persona_name():)

       return str(SYNTH_NAME)```



Best Practices## Configuration Precedence

==============

The system follows this priority order:

✅ DO

-----1. **Environment variable** (highest priority, read-only in UI)

2. **Database value** (persisted user changes via Web UI)

- Use ``config_registry.get_var()`` for all global configuration variables3. **Default value** (fallback if not set anywhere)

- Access ConfigVar directly in conditionals and string operations

- Add descriptive labels and categories for the WebUIWhen an ENV variable exists, it:

- Specify the correct ``var_type`` for validation- Overrides the database value

- Is marked as read-only in the Web UI

❌ DON'T- Shows an "override" indicator

--------- Still gets persisted to DB for visibility



- Don't use ``get_value()`` for global variables## Testing Your Component

- Don't write manual ``add_listener()`` calls

- Don't create update functions like ``_update_config()``After implementing configuration:

- Don't try to modify ConfigVar values directly (read-only)

1. **Test with ENV variable**:

Troubleshooting   ```bash

===============   export MY_TOKEN="test_value"

   python main.py

Variable shows default instead of database value   ```

-------------------------------------------------   → Variable should be read-only in UI



**Symptom**: Your variable shows the default value even though it's set in the database.2. **Test with DB value**:

   - Remove from ENV

**Old cause**: Variable was registered after ``load_all_from_db()`` was called.   - Set value in Web UI

   - Restart application

**Solution**: Use ``get_var()`` - the core now loads the database after all components register their variables, and ConfigVar updates automatically.   → Value should persist



How to set values from code3. **Test default**:

----------------------------   - Remove from ENV and DB

   → Should use default value

.. code-block:: python

## Common Patterns

   # To programmatically set a config value:

   config_registry.set_value("MY_KEY", "new_value")### Feature Flags

```python

   # The ConfigVar will automatically reflect the changeENABLE_FEATURE = config_registry.get_var(

   # No manual updates needed!    "ENABLE_FEATURE",

    False,

Technical Details    value_type="bool",

=================    label="Enable Feature",

    ...

ConfigVar Implementation)

------------------------

if ENABLE_FEATURE:

ConfigVar is a lightweight proxy that:    # Feature code

    pass

- Stores the config key, default, and parameters```

- Implements magic methods (``__str__``, ``__bool__``, ``__eq__``, etc.)

- Retrieves the current value on each access via ``get_value()``### Numeric Settings

- Automatically registers a listener with the config registry```python

TIMEOUT = config_registry.get_var(

Initialization Sequence    "TIMEOUT",

-----------------------    30,

    value_type=int,

1. **Component imports**: ConfigVar instances created via ``get_var()``    label="Timeout (seconds)",

2. **Core initialization**: All components discover and register    constraints={"min": 1, "max": 300},

3. **Environment flush**: ``flush_env_to_db()`` syncs ENV variables    ...

4. **Database load**: ``load_all_from_db()`` loads all values from DB)

5. **Notification**: ``notify_all_listeners()`` updates all ConfigVars

6. **Runtime**: ConfigVars always return current value when accessedawait asyncio.wait_for(operation(), timeout=int(TIMEOUT))

```

Summary

=======### List/Set Settings

```python

.. important::ALLOWED_IDS = config_registry.get_var(

   **Old way**: Manual listeners, error-prone, confusing    "ALLOWED_IDS",

       "",

   **New way**: ``config_registry.get_var()`` - automatic, foolproof, standardized    label="Allowed IDs",

    description="Comma-separated list of allowed user IDs",

When developing new interfaces, plugins, or engines:    ...

)

1. Use ``get_var()`` for global config variables

2. Don't add manual listenersdef get_allowed_ids() -> set[str]:

3. Access the variable normally - it just works!    value = str(ALLOWED_IDS).strip()

    return set(x.strip() for x in value.split(",") if x.strip())

This ensures consistency across the codebase and makes development easier for everyone.```


## Migration from Old Pattern

If you have existing code using the old pattern:

```python
# Old
VAR = config_registry.get_value("VAR", "default", ...)
def _update_var(value):
    global VAR
    VAR = value
config_registry.add_listener("VAR", _update_var)
```

Convert to:

```python
# New
VAR = config_registry.get_var("VAR", "default", ...)
```

That's it! Remove the listener function and `add_listener` call.

## Registering Exposed Variables

When developing components (interfaces, plugins, or engines), you may need to expose configuration variables in the Web UI for users to configure. This is done by registering variables using the exposed variables system.

### When to Register Variables

Register variables when:
- Users need to configure your component through the Web UI
- The variable should persist across restarts
- The variable needs validation or specific UI controls

### How to Register Exposed Variables

Import the registration function and call it at module level:

```python
from core.variables_engine import register_exposed_var

# Register your variable (do this at module import time)
MY_TOKEN = register_exposed_var(
    "MY_TOKEN",  # Unique key (used in DB and API)
    label="My Service Token",  # Display name in Web UI
    default="",  # Default value
    description="Authentication token for connecting to My Service",  # Help text
    value_type=str,  # Python type: str, int, bool, list, dict
    sensitive=True,  # Hide value in UI (for passwords/tokens)
    advanced=False,  # Show in advanced settings only
    needs_component_reload=False,  # Restart component after change
)
```

### Available Parameters

- **key**: Unique identifier (uppercase with underscores)
- **label**: Human-readable name for the UI
- **default**: Default value (must match value_type)
- **description**: Help text shown in the UI
- **value_type**: Python type (str, int, bool, list, dict)
- **sensitive**: Hide the value in logs and UI
- **advanced**: Show only in advanced settings
- **needs_component_reload**: Require component restart after changes
- **readonly**: Prevent user editing
- **dangerous**: Mark as potentially harmful
- **validator**: Function or dict for input validation
- **tags**: List of tags for organization

### Using Registered Variables

After registration, access the variable through the config registry:

```python
from core.config_manager import config_registry

# Get the current value
token = config_registry.get_var("MY_TOKEN", "")

# Use in your code
if token:
    client = MyServiceClient(token=str(token))
```

### Migration Script

When deploying new exposed variables, run the pre-operation migration script to ensure database rows exist:

```bash
python core/scripts/preop_migrate_exposed_vars.py
```

This script creates database entries for any missing registered variables using their default values.

### Best Practices

- Register variables at module import time (not inside functions)
- Use descriptive labels and descriptions
- Mark sensitive data (tokens, passwords) as `sensitive=True`
- Use appropriate `value_type` for validation
- Test your component with the Web UI after registration

## Need Help?

- Check existing interfaces: `interface/telegram_bot.py`, `interface/discord_interface.py`
- Check core modules: `core/persona_manager.py`
- Ask in the development channel

## Summary

✅ **DO**:
- Use `get_var()` for module-level configuration variables
- Use ConfigVar objects naturally (they support bool, str, or, eq)
- Create helper functions for complex value processing

❌ **DON'T**:
- Use `get_value()` + manual listeners for global variables
- Update globals manually in listener functions
- Assume values stay constant (they update automatically)

---

**Remember**: If you declare a configuration variable at module level, use `get_var()`. The system handles everything else automatically!
