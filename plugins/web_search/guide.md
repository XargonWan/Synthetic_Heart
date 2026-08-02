# Web Search

Gives Synth real-time access to the internet for factual grounding. When a
reply needs up-to-date or external information, Synth can search the web and
fetch page content instead of relying only on what the model already knows.

This folder is the implementation package behind the `web_search_plugin.py`
plugin (which exposes the `search_current_knowledge` action).

## How it works

- `search_engine.py` — the single implementation of the search backends.
  SearXNG is the primary engine (self-hosted, privacy-friendly); Tavily is an
  optional fallback. It also contains the page fetcher, with a per-task
  `FetchCache` so the same URL is not downloaded twice in one turn.
- `search_orchestrator.py` — a background orchestrator that can run searches
  *outside* the normal message lifecycle: it runs several queries
  concurrently, summarizes results with the Grillo-scope cortex, and wakes
  Synth via a low-priority beat when results are ready. Task state is persisted
  in the `web_search_tasks` table.

## Configuration

| Key | Purpose |
|-----|---------|
| `SEARXNG_URL` | Base URL of the SearXNG instance to query. |
| `TAVILY_API_KEY` | API key enabling the Tavily fallback. |
| `RECON_MAX_RESULTS` / `RECON_TIMEOUT` | Result cap and timeout for searches. |

See also the `recon_web_search.py` Recon contributor, which decides *whether* a
turn needs a web search before this backend is invoked.
