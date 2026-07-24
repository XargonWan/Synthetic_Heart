# Fluxer Bot — Setup

Fluxer is a self-hostable, Discord-compatible chat platform. Connect SyntH to
your Fluxer instance in a few steps.

## 1. Create a bot on your Fluxer instance

Open your Fluxer instance and go to its **developer / bot settings** (the exact
location depends on your instance). Create a bot application and copy its **bot
token**.

> Fluxer is instance-specific and self-hostable, so follow the documentation of
> the instance you are connecting to. Its API mirrors Discord's, so the Discord
> bot concepts (application → bot → token → invite) apply.

## 2. Fill in the keys

In the settings above, set:

- **`FLUXER_TOKEN`** — the bot token from step 1.
- **`FLUXER_API_BASE_URL`** — the REST API base URL of your instance
  (only needed if you are not using the default).
- **`FLUXER_GATEWAY_URL`** — the WebSocket gateway URL of your instance
  (only needed if you are not using the default).

Save; SyntH connects to the Fluxer gateway automatically once a valid token is
present.

## 3. Invite the bot and talk

Add the bot to a Fluxer server/channel you manage and send a message.

## Tips

- The token is a secret — regenerate it on your instance if it leaks.
- Target a channel with its numeric `channel_id`, or reuse the provided
  `interface_path` to reply in the same channel.
