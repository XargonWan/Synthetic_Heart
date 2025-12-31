Compose / Environment variables
=================================

This document lists environment variables that can be provided to the
`docker-compose` file to customize runtime behaviour. All variables below
have safe defaults (either defined by the Compose file itself or by code)
so the container can start without an explicit `.env` file. Override any
value in your own `.env` when necessary.

Note: most variables are documented in `.env.example` as well. This file
focuses on the variables that are referenced in `docker-compose.yml`.

Variables
---------

ROOT_PASSWORD
~~~~~~~~~~~~~
- **Purpose**: Root password used by the Web Desktop (Selkies/VNC) process.
- **Default**: "password" (compose fallback)
- **Recommendation**: Change in production deployments; safe to omit in dev.
 
.. note::

	This project keeps `ROOT_PASSWORD` explicitly present in the `environment:`
	block of `docker-compose.yml` to make it obvious for operators that the
	Web Desktop root credential exists and should be set in production. Even
	if you omit it, the compose fallback will apply the default value.

DISPLAY_WIDTH, DISPLAY_HEIGHT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **Purpose**: Virtual display resolution for Selkies / Web Desktop.
- **Default**: 1280x720
- **Recommendation**: Leave defaults for local dev; set to higher values if you need a larger remote desktop.

SYNTH_WEBUI_HTTP_PORT, SYNTH_WEBUI_HTTPS_PORT
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **Purpose**: Ports used by the Synthetic Heart Web UI (HTTP/HTTPS).
- **Default**: HTTP 8001, HTTPS 8000 (compose fallback); web UI has additional internal defaults in code.
- **Recommendation**: Keep defaults locally; set explicit values in multi-service hosts or when port conflicts exist.

SYNTH_WEBUI_HOST
~~~~~~~~~~~~~~~~
- **Purpose**: Host binding for the web UI server (e.g. 0.0.0.0).
- **Default**: 0.0.0.0
- **Recommendation**: Keep default unless you want to restrict listening to localhost.

SECURE_CONNECTION / SYNTH_WEBUI_TLS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- **Purpose**: Toggle TLS/HTTPS for Selkies and/or the Web UI.
- **Default**: TLS enabled in dev for the Web UI (see `.env.example`).
- **Recommendation**: Keep TLS enabled in production; for local development a self-signed certificate is generated if no cert is provided.

**Default behaviour details**

- **SYNTH_WEBUI_TLS**: If not set, the application falls back to enabling TLS by default (value `1`) to provide a secure developer experience. You only need to set this to `0` when you explicitly want to disable HTTPS.
- **SYNTH_WEBUI_CERT_DIR**: If no certificate/key are provided, the Web UI will look for certificates in `/config/ssl` and, if missing, will attempt to generate a self-signed certificate there at startup.

DB_HOST
~~~~~~~
- **Purpose**: Hostname of the MariaDB service used by the application.
- **Default**: `synth-db` (compose fallback)
- **Recommendation**: For multi-container setups keep `synth-db` or set this to an external host if you run the DB elsewhere.

CLI_SERVER_PORT
~~~~~~~~~~~~~~~
- **Purpose**: Port mapping for an internal CLI server used by tooling and integrations.
- **Default**: 5555 (compose fallback; can be overridden in `.env`)
- **Recommendation**: Typically safe to leave at default for local dev.

Why these were removed from explicit compose environment list
-----------------------------------------------------------
Each variable above has been removed from the `environment:` block in
`docker-compose.yml` to avoid duplicating values that already have sensible
fallbacks in Compose (`${VAR:-fallback}`) or in the application code.
Keeping them documented here ensures maintainers know what can be set if
customisation is required.

Best Practices
--------------
- Keep a `.env` (not checked into git) for environment-specific overrides.
- Use `.env.example` to show recommended/default values for contributors.
- Lock down passwords and TLS certs in production and avoid using defaults.

If you want, I can move these entries back into `docker-compose.yml` with
clear comments and a link to this document, or add CI checks that validate
expected ports are reachable after a compose up.
