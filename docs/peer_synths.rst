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
only apply to messages from bot IDs listed in ``SYNTH_PEERS``. Humans talk
to any SyntH as normal.

.. important::

   **By default, Telegram bots cannot see messages from other bots** — a
   platform-level anti-loop safeguard. Before any of the peer settings below
   do anything useful, you must enable **Bot-to-Bot Communication Mode** for
   each bot in BotFather, in addition to Group Privacy Mode being disabled
   (or the bot having admin rights) in the shared group. See
   `Bot-to-Bot Communication Mode`_ below — this is a one-time Telegram
   setting, not something configured in this codebase.


How It Works
------------

When a message arrives in a Telegram group:

1. ``mention_utils`` checks whether the sender is a known peer bot
   (``SYNTH_PEERS`` + ``SYNTH_PEER_ENABLED``).
2. If it is, ``should_respond_to_peer()`` evaluates the active policy and
   either allows the message through or suppresses it (returns
   ``"peer_synth"`` so the bot stays silent). This check always runs first,
   before any alias/mention/attention-window logic — see `Interaction With
   the Attention Window`_ below.
3. The message is **always saved to context** regardless of suppression, so
   each instance stays aware of what the others said — **once Bot-to-Bot
   Communication Mode is enabled** per the note above. Without it, a peer's
   messages never arrive at all, so this never has anything to save.
4. When the message does reach the LLM, ``build_prompt_request`` appends a
   ``=== PEER SYNTHS ===`` instruction block to the prompt explaining who the
   other SyntHs are and what the current policy means. This block is injected
   **only for Telegram group messages** — private chats and all other
   interfaces are unaffected.


Bot-to-Bot Communication Mode (Required Telegram Setting)
------------------------------------------------------------

This is a **Telegram-side setting**, not something in this codebase — but
without it, nothing below this point does anything, because a peer's
messages never reach this instance in the first place.

**Why**

By default, a Telegram bot cannot see messages sent by other bots in a
group at all, regardless of its own Group Privacy Mode setting. This is
intentional (anti-loop safeguard), separate from the human-message privacy
behavior. Telegram added an opt-in mode specifically to lift this for
legitimate bot-to-bot use cases.

**How to enable it**

1. Open a chat with `@BotFather <https://t.me/botfather>`_ for **each** bot
   in the group.
2. Open that bot's settings (via BotFather's menu/MiniApp) and enable
   **Bot-to-Bot Communication Mode**.
3. Additionally, **each** bot needs either Group Privacy Mode disabled
   (``/setprivacy`` → Disable) or admin rights in the shared group — the
   communication mode alone only unlocks explicit ``/command@OtherBot``
   mentions and direct replies to that bot's messages; full message
   visibility (plain conversational text, no command/reply) still requires
   privacy-disabled-or-admin on top of it.
4. Repeat for every bot that needs to see the others' messages.

Once this is set, a peer's replies arrive as normal Telegram updates and are
saved to this instance's own ``chat_history_cache`` exactly like a human
message — no further code-level configuration is needed for that part.

.. note::

   Peer suppression and policy checks (``SYNTH_PEER_POLICY`` /
   ``should_respond_to_peer``) work regardless of this setting — they were
   already correctly gating *whether to respond*. What was missing was the
   underlying delivery: without this BotFather setting, a peer's message
   never arrives to be gated in the first place, so this instance's own
   ``chat_history_cache`` would show zero rows for that peer's sender ID no
   matter how the code is tuned.


Setup
-----

Each SyntH instance must be configured independently. The settings are per-instance
in the WebUI under **Peer Policy**.

.. note::

   Do `Bot-to-Bot Communication Mode`_ above **first**, for every bot
   involved — the settings below configure this codebase's *policy*, but
   Telegram won't deliver a peer's messages here at all until that's done.

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
   * - **Peer SyntHs**
     - One row per *other* bot in the group: its numeric bot ID and the
       display name to use for it. Do **not** add a row for your own bot.
       Use **+ Add peer SyntH** to add a row and **Remove** to drop one — no
       manual JSON editing needed.
   * - **Peer SyntH Response Policy**
     - See policy reference below. Start with ``mention_only``.
   * - **Peer Turn Floor (seconds)**
     - See turn coordination below. Set to ``0`` on the primary instance;
       set to a value above your typical LLM response time on every secondary
       instance (e.g. ``20`` for a 7–12 s LLM).
   * - **Peer Relay Timeout (seconds)**
     - See `Mention-Order Relay`_. Default ``60``; lower it if you'd rather
       give up sooner on a slow/unresponsive peer. Requires
       `Bot-to-Bot Communication Mode`_ to be enabled — otherwise the peer's
       reply never arrives and this always times out.

Example for a three-SyntH group (configuring **Aria**), shown as the
underlying JSON the repeater field stores:

.. code-block:: json

   SYNTH_PEERS       = [{"id": 8243553794, "name": "Sol"}, {"id": 1122334455, "name": "Nova"}]
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


Turn Coordination (Roleplay)
----------------------------

When multiple SyntHs use ``mention_only`` (or have peer mode enabled with open
responses), both instances see every user message simultaneously and would
normally both start generating at the same time. The **Peer Turn Floor** setting
coordinates this without requiring a shared database.

**How it works**

One instance is designated the *primary* (``SYNTH_PEER_TURN_FLOOR_SECONDS = 0``).
It responds immediately as normal. Every other instance is *secondary* (floor > 0):
when a group message comes in, the secondary waits the floor duration, then checks
whether the primary already posted a response. If it has, the secondary suppresses
its own turn silently. If it has not (primary is still generating), the secondary
responds — both reply that round, but this only happens on unusually slow LLM calls.

**Setting the floor**

Set the floor to a comfortable margin above your longest normal response time:

.. list-table::
   :header-rows: 1
   :widths: 25 20 55

   * - Typical LLM time
     - Recommended floor
     - Notes
   * - 3–6 s
     - ``10``
     - Fast endpoint; leaves 4–7 s margin.
   * - 7–12 s
     - ``20``
     - Standard; covers 12 s + delivery overhead with margin to spare.
   * - 15–25 s
     - ``35``
     - Slower local model or long context.

The rare spike (e.g. 60 s) will occasionally produce a double response — this
cannot be prevented without a shared database and is usually harmless in an RP
context.

**Each instance gets its own value**

.. code-block:: text

   soul  (primary)   → SYNTH_PEER_TURN_FLOOR_SECONDS = 0
   soul2 (secondary) → SYNTH_PEER_TURN_FLOOR_SECONDS = 20

Leave ``SYNTH_PEER_TURN_FLOOR_SECONDS`` at the default (``0``) on any instance
that should respond immediately. Only secondary instances need a non-zero value.

.. note::

   Turn coordination only activates for Telegram group or supergroup chats.
   Private chats and other interfaces are not affected.


Mention-Order Relay (Addressing Multiple SyntHs in One Message)
-----------------------------------------------------------------

When a single message addresses more than one SyntH in sequence — e.g. "SynthA,
tell me the plan. SynthB, what do you think?" — the SyntHs should reply in the
order they were addressed, and the later one should be able to react to what
the earlier one actually said. This is separate from turn coordination above
(which only prevents an *accidental* double reply to the same undirected
trigger); mention-order relay is for an *intentional*, ordered exchange.

**How it works**

Each instance checks whether the message names a configured peer (via
``SYNTH_PEERS``) *before* naming this instance's own alias/persona name. If
so, this instance waits — polling its own ``chat_history_cache`` — for that
specific peer to actually post a reply before generating its own. Because
every instance unconditionally records every message it sees in the shared
Telegram group into its own chat history (regardless of whether it responds
to that message), the earlier peer's reply is already present in this
instance's own context by the time it finally responds.

If the earlier peer never replies (offline, erroring, or policy-suppressed),
the wait fails open after ``SYNTH_PEER_RELAY_TIMEOUT_SECONDS`` (default
``60``) and this instance proceeds with its own turn anyway, rather than
staying silent forever.

Configure via **WebUI → Settings → Peer Policy → Peer Relay Timeout
(seconds)**. Set to ``0`` to disable relay waiting entirely (falls back to
plain turn coordination above).

**Example**

With Aria configured as a peer named "Aria" on Sol's instance (and vice
versa), a message like::

   Aria, what's your take? Sol, what about you?

makes Sol's instance detect that "Aria" is mentioned before Sol's own name,
so Sol waits for Aria's reply to land in its history before generating its
own — Sol's reply can then naturally reference what Aria just said.

.. note::

   Relay ordering only considers peers configured in ``SYNTH_PEERS`` and this
   instance's own current aliases/persona name — it has no effect on ordinary
   human users, and, like turn coordination, only applies to Telegram group
   or supergroup chats.


Attention Window (Staying in the Conversation)
-----------------------------------------------

Normally a SyntH only responds to a group message that mentions its alias or
``@handle``. That's tedious in an active roleplay scene where the alias would
otherwise need to be repeated on every line. **Attention Window** relaxes
this: once the alias/mention triggers a response, the chat is considered
*engaged* for a configurable number of seconds, and messages during that
window are treated as directed to the bot without needing the alias again.
Every directed reply refreshes the window, so an active conversation stays
alive; it lapses on its own once the chat goes quiet.

Configure it via **WebUI → Settings → Chat Attention → Attention Window
(seconds)** (``CHAT_ATTENTION_WINDOW_SECONDS``). Set to ``0`` to disable
(default) — the alias/mention is then required on every message, as before.

.. _Interaction With the Attention Window:

Interaction With Peer Suppression
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The attention window cannot be used to bypass peer suppression. The check
order in ``mention_utils.is_message_for_bot`` is fixed:

1. **Peer suppression** (unconditional, runs first) — if the sender is a
   known peer SyntH bot ID and the active ``SYNTH_PEER_POLICY`` doesn't
   allow a response, the message is rejected immediately regardless of any
   attention window.
2. Private chat / sleep-wake / reply / @mention / alias / persona checks.
3. **Attention window** — only reached if none of the above already decided
   the message, and only ever applies to messages whose sender is **not** a
   bot. A peer SyntH's message can never seed or extend, nor benefit from,
   this chat's attention window.

In practice: a human mentioning SyntH A keeps A "in" the conversation for a
while, but SyntH B's messages still need to satisfy B's own peer policy on
every single message, exactly as if the attention window didn't exist.


Disabling
---------

Toggle **Enable Peer SyntH Mode** off in the WebUI. The peer bot IDs are
treated as ordinary users, all suppression is lifted, and no instruction block
is injected. The ``SYNTH_PEERS`` value is preserved so you can re-enable
without reconfiguring.
