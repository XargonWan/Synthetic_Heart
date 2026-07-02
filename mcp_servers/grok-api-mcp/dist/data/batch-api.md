# Batch API

Process multiple requests efficiently using the Batch API.

## Overview

The Batch API allows you to:
- Create batches of requests
- Process many requests asynchronously
- Retrieve results when processing completes
- Manage batch lifecycle

## Endpoints

### Create a New Batch

```http
POST /v1/batches
```

Create a new batch for processing.

**Request Body**:
```json
{
  "name": "my-batch",
  "description": "Batch of classification requests",
  "model": "grok-4",
  "endpoint": "/v1/chat/completions"
}
```

**Response**:
```json
{
  "id": "batch_abc123",
  "object": "batch",
  "name": "my-batch",
  "status": "created",
  "created_at": "2024-01-20T10:00:00Z",
  "request_count": 0
}
```

### List Batches

```http
GET /v1/batches
```

List all batches.

**Query Parameters**:
- `limit` (optional): Number of results
- `after` (optional): Cursor for pagination
- `status` (optional): Filter by status

**Response**:
```json
{
  "object": "list",
  "data": [
    {
      "id": "batch_abc123",
      "name": "my-batch",
      "status": "completed",
      "request_count": 100,
      "completed_count": 100
    }
  ],
  "has_more": false
}
```

### Get Batch

```http
GET /v1/batches/{batch_id}
```

Get details of a specific batch.

**Response**:
```json
{
  "id": "batch_abc123",
  "object": "batch",
  "name": "my-batch",
  "status": "processing",
  "created_at": "2024-01-20T10:00:00Z",
  "request_count": 100,
  "completed_count": 45,
  "failed_count": 2
}
```

### List Batch Requests

```http
GET /v1/batches/{batch_id}/requests
```

List all requests in a batch.

**Query Parameters**:
- `limit` (optional): Number of results
- `after` (optional): Cursor for pagination
- `status` (optional): Filter by status (pending, completed, failed)

**Response**:
```json
{
  "object": "list",
  "data": [
    {
      "id": "req_xyz789",
      "custom_id": "request-1",
      "status": "completed"
    }
  ],
  "has_more": true
}
```

### Add Batch Requests

```http
POST /v1/batches/{batch_id}/requests
```

Add requests to a batch.

**Request Body**:
```json
{
  "requests": [
    {
      "custom_id": "request-1",
      "body": {
        "model": "grok-4",
        "messages": [
          {"role": "user", "content": "Classify: positive or negative? 'Great product!'"}
        ]
      }
    },
    {
      "custom_id": "request-2",
      "body": {
        "model": "grok-4",
        "messages": [
          {"role": "user", "content": "Classify: positive or negative? 'Terrible experience.'"}
        ]
      }
    }
  ]
}
```

### Get Processing Results

```http
GET /v1/batches/{batch_id}/results
```

Get results of completed requests.

**Query Parameters**:
- `limit` (optional): Number of results
- `after` (optional): Cursor for pagination

**Response**:
```json
{
  "object": "list",
  "data": [
    {
      "id": "req_xyz789",
      "custom_id": "request-1",
      "status": "completed",
      "response": {
        "id": "chatcmpl-abc",
        "choices": [
          {
            "message": {
              "role": "assistant",
              "content": "positive"
            }
          }
        ]
      }
    }
  ],
  "has_more": false
}
```

### Cancel Batch Processing

```http
POST /v1/batches/{batch_id}/cancel
```

Cancel processing of a batch.

**Response**:
```json
{
  "id": "batch_abc123",
  "status": "cancelling"
}
```

## Batch Status

| Status | Description |
|--------|-------------|
| `created` | Batch created, awaiting requests |
| `pending` | Requests added, awaiting processing |
| `processing` | Currently processing |
| `completed` | All requests processed |
| `failed` | Batch failed |
| `cancelling` | Cancellation in progress |
| `cancelled` | Batch cancelled |

## Python Example

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Create batch
batch = client.batches.create(
    name="classification-batch",
    model="grok-4",
    endpoint="/v1/chat/completions"
)

# Add requests
requests = [
    {
        "custom_id": f"req-{i}",
        "body": {
            "model": "grok-4",
            "messages": [{"role": "user", "content": f"Classify: {text}"}]
        }
    }
    for i, text in enumerate(texts)
]

client.batches.add_requests(batch.id, requests=requests)

# Start processing
client.batches.start(batch.id)

# Poll for completion
import time
while True:
    batch = client.batches.retrieve(batch.id)
    if batch.status == "completed":
        break
    elif batch.status == "failed":
        raise Exception("Batch failed")
    time.sleep(10)

# Get results
results = client.batches.list_results(batch.id)
for result in results.data:
    print(f"{result.custom_id}: {result.response.choices[0].message.content}")
```

## Use Cases

### Bulk Classification

Process thousands of classification requests:
- Sentiment analysis
- Content moderation
- Category tagging

### Data Processing

Transform large datasets:
- Summarization
- Translation
- Data extraction

### Evaluation

Run evaluations across many test cases:
- Model benchmarking
- Quality assessment

## Pricing

Batch requests may have different pricing than real-time requests. Check the pricing page for current rates.

## Limitations

- Maximum requests per batch
- Processing time depends on queue
- Some features may not be available in batch mode
- File attachments with document search don't support batch mode

## Best Practices

1. **Use custom IDs**: Track requests with meaningful identifiers
2. **Monitor progress**: Poll batch status periodically
3. **Handle failures**: Some requests may fail; handle them appropriately
4. **Batch similar requests**: Group similar workloads for efficiency
5. **Plan for latency**: Batch processing is not real-time
