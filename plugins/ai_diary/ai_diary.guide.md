# AI Diary & Long-Term Memory

Manages Synth's **personal diary** and her **long-term memory** store. Recent
diary entries are injected into the prompt context so Synth keeps a sense of
continuity across days, and memories can be written and recalled over time.

Backed by the `ai_diary`, `ai_diary_archive`, and `memories` tables.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject recent diary entries into the prompt context. |
| `create_personal_diary_entry` | Write a new first-person diary entry. |
| `update_diary_entry` | Amend an existing diary entry. |

## Configuration

| Key | Purpose |
|-----|---------|
| `ENABLE_AI_DIARY` | Master enable toggle. |
| `DIARY_HISTORY_DAYS` | How many days of diary to inject. |
| `DIARY_CONTEXT_MAX_CHARS` | Character cap on injected diary text. |
