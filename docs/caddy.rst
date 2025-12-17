Caddy reverse proxy
===================

We provide a simple local Caddy reverse proxy configuration used in development to handle TLS termination, path-based routing and WebSocket passthrough.

Features:

- Routes `/webtop` to the internal Selkies/Webtop server (synth-dev:3000)
- Routes `/` to the SyntH WebUI (`synth-dev:${SYNTH_WEBUI_PORT}`)
- Uses `tls internal` by default (self-signed CA for dev). For production change `tls internal` to `tls you@example.com` in `caddy/Caddyfile`.

Quick start (development):

1. Ensure `.env-dev` contains `CADDY_EMAIL=internal` (default) and `SYNTH_WEBUI_PORT`.
2. Start services:

   docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build caddy synth-dev

3. Verify:

   curl -v http://127.0.0.1/    # Will redirect to HTTPS
   curl -k https://127.0.0.1/   # -k accepts the dev self-signed cert

Notes
-----
- The proxy exposes port `443` and also maps host port `9009` to container port `443` for compatibility with older setups expecting TLS on 9009.
- Caddy's `reverse_proxy` handles WebSocket upgrade automatically; no extra config is required.
