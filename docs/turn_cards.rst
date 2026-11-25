Turn cards
==========

Every item the message queue hands to the consumer is a **turn**: a chat message, an action,
a Grillo beat, a Vessel reflection. One **expandable card** follows each turn from the queue
to the history. It replaces the Reason Trail (Logs) and the Grillo tab (Activity), which showed
slices of the same story.

Where to see them
-----------------

* **Activity > Queue** shows what is waiting, and above it the turns **running now** as cards
  that fill in step by step (they refresh every few seconds).
* **Activity > Turns** keeps the finished ones, newest first, each with a **coloured label**:
  *processed* (green) or *failed* (red; it did not get through, definitively). Filter by
  status or search the text.

Collapsed, a card is one line like a queue entry: status, kind, where it came from, the
message, and the numbers that matter (how long it ran, how long it waited, corrector turns).
Click to expand.

What a card holds
-----------------

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Section
     - Content
   * - Summary
     - When it was queued, started and finished; the wait in the queue; the processing time
       (or how long it has been running); priority; merged items; LLM answers received;
       corrector turns and whether the corrector ended with a correct answer.
   * - Trigger
     - What started the turn: the message, action or beat (text, chat, thread).
   * - Recon
     - Time taken to gather the recon contributions.
   * - Body (LLM)
     - The engine and model that answered, how long it took, the size of the answer.
   * - Corrector
     - How many correction rounds ran, their total time and whether one succeeded.
   * - Actions
     - The kinds of action that ran and which failed (never each error).
   * - Debrief
     - Time taken, and how many corrections the debrief itself needed.
   * - Recalled memories / Emotions
     - What drove the reply: the memories and diary entries recalled, the dominant emotion,
       the goal, the beat type and the history scope.
   * - Reply
     - **The correct answer, or, if none was correct, the last one that arrived** (marked as
       such). Wrong answers are counted, never kept.
   * - Error
     - Only for a turn that failed for good: a short reason.

Each section shows its own time. JSON is formatted and coloured.

How it works
------------

``core/turn_cards.py`` keeps a record per turn. The queue's three in-flight helpers
(``_note_item_in_flight``, ``_note_turn_failed``, ``_clear_item_in_flight``) start, mark and end
it; every stage finds its card through a ``ContextVar``, so nothing is passed around: recon,
debrief and the corrector are timed by the ``@turn_cards.timed`` decorator, the engine call by
``plugin_instance``, each LLM answer by ``message_chain`` (``attempt``), the actions by
``action_parser`` and the reason summary by ``turn_cards.note_reason``. **Recording is
fail-open**: an error in it never touches the turn, and a stage that has no card does nothing.

A finished card is stored in the ``turn_cards`` table (the last 2000; memory when the database
is unavailable). A detached agent turn shares the card of the turn that started it, but the
card is closed when the queue item is.

Not kept from before: the Reason Trail log and its ``/api/reason-trail`` endpoints, and the
Grillo tab with ``/api/history/grillo``. Their old tables (``turn_reason_trail``,
``grillo_activity_log``) are left as they are: nothing writes the first any more and the Dreams
tab still reads the second.
