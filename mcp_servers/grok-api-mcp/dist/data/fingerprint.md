# System Fingerprint

The `system_fingerprint` is a unique identifier included in every API response that serves as a version identifier for the backend system's configuration.

## Overview

Every API response includes a `system_fingerprint` field:

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "grok-4",
  "system_fingerprint": "fp_6ca29cf396",
  "choices": [...]
}
```

## Purpose

The system fingerprint changes whenever any part of the backend system changes:

- Model parameters
- Server settings
- Infrastructure configuration
- System updates

## Use Cases

### 1. Monitoring Backend Modifications

Track when the backend system has been updated:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello"}]
)

fingerprint = response.system_fingerprint
print(f"Current system fingerprint: {fingerprint}")

# Store and compare fingerprints over time
if fingerprint != previous_fingerprint:
    print("Backend system has been updated")
```

### 2. Debugging and Performance Optimization

Use fingerprints to correlate responses with specific system configurations:

```python
# Log fingerprint with each request for debugging
import logging

response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": query}]
)

logging.info(f"Query: {query}")
logging.info(f"Fingerprint: {response.system_fingerprint}")
logging.info(f"Latency: {response.usage.total_tokens}")
```

### 3. Response Integrity Verification

For security-sensitive applications, verify response integrity:

```python
def verify_response_source(response, expected_fingerprints):
    """Verify response came from expected system configuration."""
    if response.system_fingerprint not in expected_fingerprints:
        raise SecurityError("Response from unexpected system configuration")
    return True
```

### 4. Audit Trails for Compliance

Create audit trails tracking which system version produced each response:

```python
audit_record = {
    "timestamp": response.created,
    "request_id": response.id,
    "model": response.model,
    "system_fingerprint": response.system_fingerprint,
    "user_id": current_user_id
}
audit_log.append(audit_record)
```

## Important Notes

- **Variations are normal**: The fingerprint is expected to change over time as the system evolves
- **Not a security token**: Don't use fingerprints for authentication
- **Informational only**: Changes don't indicate breaking changes to the API
- **Consistent within session**: Typically stable within a single session but may change between sessions

## Best Practices

1. **Log fingerprints**: Include in your logging for debugging
2. **Don't hardcode**: Don't build logic that depends on specific fingerprint values
3. **Monitor changes**: Track fingerprint changes for observability
4. **Document for compliance**: Store with responses for audit purposes
