I. Open Questions
=================

Decisions that cannot be made responsibly from the current implementation, or
that are the maintainers' to make. Grouped by whether the answer is a fact to
verify or a choice.

Facts to verify (technical)
---------------------------

#. **No-Cortex behaviour today.** What does a fresh install do with no
   endpoint at all? Check ``core/cortex_fallback.py`` and
   ``core/db.py::_heal_cortex_config``. The explicit error message is decided;
   whether the code already does it is **Unknown**.
#. **MCP server.** Is ``core/mcp_bridge/server.py::build_server`` used in any
   deployment? Nothing in ``main.py`` or ``core/`` starts it.
#. **Scheduled events trigger.** Which component calls
   ``dispatch_pending_events`` at runtime (no in-tree caller found)? Probably
   ``plugins/event_plugin``, which becomes Core.
#. **Vessel boundary.** Which files in ``plugins/rift_vessel/`` are the Vessel
   (Core) and which are adapters? Is the AGENTS.md section 4 layering rule
   enforced by any test?
#. **Which Vessel adapters need the VRM model**, and therefore declare the
   renderer as a dependency (RetroXR does; Minecraft does not).
#. **Matrix prefix.** Why does Matrix write ``matrix`` in history and
   ``matrix_chat`` in the queue? A fact about intent that only the Matrix
   interface's owner can state.
#. **Where the engine line falls.** Between the Core endpoint/Cortex mechanism
   (``external_endpoints``, ``EndpointProtocol``, ``probe``) and an engine
   extension. The Cortex needs *some* generic mechanism.
#. **Karada wire protocol.** The REST/WS contract is spread across
   ``core/karada_api.py`` and ``frontend/src/services/protocol.ts``; extract the
   exact schema before generalising away from ``vrm_*``.
#. **Process model details.** RPC latency on the turn path (Recon/Debrief,
   hooks), handling of large payloads (audio, avatar frames, attachments) and of
   today's synchronous callbacks (``register_section(build=...)``) over a
   process boundary.
#. **Hot update.** With processes, an update is stop/start; ``importlib.reload``
   no longer matters. Confirm that stopping an extension mid-turn is safe for the
   message queue.
#. **Non-Docker and Windows.** How much of the process sandbox (dedicated user,
   egress limits, directory isolation) is enforceable outside Docker on Linux
   and on Windows.
#. **Native libraries inside an archive.** ``zipimport`` cannot load them; the
   install-time extraction must handle them. Confirm with a real example
   (``torch``-based Vox engines, ``moderngl``).
#. **Mount points.** Exact layout of volumes for installed extensions,
   per-extension data, the package cache and ``plugins_dev``; multiple roots.
   Includes how a **local repository** directory is mounted into the container.
#. **Local repository and signatures.** Whether unsigned local repositories are
   accepted only through an explicit per-repository setting (proposed) and how
   development signing keys work.

Choices for the maintainers
---------------------------

#. **Extension file extension** and **tag name** for ``experimental``.
#. **RPC protocol and transport** (the report deliberately does not choose; see
   :doc:`api_inventory`).
#. **Storage API shape**: KV/documents, manifest-declared tables with Core-
   managed migrations, or a parameterised SQL subset.
#. **Data policy** on uninstall (delete or keep) and across updates.
#. **WebUI slots** and menu model for extensions.
#. **Signature scheme and key distribution** for repositories and packages.
#. **The ``default_install`` list** and how first start works **offline** (a cache
   baked into the image).
#. **Library bundling**: carry wheels inside the archive or resolve at install.
#. **Ollama cleanup**: new identifier for ``ollama_serve``, the fate of the
   legacy ``/api/tags`` routes, the default port 11435. ``providers/ollama.json``
   and the ``openapi`` engine references describe a client **to** a real Ollama
   server and are outside this cleanup unless decided otherwise.
#. **Selenium plugins**: confirmed to move to the Zen ecosystem; whether they
   keep any link to this repository.
#. **Extension dependencies on other extensions** that "make sense" (the
   decision is case by case): the first list is built by the function-level
   matrix.
#. **Which of the "Uncertain" plugins are Core** (``emotion_manager``,
   ``recon``, ``debrief``, ``ai_diary``, ``memory_search``, ``recent_chats``,
   ``agent_plugin``, ``agpeer``, ``bio_manager``, ``blocklist``,
   ``message_map``, ``document_skill``, ``mate_engine``, ``home_assistant``,
   ``time_plugin``, ``weather_plugin``, ``web_search``, ``pdf_voice``, the
   engine hosts ``vox_plugin``/``auris_plugin``/``iris_plugin``). **Unknown**
   until the function-level matrix and a per-plugin read are done.
#. **Governance of new APIs**: the process by which a developer asks the
   maintainers for one.
