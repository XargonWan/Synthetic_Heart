# Home Assistant

Synth can see your house and act on it through Home Assistant, and Synth can hand
whole jobs over to HA instead of doing them itself. The first job that works this way is
**image generation**: Synth asks for a picture, Home Assistant renders it on its own GPU,
and the finished file comes back to Synth and gets sent to you in chat.

Nothing about this plugin is required for SyntH to work: with the token empty it loads
disabled, and if Home Assistant is down every action simply returns a clean error.

## Setup

1. **Create a long-lived token** in Home Assistant: your profile (bottom left) ->
   Security -> Long-lived access tokens -> Create token. Copy it once.
2. **Paste it into *Home Assistant Token*** below. It is stored masked and never logged.
   The plugin stays disabled while this field is empty.
3. **Check *Home Assistant URL*** (default `http://192.168.1.25:8123`).
4. **Pick what Synth may see** in *House State Entities*: entity ids or wildcards,
   comma-separated (`sensor.*_temperature, light.*, person.*`). That selection becomes one
   short line inside every prompt, e.g. *living room 21.4 C, 3 lights on, nobody home*.
5. **Decide what Synth may touch** in *Allowed Service Domains* / *Denied Service
   Domains*. Deny always wins. Locks, alarm panels, cameras, shell commands and the HA
   service domains are denied out of the box.
6. **For images**, create the script on the Home Assistant side (see below), what it
   calls, and list the styles in *Image Styles*, e.g.
   `quick=ai_task.comfyui_quick, hires=ai_task.comfyui_hires`.

## Images

Synth never knows which engine draws the picture. It calls one script
(*Image Script*, default `script.synth_image`); that script runs HA's own
`ai_task.generate_image` against whichever image entity you mapped to the style name. So
swapping engines, models or workflows later is a Home Assistant change, not a Synth one.

The generated file comes back through Home Assistant, lands in *Media Cache
Directory* (`res/hass_cache/images/`) and is then sent like any other attachment.

**Editing is not available yet.** The Home Assistant image component that drives ComfyUI
cannot take a reference image at the moment, so `hass_image_edit` politely refuses and
Synth will tell you so. The moment that is fixed on the HA side, switch *Enable Image
Editing* on.

## Beats

Synth's own autonomous beats (Grillo: observer, dream, weekly review...) can see the house too, but
that is **your decision, off by default**, and beats never get write access:

| Setting | Default | What it does |
|---|---|---|
| *Inject House State On Beats* | off | Put the house-state line into beat prompts as well |
| *Beats That Get The House State* | empty | Which beat families qualify (`observer, dream, ...`); empty means all of them. Wildcards allowed |
| *Beat House State Max Characters* | 600 | Tighter cap for beats (0 = use the conversation cap) |
| *Allow House Actions On Beats* | off | Lets a beat actually change the house |

With *Allow House Actions On Beats* off, a beat may read the house (`hass_status`, `hass_states`,
`hass_state`, `hass_history`, `hass_template`, `hass_snapshot`, `hass_image_styles`, `hass_watch`) but
every house-changing action is refused at execution time (`hass_call_service`, `hass_run_script`,
`hass_automation`, `hass_notify`, `hass_image_generate`, `hass_image_edit`), no matter what the beat
prompt happens to offer her. So the worst case is that she notices something and mentions it; she
cannot act on it until you tell her to.

## Weather and the house location

Synth's weather line and her idea of where the house is can now come from Home Assistant instead of the
local wttr.in block and the configured location string. While these are on, the old sources are **dropped
for that turn** rather than shown alongside, so she is never told two different things. Switch them off, or
disable this plugin, and the old blocks come back exactly as they were.

| Setting | Default | What it does |
|---|---|---|
| *Inject Weather From HA* | on | Weather line built from HA's own weather entity |
| *Home Assistant Weather Entity* | empty | Which `weather.*` entity to read; empty means the first one HA reports |
| *Weather Forecast Hours* | 6 | Hours of hourly forecast appended (0 = none) |
| *Inject House Location From HA* | on | House coordinates, country, timezone and elevation |

The weather line carries the condition, temperature, humidity, cloud cover, wind with direction, pressure
and UV index, plus today's sunrise/sunset and the forecast. The forecast comes from HA's
`weather.get_forecasts`, fetched in the background and cached for 30 minutes, so a turn never waits on it.
The location block replaces the anchor's `Current Location` line, which is why the configured
`PROMPT_LOCATION` string stops appearing while this is on.

Both ride **every** route - ordinary chat, autonomous beats and live sessions - because they replace blocks
Synth already received on all of them. Only the house-state entity snapshot is behind *Inject House State On
Beats*. With the weather switch off (or HA unreachable) the old wttr.in line comes back untouched; likewise
the location string comes back if you switch *Inject House Location From HA* off. The log names the
substitution on each turn:

```
[action_parser] plugin block 'home_weather' supersedes 'weather' at gather time
[action_parser] plugin block 'home_location' supersedes 'location' at gather time
```

## Actions

| Action | What it does |
|---|---|
| `hass_status` | Link state, HA version, entity count, image styles, images left today |
| `hass_states` | List entities (filter by domain / search text / limit) |
| `hass_state` | One entity in full: state, attributes, last changed |
| `hass_history` | State changes of one entity over the last N hours |
| `hass_call_service` | Control the house: `light.turn_on`, `climate.set_temperature`, ... |
| `hass_run_script` | Run a script by name, with variables, and return its response |
| `hass_automation` | Trigger / list / enable / disable automations and scripts |
| `hass_template` | Render a Jinja template inside HA and return the text |
| `hass_notify` | Notify a phone (or the HA UI), optionally with an image |
| `hass_snapshot` | Save a camera still locally and return its path |
| `hass_image_styles` | List the image styles HA can render |
| `hass_image_generate` | Draw a picture and return the local file path |
| `hass_image_edit` | Edit a picture from a reference image (refused until HA supports it) |
| `hass_watch` | Add / remove / list entities watched in the house-state line |

Every one of them declares external effects, so these calls always run on the **agent
route**: one agent turn can read the house, decide, act and report. They never appear in
the ordinary chat catalogue.

## The Home Assistant side

One script is what makes image generation work. Create it under
*Settings -> Automations & scenes -> Scripts -> Create*, or in `scripts.yaml`:

```yaml
synth_image:
  alias: "Synth: generate an image"
  fields:
    prompt:
      required: true
      selector:
        text:
          multiline: true
    style:
      required: false
      selector:
        text:
  sequence:
    - variables:
        entity: >-
          {% set map = {
            'quick': 'ai_task.comfyui_quick',
            'hires': 'ai_task.comfyui_hires'
          } %}
          {{ map.get(style, 'ai_task.comfyui_quick') }}
    - action: ai_task.generate_image
      data:
        task_name: "synth image"
        instructions: "{{ prompt }}"
        entity_id: "{{ entity }}"
      response_variable: synth_image_result
    - stop: "image ready"
      response_variable: synth_image_result
  mode: parallel
  max: 3
```

`ai_task.generate_image` is Home Assistant's own image action; the image entity behind it
is the ComfyUI integration (`custom_components/comfyui_generator`, MIT licensed, installed
through HACS), pointed at a ComfyUI instance on the GPU host.

For anything else Synth should be able to do house-wide, add a script here rather than a
long chain of service calls: the logic stays in Home Assistant, and Synth just asks for
the outcome by name.

## Notes

* **Media cache**: generated images and snapshots live in `res/hass_cache/` inside the
  SyntH checkout. That folder is inside the outbound media roots, which is what makes them
  sendable; the newest *Keep Newest Files* per subfolder are kept.
* **Daily cap**: *Image Generation Daily Cap* bounds how many pictures Synth may make per
  day (counted across restarts). `0` means unlimited.
* **Token scope**: a Home Assistant long-lived token is admin on your instance. The plugin
  keeps the token masked, never logs it, and enforces your allow/deny lists, but those
  lists are the real boundary - not the token.
* **Reconnecting**: the WebSocket link reconnects on demand with a short backoff. If HA
  restarts, the next action re-establishes it.

## Troubleshooting

* *"HASS_TOKEN is not configured"* - paste the token in the field above.
* *"Home Assistant rejected the token"* - the token was revoked or mistyped; create a new one.
* *Every `/api/` request answers 500 on your instance* - that is Home Assistant's
  login-ban middleware failing on a malformed reverse-DNS answer from your router, not a
  token problem; authenticated requests (which is all this plugin makes) work normally.
* *Image action errors* - run `hass_status` first, then check that ComfyUI is up on the
  GPU host and that the `synth_image` script runs from *Developer tools -> Actions*.
* *The picture never arrives* - look in `res/hass_cache/images/`; the file is there even if
  the send failed.
