# core/media_dispatcher.py
"""Central dispatcher for incoming media files from any interface.

Interfaces (Telegram, Discord, Matrix, WebUI …) receive media files from
their respective protocols.  **They must not attempt to understand the media
themselves** — they hand the file off here and receive a plain string that
they can enqueue into the message chain.

Escalation order
----------------
1. **Auris** (file-based STT) — primary path for audio/*  mime types.
   Delegates to whatever ``ACTIVE_AURIS_ENGINE`` is configured.

2. **Iris** (vision) — primary path for image/* and video/* mime types.
   Delegates to whatever ``ACTIVE_IRIS_ENGINE`` is configured.

3. **Live engine fallback** — invoked when Auris/Iris is unavailable or
   returns ``None``.  Uses ``live_registry.find_engine_by_capabilities`` to
   locate the first registered engine that exposes ``transcribe_file()``.

4. **``None``** — all engines failed or were unavailable.  The caller is
   responsible for providing a sensible placeholder text so the LLM receives
   at minimum a context hint (e.g. ``"[User sent a media message]"``).

Usage
-----
::

    from core.media_dispatcher import dispatch_media

    text = await dispatch_media(file_path, mime_type)
    text = text or "[User sent a media message]"

This module is **import-safe** from all interfaces and plugins — it only does
lazy imports of registries and plugins to avoid circular dependencies.
"""

from __future__ import annotations

from core.logging_utils import log_debug, log_info, log_warning


async def dispatch_media(
    file_path: str,
    mime_type: str | None,
    *,
    context_hint: str | None = None,
) -> str | None:
    """Attempt to extract a text representation from *file_path*.

    Args:
        file_path:    Absolute path to the downloaded media file.
        mime_type:    MIME type hint (e.g. ``"audio/ogg"``, ``"video/mp4"``).
                      Used to decide which engine to try first.
        context_hint: Optional free-text hint for logging (e.g. ``"voice_note"``).

    Returns:
        Transcribed / described text, or ``None`` if all engines failed.
    """
    log_debug(
        f"[media_dispatcher] dispatch_media called: mime={mime_type!r}"
        + (f" hint={context_hint!r}" if context_hint else "")
    )

    # ------------------------------------------------------------------
    # Step 1 — Auris STT (primary path, audio/* only)
    # ------------------------------------------------------------------
    if mime_type and mime_type.startswith("audio"):
        result = await _try_auris(file_path, mime_type)
        if result:
            log_info(
                f"[media_dispatcher] Auris transcription success ({len(result)} chars)."
            )
            return result
        log_debug("[media_dispatcher] Auris returned no transcription; escalating.")

    # ------------------------------------------------------------------
    # Step 2 — Iris vision (image/* and video/*)
    # ------------------------------------------------------------------
    if mime_type and mime_type.startswith(("image/", "video/")):
        result = await _try_iris(file_path, mime_type)
        if result:
            log_info(
                f"[media_dispatcher] Iris vision description success ({len(result)} chars)."
            )
            return result
        log_debug("[media_dispatcher] Iris returned no description; escalating.")

    # ------------------------------------------------------------------
    # Step 3 — Live engine fallback (transcribe_file)
    # ------------------------------------------------------------------
    result = await _try_live_fallback(file_path, mime_type)
    if result:
        log_info(
            f"[media_dispatcher] Live fallback transcription success ({len(result)} chars)."
        )
        return result
    log_warning(
        "[media_dispatcher] All engines returned no transcription. "
        "Caller should enqueue a placeholder text."
    )
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _try_auris(file_path: str, mime_type: str | None) -> str | None:
    """Try the configured Auris STT engine."""
    try:
        from core.core_initializer import PLUGIN_REGISTRY  # type: ignore[import]

        auris = PLUGIN_REGISTRY.get("auris_plugin")
        if auris is None:
            log_debug("[media_dispatcher] Auris plugin not loaded.")
            return None

        result = await auris.transcribe_audio(file_path, mime_type)
        return result.text if result else None
    except Exception as exc:
        log_warning(f"[media_dispatcher] Auris error: {exc}")
        return None


async def _try_iris(file_path: str, mime_type: str | None) -> str | None:
    """Try the configured Iris vision engine."""
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        iris = PLUGIN_REGISTRY.get("iris_plugin")
        if iris is None:
            log_debug("[media_dispatcher] Iris plugin not loaded.")
            return None

        result = await iris.describe_media(file_path, mime_type)
        return result.description if result else None
    except Exception as exc:
        log_warning(f"[media_dispatcher] Iris error: {exc}")
        return None


def _lazy_import_live_engines() -> None:
    """Lazy-import built-in Live engine modules so they self-register.

    Called the first time ``_try_live_fallback`` is invoked and the registry
    is still empty.  Python's module cache ensures the import only executes
    once per process.
    """
    for mod in ("plugins.live_engines.gemini",):
        try:
            __import__(mod)
            log_debug(f"[media_dispatcher] Lazy-imported live engine: {mod}")
        except Exception as exc:
            log_debug(
                f"[media_dispatcher] Could not lazy-import live engine '{mod}': {exc}"
            )


async def _try_live_fallback(file_path: str, mime_type: str | None) -> str | None:
    """Try the first Live engine that supports file-based transcription."""
    try:
        from core.live_registry import LIVE_REGISTRY  # type: ignore[import]

        # Ensure built-in Live engines are registered before querying.
        # This handles the case where the engine modules were never imported
        # (e.g. there is no Live plugin that imports them at startup).
        if not LIVE_REGISTRY.get_available_engines():
            _lazy_import_live_engines()

        # Prefer engines that explicitly flag input capability.
        engine_name = LIVE_REGISTRY.find_engine_by_capabilities({"input": True})
        if engine_name is None:
            log_debug("[media_dispatcher] No Live engine with input capability found.")
            return None

        engine = LIVE_REGISTRY.load_engine(engine_name)
        transcribe_file = getattr(engine, "transcribe_file", None)
        if not callable(transcribe_file):
            log_debug(
                f"[media_dispatcher] Live engine '{engine_name}' has no transcribe_file()."
            )
            return None

        log_info(
            f"[media_dispatcher] Using Live engine '{engine_name}' as fallback for "
            f"mime={mime_type!r}."
        )
        result: str | None = await transcribe_file(file_path, mime_type)
        return result or None
    except Exception as exc:
        log_warning(f"[media_dispatcher] Live fallback error: {exc}")
        return None
