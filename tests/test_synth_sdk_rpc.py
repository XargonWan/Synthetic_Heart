"""Tests for ``synth_sdk.rpc``: the extension wire protocol (platform milestone M2)."""

from __future__ import annotations

import asyncio
import json
import weakref
from typing import Any

import pytest

from synth_sdk.rpc import (
    MAX_MESSAGE_BYTES,
    MAX_VIOLATIONS,
    ErrorCode,
    RpcError,
    RpcPeer,
    decode_message,
    encode_message,
)


class PipeWriter:
    """A writer that feeds the reader of the other end of an in-memory pipe."""

    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target
        self.closed = False
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionResetError("closed")
        self.sent.extend(data)
        self._target.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._target.feed_eof()


_WRITERS: weakref.WeakKeyDictionary[RpcPeer, PipeWriter] = weakref.WeakKeyDictionary()


def sent_by(peer: RpcPeer) -> bytes:
    """Everything ``peer`` wrote to the other end."""
    return bytes(_WRITERS[peer].sent)


def make_pair(
    handler_a: Any = None,
    handler_b: Any = None,
    **kwargs: Any,
) -> tuple[RpcPeer, RpcPeer, asyncio.StreamReader, asyncio.StreamReader]:
    """Two connected peers. Also returns the two readers for raw injection."""
    reader_a = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    reader_b = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)

    async def refuse(method: str, params: Any, notification: bool) -> Any:
        raise RpcError(ErrorCode.METHOD_NOT_FOUND, method)

    writer_a, writer_b = PipeWriter(reader_b), PipeWriter(reader_a)
    a = RpcPeer(reader_a, writer_a, handler_a or refuse, name="a", **kwargs)
    b = RpcPeer(reader_b, writer_b, handler_b or refuse, name="b", **kwargs)
    _WRITERS[a], _WRITERS[b] = writer_a, writer_b
    return a, b, reader_a, reader_b


async def start(*peers: RpcPeer) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(p.serve()) for p in peers]


class TestEncodeDecode:
    def test_round_trip(self) -> None:
        message = {"jsonrpc": "2.0", "id": 1, "method": "m", "params": {"k": [1, "é"]}}
        data = encode_message(message)
        assert data.endswith(b"\n") and data.count(b"\n") == 1
        assert decode_message(data) == message

    def test_newlines_in_strings_stay_on_one_line(self) -> None:
        data = encode_message(
            {"jsonrpc": "2.0", "method": "m", "params": {"t": "a\nb"}}
        )
        assert data.count(b"\n") == 1

    def test_nan_is_refused(self) -> None:
        with pytest.raises(RpcError):
            encode_message(
                {"jsonrpc": "2.0", "method": "m", "params": {"x": float("nan")}}
            )

    def test_unserialisable_is_refused(self) -> None:
        with pytest.raises(RpcError):
            encode_message({"jsonrpc": "2.0", "method": "m", "params": {"x": object()}})

    def test_oversized_message_is_refused(self) -> None:
        big = {
            "jsonrpc": "2.0",
            "method": "m",
            "params": {"x": "a" * MAX_MESSAGE_BYTES},
        }
        with pytest.raises(RpcError) as info:
            encode_message(big)
        assert info.value.code == ErrorCode.INVALID_REQUEST

    @pytest.mark.parametrize(
        "line",
        [
            b"not json\n",
            b"[]\n",
            b"[{}]\n",
            b'"text"\n',
            b"{}\n",
            b'{"jsonrpc":"1.0","method":"m"}\n',
            b'{"jsonrpc":"2.0"}\n',
            b'{"jsonrpc":"2.0","method":"m","result":1}\n',
            b'{"jsonrpc":"2.0","method":""}\n',
            b'{"jsonrpc":"2.0","method":5}\n',
            b'{"jsonrpc":"2.0","method":"m","params":5}\n',
            b'{"jsonrpc":"2.0","method":"m","id":true}\n',
            b'{"jsonrpc":"2.0","result":1}\n',
            b'{"jsonrpc":"2.0","id":1,"result":1,"error":{"code":1,"message":"x"}}\n',
            b"\xff\xfe\n",
        ],
    )
    def test_invalid_messages_are_rejected(self, line: bytes) -> None:
        with pytest.raises(RpcError) as info:
            decode_message(line)
        assert info.value.code in (ErrorCode.PARSE_ERROR, ErrorCode.INVALID_REQUEST)

    def test_deeply_nested_json_does_not_crash(self) -> None:
        line = b"[" * 100000 + b"]" * 100000 + b"\n"
        with pytest.raises(RpcError):
            decode_message(line)


class TestRequests:
    @pytest.mark.asyncio
    async def test_request_and_response(self) -> None:
        async def handler(method: str, params: Any, notification: bool) -> Any:
            assert method == "echo"
            return {"got": params}

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        assert await a.request("echo", {"x": 1}) == {"got": {"x": 1}}
        await a.close()
        await b.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_both_sides_can_call_each_other(self) -> None:
        async def on_a(method: str, params: Any, notification: bool) -> Any:
            return "from-a"

        async def on_b(method: str, params: Any, notification: bool) -> Any:
            return "from-b"

        a, b, *_ = make_pair(on_a, on_b)
        tasks = await start(a, b)
        assert await a.request("x") == "from-b"
        assert await b.request("x") == "from-a"
        await a.close()
        await b.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_error_code_and_message_cross_the_pipe(self) -> None:
        async def handler(method: str, params: Any, notification: bool) -> Any:
            raise RpcError(ErrorCode.PERMISSION_DENIED, "nope", {"why": "test"})

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        with pytest.raises(RpcError) as info:
            await a.request("secret")
        assert info.value.code == ErrorCode.PERMISSION_DENIED
        assert info.value.message == "nope"
        assert info.value.data == {"why": "test"}
        await a.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_generic_and_not_leaked(self) -> None:
        seen: list[tuple[str, BaseException]] = []

        async def handler(method: str, params: Any, notification: bool) -> Any:
            raise ValueError("secret internal detail /etc/passwd")

        a, b, *_ = make_pair(
            handler_b=handler, on_error=lambda m, e: seen.append((m, e))
        )
        tasks = await start(a, b)
        with pytest.raises(RpcError) as info:
            await a.request("boom")
        assert info.value.code == ErrorCode.INTERNAL_ERROR
        assert "secret" not in info.value.message and "passwd" not in info.value.message
        assert seen and seen[0][0] == "boom"
        await a.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_notification_is_delivered_and_not_answered(self) -> None:
        received: list[tuple[str, Any, bool]] = []
        got = asyncio.Event()

        async def handler(method: str, params: Any, notification: bool) -> Any:
            received.append((method, params, notification))
            got.set()
            return "ignored"

        a, b, reader_a, _ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        await a.notify("ping", {"n": 1})
        await asyncio.wait_for(got.wait(), 2)
        assert received == [("ping", {"n": 1}, True)]
        await asyncio.sleep(0.05)
        assert sent_by(b) == b""  # the receiver sent nothing back
        await a.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_timeout_and_late_answer_is_ignored(self) -> None:
        release = asyncio.Event()

        async def handler(method: str, params: Any, notification: bool) -> Any:
            await release.wait()
            return "late"

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        with pytest.raises(RpcError) as info:
            await a.request("slow", timeout=0.05)
        assert info.value.code == ErrorCode.TIMEOUT
        release.set()
        await asyncio.sleep(0.05)
        assert not a.closed  # a late response did not break the connection
        await a.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_concurrent_requests_get_their_own_answers(self) -> None:
        async def handler(method: str, params: Any, notification: bool) -> Any:
            await asyncio.sleep(0.05 if params["n"] % 2 else 0)
            return params["n"]

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        results = await asyncio.gather(*(a.request("n", {"n": i}) for i in range(10)))
        assert results == list(range(10))
        await a.close()
        await asyncio.gather(*tasks)


class TestClosingAndLimits:
    @pytest.mark.asyncio
    async def test_end_of_stream_closes_and_fails_pending_requests(self) -> None:
        release = asyncio.Event()

        async def handler(method: str, params: Any, notification: bool) -> Any:
            await release.wait()

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        pending = asyncio.create_task(a.request("hang", timeout=5))
        await asyncio.sleep(0.05)
        await b.close()  # the other end goes away
        with pytest.raises(RpcError) as info:
            await pending
        assert info.value.code == ErrorCode.UNAVAILABLE
        await asyncio.wait_for(a.wait_closed(), 2)
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_request_after_close_fails_fast(self) -> None:
        a, b, *_ = make_pair()
        tasks = await start(a, b)
        await a.close()
        with pytest.raises(RpcError) as info:
            await a.request("x")
        assert info.value.code == ErrorCode.UNAVAILABLE
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_oversized_line_closes_the_connection(self) -> None:
        a, b, reader_a, _ = make_pair()
        task = asyncio.create_task(a.serve())
        reader_a.feed_data(b"x" * (MAX_MESSAGE_BYTES + 100) + b"\n")
        await asyncio.wait_for(a.wait_closed(), 2)
        assert "larger than the limit" in a.close_reason
        await task

    @pytest.mark.asyncio
    async def test_truncated_message_at_end_of_stream(self) -> None:
        a, b, reader_a, _ = make_pair()
        task = asyncio.create_task(a.serve())
        reader_a.feed_data(b'{"jsonrpc":"2.0","method":"x"')
        reader_a.feed_eof()
        await asyncio.wait_for(a.wait_closed(), 2)
        assert "middle of a message" in a.close_reason
        await task

    @pytest.mark.asyncio
    async def test_garbage_is_answered_then_the_connection_is_closed(self) -> None:
        a, b, reader_a, reader_b = make_pair()
        task = asyncio.create_task(a.serve())
        for _ in range(MAX_VIOLATIONS):
            reader_a.feed_data(b"garbage\n")
        await asyncio.wait_for(a.wait_closed(), 2)
        assert "violations" in a.close_reason
        replies = sent_by(a).splitlines()
        assert (
            replies and json.loads(replies[0])["error"]["code"] == ErrorCode.PARSE_ERROR
        )
        await task

    @pytest.mark.asyncio
    async def test_a_valid_message_resets_the_violation_count(self) -> None:
        async def handler(method: str, params: Any, notification: bool) -> Any:
            return "ok"

        a, b, reader_a, _ = make_pair(handler_a=handler)
        task = asyncio.create_task(a.serve())
        for _ in range(MAX_VIOLATIONS - 1):
            reader_a.feed_data(b"garbage\n")
        reader_a.feed_data(b'{"jsonrpc":"2.0","method":"x"}\n')
        for _ in range(MAX_VIOLATIONS - 1):
            reader_a.feed_data(b"garbage\n")
        await asyncio.sleep(0.1)
        assert not a.closed
        await a.close()
        await task

    @pytest.mark.asyncio
    async def test_too_many_calls_in_progress_are_refused(self) -> None:
        release = asyncio.Event()

        async def handler(method: str, params: Any, notification: bool) -> Any:
            await release.wait()
            return "done"

        a, b, *_ = make_pair(handler_b=handler, max_concurrent_handlers=2)
        tasks = await start(a, b)
        first = [asyncio.create_task(a.request("w", timeout=5)) for _ in range(2)]
        await asyncio.sleep(0.05)
        with pytest.raises(RpcError) as info:
            await a.request("w", timeout=2)
        assert info.value.code == ErrorCode.RATE_LIMITED
        release.set()
        assert await asyncio.gather(*first) == ["done", "done"]
        await a.close()
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_closing_cancels_running_handlers(self) -> None:
        cancelled = asyncio.Event()

        async def handler(method: str, params: Any, notification: bool) -> Any:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        a, b, *_ = make_pair(handler_b=handler)
        tasks = await start(a, b)
        await a.notify("long")
        await asyncio.sleep(0.05)
        await b.close()
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.gather(*tasks)
