# Frequently Asked Questions

## API Billing FAQ

### How am I charged for API usage?

You are charged based on token consumption:
- **Prompt tokens**: Input text tokens
- **Completion tokens**: Generated output tokens
- **Reasoning tokens**: Tokens used for model reasoning
- **Image tokens**: Tokens for image processing
- **Audio tokens**: Tokens for audio processing
- **Cached tokens**: Previously processed tokens (discounted rate)

### What payment methods are accepted?

- Credit/debit cards
- Prepaid credits
- Monthly invoiced billing (for eligible accounts)

### How do prepaid credits work?

- Pre-purchase credits for your team
- API consumption is deducted from available credits
- No overage charges until credits are exhausted
- Credits don't expire

### How does monthly invoiced billing work?

- xAI generates a monthly invoice based on API consumption
- Used when prepaid credits are not available
- Billed at the end of each billing period

### Where can I see my usage?

Use the Usage Explorer in the xAI Console to:
- View real-time usage
- See historical data
- Compare usage across API keys
- Track costs by model

### How do I get a higher rate limit?

Email support@x.ai with:
- Your account information
- Anticipated volume
- Use case description

## Grok Website/Apps FAQ

### Is the xAI API separate from the Grok website?

Yes. X Premium purchases won't affect your service status on xAI API, and vice versa. These are separate offerings.

### Can I use my X Premium subscription for API access?

No, API access requires a separate xAI API account and API key.

## Technical FAQ

### What is the knowledge cutoff date?

The knowledge cutoff date for Grok 3 and Grok 4 is **November 2024**.

### How do I get real-time information?

Enable server-side search tools:
- `web_search` - Search the web
- `x_keyword_search` - Search X posts
- `x_semantic_search` - Semantic X search

### Which models support function calling?

All Grok models support function calling:
- grok-4 (recommended)
- grok-4-fast
- grok-4-1-fast-reasoning
- grok-4-1-fast-non-reasoning
- grok-code-fast-1
- grok-3

### What image formats are supported?

Supported formats for image understanding:
- JPEG/JPG
- PNG

### What are the context length limits?

| Model | Context Length |
|-------|----------------|
| grok-4 | 2,000,000 tokens |
| grok-4-fast | 131,072 tokens |
| grok-code-fast-1 | 256,000 tokens |
| grok-3 | 131,072 tokens |

### Are there rate limits?

Yes. Rate limits vary by tier and include:
- Requests per minute (RPM)
- Tokens per minute (TPM)

Error code 429 indicates rate limit exceeded.

### How do I handle rate limit errors?

Implement exponential backoff:

```python
import time

def make_request_with_retry(func, max_retries=5):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if "429" in str(e):
                wait = 2 ** attempt
                time.sleep(wait)
            else:
                raise
```

### What parameters are not supported by reasoning models?

Grok 4 (reasoning model) does not support:
- `presencePenalty`
- `frequencyPenalty`
- `stop`
- `reasoning_effort`

Adding these will result in an error.

### Is there a batch API?

The xAI API does not currently offer a dedicated batch API. Use async requests for high-volume processing.

### How do I check service status?

- Status page: https://status.x.ai
- RSS feed: https://status.x.ai/feed.xml

## SDK FAQ

### Which SDK should I use?

| SDK | When to Use |
|-----|-------------|
| xAI SDK | Full feature access, all products |
| OpenAI SDK | Easy migration, familiar API |
| Anthropic SDK | Anthropic-style requests |

### What xAI SDK version do I need?

| Feature | Minimum Version |
|---------|-----------------|
| Agentic tool calling | 1.3.1 |
| Files API | 1.4.0 |
| Inline citations | 1.5.0 |
| Location-based search | 1.6.0 |

### Is the API compatible with OpenAI's SDK?

Yes, the xAI API is fully compatible with the OpenAI SDK. Just change the base URL:

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-xai-api-key",
    base_url="https://api.x.ai/v1"
)
```
