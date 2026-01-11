#!/usr/bin/env python3
"""generate_icons.py
Generate square icons for the Web UI from the existing `synth_logo_bg.png`.
Requires Pillow (pip install pillow).
It will create synth_icon_180.png, synth_icon_192.png, synth_icon_512.png in res/synth_webui/static.
"""
from pathlib import Path
import sys

try:
    from PIL import Image
except Exception:
    print("Pillow is required: pip install pillow")
    sys.exit(2)

BASE = Path(__file__).resolve().parent.parent / 'res' / 'synth_webui' / 'static'
SRC = BASE / 'synth_logo_bg.png'
if not SRC.exists():
    print(f"Source icon not found: {SRC}")
    sys.exit(1)

with Image.open(SRC) as img:
    # Ensure RGBA
    img = img.convert('RGBA')
    w, h = img.size
    # Crop to square center
    if w != h:
        min_side = min(w, h)
        left = (w - min_side) // 2
        top = (h - min_side) // 2
        img = img.crop((left, top, left + min_side, top + min_side))

    # Reduce artwork slightly and center it on a transparent canvas to avoid clipping
    SCALE = 0.88  # scale factor (88% -> ~12% padding)
    for size in (180, 192, 512):
        inner = int(size * SCALE)
        # Resize the artwork smaller, keeping aspect ratio
        inner_img = img.resize((inner, inner), Image.LANCZOS)
        # Create transparent canvas and paste centered
        canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        offset = ((size - inner) // 2, (size - inner) // 2)
        canvas.paste(inner_img, offset, inner_img)
        out_path = BASE / f'synth_icon_{size}.png'
        canvas.save(out_path, format='PNG')
        print('Wrote', out_path)

print('Icons generated. Add them to your Docker image or bind-mount /config/static as needed.')
