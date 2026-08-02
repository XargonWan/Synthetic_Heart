# FAQ - xAI API Team Management

Frequently asked questions about team management in xAI API.

## Creating Teams

### How do I create a team?

1. Log in to console.x.ai
2. Click "Create Team" or go to Teams section
3. Enter a team name
4. Configure settings
5. Click "Create"

### How many teams can I create?

There's no strict limit on team creation, but each team requires separate billing setup.

### Can I rename my team?

Yes, go to Team Settings and update the team name.

## Team Members

### How do I invite team members?

1. Go to Team → Members
2. Click "Invite"
3. Enter email addresses
4. Select roles for each invitee
5. Send invitations

### How many members can a team have?

Teams can have unlimited members, but you need sufficient licenses for Grok Business.

### What happens when someone leaves the team?

- Their access is revoked immediately
- Any API keys they created remain (unless specifically deleted)
- Their usage history is retained

## Roles & Permissions

### What roles are available?

| Role | Description |
|------|-------------|
| Owner | Full control, billing, can delete team |
| Admin | Manage members, keys, settings |
| Developer | Create/manage own API keys |
| Member | Use team resources, view-only settings |

### How do I change someone's role?

1. Go to Team → Members
2. Find the member
3. Click their current role
4. Select the new role
5. Confirm

### Can I create custom roles?

Custom roles are available for Enterprise plans. Contact sales for details.

## API Keys

### Are API keys shared across the team?

API keys are visible to all team members (depending on permissions) but usage is tracked individually.

### Can I restrict which models a team member can use?

Yes, through Access Control Lists (ACLs) on API keys. Enterprise plans have more granular controls.

## Billing

### How is team billing handled?

Teams share a billing account. Usage from all team API keys is aggregated and billed to the team.

### Can team members see billing information?

Only Owners and members with billing permissions can view billing details.

### How do I set budget limits per member?

1. Go to Team Settings → Budgets
2. Enable per-member budgets
3. Set limits for each member

## Transferring Ownership

### How do I transfer team ownership?

1. Go to Team Settings
2. Find "Transfer Ownership"
3. Select the new owner
4. Confirm transfer (may require additional verification)

### Can I leave a team I own?

You must transfer ownership first before leaving a team you own.

## Deleting Teams

### How do I delete a team?

1. Remove all members except yourself
2. Revoke all API keys
3. Resolve any outstanding billing
4. Go to Team Settings → Delete Team
5. Confirm deletion

### What happens when a team is deleted?

- All API keys are revoked
- Usage history is retained for records
- Active members lose access
- Billing is finalized

## Multi-Team

### Can I be in multiple teams?

Yes, you can be a member of multiple teams with different roles in each.

### How do I switch between teams?

Use the team selector in the Console navigation to switch teams.

### Are my API keys shared across teams?

No, API keys belong to specific teams and aren't shared.

## Enterprise

### What additional team features do Enterprise plans include?

- SSO integration
- Directory sync (SCIM)
- Custom roles
- Advanced audit logs
- Organization management (multiple teams)

### How do I upgrade to Enterprise?

Contact sales@x.ai for Enterprise pricing and features.
