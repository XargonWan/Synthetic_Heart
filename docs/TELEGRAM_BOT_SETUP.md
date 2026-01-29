---
description: How to configure the Telegram Bot correctly for groups
---

## Telegram Bot Configuration

### Important: Bot Privacy Mode

By default, Telegram bots have **Privacy Mode** enabled. This prevents them from seeing group messages that are not:
1. Commands (starting with `/`)
2. Replies to the bot's own messages
3. Mentions of the bot's username (e.g. `@MyBot`)
4. Service, channel, or admin messages

**To enable features like "Hey 2B" or waking up on keywords without explicit commands, you MUST disable Privacy Mode.**

#### How to Disable Privacy Mode:
1. Open a chat with **@BotFather** on Telegram.
2. Send the command `/mybots`.
3. Select your bot from the list.
4. Go to **Bot Settings** > **Group Privacy**.
5. Select **Turn off**.
6. **IMPORTANT**: You may need to remove the bot from the group and add it back for the changes to take effect immediately.

### Commands

If Privacy Mode is enabled (or you prefer commands):
- `/wake`: Forces the bot to wake up and listen to all messages in the group.
- `/sleep`: Puts the bot to sleep (only responds to mentions/replies).
- `/status`: Checks the current wake/sleep status.

### Troubleshooting

- **No response in Group**: 
  - Check if the bot is an Admin (admins sometimes bypass privacy restrictions).
  - Verify Privacy Mode is OFF (see above).
  - Use `/status` to see if the bot receives *any* commands.
  - Check logs for "RAW UPDATE RECEIVED" to confirm Telegram is sending data to the bot.
- **Bot responds to everything**: 
  - The bot is in `Awake` state. Use `/sleep` to return to selective attention.
