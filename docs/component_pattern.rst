Component Development Pattern - Two-Phase Initialization
==========================================================

Overview
--------

SyntH uses a **two-phase initialization pattern** to ensure components (interfaces, plugins, LLM engines) have access to their configuration variables loaded from the database before being instantiated.

This pattern also supports **hot-reload** when configuration changes.

Why Two Phases?
---------------

The Problem
~~~~~~~~~~~

Components need to register their configuration variables with ``config_registry.get_var()``, but:

1. Variables must be registered before loading from DB (so we know what to load)
2. Component instances need the loaded values to initialize correctly
3. Components must be reloadable when configuration changes

The Solution: Two-Phase Pattern
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Phase 1: Discovery & Variable Registration**

- Module is imported
- Variables are declared with ``config_registry.get_var()`` at module level
- **NO instances are created yet**

**Phase 2: Initialization**

- Core loads all registered variables from database
- Core notifies all listeners with updated values
- Core calls ``initialize_interface()`` to create **interface** instances
- Instances now see correct values from DB

.. note::

   The two phases are strict only for **interfaces**. There is no
   ``initialize_plugin()`` hook and no separate plugin-instantiation phase:
   ``CoreInitializer._load_plugins()`` imports a plugin module **and**
   instantiates its ``PLUGIN_CLASS`` in the same step, before the database
   values are loaded, and a synchronous ``start()`` runs there too. Only an
   ``async`` ``start()`` is deferred, by ``start_pending_async_plugins()``, until
   after the configuration is loaded. A plugin must therefore not read a
   configuration value in ``__init__`` and expect the database value; read it
   lazily (or in ``start()``).

Implementation Pattern
----------------------

For Interface Developers
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # interface/my_interface.py

   from core.config_manager import config_registry
   from core.core_initializer import register_interface

   # PHASE 1: Declare variables at module level (runs during import)
   MY_TOKEN = config_registry.get_var(
       "MY_TOKEN",
       None,  # default value
       label="My Service Token",
       description="Authentication token for my service",
       group="interface",
       component="my_interface",
       sensitive=True,
   )

   MY_SETTING = config_registry.get_var(
       "MY_SETTING",
       "default_value",
       label="My Setting",
       description="Some configurable setting",
       group="interface",
       component="my_interface",
   )

   # Global instance variable
   my_interface = None


   # PHASE 2: Initialization function (called by core AFTER DB load)
   def initialize_interface():
       """Initialize the interface after config has been loaded from DB."""
       global my_interface
       
       # Reload if already exists (for hot-reload)
       if my_interface is not None:
           shutdown_interface()
       
       # Create instance - variables now have correct values from DB
       my_interface = MyInterface()
       register_interface("my_interface", my_interface)
       
       return my_interface


   def shutdown_interface():
       """Cleanup before reload or shutdown."""
       global my_interface
       
       if my_interface is None:
           return
       
       # Cleanup resources
       if my_interface.connection:
           my_interface.connection.close()
       
       # Unregister
       from core.core_initializer import INTERFACE_REGISTRY
       if "my_interface" in INTERFACE_REGISTRY:
           del INTERFACE_REGISTRY["my_interface"]
       
       my_interface = None


   async def reload_interface():
       """Reload with updated configuration: stop, recreate, start again."""
       await my_interface.stop()
       interface = initialize_interface()
       await interface.start()


   class MyInterface:
       def __init__(self):
           # Read variables - they now have correct values from DB
           self.token = MY_TOKEN
           self.setting = MY_SETTING
           
           # Check configuration
           self.is_enabled = bool(self.token)
           self.disabled_reason = None if self.is_enabled else "Token not configured"
           
           if not self.is_enabled:
               log_warning(f"[my_interface] Loaded in disabled state: {self.disabled_reason}")
       
       async def start(self):
           """Start the interface (called by core after initialization)."""
           if not self.is_enabled:
               return
           
           # Connect, authenticate, etc.
           await self._connect()
       
       @staticmethod
       def get_supported_actions() -> dict:
           return {
               "message_my_interface": {
                   "required_fields": ["text"],
                   "optional_fields": ["target"],
                   "description": "Send a message via my interface",
               }
           }


   # Export for core
   __all__ = ['initialize_interface', 'shutdown_interface', 'reload_interface', 'MyInterface']

Core Initialization Sequence
----------------------------

The core follows this sequence in ``core_initializer.py``:

.. code-block:: python

   # Simplified from CoreInitializer.initialize_all (core/core_initializer.py)
   async def initialize_all(self, notify_fn=None):
       # 1. Registries: engine kinds and modules (register_default_engines)
       # 2. Cortex engine, then plugins
       self._load_plugins()   # imports AND instantiates plugins; sync start() runs here
       #    ensure_plugin_tables(), validation rules, ...
       await config_registry.load_all_from_db()      # first DB load
       await self.start_pending_async_plugins()      # async plugin start()
       # 3. PHASE 1 for interfaces: import, register variables, no instances yet
       self._discover_interfaces()
       # 4. Second DB load: interface variables get their stored values
       await config_registry.load_all_from_db()
       #    _build_actions_block()  (merge plugin and interface actions)
       # 5. PHASE 2 for interfaces: create instances with correct configuration
       self._initialize_interface_instances()
       # 6. Start interfaces
       await self._start_interfaces()

Hot Reload Support
------------------

When a configuration variable changes (e.g., user updates token in WebUI):

1. WebUI saves new value to database
2. WebUI calls ``reload_interface("my_interface")``
3. Core calls ``interface.shutdown_interface()`` to cleanup
4. Core reloads variables from DB
5. Core calls ``interface.initialize_interface()`` to recreate with new config
6. Core calls ``interface.start()`` if it has a start method

An interface whose module defines no ``reload_interface`` still reloads: the core
registers a default handler that calls the instance's ``stop()`` (or
``teardown`` / ``shutdown`` / ``close``) and then ``start()``, unless the
operator disabled the interface. Give the class a ``stop()`` that really
releases the connection so the restart picks up the new configuration.

Rules for Component Developers
------------------------------

✅ DO:
~~~~~

- Declare all config variables with ``config_registry.get_var()`` at **module level**
- Create instances only in ``initialize_*()`` function
- Implement ``shutdown_*()`` for proper cleanup
- Export ``initialize_*``, ``shutdown_*``, ``reload_*`` in ``__all__``
- Check configuration in ``__init__`` and set ``is_enabled`` / ``disabled_reason``

❌ DON'T:
~~~~~~~~

- Create instances directly at module level during import
- Assume variables have DB values during import
- Keep global state that survives reload (use instance variables)
- Call ``register_interface()`` during import (do it in ``initialize_*()``)

Migration Guide
---------------

Old Pattern (breaks with DB config):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # ❌ Creates instance during import - DB not loaded yet!
   my_interface = MyInterface(os.getenv('MY_TOKEN'))
   register_interface("my_interface", my_interface)

New Pattern (works with DB config):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # ✅ Registers variables during import
   MY_TOKEN = config_registry.get_var("MY_TOKEN", None, ...)

   my_interface = None

   def initialize_interface():
       global my_interface
       # Creates instance AFTER DB load - has correct values
       my_interface = MyInterface()
       register_interface("my_interface", my_interface)
       return my_interface

Benefits
--------

1. **Database Configuration**: Variables can be stored and managed in DB
2. **Environment Override**: Env vars still work (with higher priority)
3. **Hot Reload**: Components can be reloaded when config changes
4. **Consistency**: All components follow the same pattern
5. **Simplicity**: Developers don't manage listeners manually
6. **WebUI Integration**: Configuration appears automatically in WebUI

Testing
-------

Components can be tested in isolation:

.. code-block:: python

   # test_my_interface.py
   from interface import my_interface

   # Simulate config load
   my_interface.MY_TOKEN = "test_token"
   my_interface.MY_SETTING = "test_value"

   # Initialize
   interface = my_interface.initialize_interface()
   assert interface.is_enabled
   assert interface.token == "test_token"

   # Test reload
   my_interface.MY_TOKEN = "new_token"
   interface = my_interface.reload_interface()
   assert interface.token == "new_token"

See Also
--------

- ``extensions_src/telegram_bot/`` - Reference implementation (an extension; see :doc:`plugin_platform/extension_developer_guide`)
- ``interface/discord_interface.py`` - Another example
- ``core/core_initializer.py`` - Core initialization logic
- ``core/config_manager.py`` - Configuration system