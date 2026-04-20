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
