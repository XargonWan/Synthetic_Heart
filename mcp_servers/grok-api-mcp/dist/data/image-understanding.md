# Image Understanding

Grok models can analyze and understand images, enabling visual question answering, image description, and visual analysis tasks.

## Overview

Image understanding allows Grok to:
- Describe image contents
- Answer questions about images
- Extract text from images (OCR)
- Analyze charts and graphs
- Compare multiple images
- Understand visual context for search results

## Important: Server-Side Storage with Images

> **Warning**: When sending images, it is advised to **not store request/response history on the server**. Otherwise the request may fail. See [Disable Storing History](#disable-storing-history) below.

## Basic Usage

### With xAI SDK (Recommended)

The [xAI SDK](https://github.com/xai-org/xai-sdk-python) is the recommended way to use image understanding. It covers all features and uses gRPC for optimal performance.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(
    api_key=os.getenv("XAI_API_KEY"),
    timeout=3600,
)

image_url = "https://science.nasa.gov/wp-content/uploads/2023/09/web-first-images-release.png"

chat = client.chat.create(model="grok-4")
chat.append(
    user(
        "What's in this image?",
        image(image_url=image_url, detail="high"),
    )
)

response = chat.sample()
print(response)

# The response ID can be used to continue the conversation later
print(response.id)
```

### Disable Storing History

When working with images, disable server-side storage to avoid request failures:

```python
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

chat = client.chat.create(model="grok-4", store_messages=False)
chat.append(
    user(
        "What's in this image?",
        image(image_url="https://example.com/image.jpg", detail="high"),
    )
)

response = chat.sample()
print(response)
```

### With Responses API (OpenAI SDK Compatible)

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.responses.create(
    model="grok-4",
    input=[
        {
            "type": "input_image",
            "image_url": "https://example.com/image.jpg",
            "detail": "high"
        },
        {
            "type": "input_text",
            "text": "What's in this image?"
        }
    ]
)

print(response.output_text)
```

### With Chat Completions API

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Describe this image in detail"
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

print(response.choices[0].message.content)
```

## Image Input Limits

| Limit | Value |
|-------|-------|
| Maximum image size | **20 MiB** |
| Maximum number of images | **No limit** |
| Supported formats | `jpg/jpeg`, `png` |

## Image Input Methods

### URL

Provide a direct URL to an image:

```python
# xAI SDK
from xai_sdk.chat import user, image

chat.append(
    user(
        "What's in this image?",
        image(image_url="https://example.com/image.jpg"),
    )
)
```

```python
# Responses API format
{
    "type": "input_image",
    "image_url": "https://example.com/image.jpg"
}
```

### Base64 Encoded

Encode local images as base64:

```python
import base64
from xai_sdk import Client
from xai_sdk.chat import user, image

with open("image.jpg", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

client = Client(api_key=os.getenv("XAI_API_KEY"))
chat = client.chat.create(model="grok-4", store_messages=False)
chat.append(
    user(
        "What's in this image?",
        image(image_url=f"data:image/jpeg;base64,{image_data}"),
    )
)

response = chat.sample()
print(response)
```

## Detail Parameter

The `detail` parameter controls the level of image pre-processing and resolution:

| Value | Description | Token Impact |
|-------|-------------|--------------|
| `auto` | System automatically determines resolution (default) | Balanced |
| `low` | Low-resolution processing | Faster, fewer tokens, may miss fine details |
| `high` | High-resolution processing | Slower, more tokens, captures nuanced details |

```python
# xAI SDK
from xai_sdk.chat import image

image(image_url="https://example.com/image.jpg", detail="high")
```

```python
# Responses API format
{
    "type": "input_image",
    "image_url": "https://example.com/image.jpg",
    "detail": "high"
}
```

## Image Order

Any image/text input order is accepted:
- Text can precede image
- Image can precede text
- Multiple images and text can be interleaved

```python
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))
chat = client.chat.create(model="grok-4", store_messages=False)

chat.append(
    user(
        "Compare these two images:",
        image(image_url="https://example.com/image1.jpg"),
        image(image_url="https://example.com/image2.jpg"),
        "What are the differences?",
    )
)

response = chat.sample()
print(response)
```

## Image Understanding with Tools

### With Web Search

```python
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))
chat = client.chat.create(model="grok-4", store_messages=False)

chat.append(
    user(
        "What product is this? Find its price online.",
        image(image_url="https://example.com/product.jpg"),
    )
)

# Enable web search tool
response = chat.sample(tools=[{"type": "web_search"}])
print(response)
```

### Enabling in Search Tools

Set `enable_image_understanding` to true to allow the agent to analyze images found during searches:

```python
tools=[
    {
        "type": "web_search",
        "web_search": {
            "enable_image_understanding": True
        }
    }
]
```

This equips the agent with access to the `view_image` tool, allowing it to interpret images encountered during search.

**Note**: Enabling this feature increases token usage as images are processed and represented as image tokens.

## Chaining Conversations with Images

With the xAI SDK, you can continue conversations using the response ID:

```python
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

# First message with image
chat = client.chat.create(model="grok-4", store_messages=True)
chat.append(
    user(
        "What's in this image?",
        image(image_url="https://example.com/image.jpg"),
    )
)
response = chat.sample()
print(response)

# Continue the conversation
chat = client.chat.create(
    model="grok-4",
    previous_response_id=response.id,
    store_messages=True,
)
chat.append(user("Can you describe the colors in more detail?"))
second_response = chat.sample()
print(second_response)
```

## Use Cases

### Visual Q&A

```python
"What brand is shown in this logo?"
"How many people are in this photo?"
"What color is the car?"
```

### Document Analysis

```python
"Extract the text from this receipt"
"Summarize the information in this chart"
"What does this diagram show?"
```

### Technical Analysis

```python
"Explain this circuit diagram"
"What's wrong with this code screenshot?"
"Analyze this architecture diagram"
```

## Recommended Models

- **grok-4**: Best understanding, recommended for complex analysis
- **grok-4-fast**: Good balance for most use cases

## Best Practices

1. **Disable storage for images**: Set `store_messages=False` when working with images
2. **Use appropriate detail level**: High for text/fine details, low for general content
3. **Provide context**: Tell the model what you're looking for
4. **Clear images**: Better quality images yield better results
5. **Combine with text**: Give relevant context alongside images
6. **Monitor tokens**: Image processing uses additional tokens
7. **Stay under 20 MiB**: Ensure images don't exceed the size limit

## Limitations

- Maximum file size: 20 MiB per image
- Supported formats: JPEG/JPG and PNG only
- Some complex diagrams may be challenging
- Streaming is supported for responses
- Processing time increases with image complexity
- Server-side storage may cause issues with image requests
