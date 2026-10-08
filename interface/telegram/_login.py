# interface/telegram/_login.py
"""Interactive login (phone -> SMS/app code -> optional 2FA) for the user account.

State machine driven from the WebUI:

    idle --send_code--> code_sent --verify_code--> authorized
                            |                          ^
                            +--(2FA enabled)--> password_needed --verify_password--+

A temporary Telethon client (empty ``StringSession``) is kept alive between the
steps because the phone-code hash is bound to the connection that requested it.
On success the resulting ``StringSession`` is persisted through the config
registry — never logged, never echoed back to the browser.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from core.logging_utils import log_info, log_warning

# A login attempt that sits idle this long is dropped (code hashes expire anyway).
LOGIN_TTL_SECONDS = 600


class LoginFlow:
    """One in-flight login attempt. Safe to call from concurrent HTTP handlers."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._phone: Optional[str] = None
        self._phone_code_hash: Optional[str] = None
        self.state: str = "idle"  # idle | code_sent | password_needed | authorized
        self.error: Optional[str] = None
        self._touched: float = 0.0

    # -- introspection ----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        self._expire_if_stale()
        return {
            "state": self.state,
            "phone": _mask_phone(self._phone),
            "error": self.error,
        }

    def _expire_if_stale(self) -> None:
        if self.state in ("code_sent", "password_needed") and (
            time.time() - self._touched > LOGIN_TTL_SECONDS
        ):
            log_info("[telegram] login attempt expired")
            self._reset_soon()

    def _reset_soon(self) -> None:
        client, self._client = self._client, None
        self.state, self._phone, self._phone_code_hash = "idle", None, None
        if client is not None:
            try:
                asyncio.get_running_loop().create_task(_safe_disconnect(client))
            except RuntimeError:
                pass

    # -- steps ------------------------------------------------------------

    async def send_code(self, api_id: int, api_hash: str, phone: str) -> dict[str, Any]:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        async with self._lock:
            await self._drop_client()
            self.error = None
            client = TelegramClient(StringSession(), api_id, api_hash)
            try:
                await client.connect()
                sent = await client.send_code_request(phone)
            except Exception as exc:
                await _safe_disconnect(client)
                self.error = _describe(exc)
                self.state = "idle"
                log_warning(f"[telegram] send_code failed: {type(exc).__name__}")
                return self.snapshot()
            self._client = client
            self._phone = phone
            self._phone_code_hash = sent.phone_code_hash
            self.state = "code_sent"
            self._touched = time.time()
            return self.snapshot()

    async def verify_code(self, code: str) -> dict[str, Any]:
        from telethon import errors

        async with self._lock:
            if self.state != "code_sent" or self._client is None:
                self.error = "No login in progress: request a code first."
                return self.snapshot()
            try:
                await self._client.sign_in(
                    phone=self._phone,
                    code=code.strip(),
                    phone_code_hash=self._phone_code_hash,
                )
            except errors.SessionPasswordNeededError:
                self.state, self.error = "password_needed", None
                self._touched = time.time()
                return self.snapshot()
            except Exception as exc:
                self.error = _describe(exc)
                return self.snapshot()
            return await self._finish()

    async def verify_password(self, password: str) -> dict[str, Any]:
        async with self._lock:
            if self.state != "password_needed" or self._client is None:
                self.error = "Two-step verification is not pending."
                return self.snapshot()
            try:
                await self._client.sign_in(password=password)
            except Exception as exc:
                self.error = _describe(exc)
                return self.snapshot()
            return await self._finish()

    async def cancel(self) -> dict[str, Any]:
        async with self._lock:
            await self._drop_client()
            self.state, self.error, self._phone = "idle", None, None
            return self.snapshot()

    # -- internals ----------------------------------------------------------

    async def _finish(self) -> dict[str, Any]:
        """Persist the authorized session and hand control back to the interface."""
        from telethon.sessions import StringSession

        session_string = StringSession.save(self._client.session)
        try:
            from core.config_manager import config_registry

            await config_registry.set_value(
                "TELEGRAM_SESSION", session_string, require_persist=True
            )
        except Exception as exc:
            self.error = f"Login succeeded but the session could not be saved: {exc}"
            log_warning("[telegram] could not persist TELEGRAM_SESSION")
            return self.snapshot()
        self.state, self.error = "authorized", None
        log_info("[telegram] login completed; session stored")
        await self._drop_client()
        return self.snapshot()

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._phone_code_hash = None
        if client is not None:
            await _safe_disconnect(client)


async def _safe_disconnect(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


def _mask_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    return phone[:3] + "*" * max(len(phone) - 5, 0) + phone[-2:]


def _describe(exc: BaseException) -> str:
    """User-facing error text that never contains secrets."""
    name = type(exc).__name__
    friendly = {
        "PhoneNumberInvalidError": "The phone number is not valid (use +<country><number>).",
        "PhoneCodeInvalidError": "The code is wrong.",
        "PhoneCodeExpiredError": "The code expired: request a new one.",
        "PasswordHashInvalidError": "Wrong two-step verification password.",
        "ApiIdInvalidError": "API id / API hash rejected by Telegram.",
        "FloodWaitError": f"Too many attempts, wait {getattr(exc, 'seconds', '?')}s.",
    }
    return friendly.get(name, f"{name}")


FLOW = LoginFlow()
