# Object Detection

Language-driven object detection using Grok's vision capabilities. Detect, count, and locate objects using natural language instead of traditional computer vision models.

## Overview

Grok enables "language-driven vision" — using natural language prompts to perform object detection tasks that traditionally required specialized CV models. This approach excels at:

- Counting objects in complex scenes
- Locating objects by description
- Detecting objects by specific criteria (color, brand, pose, behavior)
- Identifying niche or uncommon objects
- Recognizing text across multiple languages

### When to Use Language-Driven Detection

| Use Case | Language-Driven (Grok) | Traditional CV |
|----------|------------------------|----------------|
| Arbitrary object types | ✓ Best | Limited to trained classes |
| Complex criteria (e.g., "red cars made by Tesla") | ✓ Best | Requires multiple models |
| Multilingual text detection | ✓ Best | Language-specific models |
| Real-time high-throughput | Consider latency | ✓ Best |
| Pixel-precise bounding boxes | Approximate | ✓ Best |

## Basic Setup

### With xAI SDK (Recommended)

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

def detect_objects(image_url: str, prompt: str) -> str:
    chat = client.chat.create(model="grok-4", store_messages=False)
    chat.append(
        user(prompt, image(image_url=image_url, detail="high"))
    )
    response = chat.sample()
    return str(response)
```

### With OpenAI SDK

```python
import base64
from openai import OpenAI

client = OpenAI(
    base_url="https://api.x.ai/v1",
    api_key=os.environ.get("XAI_API_KEY")
)

def encode_image(image_path: str) -> str:
    """Encode a local image to base64."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def detect_objects(image_source: str, prompt: str) -> str:
    # Handle both URLs and local files
    if image_source.startswith(("http://", "https://")):
        image_url = image_source
    else:
        base64_data = encode_image(image_source)
        image_url = f"data:image/jpeg;base64,{base64_data}"

    response = client.chat.completions.create(
        model="grok-4",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
                {"type": "text", "text": prompt}
            ]
        }]
    )
    return response.choices[0].message.content
```

## Counting Objects

Count objects in complex scenes with natural language:

```python
# Simple counting
result = detect_objects(
    "https://example.com/parking-lot.jpg",
    "How many cars are in this image? Count carefully and provide the total."
)

# Counting with criteria
result = detect_objects(
    "https://example.com/crowd.jpg",
    "How many people are wearing red shirts?"
)

# Counting multiple object types
result = detect_objects(
    "https://example.com/street.jpg",
    "Count the following in this image: cars, motorcycles, bicycles, pedestrians. "
    "Provide each count separately."
)
```

### Prompting Tips for Accurate Counts

- Ask the model to "count carefully" or "count systematically"
- For large quantities, ask for region-by-region counting
- Request confidence levels: "How confident are you in this count?"
- For ambiguous scenes, ask for a range: "Estimate the number, providing a range if uncertain"

## Locating Objects

Identify where objects are positioned in an image:

```python
# General location
result = detect_objects(
    "https://example.com/room.jpg",
    "Where is the cat in this image? Describe its position relative to other objects."
)

# Multiple objects with positions
result = detect_objects(
    "https://example.com/office.jpg",
    "List all electronic devices visible and describe where each one is located."
)

# Spatial relationships
result = detect_objects(
    "https://example.com/scene.jpg",
    "Describe the spatial arrangement of people in this image. "
    "Who is in the foreground, middle ground, and background?"
)
```

### Location Description Formats

Request specific formats for structured output:

```python
result = detect_objects(
    image_url,
    """Identify all vehicles in this image. For each vehicle, provide:
    - Type (car, truck, motorcycle, etc.)
    - Color
    - Position (left/center/right, foreground/background)
    - Any identifying features

    Format as a numbered list."""
)
```

## Specialized Detection Tasks

### Detection by Specific Criteria

```python
# By brand
result = detect_objects(
    "https://example.com/parking.jpg",
    "Are there any Tesla vehicles in this image? If so, identify the model."
)

# By color
result = detect_objects(
    "https://example.com/flowers.jpg",
    "Identify all yellow flowers. What species might they be?"
)

# By behavior or pose
result = detect_objects(
    "https://example.com/animals.jpg",
    "Which animals in this image are sleeping or resting?"
)

# By condition
result = detect_objects(
    "https://example.com/fruit.jpg",
    "Which pieces of fruit appear overripe or damaged?"
)
```

### Niche Object Identification

Grok can identify specialized objects without training:

```python
# Technical equipment
result = detect_objects(
    "https://example.com/lab.jpg",
    "Identify the laboratory equipment visible. Name specific models if recognizable."
)

# Rare items
result = detect_objects(
    "https://example.com/collection.jpg",
    "Are there any vintage cameras in this image? Identify makes and approximate eras."
)
```

## Multilingual Text Recognition

Detect and read text across languages:

```python
# General text detection
result = detect_objects(
    "https://example.com/sign.jpg",
    "What text is visible in this image? Include text in any language."
)

# Language-specific
result = detect_objects(
    "https://example.com/document.jpg",
    "Extract all Japanese text from this image and provide translations."
)

# Mixed language documents
result = detect_objects(
    "https://example.com/menu.jpg",
    "This menu has text in multiple languages. Extract all text, "
    "organizing by language, and translate non-English text."
)
```

## Generating Test Images

Use `grok-imagine-image` to generate images for testing your detection workflows:

### With xAI SDK

```python
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Generate a test scene
response = client.image.generate(
    model="grok-imagine-image",
    prompt="A busy parking lot with various car brands including Tesla, BMW, and Toyota, "
           "some cars are red, some are blue, realistic photograph",
    image_format="url"
)

test_image_url = response.url
print(f"Generated test image: {test_image_url}")

# Now detect objects in the generated image
detection_result = detect_objects(
    test_image_url,
    "Count all vehicles by brand and color."
)
```

### With OpenAI SDK

```python
response = client.images.generate(
    model="grok-imagine-image",
    prompt="A street scene with pedestrians, cyclists, and various vehicles, "
           "some people wearing bright colored clothing, urban setting",
    n=1,
    size="1024x1024"
)

test_image_url = response.data[0].url
```

### Test Image Generation Tips

- Be specific about object quantities for counting tests
- Include variety in object attributes (colors, sizes, orientations)
- Specify "realistic photograph" for detection-appropriate images
- Generate edge cases (occlusion, crowded scenes, poor lighting)

## Streaming Responses

For real-time feedback during detection:

### With xAI SDK

```python
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

chat = client.chat.create(model="grok-4", store_messages=False)
chat.append(
    user(
        "Analyze this image systematically. List every distinct object you can identify.",
        image(image_url="https://example.com/complex-scene.jpg", detail="high")
    )
)

for token in chat.stream():
    print(token, end="", flush=True)
```

### With OpenAI SDK

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=[{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
            {"type": "text", "text": "Describe every object in this scene."}
        ]
    }],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Best Practices

### Use High Detail Mode

Always use `detail: "high"` for object detection tasks:

```python
# xAI SDK
image(image_url=url, detail="high")

# OpenAI SDK
{"type": "image_url", "image_url": {"url": url, "detail": "high"}}
```

### Structured Prompts for Consistent Output

```python
detection_prompt = """Analyze this image for object detection.

Task: {task}

Provide your response in this format:
1. Objects Found: [list each object]
2. Count: [total number]
3. Confidence: [high/medium/low]
4. Notes: [any uncertainties or observations]
"""

result = detect_objects(image_url, detection_prompt.format(task="Count all vehicles"))
```

### Handle Uncertainty

Detection results may not always be accurate. Build in verification:

```python
# Ask for confidence
result = detect_objects(
    image_url,
    "Count the birds in this image. Rate your confidence (high/medium/low) "
    "and explain any factors that make counting difficult."
)

# Request multiple passes
result = detect_objects(
    image_url,
    "Count the people in this image using two methods: "
    "1) Count from left to right "
    "2) Count by groupings "
    "Compare your counts and provide a final estimate."
)
```

### Disable Server-Side Storage

When processing many images, disable storage to avoid issues:

```python
# xAI SDK
chat = client.chat.create(model="grok-4", store_messages=False)

# OpenAI SDK - use unique conversation IDs or stateless requests
```

## Recommended Models

| Task | Model | Notes |
|------|-------|-------|
| Complex detection | `grok-4` | Best accuracy, use `detail: "high"` |
| Simple detection | `grok-4` | Can use `detail: "auto"` for speed |
| Generate test images | `grok-imagine-image` | Create synthetic test data |

## Limitations

- Results are probabilistic, not deterministic
- No pixel-precise bounding box coordinates
- May struggle with very small objects or extreme occlusion
- Counting accuracy decreases with quantity (50+ similar objects)
- Processing time increases with image complexity
- Maximum image size: 20 MiB

## Example: Complete Detection Pipeline

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

def analyze_scene(image_url: str) -> dict:
    """Complete scene analysis with multiple detection tasks."""

    chat = client.chat.create(model="grok-4", store_messages=False)
    chat.append(
        user(
            """Perform a comprehensive analysis of this image:

            1. OBJECTS: List all distinct objects visible
            2. PEOPLE: Count people and describe their activities
            3. TEXT: Extract any visible text (any language)
            4. VEHICLES: Identify vehicles by type and color
            5. SETTING: Describe the location/environment

            Format each section clearly.""",
            image(image_url=image_url, detail="high")
        )
    )

    response = chat.sample()
    return {"analysis": str(response), "image": image_url}

# Run analysis
result = analyze_scene("https://example.com/street-scene.jpg")
print(result["analysis"])
```
