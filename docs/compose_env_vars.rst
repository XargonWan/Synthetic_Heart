Compose / Environment Variables
================================

Synthetic Heart accepts configuration from three places, in this order:

1. Environment variables
2. Persisted values in the ``config`` table / WebUI settings
3. Hard-coded defaults in code

The project now ships a trimmed ``.env.example`` focused on the overrides most
operators actually touch during setup. Advanced and low-frequency variables are
documented here instead of being dumped into the example file.

What belongs in ``docker-compose`` vs ``.env``
----------------------------------------------

``docker-compose.yml`` only references a small subset of the full runtime
configuration surface:

- container basics such as ``IMAGE_VERSION``, ``PUID``, ``PGID`` and ``TZ``
- MariaDB connection and port values (``DB_*``, ``EXT_DB_PORT``)
- SOUL Postgres settings (``SOUL_*``, ``EXT_SOUL_DB_PORT``)
- WebUI port and TLS-related values (``SYNTH_WEBUI_*``)
- Selkies / desktop credentials such as ``ROOT_PASSWORD``

Most other variables are still valid in ``.env`` because the config registry
loads environment overrides for registered settings at startup.

High-value variables most operators actually set
------------------------------------------------

Core runtime:

- ``BASE_CORTEX``
- ``GRILLO_CORTEX``
- ``TRAINER_CORTEX``
- ``LIVE_CORTEX``
- ``SYNTH_NAME``
- ``SYNTH_PROFILE``
- ``TRAINER_IDS``
- ``TRAINER_CHAT_ID``

Provider credentials:

- ``GEMINI_API_KEY``
- ``OPENAI_API_KEY``
- ``BOTFATHER_TOKEN``
- ``DISCORD_BOT_TOKEN``
- ``MATRIX_*``

- **SYNTH_WEBUI_TLS**: If not set, the application falls back to enabling TLS by default (value `1`) to provide a secure developer experience. You only need to set this to `0` when you explicitly want to disable HTTPS.
- **SYNTH_WEBUI_CERT_DIR**: If no certificate/key are provided, the Web UI will look for certificates in `/config/ssl` and, if missing, will attempt to generate a self-signed certificate there at startup.
- **SYNTH_ATTACHMENTS_ROOT**: Optional root directory for Web UI attachments. If set, uploaded files are stored there. If unset, the application will use `$XDG_DATA_HOME/attachments` when `XDG_DATA_HOME` is defined, otherwise `/config/uploads`.
- **Image seed**: The container image ships a default self-signed certificate and key in `/config/ssl` (copied from `/app/res/default_ssl`) so HTTPS works out-of-the-box unless a volume overwrites that path.

Persistence:

- ``SYNTH_PRIMARY_DB`` (``memory`` -> ``DB_*`` MariaDB, ``soul`` -> ``SOUL_*`` / ``SOUL_POSTGRES_DSN``)
- ``DB_HOST`` / ``DB_PORT`` / ``DB_USER`` / ``DB_PASS`` / ``DB_NAME``
- ``SOUL_REPOSITORY_BACKEND``
- ``SOUL_POSTGRES_DSN``

Primary DB selection
--------------------

Use ``SYNTH_PRIMARY_DB`` when your deployment keeps both the legacy MariaDB
settings and the SOUL PostgreSQL settings in the same ``.env`` and you want an
explicit, exclusive switch for the app's primary runtime database.

- ``SYNTH_PRIMARY_DB=memory`` forces the application to use ``DB_*`` as the
  active MariaDB connection settings and ignores Postgres DSNs for the primary
  runtime DB.
- ``SYNTH_PRIMARY_DB=soul`` forces the application to use
  ``SOUL_POSTGRES_DSN`` and optional ``SOUL_PG_*`` overrides for the active
  PostgreSQL connection settings.

If ``SYNTH_PRIMARY_DB`` is unset, the application falls back to the older
driver-based behavior using ``SYNTH_DB_TYPE`` / ``DB_TYPE`` plus ``DB_*`` /
``DATABASE_URL``.

Observability:

- ``LANGFUSE_ENABLED``
- ``LANGFUSE_HOST`` / ``LANGFUSE_BASE_URL``
- ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``
- ``CORTEX_API_LOG_ENABLED``

Prompt / runtime behavior:

- ``PROJECT_DEFAULT_LANGUAGE`` / ``PROJECT_DEFAULT_TONE``
- ``PROMPT_LITE_MODE``
- ``UNIFIED_HISTORY``
- ``ENABLE_RECON`` / ``ENABLE_DEBRIEF``
- ``EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS``

Why the example file is now smaller
-----------------------------------

``.env.example`` is now intentionally opinionated. It keeps the day-one,
operator-facing settings close at hand and leaves the rarer knobs to this
document.

Use this page for:

- advanced prompt / history / recon settings
- live-session tuning knobs
- Grillo, agent, memory, and weather internals
- path, storage, migration, and debugging overrides
- interface-specific flags beyond the basic credentials

Best practices
--------------

- Keep your real ``.env`` out of version control.
- Uncomment only the values you want to override.
- Prefer the WebUI for day-to-day tuning once the system is running.
- Reserve rarely used path and internal overrides for debugging or container
  customization.
