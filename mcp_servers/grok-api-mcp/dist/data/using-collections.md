# Using Collections

Upload and search through your documents by adding them to collections. Collections provide persistent document storage with semantic search across many documents.

## Overview

Collections enable you to:

- **Organize** - Create collections to group related documents
- **Store** - Upload up to 100,000 files per collection
- **Search** - Query documents using natural language semantic search
- **Customize** - Configure chunking, embedding, and metadata settings
- **Manage** - List, update, and delete documents programmatically

## Getting Started

There are two ways to work with Collections:

### Console Approach

Use the xAI Console for manual creation and uploads:

1. Navigate to the Collections page in the Console
2. Create a new collection
3. Upload documents through the UI
4. Search and manage through the interface

See [Using Collections in Console](./using-collections-console.md) for detailed instructions.

### API Approach

Use the SDK or REST API for programmatic management:

1. Create a Management API key with `AddFileToCollection` permission
2. Use the xAI SDK or REST API to create collections
3. Upload documents programmatically
4. Integrate search into your applications

See [Using Collections via API](./using-collections-api.md) for detailed instructions.

## Key Concepts

### Collections

A collection is a container for related documents with:
- Unique identifier
- Name and description
- Embedding index for semantic search
- Configurable metadata fields

### Documents

Documents are files uploaded to collections:
- Supported formats: PDF, TXT, DOCX, MD, CSV, HTML
- Maximum size: 100MB per file
- Automatically chunked and embedded for search
- Can include custom metadata fields

### Search

Search across documents using:
- **Keyword search** - Traditional text matching
- **Semantic search** - Meaning-based search using embeddings
- **Hybrid search** - Combines both methods (recommended)

### Metadata

Add structured attributes to documents:
- Define required fields
- Enforce uniqueness constraints
- Inject into embeddings for better retrieval
- Filter search results by metadata

See [Collections Metadata](./using-collections-metadata.md) for details.

## Integration with Chat

Use the `collections_search` tool to search collections during conversations:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What does our policy say about vacation?"}
    ],
    tools=[{
        "type": "collections_search",
        "collections_search": {
            "collection_ids": ["col_abc123"]
        }
    }]
)
```

## Pricing

| Operation | Cost |
|-----------|------|
| File indexing & storage | Free (first week) |
| Retrieval/searches | $2.50 per 1,000 searches |

## Limits

| Resource | Limit |
|----------|-------|
| Max file size | 100 MB |
| Max files per collection | 100,000 |
| Max total storage | 100 GB |

Contact xAI to increase limits.

## Requirements

- **SDK Version**: 1.4.0+ of `xai-sdk` for collections_search tool
- **API Key**: Management API key with `AddFileToCollection` permission for uploads
- **Models**: Agentic models (grok-4-fast, grok-4) for search integration

## Next Steps

- [Using Collections in Console](./using-collections-console.md)
- [Using Collections via API](./using-collections-api.md)
- [Collections Metadata](./using-collections-metadata.md)
- [Collections Search Tool](./collections-search-tool.md)
- [Collections API Reference](./collections-api-reference.md)
