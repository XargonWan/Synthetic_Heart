"""Embodiment assets: what a Rift Vessel world is told about Synth's body.

Until now a Vessel never sent a model or an animation to the game it entered (the
one exception, Minecraft's skin, is a PNG the *server* fetches). A world that draws
Synth itself (RetroXR) needs the Karada body: the VRM model, the animations to play
and the files to download.

Nothing is pushed. The world asks (``request_assets`` on its socket, or ``hello``
naming the animations it wants) and gets a description here; it then downloads the
files from the existing ``/skins/...`` static route, using ``/api/karada/assets/manifest``
and ``/api/karada/assets/missing`` to fetch only what its cache lacks.

Animations are looked up by NAME in the active skin, in this order:

1. ``animations/vessel/<name>/``   -- the vessel namespace, shared by every world
2. ``animations/<name>/``          -- a Karada state folder (``greet``, ``think``, ...)
3. a loose clip at the top of ``animations/`` whose file name has a word that starts with the name
   (``Texting And Walking.fbx`` is a ``walk``): skins ship such files without a folder, and a body that
   cannot walk is worse than one that walks with a phone in its hand
4. the same places in the fallback skin (``Rei``), as Karada itself falls back.

so an animation a world needs (walk, sit, pick_up) is added once to the skin and any
other game can use it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_warning

SKINS_ROOT = Path(__file__).resolve().parent.parent / "skins"
FALLBACK_SKIN = "Rei"
VESSEL_NAMESPACE = "vessel"
_ANIM_SUFFIXES = {".fbx"}


def _skin_from_model_url(url: str | None) -> Optional[str]:
    """``/skins/Rei/model.vrm`` -> ``Rei``; anything else -> ``None``."""
    if not url:
        return None
    parts = [p for p in str(url).split("/") if p]
    if len(parts) >= 3 and parts[0] == "skins":
        return parts[1]
    return None


def _url_for(path: Path, skins_root: Path) -> str:
    return f"/skins/{path.relative_to(skins_root).as_posix()}"


def _clip_entry(fbx: Path, skins_root: Path) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"file": fbx.name, "url": _url_for(fbx, skins_root)}
    descriptor = fbx.with_name(fbx.name + ".json")
    if descriptor.is_file():
        entry["descriptor_url"] = _url_for(descriptor, skins_root)
        try:
            entry["descriptor"] = json.loads(descriptor.read_text(encoding="utf-8"))
        except Exception as exc:
            log_warning(f"[embodiment_assets] bad descriptor {descriptor}: {exc}")
    return entry


def _clips_in(folder: Path, skins_root: Path) -> List[Dict[str, Any]]:
    """Every clip under ``folder`` (variants live in sub-folders), sorted."""
    if not folder.is_dir():
        return []
    files = sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in _ANIM_SUFFIXES
    )
    return [_clip_entry(p, skins_root) for p in files]


def _safe_name(name: str) -> bool:
    """A bare folder name: no separators, no dots-only, nothing that climbs out."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _loose_clips(base: Path, name: str, skins_root: Path) -> List[Dict[str, Any]]:
    """Files directly in ``base`` with a word in their name that starts with ``name`` ("Texting And Walking" ~ "walk")."""
    if not base.is_dir():
        return []
    wanted = name.lower()
    found: List[Path] = []
    for p in sorted(base.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _ANIM_SUFFIXES:
            continue
        words = re.split(r"[^a-z0-9]+", p.stem.lower())
        if any(w.startswith(wanted) for w in words if w):
            found.append(p)
    return [_clip_entry(p, skins_root) for p in found]


def resolve_animation(
    name: str, skin: str, skins_root: Path = SKINS_ROOT
) -> Optional[Dict[str, Any]]:
    """Find animation ``name`` for ``skin`` (vessel namespace first, fallback skin last)."""
    if not _safe_name(name) or not _safe_name(skin):
        return None
    for skin_name in dict.fromkeys((skin, FALLBACK_SKIN)):
        base = skins_root / skin_name / "animations"
        for folder in (base / VESSEL_NAMESPACE / name, base / name):
            clips = _clips_in(folder, skins_root)
            if clips:
                return {
                    "name": name,
                    "skin": skin_name,
                    "namespace": VESSEL_NAMESPACE if folder.parent.name == VESSEL_NAMESPACE else "state",
                    "clips": clips,
                }
        loose = _loose_clips(base, name, skins_root)
        if loose:
            return {"name": name, "skin": skin_name, "namespace": "loose", "clips": loose}
    return None


async def describe_assets(
    wanted_animations: List[str] | None = None,
    skins_root: Path = SKINS_ROOT,
) -> Dict[str, Any]:
    """Describe the body for a world: model, requested animations, what is missing."""
    skin = FALLBACK_SKIN
    model: Dict[str, Any] = {}
    try:
        from core.animation_handler import get_karada_state_server

        full = await get_karada_state_server().get_full_state()
        vrm = full.get("vrm_model") or {}
        if vrm.get("url"):
            model = {"name": vrm.get("name"), "url": vrm.get("url"), "hash": vrm.get("hash")}
            skin = _skin_from_model_url(vrm.get("url")) or skin
    except Exception as exc:
        log_debug(f"[embodiment_assets] Karada state unavailable: {exc}")

    if not model:
        candidate = skins_root / skin / "model.vrm"
        if candidate.is_file():
            model = {"name": candidate.name, "url": _url_for(candidate, skins_root), "hash": None}

    animations: Dict[str, Any] = {}
    missing: List[str] = []
    for name in wanted_animations or []:
        found = resolve_animation(str(name), skin, skins_root)
        if found is None:
            missing.append(str(name))
        else:
            animations[str(name)] = found
    if missing:
        log_debug(f"[embodiment_assets] skin {skin!r} has no animation for {missing}")

    return {
        "skin": skin,
        "model": model,
        "animations": animations,
        "missing_animations": missing,
        "manifest_url": "/api/karada/assets/manifest",
        "missing_url": "/api/karada/assets/missing",
    }
