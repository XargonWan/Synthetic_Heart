# SyntH Stage

A standalone Vue 3 frontend for SyntH: the avatar stage, chat, and live-voice
client. Talks to the SyntH backend over the existing `/ws` (chat + Karada
avatar events), `/api/karada/*` (avatar state/assets), and `/api/audio/stream`
(mic → VAD/STT). The legacy webui (`core/webui_templates/` +
`res/synth_webui/js/`) is untouched and keeps working alongside this app —
both are Karada clients.

## Toolchain exception

This directory is the **one Node.js corner of an otherwise uv-only Python
repo** (see `CLAUDE.md`). It uses Node ≥ 22 and pnpm. None of the Python
tooling rules apply in here; conversely, never run `uv`/`pip` in here.

```bash
pnpm install
pnpm dev        # http://localhost:5173 — proxies /api, /ws, /skins, /uploads, /avatars to the backend
pnpm build      # emits dist/ — served by FastAPI at /stage when present
pnpm typecheck
```

The dev proxy targets `https://localhost:8080` by default (self-signed TLS is
accepted by the proxy, so the browser never sees it). Override with
`SYNTH_BACKEND_ORIGIN=https://host:port pnpm dev`.

## Production

`pnpm build` produces `frontend/dist/`. It is served by the `official/synth_stage`
extension at `/stage/` (same origin, so no CORS): `scripts/build_stage_extension.sh`
builds it and copies it into the extension's `web/` folder (in the official plugins
repository), which the extension serves once it is on and granted `web.static`. The backend runs fine without it.

## URL flags

- `?transparent=1` — transparent background, minimal chrome. For OBS browser
  sources and desktop-companion overlays.

## Attribution

Portions ported from [Project AIRI](https://github.com/moeru-ai/airi) (MIT).
See `NOTICE.md` for the license text and the per-file port table.
