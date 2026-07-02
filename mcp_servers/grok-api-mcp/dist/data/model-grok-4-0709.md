# Grok 4 (0709)

Specific dated release of Grok-4 from July 9, 2024. Premium baseline for complex synthesis, analysis, and instruction following.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-4-0709` |
| Context Length | 256,000 tokens (256K input + 33K max output) |
| Knowledge Cutoff | November 2024 |
| Input Modalities | Text, Images |
| Output Modality | Text |
| Estimated Parameters | ~1.7 trillion |

## Pricing

| Type | Cost per 1M tokens |
|------|-------------------|
| Input | $3.00 |
| Output | $15.00 |

## Capabilities

- Text and image input support
- Parallel tool calling
- Structured outputs
- Real-time web search with X platform integration
- Voice mode with camera input
- Advanced semantic search on social data

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

## Tool Selection

Tool Selection Score: **0.85** - Consistently routes requests to correct APIs/databases.

## Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4-0709",
    messages=[
        {"role": "user", "content": "Analyze this complex problem"}
    ]
)
```

## Notes

- Reasoning is always enabled (no non-reasoning mode)
- Does NOT support `reasoning_effort` parameter
- Does NOT support `presencePenalty`, `frequencyPenalty`, or `stop` parameters
- Use real-time web search for information beyond knowledge cutoff
- Premium model for complex analytical tasks
