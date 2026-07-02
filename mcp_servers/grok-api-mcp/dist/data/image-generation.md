# Image Generation and Edit

Generate and edit images using Grok's image capabilities.

> **Note**: You can also animate generated images using [video generation](./video-generation.md).

## Image Generation

### With xAI SDK (Recommended)

The [xAI SDK](https://github.com/xai-org/xai-sdk-python) is the recommended way to generate images:

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv('XAI_API_KEY'))

response = client.image.sample(
    model="grok-imagine-image",
    prompt="A cat in a tree",
    image_format="url"
)

print(response.url)
```

By default, `image_format` is `url` and the generated image will be available for download on xAI managed storage.

### With OpenAI SDK

```python
from openai import OpenAI
import os

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.images.generate(
    model="grok-imagine-image",
    prompt="A futuristic city with flying cars at sunset",
    n=1,
    size="1024x1024"
)

image_url = response.data[0].url
print(image_url)
```

### Endpoint

```
POST https://api.x.ai/v1/images/generations
```

### Request Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Model to use (`grok-imagine-image`) |
| `prompt` | string | Text description of the image to generate |
| `n` | integer | Number of images to generate (1-10) |
| `size` | string | Image size ("1024x1024", "1792x1024", "1024x1792") |
| `aspect_ratio` | string | Alternative to size - e.g., "4:3", "16:9", "1:1" |
| `response_format` | string | `url` (default) or `b64_json` |
| `style` | string | Image style ("vivid", "natural") |

> **Note**: The `quality` parameter is **not currently supported** by the xAI API.

### cURL Example

```bash
curl https://api.x.ai/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-imagine-image",
    "prompt": "A serene mountain landscape with a lake",
    "n": 1,
    "size": "1024x1024"
  }'
```

### Response Format

```json
{
  "created": 1699000000,
  "data": [
    {
      "url": "https://...",
      "revised_prompt": "A detailed serene mountain landscape..."
    }
  ]
}
```

## Image Editing

Edit existing images with prompts. The Grok Imagine API supports sophisticated editing operations including object manipulation, scene transformation, and style changes.

### With xAI SDK

```python
import os
import base64
from xai_sdk import Client

client = Client(api_key=os.getenv('XAI_API_KEY'))

with open("cat-in-tree.jpg", "rb") as image_file:
    image_bytes = image_file.read()
    base64_string = base64.b64encode(image_bytes).decode("utf-8")

response = client.image.sample(
    model="grok-imagine-image",
    image_url=f"data:image/jpeg;base64,{base64_string}",
    prompt="Swap the cat in the picture with a dog."
)

print(response.url)
```

### Endpoint

```
POST https://api.x.ai/v1/images/edits
```

### With OpenAI SDK

```python
response = client.images.edit(
    model="grok-imagine-image",
    image=open("original.png", "rb"),
    mask=open("mask.png", "rb"),
    prompt="Add a rainbow in the sky",
    n=1,
    size="1024x1024"
)

edited_url = response.data[0].url
```

### Edit Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `image` | file/string | Original image to edit (file or base64 data URL) |
| `mask` | file | Mask image indicating areas to edit (PNG) |
| `prompt` | string | Description of the edit to make |
| `n` | integer | Number of images to generate |
| `size` | string | Output image size |

### Mask Requirements

- Must be same dimensions as original image
- Transparent areas indicate where to edit
- Opaque areas will be preserved

## Image Editing Operations

### Add Objects

Insert new objects into your images:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Add a hot air balloon in the sky."
)
```

**Example prompts:**
- "Add a vase of flowers on the table"
- "Add sunglasses to the person"
- "Add a mountain range in the background"

### Remove Objects

Eliminate unwanted elements with high precision:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Remove the person in the background."
)
```

**Example prompts:**
- "Remove the watermark"
- "Remove the power lines from the sky"
- "Remove all text from the image"

### Swap Objects

Replace objects while maintaining visual consistency:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Swap the wooden chair with a modern office chair."
)
```

**Example prompts:**
- "Replace the cat with a dog"
- "Swap the red car with a blue motorcycle"
- "Change the laptop to a vintage typewriter"

### Scene Transformation

Transform environment, weather, and lighting:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Transform the scene to a snowy winter setting."
)
```

**Supported transformations:**
- **Weather**: sunny, cloudy, rainy, snowy, foggy
- **Time of day**: sunrise, golden hour, sunset, night
- **Seasons**: spring, summer, autumn, winter

**Example prompts:**
- "Change from sunny to dramatic sunset lighting"
- "Transform to autumn with falling leaves"
- "Add fog and moody atmosphere"

### Restyle / Remix

Transform the visual style while preserving content:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Restyle as a watercolor painting."
)
```

**Style options:**
- Artistic: oil painting, watercolor, sketch, anime, pixel art
- Photographic: vintage film, polaroid, HDR, black and white
- Design: comic book, pop art, minimalist, retro

**Example prompts:**
- "Render as a Studio Ghibli anime style"
- "Transform into a vintage 1950s photograph"
- "Apply a cyberpunk neon aesthetic"

### Color and Product Editing

Fine-tuned control for commercial and product images:

```python
response = client.image.sample(
    model="grok-imagine-image",
    image_url="data:image/jpeg;base64,...",
    prompt="Change the product color from black to rose gold."
)
```

**Example prompts:**
- "Change the shirt color from blue to red"
- "Make the car metallic silver"
- "Adjust lighting to better highlight the product"

## Base64 Output

Instead of getting a URL, you can receive base64-encoded image data:

### With xAI SDK

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv('XAI_API_KEY'))

response = client.image.sample(
    model="grok-imagine-image",
    prompt="A cat in a tree",
    image_format="base64"
)

print(response.image)  # returns the raw image bytes
```

### With REST API

Set `"response_format": "b64_json"` in the request body. The response will contain a `b64_json` field with the encoded image.

### With gRPC

Set `"format": "IMG_FORMAT_BASE64"` in the request.

## Generating Multiple Images

Generate up to 10 images in one request:

### With xAI SDK

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv('XAI_API_KEY'))

response = client.image.sample_batch(
    model="grok-imagine-image",
    prompt="A cat in a tree",
    n=4,
    image_format="url",
)

for image in response:
    print(image.url)
```

### With OpenAI SDK

```python
response = client.images.generate(
    model="grok-imagine-image",
    prompt="A cat in a tree",
    n=4
)

for img in response.data:
    print(img.url)
```

## Setting Aspect Ratio

Instead of fixed sizes, you can specify an aspect ratio:

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv('XAI_API_KEY'))

response = client.image.sample(
    model="grok-imagine-image",
    prompt="A cat in a tree",
    aspect_ratio="4:3"
)

print(response.url)
```

### Supported Aspect Ratios

- `1:1` - Square
- `4:3` - Standard
- `3:4` - Portrait standard
- `16:9` - Widescreen landscape
- `9:16` - Widescreen portrait
- `3:2` - Classic photo landscape
- `2:3` - Classic photo portrait

## Size Options

| Size | Aspect Ratio | Best For |
|------|--------------|----------|
| 1024x1024 | 1:1 | Square images, icons, avatars |
| 1792x1024 | ~16:9 | Landscape, banners, headers |
| 1024x1792 | ~9:16 | Portrait, mobile wallpapers |

## Style Options

- **vivid**: More dramatic, hyper-real images
- **natural**: More realistic, less stylized

## Best Practices

### Prompt Engineering

**Good prompts:**
- "A photorealistic portrait of an elderly woman with silver hair, warm lighting, professional photography"
- "An oil painting of a stormy sea with a lighthouse, impressionist style, dramatic lighting"

**Less effective prompts:**
- "A woman" (too vague)
- "Make it look good" (not descriptive)

### Tips

1. **Be specific**: Include details about style, lighting, composition
2. **Specify art style**: "oil painting", "digital art", "photorealistic", etc.
3. **Describe lighting**: "soft lighting", "dramatic shadows", "golden hour"
4. **Include composition details**: "close-up", "wide angle", "bird's eye view"
5. **Mention mood/atmosphere**: "serene", "dramatic", "mysterious"

## Rate Limits

Image generation has specific rate limits. Check the [Consumption and Rate Limits](./rate-limits.md) documentation for current limits.

## Error Handling

```python
try:
    response = client.images.generate(
        model="grok-imagine-image",
        prompt="...",
        n=1
    )
except Exception as e:
    if "content_policy" in str(e):
        print("Prompt violates content policy")
    elif "rate_limit" in str(e):
        print("Rate limit exceeded")
    else:
        print(f"Error: {e}")
```

## Streaming Note

Streaming is **NOT** supported for image generation. The response is returned only after the image is fully generated.
