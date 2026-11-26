"""Fish Audio protocol extension.

Provides the ``fish`` protocol for external endpoints: a TTS-only provider with a voice
catalogue. Talks to the Core only through ``ctx``; imports nothing from the Core.
"""

from __future__ import annotations

from typing import Any

import fish
from synth_sdk.runtime import Context


async def main(ctx: Context) -> None:
    fish.Log.target = ctx.log

    async def handle(
        protocol: str,
        op: str,
        endpoint: dict[str, Any],
        api_key: str,
        args: dict[str, Any],
    ) -> Any:
        if op == "parse_model_id_from_url":  # no endpoint needed
            return fish.parse_model_id_from_url(str(args.get("url", "")))
        client = fish.Fish(endpoint, api_key)
        if op == "models":
            return fish.models()
        if op == "capabilities":
            # TTS-only provider; synthesis is billed, so no live probe request.
            return {"cortex": False, "vox": True, "auris": False, "live": False}
        if op == "health":
            return bool(api_key)  # no free health endpoint: reachable iff configured
        if op == "ping":
            return {
                "ok": False,
                "text": "Fish Audio is a TTS-only endpoint (no chat ping)",
            }
        client.need_key()
        if op == "tts":
            audio = await client.tts(
                str(args.get("text", "")),
                args.get("voice"),
                dict(args.get("kwargs") or {}),
            )
            if audio is None:
                return {}
            path = ctx.inbox_path(
                "speech.audio"
            )  # the Core reads this file and deletes it
            path.write_bytes(audio)
            return {"path": str(path)}
        if op == "list_speakers":
            return await client.list_speakers()
        if op == "fetch_model_detail":
            return await client.fetch_model_detail(str(args.get("model_id", "")))
        raise ValueError(f"operation {op!r} is not provided by this protocol")

    ctx.protocols.on_request(handle)
    await ctx.stopping.wait()
