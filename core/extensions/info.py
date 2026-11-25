"""What the Core says about itself to an extension (capability ``core.info``).

Read-only facts an extension needs to present the SyntH to the outside: its name, what
it can do as a whole (the union of what its engines provide) and whether it runs in a
container (which decides a sensible address to listen on). Nothing here is a secret and
nothing here lets an extension change anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.extensions.gateway import ApiMethod, CallContext, Gateway

CAP_INFO = "core.info"

InfoLoader = Callable[[], dict[str, Any]]


def register_info_api(gateway: Gateway, loader: InfoLoader) -> None:
    async def info(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        raw = loader()
        capabilities = raw.get("capabilities")
        return {
            "synth_name": str(raw.get("synth_name") or "")[:120],
            "capabilities": {
                str(k): True
                for k, v in (
                    capabilities.items() if isinstance(capabilities, dict) else []
                )
                if v
            },
            "in_container": bool(raw.get("in_container")),
        }

    gateway.register(
        ApiMethod(
            "core.info",
            CAP_INFO,
            (),
            info,
            "The SyntH's name, what it can do as a whole, and whether it runs in a container.",
        )
    )
