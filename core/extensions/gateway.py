"""The extension gateway: the only door between an extension and the Core (M2).

Each extension process is connected to a :class:`Session`. Every call it makes is
checked here, in the Core, before any handler runs:

#. the handshake (``session.hello``) must have happened;
#. the method must exist;
#. the session must be within its rate limit;
#. the method's capability must be **declared in the manifest and granted** (the
   grant store is asked on every call, so a revoke is immediate);
#. the parameters must match the method's declared shape;
#. the handler runs with a timeout and with the *gateway's* idea of who the
   caller is. An extension never names its own id.

Anything refused is recorded in :attr:`Gateway.audit`. Nothing about the Core's
internals (tracebacks, paths) is sent back over the pipe.

The gateway does not launch processes. The launcher hands it a reader/writer pair
(the child's stdout/stdin), see :mod:`core.extensions.launcher`.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from synth_sdk.rpc import PROTOCOL_VERSION, ErrorCode, RpcError, RpcPeer

from core.extensions.grants import GrantStore
from core.extensions.manifest import Manifest
from core.extensions.resolver import CORE_API_VERSION

SESSION_HELLO = "session.hello"
SESSION_BYE = "session.bye"

# Outcomes recorded in the audit trail.
DENIED = "denied"
RATE_LIMITED = "rate_limited"
INVALID_PARAMS = "invalid_params"
UNKNOWN_METHOD = "unknown_method"
HANDSHAKE = "handshake"
FAILED = "failed"


def extension_slug(extension_id: str) -> str:
    """``publisher/name`` as a dotted namespace token: ``publisher.name``."""
    return extension_id.replace("/", ".")


@dataclass(frozen=True)
class Param:
    """One declared parameter of an API method.

    ``kind`` is one of ``str``, ``int``, ``float``, ``bool``, ``object`` (a JSON
    object) or ``json`` (any JSON value).
    """

    name: str
    kind: str = "str"
    required: bool = True
    default: Any = None
    max_length: int | None = None
    pattern: re.Pattern[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None


Handler = Callable[["CallContext", dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ApiMethod:
    """A method an extension may call."""

    name: str
    #: Capability the caller needs, or ``None`` for methods open to any session.
    capability: str | None
    params: tuple[Param, ...]
    handler: Handler
    description: str = ""
    #: Longer than the gateway's default, for the few methods that wait for a turn.
    timeout: float | None = None


@dataclass(frozen=True)
class AuditRecord:
    time: float
    extension_id: str
    method: str
    outcome: str
    detail: str


@dataclass(frozen=True)
class RateLimit:
    """Token bucket: ``rate`` calls per second with a burst of ``burst``."""

    rate: float = 100.0
    burst: float = 200.0


class TokenBucket:
    def __init__(self, limit: RateLimit, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._clock = clock
        self._tokens = limit.burst
        self._last = clock()

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(
            self._limit.burst, self._tokens + (now - self._last) * self._limit.rate
        )
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def _check_kind(param: Param, value: Any) -> str | None:
    kind = param.kind
    if kind == "str":
        if not isinstance(value, str):
            return "must be a string"
        if param.max_length is not None and len(value) > param.max_length:
            return f"must be at most {param.max_length} characters"
        if param.pattern is not None and not param.pattern.fullmatch(value):
            return "has an invalid format"
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return "must be an integer"
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "must be a number"
        if not math.isfinite(value):
            return "must be finite"
    elif kind == "bool":
        if not isinstance(value, bool):
            return "must be true or false"
    elif kind == "object":
        if not isinstance(value, dict):
            return "must be an object"
        if param.max_length is not None and len(value) > param.max_length:
            return f"must have at most {param.max_length} entries"
    elif kind != "json":  # pragma: no cover - programming error
        return f"unknown parameter kind {kind!r}"
    if kind in ("int", "float"):
        if param.minimum is not None and value < param.minimum:
            return f"must be at least {param.minimum}"
        if param.maximum is not None and value > param.maximum:
            return f"must be at most {param.maximum}"
    if param.choices is not None and value not in param.choices:
        return f"must be one of {', '.join(map(str, param.choices))}"
    return None


def validate_params(method: ApiMethod, params: Any) -> dict[str, Any]:
    """Return the validated arguments, or raise ``INVALID_PARAMS``."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(ErrorCode.INVALID_PARAMS, "params must be an object")
    declared = {p.name: p for p in method.params}
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise RpcError(ErrorCode.INVALID_PARAMS, f"unknown parameter: {unknown[0]}")
    args: dict[str, Any] = {}
    for param in method.params:
        if param.name not in params:
            if param.required:
                raise RpcError(ErrorCode.INVALID_PARAMS, f"{param.name} is required")
            args[param.name] = param.default
            continue
        value = params[param.name]
        problem = _check_kind(param, value)
        if problem:
            raise RpcError(ErrorCode.INVALID_PARAMS, f"{param.name} {problem}")
        args[param.name] = value
    return args


@dataclass
class CallContext:
    """Who is calling, as established by the gateway."""

    extension_id: str
    manifest: Manifest
    session: Session

    @property
    def slug(self) -> str:
        return extension_slug(self.extension_id)


class EventBus:
    """Topic publish/subscribe between extensions and the Core.

    Topics are exact strings (no wildcards). Delivery never blocks the publisher:
    each subscriber has a bounded number of undelivered events and a slow or
    stuck subscriber loses events instead of stalling everyone else.
    """

    def __init__(self, max_pending_per_session: int = 100) -> None:
        self._max_pending = max_pending_per_session
        self._subscribers: dict[str, set[Session]] = {}

    def subscribe(self, session: Session, topic: str) -> None:
        self._subscribers.setdefault(topic, set()).add(session)

    def unsubscribe(self, session: Session, topic: str) -> None:
        subs = self._subscribers.get(topic)
        if subs is not None:
            subs.discard(session)
            if not subs:
                del self._subscribers[topic]

    def remove_session(self, session: Session) -> None:
        for topic in list(self._subscribers):
            self.unsubscribe(session, topic)

    def publish(self, topic: str, payload: Any) -> int:
        """Schedule delivery to every subscriber. Returns how many were scheduled."""
        scheduled = 0
        for session in list(self._subscribers.get(topic, ())):
            if session.pending_events >= self._max_pending:
                session.dropped_events += 1
                continue
            session.pending_events += 1
            task = asyncio.ensure_future(session.deliver_event(topic, payload))
            task.add_done_callback(lambda _t, s=session: s.event_done())
            scheduled += 1
        return scheduled


class Session:
    """One connected extension."""

    def __init__(
        self,
        gateway: Gateway,
        manifest: Manifest,
        reader: asyncio.StreamReader,
        writer: Any,
    ) -> None:
        self.gateway = gateway
        self.manifest = manifest
        self.hello_done = False
        self.said_bye = False
        self.pending_events = 0
        self.dropped_events = 0
        #: Free-form per-session state for API modules (for example the interfaces
        #: this extension has registered).
        self.state: dict[str, Any] = {}
        self._cleanups: list[Callable[[], Awaitable[None]]] = []
        self._bucket = TokenBucket(gateway.rate_limit, gateway.clock)
        self.peer = RpcPeer(
            reader,
            writer,
            self._dispatch,
            name=manifest.id,
            on_error=gateway.on_error,
        )

    @property
    def extension_id(self) -> str:
        return self.manifest.id

    # --------------------------------------------------------------- policy

    def allowed(self, capability: str) -> bool:
        """Declared in the manifest **and** granted right now."""
        return (
            capability in self.manifest.permissions.capabilities
            and capability in self.gateway.grants.granted(self.extension_id)
        )

    def effective_capabilities(self) -> list[str]:
        granted = self.gateway.grants.granted(self.extension_id)
        return sorted(set(self.manifest.permissions.capabilities) & granted)

    # -------------------------------------------------------------- dispatch

    async def _dispatch(self, method: str, params: Any, is_notification: bool) -> Any:
        gw = self.gateway
        if method == SESSION_HELLO:
            return self._hello(params)
        if method == SESSION_BYE:
            self.said_bye = True
            return None
        if not self.hello_done:
            gw.record(self.extension_id, method, HANDSHAKE, "call before session.hello")
            raise RpcError(ErrorCode.INVALID_REQUEST, "session.hello is required first")
        spec = gw.methods.get(method)
        if spec is None:
            gw.record(self.extension_id, method, UNKNOWN_METHOD, "")
            raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")
        if not self._bucket.take():
            gw.record(self.extension_id, method, RATE_LIMITED, "")
            raise RpcError(ErrorCode.RATE_LIMITED, "rate limit exceeded")
        if spec.capability is not None and not self.allowed(spec.capability):
            gw.record(self.extension_id, method, DENIED, spec.capability)
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"capability {spec.capability!r} is not granted",
            )
        try:
            args = validate_params(spec, params)
        except RpcError as exc:
            gw.record(self.extension_id, method, INVALID_PARAMS, exc.message)
            raise
        ctx = CallContext(self.extension_id, self.manifest, self)
        try:
            return await asyncio.wait_for(
                spec.handler(ctx, args), spec.timeout or gw.handler_timeout
            )
        except asyncio.TimeoutError:
            gw.record(self.extension_id, method, FAILED, "handler timed out")
            raise RpcError(ErrorCode.TIMEOUT, f"{method} timed out")
        except RpcError:
            raise
        except Exception as exc:
            gw.record(self.extension_id, method, FAILED, type(exc).__name__)
            raise

    def _hello(self, params: Any) -> dict[str, Any]:
        gw = self.gateway
        if self.hello_done:
            raise RpcError(ErrorCode.INVALID_REQUEST, "session.hello was already done")
        if not isinstance(params, dict):
            raise RpcError(ErrorCode.INVALID_PARAMS, "params must be an object")
        if params.get("protocol") != PROTOCOL_VERSION:
            gw.record(self.extension_id, SESSION_HELLO, HANDSHAKE, "protocol mismatch")
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"unsupported protocol (this Core speaks {PROTOCOL_VERSION})",
            )
        if params.get("extension_id") != self.extension_id:
            gw.record(self.extension_id, SESSION_HELLO, HANDSHAKE, "identity mismatch")
            asyncio.ensure_future(self.peer.close())
            raise RpcError(ErrorCode.PERMISSION_DENIED, "extension id does not match")
        self.hello_done = True
        return {
            "protocol": PROTOCOL_VERSION,
            "extension_id": self.extension_id,
            "core_api_version": gw.core_api_version,
            "granted": self.effective_capabilities(),
        }

    # ----------------------------------------------------- Core -> extension

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call a method the extension implements (hooks, lifecycle)."""
        return await self.peer.request(method, params, timeout=timeout)

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        await self.peer.notify(method, params)

    async def deliver_event(self, topic: str, payload: Any) -> None:
        try:
            await self.peer.notify(
                "events.deliver", {"topic": topic, "payload": payload}
            )
        except RpcError:
            pass

    def event_done(self) -> None:
        self.pending_events = max(0, self.pending_events - 1)

    # ------------------------------------------------------------ life cycle

    def on_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run ``callback`` when this session ends, however it ends.

        Used to undo what the extension set up in the Core (registered
        interfaces, subscriptions) so nothing outlives its process.
        """
        self._cleanups.append(callback)

    async def serve(self) -> None:
        """Handle the connection until it closes."""
        try:
            await self.peer.serve()
        finally:
            try:
                await self._run_cleanups()
            finally:
                self.gateway._detach(self)  # noqa: SLF001

    async def _run_cleanups(self) -> None:
        callbacks, self._cleanups = self._cleanups[::-1], []
        for callback in callbacks:
            try:
                await callback()
            except Exception as exc:  # noqa: BLE001 - one failing cleanup must not skip the rest
                if self.gateway.on_error is not None:
                    self.gateway.on_error(f"cleanup:{self.extension_id}", exc)

    async def close(self) -> None:
        await self.peer.close()


class Gateway:
    """Registry of API methods plus the sessions of connected extensions."""

    def __init__(
        self,
        *,
        grants: GrantStore,
        core_api_version: str = CORE_API_VERSION,
        rate_limit: RateLimit | None = None,
        handler_timeout: float = 10.0,
        audit_size: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        on_error: Callable[[str, BaseException], None] | None = None,
        on_audit: Callable[[AuditRecord], None] | None = None,
    ) -> None:
        self.grants = grants
        self.core_api_version = core_api_version
        self.rate_limit = rate_limit or RateLimit()
        self.handler_timeout = handler_timeout
        self.clock = clock
        self.on_error = on_error
        self._on_audit = on_audit
        self.methods: dict[str, ApiMethod] = {}
        self.sessions: dict[str, Session] = {}
        self.events = EventBus()
        #: Called with every new session, so API modules can attach cleanup.
        self.on_attach: list[Callable[[Session], None]] = []
        self.audit: deque[AuditRecord] = deque(maxlen=audit_size)

    def register(self, method: ApiMethod) -> None:
        if method.name in self.methods:
            raise ValueError(f"API method {method.name!r} is already registered")
        if method.name.startswith("session."):
            raise ValueError("the session.* namespace is reserved")
        self.methods[method.name] = method

    def record(self, extension_id: str, method: str, outcome: str, detail: str) -> None:
        record = AuditRecord(time.time(), extension_id, method, outcome, detail)
        self.audit.append(record)
        if self._on_audit is not None:
            try:
                self._on_audit(record)
            except Exception:  # noqa: BLE001 - auditing must never break a call
                pass

    def attach(
        self, manifest: Manifest, reader: asyncio.StreamReader, writer: Any
    ) -> Session:
        """Connect an extension process. One session per extension id."""
        if manifest.id in self.sessions:
            raise ValueError(f"{manifest.id} is already connected")
        session = Session(self, manifest, reader, writer)
        self.sessions[manifest.id] = session
        for hook in self.on_attach:
            hook(session)
        return session

    def _detach(self, session: Session) -> None:
        self.events.remove_session(session)
        if self.sessions.get(session.extension_id) is session:
            del self.sessions[session.extension_id]

    async def close_all(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
