Changelog
=========

2026-04-18 - Prompt Pipeline Rewrite Completed
----------------------------------------------

- Change: prompt assembly now centers on ``core.prompt_engine.build_prompt_request()`` and the typed ``PromptRequest`` model instead of treating every prompt as a single indented JSON blob.
- Add: ``core/prompt_request.py`` and ``core/prompt_renderers.py`` as the canonical intermediate representation and renderer layer.
- Add: dedicated prompt modes for chat, grillo, delivery, and live flows.
- Change: migrated engine paths now render prompts natively:

	- ``engines/external_engines/openapi.py`` via ``OpenAIRenderer``
	- ``engines/external_engines/openrouter.py`` via ``OpenAIRenderer``
	- ``engines/external_engines/anthropic.py`` via ``AnthropicRenderer``
	- ``engines/external_engines/gemini_api.py`` via ``GeminiRenderer``
	- ``core/external_endpoints/bridges/cortex_bridge.py`` via ``OpenAIRenderer``

- Change: auto-response delivery now attaches ``PromptRequest(mode='delivery')`` built by ``build_delivery_request()``.
- Change: live voice assembly now goes through ``build_live_prompt_request()`` and ``LiveRenderer``.
- Change: ``build_json_prompt()`` remains only as a deprecated compatibility alias.
- Docs: added prompt-pipeline documentation, updated prompt-related engine docs, trimmed ``.env.example`` to common operator-facing settings, and kept the advanced env catalog in ``docs/compose_env_vars.rst``.

2026-02-17 - Improvements
-------------------------

- Add: conservative, centralized runtime DB recovery that auto-creates missing tables/columns and retries failing statements on schema errors. Controlled via `DB_AUTO_HEAL` (default `1`). (see `core/db.py`, `tests/test_db_auto_heal.py`)
- Add: unit tests for DB auto-heal behavior and documentation entry in `docs/compose_env_vars.rst` and `.env.example`.
- Fix/Hardening: make X11 wait non-blocking at container startup and optionally create a placeholder socket to allow headless startup. Controlled by `SYNTH_X11_WAIT_SECONDS` and `SYNTH_CREATE_X11_PLACEHOLDER`. (see `webtop/s6-services/synth/run`)

2025-12-15 - Bugfixes
---------------------

- Fix: WebUI chat resizing regression. Made the client-side `CHAT_RESIZABLE` variable mutable and exposed the `createChatResizeHandles()` function so runtime toggling and edge/corner resizing works as expected. (res/synth_webui/js/vrm-viewer.mjs)
- Fix: Discord interface routing bug where `interface_path` parsing could pass a list as channel id, causing send failures. Correctly unpack the parsed levels to extract the channel/thread id. (interface/discord_interface.py)
