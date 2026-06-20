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
- Ollama local hosts
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

Cortex extra config (advanced)
------------------------------

The ``Extra Config`` JSON field on a cortex endpoint accepts optional tuning
keys. Common ones:

- ``timeout`` (number): per-endpoint request timeout in seconds. Overrides the
  global ``LLM_GENERATION_TIMEOUT_SEC`` for this endpoint.
- ``disable_thinking`` (bool): send ``enable_thinking=False`` (Qwen3 / LM Studio)
  to stop the model from spending the context window on chain-of-thought.
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

Example for a local llama.cpp endpoint::

   {"disable_thinking": true, "force_json_object": true}

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
