# Stateful Responses with Responses API

The Responses API supports stateful conversations where context is maintained across requests.

## Overview

Stateful responses allow you to:
- Continue conversations without resending history
- Reduce token usage for long conversations
- Maintain context across multiple interactions

## Basic Usage

### Create Initial Response

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# First message
response = client.responses.create(
    model="grok-4",
    input="Hello, my name is Alice. Remember this.",
    store=True  # Enable state storage
)

response_id = response.id
print(response.output_text)
```

### Continue Conversation

```python
# Continue with same context
response = client.responses.create(
    model="grok-4",
    input="What's my name?",
    previous_response_id=response_id  # Reference previous response
)

print(response.output_text)  # "Your name is Alice."
```

## Parameters

### `store`

Enable conversation state storage:

```python
response = client.responses.create(
    model="grok-4",
    input="...",
    store=True
)
```

### `previous_response_id`

Continue from a previous response:

```python
response = client.responses.create(
    model="grok-4",
    input="...",
    previous_response_id="resp_abc123"
)
```

## Response Object

```json
{
  "id": "resp_abc123",
  "object": "response",
  "created": 1699000000,
  "model": "grok-4",
  "output": [...],
  "output_text": "Response text",
  "conversation_id": "conv_xyz789",
  "usage": {
    "input_tokens": 10,
    "output_tokens": 50
  }
}
```

## Conversation Management

### Retrieving a Response

```python
response = client.responses.retrieve("resp_abc123")
```

### Conversation History

The system maintains conversation history when using stateful responses. Each response in a conversation can be referenced to continue the dialogue.

## Use Cases

### Multi-turn Dialogue

```python
# Turn 1
r1 = client.responses.create(
    model="grok-4",
    input="I'm planning a trip to Japan.",
    store=True
)

# Turn 2
r2 = client.responses.create(
    model="grok-4",
    input="What should I pack?",
    previous_response_id=r1.id
)

# Turn 3
r3 = client.responses.create(
    model="grok-4",
    input="What about the best time to visit?",
    previous_response_id=r2.id
)
```

### Customer Support Bot

```python
class SupportBot:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ.get("XAI_API_KEY"),
            base_url="https://api.x.ai/v1"
        )
        self.response_id = None

    def chat(self, message):
        response = self.client.responses.create(
            model="grok-4",
            instructions="You are a helpful customer support agent.",
            input=message,
            store=True,
            previous_response_id=self.response_id
        )
        self.response_id = response.id
        return response.output_text

bot = SupportBot()
print(bot.chat("I have a billing question"))
print(bot.chat("My account number is 12345"))
print(bot.chat("Why was I charged twice?"))
```

## Token Efficiency

Stateful responses are more token-efficient for long conversations:

### Without State (Traditional)
Each request must include full history:
```
Request 1: 100 tokens
Request 2: 200 tokens (includes history)
Request 3: 300 tokens (includes more history)
Total: 600 tokens
```

### With State
Only new input is sent:
```
Request 1: 100 tokens
Request 2: 100 tokens (state preserved)
Request 3: 100 tokens (state preserved)
Total: 300 tokens
```

## Limitations

- State has a maximum retention period
- Very long conversations may be truncated
- State is tied to specific response IDs
- Cannot modify past messages

## Best Practices

1. **Store important responses**: Use `store=True` for conversations you need to continue
2. **Track response IDs**: Save response IDs for later continuation
3. **Handle expiration**: Implement fallback for expired states
4. **Clear context when needed**: Start fresh conversations for new topics

## Error Handling

```python
try:
    response = client.responses.create(
        model="grok-4",
        input="Continue our conversation",
        previous_response_id="expired_or_invalid_id"
    )
except Exception as e:
    if "not_found" in str(e):
        # Start new conversation
        response = client.responses.create(
            model="grok-4",
            input="Continue our conversation",
            store=True
        )
```
