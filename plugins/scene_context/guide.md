# Current Scene

A small, optional spatial-continuity tracker for direct human conversations.
It keeps the scene separate from biography, memories, temporal notes, emotions
and the diary. It does not create Agent-Lane tasks, tools or parallel message flows.

## What Synth sees

The Reality Anchor contains a `[Current scene]` block, repeated immediately
above the current user message. A scene can contain:

- Shared location, when the human establishes co-presence.
- The persona's position and the human's position.
- A directional relationship, for example `You (2B) on top of Human`.
- Shared activity and the persona's own activity.

`self` always means Synth and `human` means the direct conversation's human.
The relationship stores subject and object separately; reversing them is a
change of state, not an alternative wording. Unlisted details remain unknown.
This is language-model-assisted continuity, not physical sensing or a guarantee
that a model will never contradict the anchor.

## Update lifecycle

1. **Recon, before the reply:** the existing combined Recon call receives the
   saved scene and extracts only changes supported by exact quotations from the
   current human message. This applies explicit corrections before generation.
   Global history and persona assertions are not evidence. Questions, wishes,
   hypothetical situations and discussions about this feature do not enact a
   new scene. No extra preflight model call is added when Recon already runs.
2. **Prompt:** the optional static injection loads this conversation's scene.
   Both anchor surfaces use the same block. Old conflicting narration is not
   authoritative.
3. **Debrief, after delivery:** a bounded model call per new delivery batch (5
   seconds, up to 512 output tokens where the engine supports these options)
   may record explicitly enacted first-person movements in successfully processed `send_message`
   actions to this conversation, including legacy `message_*` delivery actions
   with a matching destination. Duplicate reply text in the same turn is not
   processed twice, while a later correction batch can still update the scene.
   It uses the turn's selected Cortex scope/model and only an already-loaded
   engine. It never reads unsent diary/thought text or
   failed sends. It cannot update the human's position, shared location or
   shared activity. Fields corrected by the human during this turn are protected.
   A stale concurrent writer is rejected by a revision check.

Postflight adds an extra model request after delivery, so it adds provider cost
and can hold the message consumer for up to five seconds during generation.
Disable `SCENE_CONTEXT_POSTFLIGHT_ENABLED` for human-only tracking without that
request. No model is loaded just for this optional update.

The feature runs only on structurally identified direct human conversations.
Groups, bots, Grillo/internal beats and Rift Vessel turns are excluded. Routing
comes from the message first, then the queue's `interface_path` context field
(native immutable Telegram messages use the latter). Scenes are not shared
across interface paths or threads. Unknown chat kinds fail
closed. Postflight requires the same turn's preflight snapshot; with Recon
switched off, automatic scene updates stop rather than guessing from the reply.

## Standing spoken-conversation convention

The existing `SCENE_NOTE` setting is complementary. Set it to describe how the
conversation is experienced, for example: "We speak and hear each other aloud;
the messaging interface is only the transport. Do not infer a phone or typing."
That is a scene convention, not a claim that actual TTS or STT is enabled, and
it does not by itself assert physical co-location. Prefer a participant-specific
note when the convention applies only to one person. It is deliberately not a
hardcoded project default. `SCENE_NOTE` is independent of this plugin.

## Settings and recovery

- `SCENE_CONTEXT_ENABLED`: opt in to injection and tracking, default `False`.
  Off means no scene Recon contribution, no postflight request and no scene
  injection. Enabling can require a Recon call where language-hint shortcuts
  would otherwise have skipped it.
- `SCENE_CONTEXT_MAX_AGE_SECONDS`: per-field freshness window, default `21600`
  (6 hours), clamped to 60 seconds through 24 hours. Ordinary unrelated turns do
  not refresh a field. Expired fields disappear instead of becoming invented facts.
- `SCENE_CONTEXT_POSTFLIGHT_ENABLED`: track the persona's enacted movements,
  separate opt-in, default `False`. Enable only when the additional postflight
  model request/cost is wanted. Master tracking must also be enabled. Leaving
  this off retains human-only preflight updates when the master is on.

Both switches are ordinary, non-advanced boolean controls in WebUI Dashboard /
Control Deck under **Scene Context**, and in the Current Scene plugin's settings.
The plugin itself can also be powered off from its component card. Switches
take effect on the next hook without a restart. The standing `SCENE_NOTE` is
independent and does not add an extraction request.

No-change extraction results avoid rewriting scene state; they do not eliminate
the model request that determines there was no change. Fresh deployments are
off by default. Existing explicitly saved opt-ins keep their chosen values.

An explicit human scene reset or scene transition can clear the old state;
individual fields can also be cleared without resetting the entire scene.
No initial location, posture or activity is invented on installation.

The plugin stores only bounded current fields and their update times/sources in
`data/scene_context.sqlite3`, keyed by `interface_path`. SQLite operations run off
the event loop; no new dependency or deployment-database migration is required.
The file survives process restarts but is not part of SOUL/PostgreSQL backups:
include it in file backups if scene continuity across restore is desired. Scene
content is sensitive; the plugin's own logs contain status/error types only.
Per-turn scene snapshots stay in a bounded plugin-private map; the core's
debug-logged context carries only an opaque token and non-content field names.
Model-call tracing follows the deployment's existing tracing policy.
Existing core DEBUG prompt logging can still contain rendered scene text, just
as it contains conversation text; keep those logs private.

Storage/model failures skip tracking without failing the conversation. Removing
or disabling the plugin leaves ordinary prompt assembly intact. The existing
Recon and Debrief global switches must be enabled for their respective phases.
Core prompt-rendering changes require a normal process restart to activate.
