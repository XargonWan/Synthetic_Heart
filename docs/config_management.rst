=====================================
Configuration Management Guide
=====================================

This guide explains how to properly use the configuration system when developing new components (interfaces, plugins, Cortex engines) for Synthetic Heart.

.. contents:: Table of Contents
   :local:
   :depth: 2

⚠️ IMPORTANT: Use ConfigVar for Global Variables
================================================

When declaring configuration variables at module level (global variables), **always use `config_registry.get_var()`** instead of `config_registry.get_value()`**.

Why ConfigVar?
--------------

- **Auto-updating**: ConfigVar automatically reflects database changes without manual listeners
- **No boilerplate**: No need to write listener functions or update globals manually
- **Developer-friendly**: Simpler code, less error-prone
- **Consistent behavior**: Works the same way across all components

The Standard Pattern
--------------------

.. code-block:: python

   from core.config_manager import config_registry

   # Declare configuration using get_var() - this returns a ConfigVar object
   MY_VAR = config_registry.get_var(
       "MY_KEY",
       "default_value",
       label="My Variable",
       description="Description for the WebUI",
       group="My Category",
       component="my_component",
       value_type="string"  # or "int", "bool", "json"
   )

   # Use the variable naturally - it auto-updates when DB changes
   def some_function():
       if MY_VAR:  # ConfigVar supports __bool__
           print(f"Value is: {MY_VAR}")  # Works with string formatting
           return str(MY_VAR)  # Or explicit conversion

How It Works
------------

1. **At import time**: ``get_var()`` creates a ConfigVar proxy
2. **During initialization**: Core registers all variables
3. **After DB load**: Core calls ``notify_all_listeners()`` automatically
4. **When accessed**: ConfigVar returns the current value transparently

ConfigVar API
-------------

ConfigVar objects support natural Python operations:

.. code-block:: python

   TOKEN = config_registry.get_var("TOKEN", "", ...)

   # Boolean check
   if TOKEN:  # True if value exists and is not empty
       ...

   # String operations
   print(f"Token: {TOKEN}")  # Automatic string conversion

   # Comparisons
   if TOKEN == "expected":
       ...

   # Fallback pattern
   def get_token():
       """Get token with fallback logic."""
       return str(TOKEN or "default")  # ConfigVar supports __or__

Migration from Old Pattern
==========================

If you have existing code using the old pattern:

.. code-block:: python

   # Old
   MY_TOKEN = config_registry.get_value("MY_TOKEN", "", label="My Token", ...)
   def _update_token(value):
       global MY_TOKEN
       MY_TOKEN = value
   config_registry.add_listener("MY_TOKEN", _update_token)

Convert to:

.. code-block:: python

   # New
   MY_TOKEN = config_registry.get_var("MY_TOKEN", "", label="My Token", ...)

That's it! Remove the listener function and ``add_listener`` call.

Registering Exposed Variables
=============================

When developing components (interfaces, plugins, or engines), you may need to expose configuration variables in the Web UI for users to configure. This is done by registering variables using the exposed variables system.

When to Register Variables
--------------------------

Register variables when:
- Users need to configure your component through the Web UI
- The variable should persist across restarts
- The variable needs validation or specific UI controls

How to Register Exposed Variables
---------------------------------

Import the registration function and call it at module level:

.. code-block:: python

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

Available Parameters
--------------------

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

Using Registered Variables
--------------------------

After registration, access the variable through the config registry:

.. code-block:: python

   from core.config_manager import config_registry

   # Get the current value
   token = config_registry.get_var("MY_TOKEN", "")

   # Use in your code
   if token:
       client = MyServiceClient(token=str(token))

Migration Script
----------------

When deploying new exposed variables, run the pre-operation migration script to ensure database rows exist:

.. code-block:: bash

   python core/scripts/preop_migrate_exposed_vars.py

This script creates database entries for any missing registered variables using their default values.

Best Practices
--------------

- Register variables at module import time (not inside functions)
- Use descriptive labels and descriptions
- Mark sensitive data (tokens, passwords) as ``sensitive=True``
- Use appropriate ``value_type`` for validation
- Test your component with the Web UI after registration

Need Help?
==========

- Check existing interfaces: ``interface/telegram_bot.py``, ``interface/discord_interface.py``
- Check core modules: ``core/persona_manager.py``
- Ask in the development channel

Summary
=======

✅ **DO**:
- Use ``get_var()`` for module-level configuration variables
- Use ConfigVar objects naturally (they support bool, str, or, eq)
- Create helper functions for complex value processing
- Register exposed variables for user-configurable settings

❌ **DON'T**:
- Use ``get_value()`` + manual listeners for global variables
- Update globals manually in listener functions
- Assume values stay constant (they update automatically)

---

**Remember**: If you declare a configuration variable at module level, use ``get_var()``. The system handles everything else automatically!