# Grok 4 Fast (Non-Reasoning)

Fast inference variant without reasoning tokens for immediate responses while maintaining high quality.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-4-fast-non-reasoning` |
| Context Length | 2,000,000 tokens (2M input + 30K max output) |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Output Speed | 108+ tokens/second |

## Pricing

| Type | Cost per 1M tokens |
|------|-------------------|
| Input | $0.20 |
| Output | $0.50 |
| Cached | $0.05 |
| Web Search | $5.00 per 1K calls |

## Rate Limits

- 4M tokens/minute
- 480 requests/minute

## Capabilities

- Agentic tool calling
- Text and image input support
- No reasoning/thinking tokens (immediate response)
- Prompt caching support
- Web search integration
- Structured outputs

## Supported Parameters

- `temperature`
- `top_p`
- `max_tokens`
- `tools`
- `tool_choice`
- `response_format`
- `seed`
- `logprobs`
- `top_logprobs`
- `structured_outputs`

## Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4-fast-non-reasoning",
    messages=[
        {"role": "user", "content": "Quick answer needed"}
    ]
)
```

## Notes

- No thinking tokens = faster responses
- Same 2M context as reasoning variant
- Same pricing as reasoning variant
- Ideal for high-throughput applications
- Use `grok-4-fast-reasoning` when step-by-step reasoning is needed
