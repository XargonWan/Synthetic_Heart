"""KittenTTS Vox engine extension.

Local neural text-to-speech as an endpoint protocol: the Core's voice pipeline sees an
endpoint called ``kitten`` (created here) and asks this extension to speak. Talks to the Core
only through ``ctx``; imports nothing from the Core.
"""

from __future__ import annotations

import asyncio
from typing import Any

import kitten
from synth_sdk.runtime import Context

ENDPOINT = "kitten"


async def main(ctx: Context) -> None:
    kitten.Log.target = ctx.log

    async def settings() -> tuple[str, dict[str, str], float]:
        values = await ctx.config.all()
        try:
            speed = float(values.get("KITTEN_SPEED") or kitten.DEFAULT_SPEED)
        except (TypeError, ValueError):
            speed = kitten.DEFAULT_SPEED
        return (
            str(values.get("KITTEN_MODEL") or kitten.DEFAULT_MODEL),
            kitten.parse_language_models(values.get("KITTEN_LANGUAGE_MODELS")),
            speed,
        )

    async def handle(
        protocol: str,
        op: str,
        endpoint: dict[str, Any],
        api_key: str,
        args: dict[str, Any],
    ) -> Any:
        if op == "models":
            return kitten.model_infos()
        if op == "capabilities":
            return {"cortex": False, "vox": True, "auris": False, "live": False}
        if op == "health":
            return True
        if op == "ping":
            return {"ok": False, "text": "KittenTTS is a speech engine (no chat ping)"}
        if op == "list_speakers":
            return kitten.speakers()
        if op == "tts":
            default_model, language_models, speed = await settings()
            audio = await asyncio.to_thread(
                kitten.synthesize,
                str(args.get("text", "")),
                args.get("voice"),
                dict(args.get("kwargs") or {}),
                default_model=default_model,
                language_models=language_models,
                speed=speed,
            )
            if audio is None:
                return {}
            path = ctx.inbox_path(
                "kitten.wav"
            )  # the Core reads this file and deletes it
            path.write_bytes(audio)
            return {"path": str(path)}
        raise ValueError(f"operation {op!r} is not provided by this protocol")

    ctx.protocols.on_request(handle)
    # The engine exists as an endpoint named like it always was, so the active voice engine
    # setting and the voice key (KITTEN_VOICE) of an existing installation keep working.
    await ctx.endpoints.ensure(
        ENDPOINT,
        "",
        display_label="KittenTTS",
        protocol="kitten",
        extra_config={
            "tts_output_format": "wav",
            "tts_sample_rate": kitten.SAMPLE_RATE,
        },
        subsystem_map={"vox": True},
    )
    await ctx.stopping.wait()
