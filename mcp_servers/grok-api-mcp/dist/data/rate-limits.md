# Consumption and Rate Limits

## Overview

The cost of using the API is based on token consumption, with different prices based on token category:
- Prompt text tokens
- Audio tokens
- Image tokens
- Cached prompt tokens
- Completion tokens
- Reasoning tokens

## Rate Limits

For each tier, there is a maximum amount of:
- **Requests per minute (RPM)**
- **Tokens per minute (TPM)**

These limits ensure fair usage by all users of the system.

### Rate Limit Error

Once your request frequency has reached the rate limit, you will receive error code **429** in response.

```json
{
  "error": {
    "message": "Rate limit exceeded",
    "type": "rate_limit_error",
    "code": 429
  }
}
```

### Requesting Higher Limits

To request a higher rate limit, email **support@x.ai** with your anticipated volume.

## Token Pricing

Requests using xAI-provided server-side tools are priced based on:
- Token usage
- Server-side tool invocations

Costs scale with query complexity.

### Cached Tokens

Using cached input tokens can significantly reduce costs. Tokens that match previous requests may be served from cache at a reduced rate.

## Pricing by Token Type

| Token Type | Description |
|------------|-------------|
| Prompt tokens | Input text tokens |
| Completion tokens | Generated output tokens |
| Reasoning tokens | Tokens used for model reasoning |
| Image tokens | Tokens for image processing |
| Audio tokens | Tokens for audio processing |
| Cached tokens | Previously processed tokens (discounted) |

## Model-Specific Pricing

Different models have different pricing tiers. Check the [Models and Pricing](https://docs.x.ai/docs/models) page for current rates.

## Monitoring Usage

### Usage Explorer

You can see usage with grouping to easily compare consumption across groups.

For example, compare consumptions across test and production API keys by selecting **Group by: API Key**.

### Usage Dashboard Features

- Real-time usage tracking
- Historical usage data
- Cost breakdown by model
- API key-level analytics

## Billing

### Prepaid Credits

- Pre-purchase credits for your team
- API consumption deducted from remaining prepaid credits
- No overage charges until credits exhausted

### Monthly Invoiced Billing

- xAI generates monthly invoice based on API consumption
- Used when prepaid credits are not available
- Billed at the end of each billing period

## Best Practices for Cost Optimization

### 1. Use Appropriate Models

Choose the right model for your task:
- Simple tasks → grok-4-1-fast-non-reasoning (cheaper, faster)
- Complex tasks → grok-4 (more capable)

### 2. Optimize Prompts

- Be concise in system prompts
- Avoid redundant context
- Use efficient prompt structures

### 3. Leverage Caching

- Repeated prompts benefit from token caching
- Structure requests to maximize cache hits

### 4. Monitor Usage

- Regularly check the Usage Explorer
- Set up alerts for usage thresholds
- Track costs by API key/project

### 5. Use Streaming Wisely

- Streaming doesn't change token costs
- But can improve perceived latency

## Handling Rate Limits

### Exponential Backoff

```python
import time
import random

def make_request_with_backoff(client, **kwargs):
    max_retries = 5
    base_delay = 1

    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"Rate limited. Waiting {delay:.2f}s...")
                time.sleep(delay)
            else:
                raise
```

### Request Queuing

```python
import asyncio
from collections import deque

class RateLimitedClient:
    def __init__(self, client, rpm_limit=60):
        self.client = client
        self.rpm_limit = rpm_limit
        self.request_times = deque()

    async def make_request(self, **kwargs):
        # Wait if at rate limit
        while len(self.request_times) >= self.rpm_limit:
            oldest = self.request_times[0]
            if time.time() - oldest > 60:
                self.request_times.popleft()
            else:
                await asyncio.sleep(1)

        self.request_times.append(time.time())
        return self.client.chat.completions.create(**kwargs)
```

## Status Monitoring

If there is an ongoing service disruption:
- Visit https://status.x.ai for latest updates
- RSS feed available at https://status.x.ai/feed.xml
