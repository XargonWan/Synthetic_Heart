# xAI API Overview

The xAI API makes it easy to harness Grok's intelligence in your projects. Grok is the flagship AI model designed to deliver truthful, insightful answers.

## Base URL

```
https://api.x.ai
```

## Regional Endpoints

You can send requests to regional endpoints using the format:
```
https://<region-name>.api.x.ai
```

For example, to send a request from US East Coast to us-east-1:
```
https://us-east-1.api.x.ai
```

## Authentication

All API requests require authentication using an API key. Include your API key in the Authorization header:

```
Authorization: Bearer YOUR_API_KEY
```

## API Key Management

1. Create an API key via the API Keys Page in the xAI API Console
2. After generating an API key, save it somewhere safe
3. Recommended to export it as an environment variable or save to a .env file

```bash
export XAI_API_KEY="your-api-key-here"
```

## SDK Compatibility

The xAI APIs are fully compatible with:
- OpenAI SDK
- Anthropic SDK
- Native xAI SDK

## Key Features

- **Chat Completions**: Text generation and conversation
- **Function Calling**: Connect models to external tools and systems
- **Image Generation**: Create images from text prompts
- **Image Understanding**: Analyze and describe images
- **Video Generation**: Create videos from text prompts
- **Voice API**: Real-time voice conversations
- **Built-in Tools**: Web search, X search, code execution
- **Structured Outputs**: JSON schema-constrained responses

## Important Notes

- X Premium purchases won't affect your service status on xAI API, and vice versa, as these are separate offerings
- The knowledge cut-off date of Grok 3 and Grok 4 is November 2024
- Grok has no knowledge of current events beyond its training data. To incorporate realtime data, enable server-side search tools (Web Search / X Search)
