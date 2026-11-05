Instruction Block Budgets
=========================

Overview
--------
Every LLM turn in SyntH carries one shared JSON instruction block, rendered by
``core.prompt_engine.load_json_instructions()``. It is the same block on the
manual chat route, on every G.R.I.L.L.O. beat, on the delivery turn and on the
observer outreach — so its size is a cost paid on every turn everywhere.

That block is treated as a budget:

- the rule text lives in one place, ``core/prompt_instructions/rules.py``;
- each route selects the rules it can act on
  (``core/prompt_instructions/routes.py``), with route-specific clauses in
  ``core/prompt_instructions/overlays.py``;
- the assembled string is one minified line, and each route has a character
  ceiling (``core/prompt_instructions.INSTRUCTION_BUDGETS``) asserted by
  ``tests/test_prompt_instruction_budget.py``.

Layout
------
::

    core/prompt_instructions/
        __init__.py     build_instructions(route), INSTRUCTION_BUDGETS, naming hint
        rules.py        one constant per rule + RULE_ORDER + RULES
        routes.py       ROUTE_* ids and the route -> rule-id table
        overlays.py     per-route clauses (vessel speak, voice register)

Routes
------
A *route* is decided by WHICH builder is assembling the turn plus structural flags
that builder already computed (the beat type the beat declared, the
Grillo-internal verdict, the Vessel probe, the input source). Message **content**
is never inspected, so the routing stays safe in a multi-language deployment and
cannot be steered by what someone says.

.. list-table::
   :header-rows: 1

   * - Route
     - Decided by
     - Rule set
   * - ``chat``
     - default conversation turn
     - the shared set (the superset)
   * - ``chat_voice``
     - ``context_memory["is_voice_input"]``
     - shared set + the spoken-register overlay
   * - ``vessel``
     - ``core.vessel_focus.is_vessel_turn``
     - shared set + the in-world speak overlay
   * - ``grillo_internal``
     - ``is_grillo_internal``
     - shared set minus the reply obligation and the human-chat example
   * - ``observer``
     - the beat declared ``beat_type == "observer"``
     - shared set minus the human-chat example
   * - ``delivery``
     - ``build_delivery_request`` / the delivery payload
     - shared set minus the emotion obligation and the human-chat example
   * - ``live``
     - ``build_live_prompt_request``
     - shared set + the spoken-register overlay
   * - ``agent``
     - ``AgentLoopManager._build_agent_prompt``
     - its own tool-calling text (unchanged)

Why a rule is routed away
-------------------------
A rule is moved to a route's overlay (or excluded from it) when the route can
provably never act on it. That is a size decision, not a behaviour change:

- ``VOICE INPUT STYLE`` is conditional on ``input.payload.input_source == "voice"``.
  A text turn or an internal beat has no ``input`` payload at all, so the
  condition cannot hold there; the rule switched itself off after costing its
  characters.
- The vessel speak clause names a ``vessel_* say`` action. On a disconnected turn
  the action catalog contains only ``vessel_connect``, and the same catalog tells
  the model to use exactly one of the listed names — so the clause names an action
  that does not exist on that turn.
- ``CHAT REPLY REQUIRED`` contradicts the Grillo guard on an internal beat, which
  forbids any ``message_*`` / ``send_message`` action. ``INPUT METADATA`` is
  *kept* there, because an internal beat's own user body does carry the
  ``[lang:… | grillo:true | beat:…]`` bracket.

If a route cannot be determined, the shared set renders — the superset — so a
misclassification costs a few hundred characters rather than a missing rule.

Budgets
-------
Character ceilings for the rendered instruction string, per route:

.. list-table::
   :header-rows: 1

   * - Route
     - Ceiling (chars)
   * - ``chat``
     - 5100
   * - ``chat_voice``
     - 5500
   * - ``vessel``
     - 5600
   * - ``grillo_internal``
     - 4350
   * - ``observer``
     - 4600
   * - ``delivery``
     - 4200
   * - ``live``
     - 5500
   * - ``agent``
     - 5100

Each ceiling is the measured size of that route plus roughly 400 characters of
headroom, so ordinary wording tweaks pass and real growth fails. Raise a ceiling
deliberately, in a commit that says why.

History
-------
The block previously had no effective bound. It had grown to 8 377 characters
(measured byte-identical to the body of a live turn's prompt) while the only guard
was a single stale assertion of ``< 7500`` elsewhere in the suite, which was
failing. The shared set is now 4 687 characters — a 44 % reduction that applies to
every turn — with the per-route budgets above enforcing the new shape.

See also
--------
- ``docs/prompt_pipeline.rst`` — the ``PromptRequest`` / renderer pipeline.
- ``docs/prompt_engine_time.rst`` — the Reality Anchor block and its per-turn line.
- ``docs/prompt_engine_json_prompt.rst`` — the assembled JSON prompt contract.
- ``scripts/prompt_blocks.py`` — print the block-level composition of any live
  Langfuse trace, which is how these sizes are measured.
