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
| M5 — polish | Skin selector with real thumbnails + correct active-skin highlight, settings drawer, theme hue slider (live), loading screen with real byte progress, transparent mode (`?transparent=1`, confirmed via computed style) | `m5-polish-check.mjs` |
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
