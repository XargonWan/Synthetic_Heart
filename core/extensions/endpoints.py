"""External endpoints an extension provides (capability ``endpoints.manage``).

An LLM, speech or vision service an extension fronts shows up in the Engines page as an
*external endpoint*: a name, a base URL and some settings. The extension does not run the
engine inside the Core; it only makes sure the endpoint exists and keeps its settings
current.

What it may touch is narrow, because an endpoint can hold an API key and a URL decides
where that key is sent:

* only endpoints whose **names the manifest declares** (``provides.endpoints``);
* never one that **holds an API key**: the extension cannot read, set or keep a key, and
  cannot repoint a keyed endpoint;
* an existing endpoint only gets settings it **lacks** (``extra_config`` keys the operator
  set are left alone) and, on request, a new base URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param
from synth_sdk.rpc import ErrorCode, RpcError

CAP_ENDPOINTS = "endpoints.manage"
#: Subsystems an endpoint can be mapped to.
SUBSYSTEMS = frozenset({"cortex", "vox", "auris", "live", "vision"})
MAX_EXTRA_BYTES = 4096
PROTOCOLS = ("openai",)
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


@dataclass
class EndpointInfo:
    id: int
    name: str
    base_url: str
    has_key: bool
    extra_config: dict[str, Any] = field(default_factory=dict)


class EndpointPort(Protocol):
    async def get(self, name: str) -> EndpointInfo | None: ...

    async def count_cortex(self) -> int:
        """How many enabled endpoints serve the cortex (an LLM): what "the operator has a
        model configured" means. Voice and hearing engines do not count."""
        ...

    async def add(
        self,
        name: str,
        display_label: str,
        base_url: str,
        protocol: str,
        extra_config: dict[str, Any],
        subsystem_map: dict[str, bool],
    ) -> int: ...

    async def update(
        self,
        endpoint_id: int,
        *,
        base_url: str | None = None,
        extra_config: dict[str, Any] | None = None,
    ) -> None: ...


def _declared(ctx: CallContext, name: str) -> None:
    if name not in ctx.manifest.provides.endpoints:
        raise RpcError(
            ErrorCode.PERMISSION_DENIED,
            f"endpoint {name!r} is not declared in the manifest (provides.endpoints)",
        )


def _url(value: str) -> str:
    if value == "":  # a local engine (a protocol that needs no address)
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RpcError(ErrorCode.INVALID_PARAMS, "base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise RpcError(ErrorCode.INVALID_PARAMS, "base_url must not carry credentials")
    return value


def _extra(raw: dict[str, Any]) -> dict[str, Any]:
    import json

    if len(json.dumps(raw)) > MAX_EXTRA_BYTES:
        raise RpcError(ErrorCode.INVALID_PARAMS, "extra_config is too large")
    for key in raw:
        if not isinstance(key, str) or not 0 < len(key) <= 64 or key.startswith("_"):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, f"extra_config key {key!r} is not allowed"
            )
    return raw


def _subsystems(raw: dict[str, Any]) -> dict[str, bool]:
    unknown = sorted(set(raw) - SUBSYSTEMS)
    if unknown or not all(isinstance(v, bool) for v in raw.values()):
        raise RpcError(
            ErrorCode.INVALID_PARAMS,
            f"subsystem_map takes booleans for {', '.join(sorted(SUBSYSTEMS))}",
        )
    return dict(raw)


def register_endpoint_api(gateway: Gateway, port: EndpointPort) -> None:
    """Register ``endpoints.get`` and ``endpoints.ensure`` on the gateway."""

    async def get(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        _declared(ctx, args["name"])
        found = await port.get(args["name"])
        if found is None:
            return {"exists": False}
        return {
            "exists": True,
            "id": found.id,
            "base_url": found.base_url,
            "has_key": found.has_key,
        }

    async def ensure(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        _declared(ctx, name)
        protocol = args["protocol"]
        own = {p.name for p in ctx.manifest.provides.protocols}
        if protocol not in PROTOCOLS and protocol not in own:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"protocol {protocol!r} is neither {', '.join(PROTOCOLS)} nor one this "
                "extension provides (provides.protocols)",
            )
        base_url = _url(args["base_url"])
        extra = _extra(args["extra_config"])
        subsystems = _subsystems(args["subsystem_map"])
        found = await port.get(name)
        if found is None:
            if args["only_if_no_cortex"] and await port.count_cortex() > 0:
                return {
                    "created": False,
                    "skipped": "a cortex endpoint is already configured",
                }
            created = await port.add(
                name,
                args["display_label"] or name,
                base_url,
                protocol,
                extra,
                subsystems,
            )
            return {"created": True, "id": created}
        if found.has_key:
            raise RpcError(
                ErrorCode.PERMISSION_DENIED,
                f"endpoint {name!r} holds an API key: it is the operator's, not yours",
            )
        missing = {k: v for k, v in extra.items() if k not in found.extra_config}
        new_url = (
            base_url if args["update_base_url"] and base_url != found.base_url else None
        )
        if missing or new_url is not None:
            await port.update(
                found.id,
                base_url=new_url,
                extra_config={**found.extra_config, **missing} if missing else None,
            )
        return {
            "created": False,
            "id": found.id,
            "added": sorted(missing),
            "base_url_changed": new_url is not None,
        }

    gateway.register(
        ApiMethod(
            "endpoints.get",
            CAP_ENDPOINTS,
            (Param("name", "str", max_length=63, pattern=NAME_RE),),
            get,
            "Whether a declared endpoint exists, and its base URL.",
        )
    )
    gateway.register(
        ApiMethod(
            "endpoints.ensure",
            CAP_ENDPOINTS,
            (
                Param("name", "str", max_length=63, pattern=NAME_RE),
                Param("base_url", "str", max_length=512),
                Param(
                    "display_label", "str", required=False, default="", max_length=120
                ),
                Param(
                    "protocol",
                    "str",
                    required=False,
                    default="openai",
                    max_length=32,
                ),
                Param("extra_config", "object", required=False, default={}),
                Param("subsystem_map", "object", required=False, default={}),
                Param("only_if_no_cortex", "bool", required=False, default=False),
                Param("update_base_url", "bool", required=False, default=False),
            ),
            ensure,
            "Create a declared endpoint, or fill in what an existing one lacks.",
        )
    )
