"""Skin endpoints must never resolve outside the skins/ directory."""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


@pytest.mark.parametrize(
    "name",
    ["..", ".", "../x", "a/../..", "", ".hidden", "a/b", "a\\b", "x\x00y"],
)
def test_skin_path_rejects_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(HTTPException) as exc:
        SynthWebUIInterface._skin_path(tmp_path, name)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("name", ["Rei", "My Skin", "skin_v2.1", "Rèi"])
def test_skin_path_accepts_normal_names(tmp_path: Path, name: str) -> None:
    assert SynthWebUIInterface._skin_path(tmp_path, name) == tmp_path / name


def test_skin_path_rejects_symlink_escape(tmp_path: Path) -> None:
    skins = tmp_path / "skins"
    skins.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (skins / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException):
        SynthWebUIInterface._skin_path(skins, "link")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(SynthWebUIInterface(autostart=False).app)


def test_delete_dotdot_skin_is_rejected(client: TestClient) -> None:
    assert client.delete("/api/skins/%2e%2e").status_code == 400


def test_upload_zip_with_traversal_entry_is_rejected(client: TestClient) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil/persona.json", "{}")
    resp = client.post(
        "/api/skins/upload",
        files={"file": ("pack.zip", buf.getvalue(), "application/zip")},
    )
    assert resp.status_code == 400
