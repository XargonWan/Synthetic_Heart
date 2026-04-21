Local Time in Prompts
======================

Overview
--------
This project optionally adds structured local time context to prompts built by
``core.prompt_engine.build_prompt_request``.

Fields added (when enabled)
---------------------------
- ``local_time``: string in ``HH:MM`` 24-hour format (example: ``"04:30"``). No timezone name or UTC markers are included.
- ``local_hour``: integer hour (0-23)
- ``time_of_day``: categorical label, one of ``night``, ``early_morning``, ``morning``, ``afternoon``, ``evening``, ``late_evening`` (``early_morning`` corresponds to 04:00–05:59).
- ``local_date``: optional date string ``YYYY-MM-DD`` for local date context.

In the typed prompt path, the authoritative local date/time is folded into the
runtime context used by renderers, so the current turn can be prefixed with a
compact local timestamp without exposing timezone names or offsets.

Configuration
-------------
- ``INCLUDE_LOCAL_TIME_IN_PROMPTS`` (component: ``prompt_engine``) — boolean, default ``True``. When ``False``, the fields above are not included.

Privacy & Implementation Notes
------------------------------
- No timezone names, offsets, or UTC timestamps are included in prompts by default to avoid leaking location information. If the session sets a timezone in session meta (``session_meta`` key ``timezone``), it is used to compute the local time, otherwise the server TZ configured via the project is used.
- The mapping of labels is deterministic and test-covered. Service operators can disable the feature via the config var for privacy-sensitive deployments.
- ``build_json_prompt()`` is now a deprecated alias kept for compatibility.

Testing
-------
Unit tests are provided under ``tests/test_time_zone_utils.py`` and ``tests/test_prompt_engine_time_fields.py``.
