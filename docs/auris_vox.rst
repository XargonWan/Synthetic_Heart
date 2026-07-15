Auris & Vox — Audio Subsystem
==============================

.. versionadded:: 2.0

Overview
--------

**Auris** (Latin for *ear*), **Vox** (Latin for *voice*), and **Live** form the three complementary cores of Synthetic Heart's unified audio framework:

* **Auris** — *file-based STT*: accepts a complete audio file, returns a transcript string.
* **Vox** — *file-based TTS*: accepts a text string, returns synthesised audio bytes.
* **Live** — *bidirectional streaming*: persistent sessions with interleaved PCM-in / transcript-out and text-in / audio-out.

The three registries follow the same plug-and-play pattern as the LLM cortex engines.  New engines can be added without touching any core code.

Architecture
------------

.. code-block:: text

       ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐
       │  AurisRegistry  │  │  VoxRegistry    │  │  LiveRegistry    │
       │ auris_registry  │  │  vox_registry   │  │  live_registry   │
       └───────┬─────────┘  └───────┬─────────┘  └────────┬─────────┘
               │ file-based STT     │ file-based TTS       │ bidirectional
               ▼                    ▼                      ▼
    ┌───────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
    │   AurisPlugin     │ │    VoxPlugin         │ │  (webui WS /stream) │
    │  transcribe_audio │ │  speak()             │ │  LiveRegistry.load  │
    │  stt_transcribe   │ │  tts_speak           │ │  open/send/receive  │
    └─────────┬─────────┘ └──────────┬───────────┘ └──────────┬──────────┘
              │                      │                         │
     ┌────────┴────────┐   ┌─────────┴──────────┐   ┌─────────┴──────────┐
     │  Auris Engines  │   │   Vox Engines       │   │   Live Engines     │
     │  gemini.py      │   │   http.py           │   │   silero.py (VAD)  │
     │                 │   │                     │   │  gemini.py (stub) │
     └─────────────────┘   │   kitten.py         │   └────────────────────┘

Registry Pattern
----------------

Both registries follow the same conventions as ``core/cortex_registry.py``.

Registering an engine
~~~~~~~~~~~~~~~~~~~~~

An Auris engine module must define:

* ``ENGINE_CLASS`` — the class that extends ``AurisEngineBase``
* Optional call to ``register_auris_engine()`` (the class auto-registers via the base ``CAPABILITIES`` dict)

.. code-block:: python

   # plugins/auris_engines/my_engine.py
   from plugins.auris_base import AurisEngineBase
   from core.auris_registry import register_auris_engine


   class MyAurisEngine(AurisEngineBase):
       def transcribe(self, file_path: str, mime_type: str | None = None) -> str | None:
           # ... call your STT service ...
           return transcribed_text


   ENGINE_CLASS = MyAurisEngine

   register_auris_engine(
       "my_engine",
       "plugins.auris_engines.my_engine",
       {"file_based": True, "local": True},
   )

A Vox engine follows the same pattern:

.. code-block:: python

   # plugins/vox_engines/my_engine.py
   from plugins.vox_base import VoxEngineBase
   from core.vox_registry import register_vox_engine


   class MyVoxEngine(VoxEngineBase):
       def generate_tts(self, text: str, emotion: str | None = None, **kwargs) -> bytes | None:
           # ... synthesise audio, return WAV or raw PCM bytes ...
           return audio_bytes


   ENGINE_CLASS = MyVoxEngine

   register_vox_engine(
       "my_engine",
       "plugins.vox_engines.my_engine",
       {"streaming": False, "local": True, "voice_cloning": False, "emotions": True},
   )

Auris — STT Subsystem
----------------------

Plugin: ``auris_plugin``
~~~~~~~~~~~~~~~~~~~~~~~~

**Vosk-specific configuration**

When the ``auris_vosk`` engine is selected, two additional variables control
language handling:

* ``VOSK_LANGUAGE`` – language code for the Vosk model (``"en-us"``,
  ``"it"``, ``"fr"`` etc).  Starting in version 2.?, this defaults to
  ``"auto"`` instead of English; in ``auto`` mode the first few seconds of
  audio are probed by a Whisper‑tiny model (via the optional ``faster-whisper``
  package) to identify the spoken language.  If detection fails or the
  dependency is missing, the first downloaded Vosk model is used as a fallback.
  Explicitly setting ``VOSK_LANGUAGE`` overrides auto‑detection.

* ``VOSK_LID_CONFIDENCE`` – when Whisper LID is used, the minimum probability
  (0–1) required to accept the detected language.  Defaults to ``0.5``.  If
  the confidence is below this threshold the fallback path is taken.



The ``AurisPlugin`` is the single authoritative entry-point for all transcription.  Other plugins, interfaces, and cortex engines **must not** call STT backends directly — they should always call ``AurisPlugin.transcribe_audio()``.

Configuration (all WebUI-configurable):

.. list-table::
   :header-rows: 1
   :widths: 30 10 60

   * - Variable
     - Default
     - Description
   * - ``ACTIVE_AURIS_ENGINE``
     - ``disabled``
     - Name of the active engine (e.g. ``gemini`` or ``vosk``).  Set to ``disabled`` to disable the Auris subsystem.
   * - ``AURIS_ENGINE_SETTINGS``
     - ``{}``
     - JSON string of engine-specific settings forwarded at load time.

Public API:

.. code-block:: python

   # From any interface or plugin:
   from core.core_initializer import PLUGIN_REGISTRY

   auris = PLUGIN_REGISTRY.get("auris_plugin")
   if auris:
       text = await auris.transcribe_audio(
           "/path/to/audio.ogg",
           mime_type="audio/ogg",        # optional MIME hint
           engine_name="gemini",         # optional per-call override
       )

LLM action ``stt_transcribe``:

.. code-block:: json

   {
     "type": "stt_transcribe",
     "payload": {
       "audio_path": "/tmp/live_io/in_123.oga",
       "mime_type": "audio/ogg",
       "engine": "gemini"
     }
   }

``AurisEngineBase`` contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All engines must extend ``plugins.auris_base.AurisEngineBase``.
Auris engines are **file-based only** — for real-time / bidirectional streaming use ``LiveEngineBase`` instead.

.. code-block:: python

   class AurisEngineBase(ABC):
       # Required
       @abstractmethod
       def transcribe(self, file_path: str, mime_type: str | None = None) -> str | None: ...

       # Lifecycle hooks
       def setup(self) -> None: ...
       def teardown(self) -> None: ...

Available Auris engines
~~~~~~~~~~~~~~~~~~~~~~~

Most Auris capabilities are now configured through the External Endpoints UI. Add a provider as an endpoint and enable the `auris` mapping when the endpoint supports STT.

The only built-in Auris engine currently shipped by default is:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Alias
     - File
     - Notes
   * - ``vosk``
     - ``plugins/auris_engines/vosk_engine.py``
     - Local speech recognition engine. File-based, offline, and suitable for self-hosted deployments.

.. note::

   **Silero** has moved to the Live registry (``live_engines/silero.py``).
   Importing ``auris_engines/silero`` will emit a deprecation warning and
   register nothing.

Vox — TTS & Lip-sync Subsystem
---------------------------------

Plugin: ``vox_plugin``
~~~~~~~~~~~~~~~~~~~~~~

*Text language detection*

The core plugin performs language detection on the input text using
``lingua-language-detector`` (a much more accurate replacement for the old
``lingua-language-detector``).  The detected ISO-639-1 code (``"en"``, ``"it"`` etc)
is logged and forwarded to the active Vox engine via a ``language`` keyword
argument.  Engines can optionally use this hint to select an appropriate voice
or model.  ``lingua`` is a required dependency; if detection confidence is below
the internal threshold, no hint is supplied and the plugin continues as before.


``VoxPlugin`` owns the **entire** TTS pipeline:

1. Text cleaning (emoji removal, whitespace normalisation)
2. Engine selection and audio generation
3. WAV/PCM file writing to ``VOX_OUTPUT_DIR``
4. Optional lip-sync data extraction (``engine.get_lipsync_data()``)
5. Audio dispatch to the originating interface (WebUI ``synth:tts-play``, Discord, Telegram)
6. Text fallback when the engine fails

No interface or other plugin should handle TTS audio files or dispatch lip-sync events directly.

Configuration (all WebUI-configurable):

.. list-table::
   :header-rows: 1
   :widths: 30 10 60

   * - Variable
     - Default
     - Description
   * - ``ACTIVE_VOX_ENGINE``
     - ``kitten``
     - Name of the active TTS engine.  Set to ``disabled`` to disable the Vox subsystem.
   * - ``VOX_LANGUAGE_OVERRIDES``
     - ``{}``
     - JSON map of ISO-639-1 language code → ``{"engine", "model", "voice"}``
       used to route TTS to a different engine / model / voice per detected
       language.  Languages not present in the map use ``ACTIVE_VOX_ENGINE``
       (and its default model / ``<ENGINE>_VOICE``).  An entry whose ``engine``
       is ``"disabled"`` is treated as "use the default engine".
   * - ``VOX_ENGINE_SETTINGS``
     - ``{}``
     - JSON string forwarded to the engine at load time.
   * - ``VOX_OUTPUT_DIR``
     - ``tmp_tts``
     - Directory where generated audio files are written.
   * - ``VOX_TIMEOUT_SECONDS``
     - ``10``
     - Maximum seconds to wait for a TTS engine response.
   * - ``VOX_FALLBACK_TO_TEXT``
     - ``true``
     - When ``true``, sends a plain-text message if TTS generation fails.

.. note::

   A fresh installation uses ``kitten`` as the default ``ACTIVE_VOX_ENGINE``.

WebUI helper endpoints
~~~~~~~~~~~~~~~~~~~~~~

Two read-only HTTP endpoints support voice selection and sample playback in the
web interface.  Engines may implement them if they expose multiple speakers or
wish to supply short example clips.

* ``GET /api/vox/speakers?engine=<name>``
  returns a JSON array of speaker metadata for the specified engine (the
  configured ``ACTIVE_VOX_ENGINE`` is used when the query parameter is
  omitted).  The format is engine-specific; ``kitten`` returns
  ``[{"code": "en_1", "name": "English Female 1", "language": "en"}, …]``.
  If the engine is unknown a ``404`` is returned.

* ``GET /api/vox/sample?engine=<name>&speaker=<code>``
  streams a short WAV file for the given speaker.  Engines that cannot provide
  samples should raise ``NotImplementedError`` which results in a ``404``.
  A missing ``speaker`` parameter produces a ``400`` error.

These helpers are used internally by ``res/synth_webui/js/main.js`` to
populate the Kitten voice selector and play sample audio.

Per-language engine overrides
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

By default every message is spoken with the single ``ACTIVE_VOX_ENGINE`` (and
its configured model / ``<ENGINE>_VOICE``).  To use a *different* engine,
model, or voice depending on the language of the text, set the
``VOX_LANGUAGE_OVERRIDES`` config key to a JSON map:

.. code-block:: json

   {
     "it": {"engine": "fish-audio", "model": "s2.1-pro", "voice": "maria"},
     "en": {"engine": "kitten",     "model": "",         "voice": "luna"}
   }

* The map key is an ISO-639-1 language code (``it``, ``en``, ``fr`` …).  Region
  variants are normalised, so ``it-it`` matches the ``"it"`` entry.
* ``engine`` is required and selects the TTS engine for that language.  Use
  ``"disabled"`` to opt a language back out to the default engine.
* ``model`` and ``voice`` are optional.  When set they are forwarded as
  explicit per-call values, so they take priority over the engine's default
  model (``VOX_DEFAULT_MODEL``) and the ``<ENGINE>_VOICE`` config key.  Leave
  them as empty strings to keep the engine's defaults.
* Language is detected from the cleaned reply text via ``lingua`` inside
  ``VoxPlugin.speak()``; the override is applied only when no explicit
  per-call ``engine_name`` was supplied.

The **Engines** tab exposes this through an *Add language override* editor
(both the classic WebUI and the Vue frontend): pick a language from the full
ISO-639-1 list, then choose engine → model → voice exactly as for the default
engine.  The selection is persisted to ``VOX_LANGUAGE_OVERRIDES`` via
``POST /api/config``.  A read-only ``GET /api/languages`` endpoint serves the
language catalogue used to populate the combo box.

Public API:

.. code-block:: python

   from core.core_initializer import PLUGIN_REGISTRY

   vox = PLUGIN_REGISTRY.get("vox_plugin")
   if vox:
       result = await vox.speak(
           "Hello, world!",
           interface_path="synth_webui/session_abc",
           emotion="joy",
           engine_name="http",           # optional per-call override
           merged_text="Hello, world!",  # plain-text fallback caption
       )
       # result = {"status": "success"|"skipped"|"error", "filename": ..., ...}

LLM action ``tts_speak``:

.. code-block:: json

   {
     "type": "tts_speak",
     "payload": {
       "text": "Hello, world!",
       "emotion": "joy"
     }
   }

When ``tts_speak`` is delivered alongside a standard message action (for
example the LLM returns both a ``message_telegram_bot`` and a ``tts_speak``),
message_chain will automatically merge the text payload into the TTS action as
``__merged_text``.  This ensures that users receive a single audio message with
a caption and prevents the duplicate text reply that would otherwise occur.
The ``merged_text`` field is also used as a fallback caption when the TTS
engine fails.

``VoxEngineBase`` contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~

All engines must extend ``plugins.vox_base.VoxEngineBase``:

.. code-block:: python

   class VoxEngineBase(ABC):
       # Required
       @abstractmethod
       def generate_tts(self, text: str, emotion: str | None = None, **kwargs) -> bytes | None: ...

       # Optional — override to report your format
       @property
       def output_format(self) -> str: return "wav"   # or "pcm"
       @property
       def sample_rate(self) -> int: return 22050
       @property
       def channels(self) -> int: return 1

       # Optional — return mouth-shape data for the renderer
       def get_lipsync_data(self, audio_bytes: bytes) -> dict | None: return None

``generate_tts`` must return:

* ``bytes`` — WAV file bytes when ``output_format == "wav"``  (i.e. ``RIFF`` header present)
* ``bytes`` — raw PCM samples when ``output_format == "pcm"``
* ``None`` — on failure (triggers fallback)

``VoxPlugin`` wraps raw PCM in a valid WAV container before writing to disk, so engines only need to return the sample data.

Available Vox engines
~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Alias
     - File
     - Notes
   * - ``http``
     - ``vox_engines/http.py``
     - Generic HTTP TTS engine. Posts to one or more external TTS servers
       with failover, in either the legacy ``{text, voice_wav}`` payload
       style or the reference-id style used by
       Fish Audio's ``/v1/tts``. Fully configurable from the WebUI Engines
       tab (Vox → http box) — see the key table below. The legacy
       ``TTS_ENDPOINTS`` / ``TTS_TIMEOUT_SECONDS`` keys are still honoured
       as fallbacks.
   * - ``kitten``
     - ``vox_engines/kitten.py``
     - Neural KittenTTS engine; requires the ``kittentts`` package or uses
       the vendored shim (`vendor/kittentts`).  The shim is lightweight and
       lazily imports ``gtts``/``pydub`` – those two libraries are declared as
       normal project dependencies and **must be installed** (``uv add gtts
       pydub``) for audio output to work.  Without them the engine will fall
       back to text and log an informative error.  Produces higher-quality
       audio than the legacy system-voice implementation.

HTTP engine configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~

All keys are editable in the WebUI: Engines tab → Vox → select ``http`` →
expand the *http* box → **Configuration**.

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Key
     - Default
     - Purpose
   * - ``HTTP_TTS_ENDPOINTS``
     - *(empty)*
     - Comma-separated endpoint URLs, tried in order (failover). Fish
       Audio: ``https://api.fish.audio/v1/tts``. Falls back to the legacy
       ``TTS_ENDPOINTS`` key when empty.
   * - ``HTTP_TTS_API_KEY``
     - *(empty)*
     - Sent as ``Authorization: Bearer <key>``. Required by Fish Audio.
   * - ``HTTP_TTS_MODEL``
     - *(empty)*
     - Sent as a ``model`` HTTP header when set (Fish Audio tiers:
       ``s2.1-pro-free``, ``s2.1-pro``, ``s1``).
   * - ``HTTP_TTS_REFERENCE_ID``
     - *(empty)*
     - Voice ``reference_id`` (Fish Audio cloned/library voice). Setting it
       switches the payload to the Fish-style
       ``{text, reference_id, format}`` schema; empty keeps the legacy
       ``{text, voice_wav, use_emo_text}`` schema.
   * - ``HTTP_TTS_FORMAT``
     - ``pcm``
     - Audio format returned by the server (``pcm`` or ``wav``). Use
       ``wav`` for Fish Audio. Also sent as the payload ``format`` field in
       reference-id mode.
   * - ``HTTP_TTS_SAMPLE_RATE``
     - ``22050``
     - Sample rate used to wrap raw PCM responses into WAV (Fish Audio pcm
       is 44100 Hz). Ignored for ``wav``.
   * - ``HTTP_TTS_VOICE_WAV``
     - *(empty)*
     - Server-side reference-voice WAV path for legacy payload mode;
       omitted from the payload when empty.
   * - ``HTTP_TTS_EXTRA_HEADERS``
     - ``{}``
     - JSON object merged into the request headers.
   * - ``HTTP_TTS_EXTRA_PARAMS``
     - ``{}``
     - JSON object merged into the request payload (e.g. Fish Audio
       prosody controls such as ``temperature`` / ``top_p``).
   * - ``HTTP_TTS_TIMEOUT_SECONDS``
     - ``0``
     - Per-request timeout; ``0`` falls back to the legacy
       ``TTS_TIMEOUT_SECONDS`` key (default 300).

Example — Fish Audio ``s2.1-pro-free``: set the endpoint to
``https://api.fish.audio/v1/tts``, paste your API key, set the model to
``s2.1-pro-free``, set the reference ID to your cloned/library voice id and
the format to ``wav``, then select ``http`` as the active Vox engine.

Lip-sync Integration
---------------------

Lip-sync is **fully centralised** in ``VoxPlugin``.  Engines that can produce viseme/mouth-shape data implement ``VoxEngineBase.get_lipsync_data(audio_bytes)``:

.. code-block:: python

   def get_lipsync_data(self, audio_bytes: bytes) -> dict | None:
       # Return a dict compatible with the WebUI synth:lipsync event,
       # or None to skip lipsync for this utterance.
       return {"mouths": [...], "duration": 3.14}

``VoxPlugin.speak()`` calls this method automatically after writing the audio to disk and includes the result in the dispatched ``synth:tts-play`` payload.

No interface or plugin should call a lipsync API directly — all lipsync dispatch goes through ``VoxPlugin``.

Migration from ``tts_lipsync``
--------------------------------

The legacy ``tts_lipsync`` plugin is maintained only for backward compatibility and should be avoided for new deployments. To move external HTTP TTS support to the modern external endpoint flow:

1. Add a new endpoint in the Web UI under Settings > External Engines / External Endpoints.
2. Choose ``Protocol: custom`` and set ``Base URL`` to the root URI of your HTTP TTS server.
3. In ``extra_config``, add ``{"legacy_http_tts": true}`` and any optional adapter settings such as ``tts_voice_wav`` or ``tts_endpoint_path``.
4. Enable the ``vox`` subsystem mapping for the endpoint.
5. Set ``ACTIVE_VOX_ENGINE`` to the endpoint ``Name`` you created.

This registers the endpoint as a first-class Vox engine and removes the need to manage ``TTS_ENDPOINTS`` manually. The built-in ``http`` engine and legacy ``TTS_*`` keys remain available only for compatibility with existing deployments.

Live — Bidirectional Streaming
-------------------------------

The **Live** subsystem handles persistent sessions where audio and text flow in both directions simultaneously (e.g. a microphone feed producing transcripts while the system synthesises speech).

Configuration: select the active engine via ``LIVE_CORTEX`` in the WebUI. The components page dropdown is populated with both
cortex engines of kind ``live`` and any external endpoints that were added
and mapped to ``live``. The currently-selected value is highlighted and
persists across page reloads; choosing ``disabled`` turns the subsystem off.

Note: Gemini Live is only available after adding it as an external endpoint and enabling the ``live`` mapping; it is not automatically exposed by default.


``LiveEngineBase`` contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~

All engines extend ``plugins.live_base.LiveEngineBase``:

.. code-block:: python

   class LiveEngineBase(ABC):
       @property
       def supports_input(self) -> bool: return False   # PCM → transcript
       @property
       def supports_output(self) -> bool: return False  # text → audio

       # Session lifecycle
       async def open_session(self, session_id: str, **kwargs) -> None: ...  # abstract
       async def close_session(self, session_id: str) -> None: ...           # abstract

       # Data channels
       async def send_audio(self, session_id: str, chunk: bytes, sample_rate: int = 16000) -> None: ...
       async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]: ...  # abstract
       async def send_text(self, session_id: str, text: str) -> None: ...

       # Lifecycle hooks
       def setup(self) -> None: ...
       def teardown(self) -> None: ...

``LiveEvent`` carries typed payloads:

.. code-block:: python

   @dataclass
   class LiveEvent:
       type: LiveEventType          # TRANSCRIPT | AUDIO | VAD | ERROR
       text: str | None             # transcript text
       audio: bytes | None          # synthesised audio chunk
       is_final: bool               # True = committed transcript segment
       vad_signal: str | None       # "speech_start" | "speech_end"
       detail: str | None           # error detail or free-form annotation
       metadata: dict               # engine-specific extras

WebSocket streaming endpoint
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The WebUI exposes ``GET /api/audio/stream`` as a WebSocket.
It speaks the Live registry protocol directly:

1. Client sends a JSON config frame: ``{"sample_rate": 16000, "engine": "silero"}``
2. Client streams binary PCM frames (raw 16-bit mono).
3. Server replies with JSON events:

   * ``{"type": "partial", "text": "..."}`` — interim transcript
   * ``{"type": "final", "text": "..."}`` — committed transcript segment
   * ``{"type": "vad", "signal": "speech_start"|"speech_end"}``) — VAD markers
   * Binary frames — synthesised TTS audio (AUDIO events)

Available Live engines
~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Alias
     - File
     - Notes
   * - ``silero``
     - ``live_engines/silero.py``
     - Silero VAD with async queue per session. Local, CPU-friendly. Connect a real ASR model in ``_transcribe_segment``.

.. note::

   Gemini Live is not exposed automatically. Add a Gemini Live-capable endpoint through the External Endpoints UI and enable the ``live`` subsystem mapping if you want a Gemini-based live engine.

Adding a Live engine
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   # plugins/live_engines/my_engine.py
   from plugins.live_base import LiveEngineBase, LiveEvent, LiveEventType
   from core.live_registry import register_live_engine

   class MyLiveEngine(LiveEngineBase):
       supports_input = True
       supports_output = True

       async def open_session(self, session_id, **kwargs): ...
       async def close_session(self, session_id): ...
       async def receive_events(self, session_id):
           yield LiveEvent(type=LiveEventType.TRANSCRIPT, text="hello", is_final=True)

   ENGINE_CLASS = MyLiveEngine
   register_live_engine("my_engine", __name__, {"input": True, "output": True, "local": True})

Testing
-------

.. code-block:: bash

   uv run pytest tests/test_auris_plugin.py tests/test_vox_plugin.py tests/test_live_registry.py -v

Test suites cover:

* **Auris** registry: register, list, find-by-capabilities, missing ``ENGINE_CLASS``, instance caching; ``AurisPlugin.transcribe_audio()`` paths; ``AurisEngineBase`` contract (file-based only)
* **Vox** registry: same registry coverage; ``VoxPlugin.speak()`` disabled/success/fallback paths; ``VoxEngineBase`` property defaults
* **Live** registry: register, list, find-by-capabilities, missing ``ENGINE_CLASS``; ``LiveEngineBase`` defaults (``supports_input/output``); ``LiveEvent`` type enumeration
