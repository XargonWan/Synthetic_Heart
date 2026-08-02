# Telegram Bot — Setup

Connect SyntH to Telegram in three steps.

## 1. Create a bot

Open a chat with **[@BotFather](https://t.me/BotFather)** on Telegram and send
`/newbot`. Follow the prompts to pick a name and a username. BotFather replies
with an **HTTP API token** that looks like `123456789:AA...`.

> Official guide: <https://core.telegram.org/bots#how-do-i-create-a-bot>

## 2. Fill in the key

Paste that token into **`TELEGRAM_BOT_TOKEN`** in the settings above and save.
SyntH connects to Telegram automatically once a valid token is present.

## 3. Talk to your bot

Search for your bot's username on Telegram, press **Start**, and send a
message. To let SyntH speak in a group, add the bot to the group (and, if you
want it to read every message, disable *Group Privacy* via BotFather →
`/setprivacy`).

## Tips

- The token is a secret — treat it like a password. Regenerate it from
  BotFather (`/revoke`) if it leaks.
- Reply to a specific message by passing `reply_message_id`.
