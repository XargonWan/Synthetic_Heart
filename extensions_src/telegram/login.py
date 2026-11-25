"""Logging the user account in: phone -> code -> optional two-step password.

State machine driven from the setup panel the Core draws::

    idle --send_code--> code_sent --verify_code--> authorized
                            |                          ^
                            +--(2FA enabled)--> password_needed --verify_password--+

A temporary client (empty ``StringSession``) is kept alive between steps because the
code's hash is bound to the connection that asked for it. On success the resulting
session string is stored through ``ctx.storage`` (private to this extension) and never
logged or sent to the browser. Nothing a person types (code, password) is logged.
"""

from __future__ import annotations

import time
from typing import Any

SESSION_KEY = "session"
#: A login attempt idle this long is dropped (code hashes expire anyway).
TTL = 600.0

_FRIENDLY = {
    "PhoneNumberInvalidError": "The phone number is not valid (use +<country><number>).",
    "PhoneCodeInvalidError": "The code is wrong.",
    "PhoneCodeExpiredError": "The code expired: request a new one.",
    "PasswordHashInvalidError": "Wrong two-step verification password.",
    "ApiIdInvalidError": "API id / API hash rejected by Telegram.",
}


def describe(exc: BaseException) -> str:
    """An error text for the person that never contains a secret."""
    name = type(exc).__name__
    if name == "FloodWaitError":
        return f"Too many attempts, wait {getattr(exc, 'seconds', '?')}s."
    return _FRIENDLY.get(name, name)


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return phone[:3] + "*" * max(len(phone) - 5, 0) + phone[-2:]


class LoginFlow:
    """One in-flight login attempt."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._client: Any = None
        self._phone: str | None = None
        self._hash: str | None = None
        self.state = "idle"  # idle | code_sent | password_needed | authorized
        self.error: str | None = None
        self.session: str | None = None  # set once authorized, until stored
        self._touched = 0.0

    def expire_if_stale(self) -> bool:
        if self.state in ("code_sent", "password_needed") and (
            self._clock() - self._touched > TTL
        ):
            self.state, self._phone, self._hash = "idle", None, None
            return True
        return False

    async def _drop(self) -> None:
        client, self._client = self._client, None
        self._hash = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        await self._drop()
        self.error = None
        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.error, self.state = describe(exc), "idle"
            return
        self._client, self._phone, self._hash = client, phone, sent.phone_code_hash
        self.state, self._touched = "code_sent", self._clock()

    async def verify_code(self, code: str) -> None:
        from telethon import errors

        if self.state != "code_sent" or self._client is None:
            self.error = "No login in progress: request a code first."
            return
        try:
            await self._client.sign_in(
                phone=self._phone, code=code.strip(), phone_code_hash=self._hash
            )
        except errors.SessionPasswordNeededError:
            self.state, self.error, self._touched = (
                "password_needed",
                None,
                self._clock(),
            )
            return
        except Exception as exc:  # noqa: BLE001
            self.error = describe(exc)
            return
        await self._finish()

    async def verify_password(self, password: str) -> None:
        if self.state != "password_needed" or self._client is None:
            self.error = "Two-step verification is not pending."
            return
        try:
            await self._client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            self.error = describe(exc)
            return
        await self._finish()

    async def cancel(self) -> None:
        await self._drop()
        self.state, self.error, self._phone = "idle", None, None

    async def _finish(self) -> None:
        from telethon.sessions import StringSession

        assert self._client is not None
        self.session = StringSession.save(self._client.session)
        self.state, self.error = "authorized", None
        await self._drop()


def panel(
    *,
    connected_as: str | None,
    has_credentials: bool,
    flow: LoginFlow,
    phone: str,
    has_session: bool,
) -> dict[str, Any]:
    """The setup panel for the current situation, as data for the Core to draw."""
    blocks: list[dict[str, Any]] = []
    if flow.error:
        blocks.append({"type": "error", "text": flow.error})
    if connected_as:
        blocks.append({"type": "text", "text": f"Connected as {connected_as}."})
        blocks.append({"type": "button", "action": "logout", "label": "Log out"})
    elif not has_credentials:
        blocks.append(
            {
                "type": "text",
                "text": "Set the API ID and API hash (from my.telegram.org) in the "
                "extension settings, then come back here.",
            }
        )
    elif flow.state == "code_sent":
        blocks.append(
            {
                "type": "text",
                "text": f"Code sent to {mask_phone(flow._phone) or 'your account'} "  # noqa: SLF001
                "(check the Telegram app or SMS).",
            }
        )
        blocks.append(
            {
                "type": "form",
                "action": "verify_code",
                "submit": "Verify",
                "fields": [{"name": "code", "label": "Login code", "kind": "code"}],
            }
        )
        blocks.append({"type": "button", "action": "cancel", "label": "Cancel"})
    elif flow.state == "password_needed":
        blocks.append(
            {
                "type": "text",
                "text": "Two-step verification is on: enter your Telegram password.",
            }
        )
        blocks.append(
            {
                "type": "form",
                "action": "verify_password",
                "submit": "Verify",
                "fields": [
                    {"name": "password", "label": "Password", "kind": "password"}
                ],
            }
        )
        blocks.append({"type": "button", "action": "cancel", "label": "Cancel"})
    elif flow.state == "authorized" or has_session:
        blocks.append({"type": "text", "text": "Logged in. Connecting..."})
        blocks.append({"type": "button", "action": "logout", "label": "Log out"})
    else:
        blocks.append(
            {
                "type": "form",
                "action": "send_code",
                "submit": "Send code",
                "fields": [
                    {
                        "name": "phone",
                        "label": "Phone",
                        "kind": "tel",
                        "placeholder": "+391234567890",
                        "value": phone,
                    }
                ],
            }
        )
    return {"title": "Account login", "blocks": blocks}
