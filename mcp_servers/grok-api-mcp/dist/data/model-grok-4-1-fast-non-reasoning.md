# Grok 4.1 Fast (Non-Reasoning)

Non-reasoning variant using no thinking tokens for immediate responses, optimized for real-time applications and agentic workflows.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-4-1-fast-non-reasoning` |
| Context Length | 2,000,000 tokens (2M input + 30K max output) |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Output Speed | 90 characters/second, 108+ tokens/second |
| Release Date | November 17, 2025 |
| Elo Rating | #2 at 1465 Elo (non-reasoning mode) |

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

- Agentic tool calling optimized for agent workflows
- Text and image input support
- No reasoning/thinking tokens (immediate response)
- Prompt caching support
- Web search integration
- Structured outputs
- Long-horizon reinforcement learning trained
- **50% reduced hallucination rate** compared to Grok 4 Fast

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
    model="grok-4-1-fast-non-reasoning",
    messages=[
        {"role": "user", "content": "Quick response needed"}
    ]
)
```

## Notes

- Fastest response times in the Grok 4.1 family
- 50% fewer hallucinations than previous generation
- Ideal for high-throughput, real-time applications
- Use `grok-4-1-fast-reasoning` when step-by-step reasoning is needed
