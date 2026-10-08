import asyncio
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core import synth_avatar

PIL = pytest.importorskip("PIL.Image")


def _png(width: int, height: int, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    PIL.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "avatar" / "synth_avatar.png"
    monkeypatch.setattr(synth_avatar, "avatar_path", lambda: path)
    return path


# --- storage -----------------------------------------------------------------


def test_normalize_center_crops_to_a_square_png() -> None:
    out = synth_avatar.normalize_image(_png(800, 200))
    image = PIL.open(io.BytesIO(out))
    assert image.format == "PNG"
    assert image.size == (synth_avatar.AVATAR_SIZE, synth_avatar.AVATAR_SIZE)


@pytest.mark.parametrize("raw", [b"", b"not an image", b"\x89PNG\r\n\x1a\nbroken"])
def test_normalize_rejects_garbage(raw: bytes) -> None:
    with pytest.raises(synth_avatar.AvatarError):
        synth_avatar.normalize_image(raw)


def test_normalize_rejects_oversized_upload(monkeypatch) -> None:
    monkeypatch.setattr(synth_avatar, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(synth_avatar.AvatarError):
        synth_avatar.normalize_image(_png(64, 64))


def test_save_load_version_and_clear(store) -> None:
    assert synth_avatar.load_avatar() is None
    info = synth_avatar.save_avatar(_png(64, 64))
    data, version = synth_avatar.load_avatar() or (b"", "")
    assert version == info.version == synth_avatar.current_version()
    assert PIL.open(io.BytesIO(data)).size == (512, 512)
    # A different picture is a different version (what makes interfaces re-apply).
    other = synth_avatar.save_avatar(_png(64, 64, (0, 0, 255)))
    assert other.version != info.version
    assert synth_avatar.clear_avatar() is True
    assert synth_avatar.load_avatar() is None
    assert synth_avatar.clear_avatar() is False


# --- broadcast -----------------------------------------------------------------


class _Iface:
    def __init__(self, result: Any = True, enabled: bool = True) -> None:
        self.calls: list[tuple[Any, Any, Any]] = []
        self._result, self._enabled = result, enabled

    def uses_synth_avatar(self) -> bool:
        return self._enabled

    async def set_avatar(self, data, mime=None, version=None):
        self.calls.append((data, mime, version))
        if isinstance(self._result, Exception):
            raise self._result
        if self._result == "hang":
            await asyncio.sleep(60)
        return self._result


@pytest.mark.asyncio
async def test_broadcast_isolates_failures_and_respects_opt_in(
    store, monkeypatch
) -> None:
    info = synth_avatar.save_avatar(_png(64, 64))
    good, bad, off, hung = (
        _Iface(),
        _Iface(RuntimeError("boom")),
        _Iface(enabled=False),
        _Iface("hang"),
    )
    registry = {
        "good": good,
        "bad": bad,
        "off": off,
        "hung": hung,
        "plain": SimpleNamespace(),  # no set_avatar: not a target at all
    }
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", registry)
    monkeypatch.setattr(synth_avatar, "PER_INTERFACE_TIMEOUT_SECONDS", 0.05)

    results = await synth_avatar.broadcast_avatar_changed()

    assert set(results) == {"good", "bad", "off", "hung"}
    assert results["good"] == "applied"
    assert results["bad"].startswith("failed")
    assert results["off"].startswith("skipped")
    assert results["hung"] == "failed: timed out"
    assert off.calls == []
    data, mime, version = good.calls[0]
    assert mime == "image/png" and version == info.version and data


@pytest.mark.asyncio
async def test_broadcast_after_removal_passes_none(store, monkeypatch) -> None:
    iface = _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})
    await synth_avatar.broadcast_avatar_changed()
    assert iface.calls == [(None, None, None)]


def test_interface_support_lists_capable_interfaces(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY",
        {"a": _Iface(enabled=False), "b": SimpleNamespace()},
    )
    assert synth_avatar.interface_support() == [{"name": "a", "enabled": False}]


def test_set_avatar_is_a_structural_capability() -> None:
    from core.interface_capabilities import interface_capabilities

    assert "set_avatar" in interface_capabilities(_Iface())
    assert "set_avatar" not in interface_capabilities(SimpleNamespace())


# --- WebUI endpoints ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_endpoint_stores_persists_and_broadcasts(
    store, monkeypatch
) -> None:
    from fastapi import HTTPException

    from core.webui import WebUI

    iface = _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"tg": iface})
    persisted: list[Any] = []
    monkeypatch.setattr(
        synth_avatar, "persist_version", AsyncMock(side_effect=persisted.append)
    )

    class _Upload:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def read(self, _n: int = -1) -> bytes:
            return self._data

        async def close(self) -> None:
            return None

    response = await WebUI.upload_synth_avatar(None, _Upload(_png(300, 200)))  # type: ignore[arg-type]
    body = response.body.decode()
    assert '"success":true' in body and '"tg":"applied"' in body
    assert persisted == [synth_avatar.current_version()]
    assert len(iface.calls) == 1

    with pytest.raises(HTTPException) as excinfo:
        await WebUI.upload_synth_avatar(None, _Upload(b"not an image"))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 400
