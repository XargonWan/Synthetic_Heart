LLM Engines
===========

An LLM *engine* is what the Cortex calls to turn a prompt into a response. This
page describes how engines are found, registered and written **today**. It
replaces an older version that described a ``llm_engines/`` directory and a
``ZenLLMBase`` class; neither exists any more (``AGENTS.md`` lists ``llm_engines/``
as an obsolete path).

.. note::

   Engines are one of the first things the planned extension platform moves out
   of the Core: no native engines will remain in the main repository. See
   :doc:`plugin_platform/index`.

Two ways to reach a model
-------------------------

#. **Engine modules** in ``engines/external_engines/``: Python modules written
   for a specific provider SDK or API (``anthropic``, ``gemini_api``,
   ``openapi``, ``openrouter``, ``xai_grok``). Development-only engines
   (``manual``, ``openai_chatgpt``, ``gemini_cli``) live in
   ``engines/external_engines/dev/`` and load only when development components
   are enabled.
#. **External endpoints**: database rows describing any HTTP endpoint, created
   from the WebUI wizard with the JSON presets in ``providers/``. They are
   wrapped by bridges in ``core/external_endpoints/bridges`` and registered with
   the Cortex like an engine. See :doc:`external_endpoints`.

Zen
~~~

Zen is **not** an engine module. The Zen LLM Engine is a separate container
(``synth-zen-llm-engine`` in ``docker-compose.yml``) that exposes a browser-driven
chat UI as an OpenAI-style API. SyntH reaches it as an ordinary external endpoint
(protocol ``openai``) that the ``official/zen`` extension registers
(``endpoints.ensure``, see :doc:`plugin_platform/extension_runtime`): on a fresh install
it creates ``zen-llm-engine`` with the settings Zen needs (a 300 s timeout and a
``downstream_char_budget`` of 24000, because Zen splits larger prompts), never behind an
operator who configured other endpoints, and only filling in what an existing endpoint
lacks. The Core seeds, names and tunes nothing for Zen, and the SyntH repository contains
no driver code for it. An older page,
``zen_driver_recovery``, described browser-driver recovery inside a
``zen_llm_base`` module that no longer exists; that logic lives in the Zen
container.

Discovery and registration
--------------------------

#. ``core/cortex_registry.py::register_default_engines`` scans ``engines/``. For
   each kind directory it imports every ``*_base.py`` module and calls its
   ``discover_and_register()``.
#. ``engines/external_engines/external_engines_base.py::discover_and_register``
   registers the cortex kind ``llm_provider`` and iterates the modules of the
   directory with ``pkgutil``. It skips packages, names starting with ``_`` and
   names ending with ``_base``. A module that fails to import is logged as a
   warning and skipped.
#. Each module declares what it provides with module-level names:

   .. code-block:: python

      PLUGIN_CLASS = MyLLMPlugin          # LLM engine (cortex registration)
      ENGINE_CLASS = MySTTAdapter         # STT adapter (auris registration)
      VOX_ENGINE_CLASS = MyTTSAdapter     # TTS adapter (vox registration)

      ENGINE_CAPABILITIES = {"llm": True, "stt": False, "tts": False}
      ENGINE_LABEL = "Short human-readable description"

   ``ENGINE_CAPABILITIES`` defaults to ``{"llm": True}``. The other registries
   also accept ``AURIS_CAPABILITIES``, ``AURIS_ENGINE_LABEL``,
   ``VOX_CAPABILITIES`` and ``VOX_ENGINE_LABEL``. The kind string
   ``llm_provider`` is kept for compatibility with existing database rows such as
   ``BASE_CORTEX``.
#. ``CortexRegistry.load_engine`` also has a fallback import of
   ``engines.external_engines.<name>`` and ``engines.live.<name>``.

Labels for the WebUI
~~~~~~~~~~~~~~~~~~~~

The Components page shows a short label per engine, taken in this order from
``ENGINE_LABEL`` in the module, ``engine_label`` on ``PLUGIN_CLASS``, or the
``label=`` argument of ``CortexRegistry.register_engine_module``.

Writing an engine module
------------------------

All shipped engines subclass ``AIPluginBase`` (``core/ai_plugin_base.py``) and
export ``PLUGIN_CLASS``. Use an existing module as the reference, for example
``engines/external_engines/anthropic.py`` (smallest) or ``openrouter.py``, and the
docstring of ``external_engines_base.py`` for the module contract. The method
signatures belong to ``AIPluginBase``; read them there rather than copying an
example, because they are the source of truth.

Engines take part in the Agentic Runtime without special hooks. The runtime
drives every engine through the standard ``AIPluginBase`` interface; tool
selection and execution are handled centrally by the unified tool registry and
the agent tool executor (see :doc:`agentic_tools`), not by per-engine methods.
Web-driven endpoints such as Zen speak the standard OpenAI tool-calling API, and
an endpoint can opt into a lean agent prompt so the chat UI is not told it is in
an "agentic mode"; see "Lean prompt" in :doc:`agentic_tools`.

Choosing the active engine
--------------------------

The active Cortex engine is stored in the ``BASE_CORTEX`` configuration key and
can be changed from the Components page of the WebUI. The registry helper
``get_cortex_registry().get_available_engines(cortex)`` lists what is registered.
Engines and endpoints may declare capability flags (for example vision, audio,
actions) used to pick the best engine for a task; see :doc:`cortex`.
