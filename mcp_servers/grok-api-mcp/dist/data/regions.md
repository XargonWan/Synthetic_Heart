# Regional Endpoints

You can send requests to regional endpoints for improved latency and compliance requirements.

## Endpoint Format

```
https://<region-name>.api.x.ai
```

## Available Regions

| Region | Endpoint |
|--------|----------|
| US East 1 | https://us-east-1.api.x.ai |
| US West 1 | https://us-west-1.api.x.ai |
| EU West 1 | https://eu-west-1.api.x.ai |

## Usage

### cURL

```bash
curl https://us-east-1.api.x.ai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d '{
    "model": "grok-4",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Python

```python
from openai import OpenAI

# Use regional endpoint
client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://us-east-1.api.x.ai/v1"  # Regional endpoint
)

response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello"}]
)
```

### JavaScript/TypeScript

```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: process.env.XAI_API_KEY,
  baseURL: 'https://eu-west-1.api.x.ai/v1'  // European endpoint
});
```

## Choosing a Region

### Latency Optimization

Choose the region closest to your servers:
- **US East Coast** → `us-east-1`
- **US West Coast** → `us-west-1`
- **Europe** → `eu-west-1`

### Data Residency

For compliance requirements, use regional endpoints to ensure data stays within specific geographic areas.

## Failover Configuration

Implement failover between regions:

```python
ENDPOINTS = [
    "https://us-east-1.api.x.ai/v1",
    "https://us-west-1.api.x.ai/v1",
    "https://api.x.ai/v1"  # Global fallback
]

def create_client_with_failover():
    for endpoint in ENDPOINTS:
        try:
            client = OpenAI(
                api_key=os.environ.get("XAI_API_KEY"),
                base_url=endpoint,
                timeout=10.0
            )
            # Test connection
            client.models.list()
            return client
        except Exception:
            continue
    raise Exception("All endpoints failed")
```

## Best Practices

1. **Measure latency**: Test different regions to find the fastest for your location
2. **Implement failover**: Have backup regions configured
3. **Consider compliance**: Use appropriate regions for data residency requirements
4. **Monitor performance**: Track response times by region
5. **Use global endpoint as fallback**: `https://api.x.ai` routes automatically

## Notes

- All regions have the same API capabilities
- Pricing is the same across regions
- Models are available in all regions
- Rate limits apply per-account, not per-region
