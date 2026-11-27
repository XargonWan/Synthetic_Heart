"""Pack the default extensions into ``extensions_bundled/`` for the image.

The folder is the read-only repository the Core installs first-start defaults from
(see ``core.extensions.runtime.bundled_repository``): the packages, a signed index and
the matching ``public.key``. The signing key is made for this build and thrown away:
the key only has to match the index it signs, inside this image.

    uv run python scripts/build_bundled_extensions.py --sources DIR [--out DIR] [--default ID ...]

``--sources`` is the folder holding one directory per extension: the ``extensions/`` folder of
the official plugins repository (github.com/XargonWan/synthetic-heart-official-plugins), which
the Dockerfile clones for the build. ``SYNTH_EXTENSIONS_SOURCES`` sets it from the environment.

Packages that are not defaults can be added with ``--also``; they are in the index but
not installed on their own.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.extensions.repo.index import generate_keypair  # noqa: E402
from core.extensions.repo.pack import build_index, pack_extension  # noqa: E402

#: What a fresh image installs on its own.
DEFAULTS = (
    "official/synth_stage",
    "official/synth_dashboard",
    "official/openai_api",
    "official/fluxer",
    "official/telegram_bot",
    "official/zen",
    "official/protocol_harmony",
    "official/protocol_fish",
    "official/vox_kitten",
    "official/auris_vosk",
)


def source_of(extension_id: str, sources: Path) -> Path:
    return sources / extension_id.split("/", 1)[1]


def build(
    out: Path,
    defaults: tuple[str, ...],
    sources: Path,
    also: tuple[str, ...] = (),
) -> Path:
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for extension_id in (*defaults, *also):
        source = source_of(extension_id, sources)
        if not (source / "synth-extension.toml").is_file():
            raise SystemExit(f"no extension source for {extension_id} at {source}")
        archive, manifest = pack_extension(source, out)
        print(f"packed {manifest.id} {manifest.version} -> {archive.name}")
    private, public = generate_keypair()
    build_index(out, name="bundled", private_key=private, default_install=defaults)
    (out / "public.key").write_text(public + "\n", encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "extensions_bundled")
    parser.add_argument("--default", action="append", dest="defaults")
    parser.add_argument("--also", action="append", default=[])
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path(os.environ["SYNTH_EXTENSIONS_SOURCES"])
        if os.environ.get("SYNTH_EXTENSIONS_SOURCES")
        else None,
        required="SYNTH_EXTENSIONS_SOURCES" not in os.environ,
        help="folder with one directory per extension",
    )
    args = parser.parse_args()
    build(
        args.out,
        tuple(args.defaults or DEFAULTS),
        args.sources,
        tuple(args.also),
    )
    print(f"bundled repository written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
