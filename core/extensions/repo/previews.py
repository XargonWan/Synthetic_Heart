"""The picture a skin is shown with in the catalog: its own ``preview`` image.

A skin ships ``<its folder>/preview.png`` (the Skins tab shows it). The catalog shows it
before the skin is installed, so it travels like the pictures of a ``screenshots/`` folder, as
the first one. A preview can be far bigger than a catalog picture may be (the limit is
:data:`core.extensions.repo.index.MAX_SCREENSHOT_BYTES`): it is then shrunk, never refused.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from core.extensions.repo.index import MAX_SCREENSHOT_BYTES

#: What a skin's preview may be called, most usual first.
PREVIEW_NAMES = ("preview.png", "preview.jpg", "preview.jpeg", "preview.webp")
#: The longest edge of a shrunk preview.
MAX_EDGE = 1280


def preview_name(names: Iterable[str], folder: str) -> str | None:
    """The member of ``names`` that is the preview of the skin in ``folder``, or ``None``."""
    present = set(names)
    prefix = folder.strip("/")
    for candidate in PREVIEW_NAMES:
        member = f"{prefix}/{candidate}" if prefix else candidate
        if member in present:
            return member
    return None


def fit(
    data: bytes, suffix: str, limit: int = MAX_SCREENSHOT_BYTES
) -> tuple[bytes, str] | None:
    """``data`` as a picture of at most ``limit`` bytes, and its suffix.

    A picture that already fits is returned as it is. A bigger one is scaled down (and, if
    that is still too big, re-encoded as WebP, which keeps transparency). ``None`` when it
    cannot be done: not a picture, or no way to make it small enough."""
    if 0 < len(data) <= limit:
        return data, suffix
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image = source.convert("RGBA") if source.mode in ("P", "LA") else source
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            for fmt, ext, options in (
                ("PNG", ".png", {"optimize": True}),
                ("WEBP", ".webp", {"quality": 85, "method": 6}),
                ("WEBP", ".webp", {"quality": 60, "method": 6}),
            ):
                out = io.BytesIO()
                image.save(out, format=fmt, **options)
                if 0 < out.tell() <= limit:
                    return out.getvalue(), ext
    except Exception:  # noqa: BLE001 - not a picture, or Pillow cannot write it
        return None
    return None
