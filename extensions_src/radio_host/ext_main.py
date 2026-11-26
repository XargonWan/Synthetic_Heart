"""AzuraCast Radio DJ extension.

The synth as the DJ of an AzuraCast station: it comments between songs on air, answers
questions about what is playing, and shows its remarks in the Radio window. All of it is
reached through the Core's public API (``ctx``): the interface (where its turns run), the
model (``ctx.turns``), the voice (``ctx.voice``), the Recon question and the window's
routes. Nothing here imports the Core.
"""

from __future__ import annotations

import radiolog
from radio import (
    INTERFACE_ID,
    SPEAK,
    SPEAK_ACTION,
    UPDATE_METADATA,
    UPDATE_METADATA_ACTION,
    RadioHost,
)
from synth_sdk.runtime import Context


async def main(ctx: Context) -> None:
    radiolog.bind(ctx.log)
    host = RadioHost(ctx)
    await ctx.interfaces.register(
        INTERFACE_ID,
        display_name="Radio Host",
        path_hint=f"{INTERFACE_ID}/<id>",
        description=(
            "The synth's live radio show. It is not a chat: to speak on air use the "
            "radio_speak action."
        ),
        actions={SPEAK: SPEAK_ACTION, UPDATE_METADATA: UPDATE_METADATA_ACTION},
    )
    ctx.interfaces.on_action(host.handle_action)
    ctx.interfaces.on_send_message(host.on_send_message)
    ctx.recon.on_contribute(host.on_contribute)
    ctx.web.on_request(host.on_web_request)
    await host.start()
    try:
        await ctx.stopping.wait()
    finally:
        await host.stop()
