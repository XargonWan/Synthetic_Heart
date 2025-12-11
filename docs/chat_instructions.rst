Chat interface instructions
===========================

This document describes the new unminified chat instruction that is injected
into prompts when a message arrives from a chat-like interface (Telegram,
Discord, Web UI). The instruction appears under `instructions_verbose` in the
prompt JSON and is intentionally kept human-readable and not minified.

Behavior
--------

- The prompt builder (`core.prompt_engine.build_json_prompt`) will add
  `instructions_verbose` when the `interface_name` indicates a chat interface.
- This field must be preserved during any prompt reduction and will be
  delivered to LLM wrappers as a `system` role message (unminified) so the LLM
  receives explicit guidance.
- The instruction reminds the LLM to be concise for chat usage and reiterates
  the exact JSON response format the system expects.

Notes for implementers
----------------------

- LLM engines (Selenium, OpenAI, Gemini, etc.) should check for
  `instructions_verbose` in the prompt and prepend it as a system message
  before sending the request to the backend.
- `instructions_verbose` is protected in `reduce_prompt_for_llm_limit`, so it
  will not be removed when the prompt is reduced to fit model limits.

See also
--------

- core/prompt_engine.py
- core/selenium_llm_base.py
- llm_engines_dev/openai_chatgpt.py
