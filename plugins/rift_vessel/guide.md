# Rift Vessel

The **Rift Vessel** subsystem lets Synth *inhabit* external game and virtual
worlds (Minecraft, and — registry-ready — Skyrim, VRChat, Hytale) through
pluggable **connectors**, while its identity, memory, and personality persist
across every world and chat interface.

A Vessel is a **layer of embodiment**, not a separate mind: cognition, memory,
and identity always stay in the Synth core. A connector only translates
in-world events into normalized perceptions and Synth's normalized actions into
world-specific commands.

## Actions

All actions are `security_level: "low"` and declare **no** `external_effects`,
so they stay on the Fast Lane and never spawn agentic tasks or Drones.

The action set is **connection-driven** — it reflects whether Synth is currently
embodied, and changes automatically on the next prompt (no restart):

* **Disconnected** — only `vessel_connect` is exposed. Its required `game` field
  is an enum of every *enabled* world (a world is enabled when its
  `<world>_vessel` sub-plugin is on); optional `host`/`port` override that
  world's server address for the connect only.
* **Connected to world W** — the core set below (minus `connect`) plus W's own
  world-specific verbs appear namespaced `vessel_<W>_<verb>` (e.g.
  `vessel_minecraft_say`), together with `vessel_disconnect`. `vessel_connect`
  disappears while embodied.
* **Logout / idle cooldown** (`VESSEL_SESSION_COOLDOWN_SEC`) — the session
  closes and on the next prompt the gameplay verbs vanish and `vessel_connect`
  returns.

Ownership is **hybrid**:

* **Core set** (below) — owned by the Vessel and shared by *every* world,
  guaranteeing world-agnostic portability.
* **World-specific verbs** — each connector may add its own (e.g. a future
  Minecraft `craft`/`mine`, Skyrim `cast_spell`/`sneak`) by overriding
  `get_world_actions()`; they appear under the same `vessel_<world>_` prefix.

| Verb (core set) | Purpose |
|--------|---------|
| `connect` | Enter a world — the `game` field picks which one (optional `host`/`port` override). Only exposed while disconnected. |
| `disconnect` | Leave the world and flush the session diary entry |
| `say` | Speak/chat inside the current world (optional `audio` flag — falls back to text where the world has no voice channel) |
| `move` | Move the avatar (direction / target) |
| `look` | Turn / aim the avatar's view |
| `use` | Interact benignly with a nearby object/entity (for combat use `attack`) |
| `attack` | Hostile action against a target entity (defaults to the nearest attackable one) |
| `follow` | Start following an entity — the player or an NPC (fails cleanly if there is nothing to follow) |
| `unfollow` | Stop following |
| `status` | Report the current world state |

## Exploration (`explore`)

One walk with no place in mind, for every world: the core decides WHERE to go and WHEN to stop
(`plugins/rift_vessel/exploration.py`), the adapter answers one question at a time. A world opts in by mixing in
`ExplorerMixin` and implementing four calls; `explore` then starts a background run, answers at once, and the result
comes back as ONE `explore_done` event (prose worded in the core, so the model reads the same words in every world).

| call | answers |
|---|---|
| `explore_probe(viewpoints, max)` | `Probe`: position, heading, `candidates` (places it already knows a way to, with the cost of the way, and whether any of the `viewpoints` has a line of sight to each), `pois` (things to go up to, with their `area`), `hazards` (the player, something moving, a hostile mob; kind `threat` also ends a leg). `candidates=None` means "I cannot list what is reachable": the core then makes places up and a walk that fails marks the place (a *blind* walk). |
| `explore_walk(pos, timeout)` | `WalkResult(ok, detail, position, moved)`. Pathfinding is the engine's; the core only names the place. |
| `explore_look(seconds)` | `LookResult(saw=[{name, area}])`: a pause at a stop, naming what came into view. |
| `explore_study(poi)` / `explore_halt()` | optional: go up to a thing / stop walking now. |

What the policy does: reachable places first (never "pick a point, find out afterwards"), a bonus for places it has not
seen and things it has not studied, a penalty for the cost of the way and for going back where it has been, a little
persistence of heading, a softmax with a temperature and now and then a choice from further down the list (the generator
is injectable: tests seed it). A run lasts `budget` seconds: the interface sets it before every idle beat to the wait to
the next beat plus how long a turn takes (`explore_budget`, `LatencyMeter`; `VESSEL_EXPLORE_MIN_SEC` 20 /
`VESSEL_EXPLORE_MAX_SEC` 180), and chains legs (walk, look 1-4 s, walk) until it is spent, nothing is left to see, the way is
shut (3 failed legs in a row), a threat comes close, or someone interrupts. A connector that tells people things through
the idle beat holds the beat while a walk goes on (`idle_busy`); the walk's own result stands for the next beat.
`Tuning` is per world (`EXPLORE_TUNING`): metres in a room are not blocks on a map.

Skyrim (not built): the same contract. `pois[].area` is the room, so a body can list the objects of a house; `survey`
needs a navmesh query for the reachable set instead of the physics grid RetroXR uses.

## Design constraints

1. **No agentic tasks.** Actions map 1:1 to connector commands.
2. **No diary during a session.** Events accumulate in an in-DB
   `experience_buffer`; a single "lived experience" diary entry is written only
   at end-of-session (explicit logout OR `VESSEL_SESSION_COOLDOWN_SEC` of
   inactivity, default 3600 s).
3. **Own Activities voice.** Like Radio/Grillo, the Vessel logs to
   `vessel_activity_log` and has its own History sub-tab (🌀).

## WebUI coherence LED

A world sub-plugin (e.g. Minecraft) can be enabled while this **core** Vessel
plugin is disabled — a state in which that world can never actually connect. In
that case the Plugins tab shows an **orange** status dot on the world
sub-plugin's card (instead of green), with a tooltip explaining that the world
can't connect until the Rift Vessel plugin is enabled. Enabling this plugin
restores the normal green/grey LED.

## Config keys

| Key | Default | Purpose |
|-----|---------|---------|
| `ACTIVE_VESSEL` | `disabled` | Selected connector, or `disabled` |
| `VESSEL_SETTINGS` | — | JSON per-connector settings |
| `VESSEL_SESSION_COOLDOWN_SEC` | `3600` | Idle seconds before a session closes |

## Adding a connector

Create `plugins/rift_vessel/<world>/<world>.py`, subclass
`VesselConnectorBase`, set a module-level `CONNECTOR_CLASS`, and call
`register_vessel_connector(name, __name__, capabilities=..., label=...)` at
import time. Ship an `icon.svg` and a `guide.md` in the sub-folder. Connectors
are **not** plugins (no `PLUGIN_CLASS`) — the loader imports them so they
self-register, then skips them as plugins.

The connector automatically inherits the Vessel's core action set, exposed as
`vessel_<world>_<verb>`. To add **world-specific** verbs, override
`get_world_actions()` and return a `{verb: schema}` mapping (same shape as a
plugin's `get_supported_actions`, keyed by the bare verb, **without**
`external_effects`); the core plugin namespaces them under `vessel_<world>_` and
dispatches them back to your connector's `act(verb, payload)`.

Full reference: `docs/rift_vessel.rst`.
