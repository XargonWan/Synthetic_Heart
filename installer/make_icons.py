"""Build the installer's icons from the repository's logo artwork.

One look, on transparency. ``docs/res/synth_logo_wblack.png`` is the heart with no
background: the circuit half is black and the eye half is cyan, so on a dark surface
the eye reads and on a light one the traces read. That trade-off belongs to the
artwork.

The dark rounded-square version (``website/assets/synth_logo_bg.png``) is the brand's
logo *with* a background and is not used for icons: on Windows it shows up as a black
tile on the desktop, in the Start menu and next to the Add/Remove Programs entry, which
is not what the icon should look like.

Two files, because two things reference them:

* ``synth.ico`` - the installer itself, the shortcuts it creates and the uninstall
  entry (``SetupIconFile``, ``IconFilename``, ``UninstallDisplayIcon``).
* ``synth-tray.ico`` - the notification-area icon (``scripts/synth_tray.ps1``).

Both are cut from the same artwork with the same padding, so the heart is the same
size wherever it appears.

Run from the repository root:  ``python installer/make_icons.py``
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = REPO_ROOT / "installer"

# Windows asks for these when it draws a taskbar, alt-tab or Explorer icon.
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# The heart on transparency. The squircle artwork is deliberately not used here.
LOGO_SOURCE = REPO_ROOT / "docs" / "res" / "synth_logo_wblack.png"

# Padding around the artwork, as a fraction of its longest side. Enough that the heart
# does not touch the edges of a 16px icon, small enough that it still fills it.
MARGIN = 0.06


def _square_canvas_with_margin(image: Image.Image, margin: float) -> Image.Image:
    """Crop to the visible artwork and centre it on a padded square canvas.

    Without this the heart keeps whatever empty space the artwork happens to have
    around it, which is a lot vertically, and the icon comes out small and
    off-centre in the notification area.
    """
    rgba = image.convert("RGBA")
    visible = (
        rgba.getchannel("A").point(lambda value: 255 if value > 8 else 0).getbbox()
    )
    if visible:
        rgba = rgba.crop(visible)

    side = int(max(rgba.size) * (1 + 2 * margin))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(
        rgba,
        ((side - rgba.width) // 2, (side - rgba.height) // 2),
    )
    return canvas


def _write_ico(destination: Path, margin: float = MARGIN) -> Path:
    artwork = _square_canvas_with_margin(Image.open(LOGO_SOURCE), margin)
    artwork.save(
        destination,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
    )
    return destination


def main() -> None:
    """Write every icon the installer ships, and report what was written."""
    written = [
        _write_ico(ICON_DIR / "synth.ico"),
        _write_ico(ICON_DIR / "synth-tray.ico"),
    ]

    # A flat PNG for anything that wants a plain image rather than an icon.
    logo = _square_canvas_with_margin(Image.open(LOGO_SOURCE), MARGIN)
    png = ICON_DIR / "synth-256.png"
    logo.resize((256, 256), Image.Resampling.LANCZOS).save(png, format="PNG")
    written.append(png)

    for path in written:
        print(f"{path.relative_to(REPO_ROOT)}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
