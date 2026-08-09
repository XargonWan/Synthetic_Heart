# Matrix — Setup

Connect SyntH to a Matrix homeserver (matrix.org or your own).

## 1. Create a Matrix account for the bot

Register a dedicated user on your homeserver. The easiest way is the
**[Element](https://app.element.io/#/register)** web client, or your
homeserver's own registration page.

> About Matrix and homeservers: <https://matrix.org/docs/chat_basics/matrix-for-im/>

## 2. Fill in the keys

In the settings above, set:

- **`MATRIX_HOMESERVER_URL`** — e.g. `https://matrix.org`
- **`MATRIX_MXID`** — the bot's full user id, e.g. `@yoursynth:matrix.org`
- **`MATRIX_PASSWORD`** *or* **`MATRIX_ACCESS_TOKEN`** — either the account
  password (SyntH logs in and obtains a token) or a pre-issued access token.

Save; SyntH connects and (optionally) auto-joins rooms it is invited to.

## 3. Invite the bot to a room

From your normal Matrix account, invite the bot's MXID to a room. Use
`MATRIX_ALLOWED_ROOMS` / auto-join and invite policies to control where it
participates.

## Tips

- Prefer an **access token** over a password for long-running bots.
- The password/token is a secret — rotate it if it leaks.
