# Files API

The Files API enables you to upload documents and use them in chat conversations with Grok. When you attach files to a chat message, the system automatically activates the `attachment_search` tool, transforming your request into an agentic workflow where Grok can intelligently search through and reason over your documents to answer questions.

> **xAI Python SDK Users**: Version 1.4.0 of the `xai-sdk` package is required to use the Files API.

> **Looking for Collections?** If you need persistent document storage with semantic search across many documents, see [Collections](./collections.md). Files are different—they're for attaching documents to chat conversations for immediate context.

## How Files Work with Chat

Behind the scenes, when you attach files to a chat message, the xAI API implicitly adds the `attachment_search` server-side tool to your request. This means:

- **Automatic Agentic Behavior**: Your chat request becomes an agentic request, where Grok autonomously searches through your documents
- **Intelligent Document Analysis**: The model can reason over document content, extract relevant information, and synthesize answers
- **Multi-Document Support**: You can attach multiple files, and Grok will search across all of them

This seamless integration allows you to simply attach files and ask questions—the complexity of document search and retrieval is handled automatically by the agentic workflow.

## Understanding Document Search

When you attach files to a chat message, the xAI API automatically activates the `attachment_search` server-side tool. This transforms your request into an agentic workflow where Grok:

- **Analyzes your query** to understand what information you're seeking
- **Searches the documents** intelligently, finding relevant sections across all attached files
- **Extracts and synthesizes information** from multiple sources if needed
- **Provides a comprehensive answer** with the context from your documents

### Agentic Workflow

Just like other agentic tools (web search, X search, code execution), document search operates autonomously:

- **Multiple searches**: The model may search documents multiple times with different queries to find comprehensive information
- **Reasoning**: The model uses its reasoning capabilities to decide what to search for and how to interpret the results
- **Streaming visibility**: In streaming mode, you can see when the model is searching your documents via tool call notifications

### Token Usage with Files

File-based chats follow similar token patterns to other agentic requests:

- **Prompt tokens**: Include the conversation history and internal processing. Document content is processed efficiently
- **Reasoning tokens**: Used for planning searches and analyzing document content
- **Completion tokens**: The final answer text
- **Cached tokens**: Repeated document content benefits from prompt caching for efficiency

The actual document content is processed by the server-side tool and doesn't directly appear in the message history, keeping token usage optimized.

### Pricing

Document search is billed at **$10 per 1,000 tool invocations**, in addition to standard token costs. Each time the model searches your documents, it counts as one tool invocation.

## Quick Example

Here's a quick example of the complete workflow:

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file

client = Client(api_key=os.getenv("XAI_API_KEY"))

# 1. Upload a document
document_content = b"""Quarterly Sales Report - Q4 2024
Total Revenue: $5.2M
Growth: +18% YoY
"""

uploaded_file = client.files.upload(document_content, filename="sales.txt")

# 2. Chat with the file
chat = client.chat.create(model="grok-4-fast")
chat.append(user("What was the total revenue?", file(uploaded_file.id)))

# 3. Get the answer
response = chat.sample()
print(response.content)  # "The total revenue was $5.2M"

# 4. Clean up
client.files.delete(uploaded_file.id)
```

## Key Features

### Multiple File Support

Attach multiple documents to a single query and Grok will search across all of them to find relevant information. See [Chat with Files](./chat-with-files.md#multiple-file-attachments).

### Multi-Turn Conversations

File context persists across conversation turns, allowing you to ask follow-up questions without re-attaching files. See [Chat with Files](./chat-with-files.md#multi-turn-conversations-with-files).

### Code Execution Integration

Combine files with the code execution tool to perform advanced data analysis, statistical computations, and transformations on your uploaded data. The model can write and execute Python code that processes your files directly. See [Chat with Files](./chat-with-files.md#combining-files-with-code-execution).

## Uploading Files

### Endpoint

```
POST https://api.x.ai/v1/files
```

### Basic Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Upload a file
with open("document.pdf", "rb") as f:
    file = client.files.create(
        file=f,
        purpose="assistants"
    )

print(f"File ID: {file.id}")
```

### Supported File Types

- Plain text files (.txt)
- Markdown files (.md)
- Code files (.py, .js, .java, etc.)
- CSV files (.csv)
- JSON files (.json)
- PDF documents (.pdf)
- And many other text-based formats

### File Limits

| Limit | Value |
|-------|-------|
| Maximum file size | **48 MB** |
| Maximum files (global) | 100,000 |
| Maximum total size | 100 GB |

Contact xAI to increase these limits.

## Chat with Files

### Basic File Chat

```python
# Upload file first
file = client.files.create(
    file=open("report.pdf", "rb"),
    purpose="assistants"
)

# Use file in chat
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": "Summarize the key findings in this document",
            "attachments": [
                {"file_id": file.id}
            ]
        }
    ]
)

print(response.choices[0].message.content)
```

### Multiple Files

```python
file1 = client.files.create(file=open("doc1.pdf", "rb"), purpose="assistants")
file2 = client.files.create(file=open("doc2.pdf", "rb"), purpose="assistants")

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": "Compare these two documents",
            "attachments": [
                {"file_id": file1.id},
                {"file_id": file2.id}
            ]
        }
    ]
)
```

### Combining Files with Images

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Compare this chart with the data in the PDF"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/chart.png"}
                }
            ],
            "attachments": [
                {"file_id": file.id}
            ]
        }
    ]
)
```

## Managing Files

### List Files

```python
files = client.files.list()

for file in files.data:
    print(f"{file.id}: {file.filename} ({file.bytes} bytes)")
```

### Retrieve File Info

```python
file = client.files.retrieve("file-abc123")
print(f"Filename: {file.filename}")
print(f"Size: {file.bytes}")
print(f"Created: {file.created_at}")
```

### Delete File

```python
client.files.delete("file-abc123")
```

### Download File Content

```python
content = client.files.content("file-abc123")
with open("downloaded.pdf", "wb") as f:
    f.write(content.read())
```

## Recommended Models

For best document understanding:
- **grok-4-fast** - Good balance of speed and understanding
- **grok-4** - Best understanding, slower

## Limitations

- **File size**: Maximum 48 MB per file
- **No batch requests**: File attachments with document search are agentic requests and do not support batch mode (`n > 1`)
- **Agentic models only**: Requires models that support agentic tool calling (e.g., `grok-4-fast`, `grok-4`)

## Error Handling

```python
try:
    file = client.files.create(
        file=open("large_file.pdf", "rb"),
        purpose="assistants"
    )
except Exception as e:
    if "file_too_large" in str(e):
        print("File exceeds 48 MB size limit")
    elif "unsupported_format" in str(e):
        print("File format not supported")
    else:
        print(f"Upload failed: {e}")
```

## Best Practices

1. **Organize files**: Use meaningful filenames for easier management
2. **Clean up**: Delete files when no longer needed
3. **Use streaming**: See real-time progress during document analysis
4. **Use appropriate models**: Choose grok-4 for complex document analysis
5. **Be specific in queries**: Help the model find relevant sections
6. **Well-structured documents**: Documents with clear hierarchy work best

## Next Steps

- [Managing Files](./managing-files.md) - Learn how to upload, list, retrieve, and delete files
- [Chat with Files](./chat-with-files.md) - Explore how to attach files to chat messages and query your documents
