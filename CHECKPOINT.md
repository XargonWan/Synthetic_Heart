# CHECKPOINT — SyntH Stage (new standalone frontend)

**Date:** 2026-07-08
**Commit:** `fb802436` — `feat(frontend): add SyntH Stage — standalone Vue 3 avatar/chat/voice app`
**Branch:** `develop`
**Status:** All 6 planned milestones (M0–M6) built and verified. Working end-to-end against a live backend. Phase 2 (realtime audio-to-audio) is intentionally not built — see "What's NOT done" below.

Read this before touching `frontend/` or the `/stage`, CORS, or API-token code in `core/webui.py` / `core/karada_api.py`.

---

## 1. What this is

A brand-new, standalone Vue 3 frontend for SyntH — the avatar stage, chat, and voice client — living at `frontend/`, separate from the legacy webui (`core/webui_templates/` + `res/synth_webui/js/`, untouched). Both are just two different clients of the same backend; they can run side by side.

**Why it exists:** the user wanted a "slick" frontend like Project AIRI's, ported/adapted where it made sense, with an eye toward eventually shipping as web/mobile apps and adding AR. Full context in the approved plan, saved at `C:\Users\SERVREMU\.claude\plans\sup-c-i-have-joyful-bumblebee.md` on this machine (not in the repo — copy it in if you want the original exploration/rationale).

**How to open it:**
- Production-style: append `/stage/` to whatever URL serves the normal webui (e.g. `https://localhost:8000/stage/`). The backend auto-mounts `frontend/dist/` there if it exists — no config needed.
- Dev mode with hot reload: `cd frontend && pnpm dev` → `http://localhost:5173`. Proxies `/api`, `/ws`, `/skins`, `/uploads`, `/avatars` to `https://localhost:8080` by default (override with `SYNTH_BACKEND_ORIGIN`).
- `frontend/README.md` has the full toolchain notes (Node/pnpm — the one non-uv corner of this repo).

---

## 2. Architecture summary

```
frontend/
├── src/
│   ├── lib/                    # framework-agnostic TS (portable outside Vue)
│   │   ├── pipelines-audio/    # playback-manager.ts — ported ~verbatim from AIRI
│   │   ├── audio/               # mic-capture.ts (PCM worklet), voice-recorder.ts (MediaRecorder)
│   │   └── lipsync/             # AnalyserLipSyncDriver (amplitude-based mouth open)
│   ├── services/                 # typed backend clients
│   │   ├── protocol.ts           # the ENTIRE /ws message contract, single choke point
│   │   ├── synth-ws.ts           # /ws client (hello handshake, reconnect/backoff)
│   │   ├── karada-rest.ts        # /api/karada/* REST client
│   │   ├── audio-stream.ts       # /api/audio/stream (VAD signal only, see §4)
│   │   └── audio-upload.ts       # /api/audio/upload (the actual STT)
│   ├── stores/                   # Pinia: connection, chat, avatar, audio, mic, settings
│   ├── composables/vrm/          # the avatar rendering stack
│   │   ├── scene.ts               # SceneHost — AR-ready camera/scene abstraction
│   │   ├── loader.ts              # VRM loading (GLTFLoader + VRMLoaderPlugin)
│   │   ├── animation.ts           # Karada v2 descriptor engine, ported from res/synth_webui/js/vrm-animation-engine.mjs
│   │   ├── avatar-driver.ts       # glues WS events -> animation engine -> clip cache
│   │   ├── animation-cache.ts     # per-VRM retargeted-clip cache
│   │   ├── face.ts                # FacialDriver — composes face_values + descriptor expressions + overlay + visemes
│   │   ├── eye-saccade.ts         # idle look-around
│   │   └── retarget/              # Mixamo FBX -> VRM bone retargeting, ported from res/synth_webui/js/{loadMixamoAnimation,mixamoVRMRigMap}.js
│   └── components/                # Stage.vue (orchestrator), chat/, settings/, system/
└── scripts/                       # Playwright smoke-test scripts used to verify every milestone (see §6)
```

**Backend changes** (`core/webui.py`, `core/karada_api.py`), all additive/default-off:
- `/stage` static mount for `frontend/dist/` (guarded on directory existing).
- Config-gated CORS (`SYNTH_WEBUI_CORS_ORIGINS`, comma-separated, empty = off).
- Optional bearer/query API token (`SYNTH_WEBUI_API_TOKEN`) gating `/ws`, `/api/karada/*`, `/api/audio/stream`. Unset = no auth, identical to current behavior.
- `create_karada_router()` now returns `(rest_router, ws_router)` instead of a single router — **this is a breaking signature change**, see §5.

---

## 3. What's verified working (with evidence)

Every milestone was checked against a **live backend**, not just `pnpm build`, using throwaway Playwright scripts in `frontend/scripts/`:

| Milestone | Verified | How |
|---|---|---|
| M1 — model on stage | VRM renders, skin swap from legacy webui reflects live | `stage-check.mjs` screenshot |
| M2 — animation engine | Idle loop, think intro→loop→outro with crossfade, no T-pose, on **three 0.184** (legacy uses 0.160 — this was the explicit risk gate, it passed) | `m2-action-check.mjs` |
| M3 — chat + facial layer | Full text conversation, phase indicator (thinking/writing), **eyes actually close during `think`** (this was a documented bug in the legacy viewer, AGENTS.md §12 — the new facial layer fixes it as a side effect) | `m3-chat-check.mjs` |
| M4 — voice | TTS playback + lipsync (visemes tracked a synthetic AM tone's envelope correctly, 0.24→0.63→0.25), mic reaches `listening` state with Chrome's fake-device flag | `m4-voice-check.mjs` (note: this script's mic architecture assumptions were later found wrong and the underlying code was fixed — see §5, bug #1) |
| M5 — polish | Skin selector with real thumbnails + correct active-skin highlight, settings drawer, theme hue slider (live), loading screen with real byte progress, transparent mode (`?transparent=1`, confirmed via computed style) | `m5-polish-check.mjs` (⚠ the hue-slider "verification" was screenshot-only and was in fact broken in the shipped build, and the skin selector never switched the stage's model — both fixed 2026-07-08, see §8) |
| M6 — hardening | Token auth on REST confirmed (401 without token, 200 with); WS auth confirmed on both `/ws` and `/api/karada/ws` (open/reject correctly) after fixing a FastAPI bug (§5, bug #2); clean prod build served from `/stage` | `m6-auth-check.mjs` |

Backend edits passed `uv run ruff format/check`, scoped `uv run ty check` (no new diagnostics beyond pre-existing `_AnimStub` noise already documented in AGENTS.md §12), and the scoped test `tests/test_karada_state_server.py -k router` (3 passed).

---

## 4. Two real bugs found and fixed during this build

**These aren't cosmetic — read them before extending the voice or auth code.**

### Bug 1 — `/api/audio/stream` does not transcribe on the default engine
Initial design assumed the mic WebSocket (`/api/audio/stream`) would stream back `partial`/`final` transcripts for the default `vad`/`silero` engine. **It does not.** That engine only ever emits `ready` and `vad` (`speech_start`/`speech_end`) — verified by reading `core/webui.py`'s `audio_stream_ws_endpoint` directly (`partial`/`final` are only emitted on the separate Live-engine branch, which nothing uses by default).

The real transcription path — confirmed by reading how the **legacy webui** does it (`res/synth_webui/js/chat-window.mjs`, `startRecording`/`stopRecordingAndSend`) — is:
```
mic stream ─┬─▶ 16kHz PCM ──▶ /api/audio/stream   (VAD signal only)
            └─▶ MediaRecorder ── (per-utterance clip) ──▶ POST /api/audio/upload  (actual STT, returns {"text": "..."})
```
`frontend/src/stores/mic.ts` implements this correctly now: on `speech_start` a `VoiceRecorder` (MediaRecorder wrapper, `lib/audio/voice-recorder.ts`) starts capturing; on `speech_end` it stops and the clip is POSTed to `/api/audio/upload` (`services/audio-upload.ts`), and the returned text is sent over `/ws` exactly like typed input.

Also fixed as part of this: `services/audio-stream.ts` originally read wrong JSON field names (`event`/`message`) — the server actually sends `signal`/`detail` (confirmed by grepping the real `send_json` calls in `core/webui.py`, not just its docstring, which was also slightly stale).

### Bug 2 — FastAPI router-level `Depends()` breaks WebSocket routes
`APIRouter(dependencies=[Depends(fn)])` applies that dependency to **every** route added to the router, including `@router.websocket(...)` ones — even via `include_router()`. If `fn` is typed for HTTP `Request` (as `_require_api_token` was), FastAPI raises `TypeError: _require_api_token() missing 1 required positional argument: 'request'` deep inside dependency resolution when a WS client connects. This doesn't surface as a clean 401 — it's an unhandled server exception that closes the socket with code `1006` (abnormal closure), **regardless of whether the token was correct**. It silently "worked" in the sense that unauthorized clients got rejected, but so did authorized ones, and the real cause was buried in server logs, not visible from the client.

Fix: `create_karada_router()` in `core/karada_api.py` now returns **two** routers — `(rest_router, ws_router)`. The REST router keeps the token dependency; `ws_router` has none and does its own explicit token check inline (before `.accept()`). The single call site (`core/webui.py::__init__`) and the one test that constructed the router (`tests/test_karada_state_server.py`) were both updated. If you see this pattern elsewhere in the codebase (a websocket route on a router with `dependencies=`), it has the same bug.

---

## 5. Known gaps / things to keep in mind

- **AR is not built.** The plan explicitly deferred it, but the renderer is built to make it a real "later" and not a rewrite: `composables/vrm/scene.ts`'s `SceneHost` uses `renderer.setAnimationLoop()` (never `requestAnimationFrame`, the one non-negotiable WebXR rule), and exposes `cameraRig`/`avatarRoot` groups so nothing outside that one file touches the camera directly. When you do build AR, start there.
- **Phase 2 (realtime audio-to-audio) is not built**, by design — and confirmed: `frontend/src/services/conversation-transport.ts` from the plan was **never created**, there's no `ConversationTransport` interface in the codebase. The current voice flow (mic VAD signal → MediaRecorder clip → `/api/audio/upload` STT → `/ws` text → TTS reply) is phase 1 only, hardcoded directly into `stores/mic.ts` and `stores/audio.ts` rather than going through a swappable transport abstraction. Building phase 2 means both writing that abstraction and building the backend bridge — the backend seam exists at `core/live_registry.py` / `plugins/live_base.py::LiveEngineBase` (used today only for Discord↔Gemini Live), but nothing wires it to the browser yet.
- **No auth by default.** `SYNTH_WEBUI_API_TOKEN` is unset unless you set it — anyone who can reach the backend can reach `/ws`, `/api/karada/*`, `/api/audio/stream`. Fine for LAN/dev, not fine to port-forward as-is.
- **CORS is off by default** (`SYNTH_WEBUI_CORS_ORIGINS` empty). Needed later for a Capacitor/mobile build; not needed for the Vite dev proxy or same-origin `/stage`.
- **Skin activation uses `POST /api/skins/{name}/activate`** (webui.py), *not* `POST /api/karada/action` — the latter only accepts `AnimationState` enum values (idle/think/touch/write/talk/skin_change) and just plays the skin-change *animation*, it doesn't swap the model. This was a real trap during M5 — `services/karada-rest.ts::activateSkin()` has a comment explaining it; don't "simplify" it back to the karada action endpoint.
- **Lipsync is amplitude-based** (`lib/lipsync/index.ts::AnalyserLipSyncDriver`), not phoneme-accurate — no SyntH TTS engine currently implements `VoxBase.get_lipsync_data()` (grepped, confirmed None everywhere). The `LipSyncDriver` interface exists specifically so a real phoneme-based driver (or AIRI's `wlipsync`) can drop in later without touching callers.
- **`frontend/scripts/*.mjs`** are throwaway-but-kept Playwright smoke scripts, not a real test suite (no assertions library, just console output + screenshots you have to eyeball). They're genuinely useful for manual QA of the avatar/animation/voice pipeline going forward — Playwright + its Chromium browser are installed as a **frontend devDependency**, so `pnpm exec playwright install chromium` may be needed on a fresh clone before they'll run.
- **A live test wav** (`stage_test_tone.wav`) was generated into the OS temp attachments dir during M4 testing and already cleaned up — if you regenerate one for lipsync testing, it doesn't need to be committed (it's outside the repo).
- The GitNexus index was refreshed after this commit (`GITNEXUS_HOME=.gitnexus-home npx gitnexus analyze --skip-agents-md`, run once already) — re-run it again after your next commit per the standing repo rule.

---

## 6. Suggested next steps (not started)

Roughly in priority order:
1. **Server-side TTS sentence chunking** — flagged in the plan but not implemented; Vox currently likely synthesizes whole replies as one clip, which blunts the perceived-latency win the ported `playback-manager.ts` is designed for. Check `plugins/vox_plugin.py`.
2. **Barge-in / interrupt** — `useAudioStore().stopAll()` exists but isn't wired to anything (e.g. user starts talking while the avatar is speaking). This is also most of the work for the phase-2 realtime seam.
3. **Phase 2 realtime audio-to-audio** — see the gap noted above; verify what (if anything) exists in `services/` first.
4. **Auth UX** — `settings.apiToken` exists in the Pinia store (`stores/settings.ts`) but isn't wired into `synth-ws.ts`'s connection query string yet; currently you'd have to hand-edit code to actually use a configured token from the frontend side (the backend-side gate is fully done and tested).
5. **AR** — see §5.
6. Mobile packaging (Capacitor, matching AIRI's `stage-pocket` pattern) — CORS groundwork is already there for it.

---

## 7. Validation commands (for whoever picks this up)

```bash
# Backend
uv run ruff format core/webui.py core/karada_api.py
uv run ruff check core/webui.py core/karada_api.py
uv run ty check core/webui.py core/karada_api.py     # expect only pre-existing _AnimStub noise, nothing new
uv run pytest tests/test_karada_state_server.py -q

# Frontend
cd frontend
pnpm install
pnpm typecheck
pnpm build
pnpm dev   # http://localhost:5173, needs a running backend to proxy to
```

To spin up a scratch backend for manual testing (not your normal container):
```bash
uv run python scripts/run_webui.py   # HTTPS on :8000 by default; see the script for env var overrides
```

---

## 8. 2026-07-08 follow-up — three user-reported /stage bugs, all fixed

Reported after the checkpoint: skin switch did nothing, theme-hue slider did nothing, mic died with `Cannot read properties of undefined (reading 'getUserMedia')`. All three reproduced with `frontend/scripts/repro-issues.mjs` (kept, reusable) and fixed:

1. **Skin switch — backend never broadcast `vrm_model`.** `POST /api/skins/{name}/activate` (`core/webui.py::activate_skin`) set the active VRM and played the skin-change animation but never called `animation_handler.set_vrm_model()`, so no client got a `vrm_model` event. The legacy webui masked this by calling its own `refreshModels()` after the POST; the stage relies on the broadcast (its SkinSelector comment claimed the server broadcasts — it didn't, until now). Fixed by broadcasting in `activate_skin`, same pattern as `set_active_vrm_endpoint`. Verified on a scratch backend: clicking a skin tile now yields a `vrm_model` WS frame and the stage downloads + swaps the model. (Legacy webui is unaffected: its `vrm_model` handler calls `window.refreshVRM`, which is defined nowhere — guarded no-op.)
2. **Theme hue — colors were baked at build time.** The AIRI chromatic preset's Node entry bakes the hue into static oklch colors when `VSCODE_ESM_ENTRYPOINT` contains `extensionHostProcess` (UnoCSS IDE-extension heuristic). In-IDE agent shells export exactly that, so the previous `pnpm build` shipped CSS that ignored `--chromatic-hue`. Fixed with `frontend/chromatic-env-guard.ts` (strips the var; imported first in `uno.config.ts`) + rebuild. Full writeup in AGENTS.md §12.
3. **Mic — insecure context.** `navigator.mediaDevices` doesn't exist over plain `http://` on non-localhost origins (the user's LAN URL). Not a code bug; `stores/mic.ts::start()` now fails fast with "Microphone needs a secure context — open the stage over HTTPS or via localhost" instead of the opaque TypeError. AGENTS.md §12 entry added; the legacy webui has the same limitation, unguarded.

Validation: `ruff format/check` clean; `ty check core/webui.py` shows only the pre-existing `_AnimStub` noise (the new `set_vrm_model` call matches the diagnostic already present for the older call site); `tests/test_webui_vrm_defaults.py` + `tests/test_karada_state_server.py` — 29 passed. Frontend: `pnpm typecheck` + `pnpm build` clean; hue + skin switch + mic guard all re-verified live via Playwright (hue against the running instance on :8088, skin broadcast against a scratch backend on :8091, mic guard against `http://192.168.1.69:8088`).

**Note for the trainer:** the running SyntH instance still has the old backend code — restart it to pick up the skin-switch fix. The frontend fix is already live (`frontend/dist/` is served from disk).

Two more found while chasing the remaining log errors (same day, committed separately):

4. **`vrm_preload.descriptor` is an object, not an id — stage preloading never worked.** The server puts the parsed descriptor-*data* dict in `vrm_preload` (`animation_handler.py::_preload_animation`) but the descriptor-id *string* in `vrm_animation_v2`. The stage typed both as string ids and asked the backend to resolve `[object Object]` — the source of the recurring `GET /api/karada/animations/resolve -> 404` log spam, and it meant cache-warming silently did nothing (first play of every animation loaded late). Fixed in `protocol.ts` (honest union type) + `avatar-driver.ts::applyPreloadEvent` (string → resolve; otherwise warm the already-resolved `file` path, like the legacy viewer). Verified: preload now fetches the FBX, zero resolve 404s.
5. **`/ws` hello-phase disconnect spam.** A client dropping during the 0.3 s hello window (page reload / reconnect churn — also every Playwright smoke run) produced `Failed to push VRM state ... 'websocket.close'` + `websocket error: Cannot call "receive" once a disconnect message has been received` on every occurrence. The disconnect can also be consumed *without* raising (`asyncio.wait_for` cancellation race), so `websocket_endpoint` now both catches `WebSocketDisconnect` in the hello phase and double-checks `client_state`/`application_state` before pushing state or entering the receive loop. Verified with deliberate mid-handshake drops: one clean "Client disconnected" INFO, no warnings/errors, across repeated runs.
6. **Env-var parsing hardened against IDE `.env` injection (2026-07-09).** The trainer's "TLS on but HTTPS port dead" evening: VS Code/Antigravity injects the workspace `.env` into launched processes with a parser that keeps inline `#` comments in the value, and SyntH's `load_dotenv(override=False)` can't overwrite it — so `SYNTH_WEBUI_HTTPS_PORT='8088   # comment'` failed `int()` and *silently* fell back to HTTPS-on-the-HTTP-port. `core/webui.py::_clean_env` now strips inline comments from host/TLS/port vars and WARNs; unparsable ports also WARN instead of failing silently. Verified end-to-end with a deliberately poisoned env (warning logged, correct port served). Full writeup + psutil debugging trick in AGENTS.md §12.

---

## 9. Deployment state at handoff (2026-07-09, ~01:00)

Read this before touching ports, TLS, or the trainer's proxy setup.

- **Intended config** (in the machine-local `.env`, not committed): `SYNTH_WEBUI_TLS=1`, `SYNTH_WEBUI_HTTPS_PORT=8088`, `SYNTH_WEBUI_HTTP_PORT=8008`, host `0.0.0.0`. Expected listeners after a restart: **HTTPS on :8088 + plain HTTP on :8008** (both are served when the ports differ). Keep `.env` comments on their own lines — see §8.6.
- **The instance running at handoff predates both the `.env` comment fix and the `_clean_env` hardening** — it serves HTTPS on :8008 and nothing on :8088. **One more restart is required**; after it, sanity-check with `netstat -ano | findstr "8088 8008"` (expect `0.0.0.0:8088` and `0.0.0.0:8008`).
- **Trainer's reverse proxy**: nginx proxy manager on `192.168.1.13` forwards the public domain → `http://192.168.1.69:8088`. After the restart, :8088 becomes HTTPS, so **NPM must be repointed to :8008 (scheme http)** or switched to scheme https on :8088 — otherwise the domain breaks. The domain is WAN-exposed (other services need it) and internet scanners actively probe it (CT-log triggered); the backend has **no auth**, so an NPM Access List on the SyntH host was recommended but is user-action, not done in code.
- **Mic**: works via the domain (real cert) or `https://192.168.1.69:8088/stage/` (self-signed — accept once per device). Plain-http LAN URLs show the guard message by design.
- **A stale scratch `scripts/run_webui.py` had been squatting `127.0.0.1:8088` for days** (killed 2026-07-09). If ports behave inexplicably, check `netstat` for leftover scratch instances first, and read the *live process* env with psutil (AGENTS.md §12) before trusting `.env`.
- `frontend/dist/` on disk is current (includes the preload fix); it's served directly, so frontend fixes are live without a backend restart.
- Suggested next steps are unchanged (§6), with **auth UX (§6.4) now the top pick**: the agreed direction is a pairing-token flow (server generates token → QR/URL → wire `settings.apiToken` into `synth-ws.ts` + REST clients), with a Capacitor wrapper as the longer-term secure-webapp answer (discussion 2026-07-09).

---

## 10. 2026-07-09 — §6.4 auth UX wiring done (frontend-only, no backend changes)

`settings.apiToken` now actually reaches every gated endpoint instead of sitting unused in the Pinia store:

- New `frontend/src/lib/api-token.ts` — `apiTokenQuery()` / `withApiToken(url)`, both reading `useSettingsStore().apiToken` and building a `?token=` query string (query param chosen over the `Authorization` header so REST and WS call sites share one mechanism and cross-origin calls don't trigger CORS preflight).
- `stores/connection.ts::connect()` passes `apiTokenQuery()` into `SynthWs`'s `query` option, so `/ws` actually sends the token now.
- `services/karada-rest.ts::getJson()` and `postAction()` route through `withApiToken()` — covers every `/api/karada/*` call (`fetchFullState`, `fetchAnimationManifest`, `resolveDescriptor`, `fetchSkins`, `postAction`). **`activateSkin()` deliberately untouched** — `POST /api/skins/{name}/activate` lives directly on `self.app` in `core/webui.py`, not on `karada_api.py`'s token-gated `rest_router`, so it was never actually gated (checked by reading the route registration, not assumed).
- `services/audio-stream.ts` appends the same query string to the `/api/audio/stream` WS URL. **`services/audio-upload.ts` (`POST /api/audio/upload`) deliberately untouched** — also registered directly on `self.app`, no `_require_api_token` dependency, not gated today. If someone gates it later, this client needs the same treatment.
- `components/settings/SettingsDrawer.vue` gained an "Access token" password input (there was previously no UI at all for the field — it was only reachable via devtools localStorage).
- `stores/connection.ts` gained a `watch(() => useSettingsStore().apiToken, …)` that tears down and re-dials the live `SynthWs` when the token changes, so editing it in Settings takes effect immediately — a `SynthWs` instance otherwise keeps reconnecting with whatever query string it was constructed with, which would've meant "type token, still stuck reconnecting until you reload the page."
- `main.ts`'s debug hook (`window.__stage`) gained a `connection` entry (was `audio`/`chat`/`mic` only) — needed to observe `connection.status` from Playwright/console without a UI; kept, matches the existing "manual console sessions and Playwright scripts drive the stores directly" convention.

**Verified live**, not just typecheck/build, against a scratch backend (`SYNTH_WEBUI_TLS=0 SYNTH_WEBUI_HTTP_PORT=8091 SYNTH_WEBUI_API_TOKEN=stagetest123 uv run python scripts/run_webui.py`, killed after):
- No token configured, server requires one → `connection.status` stays `reconnecting`, `/api/karada/state` → 401.
- Correct token (set via `localStorage.setItem('synth-stage/api-token', …)`, matching what the new UI field writes) → `connection.status` → `connected`, `/api/karada/state` → 200.
- Wrong token → `reconnecting` / 401, same as no token.
- Typing the correct token into the actual Settings-drawer input (Playwright driving the real DOM, not localStorage directly) took the connection from `reconnecting` to `connected` with no page reload — confirms the live-reconnect watcher works, not just the initial-connect wiring.

New script kept for future manual QA: `frontend/scripts/auth-ux-ui-check.mjs` (drives the Settings-drawer token input end-to-end; complements `m6-auth-check.mjs`, which only exercised raw WS URLs, not the app's own store/UI wiring).

Validation: `pnpm typecheck` + `pnpm build` clean (no new errors; the `[INVALID_ANNOTATION]` rolldown/vueuse warnings are pre-existing upstream noise, unrelated to this change). No backend files touched, so no `ruff`/`ty`/`pytest` re-run was needed this round.

**Not done / still open:**
- The full pairing-token flow (server-generated token, QR/URL handoff) from the 2026-07-09 discussion — this pass only wired the *storage → transport* half; token issuance/rotation UX is still manual (paste a token that matches `SYNTH_WEBUI_API_TOKEN` server-side).
- `/api/audio/upload` and `/api/skins/{name}/activate` remain unauthenticated regardless of `SYNTH_WEBUI_API_TOKEN` — noted above, not fixed (out of scope: frontend can't gate what the backend doesn't check; would need a `core/webui.py` change plus the usual backend validation pass).
- Capacitor wrapper — untouched, longer-term item per §6.6.

---

## 11. 2026-07-09 — §6.1 server-side TTS sentence chunking done

Multi-sentence replies to the live webui/stage avatar now stream sentence-by-sentence instead of waiting for the whole reply to synthesize before any audio plays — the perceived-latency win the ported AIRI `playback-manager.ts` was built for but nothing previously exercised. User picked this over barge-in/interrupt (§6.2, still open) when offered a choice between the two remaining scoped `AGENTS.md`/checkpoint next-steps.

**Backend** (`plugins/vox_plugin.py`, `core/animation_handler.py`):
- New `_split_sentences()` — best-effort regex sentence splitter (no abbreviation handling; merges fragments under `min_chars=12` into a neighbour so "Ok." never becomes its own synthesis call).
- New `VoxPlugin._chunking_karada()` — the eligibility gate, deliberately narrow: only when `interface_path` resolves to `synth_webui` **and** the Karada state server has connected clients (Telegram/Discord/etc. still get exactly one clip — N separate voice messages would be a regression there, not an improvement), never when `[em_*]` facial-expression tags are present (their timeline anchors to whole-reply text offsets, which per-chunk synthesis doesn't reproduce), and gated by a new hidden config flag `VOX_SENTENCE_CHUNKING` (default on) as an operator kill switch.
- New `VoxPlugin._speak_chunked()` — synthesizes sentence *N+1* in the background (`asyncio.create_task`) while sentence *N* is being broadcast, then paces the broadcasts with `asyncio.sleep(duration)` so `KaradaStateServer.broadcast_audio`'s talk-animation return-to-idle timer (which resets on every call) stays roughly in step with actual client playback instead of firing early because synthesis outran real-time audio. Returns the concatenated WAV bytes so the rest of `speak()` — file write, lipsync, the persisted chat bubble, Telegram/Discord dispatch — runs completely unchanged, seeing one combined clip exactly as before.
- `KaradaStateServer.broadcast_audio()` gained an optional `turn_id` param (forwarded as-is in the payload; `None` for every existing caller, so this is purely additive). `VoxPlugin._dispatch()` gained `skip_karada_broadcast` so the final whole-reply dispatch doesn't re-broadcast (and double-play) audio that was already streamed chunk-by-chunk.
- `_write_audio` refactored into `_to_wav_bytes` (normalize engine output to WAV bytes in memory, handles both real-WAV and raw-PCM engines) + a thin disk-write wrapper, plus a new `_concat_wav_bytes` for stitching same-format chunks into the one file everything downstream expects.
- **Impact-checked before editing**: `gitnexus_impact("speak", direction="upstream")` came back **HIGH risk / 21 impacted symbols** (radio host, Discord live-tool-calls, message chain, the debug-TTS endpoint, `action_parser`). The eligibility gate means every one of those callers either has `generate_only=True` (radio host, debug endpoint — short-circuits before chunking is even considered) or a non-`synth_webui` interface path — all fall through to the byte-for-byte original single-shot code, confirmed by running the full existing `tests/test_vox_plugin.py` suite unchanged (all previously-green tests still pass) plus a new dedicated chunking test.

**Frontend** (`protocol.ts`, `stores/audio.ts`, `stores/chat.ts`):
- `TtsPlayMessage` gained `turn_id?: string`.
- `stores/audio.ts`: playback-manager's `overflowPolicy` changed from `'steal-oldest'` to `'queue'`; `scheduleTts` now computes a `turnKey` (`msg.turn_id ?? msg.url`) and calls `manager.stopAll()` **only** when the turn key changes — same-turn chunks queue and play back-to-back, a genuinely new turn still interrupts immediately. The `msg.url` fallback means single-shot (non-chunked) replies keep the exact old steal-on-arrival behavior with zero special-casing.
- `stores/chat.ts`: the `tts-play` handler's "attach replay link to the most recent synth bubble" logic now skips entirely when `msg.turn_id` is set — otherwise mid-stream chunk events would overwrite the bubble's click-to-replay link with whichever small per-sentence file happened to broadcast last. The correct combined-clip link still arrives via the caption `message` event (`msg.tts_url`) once the whole reply has streamed, unchanged.

**A real, pre-existing race condition was found and fixed while testing this** (not a regression from chunking, but chunking's turn-interrupt path was the first thing to actually exercise it — full writeup in `AGENTS.md` §12, "Aborting a playback-manager item races its cleanup against the next item's synchronous start"): interrupting one `tts-play` clip with another could make `audio.speaking` clobber back to `false` immediately after the new clip started, because the aborted item's `finally` cleanup (a deferred microtask) ran *after* the new item's synchronous start and stomped its `speaking`/`lipsync` state. Fixed in `stores/audio.ts::playItem` with an `activeItemId` ownership guard — a superseded item's cleanup no longer touches shared state it doesn't own.

**Verified live** with a scratch backend + a Playwright script (`frontend/scripts/tts-chunking-check.mjs`, kept) driving the real Pinia stores via `window.__stage`, not raw WS frames — data-URI silent WAV clips as fixtures so it needs no real TTS engine:
- Three same-turn chunks play as one continuous `speaking` run (no premature cutoff between sentences) and finish cleanly.
- A new turn interrupts a stale/in-flight one with **no** false blip in between (this is what caught the race condition above — first version of this exact check failed until the `audio.ts` fix landed).
- A mid-stream chunk (`turn_id` set) leaves the chat bubble's replay link alone; the final combined-clip `message` event sets it correctly.
- Legacy single-shot `tts-play` (no `turn_id`) still attaches directly to the bubble, unchanged.

Also added: `tests/test_vox_plugin.py::test_vox_plugin_speak_chunks_multi_sentence_reply` (3 real WAV chunks synthesized via a fake engine, asserts 3 `broadcast_audio` calls sharing one `turn_id` and that the final `_dispatch` call has `skip_karada_broadcast=True`), plus three direct `_split_sentences()` unit tests. Three pre-existing `VoxPlugin.__new__(VoxPlugin)`-based tests needed `plugin._sentence_chunking_enabled = False` added (a new `__init__`-set attribute those tests bypass by construction) — a real but mechanical fixture gap, not a design issue.

Validation: `ruff format`/`check` clean on all touched files; `ty check plugins/vox_plugin.py core/animation_handler.py` clean; `pytest tests/test_vox_plugin.py` — 30 passed (the one pre-existing unrelated failure, `test_active_vox_engine_default_is_kitten`, is the documented `AGENTS.md` §12 DB-connectivity/config-registry-pollution flake, confirmed failing identically with my changes stashed out). Frontend `pnpm typecheck` + `pnpm build` clean.

**Not done / still open:**
- §6.2 barge-in/interrupt — still open, was the other option offered and not picked this round. **Done in the following session, see §12.**
- Sentence splitting is regex-based and will mis-split on abbreviations ("Mr. Smith") — acceptable for streaming pause-points, not linguistically precise; documented in the function docstring.
- No cleanup of the extra per-chunk `.wav` files chunking leaves in `res/synth_webui/static/audio/tts/` — but there was **already no cleanup** of any Vox output file before this change (confirmed by reading the whole plugin — `VOX_AUDIO_CACHE_SIZE` is registered but only ever read for a Settings-page display string, never enforced), so this is proportionally more of a pre-existing gap, not a new one. Worth fixing generally if it ever becomes a real disk-usage problem.

---

## 12. 2026-07-09 — §6.2 barge-in/interrupt done

The other half of the choice offered in §11 — picked up next since it's the last remaining *scoped* item (phase-2 realtime, AR, and mobile packaging are all substantially bigger). Direction was agreed in advance when the choice was offered: always-on, trusting the browser's `echoCancellation` rather than gating barge-in behind a settings toggle (the `MicMode` groundwork in `stores/settings.ts` remains unused — still a real option later if real-hardware testing shows AEC isn't reliable enough).

**Change** (`frontend/src/stores/mic.ts`, `frontend/src/stores/audio.ts` — frontend only, no backend changes):
- `getUserMedia` now explicitly requests `{ echoCancellation: true, noiseSuppression: true, autoGainControl: true }` instead of bare `audio: true`. This is the actual mechanism that's supposed to keep the avatar's own voice (played through system speakers) from being picked back up by the mic and misread as user speech — previously the half-duplex guard (see below) was the *only* thing preventing that, achieved by simply never listening while the avatar spoke.
- The half-duplex guard — mic PCM was dropped entirely while `audio.speaking` was true — is removed. PCM now streams to `/api/audio/stream` continuously, VAD included, so the server can actually detect the user starting to talk *during* avatar speech.
- When a `speech_start` VAD signal arrives while `audio.speaking` is true, that's treated as a genuine barge-in: `audio.stopAll('barge-in')` cuts the avatar off immediately, then recording starts exactly as it would for any other utterance.
- `useAudioStore().stopAll()` gained an optional `reason` param (default `'user-stop'`, unchanged for its only prior caller) so the barge-in interrupt shows up distinctly from other stop reasons if anyone inspects `playback-manager`'s interrupt events later.

**What this does *not* attempt**: real acoustic AEC reliability (does the avatar's voice actually stay under Silero's VAD threshold on real speaker/mic hardware) is fundamentally not verifiable by an agent in a sandboxed headless browser — it needs a human on real hardware. This was an explicit, informed trade-off the user made when offered the choice, not an oversight. If it turns out to misfire in practice (avatar barging in on itself), the fallback documented in the discussion is the settings-gated opt-in variant that was *not* chosen this round.

**Verified live** against a scratch backend with `frontend/scripts/barge-in-check.mjs` (kept), launched with Chromium's `--use-fake-device-for-media-stream` flag (same pattern as `m4-voice-check.mjs`):
- `getUserMedia` constraints captured via a monkey-patched `navigator.mediaDevices.getUserMedia` confirm all three AEC flags are actually sent.
- The mic reaches `listening` state and its real `/api/audio/stream` WebSocket opens (`readyState === 1`) against a live backend.
- Since nothing in this environment can produce genuine speech for a fake mic device to pick up, the actual interrupt-decision code path (not just the constraints) was verified by intercepting the *real* WebSocket instance (via a `Proxy` around `window.WebSocket` installed before app code runs) and injecting a synthetic `{"type":"vad","signal":"speech_start"}` server frame while the avatar was mid-clip: `audio.speaking` flipped from `true` to `false` immediately and `mic.userSpeaking` became `true` — confirming the actual `onVad` handler wiring, not a re-implementation of it.

Validation: `pnpm typecheck` + `pnpm build` clean. No backend files touched, so no `ruff`/`ty`/`pytest` re-run needed this round. `gitnexus_detect_changes` — LOW risk, exactly `stores/audio.ts::stopAll` and `stores/mic.ts::start`, nothing unexpected.

**Not done / still open:**
- Real-hardware/human verification of AEC effectiveness — flagged above, can't be done by an agent.
- §6.3 phase-2 realtime audio-to-audio, §6.5 AR, §6.6 mobile packaging remain the substantially-bigger unstarted items.
