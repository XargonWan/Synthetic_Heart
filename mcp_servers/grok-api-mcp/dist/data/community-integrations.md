# Community Integrations

Grok is accessible via community integrations, enabling you to connect Grok to other parts of your system easily.

## LiteLLM

LiteLLM provides a simple SDK or proxy server for calling different LLM providers.

### Installation

```bash
pip install litellm
```

### Usage

```python
from litellm import completion

response = completion(
    model="xai/grok-4",
    messages=[{"role": "user", "content": "Hello!"}],
    api_key="your-xai-api-key"
)

print(response.choices[0].message.content)
```

### Proxy Server

```bash
litellm --model xai/grok-4 --api_key your-xai-api-key
```

### Configuration

```yaml
# litellm_config.yaml
model_list:
  - model_name: grok-4
    litellm_params:
      model: xai/grok-4
      api_key: your-xai-api-key
```

## LangChain

LangChain provides integrations for building LLM applications.

### Python

Documentation: [python.langchain.com](https://python.langchain.com)

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="grok-4",
    openai_api_key="your-xai-api-key",
    openai_api_base="https://api.x.ai/v1"
)

response = llm.invoke("Hello!")
print(response.content)
```

### JavaScript/TypeScript

Documentation: [js.langchain.com](https://js.langchain.com)

```typescript
import { ChatOpenAI } from "@langchain/openai";

const model = new ChatOpenAI({
  modelName: "grok-4",
  openAIApiKey: "your-xai-api-key",
  configuration: {
    baseURL: "https://api.x.ai/v1"
  }
});

const response = await model.invoke("Hello!");
console.log(response.content);
```

## Vercel AI SDK

Vercel's AI SDK supports xAI Grok Provider.

### Installation

```bash
npm install @ai-sdk/xai ai
```

### Usage

```typescript
import { xai } from '@ai-sdk/xai';
import { generateText } from 'ai';

const { text } = await generateText({
  model: xai('grok-4'),
  prompt: 'Hello!',
});

console.log(text);
```

### Environment Variable

```bash
export XAI_API_KEY="your-api-key"
```

### Note on Tool Calling

Advanced tool usage patterns are not yet supported in the Vercel AI SDK. Use the xAI SDK or OpenAI SDK for tool calling functionality.

## Native xAI SDK

For full feature support including all products (Collections, Voice API, API key management), use the native xAI SDK.

### Installation

```bash
pip install xai-sdk
```

### Usage

```python
from xai_sdk import Client

client = Client(api_key="your-xai-api-key")

response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)
```

### Version Requirements

| Feature | Minimum Version |
|---------|-----------------|
| Basic usage | 1.0.0 |
| Agentic tool calling | 1.3.1 |
| `get_tool_call_type` | 1.4.0 |
| Files API | 1.4.0 |
| Inline citations | 1.5.0 |
| Location-based search | 1.6.0 |

## OpenAI SDK

The xAI API is fully compatible with the OpenAI SDK.

### Python

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-xai-api-key",
    base_url="https://api.x.ai/v1"
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### Node.js

```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: 'your-xai-api-key',
  baseURL: 'https://api.x.ai/v1'
});

const response = await client.chat.completions.create({
  model: 'grok-4',
  messages: [{ role: 'user', content: 'Hello!' }]
});
```

## Anthropic SDK

The xAI API is also compatible with the Anthropic SDK for applicable features.

## Choosing an Integration

| Integration | Best For |
|-------------|----------|
| Native xAI SDK | Full feature access, all xAI products |
| OpenAI SDK | Familiar API, easy migration |
| LiteLLM | Multi-provider support, proxy server |
| LangChain | Building LLM applications, chains |
| Vercel AI SDK | Next.js/React applications |
