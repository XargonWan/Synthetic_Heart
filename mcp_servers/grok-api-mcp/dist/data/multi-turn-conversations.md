# Multi-Turn Conversations

Learn how to build conversational applications with Grok by managing conversation state across multiple API calls.

## The Stateless API

The Grok chat API is **stateless** - it has no memory of prior interactions beyond what you provide in the `messages` array. Each API call is independent; to maintain context, you must send the full conversation history with every request.

This design gives you complete control over:
- What context the model sees
- How long conversations persist
- When to summarize or truncate history

## Basic Conversation Management

### The Messages Array Pattern

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.x.ai/v1",
    api_key="your-api-key"
)

# Initialize conversation with optional system message
messages = [
    {"role": "system", "content": "You are a helpful assistant."}
]

def chat(user_message: str) -> str:
    """Send a message and get a response, maintaining conversation history."""
    # Add user message to history
    messages.append({"role": "user", "content": user_message})

    # Make API call with full history
    response = client.chat.completions.create(
        model="grok-4",
        messages=messages
    )

    # Extract assistant response
    assistant_message = response.choices[0].message.content

    # Add assistant response to history
    messages.append({"role": "assistant", "content": assistant_message})

    return assistant_message

# Multi-turn conversation
print(chat("My name is Alice."))
# "Hello Alice! Nice to meet you. How can I help you today?"

print(chat("What's my name?"))
# "Your name is Alice, as you just told me!"
```

## ChatApp Class Pattern

For production applications, encapsulate conversation management in a reusable class:

```python
from openai import OpenAI
from typing import Optional

class ChatApp:
    """Manages multi-turn conversations with Grok."""

    def __init__(
        self,
        model: str = "grok-4",
        system_prompt: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=api_key or os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.messages = []

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def chat(self, user_message: str) -> str:
        """Send a message and return the response."""
        self.messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages
        )

        assistant_message = response.choices[0].message.content
        self.messages.append({"role": "assistant", "content": assistant_message})

        return assistant_message

    def reset(self, keep_system: bool = True):
        """Clear conversation history."""
        if keep_system and self.messages and self.messages[0]["role"] == "system":
            self.messages = [self.messages[0]]
        else:
            self.messages = []

    @property
    def history(self) -> list:
        """Return conversation history."""
        return self.messages.copy()

# Usage
app = ChatApp(system_prompt="You are a friendly cooking assistant.")
print(app.chat("I want to make pasta tonight."))
print(app.chat("What ingredients do I need?"))
print(app.chat("How long should I cook it?"))
```

## Streaming Conversations

Add streaming for real-time responses:

```python
class StreamingChatApp:
    """Chat application with streaming responses."""

    def __init__(self, model: str = "grok-4", system_prompt: Optional[str] = None):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.messages = []

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def chat_stream(self, user_message: str):
        """Stream a response token by token."""
        self.messages.append({"role": "user", "content": user_message})

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            stream=True
        )

        full_response = ""
        for chunk in stream:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_response += content
                yield content

        # Store complete response in history
        self.messages.append({"role": "assistant", "content": full_response})

# Usage
app = StreamingChatApp(system_prompt="You are a storyteller.")

print("Assistant: ", end="")
for token in app.chat_stream("Tell me a short story about a robot."):
    print(token, end="", flush=True)
print()
```

## Conversations with Function Calling

Extend the pattern to handle tool calls:

```python
import json
from typing import Callable

class ChatAppWithTools:
    """Chat application with function calling support."""

    def __init__(
        self,
        model: str = "grok-4",
        system_prompt: Optional[str] = None
    ):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.messages = []
        self.tools = []
        self.functions: dict[str, Callable] = {}

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def register_tool(self, name: str, description: str, parameters: dict, func: Callable):
        """Register a function as a tool."""
        self.tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        })
        self.functions[name] = func

    def chat(self, user_message: str) -> str:
        """Send a message, handling any tool calls automatically."""
        self.messages.append({"role": "user", "content": user_message})

        while True:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self.tools if self.tools else None,
                tool_choice="auto" if self.tools else None
            )

            assistant_message = response.choices[0].message

            # Check for tool calls
            if assistant_message.tool_calls:
                # Add assistant message with tool calls to history
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in assistant_message.tool_calls
                    ]
                })

                # Execute each tool call
                for tool_call in assistant_message.tool_calls:
                    func_name = tool_call.function.name
                    arguments = json.loads(tool_call.function.arguments)

                    # Execute function
                    if func_name in self.functions:
                        result = self.functions[func_name](**arguments)
                    else:
                        result = {"error": f"Unknown function: {func_name}"}

                    # Add tool result to history
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result)
                    })

                # Continue loop to get final response
            else:
                # No tool calls - return final response
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_message.content
                })
                return assistant_message.content

# Usage
app = ChatAppWithTools(
    system_prompt="You are a helpful assistant with access to weather data."
)

# Register a weather function
app.register_tool(
    name="get_weather",
    description="Get current weather for a location",
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name, e.g. San Francisco"
            }
        },
        "required": ["location"]
    },
    func=lambda location: {"temp": 72, "condition": "sunny", "location": location}
)

# The app handles tool calls automatically
print(app.chat("What's the weather in Tokyo?"))
print(app.chat("How about New York?"))
```

## Conversations with Images

Handle multi-modal conversations with images:

```python
import base64

class MultiModalChatApp:
    """Chat application supporting text and images."""

    def __init__(self, model: str = "grok-4"):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.messages = []

    def chat(self, text: str, image_url: Optional[str] = None) -> str:
        """Send a message with optional image."""
        if image_url:
            content = [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url, "detail": "high"}
                }
            ]
        else:
            content = text

        self.messages.append({"role": "user", "content": content})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages
        )

        assistant_message = response.choices[0].message.content
        self.messages.append({"role": "assistant", "content": assistant_message})

        return assistant_message

    def chat_with_local_image(self, text: str, image_path: str) -> str:
        """Send a message with a local image file."""
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()

        # Determine MIME type
        ext = image_path.lower().split(".")[-1]
        mime_type = "image/jpeg" if ext in ["jpg", "jpeg"] else "image/png"

        image_url = f"data:{mime_type};base64,{image_data}"
        return self.chat(text, image_url)

# Usage
app = MultiModalChatApp()

# Analyze an image
print(app.chat(
    "What's in this image?",
    image_url="https://example.com/photo.jpg"
))

# Follow-up question (model remembers the image context)
print(app.chat("What colors are prominent?"))
```

## Conversations with Structured Outputs

Get typed responses using Pydantic:

```python
from pydantic import BaseModel
from typing import List

class MenuItem(BaseModel):
    name: str
    price: float
    description: str

class MenuAnalysis(BaseModel):
    items: List[MenuItem]
    cuisine_type: str
    price_range: str

class StructuredChatApp:
    """Chat application with structured output support."""

    def __init__(self, model: str = "grok-4"):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.messages = []

    def chat_structured(self, user_message: str, response_model: type[BaseModel]):
        """Get a structured response matching the Pydantic model."""
        self.messages.append({"role": "user", "content": user_message})

        response = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=self.messages,
            response_format=response_model
        )

        parsed = response.choices[0].message.parsed

        # Store the raw content in history
        self.messages.append({
            "role": "assistant",
            "content": response.choices[0].message.content
        })

        return parsed

# Usage
app = StructuredChatApp()

menu = app.chat_structured(
    "Create a 3-item Italian restaurant menu",
    MenuAnalysis
)

print(f"Cuisine: {menu.cuisine_type}")
for item in menu.items:
    print(f"  - {item.name}: ${item.price:.2f}")
```

## Complete Example: Customer Support Bot

A full-featured example combining all patterns:

```python
import json
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum

class TicketPriority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"

class SupportTicket(BaseModel):
    ticket_id: str
    issue: str
    priority: TicketPriority
    status: str = "open"

class CustomerSupportBot:
    """Multi-turn customer support chatbot with tools and structured outputs."""

    def __init__(self):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.messages = [{
            "role": "system",
            "content": """You are a customer support agent for TechCorp.
            You can create support tickets and look up order status.
            Be helpful, professional, and empathetic."""
        }]
        self.tickets: List[SupportTicket] = []

        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "create_ticket",
                    "description": "Create a support ticket for the customer's issue",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "issue": {
                                "type": "string",
                                "description": "Description of the customer's issue"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                                "description": "Ticket priority level"
                            }
                        },
                        "required": ["issue", "priority"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Look up an order by order ID",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {
                                "type": "string",
                                "description": "The order ID to look up"
                            }
                        },
                        "required": ["order_id"]
                    }
                }
            }
        ]

    def _create_ticket(self, issue: str, priority: str) -> dict:
        """Create a support ticket."""
        import random
        ticket = SupportTicket(
            ticket_id=f"TKT-{random.randint(10000, 99999)}",
            issue=issue,
            priority=TicketPriority(priority)
        )
        self.tickets.append(ticket)
        return ticket.model_dump()

    def _lookup_order(self, order_id: str) -> dict:
        """Look up order status (mock implementation)."""
        # Mock order data
        orders = {
            "ORD-123": {"status": "shipped", "eta": "2 days"},
            "ORD-456": {"status": "processing", "eta": "5 days"},
        }
        if order_id in orders:
            return {"order_id": order_id, **orders[order_id]}
        return {"error": f"Order {order_id} not found"}

    def _execute_tool(self, name: str, arguments: dict) -> dict:
        """Execute a tool and return the result."""
        if name == "create_ticket":
            return self._create_ticket(**arguments)
        elif name == "lookup_order":
            return self._lookup_order(**arguments)
        return {"error": f"Unknown tool: {name}"}

    def chat(self, user_message: str) -> str:
        """Process a customer message."""
        self.messages.append({"role": "user", "content": user_message})

        while True:
            response = self.client.chat.completions.create(
                model="grok-4",
                messages=self.messages,
                tools=self.tools,
                tool_choice="auto"
            )

            assistant_message = response.choices[0].message

            if assistant_message.tool_calls:
                # Add assistant message with tool calls
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in assistant_message.tool_calls
                    ]
                })

                # Execute tools and add results
                for tool_call in assistant_message.tool_calls:
                    result = self._execute_tool(
                        tool_call.function.name,
                        json.loads(tool_call.function.arguments)
                    )
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result)
                    })
            else:
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_message.content
                })
                return assistant_message.content

# Usage
bot = CustomerSupportBot()

# Multi-turn conversation
print(bot.chat("Hi, I ordered something but it hasn't arrived."))
print(bot.chat("The order number is ORD-123"))
print(bot.chat("Actually, I'd like to file a complaint about the delay."))
```

## Managing Long Conversations

For long conversations, manage context window limits:

```python
class ManagedChatApp:
    """Chat app with conversation length management."""

    def __init__(self, model: str = "grok-4", max_messages: int = 50):
        self.client = OpenAI(
            base_url="https://api.x.ai/v1",
            api_key=os.environ.get("XAI_API_KEY")
        )
        self.model = model
        self.max_messages = max_messages
        self.messages = []
        self.system_message = None

    def set_system(self, content: str):
        """Set the system message."""
        self.system_message = {"role": "system", "content": content}

    def _get_messages(self) -> list:
        """Get messages for API call, managing length."""
        msgs = []
        if self.system_message:
            msgs.append(self.system_message)

        # Keep most recent messages within limit
        if len(self.messages) > self.max_messages:
            msgs.extend(self.messages[-self.max_messages:])
        else:
            msgs.extend(self.messages)

        return msgs

    def summarize_and_reset(self) -> str:
        """Summarize conversation and start fresh with summary as context."""
        # Ask model to summarize
        summary_request = self.messages + [{
            "role": "user",
            "content": "Please provide a brief summary of our conversation so far."
        }]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=summary_request
        )

        summary = response.choices[0].message.content

        # Reset with summary as context
        self.messages = [{
            "role": "system",
            "content": f"Previous conversation summary: {summary}"
        }]

        return summary

    def chat(self, user_message: str) -> str:
        """Send a message with automatic context management."""
        self.messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=self._get_messages()
        )

        assistant_message = response.choices[0].message.content
        self.messages.append({"role": "assistant", "content": assistant_message})

        return assistant_message
```

## Server-Side State with xAI SDK

The xAI SDK supports server-side conversation storage using `previous_response_id`:

```python
from xai_sdk import Client
from xai_sdk.chat import user

client = Client(api_key=os.getenv("XAI_API_KEY"))

# First message
chat = client.chat.create(model="grok-4", store_messages=True)
chat.append(user("My favorite color is blue."))
response = chat.sample()

# Store the response ID
response_id = response.id

# Later, continue the conversation
chat = client.chat.create(
    model="grok-4",
    previous_response_id=response_id,
    store_messages=True
)
chat.append(user("What's my favorite color?"))
response = chat.sample()
# Model remembers: "Your favorite color is blue."
```

## Best Practices

1. **Always append to history**: Add both user and assistant messages to maintain context
2. **Handle tool calls properly**: Include tool call metadata and results in the message history
3. **Manage context length**: Implement truncation or summarization for long conversations
4. **Use system messages**: Set behavior and context at the start of conversations
5. **Reset when appropriate**: Clear history when starting new topics or sessions
6. **Error handling**: Wrap API calls in try/except and decide how failures affect history
7. **Serialize for persistence**: Save `messages` array to database for conversation continuity

## Message Role Reference

| Role | Purpose |
|------|---------|
| `system` | Set assistant behavior and context (optional, typically first) |
| `user` | User input messages |
| `assistant` | Model responses (include `tool_calls` when present) |
| `tool` | Results from function/tool execution (requires `tool_call_id`) |
