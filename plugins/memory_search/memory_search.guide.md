# Memory Search

Lets Synth **recall relevant memories** on demand. It searches the `ai_diary`
and `memories` tables by tags and semantic relevance, returning the entries
that best match the current topic so past context can inform the reply.

Enabled/disabled via its global plugin toggle (`PLUGIN_ENABLED__memory_search`).

## Actions

| Action | Purpose |
|--------|---------|
| `memory_search` | Retrieve memories matching a query/tags. |

## Configuration

| Key | Purpose |
|-----|---------|
| `MEMORY_SEARCH_MAX_RESULTS` | Max memories returned per query. |
