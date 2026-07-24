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

| Action | Purpose |
|--------|---------|
| `vessel_say` | Speak/chat inside the current world |
| `vessel_move` | Move the avatar (direction / target) |
| `vessel_look` | Turn / aim the avatar's view |
| `vessel_use` | Interact with a nearby object/entity |
| `vessel_status` | Report the current world state |

## Design constraints

1. **No agentic tasks.** Actions map 1:1 to connector commands.
2. **No diary during a session.** Events accumulate in an in-DB
   `experience_buffer`; a single "lived experience" diary entry is written only
   at end-of-session (explicit logout OR `VESSEL_SESSION_COOLDOWN_SEC` of
   inactivity, default 3600 s).
3. **Own Activities voice.** Like Radio/Grillo, the Vessel logs to
   `vessel_activity_log` and has its own History sub-tab (🌀).

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

Full reference: `docs/rift_vessel.rst`.
