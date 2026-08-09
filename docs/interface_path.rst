Interface Path System
====================

The Interface Path system is a unified hierarchical addressing mechanism used throughout Synthetic Heart to consistently identify and route messages across different chat platforms and conversation contexts.

Overview
--------

Interface paths provide a standardized way to address conversations regardless of the underlying platform (Telegram, Discord, Matrix, etc.). This abstraction layer enables:

- **Consistent Conversation Identification**: Same addressing format across all interfaces
- **Thread Support**: Hierarchical paths support nested conversations (threads, topics)
- **Platform Agnostic Routing**: Components can work with any interface without platform-specific code
- **Backward Compatibility**: Legacy chat_id/thread_id systems are supported through conversion utilities

Path Format
-----------

Interface paths follow a hierarchical structure separated by forward slashes:

.. code-block::

   interface_name/target_identifier[/thread_identifier]

**Components:**

- ``interface_name``: The interface type (e.g., ``telegram_bot``, ``discord_bot``, ``matrix``)
- ``target_identifier``: Primary conversation identifier (chat_id, channel_id, room_id, etc.)
- ``thread_identifier`` (optional): Thread or sub-conversation identifier

**Examples:**

- ``telegram_bot/-1001234567890`` - Telegram channel/group chat
- ``telegram_bot/123456789/987`` - Telegram chat with thread
- ``discord_bot/987654321/123456`` - Discord channel with thread
- ``matrix/!room:matrix.org/$event`` - Matrix room with thread event

Core Utilities
--------------

The ``core/interface_path_utils.py`` module provides essential functions for working with interface paths:

Building Paths
~~~~~~~~~~~~~~

.. code-block:: python

   from core.interface_path_utils import build_interface_path

   # Build from components
   path = build_interface_path('telegram_bot', '123456789', '987')
   # Result: 'telegram_bot/123456789/987'

   # Build without thread
   path = build_interface_path('telegram_bot', '123456789')
   # Result: 'telegram_bot/123456789'

Legacy Conversion
~~~~~~~~~~~~~~~~~

.. code-block:: python

   from core.interface_path_utils import build_interface_path_from_legacy

   # Convert old chat_id/thread_id format
   path = build_interface_path_from_legacy('telegram_bot', '123456789', '987')
   # Result: 'telegram_bot/123456789/987'

Parsing Paths
~~~~~~~~~~~~~

.. code-block:: python

   from core.interface_path_utils import parse_interface_path

   # Parse path into components
   components = parse_interface_path('telegram_bot/123456789/987')
   # Result: {'interface': 'telegram_bot', 'target': '123456789', 'thread': '987'}

   # Parse without thread
   components = parse_interface_path('telegram_bot/123456789')
   # Result: {'interface': 'telegram_bot', 'target': '123456789', 'thread': None}

Validation
~~~~~~~~~~

.. code-block:: python

   from core.interface_path_utils import is_valid_interface_path

   # Validate path format
   valid = is_valid_interface_path('telegram_bot/123456789/987')  # True
   valid = is_valid_interface_path('invalid_path')  # False

Database Integration
--------------------

The chat history cache has been updated to use interface paths as the primary key:

**Schema Changes:**

- Removed separate ``chat_id``, ``interface``, ``thread_id`` columns
- Added single ``interface_path VARCHAR(512)`` column
- Updated all indexes and constraints

**Migration Impact:**

All chat history operations now use interface paths:

- ``save_chat_message(interface_path, ...)``
- ``load_chat_history(interface_path, ...)``
- ``clear_chat_history(interface_path, ...)``

Message Flow Integration
-------------------------

Interface paths are integrated throughout the message processing pipeline:

1. **Interface Reception**: Each interface generates an interface path when receiving messages
2. **Context Building**: Paths are included in message context dictionaries
3. **LLM Processing**: Prompts include interface path information for context-aware responses
4. **Action Routing**: Message plugins reconstruct paths for proper delivery
5. **Response Sending**: Interfaces parse paths to extract platform-specific identifiers

**Example Flow (Telegram):**

.. code-block:: python

   # 1. Interface generates path
   interface_path = build_interface_path('telegram_bot', str(chat_id), str(thread_id))

   # 2. Context includes path
   context = {
       'interface_path': interface_path,
       'message': user_message,
       # ... other context
   }

   # 3. LLM receives path in prompt
   prompt = f"Respond to message from {interface_path}: {user_message}"

   # 4. Action includes path
   action = {
       'action': 'message_telegram_bot',
       'text': response,
       'interface_path': interface_path  # Critical for routing
   }

   # 5. Plugin reconstructs and sends
   send_payload = {
       'text': response,
       'interface_path': interface_path
   }

Interface-Specific Implementations
----------------------------------

Telegram Bot
~~~~~~~~~~~~

- **Path Format**: ``telegram_bot/chat_id[/thread_id]``
- **Generation**: ``build_interface_path('telegram_bot', str(message.chat_id), str(thread_id))``
- **Validation**: Requires ``interface_path`` in message actions
- **Thread Support**: Handles Telegram topic threads

Matrix Interface
~~~~~~~~~~~~~~~~

- **Path Format**: ``matrix/room_identifier[/thread_event_id]``
- **Generation**: ``build_interface_path('matrix', room_identifier, thread_event_id)``
- **Thread Support**: Uses Matrix thread events

OpenAI-Compatible Server
~~~~~~~~~~~~~~~~~~~~~~~~~~

- **Path Format**: ``ollama_serve/conversation_id`` (the ``ollama_serve``
  identifier is retained for backward compatibility)
- **Generation**: ``build_interface_path('ollama_serve', chat_id)``
- **Thread Support**: Single-level conversations

Best Practices
--------------

**Always Use Interface Paths:**

- Never pass separate ``chat_id`` and ``thread_id`` parameters
- Include ``interface_path`` in all message-related actions
- Use ``input.payload.source.interface_path`` for replies

**Validation:**

- Validate paths before using: ``is_valid_interface_path(path)``
- Handle invalid paths gracefully with fallback mechanisms

**Logging:**

- Include interface paths in debug logs for traceability
- Use paths instead of legacy identifiers in error messages

Error Message Routing
~~~~~~~~~~~~~~~~~~~~~

.. versionchanged:: 1.0
   LLM error messages now correctly follow the interface_path from the original message.

When LLM processing fails (JSON parsing errors, timeouts, validation failures), the system sends fallback error messages. These messages are automatically routed to the same interface and conversation using the preserved interface_path.

**Implementation:**

The error routing is handled in ``core/message_chain.py`` → ``send_llm_fallback_message()``:

.. code-block:: python

   # Extract interface_path from message or context
   interface_path = getattr(message, 'interface_path', None)
   if not interface_path and context:
       interface_path = context.get('interface_path')

   # Route through transport layer with preserved path
   await universal_send(
       bot.send_message,
       chat_id,
       text=fallback_text,
       interface_path=interface_path,  # Ensures correct routing
       is_llm_response=True
   )

**Benefits:**

- **Consistent Error Delivery**: Errors appear in the same chat where the problem occurred
- **Context Preservation**: Users see errors in the correct conversation thread
- **Interface Agnostic**: Works across Telegram, Discord, Matrix, etc.

**Migration:**

- Use ``build_interface_path_from_legacy()`` for gradual migration
- Update components incrementally to avoid breaking changes

Troubleshooting
---------------

**Common Issues:**

- **Missing interface_path in actions**: Ensure LLM prompts include path extraction instructions
- **Path reconstruction failures**: Verify message_plugin payload building includes path
- **Thread context loss**: Check that thread identifiers are properly preserved
- **Interface routing errors**: Validate path parsing in interface handlers

**Debug Commands:**

.. code-block:: bash

   # Check interface path parsing
   python3 -c "from core.interface_path_utils import parse_interface_path; print(parse_interface_path('telegram_bot/123/456'))"

   # Validate path format
   python3 -c "from core.interface_path_utils import is_valid_interface_path; print(is_valid_interface_path('telegram_bot/123/456'))"