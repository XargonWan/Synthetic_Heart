"""A photo-sized test image for the endpoint vision probe.

The probe must exercise what a chat photo exercises: a JPEG of 100-300 KB, not a
few-kilobyte PNG, so an endpoint that answers a thumbnail but stalls on a real
picture shows up. It is built deterministically (a fixed seed), so two probes send
the same bytes and their timings can be compared.
"""

from __future__ import annotations

import io
import random

_WIDTH = 960
_HEIGHT = 720
_SEED = 20261120


def build_probe_photo() -> bytes:
    """Return a deterministic photo-like JPEG (a sky, a ground and many colourful shapes)."""
    from PIL import Image, ImageDraw

    rng = random.Random(_SEED)
    img = Image.new("RGB", (_WIDTH, _HEIGHT))
    draw = ImageDraw.Draw(img)
    horizon = _HEIGHT // 2
    for y in range(_HEIGHT):
        if y < horizon:
            shade = int(120 + 110 * y / horizon)
            colour = (shade // 2, shade // 1, 255)
        else:
            shade = int(150 - 90 * (y - horizon) / (_HEIGHT - horizon))
            colour = (shade // 2, shade, shade // 3)
        draw.line([(0, y), (_WIDTH, y)], fill=colour)
    for _ in range(900):
        x = rng.randrange(_WIDTH)
        y = rng.randrange(_HEIGHT)
        r = rng.randrange(4, 38)
        fill = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        if rng.random() < 0.5:
            draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)
        else:
            draw.rectangle([x - r, y - r // 2, x + r, y + r // 2], fill=fill)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
