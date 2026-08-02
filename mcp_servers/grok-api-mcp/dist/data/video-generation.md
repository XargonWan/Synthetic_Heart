# Video Generation and Edit

Generate and edit videos using the Grok Imagine API - xAI's unified video-audio generative model.

## Overview

All video generation/edit requests are **deferred requests**:
1. Send a video generation/edit request
2. Receive a response with a request ID
3. Retrieve the video result later using the request ID

If you're using the xAI SDK, it can handle the polling automatically with `client.video.generate()`.

### Key Features

- **Native audio-video generation** - Synchronized audio without separate tools
- **Up to 15-second videos at 720p** - High quality output
- **Image-to-video** - Bring static images to life
- **Text-to-video** - Generate from text prompts
- **Video editing** - Restyle scenes, add/remove/swap objects, control motion
- **Scene transformation** - Change weather, lighting, and environment
- **Remix/Restyle** - Transform visual style while preserving content

## Pricing

| Feature | Cost |
|---------|------|
| Video generation (with audio) | $4.20/minute |

## Video Generation with Automatic Polling

The xAI SDK can automatically poll for results until the video is ready:

### Text-to-Video

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
)
print(f"Video URL: {response.url}")
```

### Image-to-Video

Generate a video from an existing image:

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="Generate a video based on the provided image.",
    model="grok-imagine-video",
    image_url="<url of the image>",
)
print(f"Video URL: {response.url}")
```

### Video Editing

Edit an existing video with a prompt:

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="Make the ball larger.",
    model="grok-imagine-video",
    video_url="<url of the video to edit>",
)
print(f"Video URL: {response.url}")
```

## Manual Polling

If you prefer to control the polling yourself, use `client.video.start()` to initiate generation and `client.video.get()` to retrieve results.

### Start Video Generation

#### With xAI SDK

```python
from xai_sdk import Client

client = Client()

response = client.video.start(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
)
print(f"Request ID: {response.request_id}")
```

#### With JavaScript (REST)

```javascript
const response = await fetch('https://api.x.ai/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'A cat playing with a ball',
    model: 'grok-imagine-video',
  }),
});
const data = await response.json();
console.log('Request ID:', data.request_id);
```

#### With cURL

```bash
curl -X 'POST' https://api.x.ai/v1/videos/generations \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "A cat playing with a ball",
      "model": "grok-imagine-video"
  }'
```

Response:
```json
{"request_id":"aa87081b-1a29-d8a6-e5bf-5807e3a7a561"}
```

### Image-to-Video (Manual)

#### With xAI SDK

```python
from xai_sdk import Client

client = Client()

response = client.video.start(
    prompt="Generate a video based on the provided image.",
    model="grok-imagine-video",
    image_url="<url of the image>",
)
print(f"Request ID: {response.request_id}")
```

#### With JavaScript (REST)

```javascript
const response = await fetch('https://api.x.ai/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'Generate a video based on the provided image.',
    model: 'grok-imagine-video',
    image: { url: '<url of the image>' },
  }),
});
const data = await response.json();
console.log('Request ID:', data.request_id);
```

#### With cURL

```bash
curl -X 'POST' https://api.x.ai/v1/videos/generations \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "Generate a video based on the provided image.",
      "model": "grok-imagine-video",
      "image": {"url": "<url of the image>"}
  }'
```

### Video Editing (Manual)

Video editing uses a separate endpoint: `POST /v1/videos/edits`

#### With xAI SDK

```python
from xai_sdk import Client

client = Client()

response = client.video.start(
    prompt="Make the ball in the video larger.",
    model="grok-imagine-video",
    video_url="<url of the previous video>",
)
print(f"Request ID: {response.request_id}")
```

#### With JavaScript (REST)

```javascript
const response = await fetch('https://api.x.ai/v1/videos/edits', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'Make the ball in the video larger.',
    video: { url: '<url of the previous video>' },
    model: 'grok-imagine-video',
  }),
});
const data = await response.json();
console.log('Request ID:', data.request_id);
```

#### With cURL

```bash
curl -X 'POST' https://api.x.ai/v1/videos/edits \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "Make the ball in the video larger.",
      "video": {"url": "<url of the previous video>"},
      "model": "grok-imagine-video"
  }'
```

Response:
```json
{"request_id":"a3d1008e-4544-40d4-d075-11527e794e4a"}
```

> **Note**: The input video URL must be a direct, publicly accessible link to the video file. The maximum supported video length for editing is **8.7 seconds**.

## Video Editing Operations

The Grok Imagine API supports sophisticated video editing with high precision and consistency. All editing operations preserve the original video duration.

### Add Objects

Insert new objects into your video scenes:

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="Add a red balloon floating in the background.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
print(f"Video URL: {response.url}")
```

**Example prompts:**
- "Add a coffee cup on the table"
- "Add falling snow in the scene"
- "Add a bird flying across the sky"

### Remove Objects

Eliminate unwanted elements from videos with high accuracy:

```python
response = client.video.generate(
    prompt="Remove the person in the background.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Example prompts:**
- "Remove the watermark from the corner"
- "Remove the car from the street"
- "Remove all text overlays"

### Swap Objects

Replace props and objects while maintaining visual coherence:

```python
response = client.video.generate(
    prompt="Swap the red car with a blue motorcycle.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Example prompts:**
- "Replace the dog with a cat"
- "Swap the wooden chair with a modern office chair"
- "Change the laptop to a vintage typewriter"

### Scene Transformation

Transform the environment, weather, and lighting conditions:

```python
response = client.video.generate(
    prompt="Transform the scene to a snowy winter setting.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Supported transformations:**
- **Weather**: sunny, cloudy, rainy, snowy, foggy, stormy
- **Time of day**: sunrise, golden hour, sunset, night, midnight
- **Seasons**: spring, summer, autumn, winter
- **Lighting**: dramatic shadows, soft diffused light, neon glow

**Example prompts:**
- "Change the scene from sunny to a dramatic sunset"
- "Transform to an autumn setting with falling leaves"
- "Add fog and moody lighting to the scene"
- "Switch to night time with street lights"

### Remix / Restyle

Transform the visual style of your video while preserving the content and motion:

```python
response = client.video.generate(
    prompt="Restyle as a watercolor painting animation.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Style options:**
- Artistic styles: oil painting, watercolor, sketch, anime, pixel art
- Film styles: noir, vintage film, VHS aesthetic, cinematic
- Visual effects: comic book, pop art, cyberpunk, steampunk

**Example prompts:**
- "Restyle as a Studio Ghibli anime"
- "Transform into a black and white noir film"
- "Apply a retro 80s VHS aesthetic"
- "Render as a stylized comic book animation"

### Color and Product Editing

Fine-tuned control for commercial and product showcase videos:

```python
response = client.video.generate(
    prompt="Change the product color from black to rose gold.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Example prompts:**
- "Change the shirt color from blue to red"
- "Make the car metallic silver instead of matte black"
- "Adjust the lighting to highlight the product better"

### Motion Control

Control camera movement and motion dynamics:

```python
response = client.video.generate(
    prompt="Add a smooth dolly forward motion toward the subject.",
    model="grok-imagine-video",
    video_url="<url of the video>",
)
```

**Motion terminology:**
- Camera: dolly forward/backward, pan left/right, tilt up/down, orbit
- Speed: slow motion, time-lapse, speed ramp
- Style: handheld sway, steady tracking, crane shot

### Retrieve Video Results

After getting a `request_id`, retrieve the generated video.

#### With xAI SDK

```python
response = client.video.get(request_id)
print(f"Video URL: {response.url}")
```

#### With JavaScript (REST)

```javascript
const requestId = 'aa87081b-1a29-d8a6-e5bf-5807e3a7a561';

const response = await fetch(`https://api.x.ai/v1/videos/${requestId}`, {
  headers: {
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
});
const data = await response.json();
console.log('Video URL:', data.url);
```

#### With cURL

```bash
curl -X 'GET' https://api.x.ai/v1/videos/{request_id} \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer $XAI_API_KEY'
```

## REST API Endpoints

### Video Generation

```
POST https://api.x.ai/v1/videos/generations
```

### Video Editing

```
POST https://api.x.ai/v1/videos/edits
```

### Video Retrieval

```
GET https://api.x.ai/v1/videos/{request_id}
```

## Using OpenAI SDK

### Basic Usage

```python
from openai import OpenAI
import os
import time

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Start generation
response = client.videos.generate(
    model="grok-imagine-video",
    prompt="A rocket launching into space with dramatic lighting"
)

request_id = response.id
print(f"Request ID: {request_id}")

# Poll for result
while True:
    result = client.videos.retrieve(request_id)
    if result.status == "completed":
        video_url = result.data[0].url
        print(f"Video URL: {video_url}")
        break
    elif result.status == "failed":
        print(f"Generation failed: {result.error}")
        break
    time.sleep(5)
```

## Video Output Parameters

### Duration

Specify video duration in seconds (1-15 seconds):

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    duration=10
)
print(f"Video URL: {response.url}")
print(f"Duration: {response.duration}")
```

Or with manual polling:

```python
response = client.video.start(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    duration=10
)
print(f"Request ID: {response.request_id}")
```

With JavaScript:

```javascript
const response = await fetch('https://api.x.ai/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'A cat playing with a ball',
    model: 'grok-imagine-video',
    duration: 10,
  }),
});
```

With cURL:

```bash
curl -X 'POST' https://api.x.ai/v1/videos/generations \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "A cat playing with a ball",
      "model": "grok-imagine-video",
      "duration": 10
  }'
```

> **Note**: Video editing doesn't support user-defined `duration`. The edited video will have the same duration as the original video.

### Aspect Ratio

Specify the aspect ratio of the video. Default is `16:9`.

**Supported aspect ratios:**
- `16:9` - Widescreen landscape (default)
- `9:16` - Widescreen portrait
- `4:3` - Standard landscape
- `3:4` - Standard portrait
- `1:1` - Square
- `3:2` - Classic landscape
- `2:3` - Classic portrait

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    aspect_ratio="4:3"
)
print(f"Video URL: {response.url}")
```

Or with manual polling:

```python
response = client.video.start(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    aspect_ratio="4:3"
)
print(f"Request ID: {response.request_id}")
```

With JavaScript:

```javascript
const response = await fetch('https://api.x.ai/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'A cat playing with a ball',
    model: 'grok-imagine-video',
    aspect_ratio: '4:3',
  }),
});
```

With cURL:

```bash
curl -X 'POST' https://api.x.ai/v1/videos/generations \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "A cat playing with a ball",
      "model": "grok-imagine-video",
      "aspect_ratio": "4:3"
  }'
```

### Resolution

Select output resolution from supported options:

**Supported resolutions:**
- `720p` - HD quality
- `480p` - Standard quality

```python
from xai_sdk import Client

client = Client()

response = client.video.generate(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    resolution="720p"
)
print(f"Video URL: {response.url}")
```

Or with manual polling:

```python
response = client.video.start(
    prompt="A cat playing with a ball",
    model="grok-imagine-video",
    resolution="720p"
)
print(f"Request ID: {response.request_id}")
```

With JavaScript:

```javascript
const response = await fetch('https://api.x.ai/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
  },
  body: JSON.stringify({
    prompt: 'A cat playing with a ball',
    model: 'grok-imagine-video',
    resolution: '720p',
  }),
});
```

With cURL:

```bash
curl -X 'POST' https://api.x.ai/v1/videos/generations \
  -H 'Authorization: Bearer $XAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
      "prompt": "A cat playing with a ball",
      "model": "grok-imagine-video",
      "resolution": "720p"
  }'
```

## Request Parameters

### xAI SDK Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Video model (`grok-imagine-video`) |
| `prompt` | string | Text description of the video |
| `duration` | number | Video duration in seconds (1-15) |
| `aspect_ratio` | string | Video aspect ratio (default: `16:9`) |
| `resolution` | string | Output resolution (`720p`, `480p`) |
| `image_url` | string | Source image URL for image-to-video |
| `video_url` | string | Source video URL for editing (max 8.7s) |

### REST API Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Video model (`grok-imagine-video`) |
| `prompt` | string | Text description of the video |
| `duration` | number | Video duration in seconds (1-15) |
| `aspect_ratio` | string | Video aspect ratio (default: `16:9`) |
| `resolution` | string | Output resolution (`720p`, `480p`) |
| `image` | object | `{ "url": "<image_url>" }` for image-to-video |
| `video` | object | `{ "url": "<video_url>" }` for editing (max 8.7s) |

## Response Format

### Initial Response (Manual Polling)

```json
{
  "request_id": "video_gen_abc123"
}
```

### Completed Response

```json
{
  "url": "https://...",
  "duration": 10
}
```

## Best Practices

### Prompt Engineering for Generation

**Good prompts:**
- "A golden retriever running through a field of wildflowers, slow motion, cinematic lighting, 4K"
- "Timelapse of a flower blooming, macro photography, soft natural lighting"

**Structure:** subject + action + setting + camera + lighting + mood

**Tips:**
1. Describe the motion/action clearly
2. Specify camera movements if desired ("pan left", "zoom in")
3. Include lighting and mood
4. Mention video style ("cinematic", "documentary", "slow motion")

### Prompt Engineering for Editing

**Effective edit prompts:**
- Be specific about what to change: "Change the red car to a blue motorcycle" rather than "change the car"
- Describe the desired end state: "Transform the sunny beach into a dramatic sunset scene"
- Use action verbs: add, remove, swap, replace, change, transform, restyle

**Editing tips:**
1. **Be precise** - Clearly identify the object or element to modify
2. **Preserve intent** - Specify what should remain unchanged if needed
3. **Use references** - For styles, reference known aesthetics ("noir film", "anime style")
4. **Layer edits** - For complex changes, consider multiple sequential edits

### Duration Considerations

- Shorter videos generate faster
- Keep prompts focused for best results
- Complex scenes may need more generation time

## Limitations

| Constraint | Value |
|------------|-------|
| Maximum generated video duration | 15 seconds |
| Maximum input video length for editing | 8.7 seconds |
| Edited video duration | Same as original (cannot specify custom duration) |
| Supported resolutions | 720p, 480p |

**Additional notes:**
- Video URLs must be direct, publicly accessible links (not embedded players)
- Streaming is NOT supported for video generation
- Processing times vary based on complexity

## Error Handling

```python
try:
    response = client.video.generate(
        prompt="...",
        model="grok-imagine-video"
    )
except Exception as e:
    if "invalid_url" in str(e):
        print("Invalid video URL format")
    elif "video_too_long" in str(e):
        print("Video exceeds maximum length (8.7s for edits)")
    elif "rate_limit" in str(e):
        print("Rate limit exceeded")
    else:
        print(f"Error: {e}")
```

## Status Values

When using manual polling, check the status field:

| Status | Description |
|--------|-------------|
| `pending` | Request queued |
| `processing` | Video being generated |
| `completed` | Video ready |
| `failed` | Generation failed |
