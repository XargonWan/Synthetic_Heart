"""Wire protocols for external endpoints that live in an extension.

An *external endpoint* (an LLM, speech or vision service the operator configured) names a
``protocol``. The Core keeps one adapter per protocol that knows how to list the models,
chat, synthesise speech, transcribe and so on (``core.external_endpoints``). An extension
that declares ``[[provides.protocols]]`` and holds ``endpoints.protocol`` takes over a
protocol: while it runs, the adapter of that protocol is a thin proxy that sends each
operation to the extension (``protocol.request``) and returns what comes back.

Layering. A protocol often differs from a common one in a few places only (Harmony is
OpenAI-compatible for chat and has its own audio endpoints). The manifest names a ``base``
protocol and the ``ops`` the extension implements; every other operation is answered by
the base protocol's adapter in the Core.

What the extension gets. For each call, the endpoint's description (name, base URL, default
model, extra settings) and its **decrypted API key**: it needs the key to speak the
protocol. It is only ever given the keys of endpoints that use *its* protocol, and an
extension cannot take over a protocol the Core has built in. Audio and images do not
travel inside a message: the Core stages a file in the extension's data directory for what
it sends, and the extension returns a file path for what it produces (the Core reads it and
deletes it).

Operations (``ProtocolProvision.ops``): ``chat``, ``models``, ``capabilities``, ``health``,
``ping``, ``tts``, ``stt``, ``vision``, and the optional extras ``list_speakers``,
``fetch_model_detail`` and ``parse_model_id_from_url`` that a voice picker uses.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import Gateway, Session
from core.extensions.manifest import ProtocolProvision
from core.extensions.media import MediaBridge, MediaError

CAP_PROTOCOL = "endpoints.protocol"
#: The largest request or answer that travels inside a message (messages are 1 MiB).
MAX_INLINE_BYTES = 800_000
#: How long each operation may take, in seconds.
TIMEOUTS = {
    "chat": 330.0,
    "models": 100.0,
    "capabilities": 100.0,
    "health": 30.0,
    "ping": 70.0,
    "tts": 130.0,
    "stt": 310.0,
    "vision": 130.0,
    "list_speakers": 60.0,
    "fetch_model_detail": 60.0,
    "parse_model_id_from_url": 10.0,
}


class ProtocolHost(Protocol):
    """What the Core offers to register a protocol in."""

    def builtin(self, name: str) -> bool:
        """True for a protocol the Core itself provides (an extension cannot take it)."""
        ...

    def factory_of(self, name: str) -> Callable[[Any, str], Any] | None:
        """The adapter factory currently registered for ``name``."""
        ...

    def register(self, name: str, factory: Callable[[Any, str], Any]) -> None: ...

    def unregister(self, name: str) -> None: ...


def _endpoint_description(endpoint: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(endpoint, "name", "")),
        "base_url": str(getattr(endpoint, "base_url", "") or ""),
        "default_model": getattr(endpoint, "default_model", None),
        "extra_config": dict(getattr(endpoint, "extra_config", None) or {}),
    }


def _model_info(raw: Any) -> Any:
    from core.external_endpoints.adapters.base import ModelInfo

    if not isinstance(raw, dict) or not str(raw.get("id") or ""):
        return None
    return ModelInfo(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        owned_by=str(raw.get("owned_by") or ""),
        capabilities={
            str(k): bool(v) for k, v in dict(raw.get("capabilities") or {}).items()
        },
        model_type=str(raw.get("model_type") or ""),
        input_modalities=[str(x) for x in raw.get("input_modalities") or []],
        output_modalities=[str(x) for x in raw.get("output_modalities") or []],
        languages=[
            {str(k): str(v) for k, v in item.items()}
            for item in raw.get("languages") or []
            if isinstance(item, dict)
        ],
    )


class _Files:
    """Moving audio and images across the boundary as files."""

    def __init__(self, session: Session, media: MediaBridge | None) -> None:
        self._session, self._media = session, media

    async def send(self, data: bytes, suffix: str) -> tuple[str, Callable[[], None]]:
        """Stage ``data`` for the extension; returns its path and a cleanup function."""
        if self._media is None:
            raise RpcError(
                ErrorCode.INTERNAL_ERROR, "files are not enabled on this Core"
            )
        handle, name = tempfile.mkstemp(prefix="synth-proto-", suffix=suffix)
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            staged = await self._media.stage_core_files(
                self._session.extension_id, [name]
            )
        finally:
            Path(name).unlink(missing_ok=True)
        if not staged.items:
            staged.cleanup()
            raise RpcError(ErrorCode.INTERNAL_ERROR, "the file could not be staged")
        return str(staged.items[0]["path"]), staged.cleanup

    async def receive(self, result: Any) -> bytes | None:
        """The audio an operation produced: a file of the extension's, read and deleted."""
        path = result.get("path") if isinstance(result, dict) else None
        if not path:
            return None
        if self._media is None:
            return None
        try:
            return await self._media.take_inbound(self._session.extension_id, str(path))
        except MediaError:
            return None


def adapter_class() -> type:
    """The proxy class. Built here so importing this module needs no Core adapter code."""
    from core.external_endpoints.adapters.base import (
        BaseProtocolAdapter,
        ChatResponse,
    )

    class RemoteProtocolAdapter(BaseProtocolAdapter):
        """An adapter whose operations an extension answers."""

        def __init__(
            self,
            session: Session,
            spec: ProtocolProvision,
            endpoint: Any,
            api_key: str,
            media: MediaBridge | None,
            base_factory: Callable[[Any, str], Any] | None,
            core_loop: asyncio.AbstractEventLoop | None = None,
        ) -> None:
            self._session, self._spec = session, spec
            self._core_loop = core_loop
            self._endpoint, self._api_key = endpoint, api_key
            self._files = _Files(session, media)
            self._base_factory = base_factory
            self._base_adapter: Any = None

        # ------------------------------------------------------------ plumbing

        def _base(self) -> Any:
            if self._base_adapter is None:
                if self._base_factory is None:
                    raise NotImplementedError(
                        f"protocol {self._spec.name!r} does not implement this operation"
                    )
                self._base_adapter = self._base_factory(self._endpoint, self._api_key)
            return self._base_adapter

        def _mine(self, op: str) -> bool:
            return op in self._spec.ops

        async def _ask(self, op: str, args: dict[str, Any]) -> Any:
            params = {
                "protocol": self._spec.name,
                "op": op,
                "endpoint": _endpoint_description(self._endpoint),
                "api_key": self._api_key,
                "args": args,
            }
            if len(json.dumps(params, default=str)) > MAX_INLINE_BYTES:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR,
                    "the request is too large to send in a message",
                )
            call = self._session.call(
                "protocol.request", params, timeout=TIMEOUTS.get(op, 100.0)
            )
            core_loop = self._core_loop
            if core_loop is None or asyncio.get_running_loop() is core_loop:
                return await call
            # Called from another loop: the Core's voice and hearing engines run their
            # adapters in a worker thread with a loop of its own. The connection belongs to
            # the Core's loop, so the call is made there and awaited from here.
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(call, core_loop)
            )

        # ---------------------------------------------------------------- chat

        async def chat_completion(
            self,
            messages: list[dict[str, Any]],
            model: str | None = None,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            if not self._mine("chat"):
                return await self._base().chat_completion(
                    messages, model=model, stream=stream, **kwargs
                )
            result = await self._ask(
                "chat",
                {"messages": messages, "model": model, "kwargs": _plain(kwargs)},
            )
            if not isinstance(result, dict):
                raise RuntimeError("the extension returned no chat answer")
            return ChatResponse(
                content=str(result.get("content") or ""),
                model=str(result.get("model") or ""),
                finish_reason=str(result.get("finish_reason") or "stop"),
                usage={
                    str(k): int(v) for k, v in dict(result.get("usage") or {}).items()
                },
            )

        async def list_models(self) -> list[Any]:
            if not self._mine("models"):
                return await self._base().list_models()
            result = await self._ask("models", {})
            found = [_model_info(item) for item in (result or [])]
            return [m for m in found if m is not None]

        # -------------------------------------------------------------- audio

        async def generate_tts(
            self, text: str, voice: str | None = None, **kwargs: Any
        ) -> bytes | None:
            if not self._mine("tts"):
                return await self._base().generate_tts(text, voice=voice, **kwargs)
            result = await self._ask(
                "tts", {"text": text, "voice": voice, "kwargs": _plain(kwargs)}
            )
            return await self._files.receive(result)

        async def transcribe_audio(
            self, audio_bytes: bytes, mime_type: str | None = None, **kwargs: Any
        ) -> str | None:
            if not self._mine("stt"):
                return await self._base().transcribe_audio(
                    audio_bytes, mime_type=mime_type, **kwargs
                )
            path, cleanup = await self._files.send(audio_bytes, ".audio")
            try:
                result = await self._ask(
                    "stt",
                    {"path": path, "mime_type": mime_type, "kwargs": _plain(kwargs)},
                )
            finally:
                cleanup()
            text = result.get("text") if isinstance(result, dict) else None
            return text if isinstance(text, str) else None

        async def describe_image(
            self,
            image_bytes: bytes,
            mime_type: str | None = None,
            prompt: str | None = None,
            **kwargs: Any,
        ) -> str | None:
            if not self._mine("vision"):
                return await self._base().describe_image(
                    image_bytes, mime_type=mime_type, prompt=prompt, **kwargs
                )
            path, cleanup = await self._files.send(image_bytes, ".image")
            try:
                result = await self._ask(
                    "vision",
                    {
                        "path": path,
                        "mime_type": mime_type,
                        "prompt": prompt,
                        "kwargs": _plain(kwargs),
                    },
                )
            finally:
                cleanup()
            text = result.get("text") if isinstance(result, dict) else None
            return text if isinstance(text, str) else None

        # ------------------------------------------------------------ probing

        async def probe_capabilities(
            self, models: list[Any] | None = None
        ) -> dict[str, bool]:
            if not self._mine("capabilities"):
                return await self._base().probe_capabilities(models=models)
            result = await self._ask(
                "capabilities",
                {"models": [m.to_dict() for m in models or []]},
            )
            return {str(k): bool(v) for k, v in dict(result or {}).items()}

        async def health_check(self) -> bool:
            if not self._mine("health"):
                return await self._base().health_check()
            return bool(await self._ask("health", {}))

        async def ping_test(
            self,
            model: str | None = None,
            timeout: float = 15.0,
            models: list[Any] | None = None,
        ) -> tuple[bool, str]:
            if not self._mine("ping"):
                return await self._base().ping_test(
                    model=model, timeout=timeout, models=models
                )
            result = await self._ask(
                "ping",
                {
                    "model": model,
                    "timeout": timeout,
                    "models": [m.to_dict() for m in models or []],
                },
            )
            if isinstance(result, dict):
                return bool(result.get("ok")), str(result.get("text") or "")
            return False, "no answer"

        # ------------------------------------------------------------- extras

        async def list_speakers(self) -> list[dict[str, Any]]:
            result = await self._ask("list_speakers", {})
            return [dict(item) for item in result or [] if isinstance(item, dict)]

        async def fetch_model_detail(self, model_id: str) -> dict[str, Any] | None:
            result = await self._ask("fetch_model_detail", {"model_id": model_id})
            return dict(result) if isinstance(result, dict) else None

        async def parse_model_id_from_url(self, url_or_id: str) -> str | None:
            result = await self._ask("parse_model_id_from_url", {"url": url_or_id})
            return str(result) if isinstance(result, str) and result else None

        # A voice picker looks for these by name: expose only what the extension has.
        def __getattribute__(self, name: str) -> Any:
            if (
                name
                in (
                    "list_speakers",
                    "fetch_model_detail",
                    "parse_model_id_from_url",
                )
                and name not in object.__getattribute__(self, "_spec").ops
            ):
                raise AttributeError(name)
            return object.__getattribute__(self, name)

    return RemoteProtocolAdapter


def _plain(value: dict[str, Any]) -> dict[str, Any]:
    """Keyword arguments reduced to what survives JSON."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def attach_protocols(
    gateway: Gateway,
    host: ProtocolHost,
    media: MediaBridge | None,
    *,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Offer an extension's protocols to the Core while it is connected."""

    def on_attach(session: Session) -> None:
        specs = session.manifest.provides.protocols
        if not specs or not session.allowed(CAP_PROTOCOL):
            return
        cls = adapter_class()
        try:
            core_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            core_loop = None
        added: list[str] = []
        for spec in specs:
            if host.builtin(spec.name):
                if warn is not None:
                    warn(
                        f"[extensions] {session.extension_id}: protocol {spec.name!r} "
                        "is built into the Core and cannot be replaced; skipped"
                    )
                continue
            base_factory = host.factory_of(spec.base) if spec.base else None
            if spec.base and base_factory is None:
                if warn is not None:
                    warn(
                        f"[extensions] {session.extension_id}: protocol {spec.name!r} "
                        f"needs the {spec.base!r} protocol, which is not available; skipped"
                    )
                continue

            def factory(
                endpoint: Any,
                api_key: str,
                spec: ProtocolProvision = spec,
                base_factory: Callable[[Any, str], Any] | None = base_factory,
            ) -> Any:
                if session.peer.closed:
                    raise ValueError(
                        f"the extension providing protocol {spec.name!r} is not running"
                    )
                return cls(
                    session, spec, endpoint, api_key, media, base_factory, core_loop
                )

            host.register(spec.name, factory)
            added.append(spec.name)

        async def cleanup() -> None:
            for name in added:
                host.unregister(name)

        if added:
            session.on_close(cleanup)

    gateway.on_attach.append(on_attach)


class CoreProtocolHost:
    """``ProtocolHost`` backed by the Core's endpoint protocol registry.

    Registering or removing a protocol also re-registers the endpoints that use it, so
    their engines appear (or go away) as the extension starts (or stops), whichever came
    first at start-up.
    """

    #: Protocols the Core implements itself: the OpenAI-compatible, Gemini and Anthropic
    #: dialects (the prompt and tool rendering is built around them) and ``custom``.
    BUILTIN = frozenset({"openai", "gemini", "anthropic", "custom"})

    def builtin(self, name: str) -> bool:
        return name in self.BUILTIN

    def factory_of(self, name: str) -> Callable[[Any, str], Any] | None:
        from core.external_endpoints import probe
        from core.external_endpoints.models import EndpointProtocol

        return probe._ADAPTER_FACTORIES.get(EndpointProtocol(name))  # noqa: SLF001

    def register(self, name: str, factory: Callable[[Any, str], Any]) -> None:
        from core.external_endpoints.models import EndpointProtocol
        from core.external_endpoints.probe import register_protocol

        register_protocol(EndpointProtocol(name), factory)
        self._resync(name)

    def unregister(self, name: str) -> None:
        from core.external_endpoints.models import EndpointProtocol
        from core.external_endpoints.probe import unregister_protocol

        unregister_protocol(EndpointProtocol(name))
        self._resync(name)

    @staticmethod
    def _resync(name: str) -> None:
        """Re-register the endpoints of this protocol (their adapters changed)."""
        import asyncio

        from core.external_endpoints.registry import get_external_endpoint_registry

        async def run() -> None:
            await get_external_endpoint_registry().sync_protocol(name)

        try:
            asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
