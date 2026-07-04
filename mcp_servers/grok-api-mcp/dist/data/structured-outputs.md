# Structured Outputs

Structured Outputs enable you to enforce a specific JSON schema on the model's response, ensuring predictable and reliable data formats.

## Overview

Ideal for tasks like:
- Document parsing
- Entity extraction
- Report generation
- API response formatting

When using structured outputs, the LLM's response is **guaranteed** to match your input schema.

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
        {"role": "user", "content": "Extract the person's name and age from: John is 25 years old."}
    ],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "person_info",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"}
                },
                "required": ["name", "age"],
                "additionalProperties": False
            }
        }
    }
)

import json
data = json.loads(response.choices[0].message.content)
print(data)  # {"name": "John", "age": 25}
```

## Using Pydantic

```python
from pydantic import BaseModel
from typing import List

class City(BaseModel):
    name: str
    country: str
    population: int

class CityList(BaseModel):
    cities: List[City]

response = client.beta.chat.completions.parse(
    model="grok-4",
    messages=[
        {"role": "user", "content": "List 3 major cities with their populations"}
    ],
    response_format=CityList
)

cities = response.choices[0].message.parsed
for city in cities.cities:
    print(f"{city.name}, {city.country}: {city.population}")
```

## Using Zod (TypeScript)

```typescript
import { z } from 'zod';
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.XAI_API_KEY,
  baseURL: 'https://api.x.ai/v1'
});

const PersonSchema = z.object({
  name: z.string(),
  age: z.number(),
  email: z.string().email().optional()
});

const response = await client.chat.completions.create({
  model: 'grok-4',
  messages: [
    { role: 'user', content: 'Extract info: Jane Doe, 30, jane@example.com' }
  ],
  response_format: {
    type: 'json_schema',
    json_schema: {
      name: 'person',
      strict: true,
      schema: zodToJsonSchema(PersonSchema)
    }
  }
});
```

## JSON Schema Format

```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "schema_name",
    "strict": true,
    "schema": {
      "type": "object",
      "properties": {
        "field1": {"type": "string"},
        "field2": {"type": "number"},
        "field3": {
          "type": "array",
          "items": {"type": "string"}
        }
      },
      "required": ["field1", "field2"],
      "additionalProperties": false
    }
  }
}
```

## Supported Schema Types

- `string`
- `number` / `integer`
- `boolean`
- `array`
- `object`
- `null`
- `enum`

## Complex Schemas

### Nested Objects

```json
{
  "type": "object",
  "properties": {
    "user": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "address": {
          "type": "object",
          "properties": {
            "street": {"type": "string"},
            "city": {"type": "string"}
          }
        }
      }
    }
  }
}
```

### Arrays with Constraints

```json
{
  "type": "array",
  "items": {"type": "string"},
  "minItems": 1,
  "maxItems": 10
}
```

### Enums

```json
{
  "type": "string",
  "enum": ["low", "medium", "high"]
}
```

## Streaming with Structured Outputs

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "List 5 programming languages"}],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "languages",
            "schema": {
                "type": "object",
                "properties": {
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                }
            }
        }
    },
    stream=True
)

full_response = ""
for chunk in stream:
    if chunk.choices[0].delta.content:
        full_response += chunk.choices[0].delta.content

data = json.loads(full_response)
```

## Best Practices

1. **Use `strict: true`**: Ensures exact schema compliance
2. **Set `additionalProperties: false`**: Prevents unexpected fields
3. **Define required fields**: Explicitly list required properties
4. **Keep schemas simple**: Complex nested schemas may impact performance
5. **Validate on your end**: Even with guarantees, validate for edge cases
