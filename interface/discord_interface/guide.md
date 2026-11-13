# Discord Bot — Setup

Connect SyntH to Discord in a few steps.

## 1. Create an application and a bot

Go to the **[Discord Developer Portal](https://discord.com/developers/applications)**,
click **New Application**, then open the **Bot** tab and add a bot. Press
**Reset Token** to reveal the **bot token**.

> Official quick start:
> <https://discord.com/developers/docs/quick-start/getting-started>

## 2. Enable the Message Content Intent

Still in the **Bot** tab, turn on **MESSAGE CONTENT INTENT** (under *Privileged
Gateway Intents*). Without it the bot cannot read message text.

## 3. Fill in the key

Paste the bot token into **`DISCORD_BOT_TOKEN`** in the settings above and save.

## 4. Invite the bot to your server

In **OAuth2 → URL Generator** select the `bot` scope and the permissions you
want (at least *Send Messages* and *Read Message History*), open the generated
URL, and add the bot to a server you manage.

## Tips

- The token is a secret — reset it from the portal if it leaks.
- Target a channel with its `channel_id`; reply to a message with
  `reply_message_id`.

## Voice calls

A voice session (the `join_voice_discord` action, the `/join` command, or the automatic
join when the trainer is in the channel) is a **call** on the central call service
(`core/call_manager.py`, see `docs/call_service.rst`). It is **audio only**: Discord gives
bots no camera or Go Live video.

By default the call is heard by speech-to-text and answered by a normal turn and voice:
what people say is cut into utterances, transcribed by Auris and sent to the SyntH as a
call turn; the reply is spoken by Vox and played into the channel. A pickup tone opens the
call and a hang-up tone ends it. It needs Auris and Vox set up (the call warns when they
are off).

The Gemini Live route (the model hears and speaks directly) is optional and **off by
default**: turn on `CALL_LIVE_ENGINE_ENABLED` to use it where a Live engine is available.
