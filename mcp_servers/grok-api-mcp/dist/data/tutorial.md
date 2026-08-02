# The Hitchhiker's Guide to Grok

A quick start tutorial for using the xAI API.

## Getting Started

### 1. Create an API Key

1. Visit the API Keys Page in the xAI API Console
2. Generate a new API key
3. Save it securely - you won't be able to see it again

### 2. Set Up Your Environment

Export your API key as an environment variable:

```bash
export XAI_API_KEY="your-api-key-here"
```

Or save it to a `.env` file:

```
XAI_API_KEY=your-api-key-here
```

### 3. Make Your First Request

#### Using cURL

```bash
curl https://api.x.ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-4",
    "messages": [
      {"role": "user", "content": "Hello, Grok!"}
    ]
  }'
```

#### Using Python with OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Hello, Grok!"}
    ]
)

print(response.choices[0].message.content)
```

#### Using the xAI SDK

```python
from xai_sdk import Client

client = Client(api_key=os.environ.get("XAI_API_KEY"))

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Hello, Grok!"}
    ]
)

print(response.choices[0].message.content)
```

### 4. Using the Responses API (Recommended)

The Responses API is the preferred method for new integrations:

```python
response = client.responses.create(
    model="grok-4",
    input="What is the meaning of life?"
)

print(response.output_text)
```

## SDK Compatibility

The xAI API is compatible with:

- **OpenAI SDK**: Use `base_url="https://api.x.ai/v1"`
- **Anthropic SDK**: Compatible with Anthropic-style requests
- **Native xAI SDK**: Full feature support

## Next Steps

- Explore [Function Calling](./function-calling.md) to connect Grok to external tools
- Learn about [Built-in Tools](./built-in-tools.md) like web search
- Check out [Image Generation](./image-generation.md) capabilities
- Review [Models](./models.md) to choose the right model for your use case
