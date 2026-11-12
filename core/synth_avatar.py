# core/synth_avatar.py
"""The synth's profile picture: one image owned by core, pushed to interfaces.

The operator uploads and crops an image in the WebUI (Settings → Synth Avatar).
Core stores the normalized square PNG and, every time it changes, calls
``set_avatar`` on each registered interface that implements it, so interfaces
can set it as their own account's avatar.

Interface contract (optional, structural — see ``core/interface_capabilities``):

``async def set_avatar(self, image_bytes: bytes | None, mime: str | None = None,
version: str | None = None) -> bool``
    Apply the image to the interface's own account; ``None`` means the avatar was
    removed. Return ``True`` when something was applied. Must not raise for
    expected failures (rate limits, not connected).

``set_avatar`` may also take ``force: bool = False``: the operator asked for a
resend, so skip any "already applied" shortcut of its own.

``def uses_synth_avatar(self) -> bool`` (optional)
    Opt-in switch; when absent the interface is treated as opted in. Accounts
    whose avatar is visible to real people (a Telegram user account) default to
    off.

The picture is a presentation asset, not a secret; it is served unauthenticated
on GET like the other WebUI assets, while changing it honours
``SYNTH_WEBUI_API_TOKEN``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from core.logging_utils import log_debug, log_info, log_warning
from core.variables_engine import register_exposed_var

AVATAR_SIZE = 512
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PIXELS = 50_000_000
PER_INTERFACE_TIMEOUT_SECONDS = 45.0
AVATAR_MIME = "image/png"

register_exposed_var(
    "SYNTH_AVATAR",
    label="Synth Avatar",
    default="",
    value_type=str,
    ui_type="avatar",
    description=(
        "Profile picture of the synth. Upload, zoom and drag to frame it; "
        "interfaces that opt in set it as their own account avatar and are "
        "updated whenever it changes. The stored value is the image version."
    ),
    scope="synth",
    component="persona",
    needs_component_reload=False,
)


class AvatarError(ValueError):
    """The uploaded data is not an acceptable avatar image."""


@dataclass(frozen=True)
class AvatarInfo:
    version: str
    size: int


def avatar_path() -> Path:
    from core.app_paths import data_root

    return data_root() / "avatar" / "synth_avatar.png"


def normalize_image(raw: bytes) -> bytes:
    """Validate ``raw`` and return a centered ``AVATAR_SIZE`` square PNG.

    The WebUI already sends the framed square; this re-encodes it anyway so the
    stored file is always a bounded, metadata-free PNG whatever was uploaded.
    """
    if not raw:
        raise AvatarError("Empty image")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AvatarError(f"Image larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception as exc:  # pragma: no cover - Pillow is a dependency
        raise AvatarError("Image support (Pillow) is not available") from exc

    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > MAX_SOURCE_PIXELS:
            raise AvatarError("Image dimensions are too large")
        image = ImageOps.exif_transpose(image) or image
        image = image.convert("RGBA")
    except AvatarError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise AvatarError("Not a valid image") from exc

    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize(
        (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
    )
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def load_avatar() -> Optional[tuple[bytes, str]]:
    """Return ``(png_bytes, version)`` or ``None`` when no avatar is set."""
    path = avatar_path()
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return (data, version_of(data)) if data else None


def current_version() -> Optional[str]:
    loaded = load_avatar()
    return loaded[1] if loaded else None


def save_avatar(raw: bytes) -> AvatarInfo:
    """Normalize and store the avatar atomically."""
    png = normalize_image(raw)
    path = avatar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    tmp.replace(path)
    return AvatarInfo(version=version_of(png), size=len(png))


def clear_avatar() -> bool:
    try:
        avatar_path().unlink()
        return True
    except FileNotFoundError:
        return False


async def persist_version(version: Optional[str]) -> None:
    """Mirror the version into the config registry (shown by Settings)."""
    try:
        from core.config_manager import config_registry

        await config_registry.set_value("SYNTH_AVATAR", version or "")
    except Exception as exc:
        log_debug(f"[synth_avatar] could not persist version: {exc}")


def register_interface_switch(
    interface_id: str, *, default: bool = True
) -> Callable[[], bool]:
    """Register the per-interface "Use Synth Avatar" option; return its reader.

    Every interface that can take the avatar calls this once at import and
    returns the reader from ``uses_synth_avatar``. The option shows up in that
    interface's own settings, next to its other variables.
    """
    key = f"{interface_id.upper()}_USE_SYNTH_AVATAR"
    register_exposed_var(
        key,
        label="Use Synth Avatar",
        default=default,
        value_type=bool,
        ui_type="bool",
        description=(
            "Set the synth avatar (Settings → Synth Avatar) as this account's "
            "profile picture and keep it updated when the avatar changes."
        ),
        scope="interface",
        component=interface_id,
    )

    def enabled() -> bool:
        try:
            from core.config_manager import config_registry

            return bool(config_registry.get_value(key, default, value_type=bool))
        except Exception:
            return default

    return enabled


def _applied_key(name: str) -> str:
    return f"SYNTH_AVATAR_APPLIED__{name}"


def applied_version(name: str) -> str:
    """Version of the avatar last applied to ``name`` ('' = none / removed)."""
    try:
        from core.config_manager import config_registry

        return str(
            config_registry.get_value(
                _applied_key(name),
                "",
                value_type=str,
                component="persona",
                hidden=True,
                label=f"{name} applied avatar",
                description="Avatar version last applied to this interface.",
            )
            or ""
        )
    except Exception:
        return ""


async def _remember_applied(name: str, version: Optional[str]) -> None:
    try:
        from core.config_manager import config_registry

        applied_version(name)  # make sure the definition exists before writing
        await config_registry.set_value(_applied_key(name), version or "")
    except Exception as exc:
        log_debug(f"[synth_avatar] could not record applied version for {name}: {exc}")


def _accepts_force(func: Any) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return "force" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _avatar_interfaces() -> dict[str, Any]:
    from core.core_initializer import INTERFACE_REGISTRY

    return {
        name: iface
        for name, iface in list(INTERFACE_REGISTRY.items())
        if callable(getattr(iface, "set_avatar", None))
    }


def interface_support() -> list[dict[str, Any]]:
    """Which registered interfaces can take the avatar, and which opted in."""
    rows = []
    for name, iface in sorted(_avatar_interfaces().items()):
        opt = getattr(iface, "uses_synth_avatar", None)
        try:
            enabled = bool(opt()) if callable(opt) else True
        except Exception:
            enabled = False
        # An interface with no credentials is registered but not connected: it
        # cannot take a picture until it is (``is_enabled`` is the common flag).
        connected = getattr(iface, "is_enabled", True) is not False
        rows.append(
            {
                "name": name,
                "enabled": enabled,
                "connected": connected,
                "applied": applied_version(name),
            }
        )
    return rows


async def _apply_one(
    name: str,
    iface: Any,
    data: Optional[bytes],
    version: Optional[str],
    force: bool = False,
    catch_up: bool = False,
) -> str:
    opt = getattr(iface, "uses_synth_avatar", None)
    try:
        if callable(opt) and not opt():
            return "skipped: not enabled for this interface"
        # A catch-up (startup, interface switched on) only repairs an interface
        # that is out of date; it never makes a first, unrequested change to an
        # account. That is what an upload or "Resend avatar" is for.
        if catch_up and not applied_version(name):
            return "skipped: never synced (use Resend avatar)"
        # Unchanged since the last time: nothing to send (and Discord, for one,
        # rate-limits avatar changes hard). A resend skips this.
        if not force and applied_version(name) == (version or ""):
            return "skipped: already applied"
        call_kwargs: dict[str, Any] = {}
        if force and _accepts_force(iface.set_avatar):
            call_kwargs["force"] = True
        applied = await asyncio.wait_for(
            iface.set_avatar(
                data, AVATAR_MIME if data else None, version, **call_kwargs
            ),
            timeout=PER_INTERFACE_TIMEOUT_SECONDS,
        )
        if applied:
            await _remember_applied(name, version)
        return "applied" if applied else "skipped: not applied"
    except asyncio.TimeoutError:
        log_warning(f"[synth_avatar] {name} timed out")
        return "failed: timed out"
    except Exception as exc:
        log_warning(f"[synth_avatar] {name} failed: {type(exc).__name__}: {exc}")
        return f"failed: {type(exc).__name__}"


async def broadcast_avatar_changed(
    *, force: bool = False, only: Optional[str] = None, catch_up: bool = False
) -> dict[str, str]:
    """Push the current avatar (or its removal) to every interface that takes it.

    One interface failing or hanging never affects the others or the save.
    ``force`` re-sends even where the same version was already applied (the
    "Resend avatar" button); ``only`` limits the push to one interface (used when
    an interface starts or is switched on); ``catch_up`` restricts the push to
    interfaces that already received an avatar from here and are now out of date.
    Returns ``{interface_name: status}``.
    """
    loaded = load_avatar()
    data, version = loaded if loaded else (None, None)
    targets = _avatar_interfaces()
    if only is not None:
        targets = {k: v for k, v in targets.items() if k == only}
    if not targets:
        return {}
    names = list(targets)
    statuses = await asyncio.gather(
        *(
            _apply_one(name, targets[name], data, version, force, catch_up)
            for name in names
        )
    )
    results = dict(zip(names, statuses))
    log_info(f"[synth_avatar] broadcast result: {results}")
    return results


async def sync_interface(name: str) -> str:
    """Bring one interface up to date (after it starts or is switched on)."""
    results = await broadcast_avatar_changed(only=name, catch_up=True)
    return results.get(name, "skipped: no avatar support")
