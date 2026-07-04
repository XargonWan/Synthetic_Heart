# Release Notes

Recent updates and changes to the xAI API.

## Latest Updates

### Grok 4.1 Fast

You can now use Grok 4.1 Fast in the xAI Enterprise API, including with agent tools.

**Models available:**
- `grok-4-1-fast-reasoning` - With reasoning capabilities
- `grok-4-1-fast-non-reasoning` - Without reasoning, optimized for speed

### Files API

You can upload files and use them in chat conversations with the Files API.

**Requirements:**
- Version 1.4.0 of the `xai-sdk` package required

### Agentic Tool Calling

Enhanced agentic tool calling capabilities with automatic tool orchestration.

**Requirements:**
- Version 1.3.1 of the `xai-sdk` package required

### Inline Citations

Responses can now include inline citations with structured metadata.

**Requirements:**
- Version 1.5.0 of the `xai-sdk` package required

### Location-Based Search

Search tools now support location-based queries for more relevant results.

**Requirements:**
- Version 1.6.0 of the `xai-sdk` package required

### Collections API

Upload files, create embeddings, and use them for inference with the Collections API.

- Organize documents into collections
- Search across documents
- Receive cited responses

### Grok 4 Fast

xAI has released grok-4-fast, the latest advancement in cost-efficient reasoning models.

**Specifications:**
- Context window: 2,000,000 tokens
- Pricing: $0.20 input / $0.50 output per million tokens
- Balanced speed and quality

### Voice Agent API

New real-time voice conversation capabilities via WebSocket.

**Features:**
- 100+ languages supported
- Native-quality accents
- Real-time bidirectional audio
- Tool calling support

## SDK Version History

| Version | Features Added |
|---------|----------------|
| 1.6.0 | Location-based search |
| 1.5.0 | Inline citations |
| 1.4.0 | Files API, `get_tool_call_type` |
| 1.3.1 | Agentic tool calling |

## API Changes

### Responses API (Preferred)

The Responses API is now the preferred method for new integrations, offering:
- Simplified interface
- Built-in tool support
- Streaming support

### Chat Completions (Legacy)

Chat Completions API remains available for backward compatibility but Responses API is recommended for new projects.

### Model Aliases

Models now support aliases for easier migration:
- `<modelname>` → Latest stable version
- `<modelname>-latest` → Latest version with newest features
- `<modelname>-<date>` → Specific release

## Breaking Changes

### Reasoning Model Parameters

Grok 4 reasoning models do not support:
- `presencePenalty`
- `frequencyPenalty`
- `stop`
- `reasoning_effort`

Requests with these parameters will return errors.

## Deprecations

### Chat Completions

While still functional, Chat Completions is considered legacy. Migrate to Responses API for new features.

## Migration Guide

See [Migrating to New Models](./migration.md) for detailed migration instructions.

## Known Issues

Check https://status.x.ai for current service status and known issues.

## Getting Help

- Documentation: https://docs.x.ai
- Support: support@x.ai
- Status: https://status.x.ai
