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
| mod → Synth | `state {state: {position, holding, extra}}`; `extra.body = {settled, busy, speaking}` says whether her body is done with what it was told to do (sent at once when `settled` changes; a mod that omits it counts as settled); `extra.following` says she is following the player (it is in her surroundings and in `WorldState.flags`) |
| Synth → mod | `action {id, action, payload}` → mod answers `action_result {id, ok, detail, data}` |
| mod → Synth | `request_assets {animations: [...]}` → Synth answers `assets {assets}` |
| mod → Synth | `audio {id, format, data, actor}` (the player's voice, base64, `wav`/`ogg`/`mp3`/`flac`, 8 MB max) → Synth answers `transcript {id, ok, text, reason}` |

`assets` carries `name`, what the current persona is called (absent with no persona; the mod's chat labels
their lines with it), and describes the **body**: the active skin's VRM model and the animations the world
named, resolved by name (`animations/vessel/<name>/`, then the Karada state folder, then
the fallback skin). The mod downloads the files from `/skins/...` and uses
`/api/karada/assets/manifest` to skip what it already has. Nothing is pushed unasked.
See `plugins/rift_vessel/embodiment_assets.py`.

## Hearing the player

The chat page's mic records the player and sends one `audio` frame. The connector hands the clip to **Auris**
(`auris_plugin.transcribe_audio`, whatever engine the player chose), answers the mod with `transcript` (`ok: false` and a
`reason` of `no_stt` / `nothing_heard` / `too_long` / `stt_failed` / `bad_clip` when there are no words, so a press is
never answered with silence) and then dispatches the text as an ordinary `chat` event (`actor` from the frame, `data.via =
"voice"`): salience, priority and history treat it exactly like typed text. Transcribing never blocks the read loop.

## Ambient events do not start a turn

`sighting`, `proximity`, `status`... (`EPHEMERAL_EVENT_TYPES`) are recorded and enter the perception ring, and ride along
with the NEXT real prompt (a chat, a routine's `*_done`, `summoned`, an idle beat). They never start an LLM turn of their
own (`VESSEL_AMBIENT_TRIGGERS_TURN`, default off; on restores the old ~15 s batched turn). The latency from a player's
chat to her answer is logged (`[retroxr_connector] answered the player after N s`, with the queue depth on arrival).

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

She is never scripted. `uses_idle_beat` makes the Vessel send an **idle beat** (2026-11-05, "the puppet" fix):

- **When it starts.** Not before she is out of the capsule and her eyes have reported (`idle_ready_since()`: the
  mod's `out` frame, or 45 s after a resumed session attached; `describe_surroundings()` is `None` until the first
  `state`). The first beat used to fire in the second of the connect, with "you cannot see the player, there is
  nothing near you", and was wasted.
- **Counted from the end, not the start.** The wait is measured from the latest of: the beat's start, her arrival, the end
  of the beat's turn (the engine free again) and the moment her body was last `settled` (`idle_anchor`, pure). It used to
  count from the start of the beat, so a turn longer than the interval was followed at once, plus a fixed 12 s rest.
- **Her body is waited for, not talked over** (`idle_body_eta`): a beat is not asked while a clip or a voice plays, unless
  the body will be done before the model could answer (`lead`, below). A body busy for over a minute is taken as stuck.
  A walk (`explore`) still holds the beat outright: its result is the next prompt. The queue is not piled on either: no
  beat while a turn of this world already waits in it.
- **Auto pacing** (`auto_pacing`, pure; `VESSEL_IDLE_BEAT_ACTIVE_GAP_SEC` = `auto` by default, or a number of seconds).
  After a beat that ended in an action the wait is worked out from the measured turns (`core/stats_registry.py`): the
  engine is busy with her turns ~70% of the time, so `wait = p50(turn) x (1/0.7 - 1)` (40 s turn: ~17 s; 15 s: ~6 s), never
  under 3 s nor over `VESSEL_IDLE_BEAT_INTERVAL_SEC`, smoothed so a noisy turn does not make it jump. `lead` = the median
  time from the ask to her first verb: a beat may go out that much before her body is done. With fewer than 5 recent turns
  it is 10 s. `VESSEL_TICK_SEC` (`auto` or seconds) is how often the scheduler looks: in `auto` it sleeps until the next beat
  is due and looks every 1.5 s while it waits on the engine, the body or the queue. The Logs > Stats tab shows the measured
  phases (`beat.*`), and what the pacing decided now; the vessel registers that block itself (`core/vessel_stats.py`).
  Both settings are changed in the Vessel's settings (advanced), not on the Stats page. Each beat also logs one `[beat-timing]` line.
- **How often** (`adaptive_idle_interval`, pure): `VESSEL_IDLE_BEAT_ARRIVAL_INTERVAL_SEC` (20 s) for the first
  `VESSEL_IDLE_BEAT_ARRIVAL_WINDOW_SEC` (180 s), then `VESSEL_IDLE_BEAT_INTERVAL_SEC` (40 s); after three beats in a
  row that ended in no action it stretches x1.5 per beat up to `VESSEL_IDLE_BEAT_MAX_INTERVAL_SEC` (180 s); a
  `sighting` (something changed in the room) brings it back to the arrival pace. Held while a player spoke in the last
  `VESSEL_IDLE_BEAT_QUIET_SEC` (45 s) and until the engine has been free `VESSEL_IDLE_BEAT_ENGINE_REST_SEC` (12 s;
  it was a whole interval, which a long turn kept from ever happening). **Trade-off: a chat that arrives while a beat's
  turn is running waits for that turn (about one LLM turn).**
- **What she is told** (facts, never orders): the connector's description, what changed since last time, the last five
  things she did (`idle_journal()`: her verbs and her routines' `*_done`) and for how long she has stood still, and the
  aim she set herself. Staying put is one choice among the others; what she does is hers and unasked.
- **Her own aims** (`set_goal` / `update_goal`, `LOCAL_VERBS`): Synth's, never sent to the mod, kept by the generic Goals
  plugin under scope `vessel` / `retroxr` / `none` (one house, one set of aims; the WebUI Goals view lists them). One
  active at a time; `idle_intention()` hands it back to the next beat. There is no goal expander, debrief or
  "you MUST set a goal" here, on purpose.
- **Every reason a beat did not start is logged at INFO** (`Idle beat held (not_ready | engine_busy | player_active |
  last_beat_running | nothing_to_describe)`, once a minute per reason) and so is the outcome of each
  (`Idle beat outcome ...: acted (...) | did nothing (streak N) | cut short by the player`). A silent puppet should
  never again need a debugger.

It is an ordinary vessel perception: **AMBIENT (4), above background work (G.R.I.L.L.O. 2) and below every human; a
player's chat is HIGH (8), prunes pending ones and postpones the next** (`tests/test_vessel_retroxr_body.py` pins all of
this). A world with `uses_idle_beat` gets no will, action, goal or reflection beat: those push a bot toward a goal.

## `explore` (2026-11-05)

The core's walk, not the mod's: `explore` is a Synth-side verb (`LOCAL_VERBS`, like `set_goal`), the mod no longer has one
to offer. The connector asks the mod two internal verbs that the model is never offered and never accepted from it
(`INTERNAL_VERBS`, declared in `hello.verbs`): **`survey`** (what can be walked to, with the length of each way, which of
the viewpoints has a line of sight to each place, the things in sight as places to go up to, and what to keep clear of) and
**`look_around`** (turn on the spot for N seconds and name what came into view). Legs are the ordinary `move` with `free: true`:
no 6 m leash from the Summoner, the floor under every cell checked instead (a cell with no floor within 0.3 m is a hole),
the player kept 0.9 m off (less if they already stand nearer) and anything rolling 0.7 m, the way ahead checked every 0.4 s for
2 m and planned round before they reach it. A mod without `survey` gives a blind walk (places made up by the core). `stop`
now also ends a walk in progress. A person talking interrupts a walk at its next safe point; a dropped rift cancels it
quietly. Budget: the interface sets it before each idle beat (`set_explore_budget`).

## Verbs

Core set: `say`, `move`, `look`, `use`, `follow`, `unfollow`, `status`, and `combine` (an **opt-in** core verb: a world gets it only by
listing it in its connector's `core_verbs`, see `docs/rift_vessel.rst`).
Her own aims (Synth-side, not the mod's): `set_goal`, `update_goal`.
Her walks (Synth-side, the core's): `explore`.
RetroXR: `look_at`, `touch`, `pick_up`, `put_down`, `hand_over`, `sit`, `emote`, `inspect`, `operate`, and the ones she does ON HER OWN
once asked: `search`, `explore`, `watch`, `bring`, `stop`.

**Hands.** She holds one thing at a time, in both hands.
- `put_down {target?}`: without a target it goes where her hands are (a table under them, else the floor); with a target (a table, a
  shelf) she walks up and sets it on the flat top of that thing. The answer says where it ended up ("on the Desk", "on the floor"), or why
  not (no flat top, nothing of that name) and that she still holds it.
- `hand_over {seconds?, say?}` / `bring {target, seconds?, say?}`: she goes to the player and holds the thing out in both hands (the
  `give` clip, `tools/make_give_clip.py`) for `seconds` (the model's choice, 3 to 60; 30 if none or not a number). `say` is what she says
  as she does it: the connector speaks it through the same path as a `say` of hers and does not send it to the mod. **If the player does not take it she is
  not made to put it down**: the result says they did not and that she still holds it, and what to do (keep it, set it on the table, try
  again) is hers to decide in the turn that result starts.
- `combine {target, item?, how?}`: one thing into another, as a cartridge into a console (`item` defaults to what she holds; a loose one is
  fetched first). Reported as `combine_done`. Only the machine slot is built; anything else is answered with what can be combined.
- `operate {interaction, target?, with?}`: switch a TV or a console on or off, volume, channel, rewind, eject, a cartridge in. Never a game.

**Following.** `follow` is always the player (any `target` is ignored); `unfollow`, `stop`, or a walk of her own ends it, and a routine she is
asked to do pauses it and she goes on after. When she cannot keep up she is told once, as an event `follow_lost {reason}`: `too_far` (the player is beyond
how far she may go from where she came in) or `blocked` (no way to them). She is still following then.

**Offered to her is what the mod declared**: `touch` and `sit` are in the schema but the mod does not build them, so she is not offered them.

### What she sees, and what she is told afterwards

The `state` frame (and so `Nearby entities` in her prompt, which prints each entity's `name`) lists only what a person in front of her would
see: about 180 degrees, within ~6 m, not hidden. Each entity's `name` is what a person would call it ("Game Boy Color", "VHS tape 2",
`unknown item`: never empty or an engine name), `type` its kind, `powered`/`held` when they apply; `id` is the node's own name. She can
name only what she has seen: for the rest she `search`es.

`search`, `explore`, `watch`, `bring`, `pick_up`, `hand_over` return **at once** ("looking for X: you will be told what you find") and the mod
sends the result later as an event (`search_done`, `explore_done`, `watch_done`, `pick_up_done`, `put_down_done`, `hand_over_done`, `bring_done`,
`combine_done`, `operate_done`, with
`data.ok`), which is a turn of its own (they are not in `EPHEMERAL_EVENT_TYPES`). Meanwhile she is not asked anything. A `move` or
`follow` replaces what she is doing.

**Pictures.** `watch_done` can carry `data.image_jpeg_b64` (what her eyes took of the thing). The connector turns it into
`data.attachments` (`{mime_type, data, filename}`); `interface/vessel_interface.py` hands attachments to the model with that turn and keeps
them out of the activity log and the experience record. The player can switch pictures off in the Summoner's menu.
