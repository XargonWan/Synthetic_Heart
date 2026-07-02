# Remote MCP Tools

Integrate Model Context Protocol (MCP) tools with the xAI API for extended functionality.

## Overview

Remote MCP Tools allow you to connect external MCP servers to Grok, enabling access to custom tools and data sources through the standardized MCP protocol.

## What is MCP?

The Model Context Protocol (MCP) is an open standard for connecting AI models to external tools and data sources. It provides:
- Standardized tool definitions
- Secure communication
- Extensible architecture

## Configuring Remote MCP Tools

### Basic Setup

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Query the database for recent orders"}
    ],
    tools=[
        {
            "type": "mcp",
            "mcp": {
                "server_url": "https://your-mcp-server.com",
                "server_name": "database-tools"
            }
        }
    ]
)
```

### Authentication

```python
tools=[
    {
        "type": "mcp",
        "mcp": {
            "server_url": "https://your-mcp-server.com",
            "server_name": "secure-tools",
            "auth": {
                "type": "bearer",
                "token": "your-auth-token"
            }
        }
    }
]
```

## MCP Server Requirements

Your MCP server must:
1. Implement the MCP protocol specification
2. Be accessible via HTTPS
3. Respond to tool discovery requests
4. Handle tool execution requests

## Tool Discovery

When you specify an MCP server, Grok will:
1. Query the server for available tools
2. Receive tool definitions (name, description, parameters)
3. Make tools available for the conversation

## Example MCP Server Response

```json
{
  "tools": [
    {
      "name": "query_database",
      "description": "Execute a read-only SQL query",
      "parameters": {
        "type": "object",
        "properties": {
          "query": {
            "type": "string",
            "description": "SQL query to execute"
          }
        },
        "required": ["query"]
      }
    }
  ]
}
```

## Use Cases

### Database Integration
Connect to your database through an MCP server for natural language queries.

### CRM Access
Query customer data, update records, create tickets.

### Custom APIs
Wrap any internal API as MCP tools for Grok access.

### Document Systems
Search and retrieve from internal document management systems.

## Combining with Built-in Tools

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Search the web for market trends and update our database"}
    ],
    tools=[
        {"type": "web_search"},
        {
            "type": "mcp",
            "mcp": {
                "server_url": "https://your-mcp-server.com",
                "server_name": "database-tools"
            }
        }
    ]
)
```

## Security Considerations

1. **Use HTTPS**: Always use encrypted connections
2. **Authenticate**: Implement proper authentication
3. **Limit permissions**: Only expose necessary operations
4. **Audit logging**: Log all tool invocations
5. **Rate limiting**: Protect against abuse

## Error Handling

```python
try:
    response = client.chat.completions.create(
        model="grok-4",
        messages=[{"role": "user", "content": "..."}],
        tools=[{"type": "mcp", "mcp": {...}}]
    )
except Exception as e:
    if "mcp_server_unreachable" in str(e):
        print("MCP server is not accessible")
    elif "mcp_auth_failed" in str(e):
        print("MCP authentication failed")
```

## Best Practices

1. **Descriptive tool names**: Help Grok understand when to use each tool
2. **Clear descriptions**: Document what each tool does
3. **Validate inputs**: Check parameters before execution
4. **Handle timeouts**: MCP servers should respond promptly
5. **Return useful errors**: Help debug issues with clear error messages
