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

Persistence:

- ``DB_HOST`` / ``DB_PORT`` / ``DB_USER`` / ``DB_PASS`` / ``DB_NAME``
- ``SOUL_REPOSITORY_BACKEND``
- ``SOUL_POSTGRES_DSN``

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
