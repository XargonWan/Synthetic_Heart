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
See `plugins/rift_vessel/embodiment_assets.py`.

## Being summoned

The player summons Synth by touching the capsule's screen. Once she has stepped out, the mod
sends a `state` frame (every nearby object with `bearing`, `distance`, `side`; the player is
one of them, `type: "player"`, their head being the camera) and then one `event` of type
`summoned`, salience 1.0. Its summary says what she can see in front of her and where the
player is; `data.seen` carries the same list. `summoned` is NOT ambient telemetry: it is not in
`EPHEMERAL_EVENT_TYPES`, so it becomes its own turn (`tests/test_vessel_retroxr.py` pins that).

## Her body follows Karada, and she speaks

While a rift is open it is registered as a **Karada transport** (`plugins/rift_vessel/karada_bridge.py`, shared by every rift). Everything Karada
says reaches the mod as `{"type": "karada", "kind": ..., "payload": ...}` with Karada's own payload:

| kind | payload | what the mod does |
|---|---|---|
| `animation` | `vrm_animation_v2`: `state` (idle/think/write/talk/touch), `descriptor`, `started_at` | plays that state's clip of her skin |
| `audio` | `tts-play`: `url`, `audio_duration_s`, `lipsync`, `text` | downloads `url` from the same server and plays it at her head |
| `face` / `expression` / `model` | Karada's | (the face is not drawn yet) |

The current state is sent once, right after `welcome` (`announce_state`). **In RetroXR a `say` is always
spoken**: the connector forces `audio: true`, synthesises the line with Vox (`generate_only`) and hands the
clip to Karada, which fans it out (WebUI and rift alike) and drives her TALK animation for its length. No
voice engine: the line stays text.

## When nobody is talking to her

She is never scripted. `uses_idle_beat` makes the Vessel send an **idle beat** every
`VESSEL_IDLE_BEAT_INTERVAL_SEC` (90 s) once no player has spoken for `VESSEL_IDLE_BEAT_QUIET_SEC` (45 s):
the connector's `describe_surroundings()` (the player and the objects near her, where, whether a machine is
on, what she holds, which animations she has) and the question *what do you want to do?*, with doing nothing
as an explicit answer. She decides. It is an ordinary vessel perception: **AMBIENT (4), above background work
(G.R.I.L.L.O. 2) and below every human; a player's chat is HIGH (8), prunes pending ones and postpones the
next** (`tests/test_vessel_retroxr_body.py` pins all of this). A world with `uses_idle_beat` gets no will,
action, goal or reflection beat: those push a bot toward a goal.

## Verbs

Core set: `say`, `move`, `look`, `use`, `follow`, `unfollow`, `status`.
RetroXR: `look_at`, `touch`, `pick_up`, `put_down`, `hand_over`, `sit`, `emote`, `inspect`.
