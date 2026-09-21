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

Reality Anchor: the system block and the per-turn line
------------------------------------------------------
The authoritative temporal context reaches the model twice, from one source:

- **The full block.** ``[SYSTEM: REALITY ANCHOR]`` (current date, weekday, time,
  season, location, plus the stable ``Temporal Delta`` sentence) is built by
  ``core.prompt_engine._build_context_summary`` and merges into the **system**
  message, where it sits next to the memories and situational notes.
- **The per-turn line.** ``core.prompt_engine._build_current_turn_anchor``
  compresses the same facts to one line, carried on
  ``RuntimeContext.reality_anchor`` and rendered by every renderer immediately
  above the current user turn, on its own line, ahead of the ``[lang:…]``
  routing bracket.

Both are built by the *same* formatters (``_pretty_anchor_date`` /
``_pretty_anchor_time``), so the block and the line cannot disagree about the
date. The line deliberately omits the boilerplate ``Temporal Delta`` sentence,
which the block already carries, and renders nothing at all when the turn has no
temporal fields.

Why the duplication exists: the system message is large. Measured on a live turn
before the instruction-budget work, the anchor block sat at character 11 115 of a
15 406-character system message — roughly 11 000 characters from the text being
generated, where an authoritative timestamp has little grip. The per-turn line
puts the same facts at the point of generation. After the instruction-block
compression the block sits at about character 4 800 of a ~10 000-character system
message, so the compression helps on its own; the line is what makes it reliable.

The exact clock **is** on the per-turn line (that is what makes the anchor
authoritative for "what time is it" and for scheduling), while the ``[lang:…]``
routing bracket continues to omit absolute timestamps. Both halves are asserted:
``tests/test_prompt_renderers.py`` pins the anchor's presence on all seven
renderer paths *and* that the bracket still carries no absolute timestamp.

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
