Peer SyntH Groups
=================

Multiple SyntH instances can share the same Telegram group without triggering
infinite response loops. This page explains how to configure it and what each
setting does.

.. contents:: On this page
   :local:
   :depth: 2


Overview
--------

When two or more SyntH bots are in the same Telegram group, each bot's message
can trigger alias matching in the others, causing a response chain that never
stops. Peer mode breaks that loop while still letting the SyntHs genuinely
participate in the conversation.

Each instance needs to be told:

* **Which other bots are SyntHs** — so it knows whose messages to gate.
* **What to call them** — so the prompt instruction block uses their real name.
* **How to respond to them** — the active policy.

Regular human users are **never affected** by peer settings. The policy gates
only apply to messages from bot IDs listed in ``SYNTH_PEER_IDS``. Humans talk
to any SyntH as normal.


How It Works
------------

When a message arrives in a Telegram group:

1. ``mention_utils`` checks whether the sender is a known peer bot
   (``SYNTH_PEER_IDS`` + ``SYNTH_PEER_ENABLED``).
2. If it is, ``should_respond_to_peer()`` evaluates the active policy and
   either allows the message through or suppresses it (returns
   ``"peer_synth"`` so the bot stays silent).
3. The message is **always saved to context** regardless of suppression, so
   each instance stays aware of what the others said.
4. When the message does reach the LLM, ``build_prompt_request`` appends a
   ``=== PEER SYNTHS ===`` instruction block to the prompt explaining who the
   other SyntHs are and what the current policy means. This block is injected
   **only for Telegram group messages** — private chats and all other
   interfaces are unaffected.


Setup
-----

Each SyntH instance must be configured independently. The settings are per-instance
in the WebUI under **Peer Policy**.

Step 1 — Find every peer's Telegram bot user ID
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

You need the numeric Telegram user ID of each peer bot, **not** the ``@username``.
The easiest way is to forward a message from the bot to ``@userinfobot`` on
Telegram, or check the ``synth.log`` while the bot is running (it logs its own
``bot_id`` at startup).

Step 2 — Configure each instance
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

For every SyntH in the group, open **WebUI → Settings → Peer Policy** and fill in:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Setting
     - Value
   * - **Enable Peer SyntH Mode**
     - Toggle on.
   * - **Peer SyntH Bot IDs**
     - JSON array of the *other* bots' IDs. Do **not** include your own. Example: ``[8243553794, 1122334455]``
   * - **Peer SyntH Names**
     - JSON object mapping each ID to their SyntH display name. Example: ``{"8243553794": "Aria", "1122334455": "Sol"}``
   * - **Peer SyntH Response Policy**
     - See policy reference below. Start with ``mention_only``.

Example for a three-SyntH group (configuring **Aria**):

.. code-block:: json

   SYNTH_PEER_IDS    = [8243553794, 1122334455]
   SYNTH_PEER_NAMES  = {"8243553794": "Sol", "1122334455": "Nova"}
   SYNTH_PEER_POLICY = mention_only

Repeat for every instance with the other bots' IDs.

Step 3 — Add the group
^^^^^^^^^^^^^^^^^^^^^^^

Add all three bots to the same Telegram group. They will now co-exist without
looping.


Policy Reference
----------------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Policy
     - Behaviour
   * - ``silent``
     - Never responds to peer messages. Peer messages are still saved to
       context so this instance remains aware of what was said, but the LLM
       is never invoked. **Default.**
   * - ``observe``
     - Explicit alias for ``silent``. Identical behaviour.
   * - ``mention_only``
     - Responds only when a peer message contains this bot's ``@username``
       or any configured alias. Additionally, if the peer message is itself
       a reply to this bot's message, it is suppressed regardless of
       mentions — this is the reply-chain break that prevents infinite loops.

.. note::

   The ``=== PEER SYNTHS ===`` instruction block is included in the prompt
   for ``silent`` / ``observe`` as well as ``mention_only``. The block tells
   the LLM what the policy means so it does not feel confused by choosing not
   to respond.


What Regular Users See
----------------------

Nothing changes for human participants. The peer policy is checked before any
alias logic, and only fires for messages from bot IDs in ``SYNTH_PEER_IDS``.
A human user messaging the group is handled exactly as it would be without
peer mode: alias matching, ``@mention`` detection, and private-chat rules all
apply normally.


Prompt Instruction Block
------------------------

When peer mode is enabled and the message is from a Telegram group, every LLM
call receives an additional instruction block. For a group with two peers named
Aria and Sol using ``mention_only`` it looks like this:

.. code-block:: text

   === PEER SYNTHS ===
   You are sharing this space with other SyntH instances. They are autonomous
   digital beings like you — not users, not bots, not assistants.

   Known peers:
   - Aria
   - Sol

   Policy: mention_only — respond to a peer only when they address you by name
   or @handle directly. Never reply to a peer message that is itself a reply to
   your own message (this prevents infinite loops).

The names come from ``SYNTH_PEER_NAMES``. If a peer ID has no name entry it
falls back to ``SyntH#<id>``.


Disabling
---------

Toggle **Enable Peer SyntH Mode** off in the WebUI. The peer bot IDs are
treated as ordinary users, all suppression is lifted, and no instruction block
is injected. The ``SYNTH_PEER_IDS`` and ``SYNTH_PEER_NAMES`` values are
preserved so you can re-enable without reconfiguring.
