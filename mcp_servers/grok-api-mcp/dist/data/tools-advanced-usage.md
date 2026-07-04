# Advanced Usage - Tools

Equip your requests with multiple tools and leverage advanced tool capabilities.

## Using Multiple Tools

Include multiple tools in the `tools` array. The model intelligently orchestrates between them based on the task at hand.

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search the web for AI news and create a summary with code analysis"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "x_search"},
        {"type": "code_execution"}
    ]
)
```

## Autonomous Orchestration

Autonomous orchestration enables complex multi-step research and analysis to happen automatically:
- Clients see the final result
- Optional real-time progress indicators via tool call notifications during streaming
- Model uses reasoning capabilities to decide what to search for and how to interpret results

## Image Integration with Tools

Include images in tool-enabled conversations for visual analysis and context-aware searches.

### Bootstrap with Image Context

Pass an image into the conversation context before initiating an agentic request:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Search for more information about this product"},
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

### Enable Image Understanding in Search

Setting `enable_image_understanding` to true equips the agent with access to the `view_image` tool:

```python
tools=[
    {
        "type": "web_search",
        "web_search": {
            "enable_image_understanding": True
        }
    }
]
```

The model can then interpret and analyze image contents encountered during search, incorporating visual information into its context.

**Note**: Enabling this feature increases token usage as images are processed as image tokens.

## Video Understanding in Search

Setting `enable_video_understanding` to true equips the agent with access to the `view_x_video` tool:

```python
tools=[
    {
        "type": "x_search",
        "x_search": {
            "enable_video_understanding": True
        }
    }
]
```

The model can analyze video content from X posts encountered during search. When invoked, you'll see it in `chunk.tool_calls` and `response.tool_calls` with the video_url as a parameter.

## Tool Call Notifications in Streaming

In streaming mode, you receive notifications when the model uses tools:

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Search for latest AI research"}],
    tools=[{"type": "web_search"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.tool_calls:
        for tool_call in chunk.choices[0].delta.tool_calls:
            print(f"Tool called: {tool_call.function.name}")
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## Citations

### All Citations

Complete list of all sources encountered:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Search for AI news"}],
    tools=[{"type": "web_search"}]
)

# Access all citations
citations = response.citations
for citation in citations:
    print(f"Source: {citation.url}")
```

### Inline Citations

Markdown-style links embedded directly in the response text. Must be explicitly enabled:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Search for AI news"}],
    tools=[{"type": "web_search"}],
    tool_options={
        "inline_citations": True
    }
)

# Response text contains inline citations like [1], [2]
print(response.choices[0].message.content)
```

Requires xai-sdk version 1.5.0 or later.

## Location-Based Search

Search tools support location-based queries for more relevant results:

```python
tools=[
    {
        "type": "web_search",
        "web_search": {
            "location": "San Francisco, CA"
        }
    }
]
```

Requires xai-sdk version 1.6.0 or later.

## Combining Custom Functions with Built-in Tools

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search for weather data and save to my database"}
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

## Best Practices

1. **Enable only needed tools**: Each tool adds complexity and potential latency
2. **Use streaming for long operations**: Get real-time progress updates
3. **Enable inline citations**: For verifiable, sourced responses
4. **Consider token usage**: Image/video understanding increases tokens
5. **Handle tool calls gracefully**: Implement proper error handling for tool failures
