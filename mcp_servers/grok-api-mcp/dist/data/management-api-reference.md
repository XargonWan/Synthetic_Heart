# Management API Reference

The Management API allows you to perform operations on your team programmatically.

## Overview

The Management API serves as a dedicated interface to the xAI platform, empowering developers and teams to programmatically manage their xAI API teams.

### Capabilities

- Provision API keys
- Handle access controls
- Perform team-level operations (create, list, update, delete keys)
- Manage access control lists (ACLs)
- Monitor billing and prepaid credit balances
- Track usage deductions
- View audit logs

## Base URL

```
https://management-api.x.ai
```

## Authentication

You need a **Management Key** (different from API keys) to use this API.

### Getting a Management Key

1. Go to xAI Console
2. Navigate to Users page
3. Ensure your account has **Management Keys Read + Write** permission
4. Obtain your Management API key from the Settings page

### Using the Key

```bash
curl https://management-api.x.ai/v1/... \
  -H "Authorization: Bearer YOUR_MANAGEMENT_KEY"
```

## API Key Management

### List API Keys

```http
GET /v1/api-keys
```

**Response**:
```json
{
  "data": [
    {
      "id": "key_abc123",
      "name": "production-key",
      "created_at": "2024-01-15T10:00:00Z",
      "last_used_at": "2024-01-20T15:30:00Z",
      "permissions": ["inference"]
    }
  ]
}
```

### Create API Key

```http
POST /v1/api-keys
```

**Request Body**:
```json
{
  "name": "new-api-key",
  "permissions": ["inference"],
  "acl": {
    "models": ["grok-4", "grok-4-fast"]
  }
}
```

### Get API Key

```http
GET /v1/api-keys/{key_id}
```

### Update API Key

```http
PATCH /v1/api-keys/{key_id}
```

### Delete API Key

```http
DELETE /v1/api-keys/{key_id}
```

## Access Control Lists (ACLs)

Control what each API key can access:

```json
{
  "acl": {
    "models": ["grok-4"],
    "features": ["chat", "image-generation"],
    "rate_limit": {
      "rpm": 100,
      "tpm": 100000
    }
  }
}
```

## Billing Management

### Get Credit Balance

```http
GET /v1/billing/credits
```

**Response**:
```json
{
  "balance": 1000.00,
  "currency": "USD",
  "last_updated": "2024-01-20T15:30:00Z"
}
```

### Get Usage Summary

```http
GET /v1/billing/usage
```

**Query Parameters**:
- `start_date`: Start of period (YYYY-MM-DD)
- `end_date`: End of period
- `group_by`: Grouping (api_key, model, day)

**Response**:
```json
{
  "usage": [
    {
      "date": "2024-01-20",
      "model": "grok-4",
      "input_tokens": 1000000,
      "output_tokens": 500000,
      "cost": 25.00
    }
  ],
  "total_cost": 25.00
}
```

## Team Management

### Get Team Info

```http
GET /v1/team
```

### List Team Members

```http
GET /v1/team/members
```

### Invite Member

```http
POST /v1/team/invitations
```

## Audit Logs

### List Audit Events

```http
GET /v1/audit-logs
```

**Query Parameters**:
- `start_time`: Filter start
- `end_time`: Filter end
- `action`: Filter by action type
- `actor`: Filter by user

**Response**:
```json
{
  "events": [
    {
      "id": "evt_abc123",
      "action": "api_key.created",
      "actor": "user@example.com",
      "timestamp": "2024-01-20T10:00:00Z",
      "details": {
        "key_name": "production-key"
      }
    }
  ]
}
```

## Error Responses

```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid management key"
  }
}
```

### Error Codes

| Code | Description |
|------|-------------|
| `unauthorized` | Invalid or missing management key |
| `forbidden` | Insufficient permissions |
| `not_found` | Resource not found |
| `rate_limited` | Too many requests |
| `invalid_request` | Invalid request format |

## Python Example

```python
import requests

class XAIManagement:
    def __init__(self, management_key):
        self.base_url = "https://management-api.x.ai/v1"
        self.headers = {
            "Authorization": f"Bearer {management_key}",
            "Content-Type": "application/json"
        }

    def list_api_keys(self):
        r = requests.get(f"{self.base_url}/api-keys", headers=self.headers)
        return r.json()

    def create_api_key(self, name, permissions=None):
        data = {"name": name, "permissions": permissions or ["inference"]}
        r = requests.post(f"{self.base_url}/api-keys", headers=self.headers, json=data)
        return r.json()

    def get_usage(self, start_date, end_date):
        params = {"start_date": start_date, "end_date": end_date}
        r = requests.get(f"{self.base_url}/billing/usage", headers=self.headers, params=params)
        return r.json()

    def get_credits(self):
        r = requests.get(f"{self.base_url}/billing/credits", headers=self.headers)
        return r.json()
```

## Best Practices

1. **Secure management keys**: Never expose in client-side code
2. **Rotate keys regularly**: Update management keys periodically
3. **Use least privilege**: Grant minimal permissions needed
4. **Monitor audit logs**: Review for suspicious activity
5. **Set rate limits**: Protect against abuse with ACLs
