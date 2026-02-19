Cortex
======

The **Cortex** is the canonical runtime-engine abstraction in Synthetic Heart. A
cortex represents a reasoning or execution backend and has a `kind` (for
example: ``llm``, ``live``, ``agent``). Cortex engines are discovered and
registered at startup and can be switched at runtime.

Quick summary (for users)
-------------------------

- What is a Cortex? — A pluggable runtime engine (text model, live/streaming
  model, or agentic runtime) that produces responses and/or executes actions.
- How to switch? — Use the Web UI **Components** page or the ``/cortex`` command.
- Common kinds: ``llm`` (chat-style models), ``live`` (low-latency audio/video
  sessions), ``agent`` (action-capable runtimes), and legacy Selenium-based
  drivers.

User-facing notes
-----------------

- Cortex engines are first-class: when you read "engine" in the docs, think
  "Cortex".

- Use the Components page in the Web UI to enable/disable, login, or configure
  a Cortex engine.

Switching and configuration
---------------------------

- Switch at runtime with the command: ``/cortex <engine_name>`` (``/llm`` is a
  deprecated alias).
- Each Cortex exposes its configuration fields (API keys, model selectors,
  limits) in the Components view so administrators can update settings
  without editing code.

Developer notes (summary)
-------------------------

- Implement a Cortex by subclassing the appropriate base (``AIPluginBase`` for
  most engines; Selenium helpers or Cortex-specific base classes exist for
  specializations).
- A Cortex must register itself with the Cortex/engine registry and export a
  ``PLUGIN_CLASS`` symbol so it can be auto-discovered by the core.
- Cortex kinds:
  - ``llm`` — chat-style model providers (API or browser-driven).  
  - ``live`` — streaming, low-latency voice/video models (Gemini Live,
    etc.).  
  - ``agent`` — engines that expose tools and can execute actions.  
  - ``selenium`` / legacy — browser-driven adapters (kept for compatibility).

See the developer section of this documentation for complete, step-by-step
instructions on implementing and testing new Cortex engines.

Legacy and migration
--------------------



Related pages
-------------

- :doc:`/quickstart` — quick start and Components overview
- :doc:`/gemini/synth-live-voice-integration` — example Live integration
- :doc:`/component_development_guide` — developer guide for components

