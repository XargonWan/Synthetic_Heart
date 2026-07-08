Recon & Debrief — preflight / postflight hooks
===============================================

Overview
--------
Recon (preflight) and Debrief (postflight) provide a small, extensible
pipeline for lightweight message-specific preprocessing and post-action
processing. Recon runs before the main prompt is built (prompt-0) and
collects structured contributions from Recon-capable plugins; Debrief
runs after action processing and can return recovery actions or enrich
metadata.

Key concepts
------------
- Recon: prompt-0, plugin-driven, JSON-only contributions injected into the
  main prompt's ``context.recon`` and ``instructions``.
- Debrief: postflight hook for plugins (``on_debrief``) to inspect processed
  and failed actions and optionally return recovery actions.
- Plugins: implement ``get_recon_contributions()`` and/or ``on_debrief()``.
  Recon and Debrief plugins should each live in their own file under the
  ``plugins/`` directory (e.g. ``recon_language_evaluator.py`` or
  ``debrief_action_intent.py``) and register themselves via the normal plugin
  registry. Core code only handles orchestration; individual plugin logic
  belongs in the ``plugins/`` folder.
- Language/Tone detectors: implemented as plugins or as Recon contributions.
  Language detector plugins should only consider the user message and recent
  history from the same ``interface_path`` when making a decision; global chat or
  other interface histories must not influence the chosen language.
  For the built-in language evaluator, incoming user text is given extra
  prominence (weight 3) while any assistant response and the surrounding
  local history are treated with weight 1 each when determining the primary
  language.

Plugin hooks and schemas
------------------------
- ``get_recon_contributions(self, *, message, context_memory, text, tags=None, keywords=None, max_results=5)``
  Return list of normalized contributions. Contribution ``type`` values:
  ``memory``, ``snippet``, ``instruction``, ``language_hint``,
  ``tone_hint``, ``log_flag``.

  Contribution example::

     {
       "type": "language_hint",
       "language_code": "it",
       "priority": 10,
       "source": "lang_detector"
     }

- ``on_debrief(self, *, processed_actions, failed_actions, results, context, original_message)``
  Run after actions and event delivery. May return recovery actions such as
  ``{"recovery_actions": [...], "metadata": {...}}``.

Debrief action-intent recovery
------------------------------
- The action-intent Debrief plugin is the canonical postflight path for
  missing-action recovery. It compares the original user message, the raw
  assistant reply, and the actions already processed or failed.
- Recovery is LLM-based and context-sensitive. It is not a keyword or fuzzy
  text scan.
- The plugin asks the LLM for canonical action JSON (``{"actions": [...]}``),
  then normalizes the result back into Debrief ``recovery_actions`` for the
  core orchestrator.
- If the Debrief LLM returns malformed or unusable JSON, the plugin may invoke
  the standard corrector middleware before giving up, using the same
  action-scope restrictions as the main response path.
- When auto-recovery is enabled, recovered actions are executed through the
  canonical action parser with the original interface/chat context preserved,
  so validation, safety policy, and selective correction still apply.

How Recon affects the main prompt
---------------------------------
- Recon contributions are attached to ``context.recon`` during
  ``build_prompt_request()`` (``build_json_prompt()`` remains as a deprecated
  compatibility alias).
- Language and tone hints are resolved and injected as short instruction
  prefixes (e.g. "Use Italian language for the assistant replies.").
- Memory/snippet contributions are merged into the prompt ``memories`` block
  (deduped and prioritized).

Resolution precedence (language / tone)
---------------------------------------
1. Interface override (`INTERFACE_LANGUAGE_OVERRIDES` / `INTERFACE_TONE_OVERRIDES`)
2. Recon contribution (`language_hint` / `tone_hint`) with highest priority
3. Detector plugin (highest priority wins)
4. Defaults (`PROJECT_DEFAULT_LANGUAGE`, `DEFAULT_GRILLO_LANGUAGE`, etc.)

Configuration
-------------
Important config flags (see UI / variables):

- ENABLE_RECON (bool, default: True)
- RECON_MAX_RESULTS (int, default: 5)
- RECON_TIMEOUT (s, default: 180)
- ENABLE_DEBRIEF (bool, default: True)
- LANGUAGE_DETECTOR_TIMEOUT / TONE_DETECTOR_TIMEOUT
- INTERFACE_LANGUAGE_OVERRIDES / INTERFACE_TONE_OVERRIDES

Examples
--------
- Implement a small Recon plugin that returns a `language_hint` and an
  `instruction` to be injected into the prompt (see `plugins/` for
  examples like `recon_log_reader.py`).

Video transcription (Recon Video Transcriber)
---------------------------------------------
The ``recon_video_transcriber`` plugin (``plugins/recon_video_transcriber.py``)
transcribes videos referenced in a conversation and attaches the result to the
prompt as a ``snippet`` contribution (the only recon type surfaced by the
prompt engine besides ``memory`` and ``instruction``).

How it works:

1. The shared Recon LLM call is asked (via ``get_recon_instruction``) to
   reconstruct a list of *canonical* YouTube watch URLs from the user's
   message. This handles bare video IDs (e.g. ``aoP81h68Xkk``) and malformed
   links (e.g. ``htps:/youtube.com/watch?v=...``) — no keyword or regex
   intent matching is used, so it works in any language.
2. ``parse_recon_response`` validates each candidate URL *structurally* with
   :func:`core.media_extract.is_youtube_url`, and also looks for a local video
   file referenced on the incoming message's ``raw_data`` (keys
   ``media_path`` / ``file_path`` / ``attachment_path`` / ``video_path``).
3. For each source it obtains a transcript:

   - YouTube → existing subtitles (fast path) or downloaded audio → Auris STT.
   - Local file → audio extracted via ``ffmpeg`` → Auris STT.
   - Optionally a visual description via Iris (local files only).

Because URL reconstruction runs on the message *text*, the primary use case
("transcribe this YouTube video") works on **every** interface (Telegram,
Discord, WebUI, Ollama API) with no interface-specific changes. Local video
visual passes require the interface to expose the downloaded file path on the
message's ``raw_data`` and keep the file alive until Recon runs.

Configuration flags:

- ``RECON_VIDEO_TRANSCRIBER_RECON_ENABLED`` (bool, default: True)
- ``RECON_VIDEO_MAX_SECONDS`` (int, default: 1800; 0 = no limit) — skip videos
  longer than this to stay within ``RECON_TIMEOUT``.
- ``RECON_VIDEO_INCLUDE_VISION`` (bool, default: True) — also run an Iris
  visual description for local video files.
- ``RECON_VIDEO_SNIPPET_MAX_CHARS`` (int, default: 12000; 0 = no limit) —
  truncate each transcript to avoid bloating the prompt.

Dependencies: ``yt-dlp`` (YouTube fetch + subtitles) and ``ffmpeg`` (audio
extraction). The subtitle fast-path avoids downloading/transcoding audio when
captions are available.

Testing & compatibility
------------------------
- Recon contributions are optional; if no recon-capable plugins are
  registered the system skips Recon automatically.
- Debrief hooks are fail-safe: plugin exceptions are logged and ignored.
- The action-intent Debrief plugin (see `plugins/debrief_action_intent.py`) can
  propose recovery actions when the assistant implied or promised an action
  but did not execute it. Typical examples are reminders or follow-up actions
  promised in natural language but omitted from the main JSON reply.

See also
--------
- core/recon.py — implementation and resolution helpers
- core/prompt_engine.py — where Recon contributions are injected into the prompt
- core/debrief.py — Debrief orchestration and recovery-policy handling

API reference
-------------
- gather_recon_contributions(message, context_memory, text, tags, keywords)
  -> List[contribution]
- resolve_language(...) and resolve_tone(...) helpers used by prompt builder

Changelog
---------
- Recon replaces the legacy preflight system and consolidates language,
  tone and memory-detection into plugin-driven preflight contributions.
- Added comprehensive debug logging for both Recon and Debrief; logs now
  include input parameters, generated system/user prompts, LLM responses,
  parsed data, plugin dispatch details and recovery-action decisions.
- Prompt builder and plugin-instance components now emit compatibility prompt
  dumps and renderer-backed prompt debug traces at DEBUG level, along with raw
  LLM responses. When ``LOG_LLM_TRAFFIC_ENABLED`` is enabled a separate JSONL
  file is produced so prompt / response sequences can still be replayed after
  the prompt rewrite.
