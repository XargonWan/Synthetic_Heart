Usage Overview
==============

.. image:: res/usage_overview.png
   :alt: Usage Overview
   :width: 600px
   :align: center


synth operates as a modular AI persona with multiple LLM backends. You can
switch engines on the fly using the ``/cortex`` command in your preferred chat
platform. Supported modes include ``manual``, ``openai_chatgpt`` and
``selenium_chatgpt``.

Message forwarding is automatic when synth is mentioned or receives a private
message. The ``event`` plugin stores reminders in the configured database and
delivers them when due.

Contextual memory can be toggled with ``/context``. When enabled, the last ten
messages are injected into the prompt sent to the active LLM.

Component Management
--------------------

synth automatically discovers and loads components at startup:

**Available Interfaces**
    - ``telegram_bot``: Telegram integration
    - ``discord_interface``: Discord bot support
    - ``reddit_interface``: Reddit posting and monitoring
    - ``webui``: Browser-based interface with VRM avatar animations
    - ``cli``: Command-line interface

**Available Plugins**
    - ``terminal``: Shell command execution
    - ``ai_diary``: Personal memory system
    - ``event``: Scheduled reminders
    - ``weather``: Weather information
    - ``message_plugin``: Cross-platform messaging

**Available Cortex Engines**
    - ``openai_chatgpt``: OpenAI API integration
    - ``google_cli``: Google Gemini via CLI
    - ``selenium_chatgpt``: Browser-based ChatGPT
    - ``manual``: Human trainer input

Runtime Commands
----------------

**Engine Management**
    - ``/cortex``: List available cortex engines (``/llm`` deprecated alias); output now includes per-scope overrides (live, grillo, trainer)
    - ``/cortex <engine_name>``: Switch to a specific engine (``/llm <engine_name>`` deprecated)
    - ``/cortex_live <engine_name>``: Set the live/base cortex engine (alias of ``/cortex``)
    - ``/cortex_grillo <engine_name>``: Override the engine used by grillo beats
      (use without arguments to see current override)
    - ``/cortex_trainer <engine_name>``: Override the engine used for trainer
      messages
    - ``/model``: List available models for the active engine
    - ``/model <model_name>``: Switch the active model (e.g. ``gemini_api``)

**Context Control**
    - ``/context on``: Enable message context injection
    - ``/context off``: Disable message context injection

**System Information**
    - ``/status``: Show system status and active components
    - ``/help``: Display available commands

Configuration
-------------

Components are configured through environment variables in the ``.env`` file:

**Core Settings**
    - ``TRAINER_IDS``: Authorized user IDs for sensitive operations
    - ``NOTIFY_ERRORS_TO_INTERFACES``: Error notification destinations

**Database**
    - ``DB_HOST``, ``DB_USER``, ``DB_PASS``, ``DB_NAME``: MariaDB credentials

**Interface Tokens**
    - ``BOTFATHER_TOKEN``: Telegram bot token
    - ``DISCORD_BOT_TOKEN``: Discord bot token
    - ``REDDIT_*``: Reddit API credentials

**File Storage**
    - ``SYNTH_EXPOSED_STORAGE_ROOT``: Files uploaded for `file`-type exposed variables are stored here. Defaults to ``/config/storage``.

**Cortex API Keys**
    - ``OPENAI_API_KEY``: OpenAI API access
    - ``GEMINI_API_KEY``: Google Gemini access

**Gemini API (REST)**
    - ``GEMINI_API_BASE_URL``: Override the Gemini REST base URL (default: https://generativelanguage.googleapis.com)
    - ``GEMINI_MODEL``: Select the Gemini model (visible in the WebUI when ``gemini_api`` is loaded)

The modular architecture ensures that components only load when their required configuration is present, making the system highly flexible and secure.

WebUI and VRM Avatars
----------------------

The WebUI interface provides a browser-based visual representation of synth using VRM avatar models. The system includes:

**Visual Feedback**
    - **Idle Animation**: Natural relaxed pose when not active
    - **Talking Animation**: Synchronized with text generation (estimated duration)
    - **Thinking Animation**: Visual indicator during message processing (placeholder)

**3D Environment**
    - Persistent 3D room with floor and grid
    - Visible even without a VRM model loaded
    - Professional lighting setup for optimal model presentation

**Avatar Management**
    - Upload custom VRM models via the WebUI
    - Activate/deactivate models on the fly
    - Automatic animation retargeting for compatible models

For detailed information about the VRM animation system, see :doc:`vrm_animations`.
