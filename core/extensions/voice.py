"""The synth's voice for extensions (capabilities ``voice.synthesize`` and ``voice.play``).

An extension that speaks (a radio host, a game companion) does not run a speech engine:
the Core owns Vox, the engine settings and the avatar. Two calls cover it:

``voice.synthesize``
    speak ``text`` with the synth's voice (or one named ``engine``) and get the audio as
    a **file in your own data directory** (``{"path", "mime_type", "size"}``): the
    Core makes the copy, readable by you only, and removes it after an hour at the
    latest. ``{"available": false}`` when no engine could make it.

``voice.play``
    make the avatar speak a clip **you** made (usually the one you just sent
    somewhere else), with its lip-sync and talking animation, for everyone who is
    watching the stage. You name a file in your data directory; the Core copies it
    (the same checks as an attachment) and never opens any other path.

``voice.transcribe``
    hear a recording **you** hold (a clip the player recorded in your world): you name a file
    in your data directory and its MIME type, the Core copies it (the same checks as an
    attachment) and reads it with the hearing engine the person chose (Auris), and answers
    ``{"text", "language"}``. ``{"available": false}`` when nothing could hear it.

Both ``synthesize`` and ``play`` need the file bridge (``MediaBridge``) the interface API uses, so a Core without
attachments does not offer them.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.gateway import ApiMethod, CallContext, Gateway, Param
from core.extensions.media import MediaBridge, MediaError

CAP_SYNTHESIZE = "voice.synthesize"
CAP_PLAY = "voice.play"
CAP_TRANSCRIBE = "voice.transcribe"
MAX_TEXT = 2000
ENGINE_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
#: A voice takes a while; longer than the gateway's default.
SYNTHESIZE_TIMEOUT = 90.0
PLAY_TIMEOUT = 30.0
TRANSCRIBE_TIMEOUT = 120.0
MIME_RE = re.compile(r"^(audio/[A-Za-z0-9.+-]{1,40})?$")


class VoicePort(Protocol):
    async def synthesize(self, text: str, engine: str) -> str | None:
        """A file the Core made with its voice, or ``None``."""
        ...

    async def play(self, audio_path: str, text: str, engine: str) -> bool:
        """Play a Core-owned clip on the stage; whether anyone received it."""
        ...


def register_voice_api(gateway: Gateway, port: VoicePort, media: MediaBridge) -> None:
    """Register ``voice.synthesize`` and ``voice.play`` on the gateway."""

    async def synthesize(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        if not args["text"].strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "text must not be empty")
        path = await port.synthesize(args["text"], args["engine"])
        if not path:
            return {"available": False}
        staged = await media.stage_core_files(ctx.extension_id, [path])
        if not staged.items:
            staged.cleanup()
            gateway.record(
                ctx.extension_id,
                "voice.synthesize",
                "failed",
                "; ".join(reason for _, reason in staged.rejected)[:120],
            )
            return {"available": False}
        item = staged.items[0]
        # Left for the extension to read; ``MediaBridge.prune`` removes what is left.
        return {
            "available": True,
            "path": item["path"],
            "mime_type": item["mime_type"],
            "size": item["size"],
        }

    async def play(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            imported = await media.import_inbound(
                ctx.extension_id, [{"path": args["path"]}]
            )
        except MediaError as exc:
            gateway.record(
                ctx.extension_id, "voice.play", "invalid_params", exc.reason[:80]
            )
            raise RpcError(ErrorCode.INVALID_PARAMS, f"file refused: {exc.reason}")
        delivered = await port.play(imported[0]["path"], args["text"], args["engine"])
        return {"delivered": bool(delivered)}

    async def transcribe(ctx: CallContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            imported = await media.import_inbound(
                ctx.extension_id, [{"path": args["path"]}]
            )
        except MediaError as exc:
            gateway.record(
                ctx.extension_id, "voice.transcribe", "invalid_params", exc.reason[:80]
            )
            raise RpcError(ErrorCode.INVALID_PARAMS, f"file refused: {exc.reason}")
        heard = await port.transcribe(
            imported[0]["path"], args["mime_type"] or imported[0].get("mime_type") or ""
        )
        if not heard or not str(heard.get("text") or "").strip():
            return {"available": False}
        return {
            "available": True,
            "text": str(heard["text"]).strip(),
            "language": heard.get("language"),
        }

    if hasattr(port, "transcribe"):
        gateway.register(
            ApiMethod(
                "voice.transcribe",
                CAP_TRANSCRIBE,
                (
                    Param("path", "str", max_length=500),
                    Param(
                        "mime_type",
                        "str",
                        required=False,
                        default="",
                        max_length=60,
                        pattern=MIME_RE,
                    ),
                ),
                transcribe,
                "Read a recording of yours with the hearing engine the person chose.",
                timeout=TRANSCRIBE_TIMEOUT,
            )
        )

    gateway.register(
        ApiMethod(
            "voice.synthesize",
            CAP_SYNTHESIZE,
            (
                Param("text", "str", max_length=MAX_TEXT),
                Param("engine", "str", required=False, default="", pattern=ENGINE_RE),
            ),
            synthesize,
            "Speak text with the synth's voice; the audio comes back as a file of yours.",
            timeout=SYNTHESIZE_TIMEOUT,
        )
    )
    gateway.register(
        ApiMethod(
            "voice.play",
            CAP_PLAY,
            (
                Param("path", "str", max_length=500),
                Param("text", "str", required=False, default="", max_length=MAX_TEXT),
                Param("engine", "str", required=False, default="", pattern=ENGINE_RE),
            ),
            play,
            "Make the avatar speak a clip of yours, with lip-sync, on the stage.",
            timeout=PLAY_TIMEOUT,
        )
    )
