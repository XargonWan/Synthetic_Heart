# Organization Management

Learn how to manage organizations, users, teams, and set up SSO in Grok Business Enterprise.

## Overview

Organization management allows enterprise administrators to:
- Manage users and teams
- Configure security settings
- Set up SSO
- Monitor usage
- Enforce policies

## User Management

### Inviting Users

1. Go to Organization Settings
2. Navigate to Users
3. Click "Invite User"
4. Enter email addresses
5. Assign roles

### User Roles

| Role | Permissions |
|------|-------------|
| Admin | Full organization control |
| Manager | Team management, user management |
| Member | Standard access |
| Viewer | Read-only access |

### Removing Users

1. Go to Users list
2. Select user
3. Click "Remove from Organization"
4. Confirm removal

**Note**: Removing a user revokes all access immediately.

## Team Management

### Creating Teams

1. Go to Teams section
2. Click "Create Team"
3. Name the team
4. Add members
5. Set permissions

### Team Settings

- **Name**: Team display name
- **Description**: Team purpose
- **Members**: Team membership
- **Permissions**: What the team can access

### Team Workspaces

Each team can have dedicated workspaces:
- Shared conversations
- Team-specific settings
- Isolated from other teams

## Single Sign-On (SSO)

### Supported Providers

- Okta
- Azure AD
- Google Workspace
- OneLogin
- Generic SAML 2.0

### Setting Up SSO

1. Go to Organization Settings → Security
2. Select "Configure SSO"
3. Choose your identity provider
4. Enter configuration details:
   - SSO URL
   - Entity ID
   - Certificate
5. Test the connection
6. Enable SSO

### SAML Configuration

```xml
<!-- Example SAML configuration -->
<EntityDescriptor entityID="https://grok.com/saml">
  <SPSSODescriptor>
    <AssertionConsumerService
      Location="https://grok.com/saml/acs"
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"/>
  </SPSSODescriptor>
</EntityDescriptor>
```

### SSO Enforcement

Options:
- **Optional**: Users can use SSO or email/password
- **Required**: All users must use SSO
- **Required with exceptions**: SSO required except for specified users

## Security Settings

### Authentication

- Password policies
- Multi-factor authentication
- Session timeout settings

### Access Controls

- IP allowlisting
- Device management
- API access restrictions

### Data Protection

- Data retention policies
- Export restrictions
- Encryption settings

## Usage & Analytics

### Usage Dashboard

Monitor:
- Active users
- Message volume
- Feature usage
- API consumption

### Reports

Generate reports for:
- User activity
- Team usage
- Cost allocation
- Compliance audits

### Audit Logs

Track:
- User logins
- Configuration changes
- Data access
- Admin actions

## Billing

### Subscription Management

- View current plan
- Upgrade/downgrade
- Add seats
- Manage payment methods

### Usage-Based Billing

For API usage:
- View consumption
- Set alerts
- Allocate budgets by team

### Invoicing

- Monthly invoices
- Detailed breakdown
- Export for accounting

## Policies

### Usage Policies

Set organization-wide policies:
- Acceptable use guidelines
- Data handling requirements
- External sharing rules

### Content Policies

- Content filtering
- Output restrictions
- Domain-specific rules

## Integration Management

### Connected Apps

Manage integrations:
- Slack
- Browser extensions
- API applications

### API Keys

Organization-level API key management:
- View all keys
- Revoke access
- Set permissions

## Best Practices

### Security

1. Enable SSO for all users
2. Use least-privilege access
3. Regular access reviews
4. Monitor audit logs

### User Management

1. Use teams for organization
2. Clear role definitions
3. Offboarding procedures
4. Regular user audits

### Compliance

1. Document policies
2. Regular compliance checks
3. Audit log retention
4. Data handling procedures
