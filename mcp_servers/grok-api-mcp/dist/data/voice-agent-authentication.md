# Voice Agent Authentication

Authenticate WebSocket connections to the Grok Voice Agent API.

**WebSocket Endpoint:** `wss://api.x.ai/v1/realtime`

> **IMPORTANT:** It is recommended to use an ephemeral token when authenticating from the client side (e.g., browser). If you use the xAI API key to authenticate from the client side, the client may see the API key and make unauthorized API requests with it.

## Authentication Methods

You can authenticate WebSocket connections using:
1. **xAI API Key** - For server-side connections
2. **Ephemeral Token** - For client-side connections (recommended for browsers)

## API Key Authentication

Use your xAI API key directly for server-to-server connections.

> **Server-side only:** Only use API key authentication from secure server environments. Never expose your API key in client-side code.

### Python

```python
import os
import websockets

XAI_API_KEY = os.getenv("XAI_API_KEY")
base_url = "wss://api.x.ai/v1/realtime"

# Connect with API key in Authorization header
async with websockets.connect(
    uri=base_url,
    ssl=True,
    additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"}
) as websocket:
    # WebSocket connection is now authenticated
    pass
```

### JavaScript (Node.js)

```javascript
const WebSocket = require('ws');

const ws = new WebSocket('wss://api.x.ai/v1/realtime', {
  headers: {
    'Authorization': `Bearer ${process.env.XAI_API_KEY}`
  }
});
```

## Ephemeral Token Authentication

For client-side applications (browsers, mobile apps), use ephemeral tokens to avoid exposing your API key.

### Why Ephemeral Tokens?

- Short-lived (configurable expiration)
- Scoped access to resources
- Safe to use in client-side code
- Can be revoked without affecting your main API key

### Fetching Ephemeral Tokens

You need to set up a server endpoint to fetch ephemeral tokens from xAI.

**Endpoint:** `POST https://api.x.ai/v1/realtime/client_secrets`

### Python (FastAPI Server Example)

```python
import os
import httpx
from fastapi import FastAPI

app = FastAPI()
SESSION_REQUEST_URL = "https://api.x.ai/v1/realtime/client_secrets"
XAI_API_KEY = os.getenv("XAI_API_KEY")

@app.post("/session")
async def get_ephemeral_token():
    # Send request to xAI endpoint to retrieve the ephemeral token
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url=SESSION_REQUEST_URL,
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"expires_after": {"seconds": 300}},
        )

    # Return the response body from xAI with ephemeral token
    return response.json()
```

### JavaScript (Express Server Example)

```javascript
const express = require('express');
const app = express();

// Endpoint to get ephemeral token for clients
app.post('/api/session', async (req, res) => {
  const response = await fetch('https://api.x.ai/v1/realtime/client_secrets', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.XAI_API_KEY}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      expires_after: { seconds: 300 }
    })
  });

  const data = await response.json();
  res.json(data);
});
```

### Request Format

```http
POST https://api.x.ai/v1/realtime/client_secrets
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json

{
  "expires_after": {
    "seconds": 300
  }
}
```

### Response Format

```json
{
  "client_secret": {
    "value": "ephemeral_token_abc123...",
    "expires_at": 1706792400
  }
}
```

## Using Ephemeral Tokens

### Client-Side (Browser)

```javascript
// 1. Get ephemeral token from your server
const tokenResponse = await fetch('/api/session', { method: 'POST' });
const data = await tokenResponse.json();
const ephemeralToken = data.client_secret.value;

// 2. Connect to Voice API with ephemeral token
const ws = new WebSocket('wss://api.x.ai/v1/realtime', {
  headers: {
    'Authorization': `Bearer ${ephemeralToken}`
  }
});
```

### Request Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `expires_after.seconds` | integer | Time until token expires (in seconds) |

## Token Lifecycle

1. Client requests token from your server
2. Your server requests token from xAI (using API key)
3. xAI returns ephemeral token
4. Your server returns token to client
5. Client uses token to connect to Voice API
6. Token expires after specified duration

## Security Best Practices

### Server-Side

1. **Never expose API key**: Keep your API key on the server only
2. **Validate requests**: Verify client requests before issuing tokens
3. **Set appropriate expiration**: Use short durations (5-10 minutes recommended)
4. **Log token requests**: Track who requests tokens

### Client-Side

1. **Use ephemeral tokens only**: Never embed API keys in client code
2. **Request tokens as needed**: Don't cache tokens long-term
3. **Handle expiration**: Refresh tokens before they expire
4. **Secure storage**: Don't persist tokens in localStorage

## Token Refresh Pattern

Implement token refresh before expiration:

```javascript
class VoiceClient {
  constructor() {
    this.token = null;
    this.tokenExpiry = null;
    this.ws = null;
  }

  async ensureToken() {
    // Refresh if token expires in less than 1 minute
    const now = Math.floor(Date.now() / 1000);
    if (!this.token || now > this.tokenExpiry - 60) {
      await this.refreshToken();
    }
    return this.token;
  }

  async refreshToken() {
    const response = await fetch('/api/session', { method: 'POST' });
    const data = await response.json();
    this.token = data.client_secret.value;
    this.tokenExpiry = data.client_secret.expires_at;
  }

  async connect() {
    const token = await this.ensureToken();
    this.ws = new WebSocket('wss://api.x.ai/v1/realtime', {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    });
  }
}
```

## Error Handling

### Invalid Token

```json
{
  "error": {
    "code": "invalid_token",
    "message": "The provided token is invalid or expired"
  }
}
```

**Solution**: Request a new ephemeral token from your server.

### Expired Token

```json
{
  "error": {
    "code": "token_expired",
    "message": "The token has expired"
  }
}
```

**Solution**: Implement token refresh logic to get a new token before expiration.

### Authentication Failed

```json
{
  "error": {
    "code": "authentication_failed",
    "message": "Authentication failed"
  }
}
```

**Solution**: Verify your API key is valid and has the correct permissions.

## Complete Example: Browser Voice Client

```javascript
// voice-client.js

class VoiceClient {
  constructor(sessionEndpoint) {
    this.sessionEndpoint = sessionEndpoint;
    this.token = null;
    this.tokenExpiry = null;
    this.ws = null;
  }

  async getToken() {
    const response = await fetch(this.sessionEndpoint, { method: 'POST' });
    const data = await response.json();
    this.token = data.client_secret.value;
    this.tokenExpiry = data.client_secret.expires_at;
    return this.token;
  }

  async connect() {
    if (!this.token) {
      await this.getToken();
    }

    this.ws = new WebSocket('wss://api.x.ai/v1/realtime');

    this.ws.onopen = () => {
      // Send authentication
      this.ws.send(JSON.stringify({
        type: 'session.update',
        session: {
          voice: 'Ara',
          instructions: 'You are a helpful assistant.',
          turn_detection: { type: 'server_vad' }
        }
      }));
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      this.handleMessage(data);
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    this.ws.onclose = () => {
      console.log('WebSocket closed');
    };
  }

  handleMessage(data) {
    switch (data.type) {
      case 'session.updated':
        console.log('Session configured');
        break;
      case 'response.output_audio.delta':
        // Handle audio chunk
        this.playAudio(data.delta);
        break;
      case 'response.output_audio_transcript.delta':
        // Handle transcript
        console.log('Transcript:', data.delta);
        break;
    }
  }

  sendAudio(base64Audio) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: 'input_audio_buffer.append',
        audio: base64Audio
      }));
    }
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

// Usage
const client = new VoiceClient('/api/session');
await client.connect();
```
