# RetroXR Vessel

Lets Synth step into a **RetroXR** room (the VR retro-gaming room, Godot) through the
**Synthetic Heart Rift Summoner**, an object the RetroXR mod `xenu.synth_rift` adds to
the room. Switch the Summoner on and the mod opens a WebSocket to this server; Synth
moves in the room, speaks, looks at objects and screens, is touched, and can pick things
up or hand them to the player.

**Synth does not play.** No verb writes input to a game console; the connector refuses
anything outside its declared verbs.

## Enabling

1. Turn the **RetroXR Vessel** plugin on (Plugins tab) together with the **Rift Vessel**.
2. In RetroXR, install and enable the `xenu.synth_rift` mod, spawn the Summoner, and
   enter this server's address (and token, if the WebUI has one) in its settings.
3. Switch the Summoner on. Synth enters; switching it off (or leaving the room) ends the
   session and writes the usual single recap.

## Wire protocol (protocol 1)

`/api/vessel/retroxr/ws`, JSON text frames, the same token gate as the WebUI.

| direction | frame |
|---|---|
| mod → Synth | `hello {protocol, room, capabilities, animations}` (first frame) |
| Synth → mod | `welcome {session, assets}` or `error {code, message}` |
| mod → Synth | `event {event_type, summary, actor, salience, data}` |
| mod → Synth | `state {state: {position, holding, extra}}` |
| Synth → mod | `action {id, action, payload}` → mod answers `action_result {id, ok, detail, data}` |
| mod → Synth | `request_assets {animations: [...]}` → Synth answers `assets {assets}` |

`assets` describes the **body**: the active skin's VRM model and the animations the world
named, resolved by name (`animations/vessel/<name>/`, then the Karada state folder, then
the fallback skin). The mod downloads the files from `/skins/...` and uses
`/api/karada/assets/manifest` to skip what it already has. Nothing is pushed unasked.
See `core/embodiment_assets.py`.

## Verbs

Core set: `say`, `move`, `look`, `use`, `follow`, `unfollow`, `status`.
RetroXR: `look_at`, `touch`, `pick_up`, `put_down`, `hand_over`, `sit`, `emote`, `inspect`.
