# Search Tools (Live Search)

Grok provides powerful search tools for accessing real-time information from the web and X (Twitter).

## Web Search Tools

### web_search

Search the web for current information.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What are the latest developments in quantum computing?"}
    ],
    tools=[{"type": "web_search"}]
)
```

### web_search_with_snippets

Search the web and return relevant text snippets from results.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find articles about climate change solutions"}
    ],
    tools=[{"type": "web_search_with_snippets"}]
)
```

### browse_page

Fetch and read content from a specific URL.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Summarize the content at https://example.com/article"}
    ],
    tools=[{"type": "browse_page"}]
)
```

## X (Twitter) Search Tools

### x_user_search

Search for X users by name, handle, or description.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find AI researchers on X"}
    ],
    tools=[{"type": "x_user_search"}]
)
```

### x_keyword_search

Search X posts by keywords.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find recent tweets about the stock market"}
    ],
    tools=[{"type": "x_keyword_search"}]
)
```

### x_semantic_search

Search X posts using semantic/meaning-based matching.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find discussions about the future of work"}
    ],
    tools=[{"type": "x_semantic_search"}]
)
```

### x_thread_fetch

Fetch a complete X thread given a post ID.

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Get the full thread from this tweet: [URL]"}
    ],
    tools=[{"type": "x_thread_fetch"}]
)
```

## Citations

### Inline Citations

Markdown-style links embedded directly into the response text at points where the model references sources.

Must be explicitly enabled:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What happened in tech news today?"}
    ],
    tools=[{"type": "web_search"}],
    tool_options={
        "inline_citations": True
    }
)
```

### Citation Metadata

Structured metadata is available for each citation, including:
- Source URL
- Title
- Snippet
- Publication date

## Grounding

Search tools provide **grounding** - the model can:
- Cite sources
- Provide up-to-date information beyond training data
- Verify claims with real-time data

## Combining Search Tools

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What's trending on X about AI and find related news articles"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "x_keyword_search"},
        {"type": "x_semantic_search"}
    ]
)
```

## Use Cases

### News Aggregation

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Give me a summary of today's top tech news with sources"}
    ],
    tools=[
        {"type": "web_search_with_snippets"}
    ],
    tool_options={"inline_citations": True}
)
```

### Social Media Monitoring

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What are people saying about [brand] on X?"}
    ],
    tools=[
        {"type": "x_semantic_search"},
        {"type": "x_keyword_search"}
    ]
)
```

### Research

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Find recent research on renewable energy storage"}
    ],
    tools=[
        {"type": "web_search"},
        {"type": "browse_page"}
    ]
)
```

## Best Practices

1. **Enable citations**: Use inline citations for verifiable responses
2. **Choose appropriate tools**: Web search for general info, X search for social trends
3. **Combine tools strategically**: Multiple tools can provide comprehensive results
4. **Review sources**: Validate information from search results
5. **Privacy awareness**: X search tools may return public user data

## Limitations

- Search results depend on query quality
- Rate limits apply to search tools
- Some content may be inaccessible (paywalled, private)
- Results are real-time and may change
