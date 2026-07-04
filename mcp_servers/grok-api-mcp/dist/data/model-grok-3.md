# Grok 3

xAI's advanced reasoning model with exceptional benchmark performance.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-3` |
| Context Length | 131,072 tokens (128K input + 8K max output) |
| Knowledge Cutoff | November 2024 |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Release Date | February 17, 2025 |

## Pricing

| Type | Cost per 1M tokens |
|------|-------------------|
| Input | $3.00 |
| Output | $15.00 |

## Capabilities

- Text and image input support
- Function calling
- Structured outputs
- Reasoning enabled
- Real-time web search capable

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

## Benchmarks

| Benchmark | Score |
|-----------|-------|
| AIME 2025 | 93.3% |
| GPQA | 84.6% |
| LiveCodeBench | 79.4% |
| MMMU | 78.0% |

## Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-3",
    messages=[
        {"role": "user", "content": "Explain quantum computing"}
    ]
)
```

## Notes

- Ten times more computational resources than Grok-2
- Does not support `reasoning_effort` parameter
- Use `grok-3-mini` for faster, more economical responses
