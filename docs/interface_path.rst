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

- ``interface_name``: The interface registry key (e.g., ``telegram_bot``, ``discord_bot``, ``matrix_chat``)
- ``target_identifier``: Primary conversation identifier (chat_id, channel_id, room_id, etc.)
- ``thread_identifier`` (optional): Thread or sub-conversation identifier

The path is split on ``/`` with **no escaping**: a level that itself contains
``/`` is split into two levels. The number of levels and what they mean belong to
each interface; the Core only treats the first segment as the interface name. The
Discord channel path, for example, has the guild as its first level and the channel
as its second.

**Examples:**

- ``telegram_bot/-1001234567890`` - Telegram channel/group chat
- ``telegram_bot/123456789/987`` - Telegram chat with thread
- ``discord_bot/987654321/123456`` - Discord channel with thread
- ``matrix_chat/!room:matrix.org/$event`` - Matrix room with thread event (see the
  note under "Matrix Interface" about the ``matrix`` prefix)

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

   # Parse path into (interface name, [levels]); a tuple, not a dict
   name, levels = parse_interface_path('telegram_bot/123456789/987')
   # Result: ('telegram_bot', ['123456789', '987'])

   # Parse without thread
   name, levels = parse_interface_path('telegram_bot/123456789')
   # Result: ('telegram_bot', ['123456789'])

   # Legacy chat/thread view of the first two levels
   from core.interface_path_utils import extract_legacy_ids
   extract_legacy_ids('telegram_bot/123456789/987')
   # Result: {'interface': 'telegram_bot', 'chat_id': '123456789', 'thread_id': '987'}

``parse_interface_path`` does not validate: ``'a//b'`` parses to
``('a', ['', 'b'])``. Use ``is_valid_interface_path`` to check a path. These
behaviours are pinned by ``tests/test_extension_baseline_contracts.py``.

Validation
~~~~~~~~~~

.. code-block:: python

   from core.interface_path_utils import is_valid_interface_path

   # Validate path format
   valid = is_valid_interface_path('telegram_bot/123456789/987')  # True
   valid = is_valid_interface_path('synth_webui')  # True (a bare name is valid)
   valid = is_valid_interface_path('telegram_bot//987')  # False (empty segment)
   valid = is_valid_interface_path('')  # False

A path is valid when it is a non-empty string and every ``/``-separated segment is
non-empty and not just whitespace.

Database Integration
--------------------

The chat history cache identifies a conversation by its interface path:

**Schema:**

- A single ``interface_path VARCHAR(512)`` column replaces the older separate
  ``chat_id``, ``interface`` and ``thread_id`` columns.
- The table's primary key is ``id``; ``(interface_path, created_at)`` is unique.
- ``load_chat_history(path, match_chat_level=True)`` also matches the thread
  variants of a chat: it matches ``chat_key/%`` where the chat key is the first
  two segments.
- Two further tables are keyed by path: ``interface_paths`` (primary key
  ``interface_path``, with ``last_used`` and ``segment_labels``) and
  ``interface_aliases`` (``@username`` to numeric id).

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

Telegram User Account
~~~~~~~~~~~~~~~~~~~~~

- **Path Format**: ``telegram/chat_id[/thread_id]`` or ``telegram/@username[/thread_id]``
- **Writing first**: a user account can only message a bare user id it has seen.
  the ``official/telegram`` extension (``extensions_src/telegram/tl.py``, ``Account.ensure_peer``) looks for the
  person once in the groups the account shares with them, and otherwise
  fails with a "Chat not found" error that says what to do (they message the
  account first, or the sender uses their ``@username``). A miss is remembered
  for ten minutes. A destination written as ``@username`` is resolved by
  Telegram and settled on the numeric id before it is sent.

Aliases (``@username`` instead of the numeric id)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The first segment of a Telegram path is a number, which tells a person (and the
model) nothing. ``core/interface_aliases.py`` keeps ``id <-> @username`` pairs
and converts in both directions, and **the username form is the preferred way to
build a path**:

- The **numeric id stays canonical**: history, memories, ``interface_paths``,
  trainer checks and routing all key on ``telegram/31321637``. An alias can
  change or be missing, so it is never stored in place of the id.
- ``to_display_path`` is what the model is shown (``source.interface_path``,
  ``current_chat``, the channel legend, the paths listed by the
  ``recon_interface_reference`` plugin): ``telegram/@jay/12``.
- ``to_canonical_path`` maps the model's answer back, once, in
  ``core.message_chain`` before anything routes, compares or validates it. An
  alias that is not known is left as written (the Telegram user interface can
  still resolve a public username by itself).
- Pairs are learnt from incoming traffic by both Telegram interfaces
  (``remember_message_alias``): the sender's username in a private chat, the
  chat's own username for a public group or channel. A renamed account replaces
  its old alias; a username is unique per interface. Table: ``interface_aliases``.

Matrix Interface
~~~~~~~~~~~~~~~~

- **Path Format**: ``matrix_chat/room_identifier[/thread_event_id]`` (the registry key is ``matrix_chat``)
- **Generation**: ``build_interface_path('matrix_chat', room_identifier, thread_event_id)``
- **Thread Support**: Uses Matrix thread events
- **Known inconsistency**: the queue and ``resolve_and_touch`` use the
  ``matrix_chat/...`` prefix, but the inbound history write and the history of
  sent messages use the literal prefix ``matrix``, so one room can be stored under
  two prefixes. Whether this is intended is not established by the code; see
  :doc:`plugin_platform/open_questions`.

OpenAI-Compatible Server
~~~~~~~~~~~~~~~~~~~~~~~~~~

- **Path Format**: ``openai_api/<conversation>`` (declared as ``openai_api/{chat}`` in
  the ``official/openai_api`` extension's manifest)
- **Thread Support**: Single-level conversations
- Formerly ``ollama_serve``: the startup migration ``migrate_ollama_serve_to_openai_api``
  renames the stored paths and aliases.

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