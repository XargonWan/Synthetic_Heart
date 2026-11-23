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


@pytest.fixture(autouse=True)
def applied(monkeypatch) -> dict[str, str]:
    """In-memory stand-in for the per-interface "version last applied" store."""
    versions: dict[str, str] = {}

    async def remember(name: str, version: Any) -> None:
        versions[name] = version or ""

    monkeypatch.setattr(synth_avatar, "applied_version", lambda n: versions.get(n, ""))
    monkeypatch.setattr(synth_avatar, "_remember_applied", remember)
    return versions


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
        self.forced = False
        self._result, self._enabled = result, enabled

    def uses_synth_avatar(self) -> bool:
        return self._enabled

    async def set_avatar(self, data, mime=None, version=None, force=False):
        self.calls.append((data, mime, version))
        self.forced = force
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
async def test_broadcast_after_removal_passes_none(store, monkeypatch, applied) -> None:
    iface = _Iface()
    applied["x"] = "oldversion"  # a picture was applied earlier
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})
    await synth_avatar.broadcast_avatar_changed()
    assert iface.calls == [(None, None, None)]
    assert applied["x"] == ""


@pytest.mark.asyncio
async def test_removal_with_nothing_applied_touches_nothing(store, monkeypatch) -> None:
    iface = _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})
    results = await synth_avatar.broadcast_avatar_changed()
    assert iface.calls == []
    assert results["x"] == "skipped: already applied"


@pytest.mark.asyncio
async def test_an_unchanged_avatar_is_not_pushed_again(
    store, monkeypatch, applied
) -> None:
    info = synth_avatar.save_avatar(_png(64, 64))
    iface = _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})

    first = await synth_avatar.broadcast_avatar_changed()
    second = await synth_avatar.broadcast_avatar_changed()

    assert first["x"] == "applied" and applied["x"] == info.version
    assert second["x"] == "skipped: already applied"
    assert len(iface.calls) == 1


@pytest.mark.asyncio
async def test_a_failed_push_is_retried_next_time(store, monkeypatch, applied) -> None:
    synth_avatar.save_avatar(_png(64, 64))
    iface = _Iface(result=False)
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})

    await synth_avatar.broadcast_avatar_changed()
    await synth_avatar.broadcast_avatar_changed()

    assert len(iface.calls) == 2  # nothing was recorded as applied
    assert applied == {}


@pytest.mark.asyncio
async def test_resend_pushes_again_and_tells_the_interface_it_is_forced(
    store, monkeypatch, applied
) -> None:
    info = synth_avatar.save_avatar(_png(64, 64))
    iface = _Iface()
    applied["x"] = info.version  # already up to date
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"x": iface})

    results = await synth_avatar.broadcast_avatar_changed(force=True)

    assert results["x"] == "applied"
    assert len(iface.calls) == 1 and iface.forced is True


@pytest.mark.asyncio
async def test_resend_works_with_interfaces_that_do_not_know_force(
    store, monkeypatch
) -> None:
    synth_avatar.save_avatar(_png(64, 64))
    calls: list[Any] = []

    class _Old:
        async def set_avatar(self, data, mime=None, version=None):
            calls.append(version)
            return True

    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"old": _Old()})

    assert (await synth_avatar.broadcast_avatar_changed(force=True))["old"] == "applied"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_only_limits_the_push_to_one_interface(
    store, monkeypatch, applied
) -> None:
    synth_avatar.save_avatar(_png(64, 64))
    a, b = _Iface(), _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"a": a, "b": b})
    applied["b"] = "an-older-version"  # b already got an avatar from here

    assert await synth_avatar.sync_interface("b") == "applied"
    assert a.calls == [] and len(b.calls) == 1
    assert await synth_avatar.sync_interface("nope") == "skipped: no avatar support"


def test_the_interface_switch_is_a_registered_option(monkeypatch) -> None:
    from core.config_manager import config_registry

    switch = synth_avatar.register_interface_switch("fake_avatar_iface", default=True)
    assert "FAKE_AVATAR_IFACE_USE_SYNTH_AVATAR" in {
        d["key"] for d in config_registry.export_definitions()
    }
    assert switch() is True


def test_interface_support_lists_capable_interfaces(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY",
        {"a": _Iface(enabled=False), "b": SimpleNamespace()},
    )
    assert synth_avatar.interface_support() == [
        {"name": "a", "enabled": False, "connected": True, "applied": ""}
    ]


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


@pytest.mark.asyncio
async def test_resend_endpoint_forces_a_push_and_needs_an_avatar(
    store, monkeypatch, applied
) -> None:
    from fastapi import HTTPException

    from core.webui import WebUI

    with pytest.raises(HTTPException) as excinfo:
        await WebUI.resend_synth_avatar(None)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 404

    info = synth_avatar.save_avatar(_png(64, 64))
    applied["tg"] = info.version
    iface = _Iface()
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"tg": iface})

    response = await WebUI.resend_synth_avatar(None)  # type: ignore[arg-type]

    assert '"tg":"applied"' in bytes(response.body).decode()
    assert iface.forced is True


@pytest.mark.asyncio
async def test_a_catch_up_never_makes_a_first_unrequested_change(
    store, monkeypatch
) -> None:
    synth_avatar.save_avatar(_png(64, 64))
    fresh = _Iface()  # never received an avatar from here
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"fresh": fresh})

    results = await synth_avatar.broadcast_avatar_changed(catch_up=True)

    assert results["fresh"] == "skipped: never synced (use Resend avatar)"
    assert fresh.calls == []
    # An explicit resend does it.
    assert (await synth_avatar.broadcast_avatar_changed(force=True))["fresh"] == (
        "applied"
    )


@pytest.mark.asyncio
async def test_a_catch_up_repairs_an_interface_that_is_out_of_date(
    store, monkeypatch, applied
) -> None:
    info = synth_avatar.save_avatar(_png(64, 64))
    stale = _Iface()
    applied["stale"] = "an-older-version"
    monkeypatch.setattr("core.core_initializer.INTERFACE_REGISTRY", {"stale": stale})

    results = await synth_avatar.broadcast_avatar_changed(catch_up=True)

    assert results["stale"] == "applied"
    assert applied["stale"] == info.version


def test_the_editor_registers_with_the_renderer_and_offers_resend() -> None:
    from pathlib import Path

    js = Path(__file__).resolve().parents[1] / "res" / "synth_webui" / "js"
    editor = (js / "avatar-editor.js").read_text(encoding="utf-8")
    main = (js / "main.js").read_text(encoding="utf-8")
    helpers = (js / "ui-helpers.js").read_text(encoding="utf-8")

    assert "window.registerUiType('avatar'" in editor
    assert "/api/synth_avatar/resend" in editor and "Resend avatar" in editor
    assert "window.registerUiType" in helpers and "window.getUiType" in helpers
    # Both settings renderers look the type up instead of hard-coding it.
    assert main.count("const registeredType = window.getUiType") == 2
    assert "item.ui_type === 'avatar'" not in main


def test_an_interface_without_credentials_is_listed_as_not_connected(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY",
        {
            "idle": SimpleNamespace(set_avatar=lambda *a, **k: None, is_enabled=False),
            "live": SimpleNamespace(set_avatar=lambda *a, **k: None, is_enabled=True),
        },
    )
    rows = {r["name"]: r["connected"] for r in synth_avatar.interface_support()}
    assert rows == {"idle": False, "live": True}
