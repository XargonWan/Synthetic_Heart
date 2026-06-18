# grok-api-mcp

MCP server that provides xAI/Grok API documentation to AI assistants. Includes 75 bundled documentation files covering all xAI API features, models, and endpoints.

## Features

- **Bundled documentation** - 75 markdown files with complete xAI API coverage
- **Live fetch** - Pull fresh docs from docs.x.ai when needed
- **Semantic search** - Find relevant documentation by keyword
- **API reference** - 44 endpoints across 12 categories
- **Model reference** - 12 Grok models with specs and pricing

## Installation

```bash
npm install
npm run build
```

## Usage

### With Claude Desktop

Add to your Claude Desktop configuration (`~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "grok-api-docs": {
      "command": "node",
      "args": ["/path/to/grok-api-mcp/dist/index.js"]
    }
  }
}
```

### With Claude Code

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "grok-api-docs": {
      "command": "node",
      "args": ["/path/to/grok-api-mcp/dist/index.js"]
    }
  }
}
```

### Standalone

```bash
npm start
```

## Available Tools

| Tool | Description |
|------|-------------|
| `search_docs` | Search bundled documentation for API features, models, and guides |
| `get_doc_page` | Fetch fresh documentation from docs.x.ai |
| `list_api_endpoints` | List all 44 API endpoints, optionally filtered by category |
| `list_models` | List all 13 Grok models with context lengths and capabilities |

### search_docs

Search the bundled documentation using keywords.

```
query: "function calling"
```

### get_doc_page

Fetch a specific documentation page from docs.x.ai.

```
path: "guides/function-calling"
```

### list_api_endpoints

List API endpoints. Filter by category: `chat`, `images`, `videos`, `voice`, `models`, `files`, `batch`, `collections`, `api-keys`, `billing`, `team`, `audit`.

```
category: "collections"  # optional
```

### list_models

List all available Grok models with their specifications.

## API Coverage

### Endpoints (44 total)

| Category | Count | API |
|----------|-------|-----|
| Chat | 4 | Inference |
| Images | 2 | Inference |
| Videos | 2 | Inference |
| Voice | 1 | Inference |
| Models | 3 | Inference |
| Files | 5 | Inference |
| Batch | 7 | Inference |
| Collections | 9 | Management |
| API Keys | 4 | Management |
| Billing | 3 | Management |
| Team | 3 | Management |
| Audit | 1 | Management |

### Models (12 total)

**Language models:**
- grok-4-1-fast-reasoning, grok-4-1-fast-non-reasoning
- grok-4-fast-reasoning, grok-4-fast-non-reasoning
- grok-code-fast-1, grok-4-0709
- grok-3, grok-3-mini, grok-2-vision-1212

**Image generation:**
- grok-imagine-image, grok-2-image-1212

**Video generation:**
- grok-imagine-video

### Documentation Topics

- Chat completions and Responses API
- Function calling and tool use
- Built-in tools (web search, X search, code execution)
- Collections (RAG/embeddings)
- Voice API (real-time WebSocket)
- Batch API
- Image and video generation
- Streaming and structured outputs
- Error handling and rate limits
- SDK guides (Python, TypeScript)

## Development

```bash
npm run build    # Compile TypeScript + copy data files
npm run start    # Run the server
npm run dev      # Watch mode for development
```

## Adding Documentation

1. Add `.md` file to `src/data/`
2. Run `npm run build`
3. The file is automatically discoverable by `search_docs`

## License

MIT
