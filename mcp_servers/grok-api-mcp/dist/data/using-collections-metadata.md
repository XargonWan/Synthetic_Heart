# Metadata Fields

Metadata fields allow you to attach structured attributes to documents in a collection. These fields enable:

- **Filtered retrieval** — Narrow search results to documents matching specific criteria (e.g., `author="Sandra Kim"`)
- **Contextual embeddings** — Inject metadata into chunks to improve retrieval accuracy (e.g., prepending document title to each chunk)
- **Data integrity constraints** — Enforce required fields or uniqueness across documents

## Creating a Collection with Metadata Fields

Define metadata fields using `field_definitions` when creating a collection:

### Using REST API

```bash
curl -X POST "https://management-api.x.ai/v1/collections" \
  -H "Authorization: Bearer $XAI_MANAGEMENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "collection_name": "research_papers",
    "field_definitions": [
      { "key": "author", "required": true },
      { "key": "year", "required": true, "unique": true },
      { "key": "title", "inject_into_chunk": true }
    ]
  }'
```

### Using xAI SDK

```python
import os
from xai_sdk import Client

client = Client(
    api_key=os.getenv("XAI_API_KEY"),
    management_api_key=os.getenv("XAI_MANAGEMENT_API_KEY"),
    timeout=3600,
)

collection = client.collections.create(
    name="research_papers",
    field_definitions=[
        {"key": "author", "required": True},
        {"key": "year", "required": True, "unique": True},
        {"key": "title", "inject_into_chunk": True}
    ]
)
```

## Field Definition Options

| Option | Description | Default |
|--------|-------------|---------|
| `key` | Field identifier (required) | - |
| `required` | Document uploads must include this field | `false` |
| `unique` | Only one document in the collection can have a given value for this field | `false` |
| `inject_into_chunk` | Prepends this field's value to every embedding chunk, improving retrieval by providing context | `false` |
| `description` | Human-readable description of the field (optional) | - |

## Uploading Documents with Metadata

Include metadata as a JSON object in the `fields` parameter:

### Using REST API

```bash
curl -X POST "https://management-api.x.ai/v1/collections/{collection_id}/documents" \
  -H "Authorization: Bearer $XAI_MANAGEMENT_API_KEY" \
  -F "name=paper.pdf" \
  -F "data=@paper.pdf" \
  -F "content_type=application/pdf" \
  -F 'fields={"author": "Sandra Kim", "year": "2024", "title": "Q3 Revenue Analysis"}'
```

### Using xAI SDK

```python
with open("paper.pdf", "rb") as file:
    file_data = file.read()

document = client.collections.upload_document(
    collection_id="collection_xxx",
    name="paper.pdf",
    data=file_data,
    fields={
        "author": "Sandra Kim",
        "year": "2024",
        "title": "Q3 Revenue Analysis"
    },
)
```

## Filtering Documents in Search

Use the `filter` parameter to restrict search results based on metadata values. The filter uses AIP-160 syntax:

### Using REST API

```bash
curl -X POST "https://api.x.ai/v1/documents/search" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "revenue growth",
    "source": { "collection_ids": ["collection_xxx"] },
    "filter": "author=\"Sandra Kim\" AND year>=2020"
  }'
```

### Using xAI SDK

```python
response = client.collections.search(
    query="revenue growth",
    collection_ids=["collection_xxx"],
    filter='author="Sandra Kim" AND year>=2020'
)
```

## Supported Filter Operators

| Operator | Example | Description |
|----------|---------|-------------|
| `=` | `author="Jane"` | Equals |
| `!=` | `status!="draft"` | Not equals |
| `<`, `>`, `<=`, `>=` | `year>=2020` | Numeric/lexical comparison |
| `AND` | `a="x" AND b="y"` | Both conditions must match |
| `OR` | `a="x" OR a="y"` | Either condition matches |

> **Important**: `OR` has higher precedence than `AND`. Use parentheses for clarity: `a="x" AND (b="y" OR b="z")`.

> **Note**: Wildcard matching (e.g., `author="E*"`) is not supported. All string comparisons are exact matches.

> **Note**: Filtering on fields that don't exist in your documents returns no results. Double-check that field names match your collection's `field_definitions`.

## AIP-160 Filter String Examples

### Basic Examples

```bash
# Equality (double or single quotes for strings with spaces)
author="Sandra Kim"
author='Sandra Kim'

# Equality (no quotes needed for simple values)
year=2024
status=active

# Not equal
status!="archived"
status!='archived'
```

### Comparison Operators

```bash
# Numeric comparisons
year>=2020
year>2019
score<100
price<=50.00

# Combined comparisons (range)
year>=2020 AND year<=2024
```

### Logical Operators

```bash
# AND - both conditions must match
author="Sandra Kim" AND year=2024

# OR - either condition matches
status="pending" OR status="in_progress"

# Combined (OR has higher precedence than AND)
department="Engineering" AND status="active" OR status="pending"

# Use parentheses for clarity
department="Engineering" AND (status="active" OR status="pending")
```

### Complex Examples

```bash
# Multiple conditions
author="Sandra Kim" AND year>=2020 AND status!="draft"

# Nested logic with parentheses
(author="Sandra Kim" OR author="John Doe") AND year>=2020

# Multiple fields with mixed operators
category="finance" AND (year=2023 OR year=2024) AND status!="archived"
```

## Quick Reference

| Use Case | Filter String |
|----------|---------------|
| Exact match | `author="Sandra Kim"` |
| Numeric comparison | `year>=2020` |
| Not equal | `status!="archived"` |
| Multiple conditions | `author="Sandra Kim" AND year=2024` |
| Either condition | `status="pending" OR status="draft"` |
| Grouped logic | `(status="active" OR status="pending") AND year>=2020` |
| Complex filter | `category="finance" AND year>=2020 AND status!="archived"` |

## Best Practices

1. **Plan your schema** - Define fields before uploading documents
2. **Use inject_into_chunk** - Improves retrieval for categorical fields like document type or category
3. **Enforce data integrity** - Use `required` and `unique` where appropriate
4. **Keep filters simple** - Complex filters may impact performance
5. **Use consistent values** - String comparisons are exact matches (case-sensitive)
6. **Use parentheses** - Clarify precedence when mixing AND and OR operators

## Limitations

- Wildcard matching is not supported - all string comparisons are exact matches
- Filter syntax follows AIP-160 specification
- Metadata changes require re-uploading documents
- Field names must match exactly (case-sensitive)
- Filtering on non-existent fields returns no results
