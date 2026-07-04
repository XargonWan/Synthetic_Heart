# Deferred Requests

Some xAI API operations are processed asynchronously as deferred requests. You send a request, receive a request ID, and retrieve the result later.

## Overview

Deferred requests are used for:
- **Video generation**: All video operations
- **Video editing**: All video edit operations
- **Long-running tasks**: Operations that may take significant time

## How Deferred Requests Work

1. **Submit request**: Send your generation/edit request
2. **Receive ID**: Get a request ID in the response
3. **Poll for status**: Check the status periodically
4. **Retrieve result**: Get the completed result when ready

## Video Generation Example

### Submit Request

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Start generation
response = client.videos.generate(
    model="grok-video",
    prompt="A rocket launching into space"
)

request_id = response.id
print(f"Request ID: {request_id}")
print(f"Status: {response.status}")
```

### Initial Response

```json
{
  "id": "video_gen_abc123",
  "status": "processing",
  "created": 1699000000
}
```

### Poll for Status

```python
import time

def wait_for_completion(client, request_id, timeout=300, interval=5):
    start_time = time.time()

    while time.time() - start_time < timeout:
        result = client.videos.retrieve(request_id)

        if result.status == "completed":
            return result
        elif result.status == "failed":
            raise Exception(f"Request failed: {result.error}")

        print(f"Status: {result.status}")
        time.sleep(interval)

    raise Exception("Request timed out")

# Wait for completion
result = wait_for_completion(client, request_id)
video_url = result.data[0].url
print(f"Video URL: {video_url}")
```

### Completed Response

```json
{
  "id": "video_gen_abc123",
  "status": "completed",
  "created": 1699000000,
  "data": [
    {
      "url": "https://cdn.x.ai/videos/abc123.mp4"
    }
  ]
}
```

## Status Values

| Status | Description |
|--------|-------------|
| `pending` | Request is queued |
| `processing` | Currently being processed |
| `completed` | Successfully completed |
| `failed` | Request failed |

## SDK Auto-Polling

The xAI SDK can handle polling automatically:

```python
# SDK handles polling
response = client.videos.generate(
    model="grok-video",
    prompt="A sunset over mountains",
    wait_for_completion=True  # SDK polls until done
)

# Result is ready
video_url = response.data[0].url
```

## Error Handling

```python
def handle_deferred_request(client, request_id):
    try:
        result = wait_for_completion(client, request_id)
        return result.data[0].url
    except Exception as e:
        if "timed out" in str(e):
            # Request is still processing
            print("Request is taking longer than expected")
            print(f"Check status later with ID: {request_id}")
        elif "failed" in str(e):
            # Request failed
            print(f"Request failed: {e}")
        else:
            raise
```

## Cancelling Requests

For long-running requests, you may be able to cancel:

```python
client.videos.cancel(request_id)
```

## Best Practices

1. **Save request IDs**: Store IDs to check status later
2. **Implement timeouts**: Don't poll indefinitely
3. **Use webhooks if available**: More efficient than polling
4. **Handle failures gracefully**: Retry or notify users
5. **Monitor queue position**: Some APIs provide queue info

## Async/Await Pattern

```python
import asyncio

async def generate_video_async(client, prompt):
    response = client.videos.generate(
        model="grok-video",
        prompt=prompt
    )

    request_id = response.id

    while True:
        result = client.videos.retrieve(request_id)

        if result.status == "completed":
            return result.data[0].url
        elif result.status == "failed":
            raise Exception(f"Failed: {result.error}")

        await asyncio.sleep(5)

# Run multiple generations concurrently
async def main():
    prompts = ["Sunset", "Sunrise", "Storm"]
    tasks = [generate_video_async(client, p) for p in prompts]
    results = await asyncio.gather(*tasks)
    return results
```

## Webhook Notifications

If webhook support is available:

```python
response = client.videos.generate(
    model="grok-video",
    prompt="A rocket launching",
    webhook_url="https://your-server.com/webhook"
)
```

Your server receives a POST when the request completes.
