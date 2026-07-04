# Tools Overview

The agent tools API provides powerful capabilities for enhanced AI interactions.

## Types of Tools

### 1. Built-in Tools
Pre-configured tools provided by xAI:
- Web search tools
- X (Twitter) search tools
- Code execution
- Collections search
- Attachment search

### 2. Custom Functions
User-defined functions for specific integrations:
- External API calls
- Database queries
- Custom business logic

## Citation Information

The agent tools API provides two types of citation information:

### All Citations
A complete list of all sources encountered during the response.

### Inline Citations
Markdown-style links embedded directly in the response text at points where the model references sources, with structured metadata available.

**Note**: Inline citations must be explicitly enabled.

## Enabling Tools

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search for the latest AI news"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "code_execution"}
    ]
)
```

## Multiple Tools

Equipping requests with multiple tools is straightforward—include the tools you want in the `tools` array:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find tweets about AI and summarize trends"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "x_semantic_search"},
        {"type": "code_execution"}
    ]
)
```

The model intelligently orchestrates between tools based on the task at hand.

## Tool Selection

Control tool usage with `tool_choice`:

```python
# Auto - model decides (default)
tool_choice="auto"

# None - disable tools
tool_choice="none"

# Required - must use a tool
tool_choice="required"

# Specific tool
tool_choice={"type": "function", "function": {"name": "web_search"}}
```

## Built-in Tools Reference

| Tool | Description |
|------|-------------|
| `web_search` | Search the web |
| `web_search_with_snippets` | Search with text snippets |
| `browse_page` | Fetch content from URL |
| `x_user_search` | Search X users |
| `x_keyword_search` | Search X by keywords |
| `x_semantic_search` | Semantic X search |
| `x_thread_fetch` | Fetch X threads |
| `code_execution` | Execute Python code |
| `collections_search` | Search document collections |
| `attachment_search` | Search file attachments |

## Combining Built-in and Custom Tools

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search for weather and save to database"}
    ],
    tools=[
        {"type": "web_search"},
        {
            "type": "function",
            "function": {
                "name": "save_to_database",
                "description": "Save data to the database",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "data": {"type": "string"},
                        "table": {"type": "string"}
                    },
                    "required": ["data", "table"]
                }
            }
        }
    ]
)
```

## Image Integration with Tools

Include images in tool-enabled conversations for visual analysis:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Search for more info about this product"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/product.jpg"}
                }
            ]
        }
    ],
    tools=[
        {"type": "web_search"}
    ]
)
```

## Best Practices

1. **Enable only needed tools**: Each tool adds complexity
2. **Use appropriate tools**: Match tools to the task
3. **Handle tool results**: Process and validate tool outputs
4. **Monitor costs**: Tool usage may incur additional charges
5. **Consider latency**: More tools may increase response time
