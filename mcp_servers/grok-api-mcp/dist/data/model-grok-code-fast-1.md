# Grok Code Fast 1

Speedy and economical reasoning model purpose-built for developer workflows. 314B Mixture-of-Experts architecture trained on real pull requests and practical coding tasks.

## Model Details

| Property | Value |
|----------|-------|
| Model ID | `grok-code-fast-1` |
| Context Length | 256,000 tokens (256K input + 10K max output) |
| Input Modality | Text only |
| Output Modality | Text only |
| Output Speed | 254.7 tokens/second |
| Release Date | August 26, 2025 |
| Architecture | 314-billion-parameter Mixture-of-Experts |

## Pricing

| Type | Cost per 1M tokens |
|------|-------------------|
| Input | $0.20 |
| Output | $1.50 |
| Cached | $0.02 |

## Rate Limits

- 2M tokens/minute
- 480 requests/minute

## Capabilities

- Reasoning enabled with visible reasoning traces
- Code-specialized training (not a distilled model)
- Structured outputs
- Tool mastery (grep, terminal, file editing)
- Full software development stack support

## Supported Languages

Exceptionally versatile across:
- TypeScript
- Python
- Java
- Rust
- C++
- Go

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
- `stop_sequences`

## Benchmarks

| Benchmark | Score |
|-----------|-------|
| SWE-Bench Verified | 70.8% |
| LiveCodeBench | 80.0% |

## Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-code-fast-1",
    messages=[
        {"role": "user", "content": "Refactor this function to be more efficient"}
    ]
)
```

## Prompt Engineering Tips

See the [Grok Code Prompt Engineering](./grok-code-prompt-engineering.md) guide for best practices.

## Notes

- Purpose-built for coding, not a general model fine-tuned for code
- Trained on real pull requests and practical coding tasks
- Fastest output speed among Grok models (254.7 tokens/sec)
- Cheapest cached token rate ($0.02/M)
- Does NOT support image input
- Ideal for code editors and pair programming tools
