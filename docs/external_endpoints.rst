External Endpoints
==================

Synthetic Heart supports connecting to external LLM endpoints and services via the
`External Endpoints` section in the web UI. This allows you to use:

- Ollama-compatible hosts (OpenAI API surface)
- OpenAI API endpoints (through bridge)
- Gemini/Claude/Anthropic endpoints
- Custom external providers that implement the OpenAI-style model list + chat API

.. note::

   External endpoints integrate with the built-in `cortex`, `vox`, `auris`, and
   `live` subsystems and can be used as first-class providers (similar to
   Qwen/Ollama/OpenAI endpoints). Mapping determines what role each endpoint
   takes in the subsystem pipeline.

Getting started
---------------

1. Open Web UI > Settings > External Engines (or External Endpoints tab).
2. Click **+ Add Endpoint**.
3. Fill in:
   - `Name`: internal identifier (data model key)
   - `Display Label`: human readable optional name
   - `Protocol`: `openai`, `gemini`, `anthropic`, `custom`
   - `Base URL`: host to query (e.g. `http://localhost:11435`)
   - `API Key` (optional; leave empty to keep existing secret)
4. Save; the system performs a background `probe` to detect capabilities and model list.

Probe status
------------

Probe result is shown on endpoint card:

- `success`: endpoint reachable, models enumerated
- `failed`: endpoint unreachable or unsupported response
- `pending`: probe in progress
- `never`: not probed yet

Model list and default model
---------------------------

- After successful probe, `available_models` is shown in model selector.
- You can set a default model for immediate use in all subsystems.
- If model list is empty, make sure endpoint supports `/v1/models` and has an
  OpenAI-compatible response payload.

Subsystem mapping
-----------------

Each endpoint can be mapped to subsystem roles:

- `cortex`: chat / reasoning
- `vox`: text-to-speech
- `auris`: speech-to-text
- `live`: real-time or multimodal

Map toggles are visible on the endpoint card and can be toggled per endpoint.

Use cases
---------

- Local container testing:

  - Start `selenium-llm-engine`, add endpoint `http://selenium-llm-engine:8000`,
    protocol `openai`.
  - Ensure `probe` succeeds and models are enumerated.
  - Map to `cortex` for chat in main interface.

- Multi-engine aggregation:

  - Add multiple endpoints like `ollama`, `gpt-4o-mini`, custom LLM.
  - Use endpoint mapping as a fallback or specialization by subsystem.

Legacy HTTP TTS endpoints
------------------------

If you need to integrate an existing legacy HTTP/Index-TTS style server as a Vox provider, register it as a ``custom`` external endpoint and enable the ``vox`` subsystem mapping.

- `Protocol`: ``custom``
- `Base URL`: host or root path of the TTS server
- `Extra Config`: add at least ``{"legacy_http_tts": true}``
- Optional extra config keys:
  - ``tts_voice_wav``: path to the reference WAV file used by the remote server
  - ``tts_endpoint_path``: custom POST path if the server does not accept requests at the base URL

After saving, set ``ACTIVE_VOX_ENGINE`` to the endpoint `Name` you created. This registers the endpoint as a first-class Vox engine and is the preferred external endpoint flow for HTTP TTS.

Troubleshooting
---------------

- If `probe` fails:
  - verify network connectivity from container to endpoint
  - check `base_url` uses container DNS (`host.docker.internal` for host machine)
  - validate endpoint `.well-known/openai-configuration` and `/v1/models`
- If model list is missing:
  - ensure provider returns valid `data[]` objects with `id` keys

API Reference
-------------

See `core/external_endpoints/registry.py` and `core/external_endpoints/probe.py` for
internal implementation and database field meaning.
