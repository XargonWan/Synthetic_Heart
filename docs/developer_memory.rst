Developer Agent Memory
======================

This workspace uses TencentDB Agent Memory as optional **developer tooling**
for coding agents. It is not part of SyntH's runtime MCP configuration.

The developer MCP entry is in ``.mcp.json`` as ``tencentdb-knowledge``. Its
launcher starts the local Knowledge Service on port 8421 and then exposes the
upstream CodeGraph/Wiki MCP tools over stdio. GitNexus remains on disk as an
ignored rollback cache, but is no longer registered as a developer MCP server.

The first setup is intentionally read-oriented:

* CodeGraph replaces GitNexus for symbol search, callers, callees, and impact.
* Wiki provides searchable architecture and operations knowledge.
* Agents should treat the current source, tests, ``AGENTS.md``, and maintained
  ``docs/`` pages as authoritative when generated knowledge disagrees.

Dependencies live under ``.tools/tencentdb-agent-memory`` and are ignored by
Git. The launcher expects Node.js 22+ and installed dependencies in
``MemoryKnowledge/node_modules``.

Configuration
-------------

The MCP process works without an LLM for CodeGraph. Wiki ingestion needs an
OpenAI-compatible endpoint. Set these in the environment of the coding agent
when you want to enable it::

    TDAI_KNOWLEDGE_LLM_API_KEY=...
    TDAI_KNOWLEDGE_LLM_BASE_URL=https://api.openai.com/v1
    TDAI_KNOWLEDGE_LLM_MODEL=gpt-4o-mini

The launcher does not read or print the repository ``.env`` file.

The upstream Knowledge Service requires resource IDs (``cg-...`` and
``wiki-...``) in every query. Create and maintain those through its HTTP API or
the local Swagger UI at ``http://127.0.0.1:8421/docs``. After creating a
resource, add its ID and the branch/asset purpose to the agent instructions if
the agent needs a deterministic starting point.

Initial resources
-----------------

The D16 setup provisions this CodeGraph::

    code_graph_id = cg-p22kpkhl
    wiki_id = wiki-7kop5yzt
    service_id = synthetic-heart-dev
    team_id = synth-development
    branch = feat/rift-vessel-new

The D16 MCP adapter injects these IDs automatically. Agents can call the
CodeGraph/Wiki tools with their normal query arguments; they do not need to
know or repeat the IDs.

To create/refresh the curated Wiki pages from the repository's existing
``AGENTS.md`` and maintained documentation, run::

    uv run python scripts/tencentdb_knowledge_sync.py

That command writes pages directly and does not call an LLM. The Wiki search
tools become useful immediately after the command completes.
