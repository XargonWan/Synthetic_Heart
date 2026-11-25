Extension Developer Guide (draft)
=================================

A **Proposal**: the rules for people who write extensions, and a draft
``AGENTS.md`` for the **official extensions repository**. A developer points
their coding agent at that file so the agent does not write extensions that
manipulate the Core or leave the boundary.

Rules, in one place
-------------------

#. An extension is **one file** (an uncompressed zip) built from a directory that
   has a ``synth-extension.toml``.
#. An extension talks to the Core **only** through ``synth_sdk``. It never imports
   ``core``, ``plugins`` or ``interface``, never opens the database, never reads
   the Core's files or environment.
#. Every message goes through the message chain via the ``interfaces`` API.
   Nothing bypasses it.
#. It declares what it needs: capabilities, configuration schema, hooks, UI
   slots, libraries, and **other extensions** it depends on, only if that makes
   sense.
#. It carries its own libraries. It does not use code from another extension
   unless it declared that extension as a dependency, and even then only through
   the gateway.
#. Its data goes through the ``storage`` API. Statistics go through ``stats`` with
   the enforced prefix.
#. If something is missing, **propose a new API to the maintainers**. Do not look
   for another way in.
#. A work-in-progress extension sets ``experimental = true``.

Publishing
----------

``core.extensions.cli pack`` builds the package (libraries included) and ``index`` writes the
signed index; see :doc:`repository_implementation`. A local folder works as a repository,
so a package can be installed exactly as a user would before it is published.

Draft ``AGENTS.md`` for the official extensions repository
----------------------------------------------------------

.. code-block:: markdown

   # AGENTS.md — writing SyntH extensions

   You are writing an **extension** for Synthetic Heart. Extensions run in their
   own process and reach the Core only through the public API (`synth_sdk`).

   ## Hard rules (do not break these, ever)
   - Never `import core`, `import plugins`, `import interface`, or any module of
     the main repository. If you think you need to, you need a new API instead.
   - Never open the database, read `.env`, read the process environment for
     secrets, or touch files outside your own data directory.
   - Never bypass the message chain. Inbound goes through `interfaces.inbound`;
     outbound arrives as a call from the Core.
   - Never import another extension. If you depend on one, declare it in the
     manifest; communicate only through the API.
   - Never ship code that tries to detect, escape or modify the sandbox.

   ## What you must provide
   - `synth-extension.toml`: id, version, `provides`, `core_api` range,
     `permissions`, `config` schema, `requires`, libraries.
   - Only the capabilities you really use. Unused capabilities are rejected.
   - Your own libraries, pinned.
   - Tests that run without the Core (use the SDK's fake gateway).

   ## Interfaces
   - Declare your own Interface Paths in the manifest
     (`name/level1[/level2/...]`) and document what each level means. The Core
     does not interpret them.

   ## When something is missing
   - Write a short proposal for the new API and open an issue with the
     maintainers. Do not work around it.

   ## Testing before you publish
   - Add your build output to a **local repository** (a directory with the same
     layout as the published one) and install from it in SyntH. It uses the same
     code path as a cloud repository, so a successful local install is a real
     test of the package.

   ## Checks before you finish
   - The conformance test passes (no forbidden imports, manifest valid).
   - The extension runs from `plugins_dev` and then as a packed file.
   - `experimental = true` while unfinished.

The final text of this file is written when the SDK and the manifest schema
exist (milestone M2b in :doc:`migration_plan`); the draft above fixes the
intent.
