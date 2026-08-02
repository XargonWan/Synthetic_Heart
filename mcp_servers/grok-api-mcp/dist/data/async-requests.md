# Asynchronous Requests

When working with the xAI API, you may need to process hundreds or even thousands of requests. Sending these requests sequentially can be extremely time-consuming.

## Overview

To improve efficiency, use:
- `AsyncClient` from `xai_sdk`
- `AsyncOpenAI` from `openai`

These allow you to send multiple requests concurrently.

## Python Async Example

### Using AsyncOpenAI

```python
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

async def get_completion(prompt):
    response = await client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

async def main():
    prompts = [
        "What is Python?",
        "What is JavaScript?",
        "What is Rust?",
        "What is Go?",
        "What is TypeScript?"
    ]

    # Run all requests concurrently
    tasks = [get_completion(prompt) for prompt in prompts]
    results = await asyncio.gather(*tasks)

    for prompt, result in zip(prompts, results):
        print(f"Q: {prompt}")
        print(f"A: {result[:100]}...")
        print()

asyncio.run(main())
```

### Using xAI SDK

```python
import asyncio
from xai_sdk import AsyncClient

client = AsyncClient(api_key=os.environ.get("XAI_API_KEY"))

async def process_batch(items):
    async def process_item(item):
        response = await client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": f"Process: {item}"}]
        )
        return response.choices[0].message.content

    return await asyncio.gather(*[process_item(item) for item in items])
```

## Concurrent Request Management

### Semaphore for Rate Limiting

```python
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# Limit concurrent requests
semaphore = asyncio.Semaphore(10)

async def rate_limited_request(prompt):
    async with semaphore:
        response = await client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content

async def main():
    prompts = [f"Question {i}" for i in range(100)]

    tasks = [rate_limited_request(prompt) for prompt in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Request {i} failed: {result}")
        else:
            print(f"Request {i} succeeded")

asyncio.run(main())
```

### Chunked Processing

```python
async def process_in_chunks(items, chunk_size=10):
    results = []

    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        chunk_results = await asyncio.gather(*[
            get_completion(item) for item in chunk
        ])
        results.extend(chunk_results)

        # Optional: delay between chunks to avoid rate limits
        await asyncio.sleep(1)

    return results
```

## Async Streaming

```python
async def stream_completion(prompt):
    stream = await client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": prompt}],
        stream=True
    )

    full_response = ""
    async for chunk in stream:
        if chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            full_response += content
            print(content, end="", flush=True)

    return full_response
```

## Error Handling

```python
async def robust_request(prompt, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model="grok-4",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            if "429" in str(e):  # Rate limit
                wait_time = 2 ** attempt
                print(f"Rate limited. Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            elif attempt == max_retries - 1:
                raise
            else:
                await asyncio.sleep(1)
```

## Batch API Note

**Important**: The xAI API does not currently offer a dedicated batch API.

For batch processing, use async requests as shown above.

### File Attachment Limitation

File attachments with document search are agentic requests and **do not support batch mode** (n > 1).

## Best Practices

1. **Limit concurrency**: Use semaphores to prevent overwhelming the API
2. **Handle errors gracefully**: Implement retry logic for transient failures
3. **Monitor rate limits**: Respect 429 responses with exponential backoff
4. **Process in chunks**: For very large batches, process in manageable chunks
5. **Use timeouts**: Set appropriate timeouts for requests

```python
async def request_with_timeout(prompt, timeout=30):
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="grok-4",
                messages=[{"role": "user", "content": prompt}]
            ),
            timeout=timeout
        )
        return response.choices[0].message.content
    except asyncio.TimeoutError:
        return None
```

## TypeScript/JavaScript

```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.XAI_API_KEY,
  baseURL: 'https://api.x.ai/v1'
});

async function processPrompts(prompts: string[]) {
  const promises = prompts.map(prompt =>
    client.chat.completions.create({
      model: 'grok-4',
      messages: [{ role: 'user', content: prompt }]
    })
  );

  const results = await Promise.all(promises);
  return results.map(r => r.choices[0].message.content);
}
```
