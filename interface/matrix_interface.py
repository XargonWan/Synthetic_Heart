# interface/matrix_interface.py

"""Matrix chat interface for SyntH.

Provides optional support for interacting with Matrix rooms using matrix-nio.
The interface registers itself even when credentials are missing so that UI
layers can surface the integration and prompt the operator for configuration.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from core import message_queue
from core.command_registry import execute_command
from core.core_initializer import register_interface
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.config_manager import config_registry
from core.config import get_trainer_id as core_get_trainer_id
from plugins.chat_link import ChatLinkStore

load_dotenv()

try:  # pragma: no cover - optional dependency
    from nio import (  # type: ignore
        AsyncClient,
        AsyncClientConfig,
        InviteMemberEvent,
        LoginResponse,
        MatrixRoom,
        RoomMessageNotice,
        RoomMessageText,
    )
    from nio.exceptions import LocalProtocolError  # type: ignore
except Exception:  # pragma: no cover - dependency missing
    AsyncClient = None  # type: ignore
    AsyncClientConfig = None  # type: ignore
    InviteMemberEvent = None  # type: ignore
    LoginResponse = None  # type: ignore
    MatrixRoom = None  # type: ignore
    RoomMessageNotice = None  # type: ignore
    RoomMessageText = None  # type: ignore
    LocalProtocolError = Exception  # type: ignore

INTERFACE_NAME = "matrix_chat"
ACTION_TYPE = "message_matrix_chat"

chat_link_store = ChatLinkStore()
_interface_registry = get_interface_registry()
context_memory: Dict[str, deque[str]] = {}


def _parse_allowed_rooms(raw: Optional[str]) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _extract_username(mxid: str) -> str:
    if ":" in mxid:
        return mxid.split(":")[0].lstrip("@")
    return mxid.lstrip("@")


class MatrixInterface:
    """Matrix chat interface wrapping matrix-nio."""
    
    display_name = "Matrix Interface"

    _current_instance_enabled: bool = False

    def __init__(
        self,
        homeserver: Optional[str],
        user_id: Optional[str],
        *,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        device_id: Optional[str] = None,
        device_name: Optional[str] = None,
        store_path: Optional[str] = None,
        allowed_rooms: Optional[List[str]] = None,
        trainer_id: Optional[int] = None,
    ):
        self.homeserver = str(homeserver or "").rstrip("/")
        self.user_id = str(user_id or "")
        self.password = password
        self.access_token = access_token
        self.device_id = device_id
        self.device_name = str(device_name or "SyntH Matrix Interface")
        self.store_path = store_path
        self.allowed_rooms = set(allowed_rooms or [])
        self.trainer_id = trainer_id

        self.is_enabled = True
        self.disabled_reason: Optional[str] = None
        self.username = _extract_username(self.user_id) if self.user_id else None

        self.client: Optional["AsyncClient"] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._sync_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._logged_in = False

        # Gatekeeping: determine whether the interface can be activated
        if AsyncClient is None:
            self._disable("matrix-nio is not installed")
        elif not self.homeserver or not self.user_id:
            self._disable("MATRIX_HOMESERVER or MATRIX_USER missing")
        elif not self.password and not self.access_token:
            self._disable("MATRIX_PASSWORD or MATRIX_ACCESS_TOKEN missing")

        if self.is_enabled:
            config = AsyncClientConfig(store_sync_tokens=True) if AsyncClientConfig else None
            self.client = AsyncClient(
                self.homeserver,
                self.user_id,
                device_id=self.device_id,
                store_path=self.store_path,
                config=config,
            )

            if self.access_token:
                self.client.access_token = self.access_token
                self.client.user_id = self.user_id
                if self.device_id:
                    self.client.device_id = self.device_id
                self._logged_in = True
            else:
                self._logged_in = False

            async def _resolver(room_id: int | str, thread_id: Optional[int | str], bot_instance: Any = None) -> Dict[str, Optional[str]]:
                instance = bot_instance if isinstance(bot_instance, MatrixInterface) else self
                client = getattr(instance, "client", None)
                chat_name = None
                if client and hasattr(client, "rooms"):
                    room = client.rooms.get(str(room_id))
                    if room:
                        chat_name = getattr(room, "display_name", None) or getattr(room, "canonical_alias", None) or room.room_id
                return {"chat_name": chat_name, "message_thread_name": None}

            ChatLinkStore.set_name_resolver(INTERFACE_NAME, _resolver)

            if self.client and RoomMessageText and hasattr(self.client, "add_event_callback"):
                self.client.add_event_callback(self._on_message, RoomMessageText)
            if self.client and RoomMessageNotice and hasattr(self.client, "add_event_callback"):
                self.client.add_event_callback(self._on_message, RoomMessageNotice)
            if self.client and InviteMemberEvent and hasattr(self.client, "add_event_callback"):
                self.client.add_event_callback(self._on_invite, InviteMemberEvent)

        MatrixInterface._current_instance_enabled = self.is_enabled

        register_interface(INTERFACE_NAME, self)
        _interface_registry.register_interface(INTERFACE_NAME, self)
        if self.trainer_id is not None:
            _interface_registry.set_trainer_id(INTERFACE_NAME, self.trainer_id)

        if self.is_enabled:
            log_info("[matrix_interface] Matrix interface registered")
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.start())
            except RuntimeError:
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon(lambda: asyncio.create_task(self.start()))
                except Exception:
                    log_debug("[matrix_interface] No event loop available; Matrix interface will start during application initialization")
        else:
            reason = self.disabled_reason or "missing configuration"
            log_warning(f"[matrix_interface] Interface loaded in disabled state: {reason}")

    def _disable(self, reason: str) -> None:
        self.is_enabled = False
        self.disabled_reason = reason
        MatrixInterface._current_instance_enabled = False

    # ------------------------------------------------------------------
    # Registration metadata
    @staticmethod
    def get_interface_id() -> str:
        return INTERFACE_NAME

    @staticmethod
    def get_action_types() -> List[str]:
        return [ACTION_TYPE] if MatrixInterface._current_instance_enabled else []

    @staticmethod
    def get_supported_actions() -> Dict[str, Dict[str, Any]]:
        if not MatrixInterface._current_instance_enabled:
            return {}
        return {
            ACTION_TYPE: {
                "description": "Send a text message to a Matrix room.",
                "required_fields": ["text", "target"],
                "optional_fields": ["reply_to_event_id", "thread_event_id"],
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if action_name != ACTION_TYPE or not MatrixInterface._current_instance_enabled:
            return {}
        return {
            "description": "Send a message to a Matrix room using its room ID or alias.",
            "payload": {
                "text": {
                    "type": "string",
                    "example": "Hello Matrix!",
                    "description": "Content of the message.",
                },
                "target": {
                    "type": "string",
                    "example": "!abcdefg:example.org",
                    "description": "Matrix room_id or room alias where the message should be delivered.",
                },
                "reply_to_event_id": {
                    "type": "string",
                    "description": "Optional event ID to reply to.",
                    "optional": True,
                },
                "thread_event_id": {
                    "type": "string",
                    "description": "Optional thread root event ID.",
                    "optional": True,
                },
            },
        }

    @staticmethod
    def validate_payload(action_type: str, payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if action_type != ACTION_TYPE:
            return errors
        if not MatrixInterface._current_instance_enabled:
            errors.append("Matrix interface is disabled - configure credentials to enable messaging")
            return errors
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append("payload.text must be a non-empty string")
        target = payload.get("target") or payload.get("room_id")
        if not isinstance(target, str) or not target.strip():
            errors.append("payload.target must be a room_id or alias string")
        reply_to = payload.get("reply_to_event_id")
        if reply_to is not None and not isinstance(reply_to, str):
            errors.append("payload.reply_to_event_id must be a string")
        thread_event = payload.get("thread_event_id")
        if thread_event is not None and not isinstance(thread_event, str):
            errors.append("payload.thread_event_id must be a string")
        return errors

    # ------------------------------------------------------------------
    # Lifecycle management
    async def start(self) -> None:
        if not self.is_enabled:
            log_debug("[matrix_interface] start() skipped - interface disabled")
            return
        if not self.client:
            log_debug("[matrix_interface] Start skipped - client not initialized")
            return

        await message_queue.run()

        async with self._sync_lock:
            if self._sync_task and not self._sync_task.done():
                return
            self._sync_task = asyncio.create_task(self._sync_loop())
            log_info("[matrix_interface] Sync loop scheduled")

    async def stop(self) -> None:
        if not self.is_enabled or not self.client:
            return
        self._stop.set()
        if self._sync_task:
            await self._sync_task
        try:
            await self.client.close()
        except Exception:
            pass

    async def _sync_loop(self) -> None:
        if not self.is_enabled or not self.client:
            return
        try:
            await self._ensure_login()
        except Exception as exc:
            log_error(f"[matrix_interface] Failed to login: {exc}")
            return

        while not self._stop.is_set():
            try:
                await self.client.sync(timeout=30000, full_state=False)
            except Exception as exc:  # pragma: no cover - network failure
                log_error(f"[matrix_interface] Sync error: {exc}")
                await asyncio.sleep(5)

    async def _ensure_login(self) -> None:
        if not self.is_enabled or not self.client:
            raise RuntimeError("Matrix interface is disabled or uninitialized")
        if self._logged_in:
            return
        if not self.password:
            raise RuntimeError("Matrix password not configured")
        try:
            response = await self.client.login(self.password, device_name=self.device_name)
        except LocalProtocolError as exc:  # pragma: no cover - protocol mismatch
            log_error(f"[matrix_interface] Login protocol error: {exc}")
            raise
        except Exception as exc:  # pragma: no cover - network failure
            log_error(f"[matrix_interface] Login error: {exc}")
            raise

        if isinstance(response, LoginResponse):
            self._logged_in = True
            self.client.user_id = response.user_id or self.user_id
            if response.device_id:
                self.client.device_id = response.device_id
            self.username = _extract_username(self.client.user_id)
            log_info(f"[matrix_interface] Logged in as {self.client.user_id}")
        else:  # pragma: no cover - unexpected response
            raise RuntimeError(f"Unexpected login response: {response}")

    # ------------------------------------------------------------------
    # Matrix event handlers
    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:  # pragma: no cover - invite flow requires live homeserver
        if not self.is_enabled or not self.client or not event:
            return
        try:
            if getattr(event, "membership", "") == "invite":
                await self.client.join(room.room_id)
                log_info(f"[matrix_interface] Auto-joined room {room.room_id}")
        except Exception as exc:
            log_warning(f"[matrix_interface] Failed to join room {getattr(room, 'room_id', '<unknown>')}: {exc}")

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText | RoomMessageNotice) -> None:
        if not self.is_enabled or not room or not event:
            return
        if getattr(event, "sender", None) == getattr(self.client, "user_id", self.user_id):
            return

        text = getattr(event, "body", "") or ""
        if not text.strip():
            return

        room_identifier = getattr(room, "room_id", None)
        if not room_identifier:
            return

        if self.allowed_rooms and room_identifier not in self.allowed_rooms and getattr(room, "canonical_alias", None) not in self.allowed_rooms:
            log_debug(f"[matrix_interface] Ignoring message from room {room_identifier} (not in allow list)")
            return

        chat_type = "group"
        member_count = getattr(room, "member_count", None)
        if member_count is not None and member_count <= 2:
            chat_type = "private"

        timestamp_ms = getattr(event, "server_timestamp", None)
        if timestamp_ms is not None:
            date = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            date = datetime.now(tz=timezone.utc)

        history = context_memory.setdefault(room_identifier, deque(maxlen=50))
        history.append(text)

        source = getattr(event, "source", {}) or {}
        content = source.get("content", {})
        relates_to = content.get("m.relates_to", {}) if isinstance(content, dict) else {}
        reply_payload = relates_to.get("m.in_reply_to", {}) if isinstance(relates_to, dict) else {}
        reply_event_id = reply_payload.get("event_id")

        reply_to_message = None
        if reply_event_id:
            reply_to_message = SimpleNamespace(
                message_id=reply_event_id,
                text=None,
                caption=None,
                date=None,
                from_user=SimpleNamespace(id=None, username=None, full_name=None),
            )

        room_name = getattr(room, "display_name", None) or getattr(room, "canonical_alias", None)

        wrapped = SimpleNamespace(
            message_id=getattr(event, "event_id", None),
            chat_id=room_identifier,
            text=text,
            caption=None,
            date=date,
            thread_id=relates_to.get("event_id") if isinstance(relates_to, dict) else None,
            from_user=SimpleNamespace(
                id=getattr(event, "sender", None),
                username=_extract_username(getattr(event, "sender", "")),
                full_name=None,
            ),
            chat=SimpleNamespace(
                id=room_identifier,
                type=chat_type,
                title=room_name or room_identifier,
                username=getattr(room, "canonical_alias", None),
                first_name=None,
                human_count=member_count,
            ),
            entities=None,
            reply_to_message=reply_to_message,
        )

        try:
            await chat_link_store.update_names_from_resolver(
                room_identifier,
                None,
                interface=INTERFACE_NAME,
                bot=self,
            )
        except Exception as exc:
            log_warning(f"[matrix_interface] Failed to update chat link names: {exc}")

        if text.startswith("/"):
            parts = text[1:].split()
            if parts:
                command, *args = parts
                try:
                    response = await execute_command(command, *args)
                    if response:
                        await self._send_matrix_message(room_identifier, response)
                except Exception as exc:  # pragma: no cover - command failure
                    log_error(f"[matrix_interface] Command {command} failed: {exc}")
            return

        await message_queue.enqueue(self, wrapped, context_memory, interface_id=INTERFACE_NAME)

    # ------------------------------------------------------------------
    # Messaging helpers
    async def send_message(self, room_id: Optional[str] = None, text: Optional[str] = None, **kwargs) -> None:
        if not self.is_enabled:
            log_warning("[matrix_interface] Cannot send message - interface is disabled")
            return
        if isinstance(room_id, dict):
            payload = room_id
            text = payload.get("text", text)
            room_id = payload.get("target") or payload.get("room_id")
            reply_to_event_id = payload.get("reply_to_event_id")
            thread_event_id = payload.get("thread_event_id") or payload.get("thread_id")
        else:
            payload = kwargs
            room_id = room_id or payload.get("target") or payload.get("room_id")
            reply_to_event_id = payload.get("reply_to_event_id")
            thread_event_id = payload.get("thread_event_id") or payload.get("thread_id")

        if not self.client:
            log_warning("[matrix_interface] Cannot send message - client not initialized")
            return
        if not room_id:
            log_warning("[matrix_interface] Cannot send message - room_id missing")
            return
        if not text:
            log_warning("[matrix_interface] Cannot send message - text missing")
            return

        await self._ensure_login()

        if room_id.startswith("#"):
            try:
                response = await self.client.room_resolve_alias(room_id)
                if hasattr(response, "room_id"):
                    room_id = response.room_id
            except Exception as exc:
                log_warning(f"[matrix_interface] Failed to resolve alias {room_id}: {exc}")

        await self._send_matrix_message(
            room_id,
            text,
            reply_to_event_id=reply_to_event_id,
            thread_event_id=thread_event_id,
        )

    async def _send_matrix_message(
        self,
        room_id: str,
        text: str,
        *,
        reply_to_event_id: Optional[str] = None,
        thread_event_id: Optional[str] = None,
    ) -> None:
        if not self.client:
            return
        content: Dict[str, Any] = {
            "msgtype": "m.text",
            "body": text,
        }

        relates_to: Dict[str, Any] = {}
        if thread_event_id:
            relates_to.update(
                {
                    "event_id": thread_event_id,
                    "rel_type": "m.thread",
                    "is_falling_back": True,
                }
            )
        if reply_to_event_id:
            relates_to.setdefault("m.in_reply_to", {"event_id": reply_to_event_id})

        if relates_to:
            content["m.relates_to"] = relates_to

        try:
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            log_debug(f"[matrix_interface] Message sent to {room_id}")
        except Exception as exc:  # pragma: no cover - network failure
            log_error(f"[matrix_interface] Failed to send message to {room_id}: {exc}")

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.user_id, username=self.username)


# ----------------------------------------------------------------------
# Configuration via registry

MATRIX_HOMESERVER = config_registry.get_var(
    "MATRIX_HOMESERVER",
    "https://matrix.org/homeserver",
    label="Matrix Homeserver",
    description="Base URL of the Matrix homeserver (e.g. https://matrix.org/homeserver)",
    group="interface",
    component="matrix_chat",
)

MATRIX_USER = config_registry.get_var(
    "MATRIX_USER",
    "",
    label="Matrix User ID",
    description="Matrix MXID used by the bot (e.g. @yoursynth:matrix.org).",
    group="interface",
    component="matrix_chat",
)

MATRIX_PASSWORD = config_registry.get_var(
    "MATRIX_PASSWORD",
    None,
    label="Matrix Password",
    description="Password used when logging into the homeserver (ignored if access token is provided).",
    group="interface",
    component="matrix_chat",
    sensitive=True,
)

MATRIX_ACCESS_TOKEN = config_registry.get_var(
    "MATRIX_ACCESS_TOKEN",
    None,
    label="Matrix Access Token",
    description="Optional long-lived access token used instead of password-based login.",
    group="interface",
    component="matrix_chat",
    sensitive=True,
)

MATRIX_DEVICE_ID = config_registry.get_var(
    "MATRIX_DEVICE_ID",
    None,
    label="Matrix Device ID",
    description="Device identifier to reuse when establishing a session.",
    group="interface",
    component="matrix_chat",
)

MATRIX_DEVICE_NAME = config_registry.get_var(
    "MATRIX_DEVICE_NAME",
    "SyntH",
    label="Matrix Device Name",
    description="Human readable name for the device registered on the homeserver.",
    group="interface",
    component="matrix_chat",
)

MATRIX_STORE_PATH = config_registry.get_var(
    "MATRIX_STORE_PATH",
    None,
    label="Matrix Store Path",
    description="Filesystem path where the Matrix client should store sync data.",
    group="interface",
    component="matrix_chat",
    tags=["bootstrap"],  # Hidden from UI - managed automatically
)

_MATRIX_ALLOWED_ROOMS_RAW = config_registry.get_var(
    "MATRIX_ALLOWED_ROOMS",
    "",
    label="Matrix Allowed Rooms",
    description="Comma separated list of room IDs the bot is allowed to respond in. Leave empty to allow all rooms.",
    group="interface",
    component="matrix_chat",
)

def get_matrix_allowed_rooms() -> set[str]:
    """Parse and return the current allowed rooms as a set."""
    return _parse_allowed_rooms(str(_MATRIX_ALLOWED_ROOMS_RAW))

MATRIX_TRAINER_ID: Optional[int] = core_get_trainer_id(INTERFACE_NAME)

# Always instantiate so that the interface is present even when disabled
MATRIX_INTERFACE_INSTANCE = MatrixInterface(
    MATRIX_HOMESERVER,
    MATRIX_USER,
    password=MATRIX_PASSWORD,
    access_token=MATRIX_ACCESS_TOKEN,
    device_id=MATRIX_DEVICE_ID,
    device_name=MATRIX_DEVICE_NAME,
    store_path=MATRIX_STORE_PATH,
    allowed_rooms=list(get_matrix_allowed_rooms()) if get_matrix_allowed_rooms() else None,
    trainer_id=MATRIX_TRAINER_ID,
)
