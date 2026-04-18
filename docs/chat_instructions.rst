Chat Interface Instructions
===========================

Chat interfaces still receive an explicit chat-oriented instruction block, but
the prompt rewrite changed where that instruction lives in the runtime.

Current behavior
----------------

- ``core.prompt_engine.build_prompt_request()`` is now the canonical builder.
- It still prepares compatibility keys such as ``instructions_verbose`` and
  ``instructions`` for legacy callers.
- The typed prompt path copies that chat-specific guidance into
  ``PromptRequest.system_instruction``.
- Renderer-backed engines consume the typed system instruction directly instead
  of rebuilding a system message from a monolithic JSON prompt blob.

Compatibility notes
-------------------

- ``core.prompt_engine.build_json_prompt()`` remains as a deprecated alias.
- The returned compatibility dict still carries ``instructions_verbose`` for
  engines that have not migrated yet.
- Prompt reduction keeps the stable instruction block intact before the typed
  request is attached under ``__prompt_request``.

Guidance for engine authors
---------------------------

- Prefer ``PromptRequest`` + a renderer from ``core.prompt_renderers``.
- Treat ``PromptRequest.system_instruction`` as the canonical system prompt.
- Only fall back to ``instructions_verbose`` / ``instructions`` when handling
  a legacy dict that does not carry ``__prompt_request``.

See also
--------

- ``core/prompt_engine.py``
- ``core/prompt_request.py``
- ``core/prompt_renderers.py``
- :doc:`prompt_pipeline`
