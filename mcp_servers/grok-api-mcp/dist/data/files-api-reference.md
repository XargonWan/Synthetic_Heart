# Files API Reference

API reference for file operations.

## Base URL

```
https://api.x.ai/v1
```

## Authentication

```
Authorization: Bearer <api-key>
```

## Endpoints

### Upload File

```http
POST /v1/files
```

Upload a file for use in conversations.

**Request**: multipart/form-data
- `file` (required): File to upload
- `purpose` (required): Use case, e.g., "assistants"

**cURL Example**:
```bash
curl https://api.x.ai/v1/files \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -F "file=@document.pdf" \
  -F "purpose=assistants"
```

**Response**:
```json
{
  "id": "file-abc123",
  "object": "file",
  "bytes": 102400,
  "created_at": 1699000000,
  "filename": "document.pdf",
  "purpose": "assistants",
  "status": "processed"
}
```

### List Files

```http
GET /v1/files
```

List all uploaded files.

**Query Parameters**:
- `purpose` (optional): Filter by purpose
- `limit` (optional): Number of results (default: 20)
- `order` (optional): Sort order (asc/desc)
- `after` (optional): Cursor for pagination

**Response**:
```json
{
  "object": "list",
  "data": [
    {
      "id": "file-abc123",
      "object": "file",
      "bytes": 102400,
      "created_at": 1699000000,
      "filename": "document.pdf",
      "purpose": "assistants"
    }
  ],
  "has_more": false
}
```

### Retrieve File

```http
GET /v1/files/{file_id}
```

Get metadata about a specific file.

**Response**:
```json
{
  "id": "file-abc123",
  "object": "file",
  "bytes": 102400,
  "created_at": 1699000000,
  "filename": "document.pdf",
  "purpose": "assistants",
  "status": "processed"
}
```

### Download File Content

```http
GET /v1/files/{file_id}/content
```

Download the contents of a file.

**Response**: File binary content

**cURL Example**:
```bash
curl https://api.x.ai/v1/files/file-abc123/content \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -o downloaded_file.pdf
```

### Delete File

```http
DELETE /v1/files/{file_id}
```

Delete a file.

**Response**:
```json
{
  "id": "file-abc123",
  "object": "file",
  "deleted": true
}
```

## Using Files in Chat

Attach files to messages for document-based conversations:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Upload file
file = client.files.create(
    file=open("report.pdf", "rb"),
    purpose="assistants"
)

# Use in chat
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": "Summarize this document",
            "attachments": [
                {"file_id": file.id}
            ]
        }
    ]
)
```

## File Status

| Status | Description |
|--------|-------------|
| `uploaded` | File uploaded, processing |
| `processed` | Ready to use |
| `error` | Processing failed |

## Supported File Types

- PDF (.pdf)
- Plain text (.txt)
- Markdown (.md)
- Word documents (.docx)
- CSV (.csv)
- JSON (.json)

## Limits

| Limit | Value |
|-------|-------|
| Max file size | 100 MB |
| Max files per account | 100,000 |
| Max total storage | 100 GB |

## Error Responses

### File Too Large

```json
{
  "error": {
    "message": "File size exceeds the maximum allowed size of 100MB",
    "type": "invalid_request_error",
    "code": "file_too_large"
  }
}
```

### Unsupported Format

```json
{
  "error": {
    "message": "File type not supported",
    "type": "invalid_request_error",
    "code": "unsupported_file_type"
  }
}
```

### File Not Found

```json
{
  "error": {
    "message": "No file found with id 'file-xyz'",
    "type": "not_found_error",
    "code": "file_not_found"
  }
}
```

## SDK Requirements

| Feature | xAI SDK Version |
|---------|-----------------|
| Files API | 1.4.0+ |
