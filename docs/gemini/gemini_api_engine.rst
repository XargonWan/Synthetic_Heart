Gemini API Engine
=================

The ``gemini_api`` module is the primary API-based Cortex engine for Synthetic Heart. Unlike the Selenium-based adapters that drive a browser session, this engine communicates directly with the `Google Gemini REST API <https://ai.google.dev/>`_ using HTTP requests and, optionally, the ``google-genai`` SDK for live media processing.

.. contents:: Table of Contents
   :depth: 3
   :local:


Overview
--------

**File:** ``engines/external_engines/gemini_api.py``

**Base class:** ``AIPluginBase`` (from ``core/ai_plugin_base.py``)

**Module export:** ``PLUGIN_CLASS = GeminiAPIPlugin``

The engine:

- Accepts either a direct ``PromptRequest``, a compatibility dict carrying
  ``__prompt_request``, or a legacy dict/string fallback.
- Uses ``GeminiRenderer`` to turn typed prompts into Gemini-native
  ``system_instruction_text`` + ``contents`` payloads.
- Supports **multimodal input**: images, audio, video, and documents are sent
  as native Gemini parts instead of being duplicated into a giant text blob.
- Handles **correction/retry loops** when the model produces invalid JSON or fails action validation.
- Integrates with the optional **Agent plugin** via lightweight agentic hooks.
- Exposes configuration variables (API key, base URL, model selection) to the WebUI and ``config_registry``.


Architecture Diagram
--------------------

::

   ┌─────────────────────────────────────────────────────────────────────┐
   │                       INCOMING MESSAGE                              │
   │  (Telegram, Discord, WebUI, etc.)                                   │
   └───────────────┬─────────────────────────────────────────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │     plugin_instance.py            │
   │  - Extracts multimodal            │
   │    attachments (audio, images)    │
  │  - Calls build_prompt_request()   │
  │    (build_json_prompt() is alias) │
   │  - Passes prompt to Cortex engine  │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │     GeminiAPIPlugin               │
   │     handle_incoming_message()     │
   │  - Stores request metadata        │
   │  - Calls generate_response()      │
   │  - Returns raw text response      │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │     generate_response()           │
  │  1. Detect PromptRequest path     │
  │  2. Handle correction prompts     │
  │  3. Extract multimodal parts      │
  │  4. Render via GeminiRenderer     │
  │  5. Use legacy fallback only      │
  │     when typed data is absent     │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
  │ _http_generate_content_from_      │
  │ rendered()                        │
   │  - Constructs REST API URL        │
   │  - Builds payload:                │
   │    ┌─────────────────────────┐    │
   │    │ contents:               │    │
  │    │  - native parts         │    │
  │    │  - turn history         │    │
  │    │ system_instruction_text │    │
  │    │ optional tools          │    │
   │    └─────────────────────────┘    │
   │  - POST with retry (3 attempts)  │
   │  - Parse response candidates     │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │     message_chain / action_parser │
   │  - Parses JSON actions            │
   │  - Routes to interfaces           │
   │  - Triggers diary, emotions, etc. │
   └───────────────────────────────────┘


Configuration
-------------

The engine registers three configuration variables, all visible in the WebUI
Components page and persisted via ``config_registry``.

.. list-table::
   :header-rows: 1
   :widths: 25 15 30 30

   * - Variable
     - Type
     - Default
     - Notes
   * - ``GEMINI_API_KEY``
     - password
     - ``""``
     - **Required.** Sensitive. Obtain from `Google AI Studio <https://aistudio.google.com/>`_.
   * - ``GEMINI_API_BASE_URL``
     - string
     - ``https://generativelanguage.googleapis.com``
     - Advanced. Auto-versioned to ``/v1beta`` if no version suffix present.
   * - ``GEMINI_MODEL``
     - select (dropdown)
     - ``gemini-3-flash-preview``
     - Dropdown populated from ``MODEL_CONFIGS`` keys.

All three are registered with ``needs_component_reload=True``, so changing them
in the WebUI triggers a live reload of the engine.

Setting the model
~~~~~~~~~~~~~~~~~

The ``GEMINI_MODEL`` variable uses a custom getter/setter pair:

- **Getter** (``_get_gemini_model``): reads from ``core.config.get_current_model()``,
  validates against ``MODEL_CONFIGS``, falls back to ``DEFAULT_MODEL``.
- **Setter** (``_set_gemini_model``): writes to ``core.config.set_current_model()``,
  then notifies the active plugin instance via ``plugin.set_current_model()``.

This ensures model changes propagate immediately to the running engine instance
without requiring a full component reload.


Supported Models
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 15 15 10 30

   * - Model ID
     - Max Prompt
     - Max Output
     - Thinking
     - Notes
   * - ``gemini-3-flash-preview``
     - 1M chars
     - 8192 tokens
     - Yes
     - **Default.** Latest preview, full capability.
   * - ``gemini-3-pro-preview``
     - 1M chars
     - 8192 tokens
     - Yes
     - Pro version, higher quality reasoning.
   * - ``gemini-2.0-flash-thinking-exp-01-21``
     - 500K chars
     - 8192 tokens
     - Yes
     - Experimental thinking variant.
   * - ``gemini-2.0-flash``
     - 500K chars
     - 8192 tokens
     - No
     - Standard Flash without thinking.

The ``MODEL_LIMITS_MAP`` dict maps model names to max character limits and is
exposed as ``self.model_limits_map`` for ``plugin_instance.py`` to query when
deciding how to trim prompts.


Initialization
--------------

When the engine is instantiated (``GeminiAPIPlugin.__init__``):

1. **Notification function**: stores ``notify_fn`` and registers it with
   ``core.notifier.set_notifier()`` for trainer alerts. Falls back to a
   logging-only lambda if none provided.

2. **Model resolution**: reads ``GEMINI_MODEL`` → ``core.config.get_current_model()``
   → ``DEFAULT_MODEL`` (in priority order). Validates against ``MODEL_CONFIGS``.

3. **Request metadata**: initializes ``_current_request_meta = None`` (populated
   per-request for error context).

4. **Model limits**: sets ``self.model_limits_map = MODEL_LIMITS_MAP`` for
   external callers (``plugin_instance.py``).

5. **Google GenAI SDK client** (optional): if ``google-genai`` is installed and
   ``GEMINI_API_KEY`` is set, creates a ``genai.Client`` with
   ``api_version="v1alpha"``. This client is used **only** by
   ``handle_live_processing()`` and is ``None`` if the SDK is absent.


Health Check
~~~~~~~~~~~~

``get_health_status()`` returns ``(True, "")`` if ``GEMINI_API_KEY`` is
configured and non-empty, otherwise ``(False, "GEMINI_API_KEY not configured")``.
Called by the WebUI to show engine status.


Main Request Flow
-----------------

handle_incoming_message()
~~~~~~~~~~~~~~~~~~~~~~~~~

**Signature:** ``async def handle_incoming_message(self, bot, message, prompt)``

Entry point called by ``plugin_instance.py``. This method:

1. Stores request metadata (bot, message, interface, chat_id) in
   ``_current_request_meta`` for error recovery context.
2. Calls ``generate_response(prompt)``.
3. Logs a 200-char preview of the response.
4. **Returns** the raw response string — it does **not** send anything to the
   user directly. The message chain handles JSON parsing, action execution, and
   interface routing.
5. On exception: logs, notifies trainer, returns a plain-text error string.
6. Clears ``_current_request_meta`` in the ``finally`` block.

**Critical design note:** the engine is a pure function from prompt to response
text. All side effects (sending messages, writing diary entries, updating
emotions) happen downstream in the message chain.

generate_response()
~~~~~~~~~~~~~~~~~~~

**Signature:** ``async def generate_response(self, prompt)``

Orchestrates the full generation pipeline:

**Step 1 — Prompt shape detection:**

- ``PromptRequest`` object → render directly through ``GeminiRenderer``.
- ``dict`` with ``__prompt_request`` → use the attached typed request.
- legacy ``dict`` without ``__prompt_request`` → build a minimal fallback
  ``PromptRequest`` and render that.
- ``str`` → parse only to detect correction prompts; otherwise treat as legacy
  fallback text.

**Step 2 — Correction detection:**

If the prompt contains a ``system_message`` with ``type`` in
``("error", "correction", "invalid_json", "validation_error")``, the engine
delegates to ``_handle_correction_prompt()`` (see `Correction & Error Recovery`_).

**Step 3 — Multimodal extraction:**

Calls ``_extract_multimodal_parts(prompt)`` to recursively pull out attachments
as Gemini-compatible ``inline_data`` parts (see `Multimodal Support`_).

**Step 4 — Native rendering:**

Renderer-backed paths call ``GeminiRenderer.render()`` or
``GeminiRenderer.render_with_multimodal()`` to build Gemini-native request
data.

**Step 5 — Legacy fallback only when required:**

If no typed request is available, the engine redacts bulky attachment data,
builds a minimal fallback ``PromptRequest``, and then renders that instead of
sending the original dict as one indented JSON blob.

**Step 6 — HTTP call:**

Calls ``_http_generate_content_from_rendered()`` with the rendered Gemini
payload and provider-specific token limits.

**Step 7 — Return:**

Returns the response text. On exception, returns a JSON error action:

.. code-block:: json

   {
     "actions": [{
       "type": "system_message",
       "payload": {"text": "... error description ..."}
     }]
   }


System Instruction
------------------

``_build_system_instruction(prompt)`` remains as a legacy fallback helper for
older dict-based callers that do not carry ``__prompt_request``. In the normal
renderer-backed path, the system prompt comes from
``PromptRequest.system_instruction`` and is rendered by ``GeminiRenderer``.

When the fallback helper is used, it:

1. **Extracts the interface** from the prompt dict, checking (in order):

   - ``prompt["interface"]``
   - ``prompt["current_interface"]``
   - ``prompt["input"]["source"]["interface"]``
   - ``prompt["input"]["interface"]``

2. **Maps interface to message action type:**

   .. code-block:: python

      {
          "synth_webui":   "message_synth_webui",
          "telegram_bot":  "message_telegram_bot",
          "discord_bot":   "message_discord_bot",
          "ollama_serve":  "message_ollama_serve",
      }

   Falls back to ``message_{interface}`` for unknown interfaces.

3. **Builds the instruction string** emphasizing:

   - JSON-only output format (no markdown, no explanations).
   - Required structure: ``{"actions": [{"type": "...", "payload": {...}}]}``.
   - Current interface and correct message action type.
   - Reference to the action schema in the prompt.

4. **Prepends verbose instructions** if ``prompt["instructions_verbose"]`` is
   present in the compatibility dict.

The fallback system instruction is intentionally minimal because the normal
typed prompt path already carries persona, context summary, conversation turns,
and optional tools in structured fields.


HTTP Communication
------------------

_http_generate_content()
~~~~~~~~~~~~~~~~~~~~~~~~

**Signature:**

.. code-block:: python

   async def _http_generate_content(
       self,
       prompt_text: str,
       system_instruction: str,
       max_output_tokens: int,
       multimodal_parts: list[dict] | None = None,
   ) -> str

This is the low-level HTTP wrapper around the Gemini REST API.

**URL construction:**

::

   {GEMINI_API_BASE_URL}/v1beta/models/{model}:generateContent?key={api_key}

If ``GEMINI_API_BASE_URL`` already ends with ``/v1`` or ``/v1beta``, it is used
as-is. Otherwise ``/v1beta`` is appended.

**Request payload structure:**

.. code-block:: json

   {
     "contents": [{
       "role": "user",
       "parts": [
         {"inline_data": {"mime_type": "audio/ogg", "data": "...base64..."}},
         {"inline_data": {"mime_type": "image/jpeg", "data": "...base64..."}},
         {"text": "...the rendered prompt text or current user turn..."}
       ]
     }],
     "systemInstruction": {
       "role": "system",
       "parts": [{"text": "...system instruction..."}]
     },
     "generationConfig": {
       "maxOutputTokens": 8192
     }
   }

Multimodal ``inline_data`` parts are prepended before the text part. This means
the model "sees" the media first, then the structured prompt — matching how a
human would hear/see an attachment before reading accompanying text.

**Retry logic:**

- **Max attempts:** 3
- **Backoff:** exponential — 1s, 2s, 4s (capped at 8s)
- **Retryable HTTP statuses:** 429 (rate limit), 500, 503, 504 (server errors)
- **Request timeout:** 120 seconds (increased for large multimodal payloads)

The HTTP request is executed via ``loop.run_in_executor(None, _do_request)`` to
avoid blocking the asyncio event loop (``requests`` library is synchronous).

**Response parsing:**

1. Parse JSON from ``response.json()``.
2. Extract ``data["candidates"][0]["content"]["parts"]``.
3. Concatenate all ``text`` fields from the parts list.
4. Return the concatenated text, stripped.

On any failure (missing candidates, no text, parse errors), returns a JSON
``system_message`` error action.


Multimodal Support
------------------

The engine supports images, audio, video, and documents as inline data attached
to the API request. This is the **static multimodal** path — media flows through
the full prompt pipeline with complete context (persona, memories, history).

Video Audio Extraction
~~~~~~~~~~~~~~~~~~~~~~

Gemini tends to focus on the visual track when a video is sent as a single
``video/mp4`` inline_data blob, often ignoring or under-weighting the embedded
audio track. To work around this, the attachment extractor
(``core/multimodal_attachment.py``) automatically uses ``ffmpeg`` to extract the
audio track from video files and video notes, then sends it as a **separate**
``audio/ogg`` attachment alongside the video. This ensures the model attends to
both the visual and audio content.

- Extraction is best-effort: if ``ffmpeg`` is unavailable or the video has no
  audio track, only the video is sent (no error raised).
- Audio is encoded as mono 16 kHz OGG Opus at 64 kbps to minimize size overhead.
- Applies to both regular Telegram videos and round video notes.

Supported MIME Types
~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Category
     - MIME Types
   * - **Images**
     - ``image/jpeg``, ``image/png``, ``image/gif``, ``image/webp``
   * - **Audio**
     - ``audio/mpeg``, ``audio/mp3``, ``audio/wav``, ``audio/ogg``, ``audio/flac``, ``audio/aac``, ``audio/mp4``, ``audio/x-m4a``
   * - **Video**
     - ``video/mp4``, ``video/mpeg``, ``video/mov``, ``video/quicktime``, ``video/avi``, ``video/x-msvideo``, ``video/x-flv``, ``video/mpg``, ``video/webm``, ``video/wmv``, ``video/x-ms-wmv``, ``video/3gpp``
   * - **Documents**
     - ``application/pdf``, ``text/plain``, ``text/html``, ``text/css``, ``text/javascript``, ``application/javascript``, ``text/x-python``, ``text/markdown``, ``application/json``, ``application/xml``, ``text/xml``, ``text/csv``

_extract_multimodal_parts()
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Signature:** ``def _extract_multimodal_parts(self, prompt: dict | str) -> list[dict]``

Recursively searches the prompt structure for multimodal content. Looks for
these keys at **any nesting depth**:

- ``attachments`` — generic attachment list (primary, used by ``prompt_engine``)
- ``images`` — image-specific list
- ``audio`` — audio-specific list
- ``documents`` — document-specific list
- ``videos`` — video-specific list

Each attachment can be:

- A ``dict`` with ``mime_type`` and ``data`` (base64 string) — used directly.
- A ``dict`` with ``mime_type`` and ``path``/``file_path`` — file is read and
  base64-encoded.
- A ``str`` — treated as a file path, MIME type inferred from the parent key.

For each valid attachment, produces a Gemini ``inline_data`` part:

.. code-block:: python

   {
       "inline_data": {
           "mime_type": "audio/ogg",
           "data": "...base64 encoded bytes..."
       }
   }

_copy_and_redact_data()
~~~~~~~~~~~~~~~~~~~~~~~

**Signature:** ``def _copy_and_redact_data(self, prompt: dict) -> dict``

After multimodal parts are extracted as native API parts, the base64 data still
exists in the compatibility text fallback. Sending both would:

1. **Double the payload size** (binary data as both inline_data AND text).
2. **Confuse the model** (seeing raw base64 strings in the prompt).

This method deep-copies the prompt and replaces ``data`` and ``base64`` fields
in attachment-like dicts with ``<redacted: N chars>``. Only redacts dicts that
contain attachment-related keys (``mime_type``, ``mimeType``, ``path``,
``file_path``, ``data``, ``base64``).

Helper methods
~~~~~~~~~~~~~~

- ``_get_mime_type(file_path)`` — uses ``mimetypes.guess_type()`` with extension
  fallback mapping.
- ``_encode_file_to_base64(file_path)`` — reads file as binary, returns base64
  string.
- ``_is_supported_multimodal_type(mime_type)`` — checks against
  ``SUPPORTED_*_TYPES`` class sets.


Live Processing (Legacy Path)
-----------------------------

.. warning::

   As of the static multimodal decoupling, the live processing path is
   **no longer used** for normal voice/video messages in Telegram. Voice and
   video messages now flow through the standard pipeline as inline attachments
   with full context. The ``handle_live_processing()`` method is retained for
   potential future use (e.g., real-time streaming).

handle_live_processing()
~~~~~~~~~~~~~~~~~~~~~~~~

**Signature:** ``async def handle_live_processing(self, file_path: str, mime_type_hint: str = None) -> str | None``

Processes a single media file (voice note, video note) using the Google GenAI
SDK's ``generate_content`` method. Unlike the main pipeline:

- Uses the ``google-genai`` SDK (not raw HTTP).
- Sends a **hardcoded generic prompt** without persona, memories, or history.
- Returns plain text (not JSON actions).
- System instruction: ``"You are a helpful AI assistant. Respond strictly with the text of your reply. Do not output JSON."``

This was the original approach for voice messages. It was replaced because
processing audio in isolation loses all the rich context that makes the AI
persona coherent.

**Requires:** ``google-genai`` SDK installed and ``self.client`` initialized.

Audio/Video Utility Methods
~~~~~~~~~~~~~~~~~~~~~~~~~~~

These methods support the live processing path and are kept for potential reuse:

- ``_extract_frames(video_path)`` — extracts video frames at 1fps/640px using
  ``ffmpeg``, returns list of JPEG bytes.
- ``_convert_audio_to_pcm(input_path)`` — converts audio to 16kHz mono PCM
  s16le using ``ffmpeg``.
- ``_convert_pcm_to_ogg(pcm_data, output_path)`` — converts 24kHz PCM to OGG
  Opus using ``ffmpeg``.


Correction & Error Recovery
---------------------------

When the message chain detects that the model's response is invalid (malformed
JSON, missing required fields, failed action validation), it constructs a
**correction prompt** with a ``system_message`` block and sends it back through
the engine.

_handle_correction_prompt()
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Signature:** ``async def _handle_correction_prompt(self, prompt: dict) -> str``

Triggered when ``prompt["system_message"]["type"]`` is one of:

- ``error``
- ``correction``
- ``invalid_json``
- ``validation_error``

**Standard correction flow:**

1. Extracts error details: type, message, original user message, the model's
   previous (invalid) reply, and the required JSON format.

2. Resolves the target interface (checking ``target_interface``, ``interface``,
   ``action_type_hint`` in order).

3. Builds a focused correction prompt that shows:

   - The specific error.
   - The original user message.
   - The first 500 chars of the failed reply.
   - The required JSON structure.
   - The correct message action type for the interface.

4. Sends the correction via ``_http_generate_content()`` with a tight system
   instruction: ``"You are a JSON correction assistant..."``

**Grillo (internal beat) special case:**

If the interface is ``"grillo"`` (internal introspection beat), the correction
prompt omits any ``message_*`` action and instructs the model to produce only
internal actions (e.g., ``create_personal_diary_entry``). Internal beats should
never produce user-facing messages.


Agentic Hooks
-------------

The engine supports the optional Agent plugin integration with these methods:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Method
     - Behavior
   * - ``supports_agent()``
     - Always returns ``True``.
   * - ``attach_agent(plugin)``
     - Stores reference as ``self._agent_plugin``, sets ``self.agent_enabled = True``.
   * - ``detach_agent(plugin)``
     - Removes reference, sets ``self.agent_enabled = False``.
   * - ``agent_execute(action, ctx)``
     - Lazy-imports ``PLUGIN_REGISTRY``, finds the ``"agent"`` plugin, calls ``execute_action()``. Returns ``{"status": "ok"}``, ``{"status": "pending_async"}``, or ``{"status": "unsupported"}``.

These hooks are lightweight adapters. The Agent plugin does the real work; the
engine just provides a forwarding path.


Rate Limiting
-------------

``get_rate_limit()`` returns ``(60, 60, 0.5)``:

- **60 requests** per **60-second** window (1 req/sec average).
- **0.5 burst_limit** — 50% of capacity reserved for trainers.

The HTTP layer adds its own retry logic for 429 responses (see
`HTTP Communication`_).


Error Handling Patterns
-----------------------

The engine follows a consistent error handling philosophy: **never throw, always
return a parseable response**.

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Scenario
     - Behavior
     - Response Format
   * - Missing API key
     - Returns plain-text warning
     - ``"⚠️ Gemini API Key not configured..."``
   * - Invalid model name
     - Falls back to ``DEFAULT_MODEL``
     - (silent recovery)
   * - HTTP request failure
     - Retries 3x with backoff
     - JSON ``system_message`` error action
   * - HTTP 4xx/5xx
     - Retries if retryable status
     - JSON ``system_message`` error action
   * - Response has no candidates
     - Logs error
     - JSON ``system_message`` error action
   * - Response has no text
     - Logs error
     - JSON ``system_message`` error action
   * - JSON parse failure
     - Logs error
     - JSON ``system_message`` error action
   * - Correction prompt
     - Sends focused re-prompt
     - New JSON response from model
   * - Agent unavailable
     - Returns status dict
     - ``{"status": "unsupported", ...}``
   * - Multimodal file missing
     - Logs warning, skips
     - (continues without that part)

All JSON error responses use the standard action format so the message chain
can handle them uniformly.


Google GenAI SDK Dependency
---------------------------

The ``google-genai`` SDK (``from google import genai``) is an **optional**
dependency:

- **If installed:** ``_HAS_GENAI_SDK = True``, ``self.client`` is initialized,
  ``supports_voice_interaction`` reports ``True``.
- **If absent:** ``_HAS_GENAI_SDK = False``, ``self.client = None``, live
  processing is disabled, everything else works normally.

The main pipeline (``generate_response`` →
``_http_generate_content_from_rendered``) uses the
``requests`` library directly and does **not** depend on the GenAI SDK.

Install the SDK with::

   uv add google-genai


File Structure Summary
----------------------

::

  engines/external_engines/gemini_api.py (≈1500 lines)
   │
   ├── Imports & SDK availability check          (1-38)
   ├── Variable registration                     (41-94)
   │   ├── GEMINI_API_KEY
   │   ├── GEMINI_API_BASE_URL
   │   └── GEMINI_MODEL (with getter/setter)
   ├── Model configuration                       (98-194)
   │   ├── MODEL_CONFIGS dict
   │   ├── DEFAULT_MODEL
   │   └── MODEL_LIMITS_MAP
   │
   └── class GeminiAPIPlugin(AIPluginBase)
       ├── __init__()                            — Client setup, model init
       ├── get_health_status()                   — API key check
       ├── get_supported_models()                — MODEL_CONFIGS keys
       ├── get_current_model()                   — Current model accessor
       ├── set_current_model()                   — Model setter with validation
       ├── get_rate_limit()                      — (60, 60, 0.5)
       ├── get_interface_limits()                — Dynamic caps per model
       │
       ├── supports_agent()                      — Returns True
       ├── attach_agent() / detach_agent()       — Lifecycle hooks
       ├── agent_execute()                       — Forwarding adapter
       │
       ├── handle_live_processing()              — GenAI SDK media processing
       ├── _extract_frames()                     — Video → JPEG frames (ffmpeg)
       ├── _convert_audio_to_pcm()               — Audio → 16kHz PCM (ffmpeg)
       ├── _convert_pcm_to_ogg()                 — PCM → OGG Opus (ffmpeg)
       │
       ├── handle_incoming_message()             — Main entry point
       ├── generate_response()                   — Pipeline orchestrator
       ├── _build_system_instruction()           — System prompt builder
       ├── _http_generate_content()              — REST API caller with retry
       │
       ├── SUPPORTED_*_TYPES                     — MIME type sets
       ├── _get_mime_type()                      — Extension → MIME
       ├── _encode_file_to_base64()              — File → base64
       ├── _is_supported_multimodal_type()       — MIME validation
       ├── _extract_multimodal_parts()           — Prompt → inline_data parts
       ├── _copy_and_redact_data()               — Remove duplicate base64
       │
       └── _handle_correction_prompt()           — JSON error recovery
