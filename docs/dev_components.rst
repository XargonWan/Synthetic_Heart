Development Components System
==============================

Overview
--------

The Development Components System allows developers to safely test experimental interfaces, plugins, and Cortex engines in separate ``_dev`` directories without affecting production components. This feature is designed with safety in mind: the dev components flag **resets to disabled on every restart**, ensuring that untested code never persists in production environments.

Key Features
------------

* **Runtime-Only Toggle**: Dev components can be enabled/disabled at runtime via WebUI, but the setting never persists
* **Automatic Directory Discovery**: When enabled, synth automatically discovers components from:
  
  * ``interface_dev/`` - Development interfaces
  * ``plugins_dev/`` - Development plugins
  * ``cortex/*/dev`` - Development Cortex engine subfolders (e.g. ``cortex/llm_provider/dev``)

* **Visual Identification**: Dev components are marked with yellow "⚠️ in development" badges in the WebUI
* **Safety First**: Flag automatically resets to ``False`` on every container restart

Architecture
------------

Core Components
^^^^^^^^^^^^^^^

1. **CoreInitializer** (``core/core_initializer.py``)
   
   * ``_enable_dev_components`` flag (runtime-only, defaults to ``False``)
   * ``enable_dev_components(enabled: bool)`` - Toggle the flag
   * ``are_dev_components_enabled() -> bool`` - Check current state

2. **Discovery Mechanism**
   
   * ``_discover_interfaces()`` - Scans ``interface/`` and optionally ``interface_dev/``
   * ``_load_plugins()`` - Scans ``plugins/``, ``cortex/``, and optionally their ``_dev`` variants

3. **WebUI Integration** (``core/webui.py``)
   
   * ``POST /api/components/dev/toggle`` - Toggle endpoint
   * ``GET /api/components`` - Returns ``dev_components_enabled`` status
   * Toggle switch in Components tab with warning message

Directory Structure
-------------------

.. code-block:: text

    Synthetic_Heart/
    ├── interface/          # Production interfaces
    ├── interface_dev/      # Development interfaces (optional)
    ├── plugins/            # Production plugins
    ├── plugins_dev/        # Development plugins (optional)
    ├── cortex/             # Production Cortex engines
    └── cortex/*/dev/       # Development Cortex engine subfolders (optional)

Usage
-----

Enabling Dev Components
^^^^^^^^^^^^^^^^^^^^^^^

.. note::

   A comprehensive catalog of all HTTP and WebSocket endpoints exposed by
   Synthetic Heart is available in :doc:`api_endpoints`.  Developers building
   external integrations should consult that reference when writing clients.


1. **Via WebUI**:
   
   * Navigate to the **Components** tab
   * Find the "🔧 Development Components" section at the top
   * Check the toggle switch
   * Restart synth when prompted

2. **Via API**:

   .. code-block:: bash

       curl -X POST http://localhost:8008/api/components/dev/toggle \
         -H "Content-Type: application/json" \
         -d '{"enabled": true}'

3. **Programmatically**:

   .. code-block:: python

       from core.core_initializer import core_initializer
       
       # Enable dev components
       core_initializer.enable_dev_components(True)
       
       # Check if enabled
       if core_initializer.are_dev_components_enabled():
           print("Dev components are enabled")

Creating Dev Components
^^^^^^^^^^^^^^^^^^^^^^^

1. Create the appropriate ``_dev`` directory:

   .. code-block:: bash

       mkdir interface_dev  # For interfaces
       mkdir plugins_dev    # For plugins
       mkdir -p cortex/llm_provider/dev  # Example: create a dev folder for a Cortex provider (adjust per provider) 

2. Add your experimental component following the same structure as production components

3. Enable dev components in WebUI

4. Restart synth to load the dev components

Example: Dev Interface
^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # interface_dev/my_experimental_interface.py
    
    from core.interface_adapters import BaseInterface, register_interface
    from core.logging_utils import log_info, log_error
    
    INTERFACE_NAME = "my_experimental_interface"
    
    class MyExperimentalInterface(BaseInterface):
        """Experimental interface for testing new features."""
        
        def __init__(self):
            super().__init__(INTERFACE_NAME)
            log_info("[my_experimental_interface] Initializing dev interface")
        
        async def start(self):
            log_info("[my_experimental_interface] Starting dev interface")
            # Your experimental code here
        
        async def stop(self):
            log_info("[my_experimental_interface] Stopping dev interface")
    
    # Auto-register when module is imported
    register_interface(INTERFACE_NAME, MyExperimentalInterface())

WebUI Indicators
----------------

When dev components are enabled and loaded, they appear in the Components tab with:

* **Yellow Badge**: "⚠️ in development" badge next to the component name
* **Yellow Warning Card**: At the top of Components tab showing current status
* **Non-Persistent Warning**: Reminder that setting resets on restart

.. code-block:: text

    🔧 Development Components
    ☑ Enable development components (interface_dev, plugins_dev, cortex dev folders)
    
    ⚠️ Warning: This setting is not persistent and will reset to OFF when 
    the container restarts. A restart is required after toggling.

API Reference
-------------

Toggle Dev Components
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/components/dev/toggle
    Content-Type: application/json
    
    {
        "enabled": true
    }

**Response**:

.. code-block:: json

    {
        "status": "ok",
        "enabled": true,
        "message": "Dev components enabled. Restart required to apply changes."
    }

Get Components Status
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/components

**Response includes**:

.. code-block:: json

    {
        "llm": { ... },
        "interfaces": [ ... ],
        "plugins": [ ... ],
        "summary": { ... },
        "dev_components_enabled": false
    }

Safety Considerations
---------------------

**Non-Persistent by Design**
    The dev components flag **never persists to disk**. It only exists in memory during the current session. This ensures:
    
    * Dev code never accidentally runs in production after restart
    * Explicit opt-in required for every session
    * Reduced risk of untested code causing issues

**Restart Required**
    Enabling/disabling dev components requires a full synth restart to:
    
    * Reload Python modules from the new directories
    * Properly register/unregister components
    * Maintain clean component state

**Visual Warnings**
    All dev components are clearly marked with yellow badges to prevent confusion between production and development components.

Best Practices
--------------

1. **Keep Dev Separate**: Never mix production and dev code in the same file
2. **Test Thoroughly**: Use dev components to test new features before moving to production
3. **Document Changes**: Add clear comments explaining experimental features
4. **Version Control**: Consider adding ``*_dev/`` to ``.gitignore`` if you don't want to commit dev code
5. **Clean Startup**: Always restart with dev components disabled for production use

Troubleshooting
---------------

Dev Components Not Loading
^^^^^^^^^^^^^^^^^^^^^^^^^^^

**Problem**: Enabled dev components but they don't appear in WebUI.

**Solutions**:

1. Verify the ``_dev`` directories exist:

   .. code-block:: bash

       ls -la interface_dev/ plugins_dev/ llm_engines_dev/

2. Check logs for import errors:

   .. code-block:: bash

       tail -f logs/synth_*.log | grep dev

3. Ensure you restarted synth after enabling the flag

4. Check that dev modules follow the correct naming convention (no ``__init__.py`` prefix)

Dev Components Still Showing After Restart
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

**Problem**: Dev components appear after restart even though flag should reset.

**Solutions**:

1. This should **never happen** - the flag is hardcoded to ``False`` in ``__init__``
2. Verify you're checking the correct synth instance
3. Check if someone modified the code to persist the flag (not recommended)

Component Import Errors
^^^^^^^^^^^^^^^^^^^^^^^^

**Problem**: Dev component fails to import with ``ModuleNotFoundError``.

**Solutions**:

1. Ensure the module is in the correct ``_dev`` directory
2. Check for missing dependencies in ``requirements.txt``
3. Verify the import paths are correct (use ``interface_dev.module_name`` not ``interface.module_name``)
4. Check Python syntax in the dev module

See Also
--------

* :doc:`components` - General component system documentation
* :doc:`interfaces` - Interface development guide
* :doc:`plugins` - Plugin development guide
* :doc:`cortex` - Cortex development guide
* :doc:`config_management` - Configuration management with ConfigVar
