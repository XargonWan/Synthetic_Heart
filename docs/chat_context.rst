Chat Context System
===================

.. versionadded:: 1.0
   Comprehensive chat context management including history, error routing, and activity tracking.

Overview
--------

The Chat Context System manages conversation continuity, error handling, and activity tracking across all interfaces. This system ensures that:

- **Chat History**: Messages are persisted and available for LLM context
- **Self Messages**: SyntH's responses are properly included in conversation history
- **Error Routing**: LLM error messages follow the correct interface path
- **Activity Tracking**: Chat activity is automatically tracked without LLM intervention

Chat History Management
-----------------------

The system maintains persistent chat history using the ``core/chat_history_cache.py`` module. Messages are stored in a database table with automatic cleanup of old entries.

**Key Features:**

- **Interface Path Based**: Uses unified interface paths for consistent addressing
- **Limited History**: Configurable ``CHAT_HISTORY_LIMIT`` (default: 10 messages per chat)
- **Self Inclusion**: SyntH's responses are saved with ``sender_name="self"``
- **Timestamp Tracking**: All messages include creation timestamps

**Database Schema:**

.. code-block:: sql

   CREATE TABLE chat_history_cache (
       id INT AUTO_INCREMENT PRIMARY KEY,
       interface_path VARCHAR(512) NOT NULL,
       sender_name VARCHAR(255),
       sender_id VARCHAR(255),
       message_text LONGTEXT NOT NULL,
       timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
       INDEX idx_interface_path (interface_path),
       INDEX idx_timestamp (timestamp),
       UNIQUE KEY uniq_message (interface_path, timestamp)
   );

**API Usage:**

.. code-block:: python

   from core.chat_history_cache import save_chat_message, load_chat_history

   # Save a message (automatically called by chat_context_manager)
   await save_chat_message(
       interface_path="telegram_bot/123456789/987",
       message_text="Hello world!",
       sender_name="user",
       sender_id="12345"
   )

   # Load history for LLM context
   history = await load_chat_history("telegram_bot/123456789/987")
   # Returns deque of message objects

Self Message Inclusion
~~~~~~~~~~~~~~~~~~~~~~

.. versionchanged:: 1.0
   SyntH responses are now automatically included in chat history with sender_name="self".

When SyntH sends a message through any interface, it is automatically saved to the chat history cache with ``sender_name="self"`` and ``sender_id="self"``. This ensures the LLM can see its own previous responses in the conversation context.

**Implementation:**

- **Centralized**: Handled in ``core/chat_context_manager.py`` → ``add_message_to_context()``
- **Interface Agnostic**: Works across Telegram, Discord, Matrix, and other interfaces
- **Automatic**: No manual intervention required - happens whenever a message is sent

**Example History Entry:**

.. code-block:: json

   {
       "message_id": "msg_123",
       "user_id": "self",
       "username": "self",
       "text": "Hello! How can I help you today?",
       "timestamp": "2025-11-20T10:30:00Z",
       "interface_path": "telegram_bot/123456789/987"
   }

LLM Error Message Routing
-------------------------

.. versionchanged:: 1.0
   LLM error messages now correctly follow the interface_path from the original message.

When the LLM fails to generate valid JSON actions, the system sends a fallback error message. This message is now automatically routed to the same interface and conversation where the original message was received.

**Error Scenarios:**

- **JSON Parsing Errors**: Invalid JSON structure in LLM response
- **Action Validation Failures**: Actions don't match expected schema
- **Timeout Errors**: LLM response takes too long
- **Corrector Failures**: Unable to fix invalid responses

**Routing Implementation:**

The error message routing is handled in ``core/message_chain.py`` → ``send_llm_fallback_message()``:

.. code-block:: python

   async def send_llm_fallback_message(bot, message: SimpleNamespace, failure_reason: str, context: dict = None) -> str:
       # Extract interface_path from message or context
       interface_path = getattr(message, 'interface_path', None)
       if not interface_path and context:
           interface_path = context.get('interface_path')

       # Send through transport layer with correct routing
       await universal_send(
           bot.send_message,
           chat_id,
           text=fallback_text,
           interface_path=interface_path,  # Ensures correct routing
           is_llm_response=True
       )

**Corrector Middleware:**

The corrector system (``core/transport_layer.py`` → ``run_corrector_middleware()``) also preserves interface paths when attempting to fix LLM errors:

.. code-block:: python

   # Preserve interface_path in correction message
   correction_message.interface_path = None
   if message and hasattr(message, 'interface_path'):
       correction_message.interface_path = message.interface_path
   elif context and 'interface_path' in context:
       correction_message.interface_path = context['interface_path']

Automatic Chat Activity Tracking
---------------------------------

.. versionchanged:: 1.0
   Chat activity tracking is now automatic and centralized, no longer requiring LLM intervention.

The ``update_chat_activity`` action was previously exposed to the LLM, requiring it to "decide" when to update activity timestamps. This was inefficient since activity tracking is a mechanical operation that should happen automatically.

**Changes Made:**

- **Removed from LLM Actions**: ``update_chat_activity`` is no longer in ``get_supported_actions()``
- **Centralized Implementation**: Activity tracking now happens automatically in ``core/chat_context_manager.py``
- **Interface Agnostic**: Works across all interfaces without duplication

**Implementation:**

.. code-block:: python

   # In core/chat_context_manager.py → add_message_to_context()
   # Automatically update chat activity (mechanical action, centralized here)
   try:
       from plugins.recent_chats import update_chat_activity
       import asyncio
       # Extract chat_id from interface_path (format: interface/chat_id/thread_id)
       parts = interface_path.split('/')
       chat_id = parts[1] if len(parts) > 1 else interface_path
       asyncio.create_task(update_chat_activity(
           chat_id=chat_id,
           metadata={
               'username': sender_name,
               'user_id': sender_id,
               'interface_path': interface_path
           }
       ))
   except Exception as e:
       log_debug(f"[context_manager] Failed to update chat activity: {e}")

**Benefits:**

- **Reduced Prompt Size**: One less action for LLM to consider
- **Automatic Operation**: No LLM reasoning required for basic tracking
- **Consistent Behavior**: All interfaces track activity the same way
- **Performance**: Eliminates unnecessary LLM decision-making

Recent Chats Plugin
-------------------

The ``plugins/recent_chats.py`` plugin provides additional functionality for managing chat activity:

**Available Actions:**

- ``get_recent_chats``: Retrieve most recently active chats
- ``cleanup_old_chats``: Remove old chat records

**Database Schema:**

.. code-block:: sql

   CREATE TABLE recent_chats (
       chat_id VARCHAR(255) PRIMARY KEY,
       last_active DOUBLE NOT NULL,
       metadata TEXT,
       created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
       INDEX idx_last_active (last_active)
   );

**Usage Example:**

.. code-block:: python

   from plugins.recent_chats import get_recent_chats

   # Get 10 most recent chats
   recent = await get_recent_chats(10)
   for chat in recent:
       print(f"Chat {chat['chat_id']}: {time.ctime(chat['last_active'])}")

Configuration
-------------

**Chat History Settings:**

- ``CHAT_HISTORY``: Maximum messages per chat (default: 10)
- ``CHAT_HISTORY_LIMIT``: Alias for ``CHAT_HISTORY``

**Activity Tracking:**

- Automatic - no configuration required
- Metadata includes username, user_id, and interface_path

**Error Handling:**

- Fallback messages use ``FAILED_MESSAGE_TEXT`` config variable
- Error routing preserves original conversation context
- Activity tracking failures are logged but don't block message processing

Troubleshooting
---------------

**Empty Chat History:**

- Check ``CHAT_HISTORY`` configuration value
- Verify database connectivity
- Ensure ``init_chat_history_table()`` was called during startup

**Missing Self Messages:**

- Confirm interface is calling ``add_message_to_context()``
- Check that ``sender_name="self"`` is being used
- Verify database write permissions

**Error Message Routing Issues:**

- Ensure ``interface_path`` is set on incoming messages
- Check that context includes ``interface_path``
- Verify transport layer routing configuration

**Activity Tracking Problems:**

- Confirm ``plugins/recent_chats.py`` is loaded
- Check database table creation
- Verify async task execution</content>
<parameter name="filePath">/videodrome/videodrome-deployment/Synthetic_Heart/docs/chat_context.rst