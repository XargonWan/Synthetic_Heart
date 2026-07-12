Self-Growth System
==================

Overview
--------

The **Self-Growth** system lets SyntH periodically reflect on how she has grown
and who she is becoming, and rewrite — in her own voice — a single free-form
*self-growth reflection*. That reflection is injected into every prompt next to
the persona background, so it gradually becomes part of her evolving sense of
self.

The feature is implemented as a weekly G.R.I.L.L.O. agent
(:doc:`grillo_plugin`) in ``plugins/grillo/grillo_growth.py`` and a shared state
layer in ``core/growth_state.py``.

Key properties:

- **Reflective, not reactive.** The weekly context is built from the last seven
  days of diary entries and keyword-driven long-term memory recall. It never
  includes raw chat history and never performs a web search.
- **Single evolving blob.** There is exactly one *current* self-growth
  reflection at a time. Past states are kept as a rolling history (max 10 rows)
  for review and rollback.
- **Grounded output.** The rewrite is guarded so it names concrete moments from
  the week's diary rather than producing abstract filler (see
  :ref:`self-growth-quality-guard`).
- **Likes / dislikes.** Alongside the reflection, SyntH may also rewrite her
  ``SYNTH_LIKES`` / ``SYNTH_DISLIKES`` config vars (replaced wholesale).

Modes
-----

Behaviour is controlled by the ``GROWTH_MODE`` config var:

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Mode
     - Behaviour
   * - ``off``
     - The weekly run never fires.
   * - ``on``
     - The rewrite is applied directly to the current state (default).
   * - ``request``
     - A proposal is sent to the trainer / configured ``interface_path``,
       stored as *pending*, and applied only after the operator approves it in
       chat.

.. _self-growth-request-mode:

Request mode (trainer approval)
-------------------------------

When ``GROWTH_MODE=request``, SyntH does **not** apply the rewrite on her own.
Instead, at the end of a growth cycle she delivers a proposal for approval and
keeps it pending until the operator decides:

1. The cycle builds the proposal (new self-growth reflection + likes/dislikes)
   exactly as in ``on`` mode.
2. ``_deliver_proposal`` picks a delivery target:

   - ``GROWTH_REQUEST_INTERFACE_PATH`` if set, otherwise
   - ``TRAINER_CHAT_ID`` as a fallback.

3. The proposal is **persisted as pending** in the ``GROWTH_PENDING_PROPOSAL``
   config key (never in ``growth_states`` yet). SyntH then delivers, through
   ``core.auto_response.request_llm_delivery``, a message that presents both the
   proposed reflection **and the likes/dislikes changes** (a before/after diff),
   and instructs her to apply it only once the trainer approves — via the
   ``apply_growth_proposal`` action.
4. **While a proposal is pending**, a reminder is injected into every prompt
   (static injection key ``self_growth_pending_proposal``) so SyntH stays aware
   that an approval is outstanding.
5. **On the trainer's decision**, SyntH emits ``apply_growth_proposal``:

   - ``approve=true`` (default) — commits the pending proposal: writes it to
     ``growth_states`` (``source="approved"``), applies the likes/dislikes, and
     clears ``GROWTH_PENDING_PROPOSAL``.
   - ``approve=false`` — discards the pending proposal: clears
     ``GROWTH_PENDING_PROPOSAL`` and writes nothing to ``growth_states``.

If neither ``GROWTH_REQUEST_INTERFACE_PATH`` nor ``TRAINER_CHAT_ID`` is
configured, delivery is skipped and a warning is logged; no proposal is sent.

Actions
-------

The plugin exposes two LLM actions (plus ``static_inject``):

.. list-table::
   :header-rows: 1
   :widths: 26 20 54

   * - Action
     - Optional fields
     - Effect
   * - ``run_self_growth``
     - ``dry_run``
     - Runs a growth cycle on demand (``dry_run`` builds the proposal without
       applying or delivering it).
   * - ``apply_growth_proposal``
     - ``approve``
     - Resolves the pending proposal. ``approve=true`` (default) commits it;
       ``approve=false`` discards it. String values ``false`` / ``0`` / ``no`` /
       ``off`` are treated as ``False``.

Configuration
-------------

All keys are exposed in the WebUI under the G.R.I.L.L.O. component and stored in
the config registry.

.. list-table::
   :header-rows: 1
   :widths: 32 14 54

   * - Config key
     - Default
     - Purpose
   * - ``GROWTH_MODE``
     - ``on``
     - ``off`` / ``on`` / ``request`` (see above).
   * - ``GROWTH_WEEKLY_DAY``
     - ``Sunday``
     - Day of the week the weekly reflection runs.
   * - ``GROWTH_WEEKLY_TIME``
     - ``03:00``
     - Local time (HH:MM) the weekly reflection runs.
   * - ``GROWTH_REQUEST_INTERFACE_PATH``
     - *(empty)*
     - ``interface_path`` to send the proposal to in ``request`` mode. Falls
       back to the trainer chat if empty.
   * - ``GROWTH_DIARY_DAYS``
     - ``7``
     - How many days of diary entries to reflect on.
   * - ``GROWTH_MEMORY_LIMIT``
     - ``8``
     - Max long-term memories recalled for the reflection.
   * - ``GROWTH_PENDING_PROPOSAL``
     - *(empty)*
     - Internal. Holds the serialized pending proposal in ``request`` mode.
       Set when a proposal is delivered, cleared once it is approved or
       discarded. Not meant to be edited by hand.

Storage
-------

State lives in the ``growth_states`` table (created lazily by
``core/growth_state.py::ensure_growth_table`` and seeded by ``init-db.sql``):

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Column
     - Type
     - Meaning
   * - ``id``
     - serial / int
     - Primary key.
   * - ``content``
     - text
     - The self-growth reflection text.
   * - ``created_by``
     - text
     - Origin (``grillo_growth`` for the weekly agent).
   * - ``source``
     - text
     - How it was produced (``weekly``, manual, …).
   * - ``is_current``
     - boolean
     - Exactly one row is flagged current at a time.
   * - ``created_at``
     - timestamp
     - Creation time.

Only the newest ``MAX_GROWTH_HISTORY`` (10) rows are retained.

Prompt injection
-----------------

``core/persona_manager.py`` reads the current reflection via
``core.growth_state.get_current_growth`` and, if non-empty, adds it to the
static injection under the ``self_growth`` key. ``core/prompt_engine.py`` then
renders it into the system prompt as a dedicated block, framed as *"how you have
grown and who you are becoming over time; treat it as part of your current sense
of self"*. If there is no current state, no block is added.

Manual & scheduled runs
------------------------

- **Scheduled**: a background scheduler fires once a week at
  ``GROWTH_WEEKLY_DAY`` / ``GROWTH_WEEKLY_TIME`` (skipped when
  ``GROWTH_MODE=off``).
- **Manual (WebUI "Run Now")**: ``run_now`` triggers a cycle immediately,
  bypassing the schedule and the ``off`` gate (``force=True``), while still
  honouring ``GROWTH_MODE`` for delivery.
- **Actions**: ``run_self_growth`` runs a cycle on demand and
  ``apply_growth_proposal`` resolves a pending proposal — see `Actions`_ above.

.. _self-growth-quality-guard:

Output-quality guard
--------------------

The rewrite is produced by the active cortex. Because some engines are
unconstrained and can return either malformed JSON or valid-but-abstract text,
``_ask_llm_for_rewrite`` applies two application-level guards (not keyword/intent
matching):

- **JSON-shape retry.** If the reply cannot be parsed into a single JSON object,
  a single stricter retry is attempted (and a single-item ``[{...}]`` array is
  unwrapped) before giving up.
- **Grounding retry.** If the parsed reflection reads as abstract filler (shares
  too few tokens with the week's diary, or matches a known filler marker), a
  single retry is attempted with concrete diary excerpts injected, demanding the
  note be built on real moments from the week.

These guards recover the common failure modes of unconstrained engines without
looping; if the grounding retry still comes back abstract, the note is accepted
as-is rather than discarding the whole reflection.
