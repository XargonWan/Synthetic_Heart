# Synth <-> Home Assistant bridge

**Status:** PLAN ONLY. Nothing implemented, no repo file touched except this document.
**Written:** 2026-09-21, against the live HA instance at `http://192.168.1.25:8123` (version **2026.8.3**) and the Synth checkout `D:\dev\B17\synthetic_heart`.
**Implementer:** Hermes. Sandro owns the HA-side UI steps, the ComfyUI host and the token.

**Decisions already taken (2026-09-21):**

1. Image engine: **local ComfyUI only**, on the GPU he can give it. No cloud provider in v1.
2. **Image editing is not in v1** (the ComfyUI HA component cannot take a reference image yet).
   Section 5.6 documents the exact fork that adds it, and the action ships gated off until then.
3. Scope of this pass: **Synth -> HA only** (Phases 0-2). HA -> Synth (Phase 3) and Synth-in-HA
   (Phase 4) are documented and deferred, not cancelled.
4. The HA **REST API 500** is **diagnosed** (section 2): it only hits unauthenticated requests and is
   caused by the router answering private PTR queries with garbage, via AdGuard. Fix options in
   Phase 0.1. It does not block this plan.

---

## 0. Progress (2026-09-21)

| Item | State |
|---|---|
| HA REST 500 | **Diagnosed and fixed enough**: it was the unauthenticated path only (router PTR garbage -> `ban.py` -> `UnicodeDecodeError`). AdGuard no longer forwards private PTRs; bug report ready in `hass-ban-ptr-bug-report.md`. The HA container still resolves via Docker's embedded DNS to the router, so its own 401s still 500 until the container gets `--dns=192.168.1.86` (Sandro's Unraid template edit). Authenticated API calls, which is all this plugin makes, are unaffected. |
| HA token | Created, verified against the live instance, stored in Synth's runtime config as `HASS_TOKEN` (masked). **Rotate it when convenient**: it was pasted into a chat session. |
| Phase 1 plugin | **Written and live-verified** at `plugins/home_assistant/` (1600 lines + `guide.md` + `icon.svg`): WS connect/auth, house-state injection, 13 `hass_*` actions, allow/deny policy, media sandbox, image quota. Verified against the real instance: states, templates (including template errors), history, service calls (deny list and missing service both clean errors), notify (created and dismissed), snapshot error path, media upload + download round trip, quota. |
| Phase 0.4-0.7 (ComfyUI + script) | **Not started.** HA currently has no `ai_task` entity and no scripts, so image generation has nothing to call yet. |
| Phase 1 tests | Not written yet (live verification done instead). Narrow pytest file for the policy/quota/injection decisions is the next unit of work. |
| Beat awareness (read-only) | **Done and verified.** Beats turned out to flow through the same chain hook (a beat enqueues its prompt as a synthetic message carrying `grillo_beat`/`beat_type`), so the block was already reaching them whenever conversation awareness was on. Now it is its own opt-in: `HASS_BEAT_AWARENESS_ENABLED` (default off), an optional per-beat-family allowlist, a tighter char cap, and a `HASS_BEAT_ACTIONS_ALLOWED` switch (default off) that makes beats read-only by refusing house-changing actions at execution time. Verified with a 9-case matrix: beat+off -> no block, chat -> block, one-arg call -> block, beat+on -> block, non-matching family -> no block, matching family -> block, beat service call / notify / image generation refused, beat reads allowed, chat calls unaffected, and allowed-on-beats passes through. |
| House-state delivery fix (2026-09-21) | **Fixed.** A second agent flagged (and I verified independently) that the injected block never reached a prompt: `get_static_injection()` output is merged into `context_section`, and only keys a renderer consumes by name are visible, so `home` was built, merged and dropped. `core/prompt_engine.py` now renders a declared `_PLUGIN_CONTEXT_BLOCKS` table in BOTH renderers (`_build_context_summary` for chat and beats, `build_live_prompt_request` for live sessions), and `build_prompt_request` logs once per process at WARNING for any injected key no renderer consumes. Pinned in `tests/test_plugin_context_blocks.py` (5 tests). The detector also revealed three pre-existing instances left untouched on purpose: `upcoming_events` and `facial_expression_guidance` are injected and consumed nowhere, and `weather` renders only on the live route. |
| Weather + house location from HA (2026-09-21) | **Done and verified live.** The plugin now injects `home_weather` (HA's met.no entity: condition, temperature, humidity, cloud cover, wind, pressure, UV, sun times, 6-hour hourly forecast cached for 30 min) and `home_location` (house coordinates, country, timezone, elevation), each with its own switch. The block table in `core/prompt_engine.py` now carries the legacy key each block supersedes (`weather`, `location`) and drops it when the block is present, in both prompt builders, so `wttr.in` and `PROMPT_LOCATION` are replaced rather than duplicated while HA supplies them, and untouched when it does not. Verified: `[Weather] cloudy, 25.5°C, humidity 54%, cloud 96.9%, UV 3.1, pressure 1020hPa, wind 28.1km/h from NE / sun above horizon, sunrise 06:52, sunset 19:04 / Next hours: 16:00 partlycloudy 25.4°, ...` and `[House] Home at 45.4723, 13.6732, SI, timezone Europe/Budapest`; with the switch off the old text stays. Note for him: the old plugin's `PROMPT_LOCATION` says "Ljubljana" while HA's house is at 45.4723, 13.6732 (and HA's timezone is Europe/Budapest), and the old plugin still fetches wttr.in for its daily announcement. |
| Live deployment | The running instance **hot-picked the plugin up without a restart** (its plugin scan registered `home_assistant` with all 14 actions at 13:27), connected at 15:23:15 after the restart Sandro did, and its injection has fired on every turn since. The house line does not appear in a prompt until the prompt-engine fix is loaded, i.e. after the next restart. |

Facts learned from the live instance that shape the work: HA 2026.8.3 with **82 entities**, no
`ai_task` entity and no scripts yet, two cameras (both `unavailable`), time zone Europe/Budapest.
`render_template` **streams its answer as an event frame** on the same message id (a plain result
read returns None), and the media upload endpoint **rejects any content type that is not
image/video/audio**. Both are handled in the plugin.

---

## 1. Summary

One bridge, one direction to start with.

**Synth gets Home Assistant as a capability bus.** It reads entity state, calls services, runs
scripts and automations, and **delegates whole features to HA instead of growing engines inside
Synth**. The first delegated feature is image generation.

The flagship, and the reason HA is worth having as a bridge at all: **you ask Synth for a picture,
and it arrives as an image message in the chat you already use.** Synth implements no diffusion. It
calls one HA script over the HA WebSocket API, that script runs Home Assistant's own
`ai_task.generate_image` through the HACS ComfyUI component (MIT) on the GPU host, and Synth pulls
the produced file into its own media tree and sends it through the ordinary `send_message` path.

Because Synth only ever knows a script name and a style name, adding another engine or another
workflow later is an HA-side change with zero Synth code and zero redeploy.

---

## 2. Verified facts (probes run 2026-09-21)

Everything below was measured, not assumed.

| Fact | Evidence |
|---|---|
| HA version 2026.8.3, reachable at `192.168.1.25:8123` | WebSocket handshake first frame: `{"type": "auth_required", "ha_version": "2026.8.3"}` |
| **The HA WebSocket API works** | same handshake completes; `call_service` accepts `"return_response": true`; `subscribe_events` available (HA dev docs) |
| **The HA REST API 500 is DIAGNOSED (it is the UNAUTHORIZED path only)** | `GET /api/`, `/api/config`, `/api/states` return **HTTP 500** `Server got itself in trouble` when the request is unauthenticated, while `/auth/providers`, `/manifest.json`, `/` and the WS API are fine. Cause, proven: HA 2026.8.3 `components/http/ban.py` wraps auth failures in a middleware that calls `process_wrong_login()` -> `gethostbyaddr(request.remote)` inside `with suppress(herror)`; a `UnicodeDecodeError` is NOT suppressed and escapes the middleware, so every 401 becomes a 500. The decode fails because **the router `192.168.1.1` answers private PTR queries with a malformed 4-byte name** (`40 8A ED BE`): AdGuard forwards private reverse lookups to `local_ptr_upstreams: [192.168.1.1, 192.168.1.11]` and relays the garbage. Evidence: `socket.gethostbyaddr('192.168.1.69')` inside the HA container raises `'utf-8' codec can't decode byte 0x8a in position 1`; AdGuard's own query log for `69.1.168.192.in-addr.arpa` carries the raw answer `VVeAAAABAAEAAAAAAjY5ATEDMTY4AzE5Mgdpbi1hZGRyBGFycGEAAAwAAcAMAAwAAWqwLTYABgRAiu2+AA==` with `"Upstream":"192.168.1.1:53"`, which decodes to a PTR whose target is the single 4-byte label `40 8a ed be`. **Consequence: with a valid token the REST API works normally** (no 401, no lookup, no 500), so the media-upload step in this plan is not blocked. A wrong token, or an expired signed media URL, will show up as a 500 instead of a 401. |
| Nothing else is listening on the HA host | port scan of `192.168.1.25`: only `8123`. No SSH (22), no Samba (445), no ComfyUI (8188), no Ollama (11434), no SD-WebUI (7860). |
| This VM sits on the same /24 | Ethernet `192.168.1.69` (the Tailscale adapter only holds a link-local `169.254.x`, so it is not up) |
| A Gemini key exists in the checkout | `GEMINI_API_KEY` in `D:\dev\B17\synthetic_heart\.env` (value not read). Unused by this plan. |
| Synth has **no** image generation today | zero hits for `comfyui`, `stable.diffusion`, `generate_image`, `image_generation` in the tracked tree |
| Synth's outbound media sandbox = the checkout + its logs | `core.outbound_file_utils.allowed_file_roots()` -> `['D:\dev\B17\synthetic_heart', 'D:\dev\B17\synthetic_heart\logs']`. **Anything Synth saves inside the checkout tree is sendable with no sandbox change.** |
| HA stores AI-generated images in its media tree | HA core `ai_task/media_source.py` -> `<first media dir>/ai_task/images/`, media source `media-source://ai_task/images/<file>`; the action returns `media_source_id`, `url` (pre-signed, 1 h), `revised_prompt`, `model`, `mime_type`, `width`, `height` |
| ComfyUI HA component (v0.5.0, MIT) works the way we need | it POSTs the workflow to ComfyUI `/prompt`, waits on the ComfyUI WebSocket, then GETs `/history/{prompt_id}` and `/view?filename=...`; falls back to polling `/history` every 0.5 s (backoff to 5 s) bounded by the configured timeout (default 120 s); returns the image bytes to HA, which stores and serves them |
| The component's `img2img` support is **scaffolding only** | `const.py` defines `CONF_INPUT_IMAGE_NODE_ID`, `CONF_DENOISE_NODE_ID`, `DEFAULT_DENOISE = 0.75`, but nothing in `ai_task.py` reads them and the config flow does not expose them. `_async_generate_image(self, task, chat_log)` receives HA's `UserMessage` in that second slot and ignores it, which is exactly where attachments arrive (HA's own Gemini provider reads `user_message.attachments` there). |
| Which core `ai_task` providers can generate and edit | `google_generative_ai_conversation`: `GENERATE_IMAGE` **and** consumes `attachments` (edits images); `openai_conversation`: `GENERATE_IMAGE`, no attachments; `anthropic`, `ollama`: `GENERATE_DATA` only |
| Synth's patterns to copy | `plugins/agpeer/` (external service over HTTP, masked token in config, action schemas with `security_level` + `external_effects`, `guide.md`, `icon.svg`), `plugins/weather_plugin/weather_plugin.py` (prompt injection), `interface/vessel_interface.py` (inbound events into `core.message_queue.enqueue`), `core/karada_api.py` (token-gated external REST surface), `core/variables_engine.register_exposed_var` (WebUI config keys) |

---

## 3. Repos and licences (the "don't reinvent the wheel" answer)

Synth is **GPL-3.0** (`LICENSE`) plus `LICENSE_ADDENDUM.md`. Nothing below creates a licence
conflict: HTTP conversation with a GPL program is not derivation, MIT code is forkable, and the
Apache-2.0 code is consumed unmodified.

| Repo | Licence | What it gives us | Verdict |
|---|---|---|---|
| `comfyanonymous/ComfyUI` | **GPL-3.0** | the actual image engine, on the GPU host | **Run as a separate service over HTTP.** Never vendored into Synth, so no GPL entanglement (and no diffusion engine inside a repo whose architecture is "optional components must be detachable"). |
| [`Incipiens/ComfyUI-Home-Assistant`](https://github.com/Incipiens/ComfyUI-Home-Assistant) | **MIT** | HACS component exposing ComfyUI as an `ai_task` entity (`GENERATE_IMAGE`), WebSocket progress, automatic node detection, reconfiguration, per-workflow config entries. Author-tested against HA 2026.2 and 2026.3b1. | **Use as-is.** This is the wheel: generation works today. |
| `home-assistant/core` (`ai_task` building block) | Apache-2.0 | `ai_task.generate_image`, the media-dir storage, the response shape | **Use as-is.** |
| [`homeassistant-ai/ha-mcp`](https://github.com/homeassistant-ai/ha-mcp) | **MIT** | 4.8k stars, 87 HA tools, installs as an in-process HA custom component exposing MCP over a webhook URL or port 9584 | Optional additive path (7.6), default off. |
| [`ganhammar/hass-mcp-server`](https://github.com/ganhammar/hass-mcp-server) | MIT | in-HA MCP server custom component | Alternative to the above; install only one. |
| [`allenporter/mcp-server-home-assistant`](https://github.com/allenporter/mcp-server-home-assistant) | Apache-2.0 | MCP server by an HA core developer | Backup option. |
| Brand assets | n/a | `LICENSE_EXTERNAL.md` + AGENTS.md section 4: a third-party logo may be committed only if that owner's policy permits it, unmodified, referring only to them | HA's trademark policy does not clearly grant that for a Synth component icon, so ship an **original glyph** (`scripts/generate_component_icons.py`). No HA logo, no ComfyUI logo. |

Deliberately not used: any cloud image provider in v1 (he chose local), any HA add-on, any diffusion
stack inside Synth.

---

## 4. Architecture

### 4.1 Layers

```
        GPU host (LXC/VM on the hypervisor, NVIDIA passthrough)
        +-----------------------------------------------------------+
        |  ComfyUI  :8188   POST /prompt  GET /history  GET /view     |
        |           WS /ws  POST /upload/image   (fork-only path)     |
        |  checkpoints + the workflows he wants, exported API format  |
        +-----------------------------------------------------------+
                ^  HTTP from HA only
                |
        Home Assistant 2026.8.3  (192.168.1.25:8123)
        +-----------------------------------------------------------+
        |  custom_components/comfyui_generator (HACS, MIT)            |
        |     one config entry per style = one ai_task entity         |
        |  scripts: script.synth_image   (+ script.synth_goodnight...)|
        |  ai_task images land in <media dir>/ai_task/images/         |
        +-----------------------------------------------------------+
              ^   WS API: call_service +return_response,
              |   subscribe_events, get_states
              |   HTTP only for image download (/media/... or signed url)
              |
   +----------+--------------------------------------------------------------+
   |  SYNTH  (D:\dev\B17\synthetic_heart)                                    |
   |                                                                         |
   |  plugins/home_assistant/    Synth -> HA: awareness + actions            |
   |     - aiohttp WS client (long-lived, reconnect with backoff)            |
   |     - bounded house-state block injected into the prompt                |
   |     - hass_* actions (security_level + external_effects => Agent Lane)   |
   |     - image bridge: call script, download, hand to send_message         |
   |                                                                         |
   |  media: res/hass_cache/...   (inside the allowed outbound roots)         |
   +-------------------------------------------------------------------------+
```

Deferred (documented, not built in this pass): `interface/hass_interface/` for HA -> Synth
(section 9), Synth entities in HA (section 10).

### 4.2 Transport decision: WebSocket first

* WS is proven working on his instance; REST is 500-ing. Building on REST would put the project
  behind a bug we cannot fix from this VM.
* WS gives `call_service` with `return_response` (the image result comes back in one round trip),
  `subscribe_events` (live state for the awareness block), `get_states`, and one long-lived
  connection instead of an auth handshake per call.
* REST/HTTP is used for exactly one thing in v1: fetching the finished image bytes.

### 4.3 The bridge rule that keeps Synth thin

Synth calls **one script name** (`HASS_IMAGE_SCRIPT`, default `script.synth_image`) with a prompt
and an optional style name. Inside HA, the script routes to the `ai_task` entity that style maps to,
and each entity is its own ComfyUI config entry with its own workflow, resolution and timeout.
Synth therefore never learns a checkpoint, a node id, a sampler or an engine. Every future
"functional bloat" feature takes the same shape: one Synth action, one HA script, provider chosen
on the HA side.

---

## 5. The image pipeline, end to end

### 5.1 Generation

1. Synth replies with the action `hass_image_generate` `{prompt: "...", style: "quick"}`. The action
   declares `external_effects`, so the router routes the turn to the **Agent Lane** structurally
   (no keyword logic), where one turn can compose generate -> wait -> send.
2. Plugin sends WS `call_service`:
   ```json
   {"id": 42, "type": "call_service",
    "domain": "script", "service": "synth_image",
    "service_data": {"prompt": "a rainy Ljubljana tram at night, film grain", "style": "quick"},
    "return_response": true}
   ```
3. HA runs `script.synth_image` -> `ai_task.generate_image` on the style's entity ->
   `comfyui_generator` POSTs the workflow to ComfyUI `/prompt`, waits on ComfyUI's WS, fetches the
   bytes from `/history` + `/view`, and returns them to HA.
4. HA stores the bytes in `<media dir>/ai_task/images/2026-09-21_183012_synth-image.png` and answers
   with `{media_source_id, url, mime_type, width, height, model, revised_prompt}`.
5. Plugin downloads `http://192.168.1.25:8123<url>` (pre-signed, valid one hour). Fallbacks, in
   order: `GET /media/ai_task/images/<file>` with the bearer token; then, only if
   `HASS_COMFY_BASE_URL` is set and reachable, the raw file from ComfyUI's own
   `/view?filename=...&subfolder=...&type=output`.
6. Plugin saves to `res/hass_cache/images/hass_<timestamp>_<slug>.png` (inside the allowed outbound
   roots, so no sandbox change) and returns that path plus a caption.
7. Synth sends it with the existing `send_message` action carrying the media path, to whichever
   conversation it is in (Telegram, Discord, WebUI).
8. Housekeeping: keep the newest `HASS_MEDIA_KEEP` files, delete the rest, so a productive Synth
   cannot fill D:.

Timing expectation to design for: a 1024x1024 SDXL render is seconds on a modern GPU but can be
30-60 s on a small one, and the component's own timeout (default 120 s, configurable) bounds it
before HA does. So `HASS_JOB_TIMEOUT_SEC` defaults to 300 and the agent turn must not treat a slow
render as a failure. The synth's guide will say plainly that a picture takes a while and that the
caption travels with the image in one message.

### 5.2 Multiple styles, one action

Each ComfyUI config entry in HA becomes one `ai_task` entity with its own workflow, resolution and
timeout. The plugin exposes them to the model as named styles through one config key:

```
HASS_IMAGE_ENTITIES = quick=ai_task.comfyui_quick, hires=ai_task.comfyui_hires, anime=ai_task.comfyui_anime
```

`hass_image_styles` lists them, and `style` on the generate action picks one (default
`HASS_IMAGE_DEFAULT_STYLE`, falling back to HA's preferred `ai_task` entity when unset). Adding a
new look later is a config entry in HA plus one line in that key.

### 5.3 The HA-side script (paste-ready)

Settings -> Automations & scenes -> Scripts -> Create (or `scripts.yaml` + `script.reload`).

```yaml
synth_image:
  alias: "Synth: generate an image"
  description: >-
    Single entry point used by Synth for image generation. Provider-agnostic:
    the style name selects the ai_task entity, the entity decides the engine.
  fields:
    prompt:
      name: Prompt
      description: What to draw.
      required: true
      selector:
        text:
          multiline: true
    style:
      name: Style
      description: Optional style name (quick, hires, ...). Empty selects the default.
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

Keeping the style map in this one place means Synth's config only ever holds script + style names,
and HA's UI is the single source of truth for which entity a style maps to. (Alternatively use an
`input_select` + a `choose` block; the map is shorter and has no hidden state.)

### 5.4 What v1 does not do

* **No image editing.** The stock component ignores attachments (section 2), so there is nothing to
  edit with locally yet. Section 5.6 is the fix.
* No img2img, no inpainting, no ControlNet, no upscaling chains, no video.
* No generation on Synth's own initiative (see `HASS_IMAGE_DAILY_CAP` and open decision 5).

### 5.5 Provider matrix (for the record)

| Path | Generation | Editing | Cost | Privacy | Infra needed |
|---|---|---|---|---|---|
| ComfyUI via the HACS component (MIT) | yes | **not yet** (fork below) | electricity | fully local | GPU host + ComfyUI + component |
| Gemini via HA core | yes | yes (attachments) | per image | leaves the LAN | API key (already present) |
| OpenAI via HA core | yes | no | per image | leaves the LAN | key |

Chosen: ComfyUI. The other two remain a one-line switch in the script if he ever wants an escape
hatch while the GPU is busy or the box is down.

### 5.6 Giving ComfyUI image editing (the fork, deferred but fully scoped)

The component is MIT and its own author left the scaffolding in place, so this is contained:

1. In `custom_components/comfyui_generator/ai_task.py`, the second positional parameter of
   `_async_generate_image` is HA's `UserMessage`. Read `user_message.attachments` (HA's Gemini
   provider does exactly this) and take the first entry's local path.
2. Upload that file to ComfyUI: `POST /upload/image` (multipart, field `image`) returns
   `{name, subfolder, type}`.
3. Inject `name` (or `subfolder/name`) into the workflow's LoadImage node: either via the existing
   `CONF_INPUT_IMAGE_NODE_ID` (expose it in the config flow, which already knows how to enumerate
   nodes by `class_type`, so it can also auto-detect the `LoadImage` node) and set denoise via
   `CONF_DENOISE_NODE_ID` / `DEFAULT_DENOISE = 0.75`.
4. Also handle the attachment arriving as a HA media-source id rather than a path (`ai_task`
   documents attachments as media selector output).
5. Ship it as a fork or an upstream PR; either way the Synth side needs no change, because
   `hass_image_edit` and its reference-upload step are already specced (7.2) and simply switch from
   "refused" to "available" when this lands.

Estimated: half a day to a day, plus testing on the GPU host. This is the natural v1.1.

---

## 6. Phase 0: infrastructure and unblocking (before any Synth code)

| # | Step | Who | Why |
|---|---|---|---|
| 0.1 | **HA REST 500 (diagnosed, fix is his call).** Unauthenticated requests 500 because HA cannot reverse-resolve the client: the router `192.168.1.1` returns a malformed PTR target (`40 8a ed be`) for private addresses, AdGuard forwards private PTRs to it (`local_ptr_upstreams` in `/mnt/user/appdata/adguard/config/AdGuardHome.yaml`), and HA's `ban.py` only suppresses `herror`, not `UnicodeDecodeError`. Fix, best first: (a) make the router answer PTRs properly or stop it doing so; (b) remove the two private reverse-DNS upstreams in AdGuard (Settings -> DNS settings -> private reverse DNS servers) so it returns NXDOMAIN, which HA handles; (c) Docker extra-hosts entries in the HA container as a workaround that depends on the network staying as it is; (d) report upstream, since HA should tolerate a decode failure here. Pick one and the probe is one line: an unauthenticated `GET /api/` must answer **401**, not 500. | Sandro | Not blocking (a valid token never takes this path), but it means a wrong token looks like a server error, and HA's login-ban feature cannot log hostnames on this LAN. |
| 0.2 | Create a **long-lived access token** (HA profile -> Security -> Long-lived access tokens). **Do not paste it in chat.** Paste it into the Synth WebUI plugin card once Phase 1 exists (masked field, stored in Synth's config store). | Sandro | Auth for everything. HA long-lived tokens are admin-scoped, so the plugin's allow/deny lists are the real boundary (section 11). |
| 0.3 | Confirm an HA **media directory** exists (`/media` on HAOS, or `homeassistant: media_dirs:` in `configuration.yaml`). `ai_task` raises without one, so every generation would fail at the last step. | Sandro | Cheap check, silent failure otherwise. |
| 0.4 | **Stand up ComfyUI on the GPU host** (LXC or VM with NVIDIA passthrough, `nvidia-container-toolkit` or the driver installed), listening on the LAN at a fixed address (e.g. `:8188`), with the checkpoints he wants. Verify it renders from its own web UI first. | Sandro | The engine. Note that HAOS itself cannot take GPU passthrough, so this lives beside HA, not inside it. |
| 0.5 | **Export the workflow(s) in API format** (ComfyUI: "Save (API Format)") and place them somewhere under HA's `/config` (the component requires an absolute path that starts with `/config/`). | Sandro | The component reads the workflow JSON; UI-format exports are rejected with a clear error. |
| 0.6 | **Install the HACS component** and add one config entry per style: title, ComfyUI URL, timeout, workflow path, resolution (node ids are auto-detected; it prints the available ids if a configured one is missing). Then test each entity from Developer Tools -> Actions -> "Generate image". | Sandro | Proves the HA half of the flagship path with no Synth involved. This is the gate for Phase 1 images. |
| 0.7 | Create `script.synth_image` (5.3) with the real style map, and run it once from Developer Tools -> Actions. Copy the response JSON into the chat (no secrets in it). | Sandro | Confirms `return_response` carries `media_source_id`/`url` through the script, which the plugin depends on. |
| 0.8 | Tell me the style names and the `ai_task` entity id each maps to, plus the ComfyUI base URL if he wants the direct-download fallback enabled. | Sandro | Fills `HASS_IMAGE_ENTITIES` and the script map. |
| 0.9 | Decide retention and autonomy: how many generated images Synth may keep, and whether it may generate on its own initiative or only when asked. | Sandro | Cost and disk; both are config keys. |

---

## 7. Phase 1: the Synth plugin (`plugins/home_assistant/`)

Structurally a sibling of `plugins/agpeer/`, so anyone who has read that plugin can read this one.

```
plugins/home_assistant/
    __init__.py
    home_assistant.py        # the plugin
    guide.md                 # rendered in the WebUI component card
    icon.svg                 # ORIGINAL glyph (no HA logo, no ComfyUI logo)
```

### 7.1 Config keys (the plugin banner in the WebUI Plugins tab)

Registered with `register_exposed_var(...)` + `config_registry.get_var(...)`, `scope="home_assistant"`,
`component="home_assistant"`. Secrets use `ui_type="password"` (that is this repo's masking
mechanism; `register_exposed_var` has no `sensitive=` kwarg).

| Key | Type / UI | Default | Meaning |
|---|---|---|---|
| `HASS_BASE_URL` | string | `http://192.168.1.25:8123` | HA instance URL |
| `HASS_TOKEN` | password | `""` | Long-lived token. Empty means the plugin loads **disabled**, with a stated reason (fail closed, like the interfaces do) |
| `HASS_VERIFY_TLS` | bool | false | kept for a future https setup |
| `HASS_AWARENESS_ENABLED` | bool | true | inject the house-state block |
| `HASS_AWARENESS_ENTITIES` | string (csv, wildcards) | e.g. `sensor.*_temperature, light.*, person.*, switch.washing_machine` | what the block carries |
| `HASS_AWARENESS_MAX_CHARS` | number | 1200 | hard cap on the block |
| `HASS_AWARENESS_MAX_AGE_SEC` | number | 900 | block omitted rather than rendered from a staler cache |
| `HASS_ALLOWED_DOMAINS` | string (csv) | `light, switch, climate, media_player, cover, scene, script, automation, input_boolean, input_number, input_select, fan, vacuum, notify` | allowlist for `hass_call_service` / `hass_run_script` |
| `HASS_DENIED_DOMAINS` | string (csv) | `lock, alarm_control_panel, camera, shell_command, homeassistant, hassio, tts` | denied even if allowlisted; denial wins |
| `HASS_CALL_TIMEOUT_SEC` | number | 30 | ordinary service calls |
| `HASS_JOB_TIMEOUT_SEC` | number | 300 | long jobs (images, scripts with responses) |
| `HASS_IMAGE_ENABLED` | bool | true | master switch for the image actions |
| `HASS_IMAGE_SCRIPT` | string | `script.synth_image` | the only script name Synth knows |
| `HASS_IMAGE_ENTITIES` | string (csv) | `quick=ai_task.comfyui_quick` | style name -> `ai_task` entity |
| `HASS_IMAGE_DEFAULT_STYLE` | string | `quick` | used when the model omits `style` |
| `HASS_IMAGE_EDIT_ENABLED` | bool | **false** | stays false until the fork in 5.6 lands; the action fails closed with a reason meanwhile |
| `HASS_IMAGE_DAILY_CAP` | number | 10 | per-day counter enforced by the plugin and shown in the WebUI card (`0` = unlimited) |
| `HASS_COMFY_BASE_URL` | string | `""` | optional direct-download fallback at the ComfyUI `/view` endpoint |
| `HASS_MEDIA_CACHE_DIR` | string | `res/hass_cache` | where downloads land; re-validated against the outbound roots before every write |
| `HASS_MEDIA_KEEP` | number | 50 | newest-N retention |
| `HASS_MAX_MEDIA_MB` | number | 25 | refuse bigger downloads |

(Phase 3 keys, `HASS_INGRESS_*`, are specified in section 9 and not registered in v1.)

### 7.2 Actions

Same schema shape as agpeer: a `description` written for the model, `required_fields`,
`optional_fields`, `security_level`, `external_effects` (the last one is what routes a turn to the
Agent Lane, structurally). Every call is logged with entity/service/outcome and never with secrets.

| Action | Does | Security | Effects |
|---|---|---|---|
| `hass_status` | Connection state, HA version, entity count, image quota left, which styles exist. Call first when something errors. | low | network |
| `hass_states` | List/filter entities (`domain`, `area`, `search`, `limit`), state plus the relevant attributes | low | network |
| `hass_state` | One entity, full state, attributes, last changed | low | network |
| `hass_history` | State changes for one entity over a window | low | network |
| `hass_call_service` | `domain` + `service` + target (`entity_id` / `area_id`) + `data`, optional `wait` for the response | medium | network, actuation |
| `hass_run_script` | Run a script by key with variables, wait for and return its response | medium | network, actuation |
| `hass_automation` | Trigger an automation, or list/toggle automations and scripts | medium | network, actuation |
| `hass_template` | Render a Jinja template inside HA and return the text (read-only, but it can read anything) | medium | network |
| `hass_notify` | Notify through HA (`notify` service or `persistent_notification`), optionally with a local image | medium | network, outbound to a human |
| `hass_snapshot` | Camera snapshot into `res/hass_cache/camera/`, path returned so Synth can look at it or forward it | medium | network, filesystem |
| `hass_image_styles` | List the configured style names and their entities, so the model knows what it may ask for | low | network |
| `hass_image_generate` | The flagship: `prompt`, optional `style`, optional `aspect`. Returns `{path, width, height, model, revised_prompt}` | medium | network, filesystem |
| `hass_image_edit` | Same path with `reference_path` required. Registered but **refused while `HASS_IMAGE_EDIT_ENABLED` is false**, with a message naming the reason so the model can tell the user instead of pretending | medium | network, filesystem |
| `hass_watch` | Subscribe/unsubscribe `state_changed` for a set of entities (feeds the awareness snapshot today, the Phase 3 interface later) | medium | network |

### 7.3 House awareness in the prompt

Template: `plugins/weather_plugin/weather_plugin.py` (an injection priority plus a short rendered
summary). The plugin keeps a cached snapshot of the configured entities, refreshed on connect and on
`state_changed` for the watched set, and renders a bounded block:

```
[Home] living room 21.4 C / 48% RH, bedroom 19.8 C, 3 lights on (kitchen, desk, hall),
front door closed, nobody home, washing machine running (42 min left), energy today 6.1 kWh.
```

Rules the implementation follows (repo rules, not preferences):
* bounded by `HASS_AWARENESS_MAX_CHARS`, newest value per entity, never a log;
* no keyword or regex reasoning anywhere: which entities are watched is configuration, what the
  model does with them is the model's business;
* omitted entirely when the link is down or the cache is older than `HASS_AWARENESS_MAX_AGE_SEC`.

### 7.4 WebUI

* A **Plugins** tab card generated from the config keys above (that is what `scope`/`component` do),
  plus the `guide.md` body.
* One small operator card in the same tab: last `hass_status` result, the last image job (prompt,
  style, file, size), the daily counter, and a "test connection" button. Same markup as the sibling
  cards in `core/webui_templates/sections/settings.html`; the JS goes in
  `res/synth_webui/js/main.js` with its own `window.__synth_hass_wired` guard. Those files are CRLF.
* Nothing blocks the request: a test runs as an `asyncio.create_task` with a status reader, and a
  second press while one is running is refused, not queued.

### 7.5 Failure and safety model

* **Fail closed, never fatal** (AGENTS.md): HA down, ComfyUI down, token wrong, unknown style ->
  a clean per-action error string the model can relay; importing or deleting the plugin leaves the
  rest of Synth untouched.
* **Deny beats allow**; locks, alarm panels, cameras and shell commands stay out unless he widens
  the list on purpose.
* **WS reconnect with backoff**, never stacking connections, and `hass_status` states which state
  the link is in.
* **Every job bounded twice:** per call (`HASS_JOB_TIMEOUT_SEC`) and per day
  (`HASS_IMAGE_DAILY_CAP`), with the counter in the activity log so a runaway loop is visible.
* **Media only inside `HASS_MEDIA_CACHE_DIR`**, re-validated against
  `core.outbound_file_utils.allowed_file_roots()` before every write, so a misconfigured value fails
  loudly instead of writing somewhere Synth would later refuse to send from.
* A failed download after HA produced the image must say so and name the media-source id, so the
  user can pick the picture out of HA's media browser instead of losing the render.
* Anything that ends in a message being lost still records a `delivery_failed` entry in
  `llm_failure_log` (stage `delivery`) per the repo rule, so a vanished image is diagnosable from
  the database rather than from raw logs.

### 7.6 Optional, additive: HA's own MCP tools

`config/synth_mcp.json` is Synth's runtime MCP registry (separate from the dev `.mcp.json`). One
entry gives Synth dozens of extra HA tools (`mcp_ha_*`) with no plugin code:

```json
"home_assistant": {
  "enabled": false,
  "transport": "http",
  "url": "http://192.168.1.25:9584/private_<id>",
  "security_level": "medium",
  "description": "ha-mcp (MIT): admin-level HA tooling. Disabled by default."
}
```

Recommended as a second, opt-in path, not the primary one: the native plugin gives Synth a stable
action family, prompt awareness and allowlists it controls, while an MCP surface hands the agent the
whole instance administration. Keep it disabled until he actually wants Synth administering HA.

---

## 8. Phase 2: the rest of the HA side

1. `script.synth_image` (5.3), plus one extra script per house-wide action he wants Synth to be able
   to invoke, e.g.:
   ```yaml
   synth_goodnight:
     alias: "Synth: goodnight"
     sequence:
       - action: light.turn_off
         target: { entity_id: all }
       - action: cover.close_cover
         target: { area_id: bedroom }
       - stop: "done"
         response_variable: { done: true }
   ```
2. Optional helpers (`input_text.synth_last_image`) as a polling fallback; with `return_response`
   working they are not needed.
3. A short list for Sandro of what HA can now do on Synth's behalf, so the "functional bloat" list
   (5.6 first, then whatever else he wants delegated) has a home.

---

## 9. Phase 3 (deferred): HA -> Synth

Documented now so the design does not drift; built only when he asks.

* **Template:** `interface/vessel_interface.py`: duck-typed like the other interfaces
  (`display_name`, `get_interface_id()`, `get_supported_actions()`, `send_message(...)`,
  `start()`/`stop()`, module-level `initialize_interface()`), a language-agnostic salience filter
  (dedup + rate limit, no LLM, no keywords), then
  `await core.message_queue.enqueue(self, wrapped, interface_id="hass", skip_mention_check=True, ...)`.
* **Interface path:** `hass/<instance>/<source>` via `core.interface_path_utils.build_interface_path`,
  one conversation scope per source (e.g. `hass/home/doorbell`).
* **Ingress:** its own aiohttp listener on `HASS_INGRESS_PORT` (8199), gated by
  `HASS_INGRESS_TOKEN`: `POST /events` (source, kind, summary, urgency, optional media),
  `POST /media` (multipart -> `res/hass_cache/inbox/` -> a chain message carrying the local path so
  Synth can actually see the snapshot), `GET /health` for the reachability test.
* **HA side:** `rest_command.synth_push` plus sample automations (doorbell with snapshot, laundry
  finished, energy spike, arrival). If HA cannot reach this VM, the fallback is the plugin's
  `hass_watch` WS subscription forwarding the same events with no inbound port at all.
* **Structural salience, not keywords:** the payload declares `kind` and `urgency`; urgent kinds
  enqueue immediately, the rest batch into one perception turn per window, as the vessel interface
  batches telemetry. That is what stops a chatty house from burning a full LLM turn per sensor tick.

---

## 10. Phase 4 (deferred, optional): Synth inside HA

1. **YAML only, cheapest:** `rest` sensors reading Synth's WebUI API (mood, current activity, last
   reply age), a `rest_command` to hand Synth a message, a `notify` platform so automations can make
   Synth speak. An hour or two, nothing to maintain.
2. **HACS custom component `synth`:** proper entities (sensors for mood/emotion/queue/uptime, a
   switch for auto-response, services `synth.say` / `synth.ask`) and optionally a `conversation`
   agent so HA Assist relays to Synth. Worth it only if he wants voice/Assist to reach Synth.
3. **ha-mcp (7.6):** not this feature. 10 is HA looking at Synth; 7.6 is Synth operating HA.

---

## 11. Security and failure model (cross-cutting)

* **The HA token is root on his house.** Long-lived HA tokens are admin-scoped with no read-only
  flavour, so the plugin's allow/deny lists are the real boundary, not the token's scope.
  Locks, alarm panels, cameras, `shell_command`, `homeassistant` and `hassio` are denied by default.
* **Autonomy:** each action's `security_level` is enforced by
  `core.action_safety.is_action_allowed_for_execution` against his configured ceiling, so "Synth may
  read the house" and "Synth may actuate the house" are separable by configuration rather than by
  prompt wording. Image generation is its own flag plus a daily cap.
* **Cost and privacy:** local ComfyUI means prompts and pictures never leave the LAN. The GPU host
  is the only new attack surface: ComfyUI has no auth, so bind it to the LAN and keep it off the
  internet (a firewall rule on the hypervisor, or its own VLAN, is enough).
* **No parallel message flow** (AGENTS.md): the plugin produces actions on the existing chain; the
  Phase 3 ingress enqueues onto the existing chain. Nothing calls the engine directly.

---

## 12. Tests, docs, changelog, deploy

* **Unit tests, no live HA needed:** a fake WS transport that records the frames the plugin sends
  and replays canned `result` frames (states, service response, image response, error). Assertions
  on the allow/deny decision table, on the awareness block being bounded, on the style map
  resolution, and on the daily cap.
* **Image path:** fake response with a `media_source_id` + `url`, a local aiohttp test server
  serving PNG bytes; assert the file lands in `HASS_MEDIA_CACHE_DIR`, that
  `core.outbound_file_utils.classify_media` calls it an image, that retention prunes the oldest, and
  that a directory outside the outbound roots is refused.
* **WebUI:** card HTML parsed with `html.parser` (tag stack ends empty), `node --check` on
  `main.js`.
* **Repo rule respected:** the full pytest suite writes fixtures into the live DB and costs 10+ GB
  while the 2B instance runs out of this same checkout, so only the new narrow test files get run,
  in the background with a redirect, and no interpreter is left behind afterwards.
* **Docs:** plugin `guide.md`, a `docs/plugins.rst` (or `docs/home_assistant.rst`) page, and a
  `CHANGELOG.md` entry in the repo's shape (heading with a date comment, then Symptom / Root cause /
  Fix / Notes).
* **Deploy facts to state every time:** a code change is live only after the instance restarts (it
  runs out of this checkout); a config value written straight to the `config` table is not live until
  the config page load or that restart.
* **Commit policy:** nothing is committed unless he asks; this plan file stays untracked.

---

## 13. Sequence, effort, acceptance

| Phase | Work | Effort |
|---|---|---|
| 0 | ComfyUI host + models + workflow in API format + HACS component entries + `script.synth_image` live + HA log traceback for the REST 500 + token | 2-4 h, his side |
| 1 | Plugin: WS client, config keys, `hass_*` actions, awareness block, image bridge, WebUI card, tests, guide | 6-10 h |
| 2 | HA-side scripts for the delegated actions, style map, house actions list | 2-3 h |
| 1.1 | Fork of the MIT component adding attachments/img2img, which turns on `hass_image_edit` | 4-8 h |
| 3 | `interface/hass_interface/` and its sample automations | 4-6 h, deferred |
| 4 | YAML sensors, or the HACS component with a conversation agent | 2 h / 1-2 days, deferred |

**Acceptance for v1:** from Telegram, asking Synth for a picture returns a real image in the chat,
the file is in `res/hass_cache/`, the WebUI card shows the job and the day's count, asking for a
different style produces a different look, and asking for an edit answers honestly that editing is
not available yet. Killing ComfyUI or HA mid-request produces a clean error message to the user, no
crash, no stuck turn, no orphan file, and the daily cap holds.

**Non-goals:** no diffusion inside Synth, no cloud image provider, no HA add-on, no camera
streaming, no exposing Synth's database to HA, no replacing HA's automations with Synth's judgement,
no image editing until 5.6 lands.

---

## 14. Open decisions (need Sandro)

1. **Styles and workflows:** which looks does he want first (a fast 1024 SDXL default, a slower
   high-res one, an anime model, his own LoRA)? One ComfyUI config entry each.
2. **ComfyUI host placement:** LXC vs VM, which GPU, and its fixed LAN address. HA must reach it, and
   a firewall rule should keep it off the internet (it has no auth).
3. **Direct-download fallback:** does he want `HASS_COMFY_BASE_URL` set so Synth can pull the raw
   file from ComfyUI when HA's media serving hiccups? It needs the same LAN access from this VM.
4. **Autonomy and retention:** on request only (recommended) or may Synth generate on its own
   during a beat, and how many files may it keep?
5. **The fork:** does he want image editing (5.6) as part of this push, or as a follow-up once
   generation is proven end to end? Editing is the only feature he originally asked for that v1
   cannot deliver, and the fork is the only local route to it.
6. **The HA REST 500:** waiting on the traceback (0.1). Optional and not blocking, but worth
   closing.

---

## 15. Appendix: exact interfaces used

* **HA WS API:** `POST /api/websocket` (upgrade), then `{"type": "auth", "access_token": "..."}` ->
  `auth_ok`. Commands used: `get_states`, `get_state`, `call_service` (+`"return_response": true`),
  `subscribe_events {event_type: "state_changed"}`, `render_template`, `history/history_during_period`.
* **HA actions:** `ai_task.generate_image` (`task_name`, `instructions`, `entity_id`,
  optional `attachments`) returning `media_source_id`, `url` (pre-signed, 1 h), `revised_prompt`,
  `model`, `mime_type`, `width`, `height`, `conversation_id`; files stored under
  `<first media dir>/ai_task/images/`.
* **HA media:** `GET /media/ai_task/images/<file>` with the bearer token (the pre-signed `url` needs
  no token); REST upload of a reference image when the fork lands:
  `POST /api/media_source/local_source/upload` (multipart) -> `{"media_content_id": "media-source://..."}`
  (the only place REST is required anywhere in this plan, and it is unaffected by the 500, which
  only fires on unauthenticated requests).
* **ComfyUI API (used by the component, or by the fork):** `POST /prompt` with `{prompt, client_id}`,
  WebSocket `/ws` for progress, `GET /history/{prompt_id}`, `GET /view?filename=&subfolder=&type=`,
  `POST /upload/image` (multipart, fork only), `POST /interrupt`.
* **ComfyUI HA component:** domain `comfyui_generator`, config entry fields (title, ComfyUI base URL,
  timeout, workflow path under `/config/`, resolution), automatic node-id detection, one `ai_task`
  entity per entry, v0.5.0.
* **Synth side:** `plugins/agpeer/agpeer.py` (external-service plugin shape),
  `plugins/weather_plugin/weather_plugin.py` (prompt injection),
  `interface/vessel_interface.py` (Phase 3 inbound pattern),
  `core/message_queue.enqueue` (Phase 3 entry point),
  `core/karada_api.py` (token-gated external surface precedent),
  `core/outbound_file_utils.allowed_file_roots` (media sandbox),
  `core/variables_engine.register_exposed_var` (WebUI config keys, `ui_type="password"` for secrets),
  `core/action_safety.is_action_allowed_for_execution` (autonomy ceiling),
  `core/llm_failure_log` (`delivery_failed` recording).
