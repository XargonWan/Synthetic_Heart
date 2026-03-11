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
  main prompt's `context.recon` and `instructions`.
- Debrief: postflight hook for plugins (`on_debrief`) to inspect processed
  and failed actions and optionally return recovery actions.
- Plugins: implement `get_recon_contributions()` and/or `on_debrief()`.
  Recon and Debrief plugins should each live in their own file under the
  `plugins/` directory (e.g. `recon_language_evaluator.py` or
  `debrief_action_intent.py`) and register themselves via the normal plugin
  registry. Core code only handles orchestration; individual plugin logic
  belongs in the `plugins/` folder.
- Language/Tone detectors: implemented as plugins or as Recon contributions.
  Language detector plugins should only consider the user message and recent
  history from the same `interface_path` when making a decision; global chat or
  other interface histories must not influence the chosen language.
  For the built-in language evaluator, incoming user text is given extra
  prominence (weight 3) while any assistant response and the surrounding
  local history are treated with weight 1 each when determining the primary
  language.

Plugin hooks and schemas
------------------------
- get_recon_contributions(self, *, message, context_memory, text, tags=None, keywords=None, max_results=5)
  - Return list of normalized contributions. Contribution `type` values:
    `memory`, `snippet`, `instruction`, `language_hint`, `tone_hint`, `log_flag`.
  - Contribution example:

    {
      "type": "language_hint",
      "language_code": "it",
      "priority": 10,
      "source": "lang_detector"
    }

- on_debrief(self, *, processed_actions, failed_actions, results, context, original_message)
  - Run after actions and event delivery. May return recovery actions:
    `{"recovery_actions": [...], "metadata": {...}}`.

How Recon affects the main prompt
---------------------------------
- Recon contributions are attached to `context.recon` (see `build_json_prompt`).
- Language and tone hints are resolved and injected as short instruction
  prefixes (e.g. "Use Italian language for the assistant replies.").
- Memory/snippet contributions are merged into the prompt `memories` block
  (deduped and prioritized).

Resolution precedence (language / tone)
--------------------------------------
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

Testing & compatibility
------------------------
- Recon contributions are optional; if no recon-capable plugins are
  registered the system skips Recon automatically.
- Debrief hooks are fail-safe: plugin exceptions are logged and ignored.
- The action-intent Debrief plugin (see `plugins/debrief_action_intent.py`) can
  propose recovery actions when the assistant implied or promised an action
  but did not execute it.

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
- Prompt builder and plugin-instance components now emit full JSON prompt
  dumps at DEBUG level (`[json_prompt]` and plugin-instance `🌐 JSON PROMPT`),
  along with raw LLM responses. When `LOG_LLM_TRAFFIC_ENABLED` is enabled a
  separate JSONL file is produced. Together these allow the entire sequence of
  prompts/responses to be replayed from the logs.
