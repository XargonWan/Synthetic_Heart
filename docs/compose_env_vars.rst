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
- PostgreSQL runtime connection and port values (``DB_*``, ``EXT_DB_PORT``)
- legacy MySQL source values used only for first-boot migration (``SOURCE_DB_*``)
- SOUL Postgres override settings (``SOUL_*``) when you intentionally want a separate DSN
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

- ``DB_HOST`` / ``DB_PORT`` / ``DB_USER`` / ``DB_PASS`` / ``DB_NAME``
- ``LEGACY_SOUL_POSTGRES_DSN`` (optional one-time SOUL migration source)
- ``SOUL_POSTGRES_DSN`` (legacy alias for the SOUL migration source DSN)
- ``SOURCE_DB_HOST`` / ``SOURCE_DB_PORT`` / ``SOURCE_DB_USER`` / ``SOURCE_DB_PASSWORD`` / ``SOURCE_DB_NAME``
- ``SYNTH_DB_BACKUP_ENABLED`` / ``SYNTH_DB_BACKUP_INTERVAL_HOURS`` / ``SYNTH_BACKUPS_DIR``

Primary DB selection
--------------------

The default deployment now uses a single PostgreSQL runtime database.
``DB_*`` points at that Postgres service, and SOUL uses the same runtime DB by default.

- ``DB_*`` points at the active runtime PostgreSQL service; no extra runtime DB selector is needed in the default Docker stack.
- SOUL persists into that same runtime Postgres automatically.
- ``LEGACY_SOUL_POSTGRES_DSN`` can point at an older standalone SOUL Postgres so startup can import it into the runtime DB.
- ``SOUL_POSTGRES_DSN`` remains accepted as a legacy alias for that migration source.
- ``SOURCE_DB_*`` is only used by the first-boot migration flow that imports a
  legacy MariaDB/MySQL deployment.

Automatic cutover and backups
-----------------------------

- Legacy MySQL → Postgres cutover is enabled by default in the Docker stack and uses ``SOURCE_DB_*`` as the preserved source.
- Legacy standalone SOUL Postgres → runtime Postgres cutover runs first when ``LEGACY_SOUL_POSTGRES_DSN`` or its legacy alias is set.
- ``SOURCE_DB_*`` identifies the preserved legacy MariaDB source used only for migration and verification.
- ``SYNTH_DB_BACKUP_ENABLED=1`` enables the embedded application-owned backup scheduler.
- ``SYNTH_DB_BACKUP_INTERVAL_HOURS=24`` controls the pg_dump cadence.
- ``SYNTH_BACKUPS_DIR`` selects where runtime and legacy archival dumps are written inside the synth container. The WebUI Settings tab also exposes a manual backup action that writes to this same directory. The manual backup action now also downloads the resulting archive to the browser, and a companion action can back up a specific list of tables (see :doc:`api_endpoints`).

Logging & retention
-------------------

The on-disk log naming scheme, rotation, gzip compression, and retention are
documented in full in :doc:`logging`. The relevant environment variables are:

- ``LOG_DIR`` — directory that holds all log files (default ``logs``).
- ``LOG_RETENTION_DAYS`` — days of logs to keep (plain + gzip combined) before
  deletion (default ``7``). Today and yesterday stay plain text; older days
  within the window are gzip-compressed; anything past the window is deleted.
- ``LOGGING_LEVEL`` — root logging level for the main runtime (default ``INFO``).

Because logging is initialised during bootstrap (before the database), these
are environment variables rather than runtime config-registry keys.

Legacy migration note
---------------------

For users migrating from older Synthetic Heart installations:

- ``DB_*`` now points at the active runtime PostgreSQL service. All new runtime data is written there.
- ``SOURCE_DB_*`` is only needed when you still have a legacy MariaDB/MySQL deployment to import from. The Docker stack preserves that source service and uses it only during first-boot migration.
- ``LEGACY_SOUL_POSTGRES_DSN`` (or legacy alias ``SOUL_POSTGRES_DSN``) is optional and used only when you have an older standalone SOUL PostgreSQL database that should be imported into the new runtime DB.
- If both legacy sources are configured, the SOUL Postgres import runs first, then the legacy MariaDB migration.
- After migration, the application continues using the runtime Postgres database from ``DB_*``; the legacy source settings are not used for normal operation.
- The legacy containers/services are preserved for verification and rollback, but the application no longer writes new runtime state to them.

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

Generation / timeout tuning
---------------------------

These control how long the synth waits for a single LLM generation. They matter
most on slow hardware (CPU-only or older GPUs) and with local ``llama.cpp`` /
LM Studio backends, where a long reply can take minutes. If the cap is too low
the synth aborts the request mid-generation, which closes the HTTP connection
and makes ``llama.cpp`` cancel the in-flight task (its log shows
``stopping wait for next result due to should_stop condition`` /
``stop: cancel task``).

- ``LLM_GENERATION_TIMEOUT_SEC`` — primary knob. Max seconds to wait for one
  cortex generation before aborting. Default ``1800`` (30 min). Raise it if
  long replies on slow hardware get cut off. A per-endpoint
  ``extra_config["timeout"]`` still overrides this for that endpoint.

The following outer guards must stay **above** ``LLM_GENERATION_TIMEOUT_SEC`` or
they become the new invisible cap (they are raised to match by default):

- ``RESPONSE_TIMEOUT`` — outer wait before the fallback message is sent
  (default ``2100``).
- ``AWAIT_RESPONSE_TIMEOUT`` — wait for a corrected reply (default ``2400``).
- ``LLM_CHAIN_LEASE_TIMEOUT_SEC`` — force-release of the global LLM chain lease
  (default ``2400``).

.. note::

   ``llama.cpp`` has its own server-side ``--timeout`` argument that the synth
   cannot set. If you raise ``LLM_GENERATION_TIMEOUT_SEC`` for very long
   generations, also start the ``llama.cpp`` server with a matching or larger
   ``--timeout`` (e.g. ``--timeout 1800``) so the server does not cancel first.

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
