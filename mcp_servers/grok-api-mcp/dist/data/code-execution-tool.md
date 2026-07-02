# Code Execution Tool

The code execution tool enables Grok to write and execute Python code in real-time, dramatically expanding its capabilities beyond text generation.

## Overview

This powerful feature allows Grok to:
- Perform precise calculations
- Complex data analysis
- Statistical computations
- Solve mathematical problems that would be impossible through text alone
- Generate and test code

Code execution happens in a **secure, isolated environment**.

## Key Use Cases

### Mathematical Computations
```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Calculate the compound interest on $10,000 at 5% for 10 years"}
    ],
    tools=[{"type": "code_execution"}]
)
```

### Data Analysis
```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Analyze this CSV data and find trends: [data]"}
    ],
    tools=[{"type": "code_execution"}]
)
```

### Financial Modeling
Complex financial calculations, portfolio analysis, risk assessment.

### Scientific Computing
Statistical analysis, numerical methods, simulations.

### Code Generation & Testing
Write code and verify it works by executing it.

## Enabling Code Execution

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Calculate the first 20 Fibonacci numbers"}
    ],
    tools=[
        {"type": "code_execution"}
    ]
)

print(response.choices[0].message.content)
```

## Available Libraries

The sandboxed environment includes common Python libraries:
- **numpy** - Numerical computing
- **pandas** - Data manipulation
- **matplotlib** - Visualization
- **scipy** - Scientific computing
- **sympy** - Symbolic mathematics
- **requests** - HTTP requests (limited)

## Response Format

When code execution is used, the response includes execution results:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Here are the first 20 Fibonacci numbers: [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597, 2584, 4181]",
        "tool_calls": [
          {
            "type": "code_execution",
            "code_execution": {
              "code": "def fibonacci(n):\n    fibs = [0, 1]\n    for i in range(2, n):\n        fibs.append(fibs[-1] + fibs[-2])\n    return fibs[:n]\n\nprint(fibonacci(20))",
              "output": "[0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597, 2584, 4181]"
            }
          }
        ]
      }
    }
  ]
}
```

## Combining with Other Tools

Code execution works well with other tools:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search for Bitcoin price history and plot it"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "code_execution"}
    ]
)
```

## Visualization

The model can generate visualizations using matplotlib:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Create a bar chart showing the top 5 programming languages by popularity"}
    ],
    tools=[{"type": "code_execution"}]
)
```

## Security

- Code runs in an isolated sandbox
- No access to the host filesystem
- Limited network access
- Execution time limits
- Memory limits

## Best Practices

1. **Be specific**: Clearly describe what calculation or analysis you need
2. **Provide data**: Include data inline or reference attached files
3. **Request visualization**: Ask for charts when visual output is helpful
4. **Verify results**: Review the generated code and output
5. **Iterative refinement**: Ask follow-up questions to refine analysis

## Limitations

- Execution time limits apply
- Some libraries may not be available
- Network requests are restricted
- Cannot persist data between executions
- File system access is sandboxed
