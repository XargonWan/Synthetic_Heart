# Built-in Tools

Grok provides several built-in tools that can be enabled without defining custom function schemas.

## Web Search Tools

### web_search
Search the web for current information.

```json
{
  "tools": [{"type": "web_search"}]
}
```

### web_search_with_snippets
Search the web and return relevant text snippets from results.

```json
{
  "tools": [{"type": "web_search_with_snippets"}]
}
```

### browse_page
Fetch and read content from a specific URL.

```json
{
  "tools": [{"type": "browse_page"}]
}
```

## X (Twitter) Tools

### x_user_search
Search for X users by name, handle, or description.

### x_keyword_search
Search X posts by keywords.

### x_semantic_search
Search X posts using semantic/meaning-based matching.

### x_thread_fetch
Fetch a complete X thread given a post ID.

## Code Execution

### code_execution
Execute Python code in a sandboxed environment.

```json
{
  "tools": [{"type": "code_execution"}]
}
```

Capabilities:
- Run Python code safely
- Access to common libraries (numpy, pandas, matplotlib, etc.)
- Generate visualizations
- Process data

## Document Tools

### collections_search
Search through document collections uploaded to your account.

### attachment_search
Search through file attachments in the conversation.

## Using Built-in Tools

Enable built-in tools by adding them to the `tools` array:

```json
{
  "model": "grok-4",
  "messages": [
    {"role": "user", "content": "Search for the latest news about AI"}
  ],
  "tools": [
    {"type": "web_search"},
    {"type": "code_execution"}
  ]
}
```

## Combining with Custom Functions

You can use built-in tools alongside custom functions:

```json
{
  "tools": [
    {"type": "web_search"},
    {
      "type": "function",
      "function": {
        "name": "save_to_database",
        "description": "Save search results to database",
        "parameters": {...}
      }
    }
  ]
}
```

## Grounding with Web Search

Web search tools provide grounding - the model can cite sources and provide up-to-date information beyond its training data.

## Best Practices

1. **Enable only needed tools**: Each tool adds complexity; enable only what you need
2. **Handle rate limits**: Built-in tools may have usage limits
3. **Review outputs**: Web search results should be validated for accuracy
4. **Privacy considerations**: X search tools may return public user data
