# Grok 3 Mini

Streamlined version of Grok-3 optimized for quicker response times.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-3-mini` |
| Context Length | 131,072 tokens (128K input + 8K max output) |
| Knowledge Cutoff | November 2024 |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Release Date | February 17, 2025 |

## Pricing

| Type | Cost per 1M tokens |
|------|-------------------|
| Input | $0.30 |
| Output | $0.50 |

## Capabilities

- Text and image input support
- Function calling
- Structured outputs
- Reasoning with configurable effort
- Supports `reasoning_effort` parameter

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
- `reasoning_effort` (low/high)

## Reasoning Effort

Grok-3-mini supports the `reasoning_effort` parameter:

- `low` - Minimal thinking time, fewer tokens, quick responses
- `high` - Maximum thinking time, more tokens, complex problems

```python
response = client.chat.completions.create(
    model="grok-3-mini",
    messages=[{"role": "user", "content": "Solve this math problem"}],
    reasoning_effort="high"
)
```

## Benchmarks

| Benchmark | Score |
|-----------|-------|
| AIME 2024 | 95.8% |
| AIME 2025 | 90.8% |
| GPQA | 84% |
| LiveCodeBench | 80.4% |

## Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-3-mini",
    messages=[
        {"role": "user", "content": "Quick question about Python"}
    ]
)
```

## Notes

- Only model that supports `reasoning_effort` parameter
- 10x cheaper than grok-3 for input tokens
- 30x cheaper than grok-3 for output tokens
- Ideal for high-volume, cost-sensitive applications
