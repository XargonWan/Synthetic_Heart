# Grok 4 Fast (Reasoning)

High-speed reasoning variant with 2M context window, optimized for faster inference while maintaining strong reasoning through thinking tokens.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-4-fast-reasoning` |
| Context Length | 2,000,000 tokens (2M input + 30K max output) |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Output Speed | 148 tokens/second median |

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

- Agentic tool calling (best in class per xAI)
- Text and image input support
- Reasoning enabled with thinking tokens
- Prompt caching support
- Web search integration
- Structured outputs

## Supported Parameters

- `reasoning`
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
    model="grok-4-fast-reasoning",
    messages=[
        {"role": "user", "content": "Solve this step by step"}
    ]
)
```

## Notes

- 2M context window - largest available
- Uses thinking tokens for step-by-step reasoning
- 15x cheaper than grok-4-0709 for input
- 30x cheaper than grok-4-0709 for output
- Ideal for complex reasoning tasks requiring large context
- Use `grok-4-fast-non-reasoning` for faster responses without thinking tokens
