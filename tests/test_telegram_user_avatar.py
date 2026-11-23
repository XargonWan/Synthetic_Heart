from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

try:
    import interface.telegram as tguser
except Exception:
    pytest.skip("telethon not installed", allow_module_level=True)


class _Client:
    def __init__(self) -> None:
        self.uploaded: list[Any] = []
        self.requests: list[Any] = []

    async def upload_file(self, data, file_name=None):
        self.uploaded.append((data, file_name))
        return "INPUT_FILE"

    async def __call__(self, request):
        self.requests.append(request)

    async def get_profile_photos(self, who, limit=None):
        return [SimpleNamespace(id=1, access_hash=2, file_reference=b"x")]


@pytest.fixture
def setup(monkeypatch):
    values: dict[str, Any] = {"TELEGRAM_USE_SYNTH_AVATAR": True}
    saved: dict[str, Any] = {}

    monkeypatch.setattr(
        tguser.config_registry,
        "get_value",
        lambda key, default=None, **_k: values.get(key, default),
    )

    async def _set(key: str, value: Any, **_k: Any) -> None:
        saved[key] = value
        values[key] = value

    monkeypatch.setattr(tguser.config_registry, "set_value", _set)
    iface = tguser.TelegramUserInterface()
    iface.client = _Client()
    return iface, values, saved


@pytest.mark.asyncio
async def test_avatar_is_not_applied_unless_opted_in(setup) -> None:
    iface, values, _saved = setup
    values["TELEGRAM_USE_SYNTH_AVATAR"] = False
    assert await iface.set_avatar(b"png", "image/png", "v1") is False
    assert iface.client.requests == []


@pytest.mark.asyncio
async def test_avatar_uploaded_once_per_version(setup) -> None:
    iface, _values, saved = setup
    assert await iface.set_avatar(b"png", "image/png", "v1") is True
    assert saved["TELEGRAM_AVATAR_APPLIED"] == "v1"
    assert iface.client.uploaded == [(b"png", "avatar.png")]
    # Restart / re-broadcast with the same version must not hit Telegram again.
    assert await iface.set_avatar(b"png", "image/png", "v1") is False
    assert len(iface.client.requests) == 1
    assert await iface.set_avatar(b"png2", "image/png", "v2") is True
    assert len(iface.client.requests) == 2


@pytest.mark.asyncio
async def test_removal_only_deletes_a_picture_we_set(setup, monkeypatch) -> None:
    iface, values, saved = setup
    monkeypatch.setattr("telethon.utils.get_input_photo", lambda photo: photo)
    assert await iface.set_avatar(None) is False  # never applied: touch nothing
    assert iface.client.requests == []
    values["TELEGRAM_AVATAR_APPLIED"] = "v1"
    assert await iface.set_avatar(None) is True
    assert len(iface.client.requests) == 1
    assert saved["TELEGRAM_AVATAR_APPLIED"] == ""


@pytest.mark.asyncio
async def test_flood_wait_is_reported_not_raised(setup, monkeypatch) -> None:
    from telethon import errors

    iface, _values, saved = setup
    iface.client.upload_file = AsyncMock(
        side_effect=errors.FloodWaitError(request=None, capture=30)
    )
    assert await iface.set_avatar(b"png", "image/png", "v1") is False
    assert "TELEGRAM_AVATAR_APPLIED" not in saved


@pytest.mark.asyncio
async def test_no_client_means_nothing_to_do(setup) -> None:
    iface, _values, _saved = setup
    iface.client = None
    assert await iface.set_avatar(b"png", "image/png", "v1") is False
