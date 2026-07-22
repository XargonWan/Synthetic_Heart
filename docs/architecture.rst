Architecture Overview
=====================

.. image:: res/architecture.png
    :alt: synth Architecture Diagram
    :width: 600px
    :align: center


The Synthetic Heart is a highly modular AI persona system designed for extensibility and flexibility. The architecture separates concerns into distinct layers, allowing independent development and swapping of components without modifying the core system.

Core Principles
---------------

**Modularity and Extensibility**
    Components (Cortex engines, plugins, and interfaces) are not hardcoded in the core. Instead, they are dynamically discovered and loaded at runtime through auto-registration mechanisms. This ensures that new functionality can be added by simply placing compatible modules in the appropriate directories.

**Message-Driven Architecture**
    All interactions flow through a centralized message chain that handles JSON action parsing, validation, and execution. Components communicate through well-defined interfaces rather than direct coupling.

**Registry-Based Component Management**
    Components register themselves with central registries, providing metadata about their capabilities. The core system then orchestrates interactions based on this self-reported information.

System Components
-----------------

``core``
    The foundational layer containing:
    
    - **Message Chain** (``message_chain.py``): Central orchestrator for incoming messages, JSON extraction, and action execution flow.
    - **Action Parser** (``action_parser.py``): Executes validated actions from parsed JSON, routing them to appropriate component handlers.
    - **Core Initializer** (``core_initializer.py``): Auto-discovers and loads all components from ``plugins/``, ``cortex/``, and ``interface/`` directories.
    - **Registries**: Centralized management for Cortex engines, interfaces, plugins, and validation rules.
    - **Transport Layer**: Handles communication between components and interfaces.
    - **Interface Path System**: Unified hierarchical addressing system using ``interface_path`` format (e.g., "telegram_bot/chat_id/thread_id") for consistent conversation identification across all platforms. See :doc:`interface_path` for detailed documentation.

``interface``
    Chat platform integrations (Telegram, Discord, Reddit, etc.). Each interface:
    
    - Inherits from ``AIPluginBase`` or implements compatible interfaces
    - Registers supported actions (e.g., sending messages, media)
    - Handles platform-specific message formatting and rate limiting
    - Provides trainer ID validation for security

``cortex/``
    Runtime engine implementations (OpenAI, Google Gemini, manual input, etc.). Each engine:
    
    - Extends ``AIPluginBase`` for consistent interface
    - Handles prompt construction and response generation
    - Manages model-specific limits and capabilities
    - Supports both text and multimodal interactions

    Cortex engines expose configuration variables (API keys, endpoints, model selectors)
    through the shared settings registry. These variables can be edited in the Web UI
    even when the engine is not active. When a required variable is missing, the engine
    may load in a **degraded** state and is surfaced as failed in the Components view so
    operators can see it needs attention. Variables marked with
    ``needs_component_reload`` automatically trigger a reload of the owning Cortex engine
    after an update, allowing API keys to be applied without restarting the whole system.

Prompt Assembly
---------------

Prompt assembly is now centered on the typed
``core.prompt_request.PromptRequest`` model.

- ``core.prompt_engine.build_prompt_request()`` is the canonical builder.
- ``build_json_prompt()`` remains as a deprecated alias for compatibility.
- The builder still returns a compatibility dict for older callers, but it now
    attaches the typed request under ``__prompt_request``.
- Migrated engines render the typed request natively through
    ``core.prompt_renderers`` instead of serializing the full prompt as one
    indented JSON blob.

This split lets the runtime preserve conversation turns, keep system
instructions stable for caching, and handle chat, grillo, delivery, and live
prompt modes without forcing every engine down the same text-only path.

``plugins``
    Action providers that extend synth's capabilities (terminal access, weather, file operations, etc.). Each plugin:
    
    - Implements action schemas with required/optional fields
    - Provides prompt instructions for LLM integration
    - Handles validation and execution of specific actions
    - Can be pure action providers or full AI plugins

Advanced Components
-------------------

``Event Handler``
    Allows the synth to schedule and manage events for future execution. Events can be spontaneous or user-requested, enabling proactive behaviors like sending morning greetings, weather warnings, or checking in on users. This creates a sense of initiative and care.

``Thinking Engine (G.R.I.L.L.O.)``
    A low-priority, configurable background process that generates internal thoughts and proposals for the synth. It picks up on conversations or ideas, prompting autonomous actions. For example, if users discuss AI art limitations, the synth might research human art concepts or artist communities. This demonstrates emerging "will" and self-directed behavior, though full implementation depends on available plugins like web search.

Message Flow
------------

The following diagram shows the complete message processing pipeline:

.. graphviz::

   digraph message_flow {
       rankdir=TB;
       node [shape=box, style=rounded];
       
       User [label="User Message"];
       Interface [label="Interface\n(Telegram/Discord/etc.)"];
       MessageChain [label="Message Chain"];
       JSONExtract [label="JSON Extraction"];
       ActionParser [label="Action Parser"];
       Component [label="Component\n(Plugin/Cortex)"]; 
       Response [label="Response"];
       
       User -> Interface;
       Interface -> MessageChain;
       MessageChain -> JSONExtract;
       JSONExtract -> ActionParser [label="Valid JSON"];
       JSONExtract -> Corrector [label="Invalid JSON"];
       Corrector -> MessageChain [label="Retry"];
       ActionParser -> Component;
       Component -> Response;
       Response -> Interface;
       Response -> User;
       
       Corrector [label="Corrector Middleware\n(LLM-assisted)"];
   }

Message Queue System
--------------------

The Synthetic Heart uses an **asyncio.PriorityQueue** for processing messages in the correct order. Messages are prioritized to ensure high-priority events (like scheduled messages) are processed before regular chat messages.

**Priority Levels**:
- **HIGH_PRIORITY (0)**: Scheduled events, urgent notifications
- **NORMAL_PRIORITY (1)**: User messages, radio speech — user-facing traffic
- **AGENT_PRIORITY (2)**: Agentic turns / tool work — below user traffic, above autonomous beats
- **LOW_PRIORITY (3)**: Autonomous G.R.I.L.L.O. beats, processed only when the queue is idle

**Agentic turns run detached**: When the Fast/Agent router routes a turn to the
Agent Lane, the reasoning loop is launched as a *detached* ``asyncio`` task
rather than executed inline inside the consumer lock. This means a long
multi-step agent turn never freezes Synth — user messages queued while the agent
is working are still answered promptly, and the agent turn interleaves with them
cooperatively at the event-loop level. Each turn is persisted to the
``agent_tasks`` table from the moment it starts (status ``running``) so it is
durable across a container restart; the startup recovery sweep reconciles any
turn left ``running`` by a previous process.

**PriorityQueue Fix**: To prevent ``TypeError`` when comparing messages with identical priorities, the queue uses a **monotonic counter** as tiebreaker. Each message is stored as ``(priority, counter, item)`` instead of ``(priority, item)``, ensuring consistent ordering even when priorities are equal.

**Consumer Loop**: A dedicated async task continuously processes queued messages, ensuring no message loss and proper serialization of operations.

Database Connection Pool
-------------------------

The system maintains a **single aiomysql connection pool** with carefully tuned parameters:

- **Max Size**: 5 connections (reduced from 8 to prevent bio_manager timeouts)
- **Min Size**: 1 connection (keeps one connection alive)
- **Timeout**: 10 seconds for connection acquisition

**Bio Manager Optimization**: The bio_manager plugin caches table initialization and uses async database operations to prevent ``TimeoutError`` when retrieving user profiles during prompt injection.

Recent Fixes
------------

**PriorityQueue TypeError Fix (November 2025)**:

The message queue was experiencing ``TypeError: '<' not supported between instances of 'dict' and 'dict'`` when processing messages with identical priorities.

**Root Cause**: Python's ``heapq`` compares tuple elements when priorities are equal, but dict objects cannot be compared with ``<``.

**Solution**: Added monotonic counter as tiebreaker in queue tuples:
.. code-block:: python

    # Before: (priority, item) - fails when priority equal
    # After: (priority, counter, item) - always comparable
    
    _counter += 1
    await _queue.put((priority, _counter, item))

**Files Modified**:
- ``core/message_queue.py``: Added global counter and updated all ``put()`` calls

**Impact**: Eliminates queue crashes and ensures consistent message processing order.

**Detailed Flow:**

1. **Message Reception**: Interface receives message from user and forwards to message chain
2. **JSON Detection**: Message chain attempts to extract JSON actions from the text
3. **Validation & Correction**: If JSON is invalid, corrector middleware queries the active Cortex engine to fix it
4. **Action Execution**: Validated actions are parsed and routed to appropriate components
5. **Response Generation**: Components execute actions and generate responses
6. **Output Delivery**: Responses are sent back through the originating interface

Component Auto-Discovery
------------------------

Components are automatically discovered through a recursive import system:

- The core initializer scans ``plugins/``, ``cortex/``, and ``interface/`` directories
- Each Python file is imported and checked for a ``PLUGIN_CLASS`` attribute
- Compatible classes are instantiated and registered with appropriate registries
- Components self-report their capabilities through standardized methods
- No manual registration or configuration files required

This approach ensures that adding new functionality requires only:
1. Creating a compatible class in the appropriate directory
2. Implementing required interface methods
3. Restarting the application

Security and Validation
-----------------------

- **Trainer ID Validation**: Interfaces validate that commands come from authorized trainers
- **Action Validation**: All actions are validated against component schemas before execution
- **Rate Limiting**: Built-in rate limiting prevents abuse across all components
- **Error Handling**: Comprehensive error handling with user-friendly notifications

The architecture's modular design ensures that security policies can be consistently applied across all components without code duplication.

Unified Lane & Unified History
------------------------------

Synthetic Heart supports optional **Unified Lane** and **Unified History** behaviors
to improve continuity across interfaces and sessions.

**Unified Lane (Context Linking)**
    Allows multiple interface paths to map to a single shared context lane.
    This is useful when you want WebUI, Discord, and other interfaces to share
    a common conversation history.

    Configure the mapping via ``CONTEXT_LINK_MAP`` (JSON), for example:

    .. code-block:: json

        {
          "synth_webui/SESSION_UUID": "lane_Trainer_Main",
          "discord_bot/123456/0": "lane_Trainer_Main",
          "123456": "lane_Trainer_Main"
        }

    By default the WebUI uses a single persistent session id that is stored in
    ``backups/webui_session_id.txt``.  This ensures history and window state
    survive container restarts.  An **advanced, experimental** option
    ``MULTI_SESSION`` can be toggled to ``true``; when enabled each browser
    connection gets its own independent session.  This mode is unstable and
    should only be used for testing.

    The resolver checks for exact path matches first and then tries a user-id
    match using the second path segment (``interface/chat_id/...``).

**Unified History**
    When ``UNIFIED_HISTORY`` is enabled, the current chat history is built by
    merging global DB history with in-memory chat logs across all interfaces.
    This creates a shared “brain” for the synth. Disable this flag if you need
    strict isolation between users or channels.

    .. versionchanged:: 1.0
       Unified history entries coming from other chats are now prefixed with
       ``[from <interface_path>]`` to make it explicit they are not part of the
       current conversation.

    .. note::
       Cross-chat context is treated as ambient background, not shared social
       knowledge. The synth is instructed (via the ``CROSS-CHAT PRIVACY``
       directive in the master chat instructions and a per-block note on the
       ``[Recent context from other conversations]`` section) never to
       name-drop people from other chats to the current interlocutor, never to
       assume the current user knows them, and never to reference them unless
       the current user brings them up first. This applies to any non-current
       context (other chats, third-party memories, diary notes), not only to
       unified history.
