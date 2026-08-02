# Chat Completions API (Legacy)

The Chat Completions API is the legacy endpoint for chat interactions. For new integrations, consider using the [Responses API](./responses-api.md) instead.

## Endpoint

```
POST https://api.x.ai/v1/chat/completions
```

## Basic Usage

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
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)
```

## Request Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Model ID (required) |
| `messages` | array | Array of message objects (required) |
| `temperature` | number | Sampling temperature (0-2), default 1 |
| `top_p` | number | Nucleus sampling parameter |
| `n` | integer | Number of completions to generate |
| `stream` | boolean | Enable streaming |
| `max_tokens` | integer | Maximum tokens to generate |
| `presence_penalty` | number | Presence penalty (-2 to 2) |
| `frequency_penalty` | number | Frequency penalty (-2 to 2) |
| `stop` | string/array | Stop sequences |
| `tools` | array | Available tools/functions |
| `tool_choice` | string/object | Tool selection behavior |

### Reasoning Model Limitations

The `presencePenalty`, `frequencyPenalty`, and `stop` parameters are **not supported** by reasoning models (like Grok 4). Adding them in the request will result in an error.

## Message Format

### System Message

```json
{
  "role": "system",
  "content": "You are a helpful assistant."
}
```

### User Message

```json
{
  "role": "user",
  "content": "What is the capital of France?"
}
```

### Assistant Message

```json
{
  "role": "assistant",
  "content": "The capital of France is Paris."
}
```

### Tool Message

```json
{
  "role": "tool",
  "tool_call_id": "call_abc123",
  "content": "{\"result\": \"success\"}"
}
```

## Image Input

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "What's in this image?"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.com/image.jpg",
                        "detail": "high"
                    }
                }
            ]
        }
    ]
)
```

## Streaming

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## Response Format

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "grok-4",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 20,
    "total_tokens": 30
  }
}
```

## Batch Mode

You can request multiple completions by setting `n > 1`:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Give me a random number"}],
    n=3
)

for choice in response.choices:
    print(choice.message.content)
```

**Note**: File attachments with document search are agentic requests and do not support batch mode (n > 1).
