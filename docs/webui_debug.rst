WebUI Debug Mode (WEB_DEBUG)
===========================

Synthetic Heart ships a **WebUI-only** debug overlay intended for local development.
It is intentionally disabled by default.

Enable
------

Set the environment variable before starting the dev container/service:

.. code-block:: bash

   export WEB_DEBUG=1

When enabled, the WebUI renders an **Advanced Debug** floating window.

What It Does
------------

The debug window is designed to help validate VRM animation and facial state.

- **Pause (local / total)**

  - Freezes remote-driven animation updates *locally* (WebSocket animation/state updates are ignored).
  - Preload messages are still accepted.
  - The 3D animation clock is frozen (mixer/VRM updates are stopped).
  - **Facial overrides still apply while paused** (they are flushed directly to the model).

- **Resync**

  - Re-applies the last known animation state from ``/api/animation_state`` (only when not paused).

- **Loop Override (frames)**

  - Lets you create a temporary loop from an animation file using **frame indices**.
  - Frame semantics are **descriptor-style**: ``start`` and ``end`` are **inclusive** indices.
  - When you press **Start**, the UI forces ``end`` to the animation **max frame** (it always recomputes it).
  - The override is **local to the current browser session** and does not change global state.
  - **Clear** removes the local override and immediately resyncs with the global animation state.
  - The UI auto-fills ``start=0`` and ``end=maxFrameIndex`` when it can infer clip duration/descriptor.


Custom Emotion Face Presets
---------------------------

You can override/extend the client fallback presets at runtime from the browser console
by defining ``window.__synth_emotion_face_presets``.

Example:

.. code-block:: javascript

   window.__synth_emotion_face_presets = {
     happy: { ee: 0.30, aa: 0.10 },
     sad:   { oh: 0.18, uu: 0.08 },
     calm:  { ih: 0.05 }
   };

Notes:

- Keys like ``uu`` are treated as an alias for VRM ``ou``.
- Shorthands ``ai`` and ``oe`` are expanded (``ai`` → ``aa``+``ih``, ``oe`` → ``oh``+``ee``).

Scope
-----

This feature is intended for development only.
Do not enable it on stable deployments.

Debug Prompt Builder (``/api/debug/build_prompt``)
--------------------------------------------------

When ``WEB_DEBUG=1`` is set, the WebUI exposes a debug endpoint that builds a
**real** prompt from the live system state for a *faked* incoming message, and
returns it as JSON **without ever sending it to the LLM**. It is the fastest way
to verify that the prompt is assembled correctly (for example the
``current_chat`` anchor, the ``interface_path`` routing metadata, and the
unified-history labelling) without spending a single token.

The incoming message is simulated. By default it mimics an OpenAI-compatible API
endpoint delivering the text ``"This is a test message"``. The text and several
other fields can be overridden in the request body.

Request
~~~~~~~

.. code-block:: bash

   curl -k -X POST https://localhost:8000/api/debug/build_prompt \
     -H "Content-Type: application/json" \
     -d '{"text": "Ciao, questo è un test", "interface_path": "telegram_bot/-100123456", "history_scope": "unified"}'

All body fields are optional:

.. list-table::
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``text``
     - str
     - ``"This is a test message"``
     - The faked message body.
   * - ``interface_name``
     - str
     - ``"openai_compat"``
     - Which interface "delivered" the message.
   * - ``interface_path``
     - str
     - ``"openai_compat/test"``
     - The chat the message "arrived in" (the routing anchor).
   * - ``chat_id``
     - str/int
     - ``"test"``
     - The chat id.
   * - ``user_id``
     - str/int
     - ``0``
     - The sender id.
   * - ``username``
     - str
     - ``"DebugUser"``
     - Sender display name.
   * - ``usertag``
     - str
     - ``"@debuguser"``
     - Sender @tag.
   * - ``history_scope``
     - str
     - (global default)
     - ``"local"`` | ``"recent"`` | ``"unified"``.
   * - ``thread_id``
     - str
     - (none)
     - Optional thread id.

Response
~~~~~~~~

The endpoint returns a JSON object with two keys:

.. code-block:: json

   {
     "success": true,
     "simulated_message": { "text": "...", "interface_path": "...", ... },
     "prompt": { "system_instruction": "...", "input": { ... }, "context_summary": "..." }
   }

``prompt`` is the full ``PromptRequest`` payload produced by
``core.prompt_engine.build_prompt_request`` against the running system — persona,
history, recon context, action catalog and all. Inspect ``prompt.input.payload.current_chat``
to confirm the reply-routing anchor points at the chat you expect, and
``prompt.input.payload.source.interface_path`` for the sender's chat.

Notes
~~~~~

- The endpoint is gated by ``WEB_DEBUG=1`` (returns HTTP 403 otherwise).
- The faked message is **never** enqueued and **never** reaches the LLM.
- It is a development-only tool; do not enable it on stable deployments.
