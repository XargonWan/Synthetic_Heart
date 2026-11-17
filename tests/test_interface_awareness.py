"""The persona knows which interfaces are on, and the detail comes on demand.

* ``[Interfaces]`` (always, ids only) is recomputed every turn from
  ``core_initializer.active_interfaces``, so it follows a runtime toggle.
* ``recon_interface_reference`` adds the path formats only when the message
  talks about an interface, and only for interfaces that are on.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator

import pytest

from core import core_initializer as ci
from core import interface_aliases as ia
from core.interface_paths import describe_active_interfaces


class _Iface:
    def __init__(self, display_name: str, hint: str = "") -> None:
        self.display_name = display_name
        self._hint = hint

    def get_interface_path_hint(self) -> str:
        return self._hint


@pytest.fixture()
def interfaces() -> Iterator[None]:
    saved_registry = dict(ci.INTERFACE_REGISTRY)
    saved_active = list(ci.core_initializer.active_interfaces)
    ci.INTERFACE_REGISTRY.clear()
    ci.INTERFACE_REGISTRY.update(
        {
            "telegram": _Iface("Telegram (User)", "telegram/<chat_id>[/<thread_id>]"),
            "telegram_bot": _Iface("Telegram Bot", "telegram_bot/<chat_id>"),
            "discord_bot": _Iface("Discord Bot", "discord_bot/<guild_id>/<channel_id>"),
            "vessel": _Iface("Vessel"),
        }
    )
    ci.core_initializer.active_interfaces[:] = [
        "telegram",
        "telegram_bot",
        "vessel",
    ]
    try:
        yield
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(saved_registry)
        ci.core_initializer.active_interfaces[:] = saved_active


def test_only_active_interfaces_are_described_and_vessel_is_left_out(
    interfaces: None,
) -> None:
    ids = [item["id"] for item in describe_active_interfaces()]
    assert ids == ["telegram", "telegram_bot"]


def test_runtime_toggle_changes_the_description(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci.core_initializer.active_interfaces.remove("telegram_bot")
    ci.core_initializer.active_interfaces.append("discord_bot")
    ids = [item["id"] for item in describe_active_interfaces()]
    assert ids == ["telegram", "discord_bot"]


def test_the_always_present_line_lists_ids_only(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.action_parser import _add_runtime_state_injections

    injections = asyncio.run(_add_runtime_state_injections({}))
    line = injections["active_interfaces"]
    assert "telegram, telegram_bot" in line
    assert "vessel" not in line
    assert "discord_bot" not in line
    assert "<chat_id>" not in line  # path formats are the on-demand part


def test_no_active_interface_adds_no_line(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.action_parser import _add_runtime_state_injections

    ci.core_initializer.active_interfaces[:] = []
    injections = asyncio.run(_add_runtime_state_injections({}))
    assert "active_interfaces" not in injections


# -- recon plugin -----------------------------------------------------------


def _recon(
    monkeypatch: pytest.MonkeyPatch,
    data: Any,
    recent: list[dict[str, Any]] | None = None,
) -> list[dict]:
    from plugins.recon import recon_interface_reference as mod

    async def fake_recent(limit: int = 10) -> list[dict[str, Any]]:
        return recent or []

    monkeypatch.setattr(mod, "get_recent_interface_paths", fake_recent)
    plugin = mod.ReconInterfaceReferencePlugin()
    return asyncio.run(plugin.parse_recon_response(data))


def test_no_mention_injects_nothing(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _recon(monkeypatch, {"interface_reference": []}) == []


def test_mentioning_telegram_lists_both_telegram_interfaces(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    recent = [
        {"interface_path": "telegram/31321637", "display": "Jay"},
        {"interface_path": "telegram_bot/31321637", "display": "Jay"},
        {"interface_path": "discord_bot/1/2", "display": "x"},
    ]
    out = _recon(monkeypatch, {"interface_reference": ["Telegram"]}, recent)
    assert len(out) == 1
    content = out[0]["content"]
    assert out[0]["type"] == "snippet"
    assert "telegram/<chat_id>[/<thread_id>]" in content
    assert "telegram_bot/<chat_id>" in content
    assert "known: telegram/31321637 (Jay)" in content
    assert "discord" not in content


def test_the_bot_name_selects_only_the_bot(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _recon(monkeypatch, {"interface_reference": ["Telegram Bot"]})[0][
        "content"
    ]
    assert "telegram_bot/<chat_id>" in content
    assert "telegram/<chat_id>[/<thread_id>]" not in content


def test_an_interface_that_is_off_is_not_listed(
    interfaces: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # discord_bot is registered but not active.
    assert _recon(monkeypatch, {"interface_reference": ["discord"]}) == []


JAY = "31321637"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- known paths in the recon snippet are shown by alias ----------------------


def test_the_interface_recon_snippet_lists_known_paths_by_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import core_initializer as ci
    from plugins.recon import recon_interface_reference as mod

    class _Iface:
        display_name = "Telegram (User)"

        @staticmethod
        def get_interface_path_hint() -> str:
            return "telegram/<@username or chat_id>[/<thread_id>]"

    saved_registry = dict(ci.INTERFACE_REGISTRY)
    saved_active = list(ci.core_initializer.active_interfaces)
    ci.INTERFACE_REGISTRY["telegram"] = _Iface()
    ci.core_initializer.active_interfaces[:] = ["telegram"]

    async def recent(limit: int = 10) -> list[dict[str, Any]]:
        return [{"interface_path": f"telegram/{JAY}", "display": "Jay"}]

    monkeypatch.setattr(mod, "get_recent_interface_paths", recent)
    try:
        run(ia.remember_alias("telegram", "jaycheshire", JAY))
        out = run(
            mod.ReconInterfaceReferencePlugin().parse_recon_response(
                {"interface_reference": ["telegram"]}
            )
        )
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(saved_registry)
        ci.core_initializer.active_interfaces[:] = saved_active

    content = out[0]["content"]
    assert "known: telegram/@jaycheshire (Jay)" in content
    assert f"telegram/{JAY}" not in content
