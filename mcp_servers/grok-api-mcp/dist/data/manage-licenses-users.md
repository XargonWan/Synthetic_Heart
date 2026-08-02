# Manage Licenses and Users

The Grok Business overview page at console.x.ai is your central hub for handling team licenses and user invitations.

## License Types

### SuperGrok
Standard business access with enhanced quotas and features:
- Higher rate limits than free tier
- Priority access
- Team collaboration features
- Business-grade support

### SuperGrok Heavy
Upgraded performance for demanding workloads:
- Highest rate limits
- Priority queue access
- Extended context capabilities
- Premium support

## Accessing License Management

1. Go to console.x.ai
2. Select your team
3. Navigate to "Licenses" or "Team Management"

## Inviting Users

### Send Invitations

1. Click "Invite Users"
2. Enter email addresses (comma-separated for multiple)
3. Select license type for each user
4. Click "Send Invitations"

### Invitation Status

| Status | Description |
|--------|-------------|
| Pending | Invitation sent, awaiting acceptance |
| Accepted | User has joined the team |
| Expired | Invitation expired (resend if needed) |

## Managing Existing Users

### View Team Members

See all team members with:
- Name and email
- License type
- Status (active/inactive)
- Last active date

### Change License Type

1. Find the user in the list
2. Click on their license type
3. Select new license type
4. Confirm change

### Remove Users

1. Find the user in the list
2. Click "Remove" or the delete icon
3. Confirm removal

**Note**: Removed users lose access immediately.

## License Allocation

### Viewing Available Licenses

The dashboard shows:
- Total licenses purchased
- Licenses in use
- Available licenses

### Adding More Licenses

1. Go to Billing
2. Click "Add Licenses"
3. Select quantity
4. Complete purchase

## User Roles

| Role | Capabilities |
|------|--------------|
| Owner | Full control, billing access |
| Admin | User management, settings |
| Member | Standard access |

## Bulk Operations

### Bulk Invite

Upload a CSV file with email addresses:

```csv
email,license_type
user1@example.com,supergrok
user2@example.com,supergrok_heavy
```

### Bulk Remove

Select multiple users and remove them at once.

## Notifications

Configure notifications for:
- New user joins
- License changes
- Usage alerts

## Best Practices

1. **Assign appropriate licenses**: Match license type to user needs
2. **Regular audits**: Review team membership periodically
3. **Remove inactive users**: Free up licenses for active users
4. **Set up SSO**: Streamline access management (Enterprise)
5. **Monitor usage**: Track who's using their licenses

## Troubleshooting

### User Can't Access

- Verify invitation was accepted
- Check license is active
- Confirm user is logging into correct team

### License Limit Reached

- Remove inactive users
- Purchase additional licenses
- Contact sales for volume discounts

## Enterprise Features

For Grok Enterprise:
- SSO integration
- Directory sync (SCIM)
- Automated provisioning
- Advanced audit logs
