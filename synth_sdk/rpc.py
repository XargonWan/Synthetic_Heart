"""JSON-RPC 2.0 over newline-delimited JSON: the extension wire protocol.

One message per line, UTF-8, ``\\n`` terminated. Both ends of the extension pipe
use this module: the Core gateway and the extension process (through the SDK).
Standard library only, so it can be loaded inside the sandboxed process without
anything else from the repository.

Properties that matter for security:

* a message larger than :data:`MAX_MESSAGE_BYTES` closes the connection (a peer
  cannot make the other side buffer without limit);
* malformed input is answered with an error and counted; too many violations
  close the connection;
* handler exceptions are turned into a generic error: no traceback, no internal
  detail crosses the pipe;
* the number of requests being handled at the same time is bounded.

Batches (JSON arrays) are not supported.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping
from enum import IntEnum
from typing import Any, Protocol

PROTOCOL_VERSION = 1
#: Upper bound for one encoded message, including the trailing newline.
MAX_MESSAGE_BYTES = 1024 * 1024
#: Consecutive protocol violations tolerated before the connection is closed.
MAX_VIOLATIONS = 5


class ErrorCode(IntEnum):
    """JSON-RPC error codes. ``-32000..-32099`` is the application range."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_ERROR = -32000
    PERMISSION_DENIED = -32001
    RATE_LIMITED = -32002
    QUOTA_EXCEEDED = -32003
    TIMEOUT = -32004
    UNAVAILABLE = -32005


class RpcError(Exception):
    """An error that is sent to (or received from) the other side."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.data = data

    def to_object(self) -> dict[str, Any]:
        obj: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            obj["data"] = self.data
        return obj

    @classmethod
    def from_object(cls, obj: Any) -> RpcError:
        if (
            isinstance(obj, Mapping)
            and isinstance(obj.get("code"), int)
            and isinstance(obj.get("message"), str)
        ):
            return cls(obj["code"], obj["message"], obj.get("data"))
        return cls(ErrorCode.INTERNAL_ERROR, "malformed error object")

    def __repr__(self) -> str:
        return f"RpcError({self.code}, {self.message!r})"


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Serialise one message to a single ``\\n``-terminated line."""
    try:
        text = json.dumps(
            message, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise RpcError(
            ErrorCode.INTERNAL_ERROR, f"message is not JSON-serialisable: {exc}"
        )
    data = text.encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise RpcError(
            ErrorCode.INVALID_REQUEST,
            f"message is {len(data)} bytes, the limit is {MAX_MESSAGE_BYTES}",
        )
    return data


def decode_message(line: bytes) -> dict[str, Any]:
    """Parse and shape-check one line.

    Raises :class:`RpcError` (``PARSE_ERROR`` or ``INVALID_REQUEST``).
    """
    if len(line) > MAX_MESSAGE_BYTES:
        raise RpcError(ErrorCode.INVALID_REQUEST, "message too large")
    try:
        obj = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise RpcError(ErrorCode.PARSE_ERROR, "not valid JSON")
    if not isinstance(obj, dict):
        raise RpcError(ErrorCode.INVALID_REQUEST, "a message must be a JSON object")
    if obj.get("jsonrpc") != "2.0":
        raise RpcError(ErrorCode.INVALID_REQUEST, "missing jsonrpc 2.0 marker")
    has_method = "method" in obj
    has_outcome = "result" in obj or "error" in obj
    if has_method == has_outcome:
        raise RpcError(
            ErrorCode.INVALID_REQUEST, "a message is either a call or a response"
        )
    if has_method:
        if not isinstance(obj["method"], str) or not obj["method"]:
            raise RpcError(ErrorCode.INVALID_REQUEST, "method must be a string")
        params = obj.get("params")
        if params is not None and not isinstance(params, (dict, list)):
            raise RpcError(
                ErrorCode.INVALID_REQUEST, "params must be an object or list"
            )
        if "id" in obj and not _valid_id(obj["id"]):
            raise RpcError(ErrorCode.INVALID_REQUEST, "id must be an integer or string")
    else:
        if "id" not in obj or not _valid_id(obj["id"]):
            raise RpcError(ErrorCode.INVALID_REQUEST, "a response needs an id")
        if "result" in obj and "error" in obj:
            raise RpcError(ErrorCode.INVALID_REQUEST, "a response has result or error")
    return obj


def _valid_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


#: ``handler(method, params, is_notification)`` returns the result of a request.
Handler = Callable[[str, Any, bool], Awaitable[Any]]


class RpcPeer:
    """One end of a bidirectional JSON-RPC connection.

    Both sides can send requests and notifications; both answer the other's.
    Create it with a connected ``reader``/``writer`` pair and run :meth:`serve`.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: _Writer,
        handler: Handler,
        *,
        name: str = "peer",
        max_concurrent_handlers: int = 32,
        request_timeout: float = 30.0,
        on_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._handler = handler
        self.name = name
        self._max_handlers = max_concurrent_handlers
        self._request_timeout = request_timeout
        self._on_error = on_error
        self._ids = itertools.count(1)
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._violations = 0
        self.close_reason = ""

    # ------------------------------------------------------------------ state

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # ---------------------------------------------------------------- sending

    async def _send(self, message: Mapping[str, Any]) -> None:
        data = encode_message(message)
        if self.closed:
            raise RpcError(ErrorCode.UNAVAILABLE, "connection is closed")
        async with self._write_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
            except (ConnectionError, BrokenPipeError, OSError, RuntimeError) as exc:
                self._close(f"write failed: {exc}")
                raise RpcError(ErrorCode.UNAVAILABLE, "connection is closed")

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call a method on the other side and return its result."""
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        try:
            await self._send(message)
            return await asyncio.wait_for(
                future, timeout if timeout is not None else self._request_timeout
            )
        except asyncio.TimeoutError:
            raise RpcError(ErrorCode.TIMEOUT, f"{method} timed out")
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        """Send a notification: no answer is expected or possible."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._send(message)

    # -------------------------------------------------------------- receiving

    async def serve(self) -> None:
        """Read messages until the other side closes or violates the protocol."""
        closed_waiter = asyncio.ensure_future(self._closed.wait())
        read: asyncio.Future[bytes] | None = None
        try:
            while not self.closed:
                read = asyncio.ensure_future(self._reader.readline())
                await asyncio.wait(
                    {read, closed_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if not read.done():
                    # Closed locally while waiting for input: stop reading.
                    read.cancel()
                    break
                try:
                    line = read.result()
                except (ValueError, asyncio.LimitOverrunError):
                    self._close("message larger than the limit")
                    break
                except (ConnectionError, OSError) as exc:
                    self._close(f"read failed: {exc}")
                    break
                if not line:
                    self._close("end of stream")
                    break
                if not line.endswith(b"\n"):
                    self._close("stream ended in the middle of a message")
                    break
                await self._on_line(line)
        finally:
            closed_waiter.cancel()
            if read is not None and not read.done():
                read.cancel()
            self._close(self.close_reason or "serve stopped")
            await self._cancel_tasks()

    async def _on_line(self, line: bytes) -> None:
        try:
            message = decode_message(line)
        except RpcError as exc:
            self._violations += 1
            await self._reply_error(None, exc)
            if self._violations >= MAX_VIOLATIONS:
                self._close("too many protocol violations")
            return
        self._violations = 0
        if "method" in message:
            await self._on_call(message)
        else:
            self._on_response(message)

    def _on_response(self, message: Mapping[str, Any]) -> None:
        future = self._pending.get(message["id"])
        if future is None or future.done():
            return  # late answer to a request that already timed out
        if "error" in message:
            future.set_exception(RpcError.from_object(message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _on_call(self, message: Mapping[str, Any]) -> None:
        is_notification = "id" not in message
        if len(self._tasks) >= self._max_handlers:
            if not is_notification:
                await self._reply_error(
                    message["id"],
                    RpcError(ErrorCode.RATE_LIMITED, "too many calls in progress"),
                )
            return
        task = asyncio.create_task(self._run_handler(message, is_notification))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_handler(
        self, message: Mapping[str, Any], is_notification: bool
    ) -> None:
        method = message["method"]
        params = message.get("params")
        request_id = message.get("id")
        try:
            result = await self._handler(method, params, is_notification)
        except RpcError as exc:
            if not is_notification:
                await self._reply_error(request_id, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing may leak across the pipe
            if self._on_error is not None:
                self._on_error(method, exc)
            if not is_notification:
                await self._reply_error(
                    request_id, RpcError(ErrorCode.INTERNAL_ERROR, "internal error")
                )
            return
        if is_notification:
            return
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RpcError as exc:
            if self.closed:
                return
            await self._reply_error(request_id, exc)

    async def _reply_error(self, request_id: Any, error: RpcError) -> None:
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "error": error.to_object()}
            )
        except RpcError:
            pass

    # ---------------------------------------------------------------- closing

    def _close(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self.close_reason = reason
        self._closed.set()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(
                    RpcError(ErrorCode.UNAVAILABLE, "connection is closed")
                )
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass

    async def _cancel_tasks(self) -> None:
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """Close the connection and stop handling calls."""
        self._close("closed locally")
        await self._cancel_tasks()
