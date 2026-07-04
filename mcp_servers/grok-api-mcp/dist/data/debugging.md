# Debugging Errors

Guide to troubleshooting common issues with the xAI API.

## Status Page

If there is an ongoing service disruption:
- **Status Page**: https://status.x.ai
- **RSS Feed**: https://status.x.ai/feed.xml

Check the status page first when experiencing unexpected errors.

## Common Error Codes

### 400 - Bad Request

Invalid request format or parameters.

```json
{
  "error": {
    "message": "Invalid parameter: temperature must be between 0 and 2",
    "type": "invalid_request_error",
    "code": 400
  }
}
```

**Solutions**:
- Check request body format
- Validate parameter values
- Ensure required fields are present

### 401 - Unauthorized

Invalid or missing API key.

```json
{
  "error": {
    "message": "Invalid API key provided",
    "type": "authentication_error",
    "code": 401
  }
}
```

**Solutions**:
- Verify API key is correct
- Check Authorization header format: `Bearer YOUR_API_KEY`
- Ensure key hasn't been revoked

### 403 - Forbidden

Access denied to the requested resource.

```json
{
  "error": {
    "message": "You don't have access to this resource",
    "type": "permission_error",
    "code": 403
  }
}
```

**Solutions**:
- Check API key permissions
- Verify you have access to the model/feature
- Contact support if access should be granted

### 404 - Not Found

Resource doesn't exist.

```json
{
  "error": {
    "message": "Model 'invalid-model' not found",
    "type": "not_found_error",
    "code": 404
  }
}
```

**Solutions**:
- Check model name spelling
- Verify endpoint path is correct
- Use `GET /v1/models` to list available models

### 429 - Rate Limited

Too many requests.

```json
{
  "error": {
    "message": "Rate limit exceeded. Please retry after 60 seconds.",
    "type": "rate_limit_error",
    "code": 429
  }
}
```

**Solutions**:
- Implement exponential backoff
- Reduce request frequency
- Request higher rate limits from support@x.ai

### 500 - Internal Server Error

Server-side error.

```json
{
  "error": {
    "message": "Internal server error",
    "type": "server_error",
    "code": 500
  }
}
```

**Solutions**:
- Retry the request
- Check status.x.ai for outages
- Contact support if persistent

### 503 - Service Unavailable

Service temporarily unavailable.

**Solutions**:
- Wait and retry
- Check status page
- Use regional endpoints for redundancy

## Debugging Tips

### 1. Enable Verbose Logging

```python
import logging
import httpx

logging.basicConfig(level=logging.DEBUG)

# For OpenAI SDK
from openai import OpenAI
client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
    http_client=httpx.Client(
        event_hooks={"request": [lambda r: print(f"Request: {r.url}")]}
    )
)
```

### 2. Check Request/Response

```python
def debug_request(client, **kwargs):
    import json

    print("Request:")
    print(json.dumps(kwargs, indent=2, default=str))

    try:
        response = client.chat.completions.create(**kwargs)
        print("\nResponse:")
        print(json.dumps(response.model_dump(), indent=2))
        return response
    except Exception as e:
        print(f"\nError: {e}")
        raise
```

### 3. Validate JSON

```python
import json

def validate_request_body(body):
    try:
        json.loads(json.dumps(body))
        print("Valid JSON")
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
```

### 4. Test with cURL

```bash
curl -v https://api.x.ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-4",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Model-Specific Errors

### Reasoning Models

```json
{
  "error": {
    "message": "Parameter 'reasoning_effort' is not supported for model grok-4",
    "type": "invalid_request_error"
  }
}
```

Grok 4 doesn't support `reasoning_effort`. Remove this parameter.

### Unsupported Parameters

```json
{
  "error": {
    "message": "Parameters 'presencePenalty', 'frequencyPenalty' are not supported by reasoning models",
    "type": "invalid_request_error"
  }
}
```

Remove unsupported parameters for reasoning models.

## Network Issues

### Timeout Errors

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
    timeout=60.0  # Increase timeout
)
```

### Connection Errors

- Check internet connectivity
- Verify firewall settings
- Try regional endpoints
- Check DNS resolution

## Best Practices

1. **Always check status page first** when debugging
2. **Log request IDs** from responses for support tickets
3. **Implement proper error handling** with retries
4. **Use structured logging** for production
5. **Monitor error rates** to detect issues early

## Getting Help

If issues persist:
1. Check documentation at docs.x.ai
2. Review status page
3. Contact support@x.ai with:
   - Request ID
   - Error message
   - Code snippet
   - Timestamp
