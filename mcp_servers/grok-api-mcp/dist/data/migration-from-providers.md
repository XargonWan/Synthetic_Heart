# Migration from Other Providers

Guide for migrating to xAI from OpenAI, Anthropic, and other providers.

## Overview

The xAI API was designed to be compatible with both the OpenAI and Anthropic SDKs, making migration straightforward.

**Recommended**: Use the native xAI Python SDK or Vercel AI SDK for JavaScript for the best experience and access to all features.

## Migrating from OpenAI

### Minimal Changes Required

1. Change the base URL
2. Update your API key
3. Update model names

### Python

**Before (OpenAI)**:
```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY")
)

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello"}]
)
```

**After (xAI)**:
```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"  # Add this line
)

response = client.chat.completions.create(
    model="grok-4",  # Change model name
    messages=[{"role": "user", "content": "Hello"}]
)
```

### Node.js/TypeScript

**Before (OpenAI)**:
```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY
});
```

**After (xAI)**:
```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.XAI_API_KEY,
  baseURL: 'https://api.x.ai/v1'  // Add this line
});
```

### Model Mapping

| OpenAI Model | xAI Equivalent |
|--------------|----------------|
| gpt-4 | grok-4 |
| gpt-4-turbo | grok-4-fast |
| gpt-3.5-turbo | grok-4-1-fast-non-reasoning |
| dall-e-3 | grok-imagine-image |

## Migrating from Anthropic

### SDK Deprecation Notice

**Important**: The Anthropic SDK compatibility is fully deprecated. Migrate to the Responses API, OpenAI SDK, or gRPC.

### Using OpenAI SDK Instead

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"}
    ]
)
```

### Model Mapping

| Anthropic Model | xAI Equivalent |
|-----------------|----------------|
| claude-3-opus | grok-4 |
| claude-3-sonnet | grok-4-fast |
| claude-3-haiku | grok-4-1-fast-non-reasoning |

## Configuration Changes

### Base URL

```
https://api.x.ai/v1
```

### API Key

Get your API key from the xAI Console.

```bash
export XAI_API_KEY="your-xai-api-key"
```

## Feature Differences

### Parameters Not Supported by Reasoning Models

Grok 4 does not support:
- `presencePenalty`
- `frequencyPenalty`
- `stop`
- `reasoning_effort`

Remove these parameters when migrating to Grok 4.

### New Features Available

After migrating, you gain access to:
- Built-in web search tools
- X (Twitter) search tools
- Code execution
- Collections search
- Voice API
- 2M token context (Grok 4)

## Testing Migration

### Parallel Testing

```python
def compare_providers(prompt):
    # OpenAI response
    openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    openai_response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )

    # xAI response
    xai_client = OpenAI(
        api_key=os.environ.get("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )
    xai_response = xai_client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "openai": openai_response.choices[0].message.content,
        "xai": xai_response.choices[0].message.content
    }
```

## Common Migration Issues

### 1. Parameter Errors

```
Error: presencePenalty is not supported
```

**Solution**: Remove unsupported parameters for reasoning models.

### 2. Model Not Found

```
Error: Model 'gpt-4' not found
```

**Solution**: Use xAI model names (grok-4, grok-4-fast, etc.)

### 3. Authentication Errors

```
Error: Invalid API key
```

**Solution**: Ensure you're using an xAI API key, not OpenAI/Anthropic.

## LiteLLM Migration

If using LiteLLM, update your configuration:

```python
from litellm import completion

response = completion(
    model="xai/grok-4",  # Prefix with xai/
    messages=[{"role": "user", "content": "Hello"}],
    api_key="your-xai-api-key"
)
```

## Best Practices

1. **Test thoroughly**: Validate outputs match expectations
2. **Update model names**: Use appropriate Grok models
3. **Remove unsupported params**: Clean up incompatible parameters
4. **Use native SDK**: Consider xAI SDK for full feature access
5. **Gradual migration**: Migrate incrementally, not all at once
