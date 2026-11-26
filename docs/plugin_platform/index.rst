Plugin Platform — Architectural Research
=========================================

.. note::

   **Status: research report, not an implementation.** Nothing described as a
   *proposal* here exists in the code yet. This report changes no application
   code and migrates nothing. It is the input for a later implementation phase
   that can be split into small, independently testable milestones
   (see :doc:`migration_plan`).

Why this report exists
----------------------

Synthetic Heart (SyntH) grew as an application with embedded functionality:
interfaces, engines, avatar renderers, dashboards and integrations live inside
the main repository and import ``core.*`` directly. The goal of the platform
work is a **small, stable Core** plus **independently installable extensions**
that can only reach the Core through a **public, versioned, permission-checked
API**.

The brief this report answers is the draft ``TODO - Draft Synthetic_Heart —
Plugin Architecture .md`` in the repository root. The investigation was done
on 2026-11-23 against branch ``develop``.

How to read the evidence tags
-----------------------------

Every significant statement carries one of these tags, so verified facts,
maintainer decisions and recommendations are never mixed:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Tag
     - Meaning
   * - **Verified**
     - Read in the source or documentation during this investigation. A
       ``file::symbol`` reference is given. Line numbers are those of the
       2026-11-23 checkout and drift over time; the symbol name is the stable
       reference.
   * - **Decision**
     - Taken by the maintainers while this plan was drafted. Not re-opened here.
   * - **Proposal**
     - A recommendation of this report. Names such as RPC method names are
       illustrative, not a frozen contract.
   * - **Unknown**
     - Could not be established from the available code or docs. The entry says
       what would be needed to settle it.

Maintainer decisions that frame the whole report
------------------------------------------------

#. **Everything non-core is an extension, and every extension runs in its own
   process.** Official extensions included. The only channel to the Core is the
   public RPC API, enforced inside the Core on every call. Python cannot
   enforce a boundary inside one process (``sys.modules``, ``gc``, ``ctypes``,
   ``import core.db``), so the process boundary is the security boundary.
#. **No bypasses.** In particular nobody bypasses the message chain. If an
   extension cannot do something with the public API, either a new API is
   proposed to the maintainers, or the capability is large and central enough to
   become part of the Core. It never gets a shortcut.
#. **"Plugin" and "interface" stop being separate categories.** An extension
   declares what it provides (an interface, actions, an engine, hooks, UI slots,
   a Vessel adapter, ...).
#. **One file is one plugin**: an uncompressed zip archive.
#. **Extension data lives in the Core database**, but extensions never touch the
   database; they use the ``storage`` API only.
#. **Extensions do not add containers.** The single exception is the Zen LLM
   Engine container, which SyntH ships because it is free and needs no API key.
#. **Core:** GRILLO, SOUL, Cortex, goals, the Karada State Server, statistics,
   the calendar and event system, the WebUI, and the Rift Vessel.
   **Extensions:** every engine (LLM, Vox, Auris, Iris, Live), every interface
   including Telegram, Zen, VRM rendering, Vessel adapters (Minecraft, RetroXR,
   ...), Dashboard, AzuraCast and Synth Stage.
#. **Some extensions are installed by default** (``default_install``) and the
   user may uninstall them. Without any Cortex endpoint the Synth falls back to
   an explicit error message.
#. **Telegram is one extension among many.** Today it is far too baked into the
   Core; that is debt to remove, not a special case.

Contents
--------

.. toctree::
   :maxdepth: 1

   current_architecture
   component_classification
   dependency_analysis
   api_inventory
   api_matrix
   manifest_proposal
   repository_architecture
   security_analysis
   migration_plan
   extension_runtime
   telegram_extension
   repository_implementation
   remaining_work
   manual_checklist
   open_questions
   testing_strategy
   extension_developer_guide

Method and limits
-----------------

The facts were gathered by reading ``AGENTS.md``, ``docs/``, ``core/``,
``plugins/``, ``interface/``, ``engines/``, ``main.py``, ``Dockerfile``,
``docker-compose.yml`` and ``pyproject.toml``, and by ``grep``-based counts over
the tree. Limits worth knowing:

* Dependency counts are **module-level** (which ``core`` module is imported by
  how many plugin files). A **function-level** matrix (which functions each
  extension actually calls) is the first work item of the implementation phase,
  because it sizes the initial public API. See :doc:`api_inventory`.
* No code was executed and no test was run. Runtime behaviours that depend on
  execution (for example the exact fallback when no Cortex is configured) are
  marked **Unknown** until verified.
* ``docs/`` and the code disagree in several places; those are listed in
  :ref:`pp-doc-discrepancies` instead of being silently resolved.
