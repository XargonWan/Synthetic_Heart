Event ID Flow Documentation
================================

Overview
--------

This document explains how ``event_id`` flows through the synth bot system, particularly for scheduled events and Telegram message handling.

Problem & Solution
------------------

The Problem
~~~~~~~~~~~

Previously, ``event_id`` was being passed through ``**kwargs`` all the way to Telegram Bot API calls (``bot.send_message()``, ``bot.edit_message_text()``), but these methods don't support ``event_id`` as a parameter, causing:

.. code-block:: python

    TypeError: ExtBot.send_message() got an unexpected keyword argument 'event_id'

The Solution
~~~~~~~~~~~~

Filter ``event_id`` from kwargs before Telegram API calls, while preserving it in the system for business logic.

Event ID Flow Diagram
----------------------

.. code-block:: text

    ┌─────────────────┐
    │   event_id      │  (Initial event trigger)
    │   in kwargs     │
    └─────────┬───────┘
              │
              ▼
    ┌─────────────────┐
    │ transport_layer │  
    │ telegram_safe_  │  • Copies event_id to context['event_id']
    │ send()          │  • Copies event_id to message.event_id
    └─────────┬───────┘  • Preserves event_id for business logic
              │
              ▼
    ┌─────────────────┐
    │ run_actions()   │  • Uses event_id from context
    │ (action_parser) │  • Processes scheduled events
    └─────────┬───────┘  • Marks events as delivered in DB
              │
              ├─────────────────┐
              │                 │
              ▼                 ▼
    ┌─────────────────┐  ┌─────────────────┐
    │ _send_with_     │  │ Database &      │
    │ retry()         │  │ Event System    │
    │                 │  │                 │
    │ ✅ event_id     │  │ ✅ Uses         │
    │ FILTERED from   │  │ event_id from   │
    │ kwargs          │  │ context/message │
    │                 │  │                 │
    │ ▼               │  │ • mark_event_   │
    │ bot.send_       │  │   delivered()   │
    │ message()       │  │ • event_        │
    │ (clean kwargs)  │  │   completed()   │
    └─────────────────┘  └─────────────────┘

Key Components
--------------

1. Transport Layer (``transport_layer.py``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Purpose**: Manages event_id at the system level

.. code-block:: python

    # Extract and preserve event_id
    if 'event_id' in kwargs:
        message.event_id = kwargs['event_id']    # For message object
        context['event_id'] = kwargs['event_id'] # For action context

2. Action Parser (``action_parser.py``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Purpose**: Uses event_id for business logic

.. code-block:: python

    # Mark scheduled events as delivered
    event_id = context.get("event_id") or getattr(original_message, "event_id", None)
    if event_id:
        await db.mark_event_delivered(event_id)
        event_dispatcher.event_completed(event_id)

3. Telegram Utils (``telegram_utils.py``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Purpose**: Filters event_id before Telegram API calls

.. code-block:: python

    # Filter out custom parameters not supported by Telegram Bot API
    valid_kwargs = {k: v for k, v in kwargs.items() if k not in ['event_id']}
    return await bot.send_message(chat_id=chat_id, text=text, **valid_kwargs)

Why This Design Works
---------------------

✅ **Separation of Concerns**
    - **Business Logic**: event_id available in context and message objects
    - **Telegram API**: Only receives valid parameters

✅ **Data Preservation**
    - event_id is preserved where needed (context, message)
    - Filtered only at the final API boundary

✅ **Error Prevention**
    - No more "unexpected keyword argument" errors
    - Maintains compatibility with Telegram Bot API

Usage Examples
--------------

Scheduled Event Processing
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # Event triggered with event_id
    await enqueue_event(bot, prompt_data, event_id=123)

    # System processes event
    # → event_id flows through context
    # → Actions executed successfully
    # → Event marked as delivered in DB
    # → No Telegram API errors

Regular Message Handling
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    # Regular message (no event_id)
    await safe_send(bot, chat_id=12345, text="Hello")

    # → No event_id in flow
    # → Clean kwargs to Telegram API
    # → Works normally

Testing
-------

The system includes comprehensive tests:

- ``test_event_id_filtering.py`` - Tests kwargs filtering
- ``test_event_id_full_flow.py`` - Tests complete flow
- ``test_action_validation.py`` - Tests action validation

All tests confirm that:

1. ✅ event_id is correctly filtered from Telegram API calls
2. ✅ Other valid parameters are preserved
3. ✅ Business logic continues to work with event_id

Related Files
-------------

- ``core/transport_layer.py`` - Main event_id handling
- ``core/telegram_utils.py`` - Telegram API filtering
- ``core/action_parser.py`` - Event delivery logic
- ``core/db.py`` - Database event marking
- ``core/auto_response.py`` - Auto-response system for interface callbacks
- ``plugins/terminal.py`` - Terminal plugin with auto-response integration
- ``cortex/selenium_engine/selenium_chatgpt.py`` - Event_id usage

.. note::
   Last updated: August 3, 2025
   
   Status: ✅ Working correctly after fix
