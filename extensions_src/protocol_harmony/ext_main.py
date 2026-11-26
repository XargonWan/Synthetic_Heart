"""Harmony AI protocol extension.

Provides the ``harmony`` protocol for external endpoints: the model listing and the audio
endpoints that are Harmony's own. Everything else (chat, embeddings, vision, probing) is
OpenAI-shaped and answered by the Core's OpenAI adapter (``base = "openai"``). Talks to the
Core only through ``ctx``; imports nothing from the Core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import harmony
from synth_sdk.runtime import Context


async def main(ctx: Context) -> None:
    harmony.Warn.log = ctx.log

    async def handle(
        protocol: str,
        op: str,
        endpoint: dict[str, Any],
        api_key: str,
        args: dict[str, Any],
    ) -> Any:
        client = harmony.Harmony(endpoint, api_key)
        if op == "models":
            return await client.models()
        if op == "tts":
            audio = await client.tts(
                str(args.get("text", "")),
                args.get("voice"),
                dict(args.get("kwargs") or {}),
            )
            if audio is None:
                return {}
            # The Core reads this file and deletes it.
            path = ctx.inbox_path("speech.audio")
            path.write_bytes(audio)
            return {"path": str(path)}
        if op == "stt":
            audio_bytes = Path(str(args["path"])).read_bytes()
            text = await client.stt(audio_bytes, dict(args.get("kwargs") or {}))
            return {"text": text}
        raise ValueError(f"operation {op!r} is not provided by this protocol")

    ctx.protocols.on_request(handle)
    await ctx.stopping.wait()
