"""Turns an extension starts on its own (capability ``turns.request``).

Most turns begin with a message somebody sent (``interfaces.inbound``). Some begin
with the extension: a radio that has just changed track wants a remark, a monitor
wants a summary. ``turns.request`` asks the Core to run one *self-initiated turn* on
one of the extension's own interfaces:

* ``text`` is what the model is asked, as if said by the interface itself;
* the turn is **background work**: it is not written to the chat history, yields to
  people, and is scoped to ``allowed_actions`` (actions of this very interface, at
  least one), so the model cannot use it to reach anything else;
* ``priority`` picks one of a few named bands, never above ordinary chat by more than
  one step (``elevated``), so an extension cannot jump ahead of a person or the trainer;
* ``context`` (a small JSON object) is handed back to the extension, untouched, when
  the model answers with one of its actions (``interface.action``): the way a request
  and its answer meet again without the extension keeping a table of pending turns.

The extension is rate-limited here on top of the gateway's own limit, because every
turn costs a model call.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Protocol

from synth_sdk.paths import match_path, path_problem
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param
from core.extensions.interfaces import CONTEXT_KEY, _owned, turn_context_for  # noqa: F401

CAP_TURNS = "turns.request"
#: Named bands, lowest first. ``elevated`` is one step above ordinary chat.
PRIORITIES = ("background", "low", "ambient", "general", "elevated")
MAX_TEXT = 8000
MAX_CONTEXT_BYTES = 4096
MAX_HISTORY = 50
#: Requests one extension may make per window, and the window (seconds).
RATE_LIMIT = 20
RATE_WINDOW = 60.0


class TurnPort(Protocol):
    async def request(
        self,
        *,
        extension_id: str,
        interface_id: str,
        path: str,
        text: str,
        sender_name: str,
        priority: str,
        context: dict[str, Any],
        allowed_actions: list[str],
        history_recent_max: int,
    ) -> None: ...


def register_turn_api(
    gateway: Gateway, port: TurnPort, *, clock: Any = time.monotonic
) -> None:
    """Register ``turns.request`` on the gateway."""
    recent: dict[str, deque[float]] = {}

    async def request(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        interface_id = args["interface_id"]
        remote = _owned(ctx.session).get(interface_id)
        if remote is None:
            gateway.record(ctx.extension_id, "turns.request", "denied", interface_id)
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"{interface_id!r} is not an interface you registered",
            )
        path = args["path"]
        problem = path_problem(path)
        if problem or path.split("/", 1)[0] != interface_id:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"path {problem}"
                if problem
                else "path must start with the interface id",
            )
        if not match_path(path, remote.spec.patterns):
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "path does not match a pattern declared in the manifest",
            )
        if not args["text"].strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "text must not be empty")
        raw_allowed = args["allowed_actions"]
        if (
            not isinstance(raw_allowed, list)
            or not raw_allowed
            or not all(isinstance(a, str) for a in raw_allowed)
        ):
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "allowed_actions must list at least one action of this interface",
            )
        allowed: list[str] = [str(a) for a in raw_allowed]
        own = set(remote.spec.actions) | {"send_message"}
        stray = sorted(set(allowed) - own)
        if stray:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"not actions of {interface_id!r}: {', '.join(stray)}",
            )
        context = args["context"]
        try:
            size = len(json.dumps(context, separators=(",", ":"), allow_nan=False))
        except (TypeError, ValueError):
            raise RpcError(ErrorCode.INVALID_PARAMS, "context is not valid JSON")
        if size > MAX_CONTEXT_BYTES:
            raise RpcError(
                ErrorCode.QUOTA_EXCEEDED,
                f"context is larger than {MAX_CONTEXT_BYTES} bytes",
            )
        now = clock()
        window = recent.setdefault(ctx.extension_id, deque())
        while window and now - window[0] > RATE_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            gateway.record(ctx.extension_id, "turns.request", "rate_limited", "")
            raise RpcError(
                ErrorCode.QUOTA_EXCEEDED,
                f"at most {RATE_LIMIT} turns every {int(RATE_WINDOW)} seconds",
            )
        window.append(now)
        await port.request(
            extension_id=ctx.extension_id,
            interface_id=interface_id,
            path=path,
            text=args["text"],
            sender_name=args["sender_name"] or remote.display_name,
            priority=args["priority"],
            context=context,
            allowed_actions=allowed,
            history_recent_max=args["history_recent_max"],
        )
        return {"queued": True}

    gateway.register(
        ApiMethod(
            "turns.request",
            CAP_TURNS,
            (
                Param("interface_id", "str", max_length=40),
                Param("path", "str", max_length=200),
                Param("text", "str", max_length=MAX_TEXT),
                Param("allowed_actions", "json"),
                Param("sender_name", "str", required=False, default="", max_length=80),
                Param(
                    "priority",
                    "str",
                    required=False,
                    default="low",
                    choices=PRIORITIES,
                ),
                Param("context", "object", required=False, default={}),
                Param(
                    "history_recent_max",
                    "int",
                    required=False,
                    default=0,
                    minimum=0,
                    maximum=MAX_HISTORY,
                ),
            ),
            request,
            "Run one background turn of the model on one of your own interfaces.",
            timeout=15.0,
        )
    )
