# Collections Search Tool

Use Grok with collections search to analyze and synthesize information across documents in an agentic manner.

## Overview

The collections search tool enables:
- Searching across document collections
- Analyzing and synthesizing information
- Code execution for calculations on document data
- Receiving cited responses with tool usage information

## Basic Usage

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What are our company's vacation policies?"}
    ],
    tools=[
        {
            "type": "collections_search",
            "collections_search": {
                "collection_ids": ["col_abc123"]
            }
        }
    ]
)

print(response.choices[0].message.content)
```

## Configuration Options

### Single Collection

```python
tools=[
    {
        "type": "collections_search",
        "collections_search": {
            "collection_ids": ["col_abc123"]
        }
    }
]
```

### Multiple Collections

```python
tools=[
    {
        "type": "collections_search",
        "collections_search": {
            "collection_ids": ["col_hr_policies", "col_employee_handbook", "col_benefits"]
        }
    }
]
```

## Combining with Code Execution

Analyze document data with calculations:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Calculate the average salary from the compensation data in our HR documents"}
    ],
    tools=[
        {
            "type": "collections_search",
            "collections_search": {
                "collection_ids": ["col_hr_data"]
            }
        },
        {"type": "code_execution"}
    ]
)
```

## Citations

The collections search tool provides citations for information retrieved from documents.

### Response with Citations

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "According to the employee handbook [1], vacation policy allows 15 days per year [2].",
        "citations": [
          {
            "index": 1,
            "document_id": "doc_abc123",
            "document_name": "employee_handbook.pdf",
            "page": 42,
            "snippet": "All full-time employees are entitled to..."
          },
          {
            "index": 2,
            "document_id": "doc_abc123",
            "document_name": "employee_handbook.pdf",
            "page": 43,
            "snippet": "The standard vacation allowance is 15 days..."
          }
        ]
      }
    }
  ]
}
```

## Agentic Workflow

The collections search operates as an agentic tool:

1. **Query Analysis**: Grok analyzes the user's question
2. **Search Execution**: Searches relevant collections
3. **Result Processing**: Synthesizes information from multiple documents
4. **Response Generation**: Creates a comprehensive answer with citations

## Use Cases

### HR & Policy Questions

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "What is the process for requesting parental leave?"}
    ],
    tools=[{
        "type": "collections_search",
        "collections_search": {"collection_ids": ["col_hr_policies"]}
    }]
)
```

### Technical Documentation

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "How do I configure the authentication module?"}
    ],
    tools=[{
        "type": "collections_search",
        "collections_search": {"collection_ids": ["col_technical_docs"]}
    }]
)
```

### Research & Analysis

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "Summarize the key findings from our Q3 market research"}
    ],
    tools=[
        {
            "type": "collections_search",
            "collections_search": {"collection_ids": ["col_market_research"]}
        },
        {"type": "code_execution"}  # For data analysis
    ]
)
```

## Best Practices

1. **Organize collections by topic**: Easier for the model to find relevant information
2. **Use descriptive collection names**: Help identify the right collection
3. **Keep documents updated**: Ensure collections have current information
4. **Enable code execution**: For analytical tasks requiring calculations
5. **Request citations**: Verify information sources

## Limitations

- Collections must be pre-created via Management API
- Document size and count limits apply
- Processing time increases with collection size
- Some file formats may have extraction limitations
