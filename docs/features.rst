Features
========

.. image:: res/features.png
    :alt: synth Features Overview
    :width: 600px
    :align: center


Synthetic Heart is a highly modular AI system with extensible capabilities. The core system automatically discovers and integrates components at runtime, ensuring that functionality can be added or removed without code changes.

Modular Architecture
--------------------

**Component Auto-Discovery**
    synth automatically scans ``interface/``, ``plugins/``, and ``cortex/`` directories to load compatible components. No manual registration or configuration files required.

**Zero Hardcoding**
    Components are completely decoupled from the core. Adding new functionality requires only placing a compatible Python module in the appropriate directory.

**Runtime Flexibility**
    Switch Cortex engines, enable/disable plugins, and manage interfaces dynamically without restarting the system.

Adaptive Intelligence
---------------------

synth supports multiple language model backends with seamless switching. **Note: Currently, only the Selenium ChatGPT (Legacy) engine is fully functional. Other engines are experimental and may not work reliably.**

* ``selenium_chatgpt_legacy`` – Legacy browser-controlled ChatGPT (fully functional).
* ``selenium_chatgpt`` – Browser-controlled ChatGPT for advanced interaction (experimental).
* ``selenium_gemini`` – Browser-controlled Google Gemini with multimodal support (experimental).
* ``selenium_grok`` – Browser-controlled xAI Grok with reasoning capabilities (experimental).
* ``manual`` – Human trainer input for debugging and development.

**Runtime Engine Switching**
    Use ``/cortex <engine_name>`` to switch engines instantly during operation (``/llm <engine_name>`` is a deprecated alias).

Multi-Platform Integration
---------------------------

**Available Interfaces**
    - **Telegram**: Bot support with media handling
    - **Discord**: Full bot integration with threading
    - **Matrix**: Chat bridge with real-time messaging
    - **Ollama Compatible**: REST API bridge for external clients

**Cross-Platform Messaging**
    Send messages across different platforms using unified chat identifiers.

Progressive Web App (PWA) Mode (Experimental)
---------------------------------------------

**Status**: In development — may not be fully functional. Use with caution in production environments.

The Web UI can be installed as a Progressive Web App (PWA) on supported browsers to provide an app-like experience (fullscreen, home-screen install, and basic offline caching). A service worker is included (see ``res/synth_webui/static/service-worker.js``) and the Web UI templates register the service worker and handle the ``beforeinstallprompt`` event to show an install button.

Known limitations include partial offline behavior (real-time features are limited while offline), varying support for push notifications and background sync across browsers, and occasional install prompt availability depending on the client environment.

Desktop Notifications (Web UI)
------------------------------

The Web UI can display in-browser desktop notifications when new messages arrive while the tab is in the background.

Notes:
    - Requires HTTPS (or ``localhost``) due to browser security restrictions.
    - The browser permission prompt is requested on the first user action that sends a message.
    - Notifications can be toggled from the Web UI settings.

See ``docs/chat_history.rst`` for how session persistence and archives interact with the Web UI when using PWA mode.

Web UI Chat Archiving
---------------------

The Web UI supports archiving and restoring entire conversations for the single persistent session. Archives are filesystem-backed JSON snapshots stored under ``backups/chat_archives/`` and exposed via the Web UI API. Basic operations (archive, list, restore, delete) are available and documented in :doc:`chat_history`.

This archiving mechanism is intended for the Web UI MVP; production deployments may prefer database-backed archives or additional metadata storage.

Avatar System
-------------

**VRM Avatar Support**
    Synthetic Heart includes a default SyntH avatar for 3D visualization, but users can provide their own VRM avatar files for customization. The system supports animated states including idle, talking, and thinking, reflecting the persona's current activity.

Web UI Animation Sync
---------------------

The Web UI receives real-time animation commands from the backend. When the global action phase changes (e.g. ``THINKING``, ``WRITING``, ``TALKING``, ``IDLE``), the backend triggers the AnimationHandler to broadcast the corresponding animation state to Web UI clients. This keeps the backend authoritative for animation timing, while still allowing plugins/skins to override which files are used for each logical state.

Plugin Ecosystem
----------------

**Action Plugins**
    - ``ai_diary``: Personal memory and interaction tracking
    - ``bio_manager``: Persistent user profile management
    - ``blocklist``: User access control
    - ``message_map``: Message threading and tracking
    - ``message_plugin``: Cross-platform message routing
    - ``recent_chats``: Access to conversation history
    - ``time_plugin``: Time and location awareness
    - ``weather_plugin``: Real-time weather information

Contextual Memory
-----------------

**Message Context**
    Toggle contextual memory with ``/context on/off`` to include recent conversation history in prompts.

**Personal Memory**
    The AI Diary plugin provides persistent memory of interactions, emotions, and relationships.

**User Profiles**
    Bio management system maintains detailed user profiles for personalized interactions.

Security & Access Control
-------------------------

**Trainer ID Validation**
    Sensitive operations require authorization from configured trainer IDs.

**Rate Limiting**
    Built-in rate limiting prevents abuse across all components.

**Input Validation**
    All actions are validated against component schemas before execution.

**Error Handling**
    Comprehensive error handling with user-friendly notifications and automatic recovery.

Extensibility
-------------

**Creating New Components**
    Add functionality by implementing ``AIPluginBase`` and placing modules in appropriate directories:

    - **Plugins**: Extend capabilities with new actions
    - **Cortex engines**: Add support for new runtime engines (kinds: ``llm``, ``live``, ``agent``).
    - **Interfaces**: Integrate new communication platforms

**No Core Modifications**
    Components are self-contained and register their capabilities automatically. The core system remains unchanged when adding features.

**Development Friendly**
    Clear interfaces, comprehensive documentation, and example implementations make extension straightforward.

AI Diary
--------

The AI Diary is a modular plugin that provides synth with persistent memory of
interactions and activities. This plugin is completely self-contained and can be
removed without affecting the core system.

**Key Features:**

* **Modular Design**: The diary plugin is fully self-contained with internal
  configuration and dedicated database storage.

* **Automatic Entry Creation**: After each action execution, the system creates
  diary entries summarizing activities, involved parties, tags, and emotions.

* **Static Injection**: Recent diary entries are injected into prompts when space
  allows, providing context from previous interactions.

* **User Access**: Authorized users (trainers) can view diary entries using the
  ``/diary`` command.

* **Fail-Safe Operation**: The plugin automatically disables itself in case of
  errors, ensuring the core system continues functioning.

**Database Schema:**

.. code-block:: sql

   CREATE TABLE ai_diary (
       id INT AUTO_INCREMENT PRIMARY KEY,
       content TEXT NOT NULL,
       timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
       tags JSON,
       involved JSON,
       emotions JSON,
       interface_path VARCHAR(512),
       INDEX idx_timestamp (timestamp),
       INDEX idx_interface_path (interface_path)
   );

**Usage Example:**

After helping Takeshi with a coding task, synth automatically creates a diary entry:

.. code-block:: text

   === synth's Recent Diary ===

   📅 2024-01-15 14:30:22
   Helped Takeshi with bio update and security improvements
   #tags: bio, security, helpful
   #involved: Takeshi
   #emotions: helpful(8), focused(7)
   #context: telegram_bot/123456/2

   === End Diary ===

**Plugin Management:**

The diary plugin can be enabled/disabled dynamically:

.. code-block:: python

   from plugins.ai_diary import is_plugin_enabled, enable_plugin, disable_plugin

   # Check status
   if is_plugin_enabled():
       print("Plugin active")

   # Disable manually
   disable_plugin()

   # Re-enable (tests database connection)
   success = enable_plugin()

**Configuration:**

Each Cortex engine has its own configuration for diary integration:

* **OpenAI**: Up to 2000 characters for diary content
* **Selenium ChatGPT**: Up to 1500 characters
* **Google CLI**: Up to 1200 characters
* **Manual**: Up to 800 characters

This ensures optimal performance across different interfaces while maintaining
contextual awareness.
