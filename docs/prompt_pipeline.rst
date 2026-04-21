Prompt Pipeline
===============

The prompt rewrite described in ``REWRITE-TASK.md`` is now the live runtime
architecture.

Synthetic Heart no longer treats prompt assembly as a single pretty-printed
JSON blob that every engine must parse as text. The canonical pipeline is now:

1. ``core.plugin_instance`` calls ``core.prompt_engine.build_prompt_request()``.
2. The prompt builder assembles a typed ``PromptRequest`` plus compatibility
   data for legacy callers.
3. Engines render that typed request into their native transport format with a
   renderer from ``core.prompt_renderers``.
4. The model returns either plain text or native tool/function calls, which are
   normalized back into SyntH's ``{"actions": [...]}`` format.

Compatibility status
--------------------

- ``build_prompt_request()`` is the canonical prompt builder.
- ``build_json_prompt()`` still exists as a deprecated alias for backward
  compatibility.
- The legacy dict payload is still returned today, but it now carries the typed
  request under ``__prompt_request`` so migrated engines can use the new path
  immediately.
- Engines may also accept a ``PromptRequest`` object directly.

PromptRequest
-------------

``core.prompt_request.PromptRequest`` is the engine-agnostic intermediate
representation. It splits prompt state by stability so renderers can preserve
conversation structure and enable prompt caching where the provider supports it.

Stable fields:

- ``system_instruction``: persona, safety rules, and high-level response rules.
- ``tool_declarations``: tool manifests derived from the actions registry.

Moderately stable field:

- ``context_summary``: diary, memories, cross-chat recaps, and participant
  summaries formatted as plain text.

Dynamic fields:

- ``conversation_history``: parsed user / assistant turns for the active chat.
- ``current_text``: the current user turn.
- ``runtime_ctx``: timestamp, scope, language, tone, emotions, interface data,
  and grillo flags.
- ``attachments``: multimodal payload metadata for the current turn.
- ``reply_to``: optional reply metadata.

Modes
-----

The rewrite is not chat-only. The builder now produces different
``PromptRequest.mode`` values for different runtime surfaces.

``chat``
   Standard message processing with full conversation history and context.

``grillo``
   Internal autonomous beats. These omit normal conversation history and use a
   minimal context summary.

``delivery``
   Auto-response delivery prompts created by
   ``core.prompt_engine.build_delivery_request()``. These contain persona,
   delivery instructions, action outputs, and only ``message_*`` tools.

``live``
   Live voice prompts created by ``core.prompt_engine.build_live_prompt_request()``
   and rendered to one flat instruction string for live sessions.

Renderers
---------

``core.prompt_renderers`` contains the provider-specific renderers.

``OpenAIRenderer``
   Baseline renderer for OpenAI-compatible chat-completions APIs. Produces
   ``messages`` arrays and optional tool schemas.

``AnthropicRenderer``
   Produces Anthropic Messages payloads. The stable system block is emitted
   with ``cache_control`` when ``ENABLE_PROMPT_CACHING`` is enabled.

``GeminiRenderer``
   Produces Gemini-native ``system_instruction_text`` + ``contents`` payloads
   and Gemini function declarations.

``TextRenderer``
   Compact fallback for engines that cannot consume structured conversation
   turns. This is still smaller than the old indented JSON blob path.

``LiveRenderer``
   Flattens ``PromptRequest(mode='live')`` into the plain-text instruction used
   by live voice callers.

Where the new path is used
--------------------------

The rewrite is already active in the main engine families:

- ``engines/external_engines/openapi.py`` uses ``OpenAIRenderer``.
- ``engines/external_engines/openrouter.py`` uses ``OpenAIRenderer``.
- ``engines/external_engines/anthropic.py`` uses ``AnthropicRenderer``.
- ``engines/external_engines/gemini_api.py`` uses ``GeminiRenderer``.
- ``core/external_endpoints/bridges/cortex_bridge.py`` uses
  ``OpenAIRenderer`` for external endpoint Cortex engines.
- ``core.auto_response.AutoResponseSystem`` attaches
  ``PromptRequest(mode='delivery')`` to action-result deliveries.
- ``core.prompt_engine.build_live_system_instruction()`` now renders a
  ``PromptRequest(mode='live')`` through ``LiveRenderer``.

Multimodal handling
-------------------

Attachments are no longer documented as a giant base64 blob living inside a
 single user message by default. The typed pipeline keeps attachment metadata in
 ``PromptRequest.attachments`` and migrated engines extract native multimodal
 parts before sending the request to the provider.

This keeps text prompts smaller and avoids duplicating heavy binary payloads in
 both the text and multimodal layers.

Operational notes
-----------------

- ``instructions_verbose`` and ``instructions`` still exist in the compatibility
  dict for legacy callers, but renderer-backed engines treat
  ``PromptRequest.system_instruction`` as the canonical system prompt.
- ``system_message`` payloads are still used for correction and delivery flows.
  They coexist with ``__prompt_request`` during the transition.
- Debugging may still show compatibility prompt dicts in logs, but the runtime
  decision point for modern engines is the typed prompt object.

See also
--------

- ``core/prompt_request.py``
- ``core/prompt_renderers.py``
- ``core/prompt_engine.py``
- ``core/auto_response.py``
- ``engines/external_engines/openapi.py``
- ``engines/external_engines/anthropic.py``
- ``engines/external_engines/gemini_api.py``
