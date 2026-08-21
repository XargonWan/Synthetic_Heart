# interface/fluxer_interface.py

"""Fluxer chat interface for SyntH.

Provides support for interacting with the Fluxer platform (https://fluxer.app,
also self-hostable) using a small, embedded Python client that speaks Fluxer's
REST API and real-time WebSocket gateway directly. No third-party SDK is
required — the official library (Fluxer.Net) is .NET only, and the runtime image
(``python:3.12-slim``) has no node/dotnet, so we implement the protocol natively
on top of :mod:`aiohttp` (already a project dependency).

The interface follows the same duck-typed contract as the other SyntH
interfaces (Telegram, Discord, Matrix): it registers itself even when a token is
missing so the WebUI can surface the integration and prompt the operator for
configuration. Endpoints are fully configurable because a Fluxer instance can be
self-hosted anywhere.

Scope (v1): inbound text messages, outbound text messages and outbound file
attachments (parity with the Discord interface's message/send-file actions).
Voice/live-audio, reactions and guild management are intentionally out of scope.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

from core import message_queue
from core.command_registry import handle_command_message
from core.config import get_trainer_id as core_get_trainer_id
from core.config_manager import config_registry
from core.core_initializer import register_interface
from core.interface_path_utils import build_interface_path
from core.interface_paths import resolve_and_touch, set_name_resolver
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

load_dotenv()

try:  # pragma: no cover - optional dependency (always present in prod)
    import aiohttp
except Exception:  # pragma: no cover - dependency missing
    aiohttp = None  # type: ignore[assignment]

INTERFACE_NAME = "fluxer_bot"
ACTION_TYPE = "message_fluxer_bot"
FILE_ACTION_TYPE = "send_file_fluxer_bot"

# Fluxer gateway opcodes (mirror FluxerOpCode in the Fluxer.Net source).
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Gateway close codes that must NOT trigger a reconnect (auth is broken).
_FATAL_CLOSE_CODES = {4004}

_interface_registry = get_interface_registry()


def _resolve_api_base(raw_base: str, version: Any) -> str:
    """Return the REST base URL with the ``{v}`` placeholder substituted."""
    base = str(raw_base or "").strip().rstrip("/")
    try:
        ver = str(int(version))
    except Exception:
        ver = "1"
    return base.replace("{v}", ver)


def _rest_auth_header(token: str) -> str:
    """Build the REST ``Authorization`` header value.

    Bot tokens require a ``Bot `` prefix for REST requests; user tokens
    (``flx_...``) are sent verbatim. If the operator already included the
    prefix we keep it as-is.
    """
    tok = str(token or "").strip()
    if not tok:
        return ""
    if tok.lower().startswith("bot "):
        return tok
    if tok.startswith("flx_"):
        return tok
    return f"Bot {tok}"


def _gateway_token(token: str) -> str:
    """Return the raw token for the gateway IDENTIFY/RESUME packets.

    The gateway protocol expects the raw token without the ``Bot `` prefix.
    """
    tok = str(token or "").strip()
    if tok.lower().startswith("bot "):
        return tok[4:].strip()
    return tok


class _FluxerRestClient:
    """Minimal async REST client for the Fluxer API."""

    def __init__(self, token: str, api_base: str) -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._session: Optional["aiohttp.ClientSession"] = None

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None or self._session.closed:
            if aiohttp is None:
                raise RuntimeError("aiohttp is not available")
            headers = {"Authorization": _rest_auth_header(self._token)}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        data: Any = None,
    ) -> Optional[Dict[str, Any]]:
        session = await self._ensure_session()
        url = f"{self._api_base}{path}"
        # Basic 429 handling: honour retry_after up to a couple of attempts.
        for attempt in range(3):
            async with session.request(method, url, json=json_body, data=data) as resp:
                if resp.status == 429:
                    try:
                        body = await resp.json()
                        retry_after = float(body.get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    log_warning(
                        f"[fluxer_interface] Rate limited on {method} {path}; "
                        f"retrying in {retry_after}s"
                    )
                    await asyncio.sleep(min(retry_after, 10.0))
                    continue
                if resp.status >= 400:
                    text = await resp.text()
                    log_error(
                        f"[fluxer_interface] {method} {path} failed "
                        f"({resp.status}): {text[:300]}"
                    )
                    return None
                if resp.status == 204 or resp.content_length == 0:
                    return {}
                try:
                    return await resp.json()
                except Exception:
                    return {}
        return None

    async def get_me(self) -> Optional[Dict[str, Any]]:
        return await self._request("GET", "/users/@me")

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"content": content}
        if reply_to_message_id:
            body["message_reference"] = {"message_id": str(reply_to_message_id)}
        return await self._request(
            "POST", f"/channels/{channel_id}/messages", json_body=body
        )

    async def send_file(
        self,
        channel_id: str,
        file_path: Path,
        *,
        caption: Optional[str] = None,
        mime_type: str = "application/octet-stream",
    ) -> Optional[Dict[str, Any]]:
        if aiohttp is None:
            raise RuntimeError("aiohttp is not available")
        session = await self._ensure_session()
        url = f"{self._api_base}/channels/{channel_id}/messages"
        payload: Dict[str, Any] = {}
        if caption and caption.strip():
            payload["content"] = caption

        form = aiohttp.FormData()
        form.add_field(
            "payload_json", json.dumps(payload), content_type="application/json"
        )
        with open(file_path, "rb") as fh:
            form.add_field(
                "files[0]",
                fh.read(),
                filename=file_path.name,
                content_type=mime_type,
            )
        for attempt in range(3):
            async with session.post(url, data=form) as resp:
                if resp.status == 429:
                    try:
                        body = await resp.json()
                        retry_after = float(body.get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    await asyncio.sleep(min(retry_after, 10.0))
                    continue
                if resp.status >= 400:
                    text = await resp.text()
                    log_error(
                        f"[fluxer_interface] File upload to {channel_id} failed "
                        f"({resp.status}): {text[:300]}"
                    )
                    return None
                try:
                    return await resp.json()
                except Exception:
                    return {}
        return None


class _FluxerGatewayClient:
    """Minimal async WebSocket gateway client for the Fluxer platform.

    Handles the HELLO/IDENTIFY/heartbeat/RESUME lifecycle and forwards
    ``MESSAGE_CREATE`` dispatches to ``on_message``. Reconnects automatically
    with session resume where possible.
    """

    def __init__(
        self,
        token: str,
        gateway_url: str,
        *,
        on_message: Callable[[Dict[str, Any]], Any],
        on_ready: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self._token = token
        self._gateway_url = gateway_url
        self._on_message = on_message
        self._on_ready = on_ready

        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._session: Optional["aiohttp.ClientSession"] = None
        self._heartbeat_interval_ms: int = 0
        self._sequence: Optional[int] = None
        self._session_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Run the gateway with automatic reconnection until stopped."""
        if aiohttp is None:
            log_error("[fluxer_interface] aiohttp unavailable; gateway cannot start")
            return
        backoff = 5
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 5  # reset after a clean session
            except _FatalGatewayError as exc:
                log_error(
                    f"[fluxer_interface] Fatal gateway error, not reconnecting: {exc}"
                )
                return
            except Exception as exc:  # pragma: no cover - network failure
                log_error(f"[fluxer_interface] Gateway error: {exc}")
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass

    async def _connect_once(self) -> None:
        assert aiohttp is not None
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.ws_connect(self._gateway_url) as ws:
                self._ws = ws
                log_info("[fluxer_interface] Gateway WebSocket connected")
                async for msg in ws:
                    if self._stop.is_set():
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_text(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"WebSocket error: {ws.exception()}")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        break
                close_code = ws.close_code
                if close_code in _FATAL_CLOSE_CODES:
                    raise _FatalGatewayError(f"close code {close_code}")
                log_warning(
                    f"[fluxer_interface] Gateway closed (code={close_code}); "
                    "will reconnect"
                )
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._ws = None

    async def _handle_text(self, raw: str) -> None:
        try:
            packet = json.loads(raw)
        except Exception:
            log_warning("[fluxer_interface] Received non-JSON gateway packet")
            return

        op = packet.get("op")
        seq = packet.get("s")
        if seq is not None:
            self._sequence = seq

        if op == OP_HELLO:
            data = packet.get("d", {}) or {}
            self._heartbeat_interval_ms = int(data.get("heartbeat_interval", 30000))
            self._start_heartbeat()
            await self._identify_or_resume()
        elif op == OP_HEARTBEAT:
            await self._send_heartbeat()
        elif op == OP_HEARTBEAT_ACK:
            log_debug("[fluxer_interface] Heartbeat acknowledged")
        elif op == OP_RECONNECT:
            log_info("[fluxer_interface] Gateway requested reconnect")
            if self._ws and not self._ws.closed:
                await self._ws.close()
        elif op == OP_INVALID_SESSION:
            log_warning("[fluxer_interface] Invalid session; re-identifying")
            self._session_id = None
            self._sequence = None
            await asyncio.sleep(1 + random.random() * 4)
            await self._identify_or_resume()
        elif op == OP_DISPATCH:
            await self._handle_dispatch(packet)

    async def _handle_dispatch(self, packet: Dict[str, Any]) -> None:
        event = packet.get("t")
        data = packet.get("d", {}) or {}
        if event == "READY":
            self._session_id = data.get("session_id")
            log_info("[fluxer_interface] Gateway READY")
            if self._on_ready:
                try:
                    await _maybe_await(self._on_ready(data))
                except Exception as exc:
                    log_warning(f"[fluxer_interface] on_ready handler failed: {exc}")
        elif event == "MESSAGE_CREATE":
            try:
                await _maybe_await(self._on_message(data))
            except Exception as exc:
                log_error(f"[fluxer_interface] on_message handler failed: {exc}")

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        interval = max(self._heartbeat_interval_ms, 1000) / 1000.0
        jitter = random.random() * 0.5
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval + jitter)
                await self._send_heartbeat()
        except asyncio.CancelledError:  # pragma: no cover - task cancellation
            pass

    async def _send_heartbeat(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"op": OP_HEARTBEAT, "d": self._sequence})

    async def _identify_or_resume(self) -> None:
        if not self._ws or self._ws.closed:
            return
        if self._session_id and self._sequence is not None:
            packet = {
                "op": OP_RESUME,
                "d": {
                    "token": _gateway_token(self._token),
                    "session_id": self._session_id,
                    "seq": self._sequence,
                },
            }
            log_info("[fluxer_interface] Sending RESUME")
        else:
            packet = {
                "op": OP_IDENTIFY,
                "d": {
                    "token": _gateway_token(self._token),
                    "properties": {
                        "os": "linux",
                        "browser": "synthetic-heart",
                        "device": "synthetic-heart",
                    },
                },
            }
            log_info("[fluxer_interface] Sending IDENTIFY")
        await self._ws.send_json(packet)


class _FatalGatewayError(Exception):
    """Raised when the gateway must not attempt to reconnect (e.g. bad auth)."""


async def _maybe_await(result: Any) -> Any:
    if asyncio.iscoroutine(result):
        return await result
    return result


class FluxerInterface:
    """Fluxer chat interface backed by an embedded REST + gateway client."""

    display_name = "Fluxer Bot"

    # Declarative "must-have" configuration checked by the loader
    # (core_initializer). Without a token the interface is present in the WebUI
    # but registers no actions and shows a red LED until configured.
    required_config_vars = ["FLUXER_TOKEN"]

    _current_instance_enabled: bool = False

    def __init__(
        self,
        token: Optional[str],
        *,
        api_base: Optional[str] = None,
        gateway_url: Optional[str] = None,
        api_version: Any = 1,
        trainer_id: int | str | None = None,
    ) -> None:
        self.token = str(token or "").strip()
        self.api_base = _resolve_api_base(
            api_base or "https://api.fluxer.app/v{v}", api_version
        )
        self.gateway_url = str(
            gateway_url or "wss://gateway.fluxer.app/?v=1&encoding=json"
        ).strip()
        self.trainer_id = trainer_id

        self.is_enabled = True
        self.disabled_reason: Optional[str] = None

        # Cached bot identity (learned from READY / GET /users/@me) so the
        # interface never replies to its own messages.
        self._self_user_id: Optional[str] = None

        self._rest: Optional[_FluxerRestClient] = None
        self._gateway: Optional[_FluxerGatewayClient] = None
        self._gateway_task: Optional[asyncio.Task] = None

        # Gatekeeping
        if aiohttp is None:
            self._disable("aiohttp is not installed")
        elif not self.token:
            log_warning(
                "[fluxer_interface] No FLUXER_TOKEN configured — interface will be "
                "available in the WebUI but will not connect until a token is set"
            )

        if self.is_enabled:
            self._rest = _FluxerRestClient(self.token, self.api_base)

            async def _resolver(
                channel_id: int | str,
                thread_id: Optional[int | str],
                bot_instance: Any = None,
            ) -> Dict[str, Optional[str]]:
                # Fluxer does not expose a cheap channel-name lookup in v1;
                # fall back to the channel id as the display name.
                return {
                    "chat_name": str(channel_id),
                    "message_thread_name": None,
                }

            set_name_resolver(INTERFACE_NAME, _resolver)

        FluxerInterface._current_instance_enabled = self.is_enabled

        register_interface(INTERFACE_NAME, self)
        _interface_registry.register_interface(INTERFACE_NAME, self)
        if self.trainer_id is not None:
            _interface_registry.set_trainer_id(INTERFACE_NAME, self.trainer_id)

        if self.is_enabled:
            log_info("[fluxer_interface] Fluxer interface registered")
            if self.token:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.start())
                except RuntimeError:
                    log_debug(
                        "[fluxer_interface] No event loop available; interface will "
                        "start during application initialization"
                    )
            else:
                log_info(
                    "[fluxer_interface] Interface initialized but not authenticated — "
                    "provide FLUXER_TOKEN to enable the connection"
                )
        else:
            reason = self.disabled_reason or "missing configuration"
            log_warning(
                f"[fluxer_interface] Interface loaded in disabled state: {reason}"
            )

    def _disable(self, reason: str) -> None:
        self.is_enabled = False
        self.disabled_reason = reason
        FluxerInterface._current_instance_enabled = False

    # ------------------------------------------------------------------
    # Registration metadata
    @staticmethod
    def get_interface_id() -> str:
        return INTERFACE_NAME

    @staticmethod
    def get_action_types() -> List[str]:
        if not FluxerInterface._current_instance_enabled:
            return []
        return [ACTION_TYPE, FILE_ACTION_TYPE]

    @staticmethod
    def get_supported_actions() -> Dict[str, Dict[str, Any]]:
        if not FluxerInterface._current_instance_enabled:
            return {}
        return {
            ACTION_TYPE: {
                "description": "Send a text message to a Fluxer channel.",
                "required_fields": ["text"],
                "optional_fields": [
                    "interface_path",
                    "channel_id",
                    "reply_to_message_id",
                ],
            },
            FILE_ACTION_TYPE: {
                "description": (
                    "Send a file attachment (image, video, audio or document) to a "
                    "Fluxer channel. The file must live inside Synth's filesystem "
                    "sandbox."
                ),
                "required_fields": ["path"],
                "optional_fields": ["interface_path", "channel_id", "caption"],
                "security_level": "medium",
                "external_effects": ["filesystem"],
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if not FluxerInterface._current_instance_enabled:
            return {}
        if action_name == ACTION_TYPE:
            return {
                "description": "Send a message to a Fluxer channel.",
                "payload": {
                    "text": {
                        "type": "string",
                        "example": "Hello Fluxer!",
                        "description": "Content of the message.",
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "fluxer_bot/123/456",
                        "description": (
                            "Interface path of the originating conversation. Reuse it "
                            "verbatim to reply in the same channel."
                        ),
                        "optional": True,
                    },
                    "channel_id": {
                        "type": "string",
                        "example": "456",
                        "description": (
                            "Explicit Fluxer channel id. Optional when interface_path "
                            "is provided."
                        ),
                        "optional": True,
                    },
                    "reply_to_message_id": {
                        "type": "string",
                        "description": "Optional message id to reply to.",
                        "optional": True,
                    },
                },
            }
        if action_name == FILE_ACTION_TYPE:
            return {
                "description": "Send a file attachment to a Fluxer channel.",
                "payload": {
                    "path": {
                        "type": "string",
                        "example": "/app/data/report.pdf",
                        "description": (
                            "Path to the file to send. Must be inside Synth's "
                            "filesystem sandbox."
                        ),
                    },
                    "interface_path": {
                        "type": "string",
                        "example": "fluxer_bot/123/456",
                        "description": "Interface path of the target channel.",
                        "optional": True,
                    },
                    "channel_id": {
                        "type": "string",
                        "example": "456",
                        "description": "Explicit Fluxer channel id.",
                        "optional": True,
                    },
                    "caption": {
                        "type": "string",
                        "example": "Here is the report you asked for",
                        "description": "Optional text sent alongside the file.",
                        "optional": True,
                    },
                },
            }
        return {}

    @staticmethod
    def get_interface_instructions() -> str:
        return (
            "Fluxer is a Discord-like chat platform. Messages are delivered to "
            "channels identified by a numeric channel id. To reply in the same "
            "channel a message came from, reuse the provided interface_path."
        )

    @staticmethod
    def validate_payload(action_type: str, payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if action_type not in (ACTION_TYPE, FILE_ACTION_TYPE):
            return errors
        if not FluxerInterface._current_instance_enabled:
            errors.append(
                "Fluxer interface is disabled - configure FLUXER_TOKEN to enable "
                "messaging"
            )
            return errors

        channel_id = payload.get("channel_id")
        interface_path = payload.get("interface_path")
        has_target = bool(
            (isinstance(channel_id, (str, int)) and str(channel_id).strip())
            or (isinstance(interface_path, str) and interface_path.strip())
        )
        if not has_target:
            errors.append("payload.channel_id or payload.interface_path is required")

        if action_type == ACTION_TYPE:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                errors.append("payload.text must be a non-empty string")
            reply_to = payload.get("reply_to_message_id")
            if reply_to is not None and not isinstance(reply_to, (str, int)):
                errors.append("payload.reply_to_message_id must be a string")
        elif action_type == FILE_ACTION_TYPE:
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                errors.append("payload.path must be a non-empty string")
        return errors

    # ------------------------------------------------------------------
    # Helpers
    @staticmethod
    def _channel_from_interface_path(interface_path: Optional[str]) -> Optional[str]:
        """Extract the channel id from a ``fluxer_bot/...`` interface path.

        Layout: ``fluxer_bot/<guild_id>/<channel_id>[/<thread_id>]`` for guild
        channels, ``fluxer_bot/<channel_id>`` for direct messages.
        """
        if not interface_path or not isinstance(interface_path, str):
            return None
        parts = [p for p in interface_path.split("/") if p]
        if not parts or parts[0] != INTERFACE_NAME:
            return None
        levels = parts[1:]
        if len(levels) >= 2:
            return levels[1]  # <guild_id>/<channel_id>
        if len(levels) == 1:
            return levels[0]  # DM: <channel_id>
        return None

    def _resolve_channel_id(self, payload: Dict[str, Any]) -> Optional[str]:
        channel_id = payload.get("channel_id")
        if isinstance(channel_id, (str, int)) and str(channel_id).strip():
            return str(channel_id).strip()
        return self._channel_from_interface_path(payload.get("interface_path"))

    # ------------------------------------------------------------------
    # Lifecycle management
    async def start(self) -> None:
        if not self.is_enabled:
            log_debug("[fluxer_interface] start() skipped - interface disabled")
            return
        if not self.token:
            log_debug("[fluxer_interface] start() skipped - no token configured")
            return
        if self._gateway_task and not self._gateway_task.done():
            return

        await message_queue.run()

        # Learn our own user id so we never reply to ourselves.
        try:
            if self._rest:
                me = await self._rest.get_me()
                if me and me.get("id") is not None:
                    self._self_user_id = str(me["id"])
        except Exception as exc:
            log_debug(f"[fluxer_interface] Could not fetch self identity: {exc}")

        self._gateway = _FluxerGatewayClient(
            self.token,
            self.gateway_url,
            on_message=self._on_message,
            on_ready=self._on_ready,
        )
        self._gateway_task = asyncio.create_task(self._gateway.run())
        log_info("[fluxer_interface] Gateway task scheduled")

    async def stop(self) -> None:
        if self._gateway:
            await self._gateway.stop()
        if self._gateway_task:
            self._gateway_task.cancel()
            self._gateway_task = None
        if self._rest:
            await self._rest.close()

    # ------------------------------------------------------------------
    # Gateway event handlers
    async def _on_ready(self, data: Dict[str, Any]) -> None:
        user = data.get("user") or {}
        user_id = user.get("id")
        if user_id is not None:
            self._self_user_id = str(user_id)
            log_debug(f"[fluxer_interface] Cached self user id {self._self_user_id}")

    async def _on_message(self, data: Dict[str, Any]) -> None:
        if not self.is_enabled:
            return
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        message = message or {}

        author = message.get("author") or {}
        author_id = author.get("id")
        if author_id is not None and str(author_id) == str(self._self_user_id):
            return  # ignore our own messages
        if author.get("bot") is True:
            return  # ignore other bots to avoid loops

        text = message.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            return

        channel_id = message.get("channel_id")
        if channel_id is None:
            return
        channel_id = str(channel_id)
        guild_id = message.get("guild_id")

        if guild_id is not None:
            interface_path = build_interface_path(
                INTERFACE_NAME, str(guild_id), channel_id
            )
        else:
            interface_path = build_interface_path(INTERFACE_NAME, channel_id)

        sender_name = author.get("username") or author.get("global_name") or "unknown"
        sender_id = str(author_id) if author_id is not None else "unknown"
        message_id = message.get("id")

        # Discord-like reply payload (2026-08-21 reply-context fix): when the
        # user replies to a message, the gateway event carries
        # ``referenced_message`` (the full quoted message) and/or
        # ``message_reference`` (ids only). Extract the quoted TEXT so both
        # the prompt paths (message.reply_to_message) and the history metadata
        # can show what the user is replying to. Previously nothing was read
        # inbound — the quote was silently lost.
        _ref = message.get("referenced_message")
        _ref_ref = message.get("message_reference")
        reply_to_message = None
        _reply_meta: dict | None = None
        if isinstance(_ref, dict) and _ref.get("id") is not None:
            _ref_author = _ref.get("author") or {}
            _ref_name = (
                _ref_author.get("username")
                or _ref_author.get("global_name")
                or "unknown"
            )
            _ref_text = str(_ref.get("content") or "")
            reply_to_message = SimpleNamespace(
                message_id=str(_ref.get("id")),
                text=_ref_text or None,
                caption=None,
                date=None,
                from_user=SimpleNamespace(
                    id=(
                        str(_ref_author.get("id"))
                        if _ref_author.get("id") is not None
                        else None
                    ),
                    username=_ref_name,
                    full_name=_ref_author.get("global_name") or _ref_name,
                ),
            )
            if _ref_text.strip():
                _reply_meta = {
                    "reply_to": {
                        "sender_name": _ref_name,
                        "text": _ref_text,
                        "message_id": str(_ref.get("id")),
                    }
                }
        elif isinstance(_ref_ref, dict) and _ref_ref.get("message_id") is not None:
            # Ids only: keep the reference for outbound threading, but there is
            # no local way to resolve the quoted text.
            reply_to_message = SimpleNamespace(
                message_id=str(_ref_ref["message_id"]),
                text=None,
                caption=None,
                date=None,
                from_user=SimpleNamespace(id=None, username=None, full_name=None),
            )

        try:
            from core.chat_context_manager import add_message_to_context

            await add_message_to_context(
                interface_path=interface_path,
                message_text=text,
                sender_name=sender_name,
                sender_id=sender_id,
                message_id=None,
                timestamp=message.get("timestamp"),
                metadata=_reply_meta,
                fluxer_message_id=(str(message_id) if message_id is not None else None),
            )
        except Exception as exc:
            log_warning(f"[fluxer_interface] Failed to add message to context: {exc}")

        wrapped = SimpleNamespace(
            message_id=str(message_id) if message_id is not None else None,
            chat_id=channel_id,
            interface_path=interface_path,
            text=text,
            caption=None,
            date=None,
            thread_id=None,
            from_user=SimpleNamespace(
                id=sender_id,
                username=sender_name,
                full_name=author.get("global_name") or sender_name,
            ),
            chat=SimpleNamespace(
                id=channel_id,
                type="group" if guild_id is not None else "private",
                title=channel_id,
                username=None,
                first_name=None,
            ),
            entities=None,
            reply_to_message=reply_to_message,
        )

        try:
            await resolve_and_touch(
                interface_path,
                channel_id,
                None,
                bot=self,
            )
        except Exception as exc:
            log_warning(f"[fluxer_interface] Failed to update chat link names: {exc}")

        if text.startswith("/"):
            try:
                response = await handle_command_message(
                    text,
                    user_id=sender_id,
                    interface_id=INTERFACE_NAME,
                    interface_context={"bot": self, "wrapped": wrapped},
                )
                if response:
                    await self.send_message(channel_id=channel_id, text=response)
            except Exception as exc:  # pragma: no cover - command failure
                log_error(f"[fluxer_interface] Command failed: {exc}")
            return

        await message_queue.enqueue(self, wrapped, interface_id=INTERFACE_NAME)

    # ------------------------------------------------------------------
    # Messaging helpers
    async def send_message(
        self,
        channel_id: Any = None,
        text: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """Send a text message. Accepts either an action payload dict as the
        first positional argument (how the action dispatcher calls it) or an
        explicit ``channel_id`` + ``text``.
        """
        if not self.is_enabled or not self._rest:
            log_warning(
                "[fluxer_interface] Cannot send message - interface is disabled"
            )
            return False

        skip_history = False
        reply_to_message_id: Optional[str] = None
        interface_path: Optional[str] = None

        if isinstance(channel_id, dict):
            payload = channel_id
            text = payload.get("text", text)
            interface_path = payload.get("interface_path")
            resolved_channel = self._resolve_channel_id(payload)
            reply_to_message_id = payload.get("reply_to_message_id")
            skip_history = bool(payload.get("skip_history", False))
        else:
            payload = kwargs
            resolved_channel = (
                str(channel_id).strip()
                if channel_id is not None and str(channel_id).strip()
                else self._resolve_channel_id(payload)
            )
            interface_path = payload.get("interface_path")
            reply_to_message_id = payload.get("reply_to_message_id")
            skip_history = bool(payload.get("skip_history", False))

        if not resolved_channel:
            log_warning("[fluxer_interface] Cannot send message - channel id missing")
            return False
        if not text or not str(text).strip():
            log_warning("[fluxer_interface] Cannot send message - text missing")
            return False

        result = await self._rest.send_message(
            resolved_channel,
            str(text),
            reply_to_message_id=(
                str(reply_to_message_id) if reply_to_message_id else None
            ),
        )
        if result is None:
            log_error(
                f"[fluxer_interface] send_message failed for channel {resolved_channel}"
            )
            return False

        if not skip_history:
            try:
                from core.chat_context_manager import save_response_message

                path = interface_path or build_interface_path(
                    INTERFACE_NAME, resolved_channel
                )
                await save_response_message(path, str(text))
            except Exception as exc:
                log_debug(
                    f"[fluxer_interface] Failed to save response via "
                    f"context_manager: {exc}"
                )

        return True

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        """Dispatch non-message Fluxer actions (currently file attachments)."""
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type != FILE_ACTION_TYPE:
            log_warning(
                f"[fluxer_interface] execute_action: unknown action_type={action_type}"
            )
            return {"status": "failed", "message": f"Unknown action {action_type}"}

        if not self.is_enabled or not self._rest:
            return {
                "status": "failed",
                "message": "Fluxer interface is disabled or not initialized",
            }

        from core.outbound_file_utils import (
            classify_media,
            guess_mime_type,
            resolve_safe_outbound_path,
        )

        raw_path = payload.get("path")
        caption = payload.get("caption")
        channel_id = self._resolve_channel_id(payload)

        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"status": "failed", "message": "payload.path must be a string"}
        if not channel_id:
            return {
                "status": "failed",
                "message": "payload.channel_id or payload.interface_path is required",
            }
        caption = caption if isinstance(caption, str) else None

        resolved, err = resolve_safe_outbound_path(raw_path)
        if err or resolved is None:
            log_warning(f"[fluxer_interface] Rejected file path {raw_path!r}: {err}")
            return {"status": "failed", "message": err or "Invalid path"}

        mime_type = guess_mime_type(resolved)
        # classify_media is used for logging/future per-kind handling; Fluxer
        # renders media inline from the uploaded attachment automatically.
        kind = classify_media(resolved)

        result = await self._rest.send_file(
            channel_id,
            resolved,
            caption=caption,
            mime_type=mime_type,
        )
        if result is None:
            return {"status": "failed", "message": "File upload failed"}

        log_info(f"[fluxer_interface] Sent file ({kind}) to channel {channel_id}")
        return {"status": "success"}

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self._self_user_id, username=None)

    async def reload_from_config(self) -> None:
        """Reload runtime configuration and (re)connect if the token changed."""
        try:
            log_info("[fluxer_interface] Applying configuration changes from registry")
            await config_registry.load_all_from_db()

            new_token = str(FLUXER_TOKEN).strip() if FLUXER_TOKEN else ""
            new_api_base = _resolve_api_base(
                str(FLUXER_API_BASE_URL), FLUXER_API_VERSION
            )
            new_gateway = str(FLUXER_GATEWAY_URL).strip()

            token_changed = new_token != self.token
            endpoint_changed = (
                new_api_base != self.api_base or new_gateway != self.gateway_url
            )

            self.api_base = new_api_base
            self.gateway_url = new_gateway

            if token_changed or endpoint_changed:
                await self.stop()
                self.token = new_token
                self._self_user_id = None
                if new_token and aiohttp is not None:
                    self.is_enabled = True
                    FluxerInterface._current_instance_enabled = True
                    self._rest = _FluxerRestClient(self.token, self.api_base)
                    await self.start()
                    log_info("[fluxer_interface] Reconnected after config change")
                else:
                    log_info(
                        "[fluxer_interface] Token removed — interface idle until "
                        "reconfigured"
                    )
        except Exception as exc:
            log_error(f"[fluxer_interface] reload_from_config failed: {exc}")


# ----------------------------------------------------------------------
# Configuration via registry

register_exposed_var(
    "FLUXER_TOKEN",
    label="Fluxer Bot Token",
    default="",
    value_type=str,
    ui_type="password",
    description=(
        "Authentication token for the Fluxer bot. Bot tokens are prefixed with "
        "'Bot ' automatically for REST requests; user tokens (flx_...) are used "
        "as-is. Get it from Settings > Developer > Applications on your Fluxer "
        "instance."
    ),
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component="fluxer_bot",
)

register_exposed_var(
    "FLUXER_API_BASE_URL",
    label="Fluxer API Base URL",
    default="https://api.fluxer.app/v{v}",
    value_type=str,
    ui_type="string",
    description=(
        "Base URL of the Fluxer REST API. Use '{v}' as the API version "
        "placeholder. Change this only for a self-hosted Fluxer instance or a "
        "proxy."
    ),
    scope="interface",
    needs_component_reload=True,
    component="fluxer_bot",
)

register_exposed_var(
    "FLUXER_GATEWAY_URL",
    label="Fluxer Gateway URL",
    default="wss://gateway.fluxer.app/?v=1&encoding=json",
    value_type=str,
    ui_type="string",
    description=(
        "WebSocket gateway URL for real-time events. Must use JSON encoding. "
        "Change this only for a self-hosted Fluxer instance."
    ),
    scope="interface",
    needs_component_reload=True,
    component="fluxer_bot",
)

register_exposed_var(
    "FLUXER_API_VERSION",
    label="Fluxer API Version",
    default=1,
    value_type=int,
    ui_type="number",
    description="API version number substituted into the API base URL '{v}'.",
    scope="interface",
    needs_component_reload=True,
    component="fluxer_bot",
)

FLUXER_TOKEN = config_registry.get_var(
    "FLUXER_TOKEN",
    "",
    label="Fluxer Bot Token",
    description="Authentication token for the Fluxer bot.",
    group="interface",
    component="fluxer_bot",
    sensitive=True,
)

FLUXER_API_BASE_URL = config_registry.get_var(
    "FLUXER_API_BASE_URL",
    "https://api.fluxer.app/v{v}",
    label="Fluxer API Base URL",
    description="Base URL of the Fluxer REST API ('{v}' = version placeholder).",
    group="interface",
    component="fluxer_bot",
)

FLUXER_GATEWAY_URL = config_registry.get_var(
    "FLUXER_GATEWAY_URL",
    "wss://gateway.fluxer.app/?v=1&encoding=json",
    label="Fluxer Gateway URL",
    description="WebSocket gateway URL for real-time events.",
    group="interface",
    component="fluxer_bot",
)

FLUXER_API_VERSION = config_registry.get_var(
    "FLUXER_API_VERSION",
    1,
    label="Fluxer API Version",
    description="API version number substituted into the API base URL.",
    group="interface",
    component="fluxer_bot",
)

FLUXER_TRAINER_ID: int | str | None = core_get_trainer_id(INTERFACE_NAME)

# Always instantiate so the interface is present even when disabled.
FLUXER_INTERFACE_INSTANCE = FluxerInterface(
    str(FLUXER_TOKEN) if FLUXER_TOKEN else "",
    api_base=str(FLUXER_API_BASE_URL),
    gateway_url=str(FLUXER_GATEWAY_URL),
    api_version=int(FLUXER_API_VERSION) if FLUXER_API_VERSION else 1,
    trainer_id=FLUXER_TRAINER_ID,
)


# ------------------------------------------------------------------
# Runtime reload / config listeners
async def reload_interface() -> None:
    """Reload the Fluxer interface when component-level config changes.

    Discovered by core_initializer and registered as the component reload
    handler so ``needs_component_reload`` flips trigger a runtime reload.
    """
    try:
        log_info("[fluxer_interface] Reload handler invoked - reloading from config")
        await FLUXER_INTERFACE_INSTANCE.reload_from_config()
    except Exception as exc:
        log_error(f"[fluxer_interface] Failed to reload interface: {exc}")


def _schedule_instance_reload(_new_value: Any = None) -> None:
    """Schedule an async reload of the active interface instance."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(FLUXER_INTERFACE_INSTANCE.reload_from_config())
    except RuntimeError:
        log_debug(
            "[fluxer_interface] Reload requested but no running loop; will apply on "
            "next start"
        )


for _key in (
    "FLUXER_TOKEN",
    "FLUXER_API_BASE_URL",
    "FLUXER_GATEWAY_URL",
    "FLUXER_API_VERSION",
):
    try:
        config_registry.add_listener(_key, _schedule_instance_reload)
    except Exception:  # pragma: no cover - best-effort listener wiring
        pass


INTERFACE_CLASS = FluxerInterface
