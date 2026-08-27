"""Unit tests for the agpeer plugin (no live agpeer core required).

Covers: the destination sandbox guard, the destructive-verb config gate, the
agent-lane-only routing invariant (every action declares external effects),
action → REST endpoint mapping (via a mocked ``agpeer_request``), and the
thin HTTP helper itself (auth header injection, error mapping, fail-closed
unreachable core) against a fake aiohttp session.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from plugins.agpeer.agpeer import (
    AgpeerPlugin,
    agpeer_request,
    resolve_agpeer_destination,
)

# The package __init__ shim rebinds plugins.agpeer to the agpeer.py module,
# so the plain ``import plugins.agpeer.agpeer as mod`` form is unreliable —
# resolve the real submodule through importlib instead.
agpeer_module = importlib.import_module("plugins.agpeer.agpeer")


# ---------------------------------------------------------------------------
# Config fakes
# ---------------------------------------------------------------------------


def _install_config(monkeypatch: Any, overrides: dict[str, Any]) -> None:
    """Point config_registry.get_value at a dict of test overrides."""
    from core.config_manager import config_registry

    original = config_registry.get_value

    def fake_get_value(key: str, default: Any = None, *a: Any, **kw: Any) -> Any:
        if key in overrides:
            return overrides[key]
        return original(key, default, *a, **kw)

    monkeypatch.setattr(config_registry, "get_value", fake_get_value)


# ---------------------------------------------------------------------------
# Destination sandbox
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    return tmp_path / "media"


def test_destination_empty_uses_agpeer_default(monkeypatch):
    _install_config(monkeypatch, {})
    resolved, error = resolve_agpeer_destination("")
    assert resolved is None and error is None
    resolved, error = resolve_agpeer_destination(None)
    assert resolved is None and error is None


def test_destination_relative_resolves_inside_root(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    resolved, error = resolve_agpeer_destination("some artist/new album")
    assert error is None
    assert resolved is not None
    assert Path(resolved).is_relative_to(sandbox_root.resolve())


def test_destination_traversal_rejected(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    resolved, error = resolve_agpeer_destination("../escape")
    assert resolved is None
    assert error is not None and "outside" in error


def test_destination_absolute_inside_root_allowed(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    resolved, error = resolve_agpeer_destination(str(sandbox_root / "flac"))
    assert error is None
    assert resolved is not None


def test_destination_absolute_outside_root_rejected(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    resolved, error = resolve_agpeer_destination("C:\\Windows\\System32")
    assert resolved is None
    assert error is not None and "outside" in error


# ---------------------------------------------------------------------------
# Routing invariant + schema
# ---------------------------------------------------------------------------


def test_every_action_declares_external_effects():
    """Agent Lane only, structurally: without external_effects a call could
    ride the Fast Lane — the design (and the user's requirement) forbids it."""
    actions = AgpeerPlugin.get_supported_actions(AgpeerPlugin())
    assert actions, "plugin exposes no actions"
    for name, schema in actions.items():
        effects = schema.get("external_effects")
        assert isinstance(effects, list) and effects, (
            f"{name} must declare external_effects (agent-lane routing)"
        )
        assert "network" in effects, f"{name} must declare the network effect"


def test_action_required_fields():
    actions = AgpeerPlugin.get_supported_actions(AgpeerPlugin())
    assert actions["agpeer_search"]["required_fields"] == ["query"]
    assert set(actions["agpeer_download"]["required_fields"]) == {
        "search_id",
        "result_id",
    }
    assert actions["agpeer_add_magnet"]["required_fields"] == ["source"]
    assert actions["agpeer_delete_transfer"]["required_fields"] == ["id"]
    assert actions["agpeer_stop_search"]["required_fields"] == ["id"]
    assert actions["agpeer_transfer_files"]["required_fields"] == ["id"]
    assert actions["agpeer_pause_transfer"]["required_fields"] == ["id"]
    assert actions["agpeer_resume_transfer"]["required_fields"] == ["id"]
    assert actions["agpeer_setting_set"]["required_fields"] == ["key", "value"]
    assert actions["agpeer_setting_delete"]["required_fields"] == ["key"]


def test_settings_mutation_actions_are_high_security():
    """Changing the P2P core's runtime settings is privileged: the declared
    level must be 'high' so the autonomy ceiling gates it."""
    actions = AgpeerPlugin.get_supported_actions(AgpeerPlugin())
    assert actions["agpeer_setting_set"]["security_level"] == "high"
    assert actions["agpeer_setting_delete"]["security_level"] == "high"


# ---------------------------------------------------------------------------
# Action dispatch (mocked agpeer_request)
# ---------------------------------------------------------------------------


class _RecordingStub:
    """Replaces plugins.agpeer.agpeer.agpeer_request; records every call."""

    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, **kw})
        return self.response


def _install_stub(monkeypatch: Any, response: dict[str, Any]) -> _RecordingStub:
    stub = _RecordingStub(response)
    monkeypatch.setattr(agpeer_module, "agpeer_request", stub)
    return stub


@pytest.mark.asyncio
async def test_status_maps_to_get_status(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"version": "0.1.0"}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action({"type": "agpeer_status", "payload": {}})
    assert result["status"] == "ok"
    assert result["data"] == {"version": "0.1.0"}
    assert stub.calls[0]["method"] == "GET"
    assert stub.calls[0]["path"] == "/api/v1/status"


@pytest.mark.asyncio
async def test_search_builds_request_body(monkeypatch):
    stub = _install_stub(
        monkeypatch, {"ok": True, "data": {"search_id": "s-123"}}
    )
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_search",
            "payload": {"query": "radiohead ok computer", "extension": "flac"},
        }
    )
    assert result["status"] == "ok"
    assert result["search_id"] == "s-123"
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/searches"
    assert call["json_body"] == {
        "backend": "soulseek",
        "query": "radiohead ok computer",
        "extension": "flac",
    }
    assert "agpeer_search_results" in result["note"]


@pytest.mark.asyncio
async def test_search_requires_query(monkeypatch):
    _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_search", "payload": {"extension": "flac"}}
    )
    assert result["status"] == "error"
    assert "query" in result["message"]


@pytest.mark.asyncio
async def test_search_results_unwraps_bare_array(monkeypatch):
    stub = _install_stub(
        monkeypatch, {"ok": True, "data": [{"result_id": "r1"}, {"result_id": "r2"}]}
    )
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_search_results", "payload": {"search_id": "s-123"}}
    )
    assert result["status"] == "ok"
    # agpeer returns a BARE array for search results.
    assert stub.calls[0]["path"] == "/api/v1/searches/s-123/results"
    assert result["count"] == 2
    assert result["results"][0]["result_id"] == "r1"


@pytest.mark.asyncio
async def test_download_resolves_relative_destination(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"transfer_id": "t-1"}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_download",
            "payload": {
                "search_id": "s-1",
                "result_id": "r-1",
                "destination": "unsorted",
            },
        }
    )
    assert result["status"] == "ok"
    assert result["transfer_id"] == "t-1"
    call = stub.calls[0]
    assert call["path"].endswith("/searches/s-1/results/r-1/download")
    assert call["json_body"]["destination"] == str(
        (sandbox_root / "unsorted").resolve()
    )


@pytest.mark.asyncio
async def test_download_rejects_escape_destination(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"transfer_id": "t"}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_download",
            "payload": {
                "search_id": "s-1",
                "result_id": "r-1",
                "destination": "../../etc",
            },
        }
    )
    assert result["status"] == "error"
    assert "outside" in result["message"]
    assert stub.calls == []  # fail closed before any HTTP


@pytest.mark.asyncio
async def test_delete_blocked_without_destructive_flag(monkeypatch):
    _install_config(monkeypatch, {"AGPEER_ALLOW_DESTRUCTIVE": False})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_delete_transfer", "payload": {"id": "t-1"}}
    )
    assert result["status"] == "error"
    assert "AGPEER_ALLOW_DESTRUCTIVE" in result["message"]
    assert stub.calls == []  # refused before any HTTP


@pytest.mark.asyncio
async def test_delete_allowed_with_flag_and_delete_data(monkeypatch):
    _install_config(monkeypatch, {"AGPEER_ALLOW_DESTRUCTIVE": True})
    stub = _install_stub(monkeypatch, {"ok": True, "data": None})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_delete_transfer", "payload": {"id": "t-1", "delete_data": True}}
    )
    assert result["status"] == "ok"
    call = stub.calls[0]
    assert call["method"] == "DELETE"
    assert call["path"] == "/api/v1/transfers/t-1"
    assert call["params"] == {"delete_data": "true"}


@pytest.mark.asyncio
async def test_cancel_blocked_without_flag(monkeypatch):
    _install_config(monkeypatch, {"AGPEER_ALLOW_DESTRUCTIVE": False})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_cancel_transfer", "payload": {"id": "t-1"}}
    )
    assert result["status"] == "error"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_transfer_single_vs_list(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": []})
    plugin = AgpeerPlugin()
    await plugin.execute_action({"type": "agpeer_transfer", "payload": {}})
    assert stub.calls[0]["path"] == "/api/v1/transfers"
    await plugin.execute_action({"type": "agpeer_transfer", "payload": {"id": "t-9"}})
    assert stub.calls[1]["path"] == "/api/v1/transfers/t-9"


@pytest.mark.asyncio
async def test_add_magnet_builds_torrent_body(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"transfer_id": "t-2"}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_add_magnet",
            "payload": {
                "source": "magnet:?xt=urn:btih:abc",
                "destination": "torrents",
            },
        }
    )
    assert result["status"] == "ok"
    call = stub.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/transfers"
    assert call["json_body"]["backend"] == "torrent"
    assert call["json_body"]["source"] == "magnet:?xt=urn:btih:abc"
    assert call["json_body"]["destination"] == str(
        (sandbox_root / "torrents").resolve()
    )


@pytest.mark.asyncio
async def test_action_error_passthrough(monkeypatch):
    _install_stub(
        monkeypatch,
        {"ok": False, "error": "agpeer responded with 404: {\"code\":\"TransferNotFound\"}"},
    )
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_transfer", "payload": {"id": "nope"}}
    )
    assert result["status"] == "error"
    assert "404" in result["message"]


@pytest.mark.asyncio
async def test_add_magnet_file_selection_and_torrent_url(monkeypatch, sandbox_root):
    _install_config(monkeypatch, {"AGPEER_DOWNLOAD_ROOT": str(sandbox_root)})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"transfer_id": "t-3"}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_add_magnet",
            "payload": {
                "source": "https://example.org/some.torrent",
                "destination": "torrents",
                "file_selection": [
                    {"index": 0, "selected": True},
                    {"index": "2", "selected": "false"},
                    {"no_index": True},  # dropped: no index
                    "garbage",  # dropped: not a dict
                ],
            },
        }
    )
    assert result["status"] == "ok"
    body = stub.calls[0]["json_body"]
    # Indices are normalised to strings; selected coerced to bool; invalid
    # entries dropped.
    assert body["file_selection"] == [
        {"index": "0", "selected": True},
        {"index": "2", "selected": False},
    ]


@pytest.mark.asyncio
async def test_searches_list(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": []})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action({"type": "agpeer_searches", "payload": {}})
    assert result["status"] == "ok"
    assert stub.calls[0]["path"] == "/api/v1/searches"


@pytest.mark.asyncio
async def test_stop_search(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_stop_search", "payload": {"id": "s-7"}}
    )
    assert result["status"] == "ok"
    assert stub.calls[0]["method"] == "POST"
    assert stub.calls[0]["path"] == "/api/v1/searches/s-7/stop"


@pytest.mark.asyncio
async def test_transfer_files(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": []})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_transfer_files", "payload": {"id": "t-4"}}
    )
    assert result["status"] == "ok"
    assert stub.calls[0]["path"] == "/api/v1/transfers/t-4/files"


@pytest.mark.asyncio
async def test_pause_resume_transfer(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    await plugin.execute_action(
        {"type": "agpeer_pause_transfer", "payload": {"id": "t-5"}}
    )
    await plugin.execute_action(
        {"type": "agpeer_resume_transfer", "payload": {"id": "t-5"}}
    )
    assert stub.calls[0]["path"] == "/api/v1/transfers/t-5/pause"
    assert stub.calls[1]["path"] == "/api/v1/transfers/t-5/resume"


@pytest.mark.asyncio
async def test_pause_transfer_not_gated_by_destructive_flag(monkeypatch):
    """Pause is reversible — it must NOT sit behind the destructive gate."""
    _install_config(monkeypatch, {"AGPEER_ALLOW_DESTRUCTIVE": False})
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_pause_transfer", "payload": {"id": "t-5"}}
    )
    assert result["status"] == "ok"
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_library(monkeypatch):
    stub = _install_stub(
        monkeypatch,
        {"ok": True, "data": [{"path": "TV/ep.mkv", "is_dir": False}]},
    )
    plugin = AgpeerPlugin()
    result = await plugin.execute_action({"type": "agpeer_library", "payload": {}})
    assert result["status"] == "ok"
    assert stub.calls[0]["path"] == "/api/v1/library"


@pytest.mark.asyncio
async def test_postprocess_list_vs_one(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": []})
    plugin = AgpeerPlugin()
    await plugin.execute_action({"type": "agpeer_postprocess", "payload": {}})
    assert stub.calls[0]["path"] == "/api/v1/postprocess"
    await plugin.execute_action({"type": "agpeer_postprocess", "payload": {"id": "j-1"}})
    assert stub.calls[1]["path"] == "/api/v1/postprocess/j-1"


@pytest.mark.asyncio
async def test_settings_all_vs_one_key(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"hook_search": {}}})
    plugin = AgpeerPlugin()
    await plugin.execute_action({"type": "agpeer_settings", "payload": {}})
    assert stub.calls[0]["path"] == "/api/v1/settings"
    await plugin.execute_action(
        {"type": "agpeer_settings", "payload": {"key": "hook_search.enabled"}}
    )
    # Dotted keys ride in one path segment.
    assert stub.calls[1]["path"] == "/api/v1/settings/hook_search.enabled"


@pytest.mark.asyncio
async def test_setting_set_puts_raw_json_value(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {"hook_search.enabled": True}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_setting_set",
            "payload": {"key": "hook_search.enabled", "value": True},
        }
    )
    assert result["status"] == "ok"
    call = stub.calls[0]
    assert call["method"] == "PUT"
    assert call["path"] == "/api/v1/settings/hook_search.enabled"
    # The body is the RAW JSON value, not a wrapper object.
    assert call["json_body"] is True


@pytest.mark.asyncio
async def test_setting_set_rejects_path_smuggling_keys(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {
            "type": "agpeer_setting_set",
            "payload": {"key": "hook_search.enabled/extra", "value": True},
        }
    )
    assert result["status"] == "error"
    assert stub.calls == []


@pytest.mark.asyncio
async def test_setting_set_requires_value(monkeypatch):
    _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_setting_set", "payload": {"key": "hook_search.enabled"}}
    )
    assert result["status"] == "error"
    assert "value" in result["message"]


@pytest.mark.asyncio
async def test_setting_delete(monkeypatch):
    stub = _install_stub(monkeypatch, {"ok": True, "data": {}})
    plugin = AgpeerPlugin()
    result = await plugin.execute_action(
        {"type": "agpeer_setting_delete", "payload": {"key": "hook_search.enabled"}}
    )
    assert result["status"] == "ok"
    assert stub.calls[0]["method"] == "DELETE"
    assert stub.calls[0]["path"] == "/api/v1/settings/hook_search.enabled"


# ---------------------------------------------------------------------------
# HTTP helper (fake aiohttp session)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakeSession:
    # Class-level recording so tests can inspect the outgoing request.
    last_request: dict[str, Any] | None = None
    next_response: _FakeResponse | None = None
    raise_on_request: Exception | None = None

    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    def request(self, method: str, url: str, **kw: Any) -> _FakeResponse:
        _FakeSession.last_request = {"method": method, "url": url, **kw}
        if _FakeSession.raise_on_request is not None:
            raise _FakeSession.raise_on_request
        return _FakeSession.next_response or _FakeResponse(200, "{}")

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False


@pytest.fixture
def fake_aiohttp(monkeypatch: Any):
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    _FakeSession.last_request = None
    _FakeSession.next_response = None
    _FakeSession.raise_on_request = None
    yield _FakeSession
    _FakeSession.last_request = None
    _FakeSession.next_response = None
    _FakeSession.raise_on_request = None


@pytest.mark.asyncio
async def test_request_injects_bearer_token(monkeypatch, fake_aiohttp):
    _install_config(
        monkeypatch, {"AGPEER_API_BASE": "http://127.0.0.1:41000", "AGPEER_TOKEN": "sekrit"}
    )
    fake_aiohttp.next_response = _FakeResponse(200, json.dumps({"db": "ok"}))
    outcome = await agpeer_request("GET", "/api/v1/status")
    assert outcome["ok"] is True
    assert outcome["data"] == {"db": "ok"}
    headers = fake_aiohttp.last_request["headers"]
    assert headers["Authorization"] == "Bearer sekrit"
    assert fake_aiohttp.last_request["url"] == "http://127.0.0.1:41000/api/v1/status"


@pytest.mark.asyncio
async def test_request_without_token_sends_no_auth_header(monkeypatch, fake_aiohttp):
    _install_config(monkeypatch, {"AGPEER_API_BASE": "http://127.0.0.1:41000", "AGPEER_TOKEN": ""})
    fake_aiohttp.next_response = _FakeResponse(200, "{}")
    await agpeer_request("GET", "/api/v1/status")
    assert "headers" not in fake_aiohttp.last_request or not fake_aiohttp.last_request["headers"]


@pytest.mark.asyncio
async def test_request_http_error_surfaces_status_and_body(monkeypatch, fake_aiohttp):
    _install_config(monkeypatch, {"AGPEER_API_BASE": "http://127.0.0.1:41000"})
    fake_aiohttp.next_response = _FakeResponse(
        404, json.dumps({"code": "TransferNotFound"})
    )
    outcome = await agpeer_request("GET", "/api/v1/transfers/nope")
    assert outcome["ok"] is False
    assert "404" in outcome["error"]
    assert "TransferNotFound" in outcome["error"]


@pytest.mark.asyncio
async def test_request_unreachable_core_fails_closed(monkeypatch, fake_aiohttp):
    _install_config(
        monkeypatch,
        {"AGPEER_API_BASE": "http://127.0.0.1:41000", "AGPEER_TOKEN": "sekrit"},
    )
    fake_aiohttp.raise_on_request = ConnectionError("connection refused")
    outcome = await agpeer_request("GET", "/api/v1/status")
    assert outcome["ok"] is False
    assert "unreachable" in outcome["error"]
    assert "is it running" in outcome["error"]
    # The bearer token never leaks into error text.
    assert "sekrit" not in outcome["error"]


def test_poll_verbs_declare_repeatable():
    """Read/poll verbs must opt into re-execution so the agent loop's
    identical-call dedup never serves a cached stale snapshot to a polling
    model (live incident: a stuck-queued transfer polled ~15x against the
    first snapshot because every poll hit the dedup cache)."""
    actions = AgpeerPlugin.get_supported_actions(AgpeerPlugin())
    poll_verbs = {
        "agpeer_status",
        "agpeer_searches",
        "agpeer_search_results",
        "agpeer_transfer",
        "agpeer_transfer_files",
        "agpeer_postprocess",
        "agpeer_library",
        "agpeer_settings",
    }
    for verb in poll_verbs:
        assert actions[verb].get("repeatable") is True, verb
    # Mutating verbs must NOT be repeatable.
    for verb in (
        "agpeer_download",
        "agpeer_add_magnet",
        "agpeer_cancel_transfer",
        "agpeer_delete_transfer",
        "agpeer_setting_set",
    ):
        assert not actions[verb].get("repeatable"), verb
