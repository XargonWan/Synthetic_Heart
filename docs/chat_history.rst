Chat History System
==================

.. versionadded:: 1.0
   Persistent chat history cache with interface path support and automatic cleanup.

Overview
--------

The Chat History System provides persistent storage and retrieval of conversation history across container restarts. Messages are cached in a database with configurable limits and automatic cleanup, ensuring that SyntH maintains conversation context even after system restarts.

**Key Features:**

- **Interface Path Based**: Uses unified interface paths for consistent addressing
- **Configurable Limits**: Adjustable message history per conversation
- **Automatic Cleanup**: Old messages removed to prevent database bloat
- **Timestamp Tracking**: All messages include precise timestamps
- **Cross-Platform**: Works across Telegram, Discord, Matrix, and other interfaces

Architecture
------------

The chat history system consists of:

1. **Database Layer**: Persistent storage with optimized indexing
2. **Cache Management**: Automatic cleanup and size limits
3. **Interface Integration**: Seamless integration with all chat interfaces
4. **Context Manager**: Centralized message tracking and retrieval

Database Schema
---------------

**chat_history_cache Table:**

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

**Fields:**

- ``interface_path``: Unified address (e.g., ``telegram_bot/123456/789``)
- ``sender_name``: Display name of message sender
- ``sender_id``: Unique identifier of sender
   Chat Archiving (WebUI)
   ----------------------

   The Web UI supports archiving and restoring entire conversations for the single persistent session. Archives are filesystem-backed JSON snapshots located under ``backups/chat_archives/``. The following endpoints are exposed on the Web UI API:

   - ``POST /api/chat/archive``
      Archive the current conversation; returns ``{ "success": true, "archive_id": "..." }``. The current chat is cleared after archiving.
   - ``GET /api/chat/archives``
      List available archives with basic metadata.
   - ``POST /api/chat/restore``
      Restore an archive into the current persistent session (payload: ``{ "archive_id": "..." }``). The current chat will be archived first.
   - ``DELETE /api/chat/archives/{archive_id}``
      Delete an archive file.

   Notes:
   - Archiving is filesystem-backed for the MVP to avoid DB schema changes. Production deployments may prefer database-backed archives or additional metadata storage.
   - Archiving/restore operations are atomic from the client's perspective and broadcast restored messages to the WebSocket-connected client.

   WebUI Session Persistence
   -------------------------

   The Web UI uses a single persistent session per deploy (single user semantics). The session id is stored in ``backups/webui_session_id.txt`` on the server and is used as the ``interface_path`` namespace for chat history (``synth_webui/<session_id>``). This allows the Web UI to restore conversation history when the container restarts.
- ``message_text``: Full message content
- ``timestamp``: Message timestamp with microsecond precision

**Indexes:**

- ``idx_interface_path``: Fast lookup by conversation
- ``idx_timestamp``: Efficient time-based queries
- ``uniq_message``: Prevents duplicate messages

Configuration
-------------

**Environment Variables:**

- ``CHAT_HISTORY``: Maximum messages per conversation (default: 10)
- ``CHAT_HISTORY_LIMIT``: Alias for ``CHAT_HISTORY``

**Database Requirements:**

- MySQL/MariaDB with LONGTEXT support
- UTF-8 character set for international content
- Automatic table creation on startup

API Reference
-------------

**Core Functions:**

``init_chat_history_table()``
   Create database table if it doesn't exist

``save_chat_message(interface_path, message_text, sender_name, sender_id, timestamp)``
   Save a message to the cache with automatic cleanup

``load_chat_history(interface_path)``
   Load recent messages for a conversation

``load_chat_history_for_guild(guild_id, since=None, limit=100)``
   Load recent text messages across all interface paths belonging to a Discord
   guild (paths matching ``discord_<guild>_%``).  This is used by the live voice
   synchronization subsystem to mirror text channel activity into ongoing
   voice sessions.

**Usage Examples:**

.. code-block:: python

   from core.chat_history_cache import save_chat_message, load_chat_history

   # Save a message
   await save_chat_message(
       interface_path="telegram_bot/123456789/987",
       message_text="Hello, how are you?",
       sender_name="user",
       sender_id="12345"
   )

   # Load conversation history
   history = await load_chat_history("telegram_bot/123456789/987")
   # Returns deque of message objects in chronological order

Message Lifecycle
-----------------

**1. Message Reception:**

When a message is received from any interface:

.. code-block:: python

   # Interface generates interface_path
   interface_path = build_interface_path('telegram_bot', chat_id, thread_id)

   # Context manager saves to history
   await add_message_to_context(
       interface_path=interface_path,
       message_text=text,
       sender_name=username,
       sender_id=user_id
   )

**2. Automatic Cleanup:**

The system automatically removes old messages:

.. code-block:: python

   # Delete messages beyond limit for this conversation
   DELETE FROM chat_history_cache
   WHERE interface_path = %s
   AND id NOT IN (
       SELECT id FROM (
           SELECT id FROM chat_history_cache
           WHERE interface_path = %s
           ORDER BY timestamp DESC
           LIMIT %s
       ) AS temp
   )

**3. LLM Context Integration:**

History is loaded for LLM prompts:

.. code-block:: python

   # Load recent history for context
   history = await load_chat_history(interface_path)

   # Format for LLM prompt
   context_lines = []
   for msg in history:
       context_lines.append(f"{msg['username']}: {msg['text']}")

   prompt = f"Previous conversation:\n" + "\n".join(context_lines[-10:])

Interface Integration
---------------------

**Telegram Bot:**

.. code-block:: python

   # In telegram_bot.py
   from core.chat_context_manager import add_message_to_context

   await add_message_to_context(
       interface_path=interface_path,
       message_text=text,
       sender_name=username,
       sender_id=str(user_id),
       message_id=message.message_id,
       timestamp=message.date.isoformat()
   )

**Discord Bot:**

.. code-block:: python

   # In discord_interface.py
   await add_message_to_context(
       interface_path=interface_path,
       message_text=content,
       sender_name=message.author.display_name,
       sender_id=str(message.author.id),
       message_id=message.id,
       timestamp=message.created_at.isoformat()
   )

**Matrix:**

.. code-block:: python

   # In matrix_interface.py
   await add_message_to_context(
       interface_path=interface_path,
       message_text=text,
       sender_name=_extract_username(event.sender),
       sender_id=event.sender,
       message_id=event.event_id,
       timestamp=date.isoformat()
   )

Self Message Inclusion
----------------------

.. versionchanged:: 1.0
   SyntH's responses are automatically included in chat history.

When SyntH sends a message through any interface, it is automatically saved with ``sender_name="self"``:

.. code-block:: python

   # In interface send_message methods
   await save_chat_message(
       interface_path=interface_path,
       message_text=text,
       sender_name="self",  # Key identifier
       sender_id="self"
   )

This ensures the LLM can see its own previous responses in conversation context.

Performance Considerations
--------------------------

**Indexing Strategy:**

- Interface path indexing enables fast conversation lookup
- Timestamp indexing supports efficient cleanup operations
- Unique constraint prevents duplicate messages

**Cleanup Automation:**

- Automatic deletion of old messages prevents unbounded growth
- Per-conversation limits maintain consistent memory usage
- Background cleanup doesn't impact message processing

**Memory Management:**

- Deque-based loading provides efficient recent message access
- Configurable limits prevent memory exhaustion
- Timestamp-based ordering ensures chronological accuracy

Troubleshooting
---------------

**Common Issues:**

**Missing chat history:**
   Check ``CHAT_HISTORY`` configuration value

**Messages not saving:**
   Verify database connectivity and permissions

**Duplicate messages:**
   Check for unique constraint violations

**Performance degradation:**
   Monitor table size and cleanup frequency

**Debug Commands:**

.. code-block:: bash

   # Check table structure
   mysql -e "DESCRIBE syntheart.chat_history_cache;"

   # Count messages per conversation
   mysql -e "
   SELECT interface_path, COUNT(*) as msg_count
   FROM syntheart.chat_history_cache
   GROUP BY interface_path
   ORDER BY msg_count DESC
   LIMIT 10;
   "

   # Check recent messages
   python3 -c "
   import asyncio
   from core.chat_history_cache import load_chat_history
   history = asyncio.run(load_chat_history('telegram_bot/123456789'))
   print('Recent messages:', len(history))
   for msg in history[-3:]:
       print(f'{msg[\"username\"]}: {msg[\"text\"][:50]}...')
   "

Migration Notes
---------------

**From Legacy System:**

The system migrated from separate ``chat_id``, ``interface``, ``thread_id`` columns to unified ``interface_path``:

- **Before:** Multiple columns with complex joins
- **After:** Single ``interface_path`` column with simple queries

**Data Migration:**

Existing data is automatically migrated during startup. No manual intervention required.

**Backward Compatibility:**

Legacy chat_id/thread_id systems are supported through conversion utilities in ``interface_path_utils.py``.</content>
<parameter name="filePath">/videodrome/videodrome-deployment/Synthetic_Heart/docs/chat_history.rst