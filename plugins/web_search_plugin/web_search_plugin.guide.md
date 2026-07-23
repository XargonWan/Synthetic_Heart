# Web Search (action)

The model-facing side of Synth's web search. When a reply needs current or
external facts, this plugin runs an internet search and returns grounded
results. It delegates the actual work to the `web_search/` package
(`search_engine.run_search`), which uses SearXNG as the primary engine and
Tavily as an optional fallback.

## Actions

| Action | Purpose |
|--------|---------|
| `search_current_knowledge` | Search the internet and return relevant results. |

## Configuration

| Key | Purpose |
|-----|---------|
| `SEARXNG_URL` | SearXNG instance to query (primary). |
| `TAVILY_API_KEY` | API key enabling the Tavily fallback. |

See the `web_search/` package guide for the search backend internals.
