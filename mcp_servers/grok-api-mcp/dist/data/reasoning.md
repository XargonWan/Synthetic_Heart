# Reasoning

Grok 4 and other reasoning models provide advanced reasoning capabilities for complex problem-solving tasks.

## Overview

Reasoning models like Grok 4 are designed to:
- Break down complex problems into steps
- Show their thought process
- Provide more accurate answers for difficult tasks
- Handle multi-step reasoning and analysis

## Grok 4 Reasoning

Grok 4 is a reasoning model with **no non-reasoning mode available**. It always applies extended reasoning to generate responses.

### Important Notes

- `reasoning_effort` parameter is **not supported** by Grok 4. Providing it will result in an error.
- The `presencePenalty`, `frequencyPenalty`, and `stop` parameters are **not supported** by reasoning models.

## Using Reasoning Models

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": "Solve this step by step: If a train travels 120 miles in 2 hours, then slows down and travels 80 miles in 2 more hours, what is the average speed for the entire journey?"
        }
    ]
)

print(response.choices[0].message.content)
```

## Reasoning vs Non-Reasoning Models

| Feature | Reasoning Models | Non-Reasoning Models |
|---------|------------------|---------------------|
| Extended thinking | Yes | No |
| Complex problem solving | Excellent | Good |
| Response speed | Slower | Faster |
| Token usage | Higher | Lower |
| Best for | Analysis, math, coding | Quick responses, simple tasks |

## Model Variants

### Grok 4
- Full reasoning capability
- No non-reasoning mode
- 2M context length
- Best for complex tasks

### Grok 4.1 Fast Reasoning
- Reasoning enabled
- Optimized for speed
- 131K context length
- Good for agentic tool calling

### Grok 4.1 Fast Non-Reasoning
- No extended reasoning
- Fastest responses
- 131K context length
- Best for simple tasks

### Grok 4 Fast Reasoning
- Balanced reasoning
- Cost-efficient
- Good for moderate complexity

### Grok 4 Fast Non-Reasoning
- No extended reasoning
- Very fast
- Best for high-throughput simple tasks

## When to Use Reasoning Models

**Use reasoning models for:**
- Mathematical problems
- Logic puzzles
- Code debugging and analysis
- Multi-step planning
- Complex document analysis
- Scientific reasoning

**Use non-reasoning models for:**
- Simple Q&A
- Text summarization
- Quick translations
- High-volume simple tasks
- Cost-sensitive applications

## Reasoning Tokens

Reasoning models may generate "reasoning tokens" that represent the model's thought process. These tokens contribute to the total token count and cost.

```json
{
  "usage": {
    "prompt_tokens": 50,
    "completion_tokens": 200,
    "reasoning_tokens": 150,
    "total_tokens": 400
  }
}
```

## Best Practices

1. **Be specific**: Clearly state what you want the model to reason about
2. **Provide context**: Give relevant background information
3. **Ask for steps**: Request step-by-step explanations when needed
4. **Use appropriate models**: Match model capability to task complexity
5. **Monitor costs**: Reasoning models use more tokens
