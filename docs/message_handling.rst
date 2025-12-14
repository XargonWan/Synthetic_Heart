Message handling and queue entry
================================

Guidelines for enqueueing messages and using the central message queue.

Overview
--------

The synth core uses a centralized priority queue to process messages coming
from external interfaces (Telegram, Discord, Matrix, WebUI, etc.) and from
internal plugins (e.g. G.R.I.L.L.O.). To ensure consistent behavior and
prioritization, plugins and interfaces MUST use the provided enqueue APIs
instead of writing directly to the protected queue internals.

Key Points
^^^^^^^^^^

- Use `core.message_queue.enqueue(...)` for regular messages. This function:
  - normalizes `message.from_user` fields (adds `full_name`, `first_name`, `username` where missing)
  - ensures `message.date` is present for timestamping
  - applies rate limiting and blocklist checks
  - resolves chat/interface metadata
  - pushes the item into the prioritized queue (HIGH/NORMAL)
- Use `core.message_queue.enqueue_low_priority(...)` for background tasks
  (e.g. autonomous beats from G.R.I.L.L.O). This ensures the message is
  enqueued with LOW priority so it does not block/compete with user interactions.
- Do NOT write directly into the priority queue internals (``message_queue._queue``)
  or call ``._queue.put((prio, counter, item))`` from plugin code. This bypasses
  important normalization, checks, and queue invariants.

Enforcement
^^^^^^^^^^^

The synth core enforces this policy during startup: any plugin that directly
accesses the queue internals (e.g., ``message_queue._queue`` or
``_queue._queue``) will be flagged and the initialization will fail. This is a
hard requirement to prevent crashes and ensure consistent queue semantics.

Message Normalization
---------------------

Before a plugin or interface message is passed to the prompt engine, the core
ensures the following fields are present and normalized on the inbound message
object (``message``):

- ``message.from_user``: always a mutable object with at least the fields:
  - ``id`` (int-like)
  - ``full_name`` (string) — fallback to ``first_name``, ``username``, or the id
  - ``first_name`` (string)
  - ``username`` (string) or ``None``
- ``message.date``: UTC datetime object

This normalization happens at the queue entry point and inside internal
entry functions (`enqueue`, `enqueue_low_priority`, and plugin instance
handlers). Plugins and interfaces should therefore create message objects
with minimal fields and rely on the core to normalize them prior to prompt
creation.

Usage examples
--------------

Enqueue a normal (user) message from an interface:

.. code-block:: python

    from core import message_queue

    await message_queue.enqueue(bot, message, context_memory, priority=False, interface_id='telegram')

Enqueue a low-priority background message from a plugin:

.. code-block:: python

    from core import message_queue

    await message_queue.enqueue_low_priority(bot=None, message=internal_message, context_memory={'grillo_beat': True}, interface_id='grillo')

  Migration from direct queue writes
  ---------------------------------

  If your plugin previously wrote directly to the queue internals using code like::

    from core import message_queue
    # BAD: Direct access to internals - DO NOT DO THIS
    message_queue._queue.put((LOW_PRIORITY, _counter, item))

  Convert it to use the official API instead. For background/low-priority messages, prefer::

    from core import message_queue
    await message_queue.enqueue_low_priority(bot=None, message=message, context_memory={'your_context': True}, interface_id='your_plugin')

  For normal user-triggered messages, use::

    from core import message_queue
    await message_queue.enqueue(bot, message, context_memory, priority=False, interface_id='telegram')

  If you're doing special cases (like events with custom priority), consult the queue API or contact the maintainers.

Failure cases to avoid
----------------------

- Do not call ``message_queue._queue.put(...)`` directly. This bypasses the
  core's normalization and can lead to crashes (AttributeError: 'date' missing,
  or 'from_user' missing `full_name`) and priority inversion.
- Avoid modifying ``message`` objects concurrently from multiple threads.

Validation and Tests
--------------------

Add unit tests for plugins that generate background messages (e.g. G.R.I.L.L.O.)
to confirm they use the proper enqueue APIs and that messages are normalized.
