External Endpoints
==================

Synthetic Heart supports connecting to external AI services through the
`External Endpoints` section in the Web UI. External endpoints can be used as
first-class providers for the built-in `cortex`, `vox`, `auris`, and `live`
subsystems.

Overview
--------

The External Endpoints workflow is designed to be simple and guided for users
who do not already know provider URLs or configuration details.

- Known providers are exposed as built-in presets in the Add Endpoint wizard.
- Presets pre-fill the protocol, base URL, and subsystem defaults.
- For commercial providers, only the API key is typically required.
- Custom endpoints are still supported and remain fully editable.

Supported endpoint types include:

- Gemini
- Anthropic
- OpenRouter
- GitHub Models / Copilot
- OpenAI-compatible local hosts (e.g. Ollama, LM Studio)
- Selenium LLM Engine
- Generic OpenAI-compatible services
- Legacy HTTP TTS services via ``custom`` mode

Note
----

Each provider preset is implemented as a separate JSON file under the
project-level ``providers/`` directory. This keeps the core engine agnostic and
allows users to remove unused provider presets without breaking the rest of the
project.

Built-in default endpoint
-------------------------

Selenium LLM Engine is distributed as a default endpoint for new installs.
It is available automatically, but users may remove it if they do not need it.
If removed, it can be restored from the provider presets list.

Getting started
---------------

1. Open the Web UI and navigate to **Settings > External Endpoints**.
2. Click **+ Add Endpoint**.
3. Choose a provider preset or select **Custom Endpoint**.
4. Complete the form:
   - ``Name``: internal identifier (unique engine name)
   - ``Display Label``: human-readable label
   - ``Base URL``: pre-filled for known providers or entered manually
   - ``API Key``: required only when the provider needs one
   - ``Capabilities``: select which subsystems the endpoint should support
5. Save the endpoint.

After saving, Synthetic Heart performs a probe to detect capabilities, available
models, and register the endpoint with the chosen subsystems.

Provider presets
----------------

Known providers appear as a preset grid when adding a new endpoint. A preset
usually does the following:

- pre-fills the protocol and base URL
- sets sensible default capability mappings
- reduces the configuration burden to just an API key and a display name
- keeps the base URL locked by default for known providers, while still
  allowing manual override if needed

The Custom Endpoint preset is also available for any service that is not
already covered by the built-in presets.

TTS-only providers (e.g. **Fish Audio**) are listed in a dedicated *TTS
Endpoints* section below the main provider grid. Their presets can define
provider-specific form fields (``extra_fields`` in the preset JSON) that are
rendered in the wizard and persisted into the endpoint's ``extra_config`` —
for Fish Audio: the model tier (``s2.1-pro-free`` by default), the audio
format (``wav`` recommended), and the ``reference_id`` of the cloned/library
voice. The API key is sent as an ``Authorization: Bearer`` header
automatically; requests use the Fish ``{text, reference_id, format}`` payload
schema with the model tier passed as a ``model`` HTTP header.

Probe status
------------

The endpoint card displays probe status:

- ``success``: reachable endpoint, models enumerated
- ``failed``: endpoint unreachable or unsupported response
- ``pending``: probe is in progress
- ``never``: probe has not been run yet

Model list and default model
---------------------------

- After a successful probe, available models appear in the endpoint card.
- If no default model is configured, the first discovered model is set
  automatically.
- You can change the default model at any time from the card.
- If the model list is empty, verify that the endpoint supports ``/v1/models``
and returns a valid OpenAI-style response.

Subsystem mapping
-----------------

Each endpoint may be mapped to one or more subsystems:

- ``cortex``: chat and reasoning
- ``vox``: text-to-speech
- ``auris``: speech-to-text
- ``live``: real-time or multimodal behavior

The endpoint card shows the current effective mapping. To change mapping,
open the endpoint with the **Edit** button and update the capability checkboxes.

Media subsystem models (Vox / Auris / Iris)
-------------------------------------------

Cortex resolves its model from the endpoint's ``default_model`` / model list, but
the media subsystems (Vox, Auris, Iris) do **not**: their model — and, for TTS,
the voice and language — must be supplied explicitly in the endpoint's
``Extra Config`` JSON. This is required whenever a single endpoint serves both
cortex and a media subsystem, because ``default_model`` is usually a chat model
(or, for multi-modal providers such as Harmony, a non-media default like
``voicefixer``) that the audio/vision route cannot use.

Recognised per-subsystem keys:

- ``stt_model`` (Auris): the speech-to-text model, e.g.
  ``faster-whisper-large-v3-turbo``.
- ``tts_model`` (Vox): the text-to-speech model, e.g. ``kitten-tts-nano``.
- ``tts_voice`` (Vox): the voice name (provider-specific, e.g. ``Luna``).
- ``tts_language`` (Vox): the language code; single-speaker models such as
  KittenTTS require ``default``.
- ``iris_model`` (Iris): the image/video description (vision) model, e.g.
  ``gemma4-meromero-26b-a4b``. Must be a vision-capable model — a non-vision
  default (such as the audio-conversion ``voicefixer``) yields no description
  (observed as Iris failing to see images).

If a media key is absent the bridge falls back to ``default_model``; when that
default is not a valid media model the provider returns no audio/text/description
(observed as the *"Transcription returned no text"* error on Auris, or Iris
failing to describe an image). Example for a Harmony endpoint mapped to cortex
and every media subsystem::

   {"stt_model": "faster-whisper-large-v3-turbo", "tts_model": "kitten-tts-nano", "tts_voice": "Luna", "tts_language": "default", "iris_model": "gemma4-meromero-26b-a4b"}

After saving, set ``ACTIVE_AURIS_ENGINE`` / ``ACTIVE_VOX_ENGINE`` /
``ACTIVE_IRIS_ENGINE`` to the endpoint ``Name`` to register it as the active
STT / TTS / vision engine.

Cortex extra config (advanced)
------------------------------

The ``Extra Config`` JSON field on a cortex endpoint accepts optional tuning
keys. Common ones:

- ``timeout`` (number): per-endpoint request timeout in seconds. Overrides the
  global ``LLM_GENERATION_TIMEOUT_SEC`` for this endpoint.
- ``enable_thinking`` (bool): opt into thinking/reasoning (default: ``false``).
  ``disable_thinking`` remains accepted for backwards compatibility, but is no
  longer needed. For Venice endpoints, SyntH translates this at the adapter
  boundary to the nested ``venice_parameters.disable_thinking`` request field;
  the alias is never sent at the top level or into action payloads.
- ``enable_tools`` (bool): opt this endpoint into native function/tool calling.
  Native tools remain disabled by default globally; ``disable_tools`` or
  ``force_action_grammar`` still takes precedence when present.
  For OpenAI-compatible endpoints, SyntH requests one required function call
  per turn and disables parallel tool calls. This keeps a model that is
  returning actions from flooding the message chain with a large batch. If a
  provider returns a successful plain ``actions`` JSON response instead of a
  native tool call, SyntH keeps only the first offered action as a fail-safe.
- ``max_tools`` (positive integer): cap the number of native tool definitions
  sent to this endpoint. Venice's current Gemma endpoint is automatically capped
  at 20 even when this key is omitted; an explicit value can lower that cap.
  On Vessel turns, the native set is scoped to Vessel/world actions first, with
  the core embodiment verbs retained ahead of optional world verbs.
- ``disable_tools`` (bool): stop advertising native function/tool-calling to this
  endpoint and use the legacy in-prompt JSON-action protocol instead. The full
  action catalog is folded into the system prompt, so nothing is lost — only the
  delivery changes. Recommended for small local quants that ignore native
  tool-calling and emit the action JSON in plain content anyway (advertising 49
  tools tends to confuse them into replies with no ``message_*`` action). Pairs
  well with ``force_json_object`` (with tools off, ``response_format`` is no
  longer suppressed, so it actually applies to chat turns).
- ``max_tokens`` (number): cap on completion length. An explicit value always
  applies. When unset, a safe default (4096) is applied **only** to local-model
  endpoints — those with ``disable_tools`` or ``force_action_grammar`` set; cloud
  openai endpoints (xAI, OpenRouter, …) stay uncapped unless you set this. The cap
  prevents a small local model stuck in a repetition loop from generating until it
  fills the whole context window (observed: ~28k tokens / 20 minutes).
- ``retry_attempts`` / ``retry_backoff`` / ``retry_on_timeout``: transient-error
  retry behavior.

Constrained JSON output (recommended for small local models)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Small local quants (llama.cpp / LM Studio Q4 models) frequently emit malformed
action JSON — unescaped quotes or missing delimiters on long replies — which
fails parsing and triggers corrector retries. Force the server to constrain
decoding to valid JSON:

- ``force_json_object`` (bool): adds ``response_format={"type": "json_object"}``.
  The simplest, broadly-supported option; guarantees syntactically valid JSON.
- ``response_format`` (object): forwarded verbatim, e.g. an explicit
  ``{"type": "json_schema", ...}`` constraint. Takes precedence over
  ``force_json_object``.
- ``grammar`` (string): a llama.cpp GBNF grammar, sent via ``extra_body`` for the
  strictest, schema-level constraint.
- ``force_action_grammar`` (bool): auto-build and send a GBNF grammar for the
  action-JSON shape (the ``type`` field is constrained to the exact set of
  available action names). This is the **strongest** option for local models — the
  model is physically forced to emit one well-formed
  ``{"actions":[{"type":<known name>,"payload":{...}}]}`` object: no ``<think>``/
  ``<thought>`` preamble, no malformed JSON, no invented/duplicated types, and no
  repeated trailing objects (generation stops after the first object). Implies
  ``disable_tools`` (a grammar constrains plain-content output). A manual
  ``grammar`` takes precedence. If a turn ever fails after enabling it, remove the
  key to fall back to the unconstrained path.

  The grammar also covers callers that don't pass a typed prompt request — most
  importantly the corrector's JSON-correction retries, which arrive as a raw
  string prompt. For those the ``type`` enum falls back to the **full registered
  action catalog** (rather than the per-turn scoped set), so correction retries
  stay grammar-constrained instead of regressing to unconstrained JSON that a
  small model cannot recover from. This fallback is gated on this flag, so other
  engines' correctors are never affected.

Example for a local llama.cpp endpoint::

   {"enable_thinking": false, "disable_tools": true, "force_json_object": true}

``disable_tools`` is usually the most impactful setting for small local quants:
it removes the native-tool confusion (the common cause of replies that contain
only a diary entry and no ``message_*`` action) and lets ``force_json_object``
take effect on chat turns. For the hardest guarantee on a llama.cpp backend,
prefer ``force_action_grammar`` over ``force_json_object`` (which many local
servers silently ignore)::

   {"enable_thinking": false, "force_action_grammar": true, "max_tokens": 4096}

For a model that supports native function calling, use the opt-in form instead::

   {"enable_thinking": false, "enable_tools": true, "max_tokens": 4096}

These are automatically dropped when native tool-calling is active for the
request (tool-calling already constrains output and most servers reject the
combination).

Use cases
---------

- Local development and testing:

  - Start Selenium LLM Engine and add it as an external endpoint using the
    built-in Selenium preset.
  - Confirm probe success and map the endpoint to ``cortex`` for chat usage.

- Multi-provider setups:

  - Add Gemini, Anthropic, OpenRouter, or GitHub Models endpoints.
  - Map each endpoint to the subsystems where it is strongest.

Legacy HTTP TTS endpoints
------------------------

Legacy TTS servers can still be integrated as a Vox provider using the
``custom`` protocol.

- ``Protocol``: ``custom``
- ``Base URL``: root path of the legacy TTS server
- ``Extra Config``: add at least ``{"legacy_http_tts": true}``

Optional extra config keys:

- ``tts_voice_wav``: path to the reference WAV file used by the remote server
- ``tts_endpoint_path``: custom POST path when the server does not accept
  requests at the base URL

After saving, set ``ACTIVE_VOX_ENGINE`` to the endpoint ``Name`` you created.
This registers the endpoint as a first-class Vox engine.

Troubleshooting
---------------

- If probe fails:
  - verify network connectivity from inside the container
  - use container DNS such as ``host.docker.internal`` when needed
  - verify the endpoint exposes ``/.well-known/openai-configuration`` and
    ``/v1/models``
- If the model list is missing:
  - ensure the provider returns valid ``data[]`` objects with ``id`` fields
  - check whether an API key or provider-specific permission is required

API Reference
-------------

See ``core/external_endpoints/registry.py`` and
``core/external_endpoints/probe.py`` for implementation details and field
meanings.
