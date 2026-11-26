# Matrix

Connect SyntH to a Matrix homeserver (matrix.org or your own): rooms, threads, rich replies,
files and the typing indicator.

## 1. Create an account for the bot

Register a dedicated user on your homeserver, for instance with the
[Element](https://app.element.io/#/register) web client.

## 2. Fill in the settings

- **Homeserver** — e.g. `https://matrix.org`
- **Matrix user id** — the bot's MXID, e.g. `@yoursynth:matrix.org`
- **Access token** *or* **Password** — a token is preferred for a long-running bot; with a
  password SyntH logs in and gets a token.

## 3. Invite the bot

Invite its MXID to a room. With **Auto-join** on, it joins invitations from the trainer and
the **Trusted users** (policy `trainer_only`) or from anyone (`allow_all`). **Allowed rooms**
limits where it answers; **Private messages** decides who may talk to it in a private room.

## Notes

- The interface id is `matrix` and a path is `matrix/<room>` (`matrix/<room>/<thread>` in a
  thread): the id used to differ from the prefix of the stored history, which is now the same.
- The sync state is kept in the extension's own data directory.
- The password and the token are secrets; rotate them if they leak.
