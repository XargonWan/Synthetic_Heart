# Telegram (User account) — Setup

Let SyntH use a **real Telegram account** (a normal user with a phone number),
instead of a BotFather bot. Use `telegram_bot` if you only need a bot.

> Use a dedicated number/account for SyntH. Automating a personal account may
> breach Telegram's Terms of Service and can get it limited.

## 1. Get API credentials

Log in at **<https://my.telegram.org>** → *API development tools* → create an
application. Copy the **api_id** and **api_hash**.

## 2. Fill in the settings

Paste them into **Telegram API ID** and **Telegram API Hash** above, save, then
reload the interface.

## 3. Log in

In the *Account login* card enter the phone number (`+<country><number>`),
press **Send code**, type the code Telegram sends you and, if two-step
verification is on, your password. The session is stored encrypted-at-rest
only as far as your database is; treat it like a password.

## 4. Trainer

Add `telegram:<your_user_id>` to **TRAINER_IDS** to enable trainer features.

## Notes

- Interface paths look like `telegram/<chat_id>[/<topic_id>]`.
- In groups SyntH only answers when addressed (alias, @username, reply).
- **Log out** in the card terminates the session on Telegram's side too.
