"""Model-agnostic Live tool executor.

Receives ``TOOL_CALL`` events from a Live engine's receive stream,
dispatches them to the SyntH action pipeline via ``run_action``, and
sends the result back through the engine's ``send_tool_response`` method.

This executor is intentionally engine-agnostic: it works with any
``LiveEngineBase`` subclass that implements ``send_tool_response``.

Usage (inside a receive loop)::

    executor = LiveToolExecutor(engine=my_engine, timeout=10.0)

    async for event in engine.receive_events(session_id):
        if event.type == LiveEventType.TOOL_CALL:
            await executor.handle(session_id, event, context, bot)
        elif event.type == LiveEventType.AUDIO:
            ...

Multi-call batching
-------------------
Gemini 3.1 emits tool calls one at a time (sequential only).  Gemini 2.5
may emit multiple function calls in a single event — the executor runs
them sequentially by default; pass ``concurrent=True`` to run them with
``asyncio.gather`` (only safe on engines that support async / NON_BLOCKING
function calling).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.logging_utils import log_error, log_info, log_warning
from plugins.live_base import LiveEngineBase, LiveEvent, LiveEventType

logger = logging.getLogger(__name__)

# Default per-tool timeout in seconds.  Prevents a stalled action from
# freezing the entire live session (especially on Gemini 3.1 which blocks
# the model until a response is sent).
_DEFAULT_TIMEOUT_S = 15.0


class LiveToolExecutor:
    """Dispatches ``TOOL_CALL`` events to the SyntH action pipeline.

    Args:
        engine:    The live engine whose ``send_tool_response`` will be
                   called with the result.
        timeout:   Per-tool execution timeout in seconds.  A ``TimeoutError``
                   is caught, logged, and an error response is sent back.
        concurrent: If ``True``, multiple tool calls in a single event are
                    run concurrently via ``asyncio.gather``.  Only safe on
                    engines that support async (NON_BLOCKING) function
                    calling.  Defaults to ``False`` (sequential).
    """

    def __init__(
        self,
        engine: LiveEngineBase,
        timeout: float = _DEFAULT_TIMEOUT_S,
        concurrent: bool = False,
        bot: Any = None,
    ) -> None:
        self._engine = engine
        self._timeout = timeout
        self._concurrent = concurrent
        # Optional bot/client instance forwarded to run_action.  Can also be
        # supplied per-call via context["bot"].
        self._bot = bot

    async def handle(
        self,
        session_id: str,
        event: LiveEvent,
        context: dict[str, Any],
        bot: Any,
    ) -> None:
        """Execute the tool described by a ``TOOL_CALL`` event.

        Should be called from within the engine's receive loop whenever an
        event of type ``TOOL_CALL`` is yielded.

        Args:
            session_id: The live session that owns this call.
            event:      The ``TOOL_CALL`` ``LiveEvent``.
            context:    Context dict forwarded to ``run_action``
                        (e.g. ``{"source": "live_voice", "guild_id": ...}``).
            bot:        Bot/client instance forwarded to ``run_action``.
        """
        if event.type is not LiveEventType.TOOL_CALL or event.tool_call is None:
            return

        tc = event.tool_call
        log_info(
            f"[live_tool_executor] Tool call in session {session_id!r}: "
            f"{tc.name}({tc.args})"
        )
        await self._execute_one(session_id, tc.call_id, tc.name, tc.args, context, bot)

    async def handle_batch(
        self,
        session_id: str,
        events: list[LiveEvent],
        context: dict[str, Any],
        bot: Any,
    ) -> None:
        """Execute a batch of ``TOOL_CALL`` events from a single model turn.

        Args:
            session_id: The live session.
            events:     All ``TOOL_CALL`` events from the turn.
            context:    Forwarded to ``run_action``.
            bot:        Forwarded to ``run_action``.
        """
        tool_events = [
            e for e in events if e.type is LiveEventType.TOOL_CALL and e.tool_call
        ]
        if not tool_events:
            return

        if self._concurrent and len(tool_events) > 1:
            await asyncio.gather(
                *(
                    self._execute_one(
                        session_id,
                        e.tool_call.call_id,  # type: ignore[union-attr]
                        e.tool_call.name,  # type: ignore[union-attr]
                        e.tool_call.args,  # type: ignore[union-attr]
                        context,
                        bot,
                    )
                    for e in tool_events
                )
            )
        else:
            for e in tool_events:
                await self.handle(session_id, e, context, bot)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_one(
        self,
        session_id: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
        context: dict[str, Any],
        bot: Any,
    ) -> None:
        """Run a single tool and send its response back through the engine."""
        from core.action_parser import run_action

        action: dict[str, Any] = {"type": name, "payload": args}
        result: dict[str, Any]

        # Priority: call-site bot > constructor bot > context["bot"]
        effective_bot = (
            bot
            if bot is not None
            else (self._bot if self._bot is not None else context.get("bot"))
        )

        try:
            raw = await asyncio.wait_for(
                run_action(action, context, effective_bot, None),
                timeout=self._timeout,
            )
            result = raw if isinstance(raw, dict) else {"status": "ok"}
        except asyncio.TimeoutError:
            log_warning(
                f"[live_tool_executor] Tool {name!r} timed out after "
                f"{self._timeout}s in session {session_id!r}"
            )
            result = {
                "status": "error",
                "message": f"Tool execution timed out after {self._timeout}s",
            }
        except Exception as exc:
            log_error(
                f"[live_tool_executor] Tool {name!r} raised in session "
                f"{session_id!r}: {exc}"
            )
            result = {"status": "error", "message": str(exc)}

        try:
            await self._engine.send_tool_response(session_id, call_id, name, result)
            log_info(
                f"[live_tool_executor] Sent response for {name!r} "
                f"(session {session_id!r}): {result.get('status', '?')}"
            )
        except Exception as exc:
            log_error(
                f"[live_tool_executor] Failed to send tool response for "
                f"{name!r} in session {session_id!r}: {exc}"
            )
