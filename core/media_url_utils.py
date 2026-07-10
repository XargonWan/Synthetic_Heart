"""Filesystem-path → client URL derivation for generated media (TTS audio, ...).

The WebUI serves generated audio to browsers over HTTP static mounts. Two mounts
can exist:

* ``/static``  → ``res/synth_webui/static`` (the default, in-image location).
* ``/media``   → the configured :data:`VOX_OUTPUT_DIR` **when it points outside**
  the ``/static`` tree (e.g. a persistent volume such as ``/config/media/tts``).

Historically the URL was derived by searching the filesystem path for a
``static`` segment and, when absent, blindly falling back to
``/static/audio/tts/<name>``. That fallback produced a broken URL (HTTP 404)
whenever ``VOX_OUTPUT_DIR`` was moved off the ``/static`` tree, because the file
physically lived under ``/config/media/tts`` while the URL pointed at the empty
``res/synth_webui/static/audio/tts``.

This helper resolves the URL against the *configured* output directory so both
the Karada state server and the WebUI caption path agree on where the audio is
actually served from.
"""

from __future__ import annotations

from pathlib import Path

# Client-facing prefix for the alternate ("outside /static") media mount.
MEDIA_MOUNT_PATH = "/media"


def get_vox_output_dir() -> Path:
    """Return the configured Vox output directory as an absolute path.

    Reads the ``VOX_OUTPUT_DIR`` config value (falling back to the in-image
    default) and resolves it. Never raises — returns the default on any error.
    """
    default = "res/synth_webui/static/audio/tts"
    try:
        from core.config_manager import config_registry

        raw = config_registry.get_value(
            "VOX_OUTPUT_DIR",
            default,
            value_type=str,
            group="plugins",
            component="vox_plugin",
        )
    except Exception:
        raw = default
    try:
        return Path(str(raw)).resolve()
    except Exception:
        return Path(default)


def vox_output_is_outside_static() -> bool:
    """True when the configured Vox output dir is not under a ``static`` tree.

    Used by the WebUI to decide whether to add the alternate ``/media`` mount.
    """
    return "static" not in get_vox_output_dir().parts


def derive_audio_url(audio_path: str) -> str:
    """Derive a client-accessible URL from a generated-audio filesystem path.

    Resolution order:

    1. If the path contains a ``static`` segment, serve it under ``/static``
       preserving everything from that segment onward (unchanged legacy
       behaviour for in-image audio).
    2. Otherwise, if the path is under the configured :data:`VOX_OUTPUT_DIR`
       (e.g. a persistent ``/config/media/tts`` volume), serve it under the
       :data:`MEDIA_MOUNT_PATH` mount, preserving the path relative to that
       directory (so ``/config/media/tts/foo.wav`` → ``/media/tts/foo.wav``).
    3. Fallback: serve the bare filename under ``/media/tts`` — matching the
       alternate mount, not the ``/static`` tree, so the URL is not silently
       broken when the file lives outside ``static``.

    Never raises.
    """
    try:
        p = Path(audio_path)
        parts_list = list(p.parts)

        # (1) In-image /static tree.
        try:
            idx = parts_list.index("static")
            return "/" + "/".join(parts_list[idx:])
        except ValueError:
            pass

        # (2) Under the configured output dir → alternate /media mount.
        out_dir = get_vox_output_dir()
        try:
            rel = p.resolve().relative_to(out_dir)
            leaf = out_dir.name  # e.g. "tts"
            return MEDIA_MOUNT_PATH + "/" + leaf + "/" + str(rel).replace("\\", "/")
        except Exception:
            pass

        # (3) Fallback under the alternate mount (not /static).
        return MEDIA_MOUNT_PATH + "/tts/" + p.name
    except Exception:
        return MEDIA_MOUNT_PATH + "/tts/" + str(audio_path).rsplit("/", 1)[-1]
