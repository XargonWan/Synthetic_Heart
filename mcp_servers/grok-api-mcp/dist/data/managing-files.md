# Managing Files

The Files API provides a complete set of operations for managing your files. Before using files in chat conversations, you need to upload them using one of the methods described below.

## Prerequisites

> **xAI Python SDK Users**: Version 1.4.0 of the `xai-sdk` package is required to use the Files API.

## Uploading Files

You can upload files in several ways: from a file path, raw bytes, BytesIO object, or an open file handle.

### Upload from File Path

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a file from disk
file = client.files.upload("/path/to/your/document.pdf")

print(f"File ID: {file.id}")
print(f"Filename: {file.filename}")
print(f"Size: {file.size} bytes")
print(f"Created at: {file.created_at}")
```

### Upload from Bytes

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload file content directly from bytes
content = b"This is my document content.\nIt can span multiple lines."
file = client.files.upload(content, filename="document.txt")

print(f"File ID: {file.id}")
print(f"Filename: {file.filename}")
```

### Upload from File Object

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a file directly from disk
file = client.files.upload(open("document.pdf", "rb"), filename="document.pdf")

print(f"File ID: {file.id}")
print(f"Filename: {file.filename}")
```

### Using OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

file = client.files.create(
    file=open("document.pdf", "rb"),
    purpose="assistants"
)
```

## Upload with Progress Tracking

Track upload progress for large files using callbacks or progress bars.

### Custom Progress Callback

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Define a custom progress callback
def progress_callback(bytes_uploaded: int, total_bytes: int):
    percentage = (bytes_uploaded / total_bytes) * 100 if total_bytes else 0
    mb_uploaded = bytes_uploaded / (1024 * 1024)
    mb_total = total_bytes / (1024 * 1024)
    print(f"Progress: {mb_uploaded:.2f}/{mb_total:.2f} MB ({percentage:.1f}%)")

# Upload with progress tracking
file = client.files.upload(
    "/path/to/large-file.pdf",
    on_progress=progress_callback
)

print(f"Successfully uploaded: {file.filename}")
```

### Progress Bar with tqdm

```python
import os
from xai_sdk import Client
from tqdm import tqdm

client = Client(api_key=os.getenv("XAI_API_KEY"))

file_path = "/path/to/large-file.pdf"
total_bytes = os.path.getsize(file_path)

# Upload with tqdm progress bar
with tqdm(total=total_bytes, unit="B", unit_scale=True, desc="Uploading") as pbar:
    file = client.files.upload(
        file_path,
        on_progress=pbar.update
    )

print(f"Successfully uploaded: {file.filename}")
```

## Listing Files

Retrieve a list of your uploaded files with pagination and sorting options.

### Available Options

- **`limit`**: Maximum number of files to return. If not specified, uses server default of 100.
- **`order`**: Sort order for the files. Either `"asc"` (ascending) or `"desc"` (descending).
- **`sort_by`**: Field to sort by. Options: `"created_at"`, `"filename"`, or `"size"`.
- **`pagination_token`**: Token for fetching the next page of results.

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# List files with pagination and sorting
response = client.files.list(
    limit=10,
    order="desc",
    sort_by="created_at"
)

for file in response.data:
    print(f"File: {file.filename} (ID: {file.id}, Size: {file.size} bytes)")
```

### Pagination

```python
# Get first page
response = client.files.list(limit=10)

for file in response.data:
    print(f"{file.id}: {file.filename}")

# Get next page using pagination token
if response.pagination_token:
    next_response = client.files.list(
        limit=10,
        pagination_token=response.pagination_token
    )
```

## Getting File Metadata

Retrieve detailed information about a specific file.

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Get file metadata by ID
file = client.files.get("file-abc123")

print(f"Filename: {file.filename}")
print(f"Size: {file.size} bytes")
print(f"Created: {file.created_at}")
print(f"Team ID: {file.team_id}")
```

## Getting File Content

Download the actual content of a file.

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Get file content
content = client.files.content("file-abc123")

# Content is returned as bytes
print(f"Content length: {len(content)} bytes")
print(f"Content preview: {content[:100]}")
```

### Save to Disk

```python
content = client.files.content("file-abc123")

with open("downloaded.pdf", "wb") as f:
    f.write(content)
```

## Deleting Files

Remove files when they're no longer needed.

```python
import os
from xai_sdk import Client

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Delete a file
delete_response = client.files.delete("file-abc123")

print(f"Deleted: {delete_response.deleted}")
print(f"File ID: {delete_response.id}")
```

## File Status

| Status | Description |
|--------|-------------|
| `uploaded` | File uploaded, processing |
| `processed` | Ready to use in conversations |
| `error` | Processing failed |

## Limitations and Considerations

### File Size Limits

- **Maximum file size**: 48 MB per file
- **Processing time**: Larger files may take longer to process

### File Retention

- **Cleanup**: Delete files when no longer needed to manage storage
- **Access**: Files are scoped to your team/organization

### Supported Formats

While many text-based formats are supported, the system works best with:

- Structured documents (with clear sections, headings)
- Plain text and markdown
- Documents with clear information hierarchy

Supported file types include:

- Plain text files (.txt)
- Markdown files (.md)
- Code files (.py, .js, .java, etc.)
- CSV files (.csv)
- JSON files (.json)
- PDF documents (.pdf)
- And many other text-based formats

## Limits

| Limit | Value |
|-------|-------|
| Max file size | **48 MB** |
| Max files per account | 100,000 |
| Max total storage | 100 GB |

## Waiting for Processing

```python
import time

def upload_and_wait(client, file_path, timeout=60):
    # Upload file
    file = client.files.upload(file_path)

    # Wait for processing
    start_time = time.time()
    while time.time() - start_time < timeout:
        file = client.files.get(file.id)
        if file.status == "processed":
            return file
        elif file.status == "error":
            raise Exception("File processing failed")
        time.sleep(2)

    raise Exception("File processing timed out")
```

## Error Handling

```python
try:
    file = client.files.upload("/path/to/large_file.pdf")
except Exception as e:
    if "file_too_large" in str(e):
        print("File exceeds 48 MB limit")
    elif "unsupported_file_type" in str(e):
        print("File type not supported")
    elif "quota_exceeded" in str(e):
        print("Storage quota exceeded")
    else:
        raise
```

## Best Practices

1. **Check file status**: Wait for `processed` status before using in chat
2. **Use descriptive names**: Helps identify files later
3. **Clean up unused files**: Delete files when no longer needed
4. **Handle errors gracefully**: Implement retry logic for uploads
5. **Monitor storage**: Track usage to avoid quota limits
6. **Use progress tracking**: For large files, show upload progress to users

## Next Steps

Now that you know how to manage files, learn how to use them in chat conversations:

- [Chat with Files](./chat-with-files.md) - Learn how to attach files to chat messages and query your documents
