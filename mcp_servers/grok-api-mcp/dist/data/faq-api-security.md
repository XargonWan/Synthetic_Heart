# FAQ - xAI API Security

Security best practices and guidelines for the xAI API.

## API Key Security

### How should I treat my API keys?

Your xAI API keys should be treated as sensitive information, like passwords or credit card details.

Best practices:
- **Don't share keys** between teammates to avoid unauthorized access
- **Store keys securely** using environment variables or secret management tools
- **Never commit keys** to public repositories or source code
- **Rotate keys regularly** for added security

### What should I do if my API key is compromised?

If you suspect your key has been compromised:

1. Log into the xAI console immediately
2. Ensure you're viewing the correct team (API keys are tied to specific teams)
3. Navigate to the "API Keys" section via the sidebar
4. Click the vertical ellipsis next to the compromised key
5. Select "Disable key" or "Delete key"
6. Create a new API key
7. Update your applications with the new key

### Does xAI detect leaked keys?

Yes. xAI partners with GitHub's Secret Scanning program to detect leaked keys in public repositories. If a leak is detected, you'll be notified.

## Data Privacy

### Does xAI train on my API data?

**No.** xAI never trains on your API inputs or outputs without your explicit permission.

### How long is API data retained?

API requests and responses are temporarily stored on xAI servers for **30 days** for potential abuse/misuse auditing. This data is automatically deleted after 30 days.

### Can I opt out of data retention?

For the Responses API, you can set `store: false` on your requests to prevent conversation history from being stored on the server.

## Network Security

### What protocols are supported?

All API communication uses HTTPS (TLS 1.2+). Unencrypted HTTP connections are not supported.

### Are there IP allowlists?

Enterprise customers may have options for IP allowlisting. Contact support@x.ai for details.

## Access Control

### How do I control who can use my API keys?

- Create separate API keys for different applications or team members
- Use the Management API to set rate limits and token limits per key
- Regularly audit API key usage in the console
- Disable keys that are no longer needed

### What permissions can I set on API keys?

Using the Management API, you can:
- Limit requests per second (RPS)
- Limit requests per minute (RPM)
- Limit tokens per minute (TPM)
- Enable/disable specific models
- Enable/disable specific endpoints

## Compliance

### What security certifications does xAI have?

Contact enterprise@x.ai for information about security certifications and compliance.

### Is there a SOC 2 report available?

Enterprise customers may request security documentation. Contact enterprise@x.ai.

## Incident Response

### How do I report a security vulnerability?

Contact security@x.ai to report security vulnerabilities. xAI has a responsible disclosure program.

### Where can I check for security incidents?

Monitor status.x.ai for any security-related incidents or outages.

## Enterprise Security

### Are there enterprise security features?

Yes, enterprise customers have access to:
- Dedicated support channels
- Custom data retention policies
- SSO/SAML integration options
- Audit logging
- Custom security configurations

Contact enterprise@x.ai for details.
