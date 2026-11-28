"""A Live engine, written as an extension (``provides.engines = ["live"]``).

A Live engine is a full-duplex conversation with a model: the caller's sound goes in, the
model's sound comes out, the model may call tools and may be interrupted. The Core owns the
*conversation* (the persona, the tools, the history, the call); you own the *model
connection*. Subclass :class:`LiveEngine`, give it to ``await ctx.live.serve(name, engine)``
and it appears in the Core's Live selector.

The Core opens a session with a neutral description (:class:`LiveSession`): the system
instruction, the tools as ``{"name", "description", "parameters": [{"name", "type",
"description", "required", "enum", "schema"}]}`` (turn them into your provider's function
declarations), the voice and the language the operator chose, and the handle of an earlier
session to resume, if there is one. From then on:

* what the caller says reaches :meth:`LiveEngine.audio` (16-bit mono PCM, 16 kHz);
* what the model does you report on the session: :meth:`LiveSession.audio`,
  :meth:`LiveSession.transcript`, :meth:`LiveSession.tool_call`,
  :meth:`LiveSession.turn_complete`, :meth:`LiveSession.interrupted`,
  :meth:`LiveSession.state`, :meth:`LiveSession.error`;
* the answer to a tool call arrives at :meth:`LiveEngine.tool_result`.

Reconnecting to the provider, resuming after its go-away and the provider's own session
limits are yours: report ``state("reconnecting")`` and ``state("connected")`` and the Core
leaves the call as it is. Report ``state("closed")`` only when the session is over.

Standard library only: it never imports ``core``.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: ``await emit(params)`` sends one ``live.event`` to the Core.
Emit = Callable[[dict[str, Any]], Awaitable[Any]]

#: Audio below this many milliseconds is held back and sent with the next chunk, so a model
#: that streams tiny chunks does not flood the protocol (it allows about 100 events a second).
COALESCE_MS = 100
#: One ``live.event`` carries at most this many bytes of PCM.
MAX_CHUNK = 200_000


@dataclass
class LiveSession:
    """One session the Core opened, and the way to report what the model does in it."""

    session_id: str
    system_instruction: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    voice: str = ""
    language: str = ""
    resume_handle: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    _emit: Emit | None = field(default=None, repr=False)
    _held: bytearray = field(default_factory=bytearray, repr=False)
    _rate: int = 24000

    async def _send(self, **params: Any) -> None:
        if self._emit is None:
            return
        await self._emit({"session_id": self.session_id, **params})

    async def audio(self, pcm: bytes, rate: int = 24000) -> None:
        """The model speaks: 16-bit mono PCM at ``rate``. Chunks are coalesced to about
        100 ms and sent in order."""
        if not pcm:
            return
        if self._held and rate != self._rate:
            await self.flush_audio()
        self._rate = rate
        self._held.extend(pcm)
        if len(self._held) >= rate * 2 * COALESCE_MS // 1000:
            await self.flush_audio()

    async def flush_audio(self) -> None:
        """Send the audio held back. Called for you before any other event."""
        held, self._held = bytes(self._held), bytearray()
        for start in range(0, len(held), MAX_CHUNK):
            part = held[start : start + MAX_CHUNK]
            part = part[: len(part) - len(part) % 2]
            if part:
                await self._send(
                    type="audio",
                    pcm=base64.b64encode(part).decode("ascii"),
                    rate=self._rate,
                )

    async def transcript(
        self, text: str, side: str = "output", final: bool = True
    ) -> None:
        """What was said: ``side`` is ``"input"`` for the caller, ``"output"`` for the model."""
        if text.strip():
            await self.flush_audio()
            await self._send(type="transcript", text=text, side=side, final=final)

    async def tool_call(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        """The model asks for an action. The result comes back to
        :meth:`LiveEngine.tool_result`."""
        await self.flush_audio()
        await self._send(type="tool_call", call_id=call_id, name=name, args=args)

    async def turn_complete(self) -> None:
        """The model finished answering."""
        await self.flush_audio()
        await self._send(type="turn_complete")

    async def interrupted(self) -> None:
        """The caller spoke over the model: the Core drops what is queued to play."""
        self._held.clear()
        await self._send(type="interrupted")

    async def state(self, state: str, resume_handle: str = "") -> None:
        """``"connected"``, ``"reconnecting"`` or ``"closed"``; a ``resume_handle`` lets the
        Core open the next session where this one stopped."""
        await self.flush_audio()
        await self._send(type="state", state=state, resume_handle=resume_handle)

    async def error(self, detail: str) -> None:
        """Something went wrong. The Core gives the call up after a few in a row."""
        await self.flush_audio()
        await self._send(type="error", detail=detail[:500])


class LiveEngine:
    """Base class of a Live engine. Override what your provider supports."""

    #: Shown in the Core's Live selector.
    label: str = ""
    #: PCM rates you take and give (the Core resamples nothing: say what you use).
    input_rate: int = 16000
    output_rate: int = 24000
    #: The longest session your provider holds, in seconds; ``0`` for no limit.
    max_session_seconds: int = 0

    async def open(self, session: LiveSession) -> None:
        """Connect to the model for ``session``. Raise to refuse (the call then goes on
        the classic path)."""
        raise NotImplementedError

    async def audio(self, session_id: str, pcm: bytes, rate: int) -> None:
        """What the caller says (16-bit mono PCM at ``rate``)."""

    async def text(self, session_id: str, text: str, kind: str) -> None:
        """A line for the model. ``kind`` is ``"context"`` (it should know this) or
        ``"turn"`` (it should answer this)."""

    async def tool_result(
        self, session_id: str, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        """The outcome of a tool call the model made."""

    async def close(self, session_id: str) -> None:
        """The Core ended the session: disconnect from the model."""
