"""Recon questions an extension adds (``[[provides.recon]]``, capability ``recon.contribute``).

Recon is the small step before every turn in which the model answers a few structured
questions about the message, all the plugins' questions in one call. An extension that
declares a question in its manifest takes part: when its session is connected and the
operator granted ``recon.contribute``, the Core adds the question to that call and, with
the model's answer, calls ``recon.contribute`` **on the extension**:

    ``{"key", "answer", "text", "interface_path"}`` -> ``[{"content": "..."}, ...]``

What the extension returns goes into the prompt as ordinary snippets (a few, each of
limited length, marked with the extension's id as source). The extension cannot add any
other kind of contribution, and a slow or failing extension only costs its own snippet:
the call is bounded, and Recon carries on without it.

The Core side is a plain object registered where the Core's own recon plugins are, so
Recon needs no special case for an extension.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from synth_sdk.rpc import RpcError

from core.extensions.gateway import Gateway, Session
from core.extensions.manifest import ReconProvision

CAP_RECON = "recon.contribute"
#: Recon is on the turn's critical path.
CALL_TIMEOUT = 5.0
MAX_SNIPPETS = 3
MAX_SNIPPET_CHARS = 2000
#: Keys the Core's own recon plugins use; an extension may not shadow them.
RESERVED_KEYS = frozenset(
    {"language_hint", "tone_hint", "agent_intent", "keywords", "tags"}
)
_SAFE_ID = re.compile(r"[^a-z0-9]+")


class ReconHost(Protocol):
    def add(self, name: str, plugin: object) -> None: ...

    def remove(self, name: str, plugin: object) -> None: ...

    def taken_keys(self) -> set[str]: ...


def _json_safe(value: Any, depth: int = 0) -> Any:
    """The model's answer, reduced to plain JSON (it comes from a model, not a person)."""
    if depth > 4:
        return None
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, list):
        return [_json_safe(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        return {
            str(k)[:64]: _json_safe(v, depth + 1) for k, v in list(value.items())[:20]
        }
    return None


class RemoteRecon:
    """A recon question that is answered by an extension."""

    def __init__(self, session: Session, spec: ReconProvision) -> None:
        self._session = session
        self.spec = spec
        self.display_name = f"{session.extension_id}:{spec.key}"
        self.recon_priority = spec.priority

    def get_supported_actions(self) -> dict[str, Any]:
        return {}

    def get_recon_key(self) -> str:
        return self.spec.key

    def get_recon_instruction(self, **_: Any) -> str:
        return self.spec.instruction

    def is_recon_eligible(self, message: Any, context_memory: Any) -> bool:
        return not self._session.peer.closed

    async def parse_recon_response(
        self,
        data: Any,
        *,
        message: Any = None,
        context_memory: Any = None,
        text: str | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        if self._session.peer.closed:
            return []
        path = ""
        if isinstance(context_memory, dict):
            path = str(context_memory.get("interface_path") or "")
        if not path:
            path = str(getattr(message, "interface_path", "") or "")
        # Recon hands over the model's answer for every key; this one is ours.
        answer = data.get(self.spec.key) if isinstance(data, dict) else data
        try:
            result = await self._session.call(
                "recon.contribute",
                {
                    "key": self.spec.key,
                    "answer": _json_safe(answer),
                    "text": (text or "")[:2000],
                    "interface_path": path[:200],
                },
                timeout=CALL_TIMEOUT,
            )
        except RpcError:
            return []
        items = result if isinstance(result, list) else []
        found: list[dict[str, Any]] = []
        for item in items[:20]:
            if len(found) >= MAX_SNIPPETS:
                break
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, str) and content.strip():
                found.append(
                    {
                        "type": "snippet",
                        "content": content[:MAX_SNIPPET_CHARS],
                        "source": self._session.extension_id,
                        "priority": self.recon_priority,
                    }
                )
        return found

    async def get_recon_contributions(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.parse_recon_response(None, **kwargs)


def _name(extension_id: str, key: str) -> str:
    return f"ext_recon_{_SAFE_ID.sub('_', extension_id.lower())}_{key}"


def attach_recon(
    gateway: Gateway, host: ReconHost, *, warn: Callable[[str], None] | None = None
) -> None:
    """Register an extension's recon questions while it is connected."""

    def on_attach(session: Session) -> None:
        specs = session.manifest.provides.recon
        if not specs or not session.allowed(CAP_RECON):
            return
        taken = host.taken_keys() | RESERVED_KEYS
        added: list[tuple[str, RemoteRecon]] = []
        for spec in specs:
            if spec.key in taken:
                if warn is not None:
                    warn(
                        f"[extensions] {session.extension_id}: recon question "
                        f"{spec.key!r} is already taken; skipped"
                    )
                continue
            plugin = RemoteRecon(session, spec)
            name = _name(session.extension_id, spec.key)
            host.add(name, plugin)
            added.append((name, plugin))
            taken.add(spec.key)

        async def cleanup() -> None:
            for name, plugin in added:
                host.remove(name, plugin)

        if added:
            session.on_close(cleanup)

    gateway.on_attach.append(on_attach)
