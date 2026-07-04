# Streaming Response

Streaming outputs use Server-Sent Events (SSE) that let the server send back the delta of content in event streams.

## Benefits

Streaming responses are beneficial for:
- Providing real-time feedback
- Enhancing user interaction by displaying text as it's generated
- Reducing perceived latency for long responses

## Supported Features

Streaming is supported by:
- All models with text output capability (Chat, Image Understanding, etc.)
- Function calling (tool calls are streamed incrementally)
- Structured outputs

Streaming is **NOT** supported by:
- Models with image output capability (Image Generation)
- Video generation

## Basic Usage

### Python with OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

stream = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Write a poem about AI"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### Using Responses API

```python
stream = client.responses.create(
    model="grok-4",
    input="Explain quantum computing",
    stream=True
)

for event in stream:
    if hasattr(event, 'output_text'):
        print(event.output_text, end="", flush=True)
```

### cURL

```bash
curl https://api.x.ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-4",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

## Stream Event Format

Each SSE event contains a JSON object:

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1699000000,"model":"grok-4","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1699000000,"model":"grok-4","choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1699000000,"model":"grok-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

## Streaming with Function Calls

Tool call deltas are sent incrementally:

```json
{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\""}}]}}]}
{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "location"}}]}}]}
{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\": \""}}]}}]}
```

## Streaming with Structured Outputs

You can use streaming with `response_format` to get streaming structured output:

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "List 3 cities"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "cities",
            "schema": {
                "type": "object",
                "properties": {
                    "cities": {"type": "array", "items": {"type": "string"}}
                }
            }
        }
    },
    stream=True
)
```

## Error Handling

```python
try:
    stream = client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": "Hello"}],
        stream=True
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="")

except Exception as e:
    print(f"Stream error: {e}")
```

## Async Streaming

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

async def stream_response():
    stream = await client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": "Hello"}],
        stream=True
    )

    async for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="")

await stream_response()
```
