# Management API

The Management API provides administrative control over your xAI resources.

## Overview

The Management API is separate from the inference API and requires a different authentication key.

### Key Differences

| Aspect | Inference API | Management API |
|--------|---------------|----------------|
| Base URL | https://api.x.ai | https://management-api.x.ai |
| Key Type | API Key | Management Key |
| Purpose | Model inference | Resource management |

## Getting a Management Key

1. Go to xAI Console
2. Navigate to Settings → Management Keys
3. Generate a new management key
4. Save it securely

## Base URL

```
https://management-api.x.ai
```

## Authentication

```bash
curl https://management-api.x.ai/v1/... \
  -H "Authorization: Bearer YOUR_MANAGEMENT_KEY"
```

## Available Operations

### API Keys Management

#### List API Keys

```bash
curl https://management-api.x.ai/v1/api-keys \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

#### Create API Key

```bash
curl -X POST https://management-api.x.ai/v1/api-keys \
  -H "Authorization: Bearer $MANAGEMENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "production-key",
    "permissions": ["inference"]
  }'
```

#### Delete API Key

```bash
curl -X DELETE https://management-api.x.ai/v1/api-keys/{key_id} \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

### Collections Management

#### List Collections

```bash
curl https://management-api.x.ai/v1/collections \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

#### Create Collection

```bash
curl -X POST https://management-api.x.ai/v1/collections \
  -H "Authorization: Bearer $MANAGEMENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-collection",
    "description": "Collection description"
  }'
```

#### Delete Collection

```bash
curl -X DELETE https://management-api.x.ai/v1/collections/{collection_id} \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

### Documents Management

#### Upload Document

```bash
curl -X POST https://management-api.x.ai/v1/collections/{collection_id}/documents \
  -H "Authorization: Bearer $MANAGEMENT_KEY" \
  -F "file=@document.pdf"
```

#### List Documents

```bash
curl https://management-api.x.ai/v1/collections/{collection_id}/documents \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

#### Delete Document

```bash
curl -X DELETE https://management-api.x.ai/v1/collections/{collection_id}/documents/{document_id} \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

### Usage & Billing

#### Get Usage Statistics

```bash
curl https://management-api.x.ai/v1/usage \
  -H "Authorization: Bearer $MANAGEMENT_KEY" \
  -G -d "start_date=2024-01-01" -d "end_date=2024-01-31"
```

#### Get Billing Information

```bash
curl https://management-api.x.ai/v1/billing \
  -H "Authorization: Bearer $MANAGEMENT_KEY"
```

## Python Examples

```python
import requests

class XAIManagementClient:
    def __init__(self, management_key):
        self.base_url = "https://management-api.x.ai/v1"
        self.headers = {
            "Authorization": f"Bearer {management_key}",
            "Content-Type": "application/json"
        }

    def list_api_keys(self):
        response = requests.get(
            f"{self.base_url}/api-keys",
            headers=self.headers
        )
        return response.json()

    def create_collection(self, name, description=""):
        response = requests.post(
            f"{self.base_url}/collections",
            headers=self.headers,
            json={"name": name, "description": description}
        )
        return response.json()

    def upload_document(self, collection_id, file_path):
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{self.base_url}/collections/{collection_id}/documents",
                headers={"Authorization": self.headers["Authorization"]},
                files={"file": f}
            )
        return response.json()

    def get_usage(self, start_date, end_date):
        response = requests.get(
            f"{self.base_url}/usage",
            headers=self.headers,
            params={"start_date": start_date, "end_date": end_date}
        )
        return response.json()

# Usage
client = XAIManagementClient(os.environ["XAI_MANAGEMENT_KEY"])
keys = client.list_api_keys()
```

## Security Best Practices

1. **Keep management keys secure**: Never expose in client-side code
2. **Rotate keys regularly**: Update management keys periodically
3. **Limit permissions**: Create API keys with minimal required permissions
4. **Audit access**: Monitor management API usage
5. **Use separate keys**: Don't use management keys for inference
