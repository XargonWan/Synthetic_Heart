# Responses API

The Responses API is the preferred method for inference with Grok models.

## Basic Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.responses.create(
    model="grok-4",
    input="What is quantum computing?"
)

print(response.output_text)
```

## System Messages

Only a single system/developer message should be used, and it should always be the first message in your conversation.

```python
response = client.responses.create(
    model="grok-4",
    instructions="You are a helpful coding assistant.",
    input="Write a Python function to calculate fibonacci numbers"
)
```

## Multi-turn Conversations

```python
response = client.responses.create(
    model="grok-4",
    input=[
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "Python is a high-level programming language..."},
        {"role": "user", "content": "Show me a simple example"}
    ]
)
```

## Image Understanding

The request for image understanding is similar to text-only prompts. Use an array with `input_image` and `input_text` types.

```python
response = client.responses.create(
    model="grok-4",
    input=[
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg"
        },
        {
            "type": "input_text",
            "text": "What's in this image?"
        }
    ]
)
```

### Base64 Encoded Images

```python
import base64

with open("image.jpg", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

response = client.responses.create(
    model="grok-4",
    input=[
        {
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{image_data}"
        },
        {
            "type": "input_text",
            "text": "Describe this image"
        }
    ]
)
```

### Image Detail Parameter

The `detail` field controls the level of pre-processing applied to the image:

- **auto**: System automatically determines image resolution (default)
- **low**: Processes low-resolution version - faster, fewer tokens, may miss finer details
- **high**: Processes high-resolution version - slower, more expensive, captures nuanced details

```python
{
    "type": "input_image",
    "image_url": "https://example.com/image.jpg",
    "detail": "high"
}
```

### Supported Image Formats

- jpg/jpeg
- png

Any image/text input order is accepted (text prompt can precede image prompt).

## Streaming

Enable streaming to receive responses in real-time:

```python
stream = client.responses.create(
    model="grok-4",
    input="Write a long story",
    stream=True
)

for chunk in stream:
    print(chunk.output_text, end="", flush=True)
```

## Parameters

| Parameter | Description |
|-----------|-------------|
| `model` | Model ID to use |
| `input` | User input (string or array of messages) |
| `instructions` | System instructions |
| `stream` | Enable streaming (boolean) |
| `temperature` | Sampling temperature (0-2) |
| `max_tokens` | Maximum tokens to generate |
| `tools` | Array of tools to enable |
| `tool_choice` | Control tool usage behavior |

## Response Format

```json
{
  "id": "resp_abc123",
  "object": "response",
  "created": 1699000000,
  "model": "grok-4",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": "Response text here..."
    }
  ],
  "output_text": "Response text here...",
  "usage": {
    "input_tokens": 10,
    "output_tokens": 50
  }
}
```
