"""Interfaces as extensions: registration, inbound and outbound messages (M3).

An interface extension talks to a chat platform. It reaches the Core through
three RPC methods, and the Core reaches it through one:

* ``interfaces.register`` / ``interfaces.unregister``: the extension announces an
  interface id **it declared in its manifest**. The Core then creates a
  :class:`RemoteInterface`, an object with the same duck-typed contract as the
  in-process interfaces, and puts it in the Core's registries. Everything after
  that (the action prompt, capability gate, ``_dispatch_send_message``, name
  resolution) works unchanged.
* ``interfaces.inbound``: a message arrived on the platform. **The Core does the
  rest**, exactly as an in-process interface does: it records the message in the
  chat history, tracks the path, answers ``/commands`` and puts everything else
  on ``message_queue``. The extension never sees the history or the queue, and
  there is no other way to feed the message chain.
* ``interface.send_message`` (Core to extension): the Core asks the extension to
  deliver a reply.

Rules enforced here, in the Core:

* an extension can register **only interface ids declared in its manifest** and
  only if no other interface (in-process or remote) already has that id;
* an extension can inject messages **only into interfaces it registered itself**,
  and only on paths that match the patterns it declared, so it cannot write into
  another interface's conversations;
* when the extension's session ends for any reason, its interfaces are removed
  from every registry, so a stopped or crashed extension leaves nothing behind.

.. warning::

   ``interfaces.inbound`` lets an extension say who sent a message. Sender ids are
   what ``TRAINER_IDS`` (``"<interface>:<id>"``) matches, so an interface extension
   can speak as the trainer **on its own interface** and run trainer commands
   there. It cannot do so on any other interface. Grant the capability accordingly.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol

from synth_sdk.paths import (
    MAX_PATH_LENGTH,
    build_interface_path,
    default_patterns,
    match_fields,
    match_path,
    parse_interface_path,
    path_problem,
)
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param, Session
from core.extensions.media import MediaBridge, MediaError
from core.interface_traits import SEND_MESSAGE, InterfaceTraits

CAP_REGISTER = "interfaces.register"
CAP_INBOUND = "interfaces.inbound"

#: Capabilities that move files across the boundary (see ``core.extensions.media``).
MEDIA_CAPABILITIES = frozenset({"send_file", "send_audio", "send_voice"})
#: ``activity``: the interface can show "typing..." and similar while the model works.
#: ``reaction``: it can react to a message with an emoji. ``avatar``: it can set the
#: synth avatar as its account picture. ``call``: it can place and host calls (see
#: ``core.extensions.calls``).
#: Every capability an interface extension may advertise.
SUPPORTED_CAPABILITIES = (
    frozenset({"send_message", "activity", "reaction", "avatar", "call"})
    | MEDIA_CAPABILITIES
)
#: What the Core's structural capability gate calls the capabilities above.
_CORE_TOKEN = {"avatar": "set_avatar"}

MAX_ACTIONS = 8
MAX_FIELDS = 12
_FIELD_TYPES = {"string": str, "integer": int, "boolean": bool}

_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_USERNAME_RE = re.compile(r"[A-Za-z0-9_.@-]*")
MAX_TEXT = 20_000
MAX_NAME = 200
SEND_TIMEOUT = 30.0
MAX_AVATAR_BYTES = 700_000
#: The longest ``wait_reply`` holds a call open, in seconds.
MAX_REPLY_WAIT = 900.0
RESOLVE_TIMEOUT = 3.0

#: The only payload fields forwarded to an extension on ``send_message``.
_OUTBOUND_FIELDS = ("text", "interface_path", "reply_to", "send_as_voice")

NameResolver = Callable[..., Awaitable[dict[str, str | None]]]


# ------------------------------------------------------------------ ports


#: Key under which a turn the extension started (``turns.request``) keeps its context.
CONTEXT_KEY = "extension_turn"


def turn_context_for(context_memory: Any, extension_id: str) -> dict[str, Any]:
    """What the extension passed in ``turns.request``, if this turn is its own."""
    turn = context_memory.get(CONTEXT_KEY) if isinstance(context_memory, dict) else None
    if isinstance(turn, dict) and turn.get("extension_id") == extension_id:
        value = turn.get("context")
        return dict(value) if isinstance(value, dict) else {}
    return {}


class InterfaceHost(Protocol):
    """Where interfaces are registered in the Core."""

    def exists(self, name: str) -> bool: ...

    async def register(self, name: str, interface: RemoteInterface) -> None: ...

    async def unregister(self, name: str, interface: RemoteInterface) -> None: ...

    def set_name_resolver(self, name: str, resolver: NameResolver | None) -> None: ...

    def set_traits(self, name: str, traits: InterfaceTraits | None) -> None: ...

    def interface_enabled(self, name: str) -> None:
        """The interface can deliver now (it was disabled): bring it up to date."""
        ...


class MessagingPort(Protocol):
    """The Core services an inbound message goes through."""

    async def record(
        self,
        interface_path: str,
        text: str,
        sender_name: str,
        sender_id: str,
        message_id: str | None,
        metadata: dict[str, Any] | None,
        timestamp: datetime | None = None,
    ) -> None: ...

    async def seen(
        self, interface_path: str, sender_id: str, timestamp: datetime
    ) -> bool: ...

    async def touch(
        self, interface_path: str, chat_id: Any, thread_id: Any, bot: Any
    ) -> None: ...

    async def learn_alias(self, interface_id: str, message: Any) -> None: ...

    def trainer_of(self, interface_id: str) -> str | None: ...

    async def command(
        self, text: str, user_id: str, interface_id: str, context: dict[str, Any]
    ) -> str | None: ...

    async def enqueue(
        self, bot: Any, message: Any, interface_id: str, skip_mention_check: bool
    ) -> None: ...

    async def enqueue_and_wait(
        self,
        bot: Any,
        message: Any,
        interface_id: str,
        *,
        context_memory: Any,
        history_scope: str | None,
        timeout: float | None,
    ) -> str | None:
        """Run the turn and return its reply text (``None`` if it produced none)."""
        ...

    def agent_in_flight(self, interface_path: str) -> bool:
        """A detached agent turn for this path will deliver its text later."""
        ...

    async def set_processing(self, interface_path: str, processing: bool) -> None: ...


@dataclass
class InterfacePorts:
    host: InterfaceHost
    messaging: MessagingPort
    #: Copies files across the boundary. ``None``: attachments are not enabled.
    media: MediaBridge | None = None


# --------------------------------------------------------------- the proxy


@dataclass(frozen=True)
class InterfaceSpec:
    """What an extension told the Core about one of its interfaces."""

    id: str
    display_name: str
    patterns: tuple[str, ...]
    capabilities: frozenset[str] = frozenset({"send_message"})
    bot_id: str | None = None
    bot_username: str | None = None
    path_hint: str = ""
    description: str = ""
    resolve_names: bool = False
    #: Actions the interface offers besides ``send_message``: name -> definition.
    actions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


class RemoteInterface:
    """An interface that lives in an extension process.

    Implements the duck-typed contract the Core uses for in-process interfaces
    (``display_name``, ``get_interface_id``, ``send_message``,
    ``get_supported_actions``, ...) by forwarding to the extension over the
    gateway. If the extension is gone, a send simply fails (``False``): the Core's
    normal "delivery failed" handling applies.
    """

    required_config_vars: tuple[str, ...] = ()

    def __init__(
        self, session: Session, spec: InterfaceSpec, media: MediaBridge | None = None
    ) -> None:
        self._session = session
        self._media = media
        self.spec = spec
        self.display_name = spec.display_name
        #: False while the extension has nothing to deliver with (for example a user
        #: account that is not logged in). Sending then fails; the WebUI shows why.
        self.is_enabled = True
        self.disabled_reason: str | None = None
        self.sent = 0
        self.failed = 0

    # --------------------------------------------------------- declaration

    def get_interface_id(self) -> str:
        return self.spec.id

    def get_capabilities(self) -> frozenset[str]:
        return frozenset(_CORE_TOKEN.get(c, c) for c in self.spec.capabilities)

    def get_supported_actions(self) -> dict[str, Any]:
        from core.message_registry import get_send_message_schema

        supported: dict[str, Any] = {
            "send_message": get_send_message_schema([self.spec.id])
        }
        for name, definition in self.spec.actions.items():
            fields = definition.get("fields", {})
            supported[name] = {
                "description": str(definition.get("description", "")),
                "required_fields": [
                    f for f, d in fields.items() if not d.get("optional")
                ],
                "optional_fields": [f for f, d in fields.items() if d.get("optional")],
            }
        return supported

    def get_interface_path_hint(self) -> str:
        if self.spec.path_hint:
            return self.spec.path_hint
        return ", ".join(self.spec.patterns)

    def get_interface_instructions(self) -> str:
        return self.spec.description

    def get_prompt_instructions(self, action_name: str) -> dict[str, Any]:
        own = self.spec.actions.get(action_name)
        if own is not None:
            return {
                "description": str(own.get("description", "")),
                "payload": {
                    name: {
                        key: value
                        for key, value in dict(spec).items()
                        if key in ("type", "example", "description", "optional")
                    }
                    for name, spec in own.get("fields", {}).items()
                },
                "important_notes": list(own.get("notes", [])),
            }
        if action_name != "send_message":
            return {}
        return {
            "description": f"Send a message through {self.spec.display_name}.",
            "payload": {
                "text": {"type": "string", "description": "Content of the message."},
                "interface_path": {
                    "type": "string",
                    "description": (
                        "Destination path, one of: "
                        + self.get_interface_path_hint()
                        + ". Optional when replying to an incoming message."
                    ),
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional message id to quote.",
                    "optional": True,
                },
            },
        }

    def validate_payload(self, action_type: str, payload: dict[str, Any]) -> list[str]:
        own = self.spec.actions.get(action_type)
        if own is not None:
            problems: list[str] = []
            for name, spec in own.get("fields", {}).items():
                value = payload.get(name)
                if value is None:
                    if not spec.get("optional"):
                        problems.append(f"payload.{name} is required")
                    continue
                kind = _FIELD_TYPES.get(str(spec.get("type", "string")), str)
                if not isinstance(value, kind) or (
                    kind is int and isinstance(value, bool)
                ):
                    problems.append(f"payload.{name} must be a {spec.get('type')}")
            return problems
        if action_type != "send_message":
            return []
        text = payload.get("text")
        media = payload.get("media")
        if (not isinstance(text, str) or not text.strip()) and not media:
            return ["payload.text or payload.media is required"]
        return []

    async def execute_action(
        self,
        action: dict[str, Any],
        context: Any = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> dict[str, Any]:
        """Run one of the interface's own actions in the extension."""
        name = action.get("type") or action.get("action")
        if name not in self.spec.actions or not self.connected:
            return {"status": "failed", "message": f"Unknown action {name}"}
        payload = action.get("payload")
        try:
            result = await self._session.call(
                "interface.action",
                {
                    "interface_id": self.spec.id,
                    "name": name,
                    "payload": dict(payload) if isinstance(payload, Mapping) else {},
                    # What the extension itself put in ``turns.request``, when this
                    # action answers one of its turns; nothing otherwise.
                    "context": turn_context_for(context, self._session.extension_id),
                },
                timeout=SEND_TIMEOUT,
            )
        except RpcError as exc:
            return {"status": "failed", "message": exc.message[:200]}
        if not isinstance(result, Mapping):
            return {"status": "failed", "message": "no result"}
        return {
            "status": str(result.get("status", "failed"))[:20],
            "message": str(result.get("message", ""))[:500],
        }

    async def add_reaction(self, message: Any, emoji: str) -> bool:
        """React to a message. ``False`` when the extension cannot or does not."""
        if "reaction" not in self.spec.capabilities or not self.connected:
            return False
        try:
            result = await self._session.call(
                "interface.reaction",
                {
                    "interface_id": self.spec.id,
                    "chat_id": _as_text(getattr(message, "chat_id", None)),
                    "message_id": _as_text(getattr(message, "message_id", None)),
                    "emoji": str(emoji)[:32],
                },
                timeout=RESOLVE_TIMEOUT * 3,
            )
        except RpcError:
            return False
        return bool(result.get("ok")) if isinstance(result, Mapping) else False

    def uses_synth_avatar(self) -> bool:
        return "avatar" in self.spec.capabilities

    async def set_avatar(
        self,
        image_bytes: bytes | None,
        mime: str | None = None,
        version: str | None = None,
        force: bool = False,
    ) -> bool:
        """Hand the synth avatar to the extension (``None`` removes it)."""
        if "avatar" not in self.spec.capabilities or not self.connected:
            return False
        if image_bytes is not None and len(image_bytes) > MAX_AVATAR_BYTES:
            return False
        params: dict[str, Any] = {
            "interface_id": self.spec.id,
            "version": version,
            "force": bool(force),
            "mime": mime,
        }
        if image_bytes is not None:
            params["image"] = base64.b64encode(image_bytes).decode("ascii")
        try:
            result = await self._session.call(
                "interface.set_avatar", params, timeout=SEND_TIMEOUT
            )
        except RpcError:
            return False
        return bool(result.get("ok")) if isinstance(result, Mapping) else False

    async def start_call(self, user_id: Any) -> dict[str, str]:
        """Ring a person (see ``core.extensions.calls``)."""
        return await self._call_control("interface.start_call", user_id)

    async def end_call(self, user_id: Any) -> dict[str, str]:
        """Hang up the call with a person."""
        return await self._call_control("interface.end_call", user_id)

    async def _call_control(self, method: str, user_id: Any) -> dict[str, str]:
        if "call" not in self.spec.capabilities or not self.connected:
            return {"status": "failed", "message": "calls are not available"}
        try:
            result = await self._session.call(
                method,
                {"interface_id": self.spec.id, "user_id": _as_text(user_id)},
                timeout=SEND_TIMEOUT,
            )
        except RpcError as exc:
            return {"status": "failed", "message": exc.message[:200]}
        if not isinstance(result, Mapping):
            return {"status": "failed", "message": "no result"}
        return {
            "status": str(result.get("status", "failed"))[:20],
            "message": str(result.get("message", ""))[:500],
        }

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.spec.bot_id, username=self.spec.bot_username)

    async def start(self) -> None:
        """Nothing to start: the extension process is run by the supervisor."""

    async def stop(self) -> None:
        """Nothing to stop: see :meth:`start`."""

    # ------------------------------------------------------------- sending

    @property
    def connected(self) -> bool:
        return not self._session.peer.closed

    def _outbound_payload(
        self, payload: Any, kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        if isinstance(payload, Mapping):
            source: Mapping[str, Any] = payload
        else:
            # Older call style: send_message(channel_id=..., text=...).
            channel = kwargs.get("channel_id", kwargs.get("chat_id", payload))
            source = {
                "text": kwargs.get("text"),
                "interface_path": build_interface_path(
                    self.spec.id, channel, kwargs.get("thread_id")
                )
                if channel is not None
                else None,
                "reply_to": kwargs.get("reply_to"),
            }
        out: dict[str, Any] = {}
        for key in _OUTBOUND_FIELDS:
            value = source.get(key)
            if isinstance(value, (str, bool, int, float)) and value != "":
                out[key] = value if not isinstance(value, str) else value[:MAX_TEXT]
        return out

    async def send_message(
        self, payload: Any = None, original_message: Any = None, **kwargs: Any
    ) -> bool:
        """Ask the extension to deliver a message. ``True`` when it reports success."""
        if not self.connected or not self.is_enabled:
            self.failed += 1
            return False
        out = self._outbound_payload(payload, kwargs)
        staged = None
        raw_media = (
            payload.get("media") if isinstance(payload, Mapping) else None
        ) or ()
        if (
            raw_media
            and self._media is not None
            and self.spec.capabilities & MEDIA_CAPABILITIES
        ):
            paths = (
                [str(p) for p in raw_media]
                if isinstance(raw_media, (list, tuple))
                else []
            )
            try:
                staged = await self._media.stage_outbound(
                    self._session.extension_id, paths
                )
            except (OSError, MediaError):
                staged = None
            if staged is not None and staged.items:
                out["attachments"] = staged.items
            if "attachments" not in out and not out.get("text"):
                if staged is not None:
                    staged.cleanup()
                self.failed += 1
                return False
        try:
            return await self._deliver(out, original_message)
        finally:
            if staged is not None:
                await asyncio.to_thread(staged.cleanup)

    async def _deliver(self, out: dict[str, Any], original_message: Any) -> bool:
        origin = {
            "message_id": _as_text(getattr(original_message, "message_id", None)),
            "interface_path": _as_text(
                getattr(original_message, "interface_path", None)
            ),
        }
        try:
            result = await self._session.call(
                "interface.send_message",
                {"interface_id": self.spec.id, "payload": out, "origin": origin},
                timeout=SEND_TIMEOUT,
            )
        except RpcError:
            self.failed += 1
            return False
        ok = bool(result.get("ok")) if isinstance(result, Mapping) else bool(result)
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    # ------------------------------------------------------ name resolution

    async def send_activity(self, interface_path: str, kind: str) -> None:
        """Show "typing..." and the like in a chat. Fail-open: never raises."""
        if not self.connected or "activity" not in self.spec.capabilities:
            return
        try:
            await self._session.call(
                "interface.activity",
                {
                    "interface_id": self.spec.id,
                    "interface_path": str(interface_path)[:MAX_PATH_LENGTH],
                    "kind": str(kind)[:MAX_NAME],
                },
                timeout=RESOLVE_TIMEOUT,
            )
        except RpcError:
            return

    async def resolve_names(
        self, chat_id: Any, thread_id: Any, bot: Any = None
    ) -> dict[str, str | None]:
        """Ask the extension for the live chat/thread names. Fails soft (empty)."""
        if not self.connected:
            return {}
        try:
            result = await self._session.call(
                "interface.resolve_names",
                {
                    "interface_id": self.spec.id,
                    "chat_id": _as_text(chat_id),
                    "thread_id": _as_text(thread_id),
                },
                timeout=RESOLVE_TIMEOUT,
            )
        except RpcError:
            return {}
        names: dict[str, str | None] = {}
        if isinstance(result, Mapping):
            for key in ("chat_name", "message_thread_name"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    names[key] = value[:MAX_NAME]
        return names


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)[:MAX_NAME]


# -------------------------------------------------------------- the API


def _owned(session: Session) -> dict[str, RemoteInterface]:
    interfaces: dict[str, RemoteInterface] = session.state.setdefault("interfaces", {})
    return interfaces


def _patterns_for(ctx: CallContext, interface_id: str) -> tuple[str, ...]:
    for provided in ctx.manifest.provides.interfaces:
        if provided.id == interface_id:
            return provided.paths or default_patterns(interface_id)
    return ()


def _traits_for(ctx: CallContext, interface_id: str) -> InterfaceTraits:
    """The traits the manifest declares for ``interface_id``."""
    for provided in ctx.manifest.provides.interfaces:
        if provided.id == interface_id:
            return InterfaceTraits(
                user_facing=provided.user_facing,
                voice_input_tts_only=provided.voice_input_tts_only,
                negative_chat_id_is_group=provided.negative_chat_id_is_group,
                bot_username_suffix=provided.bot_username_suffix or None,
                peer_ids=provided.peer_ids,
                reply_only=provided.reply_only,
                speech_as_media=provided.speech_as_media,
                voice_format=provided.voice_format,
                message_action=provided.message_action or SEND_MESSAGE,
            )
    return InterfaceTraits()


def _parse_timestamp(raw: Any) -> datetime | None:
    """An ISO-8601 time as an aware UTC datetime; ``None`` when absent."""
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        raise RpcError(
            ErrorCode.INVALID_PARAMS, "timestamp must be an ISO-8601 time"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return min(moment.astimezone(timezone.utc), datetime.now(timezone.utc))


MAX_HISTORY_MESSAGES = 60
_HISTORY_ROLES = ("user", "assistant", "system")


def _history_memory(raw: Any, chat_id: Any, last_text: str) -> dict[str, Any] | None:
    """The conversation the client sent along, as the Core's local prompt memory.

    A stateless client (an HTTP API) resends the whole conversation each time; that
    conversation, not the chat's stored history, is what the prompt is built from.
    """
    from collections import deque

    if raw in (None, []):
        return None
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY_MESSAGES:
        raise RpcError(
            ErrorCode.INVALID_PARAMS,
            f"history must be a list of at most {MAX_HISTORY_MESSAGES} messages",
        )
    stamp = datetime.now(timezone.utc).isoformat()
    memory: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY_MESSAGES + 1)
    for item in raw:
        if (
            not isinstance(item, dict)
            or item.get("role") not in _HISTORY_ROLES
            or not isinstance(item.get("text"), str)
        ):
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "history items are {role: user|assistant|system, text}",
            )
        if item["text"].strip():
            memory.append(
                {
                    "role": item["role"],
                    "text": item["text"][:MAX_TEXT],
                    "timestamp": stamp,
                    "chat_id": chat_id,
                }
            )
    memory.append(
        {"role": "user", "text": last_text, "timestamp": stamp, "chat_id": chat_id}
    )
    return {chat_id: memory}


def _parse_actions(raw: Any, declared: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Validate the actions an interface offers (each must be declared in the manifest)."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_ACTIONS:
        raise RpcError(
            ErrorCode.INVALID_PARAMS,
            f"actions must be an object of at most {MAX_ACTIONS} entries",
        )
    parsed: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if name not in declared:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"action {name!r} is not declared in the manifest",
            )
        if not isinstance(definition, dict):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, f"action {name!r} must be an object"
            )
        fields_in = definition.get("fields", {})
        if not isinstance(fields_in, dict) or len(fields_in) > MAX_FIELDS:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, f"action {name!r}: fields must be an object"
            )
        fields: dict[str, dict[str, Any]] = {}
        for field_name, spec in fields_in.items():
            if (
                not isinstance(field_name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", field_name)
                or not isinstance(spec, dict)
                or spec.get("type", "string") not in _FIELD_TYPES
            ):
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    f"action {name!r}: bad field {field_name!r}",
                )
            fields[field_name] = {
                "type": spec.get("type", "string"),
                "description": str(spec.get("description", ""))[:300],
                "optional": bool(spec.get("optional", False)),
                **({"example": spec["example"]} if "example" in spec else {}),
            }
        notes = definition.get("notes", [])
        if not isinstance(notes, list):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, f"action {name!r}: notes must be a list"
            )
        parsed[name] = {
            "description": str(definition.get("description", ""))[:500],
            "fields": fields,
            "notes": [str(n)[:300] for n in notes[:6]],
        }
    return parsed


def _declared_actions(ctx: CallContext, interface_id: str) -> tuple[str, ...]:
    for provided in ctx.manifest.provides.interfaces:
        if provided.id == interface_id:
            return provided.actions
    return ()


@dataclass
class InterfaceStats:
    registered: int = 0
    inbound: int = 0
    commands: int = 0
    rejected: int = 0
    last_inbound: float = field(default=0.0)


def optional(
    name: str, *, max_length: int | None = None, pattern: re.Pattern[str] | None = None
) -> Param:
    """An optional string parameter that defaults to ``None``."""
    return Param(
        name, required=False, default=None, max_length=max_length, pattern=pattern
    )


def register_interface_api(gateway: Gateway, ports: InterfacePorts) -> InterfaceStats:
    """Register ``interfaces.*`` on the gateway. Returns counters for monitoring."""
    stats = InterfaceStats()
    host, messaging, media = ports.host, ports.messaging, ports.media

    async def register(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        interface_id = args["interface_id"]
        patterns = _patterns_for(ctx, interface_id)
        if not patterns:
            gateway.record(
                ctx.extension_id, "interfaces.register", "denied", interface_id
            )
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"interface {interface_id!r} is not declared in the manifest",
            )
        raw_caps = args["capabilities"]
        if not isinstance(raw_caps, list) or not all(
            isinstance(c, str) for c in raw_caps
        ):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "capabilities must be a list of strings"
            )
        capabilities: frozenset[str] = frozenset(str(c) for c in raw_caps)
        unsupported = sorted(capabilities - SUPPORTED_CAPABILITIES)
        if capabilities & MEDIA_CAPABILITIES and media is None:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "attachments are not enabled on this Core"
            )
        if unsupported:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"capability not supported yet: {', '.join(unsupported)}",
            )
        owned = _owned(ctx.session)
        previous = owned.get(interface_id)
        if host.exists(interface_id) and previous is None:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"interface id {interface_id!r} is already registered",
            )
        spec = InterfaceSpec(
            id=interface_id,
            display_name=args["display_name"],
            patterns=patterns,
            capabilities=frozenset(capabilities | {"send_message"}),
            bot_id=args["bot_id"],
            bot_username=args["bot_username"],
            path_hint=args["path_hint"],
            description=args["description"],
            resolve_names=args["resolve_names"],
            actions=_parse_actions(
                args["actions"], _declared_actions(ctx, interface_id)
            ),
        )
        remote = RemoteInterface(ctx.session, spec, media)
        if previous is not None:
            await host.unregister(interface_id, previous)
        await host.register(interface_id, remote)
        host.set_traits(interface_id, _traits_for(ctx, interface_id))
        host.set_name_resolver(
            interface_id, remote.resolve_names if spec.resolve_names else None
        )
        owned[interface_id] = remote
        stats.registered += 1
        return {"registered": interface_id, "patterns": list(patterns)}

    async def set_state(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        remote = _owned(ctx.session).get(args["interface_id"])
        if remote is None:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{args['interface_id']!r} is not an interface you registered",
            )
        was_enabled = remote.is_enabled
        remote.is_enabled = bool(args["enabled"])
        remote.disabled_reason = (
            None if remote.is_enabled else (args["reason"] or "disabled")
        )
        if remote.is_enabled and not was_enabled:
            host.interface_enabled(args["interface_id"])
        return {"ok": True}

    async def unregister(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        owned = _owned(ctx.session)
        remote = owned.pop(args["interface_id"], None)
        if remote is None:
            return {"unregistered": False}
        host.set_name_resolver(args["interface_id"], None)
        host.set_traits(args["interface_id"], None)
        await host.unregister(args["interface_id"], remote)
        return {"unregistered": True}

    async def inbound(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        interface_id = args["interface_id"]
        remote = _owned(ctx.session).get(interface_id)
        if remote is None:
            stats.rejected += 1
            gateway.record(
                ctx.extension_id, "interfaces.inbound", "denied", interface_id
            )
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{interface_id!r} is not an interface you registered",
            )
        path = args["path"]
        problem = path_problem(path)
        name = parse_interface_path(path)[0]
        if problem or name != interface_id:
            stats.rejected += 1
            gateway.record(
                ctx.extension_id, "interfaces.inbound", "invalid_params", str(path)[:80]
            )
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"path {problem}"
                if problem
                else "path must start with the interface id",
            )
        if not match_path(path, remote.spec.patterns):
            stats.rejected += 1
            gateway.record(
                ctx.extension_id, "interfaces.inbound", "invalid_params", "pattern"
            )
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "path does not match a pattern declared in the manifest",
            )

        # What is the chat and what is the thread is the interface's to say, by naming the
        # placeholders of its path patterns ``chat`` and ``thread`` (the default shape),
        # or by passing ``chat_id``/``thread_id``. Levels are never guessed: a
        # ``<guild>/<channel>`` path has a second level that is not a thread.
        fields = match_fields(path, remote.spec.patterns) or {}
        chat_id = args["chat_id"] if args["chat_id"] is not None else fields.get("chat")
        if chat_id is None:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "name the chat: pass chat_id, or call a placeholder {chat} in the path pattern",
            )
        thread_id = (
            args["thread_id"] if args["thread_id"] is not None else fields.get("thread")
        )
        sender_id, sender_name = args["sender_id"], args["sender_name"]
        text, message_id = args["text"], args["message_id"]

        reply_to = args["reply_to"]
        reply_namespace = None
        metadata: dict[str, Any] | None = None
        if isinstance(reply_to, dict):
            reply_text = str(reply_to.get("text") or "")[:MAX_TEXT]
            reply_name = str(reply_to.get("sender_name") or "unknown")[:MAX_NAME]
            reply_id = _as_text(reply_to.get("message_id"))
            reply_namespace = SimpleNamespace(
                message_id=reply_id,
                text=reply_text or None,
                caption=None,
                date=None,
                from_user=SimpleNamespace(
                    id=_as_text(reply_to.get("sender_id")),
                    username=reply_name,
                    full_name=reply_name,
                ),
            )
            if reply_text.strip():
                metadata = {
                    "reply_to": {
                        "sender_name": reply_name,
                        "text": reply_text,
                        "message_id": reply_id,
                    }
                }

        forwarded_from = args["forwarded_from"]
        forward_origin = None
        if isinstance(forwarded_from, dict):
            origin_id = _as_text(forwarded_from.get("sender_id"))
            try:
                origin_date = _parse_timestamp(forwarded_from.get("date"))
            except RpcError:
                origin_date = None  # a bad origin date must not drop the message
            forward_origin = SimpleNamespace(
                sender_id=origin_id,
                sender_name=str(forwarded_from.get("sender_name") or "")[:MAX_NAME]
                or None,
                date=origin_date,
                # Forwarded by the person who first said it: a message from their past,
                # not something they are saying now.
                is_sender=bool(origin_id) and origin_id == str(sender_id),
            )

        sent_at = _parse_timestamp(args["timestamp"])
        attachments_in = None if args["backlog"] else args["attachments"]
        imported: list[dict[str, Any]] = []
        if attachments_in:
            if media is None:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS, "attachments are not enabled on this Core"
                )
            if not isinstance(attachments_in, list) or not all(
                isinstance(a, dict) for a in attachments_in
            ):
                raise RpcError(
                    ErrorCode.INVALID_PARAMS, "attachments must be a list of objects"
                )
            try:
                imported = await media.import_inbound(ctx.extension_id, attachments_in)
            except MediaError as exc:
                stats.rejected += 1
                gateway.record(
                    ctx.extension_id,
                    "interfaces.inbound",
                    "invalid_params",
                    f"attachment: {exc.reason}",
                )
                raise RpcError(
                    ErrorCode.INVALID_PARAMS, f"attachment refused: {exc.reason}"
                ) from None
            note = [
                {
                    "filename": a["filename"],
                    "mime_type": a["mime_type"],
                    "size": a["size"],
                }
                for a in imported
            ]
            metadata = {**(metadata or {}), "attachments": note}

        wrapped = SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            interface_path=path,
            text=text,
            caption=None,
            date=sent_at,
            thread_id=thread_id,
            from_user=SimpleNamespace(
                id=sender_id,
                username=args["sender_username"] or sender_name,
                full_name=args["sender_full_name"] or sender_name,
            ),
            chat=SimpleNamespace(
                id=chat_id,
                type=args["chat_type"],
                title=args["chat_title"] or chat_id,
                username=args["chat_username"],
                first_name=None,
            ),
            entities=None,
            reply_to_message=reply_namespace,
        )
        if imported:
            # The shape the Core's generic extractor already understands: it reads
            # each ``path`` (now a copy the Core owns) and inlines supported types.
            wrapped.attachments = [
                {
                    "mime_type": a["mime_type"],
                    "filename": a["filename"],
                    "path": a["path"],
                }
                for a in imported
            ]
        if args["is_explicit_trigger"]:
            wrapped.is_explicit_trigger = True
        if forward_origin is not None:
            wrapped.forward_origin = forward_origin
        # Spoken means the user is talking to the agent. A forwarded voice note, or one
        # sent with a caption ("transcribe this"), is material the user hands over: its
        # transcript is not their own speech, so the audio stays an attachment.
        is_material = forward_origin is not None or bool(
            text.strip() and (imported or args["attachments"])
        )
        if args["is_voice_input"] and not is_material:
            wrapped.is_voice_input = True
        if args["human_count"] is not None:
            wrapped.human_count = args["human_count"]

        await messaging.record(
            path, text, sender_name, sender_id, message_id, metadata, sent_at
        )
        await messaging.touch(path, chat_id, thread_id, remote)
        if args["sender_username"] or args["chat_username"]:
            await messaging.learn_alias(interface_id, wrapped)
        stats.inbound += 1
        stats.last_inbound = time.time()

        if args["backlog"]:
            # History delivered late: it belongs in the chat history under its own
            # time and nowhere else (no command, no turn).
            return {"stored": True}

        wait = bool(args["wait_reply"])
        if text.startswith("/"):
            reply = await messaging.command(
                text, sender_id, interface_id, {"bot": remote, "wrapped": wrapped}
            )
            stats.commands += 1
            if wait:
                # A request/response interface gets the answer in the response.
                return {
                    "handled": "command",
                    "reply": reply or None,
                    "pending": messaging.agent_in_flight(path),
                }
            if reply:
                await remote.send_message({"text": reply, "interface_path": path})
            return {"handled": "command"}
        if not wait:
            await messaging.enqueue(
                remote, wrapped, interface_id, args["skip_mention_check"]
            )
            return {"queued": True}
        return await _turn_and_wait(remote, wrapped, args, path, chat_id, text)

    async def _turn_and_wait(
        remote: RemoteInterface,
        wrapped: SimpleNamespace,
        args: dict[str, Any],
        path: str,
        chat_id: Any,
        text: str,
    ) -> dict[str, Any]:
        """Run the turn and answer with its text: for interfaces that are a request
        and a response (an HTTP API) rather than a conversation."""
        history = _history_memory(args["history"], chat_id, text)
        timeout = float(args["reply_timeout"]) or None
        await messaging.set_processing(path, True)
        try:
            reply = await messaging.enqueue_and_wait(
                remote,
                wrapped,
                remote.spec.id,
                context_memory=history,
                history_scope="local" if history is not None else None,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return {"queued": True, "reply": None, "pending": False, "timed_out": True}
        finally:
            await messaging.set_processing(path, False)
        text_reply = reply if isinstance(reply, str) and reply else None
        return {
            "queued": True,
            "reply": text_reply,
            # An agent turn runs detached: its text arrives later as a normal delivery.
            "pending": text_reply is None and messaging.agent_in_flight(path),
        }

    async def seen(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        remote = _owned(ctx.session).get(args["interface_id"])
        if remote is None:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{args['interface_id']!r} is not an interface you registered",
            )
        moment = _parse_timestamp(args["timestamp"])
        if moment is None:
            raise RpcError(ErrorCode.INVALID_PARAMS, "timestamp is required")
        found = await messaging.seen(args["path"], args["sender_id"], moment)
        return {"seen": bool(found)}

    async def trainer(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        if args["interface_id"] not in _owned(ctx.session):
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{args['interface_id']!r} is not an interface you registered",
            )
        found = messaging.trainer_of(args["interface_id"])
        return {"trainer_id": found}

    async def close_interfaces(session: Session) -> None:
        for interface_id, remote in list(_owned(session).items()):
            host.set_name_resolver(interface_id, None)
            host.set_traits(interface_id, None)
            await host.unregister(interface_id, remote)
        _owned(session).clear()

    gateway.on_attach.append(
        lambda session: session.on_close(lambda: close_interfaces(session))
    )

    ident = Param("interface_id", max_length=64, pattern=_ID_RE)
    for method in (
        ApiMethod(
            "interfaces.register",
            CAP_REGISTER,
            (
                ident,
                Param("display_name", max_length=MAX_NAME),
                Param("capabilities", "json", required=False, default=["send_message"]),
                optional("bot_id", max_length=MAX_NAME),
                optional("bot_username", max_length=MAX_NAME, pattern=_USERNAME_RE),
                Param("path_hint", max_length=500, required=False, default=""),
                Param("description", max_length=1000, required=False, default=""),
                Param("resolve_names", "bool", required=False, default=False),
                Param("actions", "json", required=False, default=None),
            ),
            register,
            "Announce an interface declared in the manifest.",
        ),
        ApiMethod("interfaces.unregister", CAP_REGISTER, (ident,), unregister),
        ApiMethod(
            "interfaces.set_state",
            CAP_REGISTER,
            (
                ident,
                Param("enabled", "bool"),
                optional("reason", max_length=200),
            ),
            set_state,
            "Say whether the interface can deliver right now, and why not.",
        ),
        ApiMethod(
            "interfaces.seen",
            CAP_INBOUND,
            (
                ident,
                Param("path", max_length=512),
                Param("sender_id", max_length=MAX_NAME),
                Param("timestamp", max_length=40),
            ),
            seen,
            "Whether a message from that sender at that time is already in the history.",
        ),
        ApiMethod(
            "interfaces.trainer",
            CAP_INBOUND,
            (ident,),
            trainer,
            "The trainer id the operator configured for the interface, if any.",
        ),
        ApiMethod(
            "interfaces.inbound",
            CAP_INBOUND,
            (
                ident,
                Param("path", max_length=512),
                Param("text", max_length=MAX_TEXT),
                Param("sender_id", max_length=MAX_NAME),
                Param("sender_name", max_length=MAX_NAME),
                optional("sender_full_name", max_length=MAX_NAME),
                optional("message_id", max_length=MAX_NAME),
                optional("chat_id", max_length=MAX_NAME),
                optional("thread_id", max_length=MAX_NAME),
                Param(
                    "chat_type",
                    required=False,
                    default="private",
                    choices=("private", "group"),
                ),
                optional("chat_title", max_length=MAX_NAME),
                optional("sender_username", max_length=MAX_NAME),
                optional("chat_username", max_length=MAX_NAME),
                Param("reply_to", "object", required=False, default=None, max_length=8),
                Param(
                    "forwarded_from",
                    "object",
                    required=False,
                    default=None,
                    max_length=8,
                ),
                Param("skip_mention_check", "bool", required=False, default=False),
                Param("is_explicit_trigger", "bool", required=False, default=False),
                Param("is_voice_input", "bool", required=False, default=False),
                optional("timestamp", max_length=40),
                Param("backlog", "bool", required=False, default=False),
                Param(
                    "human_count",
                    "int",
                    required=False,
                    default=None,
                    minimum=0,
                    maximum=1_000_000,
                ),
                Param("attachments", "json", required=False, default=None),
                Param("wait_reply", "bool", required=False, default=False),
                Param("history", "json", required=False, default=None),
                Param(
                    "reply_timeout",
                    "float",
                    required=False,
                    default=0.0,
                    minimum=0.0,
                    maximum=MAX_REPLY_WAIT,
                ),
            ),
            inbound,
            "Deliver a message received on the platform to the Core.",
            timeout=MAX_REPLY_WAIT + 30.0,
        ),
    ):
        gateway.register(method)
    return stats
