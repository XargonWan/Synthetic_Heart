"""Generate original, non-branded 256x256 component icons for the WebUI.

The project rule (AGENTS.md / docs/plugins.rst) allows committing a third-party
brand/trademark logo only when its owner's licence or press kit permits it
(attributed in ``LICENSE_EXTERNAL.md``). For services without such a grant — or
where an original mark is preferable — this script draws original glyphs that
*evoke* each service (a paper-plane, a speech bubble, a grid of dots, a llama
silhouette) on the SyntH accent-coloured rounded background — the same visual
language as the bundled ``radio_host`` icon.

Run it with:

    uv run python scripts/generate_component_icons.py

Output: ``res/synth_webui/static/component_icons/<name>.png`` (256x256 RGBA).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / "res"
    / "synth_webui"
    / "static"
    / "component_icons"
)

# Per-service accent palette (background gradient stops are approximated with a
# flat rounded tile + subtle inner highlight). Colours are generic, not brand
# assets.
PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "telegram_bot": ((41, 128, 185), (255, 255, 255)),  # blue tile, white plane
    "discord_bot": ((88, 101, 242), (255, 255, 255)),  # indigo tile, white face
    "matrix_chat": ((30, 30, 30), (255, 255, 255)),  # dark tile, white brackets
    "ollama_serve": ((60, 60, 66), (245, 245, 245)),  # slate tile, light llama
}


def _rounded_tile(fg: tuple[int, int, int]) -> Image.Image:
    """Return a 256x256 RGBA image with a rounded-rect coloured background."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = 56
    draw.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=radius, fill=fg + (255,))
    # subtle top highlight
    draw.rounded_rectangle(
        [8, 8, SIZE - 8, SIZE // 2], radius=radius, fill=(255, 255, 255, 26)
    )
    return img


def _draw_telegram(draw: ImageDraw.ImageDraw, fg: tuple[int, int, int]) -> None:
    """A stylised paper plane (generic messaging glyph)."""
    plane = [
        (58, 128),
        (198, 74),
        (168, 190),
        (128, 150),
        (108, 186),
        (104, 150),
    ]
    draw.polygon(plane, fill=fg + (255,))
    draw.line([(198, 74), (108, 150)], fill=(0, 0, 0, 40), width=3)


def _draw_discord(draw: ImageDraw.ImageDraw, fg: tuple[int, int, int]) -> None:
    """A rounded speech bubble with two dot 'eyes' (generic chat glyph)."""
    draw.rounded_rectangle([60, 74, 196, 168], radius=40, fill=fg + (255,))
    # tail
    draw.polygon([(84, 160), (84, 196), (116, 168)], fill=fg + (255,))
    # eyes
    draw.ellipse([98, 108, 118, 134], fill=(88, 101, 242, 255))
    draw.ellipse([138, 108, 158, 134], fill=(88, 101, 242, 255))


def _draw_matrix(draw: ImageDraw.ImageDraw, fg: tuple[int, int, int]) -> None:
    """Two tall brackets around a dot column (generic 'matrix'/grid glyph)."""
    lw = 12
    # left bracket
    draw.line([(84, 66), (66, 66)], fill=fg + (255,), width=lw)
    draw.line([(66, 66), (66, 190)], fill=fg + (255,), width=lw)
    draw.line([(66, 190), (84, 190)], fill=fg + (255,), width=lw)
    # right bracket
    draw.line([(172, 66), (190, 66)], fill=fg + (255,), width=lw)
    draw.line([(190, 66), (190, 190)], fill=fg + (255,), width=lw)
    draw.line([(190, 190), (172, 190)], fill=fg + (255,), width=lw)
    # centre dots
    for i, y in enumerate((100, 128, 156)):
        r = 12
        cx = 128
        draw.ellipse([cx - r, y - r, cx + r, y + r], fill=fg + (255,))


def _draw_ollama(draw: ImageDraw.ImageDraw, fg: tuple[int, int, int]) -> None:
    """A minimal llama silhouette (generic local-model glyph)."""
    # body
    draw.rounded_rectangle([96, 120, 168, 196], radius=20, fill=fg + (255,))
    # neck
    draw.rounded_rectangle([120, 70, 150, 140], radius=14, fill=fg + (255,))
    # head
    draw.rounded_rectangle([112, 56, 158, 92], radius=16, fill=fg + (255,))
    # ears
    draw.polygon([(116, 60), (110, 34), (128, 54)], fill=fg + (255,))
    draw.polygon([(154, 60), (160, 34), (142, 54)], fill=fg + (255,))
    # legs
    for x in (104, 150):
        draw.rounded_rectangle([x, 188, x + 14, 212], radius=6, fill=fg + (255,))
    # eye (punch-out)
    draw.ellipse([132, 66, 144, 78], fill=(60, 60, 66, 255))


DRAWERS = {
    "telegram_bot": _draw_telegram,
    "discord_bot": _draw_discord,
    "matrix_chat": _draw_matrix,
    "ollama_serve": _draw_ollama,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, (bg, fg) in PALETTE.items():
        img = _rounded_tile(bg)
        draw = ImageDraw.Draw(img)
        DRAWERS[name](draw, fg)
        out = OUT_DIR / f"{name}.png"
        img.save(out, "PNG")
        print(f"wrote {out} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
