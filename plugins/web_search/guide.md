# Web Search

Gives Synth real-time access to the internet for factual grounding. When a
reply needs up-to-date or external information, Synth can search the web and
fetch page content instead of relying only on what the model already knows.

This folder is the implementation package behind the `web_search_plugin.py`
plugin (which exposes the `search_current_knowledge` action).

## How it works

- `search_engine.py` — the single implementation of the search backends.
  SearXNG is the primary engine (self-hosted, privacy-friendly); Tavily is an
  optional fallback. When neither is configured or reachable, the keyless
  Wikipedia and Hacker News JSON APIs are tried as a last resort, so a native
  (non-Docker) deployment without SearXNG/Tavily still returns real results
  for encyclopedic and technology queries. It also contains the page fetcher,
  with a per-task `FetchCache` so the same URL is not downloaded twice in one
  turn.
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

For the best results configure `SEARXNG_URL` (the Docker image ships a SearXNG
instance on `127.0.0.1:8888`) or a `TAVILY_API_KEY`. With neither, searches fall
back to the keyless Wikipedia/Hacker News APIs, which cover encyclopedic facts
and technology/current-event discussion but are not a general web index.

## Native Windows runtimes

The Docker image bakes SearXNG in-container, but a native Windows runtime (uv on
bare metal) has no container. Run the provisioner once to install and start a
local SearXNG on `127.0.0.1:8888` — the default `SEARXNG_URL` then works with no
config change:

```powershell
.\scripts\searxng_windows.ps1 install          # clone + install + start
.\scripts\searxng_windows.ps1 status           # is it up?
.\scripts\searxng_windows.ps1 stop
.\scripts\searxng_windows.ps1 start
.\scripts\searxng_windows.ps1 restart
.\scripts\searxng_windows.ps1 update           # re-clone latest + reinstall + restart
.\scripts\searxng_windows.ps1 install -RegisterStartup   # + start at logon
```

Requirements: `git` and `uv` on PATH (`scripts\install_prereqs.ps1` installs
uv). The runtime lives under `plugins/web_search/searxng-runtime/` (gitignored)
and reuses the committed `container/searxng/settings.yml`. The provisioner
applies two Windows-only adaptations to that private copy: it skips a handful of
upstream packaging template files whose names contain a colon (invalid on NTFS),
and it guards SearXNG's Unix-only `pwd` import in `searx/valkeydb.py`.

See also the `recon_web_search.py` Recon contributor, which decides *whether* a
turn needs a web search before this backend is invoked.
