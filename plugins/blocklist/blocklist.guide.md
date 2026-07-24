# Blocklist

Lets Synth **block and unblock users**. Blocked identities are stored so their
messages can be ignored. Useful for handling abuse or unwanted contacts.

Backed by the `blocklist` table.

## Actions

| Action | Purpose |
|--------|---------|
| `block_user` | Block a user. |
| `unblock_user` | Remove a user from the blocklist. |
| `is_user_blocked` | Check whether a user is blocked. |
| `get_blocked_users` | List all blocked users. |
