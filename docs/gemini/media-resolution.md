The `media_resolution` parameter controls how the Gemini API processes media inputs like images, videos, and PDF documents by determining the **maximum number of tokens** allocated for media inputs, allowing you to balance response quality against latency and cost. For different settings, default values and how they correspond to tokens, see the [Token counts](https://ai.google.dev/gemini-api/docs/media-resolution#token-counts) section.

You can configure media resolution in two ways:

- [Per part](https://ai.google.dev/gemini-api/docs/media-resolution#per-part-media-resolution) (Gemini 3 only)

- [Globally](https://ai.google.dev/gemini-api/docs/media-resolution#global-media-resolution) for an entire `generateContent` request (all multimodal models)

## Per-part media resolution (Gemini 3 only)

Gemini 3 allows you to set media resolution for individual media objects within your request, offering fine-grained optimisation of token usage. You can mix resolution levels in a single request. For example, using high resolution for a complex diagram and low resolution for a simple contextual image. This setting overrides any global configuration for a specific part. For default settings, see [Token counts](https://ai.google.dev/gemini-api/docs/media-resolution#token-counts) section.
**Note:** Per-part media resolution is an experimental feature.  

### Python

    from google import genai
    from google.genai import types

    # The media_resolution parameter for parts is currently only available in the v1alpha API version. (experimental)
    client = genai.Client(
      http_options={
          'api_version': 'v1alpha',
      }
    )

    # Replace with your image data
    with open('path/to/image1.jpg', 'rb') as f:
        image_bytes_1 = f.read()

    # Create parts with different resolutions
    image_part_high = types.Part.from_bytes(
        data=image_bytes_1,
        mime_type='image/jpeg',
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH
    )

    model_name = 'gemini-3-pro-preview'

    response = client.models.generate_content(
        model=model_name,
        contents=["Describe these images:", image_part_high]
    )
    print(response.text)

### Javascript

    // Example: Setting per-part media resolution in JavaScript
    import { GoogleGenAI, MediaResolution, Part } from '@google/genai';
    import * as fs from 'fs';
    import { Buffer } from 'buffer'; // Node.js

    const ai = new GoogleGenAI({ httpOptions: { apiVersion: 'v1alpha' } });

    // Helper function to convert local file to a Part object
    function fileToGenerativePart(path, mimeType, mediaResolution) {
        return {
            inlineData: { data: Buffer.from(fs.readFileSync(path)).toString('base64'), mimeType },
            mediaResolution: { 'level': mediaResolution }
        };
    }

    async function run() {
        // Create parts with different resolutions
        const imagePartHigh = fileToGenerativePart('img.png', 'image/png', Part.MediaResolutionLevel.MEDIA_RESOLUTION_HIGH);
        const model_name = 'gemini-3-pro-preview';
        const response = await ai.models.generateContent({
            model: model_name,
            contents: ['Describe these images:', imagePartHigh]
            // Global config can still be set, but per-part settings will override
            // config: {
            //   mediaResolution: MediaResolution.MEDIA_RESOLUTION_MEDIUM
            // }
        });
        console.log(response.text);
    }
    run();

### REST

    # Replace with paths to your images
    IMAGE_PATH="path/to/image.jpg"

    # Base64 encode the images
    BASE64_IMAGE1=$(base64 -w 0 "$IMAGE_PATH")

    MODEL_ID="gemini-3-pro-preview"

    echo '{
        "contents": [{
          "parts": [
            {"text": "Describe these images:"},
            {
              "inline_data": {
                "mime_type": "image/jpeg",
                "data": "'"$BASE64_IMAGE1"'",
              },
              "media_resolution": {"level": "MEDIA_RESOLUTION_HIGH"}
            }
          ]
        }]
      }' > request.json

    curl -s -X POST \
      "https://generativelanguage.googleapis.com/v1alpha/models/${MODEL_ID}:generateContent" \
      -H "x-goog-api-key: $GEMINI_API_KEY" \
      -H "Content-Type: application/json" \
      -d @request.json

## Global media resolution

You can set a default resolution for all media parts in a request using the
`GenerationConfig`. This is supported by all multimodal models. If a request
includes both global and [per-part settings](https://ai.google.dev/gemini-api/docs/media-resolution#per-part-media-resolution), the per-part setting takes precedence for that specific item.  

### Python

    from google import genai
    from google.genai import types

    client = genai.Client()

    # Prepare standard image part
    with open('image.jpg', 'rb') as f:
        image_bytes = f.read()
    image_part = types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

    # Set global configuration
    config = types.GenerateContentConfig(
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH
    )

    response = client.models.generate_content(
        model='gemini-3-flash-preview',
        contents=["Describe this image:", image_part],
        config=config
    )
    print(response.text)

### Javascript

    import { GoogleGenAI, MediaResolution } from '@google/genai';
    import * as fs from 'fs';

    const ai = new GoogleGenAI({ });

    async function run() {
       // ... (Image loading logic) ...

       const response = await ai.models.generateContent({
          model: 'gemini-3-flash-preview',
          contents: ["Describe this image:", imagePart],
          config: {
             mediaResolution: MediaResolution.MEDIA_RESOLUTION_HIGH
          }
       });
       console.log(response.text);
    }
    run();

### REST

    # ... (Base64 encoding logic) ...

    curl -s -X POST \
      "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent" \
      -H "x-goog-api-key: $GEMINI_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "contents": [...],
        "generation_config": {
          "media_resolution": "MEDIA_RESOLUTION_HIGH"
        }
      }'

## Available resolution values

The Gemini API defines the following levels for media resolution:

- `MEDIA_RESOLUTION_UNSPECIFIED`: The default setting. The token count for this level varies significantly between Gemini 3 and earlier Gemini models.
- `MEDIA_RESOLUTION_LOW`: Lower token count, resulting in faster processing and lower cost, but with less detail.
- `MEDIA_RESOLUTION_MEDIUM`: A balance between detail, cost, and latency.
- `MEDIA_RESOLUTION_HIGH`: Higher token count, providing more detail for the model to work with, at the expense of increased latency and cost.
- `MEDIA_RESOLUTION_ULTRA_HIGH` (Per part only): Highest token count, required for specific use cases such as [computer use](https://ai.google.dev/gemini-api/docs/computer-use).

Note that `MEDIA_RESOLUTION_HIGH` provides the optimal performance for most use
cases.

The exact number of tokens generated for each of these
levels depends on both the **media type** (Image, Video, PDF) and the **model
version**.

## Token counts

The tables below summarize the approximate token counts for each
`media_resolution` value and media type per model family.

**Gemini 3 models**

|---|---|---|---|
| **MediaResolution** | **Image** | **Video** | **PDF** |
| `MEDIA_RESOLUTION_UNSPECIFIED` (Default) | 1120 | 70 | 560 |
| `MEDIA_RESOLUTION_LOW` | 280 | 70 | 280 + Native Text |
| `MEDIA_RESOLUTION_MEDIUM` | 560 | 70 | 560 + Native Text |
| `MEDIA_RESOLUTION_HIGH` | 1120 | 280 | 1120 + Native Text |
| `MEDIA_RESOLUTION_ULTRA_HIGH` | 2240 | N/A | N/A |

**Gemini 2.5 models**

|---|---|---|---|---|
| **MediaResolution** | **Image** | **Video** | **PDF (Scanned)** | **PDF (Native)** |
| `MEDIA_RESOLUTION_UNSPECIFIED` (Default) | 256 + Pan \& Scan (\~2048) | 256 | 256 + OCR | 256 + Native Text |
| `MEDIA_RESOLUTION_LOW` | 64 | 64 | 64 + OCR | 64 + Native Text |
| `MEDIA_RESOLUTION_MEDIUM` | 256 | 256 | 256 + OCR | 256 + Native Text |
| `MEDIA_RESOLUTION_HIGH` | 256 + Pan \& Scan | 256 | 256 + OCR | 256 + Native Text |

## Choosing the right resolution

- **Default (`UNSPECIFIED`):** Start with the default. It's tuned for a good balance of quality, latency, and cost for most common use cases.
- **`LOW`:** Use for scenarios where cost and latency are paramount, and fine-grained detail is less critical.
- **`MEDIUM` / `HIGH`:** Increase the resolution when the task requires understanding intricate details within the media. This is often needed for complex visual analysis, chart reading, or dense document comprehension.
- **`ULTRA HIGH`** - Only available for per part setting. Recommended for specific use cases such as computer use or where testing shows a clear enhancement over `HIGH`.
- **Per-part control (Gemini 3):** Optimizes token usage. For example, in a prompt with multiple images, use `HIGH` for a complex diagram and `LOW` or `MEDIUM` for simpler contextual images.

**Recommended settings**

The following lists the recommended media resolution settings for each
supported media type.

|---|---|---|---|
| **Media Type** | **Recommended Setting** | **Max Tokens** | **Usage Guidance** |
| **Images** | `MEDIA_RESOLUTION_HIGH` | 1120 | Recommended for most image analysis tasks to ensure maximum quality. |
| **PDFs** | `MEDIA_RESOLUTION_MEDIUM` | 560 | Optimal for document understanding; quality typically saturates at `medium`. Increasing to `high` rarely improves OCR results for standard documents. |
| **Video** (General) | `MEDIA_RESOLUTION_LOW` (or `MEDIA_RESOLUTION_MEDIUM`) | 70 (per frame) | **Note:** For video, `low` and `medium` settings are treated identically (70 tokens) to optimize context usage. This is sufficient for most action recognition and description tasks. |
| **Video** (Text-heavy) | `MEDIA_RESOLUTION_HIGH` | 280 (per frame) | Required only when the use case involves reading dense text (OCR) or small details within video frames. |

Always test and evaluate the impact of different resolution settings on your
specific application to find the best trade-off between quality, latency, and
cost.

## Version compatibility summary

- The `MediaResolution` enum is available for all models supporting media input.
- The token counts associated with each enum level **differ** between Gemini 3 models and earlier Gemini versions.
- Setting `media_resolution` on individual `Part` objects is **exclusive to
  Gemini 3 models**.

## Next steps

- Learn more about the multimodal capabilities of Gemini API in the [image understanding](https://ai.google.dev/gemini-api/docs/image-understanding), [video understanding](https://ai.google.dev/gemini-api/docs/video-understanding) and [document understanding](https://ai.google.dev/gemini-api/docs/document-processing) guides.