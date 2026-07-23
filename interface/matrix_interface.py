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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from core import message_queue
from core.command_registry import handle_command_message
from core.core_initializer import register_interface
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.config_manager import config_registry
from core.config import get_trainer_id as core_get_trainer_id
from core.interface_paths import resolve_and_touch, set_name_resolver
from core.variables_engine import register_exposed_var

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
FILE_ACTION_TYPE = "send_file_matrix_chat"

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
        auto_join: Optional[bool] = None,
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
        self.auto_join = (
            bool(auto_join) if auto_join is not None else bool(MATRIX_AUTO_JOIN)
        )
        self.trainer_id = trainer_id

        self.is_enabled = True
        self.disabled_reason: Optional[str] = None
        self.username = _extract_username(self.user_id) if self.user_id else None

        self.client: Optional["AsyncClient"] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._sync_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._logged_in = False

        # Policies (read from module-level config defaults)
        self.private_message_policy = str(MATRIX_PRIVATE_MESSAGES).lower()
        self.invite_policy = str(MATRIX_INVITE_POLICY).lower()

        # Gatekeeping: determine whether the interface can be activated
        if AsyncClient is None:
            self._disable("matrix-nio is not installed")
        elif not self.homeserver or not self.user_id:
            self._disable("MATRIX_HOMESERVER or MATRIX_USER missing")
        else:
            # Allow the interface to be present in the UI even when credentials are
            # not yet provided. Authentication (password or access token) is
            # required to start the sync loop; we won't attempt to auto-start
            # syncing until credentials are available.
            if not self.password and not self.access_token:
                log_warning(
                    "[matrix_interface] No MATRIX_PASSWORD or MATRIX_ACCESS_TOKEN configured — interface will be available in UI but will not sync until credentials are provided"
                )

        # Track whether we have credentials available for login/sync
        self._auth_configured = bool(self.password or self.access_token)

        if self.is_enabled:
            config = (
                AsyncClientConfig(store_sync_tokens=True) if AsyncClientConfig else None
            )
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

            async def _resolver(
                room_id: int | str,
                thread_id: Optional[int | str],
                bot_instance: Any = None,
            ) -> Dict[str, Optional[str]]:
                instance = (
                    bot_instance if isinstance(bot_instance, MatrixInterface) else self
                )
                client = getattr(instance, "client", None)
                chat_name = None
                if client and hasattr(client, "rooms"):
                    room = client.rooms.get(str(room_id))
                    if room:
                        chat_name = (
                            getattr(room, "display_name", None)
                            or getattr(room, "canonical_alias", None)
                            or room.room_id
                        )
                return {"chat_name": chat_name, "message_thread_name": None}

            set_name_resolver(INTERFACE_NAME, _resolver)

            if (
                self.client
                and RoomMessageText
                and hasattr(self.client, "add_event_callback")
            ):
                self.client.add_event_callback(self._on_message, RoomMessageText)
            if (
                self.client
                and RoomMessageNotice
                and hasattr(self.client, "add_event_callback")
            ):
                self.client.add_event_callback(self._on_message, RoomMessageNotice)
            if (
                self.client
                and InviteMemberEvent
                and hasattr(self.client, "add_event_callback")
            ):
                self.client.add_event_callback(self._on_invite, InviteMemberEvent)

        MatrixInterface._current_instance_enabled = self.is_enabled

        register_interface(INTERFACE_NAME, self)
        _interface_registry.register_interface(INTERFACE_NAME, self)
        if self.trainer_id is not None:
            _interface_registry.set_trainer_id(INTERFACE_NAME, self.trainer_id)

        if self.is_enabled:
            log_info("[matrix_interface] Matrix interface registered")
            if self._auth_configured:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.start())
                except RuntimeError:
                    try:
                        loop = asyncio.get_event_loop()
                        loop.call_soon(lambda: asyncio.create_task(self.start()))
                    except Exception:
                        log_debug(
                            "[matrix_interface] No event loop available; Matrix interface will start during application initialization"
                        )
            else:
                log_info(
                    "[matrix_interface] Interface initialized but not authenticated — provide MATRIX_PASSWORD or MATRIX_ACCESS_TOKEN to enable syncing"
                )
        else:
            reason = self.disabled_reason or "missing configuration"
            log_warning(
                f"[matrix_interface] Interface loaded in disabled state: {reason}"
            )

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
        if not MatrixInterface._current_instance_enabled:
            return []
        return [ACTION_TYPE, FILE_ACTION_TYPE]

    @staticmethod
    def get_supported_actions() -> Dict[str, Dict[str, Any]]:
        if not MatrixInterface._current_instance_enabled:
            return {}
        return {
            ACTION_TYPE: {
                "description": "Send a text message to a Matrix room.",
                "required_fields": ["text", "target"],
                "optional_fields": ["reply_to_event_id", "thread_event_id"],
            },
            FILE_ACTION_TYPE: {
                "description": (
                    "Send a file attachment (image, video, audio or document) to a "
                    "Matrix room. The media kind is auto-detected from the file and "
                    "delivered so the recipient can view/play it natively. The file "
                    "must live inside Synth's filesystem sandbox."
                ),
                "required_fields": ["path", "target"],
                "optional_fields": ["caption", "thread_event_id"],
                "security_level": "medium",
                "external_effects": ["filesystem"],
            },
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> Dict[str, Any]:
        if not MatrixInterface._current_instance_enabled:
            return {}
        if action_name == ACTION_TYPE:
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
        if action_name == FILE_ACTION_TYPE:
            return {
                "description": (
                    "Send a file attachment to a Matrix room. The media kind "
                    "(image, video, audio, document) is auto-detected."
                ),
                "payload": {
                    "path": {
                        "type": "string",
                        "example": "/app/data/report.pdf",
                        "description": "Path to the file to send. Must be inside Synth's filesystem sandbox.",
                    },
                    "target": {
                        "type": "string",
                        "example": "!abcdefg:example.org",
                        "description": "Matrix room_id or room alias where the file should be delivered.",
                    },
                    "caption": {
                        "type": "string",
                        "example": "Here is the report you asked for",
                        "description": "Optional text sent alongside the file.",
                        "optional": True,
                    },
                    "thread_event_id": {
                        "type": "string",
                        "description": "Optional thread root event ID.",
                        "optional": True,
                    },
                },
            }
        return {}

    @staticmethod
    def validate_payload(action_type: str, payload: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if action_type not in (ACTION_TYPE, FILE_ACTION_TYPE):
            return errors
        if not MatrixInterface._current_instance_enabled:
            errors.append(
                "Matrix interface is disabled - configure credentials to enable messaging"
            )
            return errors

        target = payload.get("target") or payload.get("room_id")
        if not isinstance(target, str) or not target.strip():
            errors.append("payload.target must be a room_id or alias string")
        thread_event = payload.get("thread_event_id")
        if thread_event is not None and not isinstance(thread_event, str):
            errors.append("payload.thread_event_id must be a string")

        if action_type == ACTION_TYPE:
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                errors.append("payload.text must be a non-empty string")
            reply_to = payload.get("reply_to_event_id")
            if reply_to is not None and not isinstance(reply_to, str):
                errors.append("payload.reply_to_event_id must be a string")
        elif action_type == FILE_ACTION_TYPE:
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                errors.append("payload.path must be a non-empty string")
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
        if not self._auth_configured and not self._logged_in:
            log_debug(
                "[matrix_interface] start() skipped - no credentials configured for login"
            )
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
            response = await self.client.login(
                self.password, device_name=self.device_name
            )
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
    async def _on_invite(
        self, room: MatrixRoom, event: InviteMemberEvent
    ) -> None:  # pragma: no cover - invite flow requires live homeserver
        if not self.is_enabled or not self.client or not event:
            return
        try:
            if getattr(event, "membership", "") == "invite":
                inviter = getattr(event, "sender", None)
                if not getattr(self, "auto_join", True):
                    log_debug(
                        f"[matrix_interface] Auto-join disabled; received invite for {room.room_id} from {inviter}"
                    )
                    return

                # Invite policy: allow_all vs trainer_only
                invite_policy = getattr(self, "invite_policy", "trainer_only")
                allowed = False
                if str(invite_policy).lower() == "allow_all":
                    allowed = True
                else:
                    try:
                        if inviter and _interface_registry.is_trainer(
                            INTERFACE_NAME, inviter
                        ):
                            allowed = True
                    except Exception:
                        allowed = False
                    if not allowed:
                        try:
                            if inviter in get_matrix_trusted_users():
                                allowed = True
                        except Exception:
                            allowed = False

                if allowed:
                    await self.client.join(room.room_id)
                    log_info(f"[matrix_interface] Auto-joined room {room.room_id}")
                else:
                    log_debug(
                        f"[matrix_interface] Invite from {inviter} ignored by invite_policy={invite_policy}"
                    )
        except Exception as exc:
            log_warning(
                f"[matrix_interface] Failed to join room {getattr(room, 'room_id', '<unknown>')}: {exc}"
            )

    async def _on_message(
        self, room: MatrixRoom, event: RoomMessageText | RoomMessageNotice
    ) -> None:
        if not self.is_enabled or not room or not event:
            return
        if getattr(event, "sender", None) == getattr(
            self.client, "user_id", self.user_id
        ):
            return

        text = getattr(event, "body", "") or ""
        if not text.strip():
            return

        room_identifier = getattr(room, "room_id", None)
        if not room_identifier:
            return

        if (
            self.allowed_rooms
            and room_identifier not in self.allowed_rooms
            and getattr(room, "canonical_alias", None) not in self.allowed_rooms
        ):
            log_debug(
                f"[matrix_interface] Ignoring message from room {room_identifier} (not in allow list)"
            )
            return

        chat_type = "group"
        member_count = getattr(room, "member_count", None)
        if member_count is not None and member_count <= 2:
            chat_type = "private"

        # Enforce private-message policy if configured
        if chat_type == "private":
            pm_policy = str(
                getattr(self, "private_message_policy", "trainer_only")
            ).lower()
            if pm_policy == "trainer_only":
                sender = getattr(event, "sender", None)
                allowed_pm = False
                # 1) trainer configured for interface
                try:
                    if sender and _interface_registry.is_trainer(
                        INTERFACE_NAME, sender
                    ):
                        allowed_pm = True
                except Exception:
                    allowed_pm = False
                # 2) trusted users list
                if not allowed_pm:
                    try:
                        if sender in get_matrix_trusted_users():
                            allowed_pm = True
                    except Exception:
                        allowed_pm = False
                if not allowed_pm:
                    log_debug(
                        f"[matrix_interface] Ignoring private message from {sender} due to MATRIX_PRIVATE_MESSAGES=trainer_only"
                    )
                    return

        timestamp_ms = getattr(event, "server_timestamp", None)
        if timestamp_ms is not None:
            date = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        else:
            date = datetime.now(tz=timezone.utc)

        # Build interface_path for Matrix
        from core.interface_path_utils import build_interface_path

        source = getattr(event, "source", {}) or {}
        content = source.get("content", {})
        relates_to = (
            content.get("m.relates_to", {}) if isinstance(content, dict) else {}
        )
        thread_event_id = (
            relates_to.get("event_id") if isinstance(relates_to, dict) else None
        )

        # Matrix: matrix/room_id/event_id (if threaded)
        interface_path = build_interface_path(
            "matrix", room_identifier, thread_event_id if thread_event_id else None
        )
        log_debug(f"[matrix_interface] Generated interface_path: {interface_path}")

        # Track context using centralized manager
        # NOTE: chat activity tracking is now centralized in chat_context_manager.add_message_to_context
        from core.chat_context_manager import add_message_to_context

        try:
            await add_message_to_context(
                interface_path=interface_path,
                message_text=text,
                sender_name=_extract_username(getattr(event, "sender", "")),
                sender_id=getattr(event, "sender", "unknown"),
                message_id=getattr(event, "event_id", None),
                timestamp=date.isoformat() if date else None,
            )
        except Exception as e:
            log_warning(f"[matrix_interface] Failed to add message to context: {e}")

        reply_payload = (
            relates_to.get("m.in_reply_to", {}) if isinstance(relates_to, dict) else {}
        )
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

        room_name = getattr(room, "display_name", None) or getattr(
            room, "canonical_alias", None
        )

        wrapped = SimpleNamespace(
            message_id=getattr(event, "event_id", None),
            chat_id=room_identifier,
            interface_path=interface_path,  # Add interface_path to message
            text=text,
            caption=None,
            date=date,
            thread_id=thread_event_id,
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
            await resolve_and_touch(
                f"{INTERFACE_NAME}/{room_identifier}",
                room_identifier,
                None,
                bot=self,
            )
        except Exception as exc:
            log_warning(f"[matrix_interface] Failed to update chat link names: {exc}")

        if text.startswith("/"):
            try:
                # Route through the shared handler so trainer permission checks
                # (is_trainer) are enforced consistently with other interfaces.
                response = await handle_command_message(
                    text,
                    user_id=getattr(event, "sender", None),
                    interface_id=INTERFACE_NAME,
                    interface_context={"bot": self, "wrapped": wrapped},
                )
                if response:
                    await self._send_matrix_message(room_identifier, response)
            except Exception as exc:  # pragma: no cover - command failure
                log_error(f"[matrix_interface] Command failed: {exc}")
            return

        await message_queue.enqueue(self, wrapped, interface_id=INTERFACE_NAME)

    # ------------------------------------------------------------------
    # Messaging helpers
    async def send_message(
        self, room_id: Optional[str] = None, text: Optional[str] = None, **kwargs
    ) -> bool:
        if not self.is_enabled:
            log_warning(
                "[matrix_interface] Cannot send message - interface is disabled"
            )
            return False
        skip_history = False
        if isinstance(room_id, dict):
            payload = room_id
            text = payload.get("text", text)
            room_id = payload.get("target") or payload.get("room_id")
            reply_to_event_id = payload.get("reply_to_event_id")
            thread_event_id = payload.get("thread_event_id") or payload.get("thread_id")
            skip_history = payload.get("skip_history", False)
        else:
            payload = kwargs
            room_id = room_id or payload.get("target") or payload.get("room_id")
            reply_to_event_id = payload.get("reply_to_event_id")
            thread_event_id = payload.get("thread_event_id") or payload.get("thread_id")
            skip_history = payload.get("skip_history", False)

        if not self.client:
            log_warning(
                "[matrix_interface] Cannot send message - client not initialized"
            )
            return False
        if not room_id:
            log_warning("[matrix_interface] Cannot send message - room_id missing")
            return False
        if not text:
            log_warning("[matrix_interface] Cannot send message - text missing")
            return False

        await self._ensure_login()

        if room_id.startswith("#"):
            try:
                response = await self.client.room_resolve_alias(room_id)
                if hasattr(response, "room_id"):
                    room_id = response.room_id
            except Exception as exc:
                log_warning(
                    f"[matrix_interface] Failed to resolve alias {room_id}: {exc}"
                )

        sent_ok = await self._send_matrix_message(
            room_id,
            text,
            reply_to_event_id=reply_to_event_id,
            thread_event_id=thread_event_id,
        )
        if not sent_ok:
            log_error(
                f"[matrix_interface] send_message failed: room_send to {room_id} "
                "did not succeed"
            )
            return False

        # Save SyntH's response via core chat_context_manager
        if not skip_history:
            try:
                from core.chat_context_manager import save_response_message
                from core.interface_path_utils import build_interface_path

                interface_path = build_interface_path(
                    "matrix",
                    str(room_id),
                    str(thread_event_id) if thread_event_id else None,
                )
                await save_response_message(interface_path, text)
            except Exception as e:
                log_debug(
                    f"[matrix_interface] Failed to save response via context_manager: {e}"
                )

        return True

    async def _send_matrix_message(
        self,
        room_id: str,
        text: str,
        *,
        reply_to_event_id: Optional[str] = None,
        thread_event_id: Optional[str] = None,
    ) -> bool:
        if not self.client:
            return False
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
            response = await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            # matrix-nio returns an ErrorResponse object (rather than raising)
            # when the send fails. Treat a missing event_id as failure so the
            # outcome propagates truthfully instead of a false success.
            if hasattr(response, "event_id") and response.event_id:
                log_debug(f"[matrix_interface] Message sent to {room_id}")
                return True
            log_error(
                f"[matrix_interface] room_send to {room_id} returned no event_id: "
                f"{response!r}"
            )
            return False
        except Exception as exc:  # pragma: no cover - network failure
            log_error(f"[matrix_interface] Failed to send message to {room_id}: {exc}")
            return False

    async def _send_matrix_file(
        self,
        room_id: str,
        file_path: Path,
        *,
        caption: Optional[str] = None,
        thread_event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload ``file_path`` to the media repo and send it into ``room_id``.

        The Matrix ``msgtype`` (m.image/m.audio/m.video/m.file) is chosen from
        the detected media kind so clients render images inline and expose
        native audio/video players.
        """
        from core.outbound_file_utils import (
            MEDIA_AUDIO,
            MEDIA_IMAGE,
            MEDIA_VIDEO,
            classify_media,
            guess_mime_type,
        )

        if not self.client:
            return {"status": "failed", "message": "Matrix client not initialized"}

        mime_type = guess_mime_type(file_path)
        filesize = file_path.stat().st_size
        filename = file_path.name

        try:
            # matrix-nio expects a callable data provider returning a stream.
            def _data_provider(_got_429: int, _got_timeouts: int):
                return open(file_path, "rb")

            resp, _keys = await self.client.upload(
                _data_provider,
                content_type=mime_type,
                filename=filename,
                filesize=filesize,
            )
        except Exception as exc:
            log_error(f"[matrix_interface] Upload failed for {file_path}: {exc}")
            return {"status": "failed", "message": f"Upload failed: {exc}"}

        content_uri = getattr(resp, "content_uri", None)
        if not content_uri:
            log_error(f"[matrix_interface] Upload returned no content_uri: {resp}")
            return {"status": "failed", "message": "Upload returned no content URI"}

        kind = classify_media(file_path)
        if kind == MEDIA_IMAGE:
            msgtype = "m.image"
        elif kind == MEDIA_AUDIO:
            msgtype = "m.audio"
        elif kind == MEDIA_VIDEO:
            msgtype = "m.video"
        else:
            msgtype = "m.file"

        content: Dict[str, Any] = {
            "msgtype": msgtype,
            "body": filename,
            "url": content_uri,
            "info": {"mimetype": mime_type, "size": filesize},
        }
        if thread_event_id:
            content["m.relates_to"] = {
                "event_id": thread_event_id,
                "rel_type": "m.thread",
                "is_falling_back": True,
            }

        try:
            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            log_info(f"[matrix_interface] Sent file ({kind}) to {room_id}")
        except Exception as exc:
            log_error(f"[matrix_interface] Failed to send file to {room_id}: {exc}")
            return {"status": "failed", "message": str(exc)}

        # Send an optional caption as a separate text message.
        if caption and caption.strip():
            await self._send_matrix_message(
                room_id, caption, thread_event_id=thread_event_id
            )

        return {"status": "success"}

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        """Dispatch non-message Matrix actions (currently file attachments)."""
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type != FILE_ACTION_TYPE:
            log_warning(
                f"[matrix_interface] execute_action: unknown action_type={action_type}"
            )
            return {
                "status": "failed",
                "message": f"Unknown action {action_type}",
            }

        if not self.is_enabled or not self.client:
            return {
                "status": "failed",
                "message": "Matrix interface is disabled or not initialized",
            }

        from core.outbound_file_utils import resolve_safe_outbound_path

        raw_path = payload.get("path")
        raw_target = payload.get("target") or payload.get("room_id")
        caption = payload.get("caption")
        thread_event_id = payload.get("thread_event_id") or payload.get("thread_id")

        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"status": "failed", "message": "payload.path must be a string"}
        if not isinstance(raw_target, str) or not raw_target.strip():
            return {"status": "failed", "message": "Missing target room"}
        if thread_event_id is not None and not isinstance(thread_event_id, str):
            thread_event_id = str(thread_event_id)
        caption = caption if isinstance(caption, str) else None

        resolved, err = resolve_safe_outbound_path(raw_path)
        if err or resolved is None:
            log_warning(f"[matrix_interface] Rejected file path {raw_path!r}: {err}")
            return {"status": "failed", "message": err or "Invalid path"}

        target: str = raw_target
        await self._ensure_login()

        if target.startswith("#"):
            try:
                response = await self.client.room_resolve_alias(target)
                resolved_room = getattr(response, "room_id", None)
                if isinstance(resolved_room, str):
                    target = resolved_room
            except Exception as exc:
                log_warning(
                    f"[matrix_interface] Failed to resolve alias {target}: {exc}"
                )

        return await self._send_matrix_file(
            target,
            resolved,
            caption=caption,
            thread_event_id=thread_event_id,
        )

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.user_id, username=self.username)

    async def reload_from_config(self) -> None:
        """Reload runtime configuration from the registry and restart/refresh
        the sync loop where appropriate.

        Behaviour:
        - Update in-memory policies and allowed-rooms immediately.
        - If authentication was newly provided -> start the sync loop.
        - If authentication was removed -> stop the sync loop.
        - When running, perform a short `sync()` to re-check pending invites/messages.
        """
        try:
            log_info("[matrix_interface] Applying configuration changes from registry")

            # Prime unresolved ConfigVar definitions without sync get_value() calls.
            await config_registry.load_all_from_db()

            # Read current values from ConfigVar wrappers
            new_password = (
                MATRIX_PASSWORD.value
                if hasattr(MATRIX_PASSWORD, "value")
                else MATRIX_PASSWORD
            )
            new_token = (
                MATRIX_ACCESS_TOKEN.value
                if hasattr(MATRIX_ACCESS_TOKEN, "value")
                else MATRIX_ACCESS_TOKEN
            )
            new_allowed = get_matrix_allowed_rooms() or set()
            new_auto_join = bool(MATRIX_AUTO_JOIN)
            new_invite_policy = str(MATRIX_INVITE_POLICY).lower()
            new_pm_policy = str(MATRIX_PRIVATE_MESSAGES).lower()

            # Apply lightweight changes immediately
            self.allowed_rooms = set(new_allowed)
            self.auto_join = new_auto_join
            self.invite_policy = new_invite_policy
            self.private_message_policy = new_pm_policy

            # Detect authentication changes
            has_active_session = bool(
                self.client and getattr(self, "_logged_in", False)
            )
            new_auth_configured = bool(new_password or new_token)
            prev_auth = bool(
                getattr(self, "_auth_configured", False) or has_active_session
            )
            effective_auth = bool(new_auth_configured or has_active_session)
            self.password = new_password
            self.access_token = new_token

            # If auth state changed from unauthenticated -> authenticated, start
            if not prev_auth and effective_auth:
                log_info("[matrix_interface] Credentials supplied — starting sync loop")
                self._auth_configured = effective_auth
                try:
                    await self.start()
                except Exception as exc:
                    log_warning(
                        f"[matrix_interface] Failed to start after config change: {exc}"
                    )

                # Attempt a short sync to pick up pending invites/messages
                try:
                    if self.client and has_active_session:
                        await self.client.sync(timeout=1000, full_state=False)
                except Exception as exc:
                    log_debug(
                        f"[matrix_interface] Short sync after reload failed: {exc}"
                    )

            # If auth removed, stop the interface
            elif prev_auth and not effective_auth:
                log_info("[matrix_interface] Credentials removed — stopping sync loop")
                self._auth_configured = False
                try:
                    await self.stop()
                except Exception as exc:
                    log_warning(
                        f"[matrix_interface] Failed to stop after config change: {exc}"
                    )

            # Auth unchanged: if we're logged in trigger a short sync so
            # invite/message state is re-evaluated under the new policies.
            else:
                self._auth_configured = effective_auth
                if self.client and has_active_session:
                    try:
                        await self.client.sync(timeout=1000, full_state=False)
                    except Exception as exc:
                        log_debug(
                            f"[matrix_interface] Short sync during config reload failed: {exc}"
                        )

        except Exception as exc:
            log_error(f"[matrix_interface] reload_from_config failed: {exc}")


# ----------------------------------------------------------------------
# Configuration via registry

# Register exposed variables for WebUI
register_exposed_var(
    "MATRIX_HOMESERVER",
    label="Matrix Homeserver",
    default="https://matrix.org/homeserver",
    value_type=str,
    ui_type="string",
    description="Base URL of the Matrix homeserver (e.g. https://matrix.org/homeserver)",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_USER",
    label="Matrix User ID",
    default="",
    value_type=str,
    ui_type="string",
    description="Matrix MXID used by the bot (e.g. @yoursynth:matrix.org).",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_PASSWORD",
    label="Matrix Password",
    default=None,
    value_type=str,
    ui_type="password",
    description="Optional. Password used for password-based login. If you prefer, set `MATRIX_ACCESS_TOKEN` instead; the interface will remain visible in the WebUI even without credentials.",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_ACCESS_TOKEN",
    label="Matrix Access Token",
    default=None,
    value_type=str,
    ui_type="password",
    description="Optional long-lived access token used instead of password login. Interface will appear in the WebUI even if credentials are not yet configured.",
    scope="interface",
    tags=["sensitive"],
    needs_component_reload=True,
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_DEVICE_ID",
    label="Matrix Device ID",
    default=None,
    value_type=str,
    ui_type="string",
    description="Device identifier to reuse when establishing a session.",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_DEVICE_NAME",
    label="Matrix Device Name",
    default="SyntH",
    value_type=str,
    ui_type="string",
    description="Human readable name for the device registered on the homeserver.",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_STORE_PATH",
    label="Matrix Store Path",
    default=None,
    value_type=str,
    ui_type="string",
    description="Filesystem path where the Matrix client should store sync data.",
    scope="interface",
    tags=["bootstrap"],
    hidden=True,
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_ALLOWED_ROOMS",
    label="Matrix Allowed Rooms",
    default="",
    value_type=str,
    ui_type="string",
    description="Comma separated list of room IDs the bot is allowed to respond in. Leave empty to allow all rooms.",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_AUTO_JOIN",
    label="Matrix Auto-join Invites",
    default=True,
    value_type=bool,
    ui_type="boolean",
    description="Automatically join rooms when invited (recommended for easier setup).",
    scope="interface",
    component="matrix_chat",
)

register_exposed_var(
    "MATRIX_INVITE_POLICY",
    label="Matrix Invite Policy",
    default="trainer_only",
    value_type=str,
    ui_type="select",
    description="Controls which invites are auto-joined: 'allow_all' (auto-join invites from anyone) or 'trainer_only' (only auto-join invites from trainer/trusted users).",
    scope="interface",
    component="matrix_chat",
    options=["allow_all", "trainer_only"],
)

register_exposed_var(
    "MATRIX_PRIVATE_MESSAGES",
    label="Matrix Private Messages",
    default="trainer_only",
    value_type=str,
    ui_type="select",
    description="Controls which private (1:1) messages the bot will accept: 'allow_all' or 'trainer_only'.",
    scope="interface",
    component="matrix_chat",
    options=["allow_all", "trainer_only"],
)

register_exposed_var(
    "MATRIX_TRUSTED_USERS",
    label="Matrix Trusted Users",
    default="",
    value_type=str,
    ui_type="string",
    description="Comma-separated list of MXIDs (e.g. @alice:matrix.org) that are always allowed for private messages and invites when trainer-only policy is active.",
    scope="interface",
    component="matrix_chat",
)

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


def get_matrix_trusted_users() -> set[str]:
    """Parse and return MXIDs configured as trusted users."""
    return _parse_allowed_rooms(str(_MATRIX_TRUSTED_USERS_RAW))


MATRIX_AUTO_JOIN = config_registry.get_var(
    "MATRIX_AUTO_JOIN",
    True,
    label="Matrix Auto-join Invites",
    description="If enabled, auto-join rooms when invited.",
    group="interface",
    component="matrix_chat",
)

MATRIX_INVITE_POLICY = config_registry.get_var(
    "MATRIX_INVITE_POLICY",
    "trainer_only",
    label="Matrix Invite Policy",
    description="Auto-join only trainer/trusted invites (trainer_only) or allow all invites (allow_all).",
    group="interface",
    component="matrix_chat",
)

MATRIX_PRIVATE_MESSAGES = config_registry.get_var(
    "MATRIX_PRIVATE_MESSAGES",
    "trainer_only",
    label="Matrix Private Messages",
    description="Controls whether private (1:1) messages are accepted from everyone or only from the trainer/trusted users.",
    group="interface",
    component="matrix_chat",
)

_MATRIX_TRUSTED_USERS_RAW = config_registry.get_var(
    "MATRIX_TRUSTED_USERS",
    "",
    label="Matrix Trusted Users",
    description="Comma-separated list of MXIDs (e.g. @alice:matrix.org) that are always allowed for private messages and invites when trainer-only policy is active.",
    group="interface",
    component="matrix_chat",
)

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
    allowed_rooms=list(get_matrix_allowed_rooms())
    if get_matrix_allowed_rooms()
    else None,
    auto_join=bool(MATRIX_AUTO_JOIN),
    trainer_id=MATRIX_TRAINER_ID,
)


# ------------------------------------------------------------------
# Runtime reload / config listeners
async def reload_interface():
    """Reload the Matrix interface when component-level config changes.

    This is discovered by core_initializer and registered as the component
    reload handler so `needs_component_reload` flips will automatically
    trigger a runtime reload.
    """
    try:
        log_info("[matrix_interface] Reload handler invoked - reloading from config")
        await MATRIX_INTERFACE_INSTANCE.reload_from_config()
    except Exception as exc:
        log_error(f"[matrix_interface] Failed to reload interface: {exc}")


def _schedule_instance_reload(_new_value=None) -> None:
    """Synchronous callback for config_registry.add_listener that schedules
    an asynchronous reload task for the active Matrix interface instance.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(MATRIX_INTERFACE_INSTANCE.reload_from_config())
    except RuntimeError:
        # No running loop available (e.g. during import); defer until app init
        log_debug(
            "[matrix_interface] Reload requested but no running loop; will apply on next start"
        )


# Register listeners for configuration keys that should hot-reload the
# interface when changed via WebUI/API. We register both keys that set
# needs_component_reload (handled by core_initializer) and other runtime
# settings so changes take effect immediately.
for _key in (
    "MATRIX_PASSWORD",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_USER",
    "MATRIX_HOMESERVER",
    "MATRIX_ALLOWED_ROOMS",
    "MATRIX_AUTO_JOIN",
    "MATRIX_INVITE_POLICY",
    "MATRIX_PRIVATE_MESSAGES",
    "MATRIX_TRUSTED_USERS",
    "MATRIX_DEVICE_ID",
    "MATRIX_DEVICE_NAME",
    "MATRIX_STORE_PATH",
):
    try:
        config_registry.add_listener(_key, _schedule_instance_reload)
    except Exception:
        # If registration fails during import-time tests, ignore and continue
        pass
