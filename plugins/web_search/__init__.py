"""Web search subsystem.

Two decoupled pieces:

- ``search_engine``: pure search + page-fetch helpers (Tavily / DuckDuckGo) with
  a per-task shared fetch cache so concurrent queries never scrape the same URL
  twice.
- ``search_orchestrator``: a background orchestrator that runs searches OUTSIDE
  the normal Synth message lifecycle. The recon web-search plugin only *triggers*
  it; the orchestrator scrapes, asks the cortex to fuse the raw results into a
  single aseptic text, then wakes Synth with a second turn on the originating
  interface path.
"""

from __future__ import annotations
