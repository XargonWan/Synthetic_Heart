# Use with Code Editors

You can use Grok with coding assistant plugins to help you code. Grok's Code models are specifically optimized for coding tasks.

## Recommended Model

Use `grok-code-fast-1` for the best coding experience - it's specifically optimized for this task.

## VS Code with Cline

### Setup

1. Download Cline from the VSCode marketplace
2. Install the extension
3. Click on "Use your own API key"
4. Enter your xAI API key
5. Set the base URL to `https://api.x.ai/v1`
6. Select `grok-code-fast-1` as the model

### Configuration

In Cline settings:
- **API Key**: Your xAI API key
- **Base URL**: `https://api.x.ai/v1`
- **Model**: `grok-code-fast-1`

## Cursor

### Setup

1. Open Cursor
2. Go to **Cursor Settings → Models**
3. Enter your xAI API key
4. Set **Override OpenAI Base URL** to `https://api.x.ai/v1`
5. Add `grok-code-fast-1` as a model

### Configuration

```
API Key: your-xai-api-key
Base URL: https://api.x.ai/v1
Model: grok-code-fast-1
```

## Continue.dev

### Setup

1. Install Continue extension in VS Code
2. Open Continue configuration (`~/.continue/config.json`)
3. Add xAI configuration:

```json
{
  "models": [
    {
      "title": "Grok Code",
      "provider": "openai",
      "model": "grok-code-fast-1",
      "apiKey": "your-xai-api-key",
      "apiBase": "https://api.x.ai/v1"
    }
  ]
}
```

## Zed

### Setup

1. Open Zed settings
2. Navigate to AI/Assistant settings
3. Configure OpenAI-compatible provider:

```json
{
  "assistant": {
    "provider": {
      "type": "openai",
      "api_url": "https://api.x.ai/v1",
      "model": "grok-code-fast-1"
    }
  }
}
```

## JetBrains IDEs

### With AI Assistant Plugin

Some JetBrains AI plugins support custom OpenAI-compatible endpoints:

1. Install an OpenAI-compatible AI plugin
2. Configure the endpoint:
   - URL: `https://api.x.ai/v1`
   - Model: `grok-code-fast-1`
   - API Key: Your xAI key

## Neovim

### With ChatGPT.nvim or similar

```lua
require("chatgpt").setup({
  api_key_cmd = "echo $XAI_API_KEY",
  openai_params = {
    model = "grok-code-fast-1",
  },
  openai_edit_params = {
    model = "grok-code-fast-1",
  },
  api_host_cmd = "echo api.x.ai",
})
```

## General OpenAI-Compatible Setup

For any editor that supports OpenAI-compatible APIs:

| Setting | Value |
|---------|-------|
| API Base URL | `https://api.x.ai/v1` |
| API Key | Your xAI API key |
| Model | `grok-code-fast-1` |

## Tips for Best Results

1. **Use grok-code-fast-1**: Optimized for coding tasks with 256K context
2. **Provide context**: Include relevant files in the context
3. **Be specific**: Clear instructions yield better code
4. **Iterate**: Ask follow-up questions to refine code

## Environment Variables

Set your API key as an environment variable:

```bash
export XAI_API_KEY="your-api-key-here"
```

Many editors will automatically pick up this variable.

## Troubleshooting

### "Model not found" error
- Verify the model name is exactly `grok-code-fast-1`
- Check the base URL ends with `/v1`

### Authentication errors
- Verify your API key is correct
- Check there are no extra spaces in the key

### Slow responses
- Try a regional endpoint (e.g., `https://us-east-1.api.x.ai/v1`)
- Reduce context size if possible
