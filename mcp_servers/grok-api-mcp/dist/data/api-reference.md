# REST API Reference

Reference of all available xAI REST API endpoints.

## Base URL

```
https://api.x.ai
```

## Authentication

All requests require an API key in the Authorization header:

```
Authorization: Bearer YOUR_API_KEY
```

## Endpoints

### Chat & Responses

#### Create Response (Preferred)

```
POST /v1/responses
```

Create a new response using the Responses API.

**Request Body**:
```json
{
  "model": "grok-4",
  "input": "Your message here",
  "instructions": "Optional system instructions",
  "stream": false,
  "tools": []
}
```

#### Retrieve Response

```
GET /v1/responses/{response_id}
```

Retrieve a specific response by ID.

#### Create Chat Completion (Legacy)

```
POST /v1/chat/completions
```

Legacy chat completion endpoint (OpenAI-compatible).

**Request Body**:
```json
{
  "model": "grok-4",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"}
  ],
  "temperature": 1.0,
  "max_tokens": 1000,
  "stream": false
}
```

### Images

#### Generate Image

```
POST /v1/images/generations
```

Generate images from text prompts.

**Request Body**:
```json
{
  "model": "grok-imagine-image",
  "prompt": "A futuristic cityscape",
  "n": 1,
  "size": "1024x1024"
}
```

#### Edit Image

```
POST /v1/images/edits
```

Edit an existing image.

**Request Body** (multipart/form-data):
- `image`: Original image file
- `mask`: Mask image file
- `prompt`: Edit description
- `n`: Number of images
- `size`: Output size

### Videos

#### Generate Video

```
POST /v1/videos/generations
```

Generate videos from text prompts. Returns a request ID for polling.

**Request Body**:
```json
{
  "model": "grok-video",
  "prompt": "A rocket launching",
  "duration": 5
}
```

#### Retrieve Video

```
GET /v1/videos/{request_id}
```

Retrieve video generation result.

### Models

#### List All Models

```
GET /v1/models
```

List all available models.

**Response**:
```json
{
  "data": [
    {
      "id": "grok-4",
      "object": "model",
      "created": 1699000000,
      "owned_by": "xai"
    }
  ]
}
```

#### List Language Models

```
GET /v1/language-models
```

List available language models only.

#### Retrieve Model

```
GET /v1/models/{model_id}
```

Get details about a specific model.

### Files

#### Upload File

```
POST /v1/files
```

Upload a file for use in conversations.

**Request Body** (multipart/form-data):
- `file`: File to upload
- `purpose`: Use case (e.g., "assistants")

#### List Files

```
GET /v1/files
```

List all uploaded files.

#### Retrieve File

```
GET /v1/files/{file_id}
```

Get file metadata.

#### Download File Content

```
GET /v1/files/{file_id}/content
```

Download file content.

#### Delete File

```
DELETE /v1/files/{file_id}
```

Delete a file.

### Tokenization

#### Tokenize Text

```
POST /v1/tokenize-text
```

Convert text to tokens.

**Request Body**:
```json
{
  "model": "grok-4",
  "text": "Hello, world!"
}
```

**Response**:
```json
{
  "tokens": [15496, 11, 995, 0],
  "count": 4
}
```

### Voice (WebSocket)

#### Voice Agent (Realtime)

```
WSS wss://api.x.ai/v1/realtime
```

WebSocket endpoint for real-time voice conversations. Only available in us-east-1 region.

#### Ephemeral Token

```
POST https://api.x.ai/v1/realtime/client_secrets
```

Get ephemeral token for client-side voice authentication. Required for browser-based voice applications.

## Common Parameters

### Chat/Response Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `model` | string | Model ID (required) |
| `messages`/`input` | array/string | Input messages |
| `temperature` | number | Sampling temperature (0-2) |
| `top_p` | number | Nucleus sampling |
| `max_tokens` | integer | Max output tokens |
| `stream` | boolean | Enable streaming |
| `tools` | array | Available tools |
| `tool_choice` | string/object | Tool selection |
| `response_format` | object | Output format (JSON schema) |

### Tool Choice Values

- `"auto"` - Model decides (default)
- `"none"` - Disable tools
- `"required"` - Must use a tool
- `{"type": "function", "function": {"name": "..."}}` - Specific tool

## Error Responses

```json
{
  "error": {
    "message": "Error description",
    "type": "error_type",
    "code": 400
  }
}
```

### Error Codes

| Code | Meaning |
|------|---------|
| 400 | Bad Request |
| 401 | Unauthorized |
| 403 | Forbidden |
| 404 | Not Found |
| 429 | Rate Limited |
| 500 | Server Error |
| 503 | Service Unavailable |

## Rate Limits

Rate limits vary by tier. Contact support@x.ai for higher limits.

## SDKs

The API is compatible with:
- OpenAI SDK (Python, Node.js)
- Anthropic SDK
- Native xAI SDK
