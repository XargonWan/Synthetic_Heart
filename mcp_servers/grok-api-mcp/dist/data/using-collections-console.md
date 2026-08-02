# Using Collections in Console

This guide walks you through managing collections using the [xAI Console](https://console.x.ai) interface.

## Creating a New Collection

1. Navigate to the **Collections** tab in the [xAI Console](https://console.x.ai)
2. **Make sure you are in the correct team**
3. Click on **"Create new collection"** to create a new collection

### Embeddings Setting

You can choose to enable **generate embeddings on document upload** or not. We recommend leaving the generate embeddings setting **on** for optimal search performance.

## Viewing and Editing Collection Configuration

You can view and edit the collection's configuration by clicking on **Edit Collection**.

This opens a modal where you can:
- View the current configuration
- Change the collection name
- Update the description
- Modify the embeddings setting

## Adding a Document to the Collection

1. Click on a collection in the collections table to view its documents
2. Click **"Upload document"** to upload a new document
3. Select one or more files from your computer

### Supported Formats

- PDF (.pdf)
- Plain text (.txt)
- Word documents (.docx)
- Markdown (.md)
- And many more text-based formats

### Document Details

Once the upload has completed, each document is given a **File ID**. You can view:
- **File ID** - Unique identifier for the file
- **Collection ID** - The collection this document belongs to
- **Hash** - Document content hash

To view these details, click on the document in the documents table.

### Wait for Processing

Documents are processed and indexed automatically. Processing time depends on document size and complexity.

## Deleting Documents and Collections

You can delete documents and collections by clicking on the **more button** (⋮) on the right side of the collections or documents table.

**Note**: Deleted documents and collections cannot be recovered.

## Testing Queries

### Using the Console

1. Select a collection
2. Open the "Query" or "Test" tab
3. Enter a natural language question
4. View the response with citations

### Example

**Query**: "What is the vacation policy?"

**Response**: "According to the employee handbook, full-time employees receive 15 days of paid vacation per year. [1]"

## Collection Settings

### Collection Info

- View collection ID (needed for API calls)
- See document count
- Check storage usage

### Access Controls

- View who has access to collections
- Manage permissions (if available)

## Using Collections in API

After creating collections in the console, use them via API:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What does the policy say about remote work?"}
    ],
    tools=[
        {
            "type": "collections_search",
            "collections_search": {
                "collection_ids": ["col_abc123"]  # From console
            }
        }
    ]
)
```

## Finding Collection IDs

1. Open the collection in console
2. Click on a document in the collection
3. The Collection ID is displayed in the document details
4. Copy the ID (format: `collection_xxxxx`)

## Best Practices

### Organization

- Create separate collections for different topics
- Use clear, descriptive names
- Add descriptions for team clarity

### Documents

- Use meaningful file names
- Keep documents focused on specific topics
- Update documents when content changes

### Maintenance

- Regularly review collection contents
- Remove outdated documents
- Monitor storage usage

## Limits

| Limit | Value |
|-------|-------|
| Max file size | 100 MB |
| Max files per collection | 100,000 |
| Max total storage | 100 GB |

Contact xAI to increase limits.

## Troubleshooting

### Document Not Processing

- Check file format is supported
- Verify file isn't corrupted
- Check file size limits

### Query Not Finding Content

- Ensure document processing is complete
- Try different query phrasing
- Check document actually contains the information

### Upload Failures

- Check internet connection
- Verify file size is within limits
- Try uploading fewer files at once
