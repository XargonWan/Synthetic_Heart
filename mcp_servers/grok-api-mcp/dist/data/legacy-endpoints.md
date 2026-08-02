# Legacy and Deprecated Endpoints

Documentation for legacy and deprecated API endpoints.

## Deprecation Notice

The following endpoints are deprecated and will be removed in future versions. Please migrate to the recommended alternatives.

## Completions (Legacy)

```http
POST /v1/completions
```

**Status**: Legacy (use Chat Completions or Responses API instead)

The legacy completions endpoint for text generation without chat formatting.

### Request

```json
{
  "model": "grok-4",
  "prompt": "Complete this sentence: The quick brown fox",
  "max_tokens": 50,
  "temperature": 0.7
}
```

### Response

```json
{
  "id": "cmpl-abc123",
  "object": "text_completion",
  "created": 1699000000,
  "model": "grok-4",
  "choices": [
    {
      "text": " jumps over the lazy dog.",
      "index": 0,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 6,
    "total_tokens": 16
  }
}
```

### Migration

Migrate to Chat Completions:

```python
# Legacy
response = client.completions.create(
    model="grok-4",
    prompt="Complete: The quick brown fox"
)

# New (Chat Completions)
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Complete: The quick brown fox"}]
)
```

## Messages (Anthropic Compatible)

```http
POST /v1/messages
```

**Status**: Deprecated (use Responses API or Chat Completions instead)

Anthropic-style messages endpoint for compatibility with Anthropic SDK.

### Request

```json
{
  "model": "grok-4",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "Hello, Claude!"}
  ]
}
```

### Response

```json
{
  "id": "msg-abc123",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "Hello! I'm Grok, not Claude. How can I help you today?"
    }
  ],
  "model": "grok-4",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 10,
    "output_tokens": 15
  }
}
```

### Migration

The Anthropic SDK compatibility is **fully deprecated**. Migrate to:

1. **Responses API** (recommended):
```python
response = client.responses.create(
    model="grok-4",
    input="Hello!"
)
```

2. **Chat Completions** (OpenAI-compatible):
```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

3. **gRPC API**:
Use the native xAI SDK with gRPC for optimal performance.

## Completions (Anthropic Compatible)

```http
POST /v1/complete
```

**Status**: Deprecated

Anthropic-style completions endpoint.

### Migration

Same as Messages endpoint - migrate to Responses API, Chat Completions, or gRPC.

## SDK Compatibility Status

| SDK | Status | Recommendation |
|-----|--------|----------------|
| OpenAI SDK | Supported | Recommended for migration |
| Anthropic SDK | Deprecated | Migrate to OpenAI SDK or xAI SDK |
| xAI SDK | Supported | Best for full feature access |

## Timeline

- **Deprecated**: Endpoints still work but not recommended
- **Sunset date**: Will be announced with advance notice
- **Removal**: Endpoints will return errors after removal

## Migration Checklist

1. [ ] Identify deprecated endpoint usage in your code
2. [ ] Choose replacement endpoint (Responses API recommended)
3. [ ] Update SDK initialization (base URL, API key)
4. [ ] Update request format
5. [ ] Update response handling
6. [ ] Test thoroughly
7. [ ] Monitor for errors after deployment

## Getting Help

If you need assistance migrating:
- Documentation: https://docs.x.ai/docs/guides/migration
- Support: support@x.ai
