# Using Collections via API

This guide walks you through managing collections programmatically using the xAI SDK and REST API.

## Creating a Management Key

To use the Collections API, you need to create a Management API Key with the `AddFileToCollection` permission. This permission is required for uploading documents to collections.

1. Navigate to the **Management Keys** section in the [xAI Console](https://console.x.ai)
2. Click on **Create Management Key**
3. Select the `AddFileToCollection` permission along with any other permissions you need
4. If you need to perform operations other than uploading documents (such as creating, updating, or deleting collections), enable the corresponding permissions in the **Collections Endpoint** group
5. Copy and securely store your Management API Key

> **Important**: Make sure to copy your Management API Key immediately after creation. You won't be able to see it again.

## Creating a Collection

### Using xAI SDK

```python
import os
from xai_sdk import Client

client = Client(
    api_key=os.getenv("XAI_API_KEY"),
    management_api_key=os.getenv("XAI_MANAGEMENT_API_KEY"),
    timeout=3600,
)

collection = client.collections.create(
    name="SEC Filings",
)

print(collection)
```

### Using REST API

```bash
curl -X POST https://management-api.x.ai/v1/collections \
  -H "Authorization: Bearer $XAI_MANAGEMENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "SEC Filings",
    "description": "Company SEC filings and financial documents"
  }'
```

## Listing Collections

```python
# ... Create client
collections = client.collections.list()
print(collections)
```

## Viewing Collection Configuration

```python
# ... Create client
collection = client.collections.get("collection_dbc087b1-6c99-493d-86c6-b401fee34a9d")

print(collection)
```

## Updating Collection Configuration

```python
# ... Create client
collection = client.collections.update(
    "collection_dbc087b1-6c99-493d-86c6-b401fee34a9d",
    name="SEC Filings (Updated)"
)

print(collection)
```

## Uploading Documents

Uploading a document to a collection is a two-step process:

1. Upload the file to the xAI API
2. Add the uploaded file to your collection

The SDK abstracts this into a single `upload_document` call that handles both steps automatically.

### Using xAI SDK

```python
# ... Create client
with open("tesla-20241231.html", "rb") as file:
    file_data = file.read()

document = client.collections.upload_document(
    collection_id="collection_dbc087b1-6c99-493d-86c6-b401fee34a9d",
    name="tesla-20241231.html",
    data=file_data,
)
print(document)
```

### Using REST API

```bash
curl -X POST "https://management-api.x.ai/v1/collections/{collection_id}/documents" \
  -H "Authorization: Bearer $XAI_MANAGEMENT_API_KEY" \
  -F "name=document.pdf" \
  -F "data=@document.pdf" \
  -F "content_type=application/pdf"
```

### Uploading with Metadata Fields

If your collection has [metadata fields](./using-collections-metadata.md) defined, include them using the `fields` parameter:

```python
# ... Create client
with open("paper.pdf", "rb") as file:
    file_data = file.read()

document = client.collections.upload_document(
    collection_id="collection_dbc087b1-6c99-493d-86c6-b401fee34a9d",
    name="paper.pdf",
    data=file_data,
    fields={
        "author": "Sandra Kim",
        "year": "2024",
        "title": "Q3 Revenue Analysis"
    },
)
print(document)
```

## Searching Documents

You can search documents using the SDK or REST API. You can also search documents using the Responses API with the `file_search` tool. See the [Collections Search Tool](./collections-search-tool.md) guide for more details.

### Using xAI SDK

```python
# ... Create client
response = client.collections.search(
    query="What were the key revenue drivers based on the SEC filings?",
    collection_ids=["collection_dbc087b1-6c99-493d-86c6-b401fee34a9d"],
)
print(response)
```

### Using REST API

```bash
curl https://api.x.ai/v1/documents/search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
      "query": "What were the key revenue drivers based on the SEC filings?",
      "source": {
          "collection_ids": [
              "collection_dbc087b1-6c99-493d-86c6-b401fee34a9d"
          ]
      },
      "retrieval_mode": {"type": "hybrid"}
  }'
```

### Search Modes

There are three search methods available:

| Mode | Description | Best for | Drawbacks |
|------|-------------|----------|-----------|
| **Keyword** | Searches for exact matches of specified words, phrases, or numbers | Precise terms (e.g., account numbers, dates, specific figures) | May miss contextually relevant content |
| **Semantic** | Understands meaning and context to find conceptually related content | Discovering general ideas, topics, or intent even when exact words differ | Less precise for specific terms |
| **Hybrid** | Combines keyword and semantic search for broader and more accurate results | Most real-world use cases | Slightly higher latency |

By default, the system uses **hybrid search**, which generally delivers the best and most comprehensive results.

Set the retrieval mode in REST API:
- `"retrieval_mode": {"type": "hybrid"}` - Hybrid search (default)
- `"retrieval_mode": {"type": "keyword"}` - Keyword search
- `"retrieval_mode": {"type": "semantic"}` - Semantic search

## Deleting a Document

```python
# ... Create client

client.collections.remove_document(
    collection_id="collection_dbc087b1-6c99-493d-86c6-b401fee34a9d",
    file_id="file_55a709d4-8edc-4f83-84d9-9f04fe49f832",
)
```

## Deleting a Collection

```python
# ... Create client

client.collections.delete(collection_id="collection_dbc087b1-6c99-493d-86c6-b401fee34a9d")
```

**Warning**: Deleting a collection removes all documents within it.

## Document Status

| Status | Description |
|--------|-------------|
| `processing` | Document is being indexed |
| `ready` | Document is ready for search |
| `failed` | Processing failed |

## Error Handling

```python
try:
    document = client.collections.upload_document(
        collection_id=collection_id,
        name="large_file.pdf",
        data=file_data,
    )
except Exception as e:
    if "file_too_large" in str(e):
        print("File exceeds 100MB limit")
    elif "quota_exceeded" in str(e):
        print("Storage quota exceeded")
    else:
        raise
```

## Next Steps

- [Metadata Fields](./using-collections-metadata.md) - Learn how to attach structured attributes to documents for filtering and contextual embeddings
