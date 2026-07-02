# Function Calling Guide

Function calling allows Grok models to interact with external systems by generating structured outputs that invoke your local functions or APIs.

## Overview

Function calling enables you to:
- Define custom functions with JSON schemas
- Have Grok decide when and how to call functions
- Build agentic applications that interact with external systems
- Retrieve live data from APIs (weather, databases, etc.)

## How It Works

1. **Define functions** that Grok can access
2. **Provide function signatures** to Grok via the `tools` parameter
3. **Grok returns `tool_calls`** when it needs to invoke functions
4. **Execute the functions** locally with the provided arguments
5. **Return results to Grok** for final response generation

## Quick Start with Python SDK

### Setup

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.x.ai/v1",
    api_key="your-api-key"
)
```

### Define Your Function

```python
def get_weather(location: str, units: str = "celsius") -> dict:
    """Get weather for a location (mock implementation)"""
    # In production, call a real weather API
    return {
        "location": location,
        "temperature": 22,
        "units": units,
        "condition": "sunny",
        "humidity": 45
    }
```

### Define the Tool Schema

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City and state, e.g. San Francisco, CA"
                    },
                    "units": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature units"
                    }
                },
                "required": ["location"]
            }
        }
    }
]
```

### Make the Request

```python
messages = [
    {"role": "user", "content": "What's the weather like in San Francisco?"}
]

response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    tool_choice="auto"
)
```

## Complete End-to-End Example

Here's a complete example showing the full function calling loop:

```python
import json
from openai import OpenAI

# Setup client
client = OpenAI(
    base_url="https://api.x.ai/v1",
    api_key="your-api-key"
)

# Define available functions
def get_weather(location: str, units: str = "celsius") -> dict:
    """Get weather for a location"""
    # Mock implementation - replace with real API call
    weather_data = {
        "San Francisco, CA": {"temp": 18, "condition": "foggy"},
        "New York, NY": {"temp": 25, "condition": "sunny"},
        "London, UK": {"temp": 15, "condition": "rainy"},
    }
    data = weather_data.get(location, {"temp": 20, "condition": "unknown"})
    return {
        "location": location,
        "temperature": data["temp"],
        "units": units,
        "condition": data["condition"]
    }

def book_restaurant(restaurant: str, date: str, party_size: int) -> dict:
    """Book a restaurant reservation"""
    import random
    return {
        "confirmation": f"RES{random.randint(1000, 9999)}",
        "restaurant": restaurant,
        "date": date,
        "party_size": party_size,
        "status": "confirmed"
    }

# Map function names to implementations
FUNCTIONS = {
    "get_weather": get_weather,
    "book_restaurant": book_restaurant
}

# Define tool schemas
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City and state, e.g. San Francisco, CA"
                    },
                    "units": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature units"
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_restaurant",
            "description": "Book a restaurant reservation",
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant": {
                        "type": "string",
                        "description": "Name of the restaurant"
                    },
                    "date": {
                        "type": "string",
                        "description": "Date for reservation (YYYY-MM-DD)"
                    },
                    "party_size": {
                        "type": "integer",
                        "description": "Number of people"
                    }
                },
                "required": ["restaurant", "date", "party_size"]
            }
        }
    }
]

def process_tool_calls(response, messages):
    """Process tool calls and return updated messages"""
    assistant_message = response.choices[0].message

    # Add assistant's response to history
    messages.append({
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
        ] if assistant_message.tool_calls else None
    })

    # Process each tool call
    if assistant_message.tool_calls:
        for tool_call in assistant_message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)

            print(f"Calling {function_name} with {arguments}")

            # Execute the function
            if function_name in FUNCTIONS:
                result = FUNCTIONS[function_name](**arguments)
            else:
                result = {"error": f"Unknown function: {function_name}"}

            # Add tool result to messages
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result)
            })

    return messages

def chat_with_tools(user_message: str):
    """Complete chat loop with function calling"""
    messages = [{"role": "user", "content": user_message}]

    while True:
        # Make API request
        response = client.chat.completions.create(
            model="grok-4",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )

        assistant_message = response.choices[0].message

        # Check if we need to process tool calls
        if assistant_message.tool_calls:
            messages = process_tool_calls(response, messages)
            # Continue loop to get final response
        else:
            # No more tool calls - return final response
            return assistant_message.content

# Example usage
result = chat_with_tools("What's the weather in San Francisco and New York?")
print(result)
```

## Using Pydantic for Validation

Use Pydantic models to validate function parameters and generate JSON schemas automatically:

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional

class TemperatureUnits(str, Enum):
    celsius = "celsius"
    fahrenheit = "fahrenheit"

class WeatherRequest(BaseModel):
    location: str = Field(description="City and state, e.g. San Francisco, CA")
    units: Optional[TemperatureUnits] = Field(
        default=TemperatureUnits.celsius,
        description="Temperature units"
    )

class WeatherResponse(BaseModel):
    location: str
    temperature: float
    units: str
    condition: str
    humidity: int

def get_weather(location: str, units: str = "celsius") -> WeatherResponse:
    """Get weather with Pydantic validation"""
    # Validate input
    request = WeatherRequest(location=location, units=units)

    # Get weather data (mock)
    return WeatherResponse(
        location=request.location,
        temperature=22.5,
        units=request.units.value,
        condition="sunny",
        humidity=45
    )

# Generate tool schema from Pydantic model
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": WeatherRequest.model_json_schema()
        }
    }
]
```

## Response Format

When Grok decides to call a function, it returns:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"San Francisco, CA\"}"
            }
          }
        ]
      }
    }
  ]
}
```

## Sending Tool Results

After executing functions, send results back with `role: "tool"`:

```python
messages = [
    {"role": "user", "content": "What's the weather in San Francisco?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": "{\"location\": \"San Francisco, CA\"}"
                }
            }
        ]
    },
    {
        "role": "tool",
        "tool_call_id": "call_abc123",
        "content": "{\"temperature\": 18, \"condition\": \"foggy\", \"humidity\": 80}"
    }
]

# Get final response
response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools
)

print(response.choices[0].message.content)
# "The weather in San Francisco is currently 18°C and foggy with 80% humidity."
```

## Multiple Tool Calls

Grok can request multiple function calls in a single response:

```python
# User asks about weather in multiple cities
messages = [
    {"role": "user", "content": "Compare the weather in Tokyo, Paris, and Sydney"}
]

response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    tool_choice="auto"
)

# Response may contain multiple tool_calls
for tool_call in response.choices[0].message.tool_calls:
    print(f"Call: {tool_call.function.name}({tool_call.function.arguments})")
    # Call: get_weather({"location": "Tokyo, Japan"})
    # Call: get_weather({"location": "Paris, France"})
    # Call: get_weather({"location": "Sydney, Australia"})
```

Handle all calls before sending results:

```python
# Execute all function calls
tool_results = []
for tool_call in response.choices[0].message.tool_calls:
    function_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    result = FUNCTIONS[function_name](**arguments)

    tool_results.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps(result)
    })

# Add assistant message and all tool results to history
messages.append(response.choices[0].message)
messages.extend(tool_results)

# Get final response with all results
final_response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools
)
```

## Tool Choice Options

Control function calling behavior with `tool_choice`:

| Value | Behavior |
|-------|----------|
| `"auto"` | Model decides when to call functions (default) |
| `"none"` | Disable function calling for this request |
| `"required"` | Force the model to call at least one function |
| `{"type": "function", "function": {"name": "get_weather"}}` | Force a specific function |

```python
# Force a specific function
response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    tool_choice={"type": "function", "function": {"name": "get_weather"}}
)

# Disable function calling
response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    tool_choice="none"
)
```

## Streaming with Tool Calls

Function calls work with streaming. Tool call arguments are sent incrementally:

```python
stream = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    stream=True
)

tool_calls = {}

for chunk in stream:
    delta = chunk.choices[0].delta

    if delta.tool_calls:
        for tc in delta.tool_calls:
            if tc.id:
                # New tool call
                tool_calls[tc.index] = {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": ""
                }
            if tc.function.arguments:
                # Append arguments
                tool_calls[tc.index]["arguments"] += tc.function.arguments

    if delta.content:
        print(delta.content, end="", flush=True)

# Process complete tool calls
for tc in tool_calls.values():
    print(f"Tool call: {tc['name']}({tc['arguments']})")
```

## Real-World Example: Ski Trip Planner

A practical example using live weather data for trip planning:

```python
import json
import requests
from openai import OpenAI

client = OpenAI(base_url="https://api.x.ai/v1", api_key="your-key")

# Ski resort forecast URLs (NOAA API)
RESORT_URLS = {
    "aspen": "https://api.weather.gov/gridpoints/GJT/95,86/forecast",
    "breckenridge": "https://api.weather.gov/gridpoints/BOU/30,95/forecast",
    "jackson_hole": "https://api.weather.gov/gridpoints/RIW/30,108/forecast",
    "vail": "https://api.weather.gov/gridpoints/GJT/105,91/forecast"
}

def get_ski_forecast(resort: str) -> list:
    """Get weather forecast for a ski resort"""
    if resort not in RESORT_URLS:
        return {"error": f"Unknown resort: {resort}"}

    response = requests.get(RESORT_URLS[resort])
    data = response.json()

    forecasts = []
    for period in data["properties"]["periods"][:5]:  # Next 5 periods
        forecasts.append({
            "name": period["name"],
            "temperature": period["temperature"],
            "temperatureUnit": period["temperatureUnit"],
            "windSpeed": period["windSpeed"],
            "shortForecast": period["shortForecast"],
            "detailedForecast": period["detailedForecast"]
        })
    return forecasts

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_ski_forecast",
            "description": "Get weather forecast for a ski resort",
            "parameters": {
                "type": "object",
                "properties": {
                    "resort": {
                        "type": "string",
                        "enum": ["aspen", "breckenridge", "jackson_hole", "vail"],
                        "description": "Name of the ski resort"
                    }
                },
                "required": ["resort"]
            }
        }
    }
]

FUNCTIONS = {"get_ski_forecast": get_ski_forecast}

# Plan a ski trip
messages = [
    {
        "role": "user",
        "content": "I'm planning a ski trip this weekend. Compare conditions at Aspen and Vail and recommend the best choice."
    }
]

# First request - Grok will call the weather function
response = client.chat.completions.create(
    model="grok-4",
    messages=messages,
    tools=tools,
    tool_choice="auto"
)

# Process tool calls
if response.choices[0].message.tool_calls:
    messages.append(response.choices[0].message)

    for tool_call in response.choices[0].message.tool_calls:
        result = FUNCTIONS[tool_call.function.name](
            **json.loads(tool_call.function.arguments)
        )
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": json.dumps(result)
        })

    # Get final recommendation
    final = client.chat.completions.create(
        model="grok-4",
        messages=messages,
        tools=tools
    )
    print(final.choices[0].message.content)
```

## Best Practices

1. **Clear descriptions**: Write detailed function descriptions to help Grok understand when to use them
2. **Validate inputs**: Always validate function arguments before execution
3. **Handle errors gracefully**: Return informative error messages in tool responses
4. **Use enums**: Constrain parameters with enums when there are fixed options
5. **Pydantic validation**: Use Pydantic for robust input validation and schema generation
6. **Parallel execution**: When multiple tool calls are returned, execute them in parallel if possible
7. **Timeout handling**: Set timeouts for external API calls
8. **Logging**: Log all function calls for debugging and monitoring

## Error Handling

```python
def safe_execute(function_name: str, arguments: dict) -> dict:
    """Execute a function with error handling"""
    try:
        if function_name not in FUNCTIONS:
            return {"error": f"Unknown function: {function_name}"}

        result = FUNCTIONS[function_name](**arguments)
        return result

    except TypeError as e:
        return {"error": f"Invalid arguments: {str(e)}"}
    except Exception as e:
        return {"error": f"Function failed: {str(e)}"}
```

## Models Supporting Function Calling

| Model | Notes |
|-------|-------|
| grok-4 | Recommended for function calling |
| grok-4-fast | Faster, good for simpler functions |
| grok-4-1-fast-reasoning | Optimized for agentic use |
| grok-4-1-fast-non-reasoning | Fast without reasoning overhead |
| grok-3 | Legacy support |

## Using with xAI SDK

```python
from xai_sdk import Client

client = Client(api_key="your-api-key")

chat = client.chat.create(
    model="grok-4",
    tools=tools
)

chat.append(user("What's the weather in Tokyo?"))
response = chat.sample()

# Process tool calls
if response.tool_calls:
    for tc in response.tool_calls:
        result = execute_function(tc.function.name, tc.function.arguments)
        chat.append_tool_result(tc.id, result)

    final = chat.sample()
    print(final.content)
```
