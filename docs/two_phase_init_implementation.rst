Two-Phase Initialization Implementation
=======================================

Date: 2025-10-11

Problem
-------

After core refactoring, the ``telegram_bot`` interface was not appearing in the WebUI. Investigation revealed a fundamental timing issue:

1. **Old pattern**: Components created instances at module import time
2. **New pattern**: Configuration variables loaded from database
3. **Conflict**: Variables registered during import had default values (not DB values)

Symptom
~~~~~~~

.. code-block:: python

   BOTFATHER_TOKEN = config_registry.get_var("BOTFATHER_TOKEN", None, ...)  # Returns None during import
   telegram_interface = TelegramInterface()  # Created immediately with None token

Result: Interface always disabled because token was None.

Solution: Two-Phase Initialization
----------------------------------

Phase 1: Discovery (Import Time)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- Modules are imported
- Variables **registered** with ``config_registry.get_var()``
- **NO instances created**
- Purpose: Tell core "these variables exist and should be loaded from DB"

Phase 2: Initialization (Post-DB-Load)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- All variables loaded from database
- All listeners notified with DB values
- Core calls ``initialize_interface()`` / ``initialize_plugin()``
- **Instances created with correct DB values**

Implementation
--------------

Changes to ``telegram_bot.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

1. Variable Registration (Phase 1)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   # At module level - registers variables but doesn't create instances
   BOTFATHER_TOKEN = config_registry.get_var(
       "BOTFATHER_TOKEN",
       None,
       label="Telegram Bot Token",
       description="Bot token from @BotFather",
       group="interface",
       component="telegram_bot",
       sensitive=True,
   )

   TRAINER_IDS = config_registry.get_var(
       "TRAINER_IDS",
       "",
       label="Trainer IDs",
       description="Comma-separated list of trainer IDs (format: interface:user_id)",
       group="core",
       component="trainer",
   )

   # Global instance variable
   telegram_interface = None

2. Lazy Initialization (Phase 2)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   def initialize_interface():
       """
       Initialize the Telegram interface after config has been loaded from DB.
       Called by core after database values are loaded.
       Supports reload: if instance exists, shuts it down first.
       """
       global telegram_interface
       
       # Reload support: cleanup existing instance
       if telegram_interface is not None:
           shutdown_interface()
       
       log_info("[telegram_bot] Creating Telegram interface instance...")
       telegram_interface = TelegramInterface(None)
       register_interface("telegram_bot", telegram_interface)
       
       return telegram_interface

3. Lifecycle Management
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   def shutdown_interface():
       """Cleanup before reload or shutdown."""
       global telegram_interface
       
       if telegram_interface is None:
           return
       
       # Cleanup resources
       # (e.g., close connections, stop threads)
       
       # Unregister from core
       from core.core_initializer import INTERFACE_REGISTRY
       if "telegram_bot" in INTERFACE_REGISTRY:
           del INTERFACE_REGISTRY["telegram_bot"]
       
       telegram_interface = None


   def reload_interface():
       """Reload with updated configuration."""
       return initialize_interface()

4. Instance Changes
^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   class TelegramInterface:
       def __init__(self, bot_token_ignored):
           # Read variables - NOW they have correct values from DB
           self.bot_token = BOTFATHER_TOKEN
           self.trainer_id = _parse_trainer_id_from_config()
           
           # Check configuration
           self.is_enabled = bool(self.bot_token)
           self.disabled_reason = None if self.is_enabled else "BOTFATHER_TOKEN not configured"
           
           if not self.is_enabled:
               log_warning(f"[telegram_interface] Loaded in disabled state: {self.disabled_reason}")

Changes to ``core/core_initializer.py``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Added Interface Instance Initialization
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   def _initialize_interface_instances(self):
       """
       Phase 2: Initialize interface instances after config is loaded.
       Calls initialize_interface() on modules that implement it.
       """
       log_debug("[core_initializer] Starting interface instance initialization...")
       
       for module_name in self._interface_modules:
           module = sys.modules.get(module_name)
           if module and hasattr(module, 'initialize_interface'):
               try:
                   log_debug(f"[core_initializer] Calling initialize_interface() for {module_name}")
                   interface = module.initialize_interface()
                   log_debug(f"[core_initializer] Successfully initialized {module_name}")
               except Exception as e:
                   log_error(f"[core_initializer] Failed to initialize {module_name}: {e}")
           else:
               log_debug(f"[core_initializer] Module {module_name} has no initialize_interface function")

Updated Initialization Sequence
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   async def initialize_all(self):
       # 1. Initialize base systems
       await self._initialize_registries()
       self._load_llm_engines()
       self._load_plugins()
       
       # 2. PHASE 1: Import interfaces (registers variables)
       self._discover_interfaces()
       
       # 3. Load config from database
       await config_registry.load_all_from_db()
       config_registry.notify_all_listeners()
       log_info("✅ All config listeners notified")
       
       # 4. PHASE 2: Initialize interface instances
       self._initialize_interface_instances()
       log_info("✅ Interface instances initialized")
       
       # 5. Start interfaces
       await self._start_interfaces()

Verification
------------

Log Sequence (Correct Order)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

   [01:33:10] Successfully imported: telegram_bot                    # Phase 1
   [01:33:10] ✓ Loaded 'BOTFATHER_TOKEN' from DB: 7934437...        # DB Load
   [01:33:10] ✓ Loaded 'TRAINER_IDS' from DB: telegram_bot:31321637 # DB Load
   [01:33:10] ✓ Notified 18 listener(s) with updated config values  # Notify
   [01:33:10] Calling initialize_interface() for {module_name}       # Phase 2
   [01:33:10] Creating Telegram interface instance...                # Phase 2
   [01:33:10] Interface enabled                                      # Phase 2
   [01:33:10] TelegramInterface instance initialized                 # Phase 2
   [01:33:10] 🔌 Interface loaded: telegram_bot                      # Success
   [01:33:10] 📡 Active Interfaces: ... telegram_bot                # Success

WebUI Verification
~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   $ curl -s http://localhost:9009/api/components | python3 -m json.tool | grep -A 5 telegram

Output:

.. code-block:: json

   {
       "name": "telegram_bot",
       "display_name": "Telegram Bot",
       "description": "Interface wrapper providing a standard send_message method for Telegram.",
       "actions": [
           {
               "name": "message_telegram_bot",
               ...
           },
           {
               "name": "audio_telegram_bot",
               ...
           }
       ],
       "status": "unknown",
       "details": "",
       "error": ""
   }

**✅ telegram_bot now appears in WebUI with all actions registered correctly**

Benefits
--------

1. **Database Configuration Works**: Variables loaded from DB before instances created
2. **Environment Override Still Works**: Env vars have higher priority than DB
3. **Hot Reload Supported**: Components can be reloaded when config changes
4. **Clean Architecture**: Core controls initialization order, not components
5. **Consistency**: All components follow the same pattern
6. **WebUI Integration**: Configuration automatically appears in WebUI

Next Steps
----------

Apply Pattern to Other Interfaces
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The following interfaces should be migrated to the two-phase pattern:

- [ ] ``discord_interface.py``
- [ ] ``matrix_interface.py``
- [ ] ``openai_api_server.py``

Migration checklist for each:

1. Move instance creation to ``initialize_interface()``
2. Add ``shutdown_interface()`` for cleanup
3. Add ``reload_interface()`` for hot reload
4. Keep variable registration at module level
5. Export lifecycle functions in ``__all__``
6. Test with database configuration

Documentation
-------------

See ``component_pattern.rst`` for the complete developer guide on implementing components with two-phase initialization.

Related Files
-------------

- ``/videodrome/videodrome-deployment/Synthetic_Heart/interface/telegram_bot.py``
- ``/videodrome/videodrome-deployment/Synthetic_Heart/core/core_initializer.py``
- ``/videodrome/videodrome-deployment/Synthetic_Heart/docs/component_pattern.rst``
- ``/videodrome/videodrome-deployment/Synthetic_Heart/TELEGRAM_BOT_REFACTOR.md``