Chat Link Resolution
====================

synth maintains a central **ChatLinkStore** that records the relationship
between chat identifiers and human‑readable names. Each entry stores:

* ``interface`` – originating interface (e.g. ``telegram``, ``discord``)
* ``interface_path`` – hierarchical path identifying the conversation (e.g., "telegram_bot/chat_id/thread_id")
* ``chat_name`` – optional parent chat title (Telegram chat, Discord server)
* ``message_thread_name`` – optional thread or channel title

Interfaces can resolve a link by supplying any combination of interface_path or names.
This allows actions to target a conversation using ``interface_path``
or by specifying ``chat_name``/``message_thread_name``.

Updating names
--------------

The core action ``update_chat_name`` refreshes the stored titles for a chat
or its thread. At least one of ``chat_name`` or ``message_thread_name`` must
be provided. Interfaces call this after fetching names from their own APIs
(e.g. Telegram's ``getChat`` and ``getForumTopic``).

Error handling
--------------

If resolving a name returns more than one match, the corrector issues a
structured system message:

.. code-block:: json

   {
       "system_message": {
           "type": "error",
           "message": "Multiple channels found with name <name>; please retry with the numeric chat_id",
           "your_reply": "<original reply>",
           "full_json_instructions": {"instructions": "...", "actions": { ... }},
           "error_retry_policy": {
             "description": "If you receive a system_message of type 'error' with the phrase 'Please repeat your previous message, not this very prompt, but your previous reply, corrected. If that was a web search please use the content to reply with your own words.' you must automatically re-send the exact same JSON you sent previously, but with the part indicated as invalid corrected.",
              "steps": [
                  "1. Identify which part of your last sent JSON caused the error (e.g. an unsupported action type or missing parameter).",
                  "2. Create a new JSON that is identical to the one you previously sent, except for correcting ONLY the invalid part.",
                  "3. Do not add, remove or reorder any other actions or payload content.",
                  "4. Re-submit the corrected JSON immediately (without waiting for user instructions)."
              ]
          }
      }
  }

The LLM should resend the original action using explicit IDs.

