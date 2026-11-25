"""The synth avatar for extensions.

The avatar belongs to the **Core** (Settings > Synth Avatar): one image the operator
uploads and frames, stored and versioned by ``core.synth_avatar``. Any extension, an
interface or not, may use it through this API (capability ``avatar``):

``avatar.get``
    the current avatar: ``{"available", "version", "mime", "image"}`` (``image`` is the
    PNG in base64), or ``{"available": false}`` when none is set;

topic ``core.avatar.changed`` (needs the ``events`` capability)
    the Core publishes ``{"version": <str|null>}`` whenever the avatar really changes
    (an upload, a removal, a resend; ``null`` means removed), so an extension can fetch it
    again and stay in sync. The SDK wraps both in ``ctx.avatar.sync(handler)``.

An *interface* extension also gets the avatar pushed (``interface.set_avatar``) and the
Core keeps track of which version it applied, as for any interface; see
``core.extensions.interfaces``. Whether to apply it to a platform account is always the
extension's own, opt-in decision.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from core.extensions.gateway import ApiMethod, CallContext, Gateway

CAP_AVATAR = "avatar"
TOPIC_CHANGED = "core.avatar.changed"
#: The PNG is normalised to 512 x 512; a larger file would not fit one message.
MAX_AVATAR_BYTES = 700_000

AvatarLoader = Callable[[], "tuple[bytes, str] | None"]


def register_avatar_api(
    gateway: Gateway, loader: AvatarLoader
) -> Callable[[str | None], None]:
    """Register ``avatar.get``. Returns the function that announces a change.

    Wire the returned callable to the Core's change notification
    (``core.synth_avatar.add_change_listener``).
    """

    async def get(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        loaded = loader()
        if loaded is None or len(loaded[0]) > MAX_AVATAR_BYTES:
            return {"available": False}
        data, version = loaded
        return {
            "available": True,
            "version": version,
            "mime": "image/png",
            "image": base64.b64encode(data).decode("ascii"),
        }

    gateway.register(
        ApiMethod(
            "avatar.get",
            CAP_AVATAR,
            (),
            get,
            "The synth avatar (PNG, base64) and its version, when one is set.",
        )
    )

    def announce(version: str | None) -> None:
        gateway.events.publish(TOPIC_CHANGED, {"version": version})

    return announce
