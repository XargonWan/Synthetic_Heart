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

Image handling: Iris vs. inline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default, incoming images and video are handled by the **Iris** vision
subsystem (``ACTIVE_IRIS_ENGINE``): the media is sent to a vision engine for a
textual description that is injected into the prompt as an ``[Iris vision: ...]``
block, after which the raw image/video bytes are stripped from
``PromptRequest.attachments`` so the Cortex engine never receives them.

The Iris engine dropdown in the WebUI also exposes a hardcoded ``inline``
pseudo-engine. When selected, the description step is skipped and the original
image/video bytes are forwarded untouched in ``PromptRequest.attachments``, so a
vision-capable Cortex model can see the media directly. This is the path to use
for testing native multimodal models (including local ones served over the
OpenAI-compatible adapter).

Inline mode only takes effect when the active Cortex endpoint is marked
vision-capable (the ``vision`` capability / subsystem flag, or a configured
``default_model``). Otherwise the cortex bridge drops the image parts and logs a
warning — see ``_supports_vision_for_mm_parts`` in
``core/external_endpoints/bridges/cortex_bridge.py``.

Supported image MIME types are defined in
``core/multimodal_attachment.py::SUPPORTED_IMAGE_TYPES``: ``image/jpeg``,
``image/png``, ``image/gif``, ``image/webp``, ``image/heic`` and ``image/heif``.
Adapter support varies: Anthropic accepts only ``jpeg``/``png``/``gif``/``webp``
(its real API limit, enforced in ``anthropic_adapter.describe_image``), while
Gemini and OpenAI-compatible endpoints additionally accept HEIC/HEIF.

Audio handling: Auris vs. inline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Incoming audio (voice notes, audio files) follows the same pattern through the
**Auris** speech-to-text subsystem (``ACTIVE_AURIS_ENGINE``). By default the
interface transcribes the audio to text (Auris, with a Live engine fallback via
``core/media_dispatcher.py``) and enqueues that text; the raw audio is not sent
to the Cortex engine.

The Auris engine dropdown also exposes a hardcoded ``inline`` pseudo-engine.
When selected, transcription is skipped at every stage (``transcribe_audio``, the
``dispatch_media`` Auris path, and the Live fallback all short-circuit), and
``handle_incoming_message`` always extracts the raw audio attachment — even when
a caption supplies text — so the audio is forwarded inline in
``PromptRequest.attachments`` for an audio-capable model to hear directly.

Note that the OpenAI-compatible wire format only expresses ``audio/wav`` and
``audio/mpeg`` inline (``input_audio``); other formats such as the OGG used by
Telegram voice notes are downgraded to a document placeholder for those
endpoints. Gemini endpoints accept any format via ``inline_data``.

Document handling: PDF and text
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Documents (PDF and textual files) are **not** handled by Iris — they follow a
separate extraction path in ``core/prompt_engine.py`` that runs while building
``PromptRequest.attachments`` (``_build_pr_attachments``). The extracted text is
stored in ``attachment.media_metadata['extracted_text']`` and injected into the
prompt by ``core/prompt_renderers.py::_build_multimodal_turn_text``.

For PDFs the extraction (``_extract_attachment_text_preview``) combines two
sources:

1. **Static page text** via ``pypdf``'s ``page.extract_text()``.
2. **AcroForm field values** via ``_extract_pdf_form_fields``. Fillable PDFs
   (character sheets, application forms, etc.) store user-entered data in
   interactive form fields, which ``extract_text()`` never reads. These are
   rendered as ``label: value`` pairs under a ``=== Form fields ===`` heading and
   appended to the page text (checkbox/radio ``/Off`` states are skipped). The
   combined text is capped at ``_ATTACHMENT_TEXT_CHAR_LIMIT`` (12000 chars).

When a PDF yields **no extractable text at all**, the pipeline falls back to
images (``_extract_pdf_page_images``):

- First it tries to pull the largest **embedded raster image** per page (typical
  of scanned documents that store one full-page image per page).
- If there are no embedded images either (vector/text-only scans), it
  **rasterizes** the pages to PNG via ``pypdfium2`` (``_rasterize_pdf_pages``).
  ``pypdfium2`` is used deliberately for its permissive Apache-2.0/BSD license
  (PyMuPDF's AGPL is incompatible with this project) and because it bundles the
  pdfium wheels — no system binary is required. The import is guarded, so a
  missing dependency degrades gracefully instead of breaking ingest.

Up to ``_PDF_PAGE_IMAGE_LIMIT`` (4) page images are produced and stored in
``media_metadata['page_images']``; a vision-capable Cortex model then reads them
like any other image.

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
- ``core/multimodal_attachment.py``
- ``core/auto_response.py``
- ``engines/external_engines/openapi.py``
- ``engines/external_engines/anthropic.py``
- ``engines/external_engines/gemini_api.py``
