# Recon — Web Search Evaluator

A **Recon contributor**: during pre-processing (Recon phase) it decides whether
a turn needs fresh information from the internet, and if so generates the search
queries / `check_website` URLs. It does not run the search itself — that is done
by the web search backend — it only produces the `web_search` recon hint.

Recon contributors expose no model-facing actions.

## Configuration

| Key | Purpose |
|-----|---------|
| `RECON_WEB_SEARCH_RECON_ENABLED` | Enable this Recon contributor. |
