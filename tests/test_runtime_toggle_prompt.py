"""Switching an interface or plugin on and off on the fly changes the prompt.

Registered means present in the prompt: a component that is off contributes no
action to ``core_initializer.actions_block``, one that is on does, and neither
needs a restart.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator

import pytest

FAKE_IFACE = "fake_toggle_iface"
FAKE_PLUGIN = "fake_toggle_plugin"


class _FakeInterface:
    display_name = "Fake toggle interface"

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    @staticmethod
    def get_supported_actions() -> dict:
        return {"fake_toggle_ping": {"description": "ping the fake interface"}}

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class _FakePlugin:
    display_name = "Fake toggle plugin"

    @staticmethod
    def get_supported_actions() -> dict:
        return {"fake_toggle_plugin_act": {"description": "act"}}


class _LegacyMessagePlugin:
    """Claims the per-interface ``message_*`` action the way message_plugin does."""

    display_name = "Fake legacy message plugin"

    @staticmethod
    def get_supported_actions() -> dict:
        return {f"message_{FAKE_IFACE}": {}}


@pytest.fixture(autouse=True)
def toggle_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Capture the system events a toggle records (no database involved)."""
    from core import system_events

    recorded: list[tuple[str, str, str]] = []

    async def fake_record_event(
        kind: str,
        component: str | None = None,
        detail: str | None = None,
        **_: Any,
    ) -> None:
        recorded.append((kind, component or "", detail or ""))

    monkeypatch.setattr(system_events, "record_event", fake_record_event)
    return recorded


@pytest.fixture()
def core(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from core import core_initializer as ci
    from core.config_manager import config_registry

    core_initializer = ci.core_initializer
    saved_ifaces = dict(ci.INTERFACE_REGISTRY)
    saved_plugins = dict(ci.PLUGIN_REGISTRY)
    saved_active = list(core_initializer.active_interfaces)
    saved_components = dict(core_initializer.components)
    saved_loaded = list(core_initializer.loaded_plugins)
    flags: dict[str, bool] = {}

    async def fake_set_value(key: str, value: Any, *args: Any, **kwargs: Any) -> None:
        flags[key] = bool(value)

    def fake_get_value(key: str, default: Any = None, *args: Any, **kwargs: Any) -> Any:
        return flags.get(key, default)

    monkeypatch.setattr(config_registry, "set_value", fake_set_value)
    monkeypatch.setattr(config_registry, "get_value", fake_get_value)

    try:
        yield core_initializer
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(saved_ifaces)
        ci.PLUGIN_REGISTRY.clear()
        ci.PLUGIN_REGISTRY.update(saved_plugins)
        core_initializer.active_interfaces[:] = saved_active
        core_initializer.components.clear()
        core_initializer.components.update(saved_components)
        core_initializer.loaded_plugins[:] = saved_loaded
        for act in ("fake_toggle_ping", "fake_toggle_plugin_act"):
            ci.ACTION_REGISTRY.pop(act, None)


def _available(core: Any) -> dict:
    return core.actions_block.get("available_actions", {})


def test_interface_off_then_on_without_restart(core: Any) -> None:
    from core import core_initializer as ci

    iface = _FakeInterface()
    ci.INTERFACE_REGISTRY[FAKE_IFACE] = iface
    core.active_interfaces.append(FAKE_IFACE)

    asyncio.run(core._build_actions_block())
    assert "fake_toggle_ping" in _available(core)

    result = asyncio.run(core.disable_interface(FAKE_IFACE))
    assert result["ok"] is True
    assert iface.stopped == 1
    assert FAKE_IFACE not in core.active_interfaces
    assert "fake_toggle_ping" not in _available(core)
    assert "fake_toggle_ping" not in ci.ACTION_REGISTRY

    async def turn_on() -> dict:
        outcome = await core.enable_interface(FAKE_IFACE)
        await asyncio.sleep(0)  # let the start() task run
        return outcome

    result = asyncio.run(turn_on())
    assert result["ok"] is True
    assert FAKE_IFACE in core.active_interfaces
    assert "fake_toggle_ping" in _available(core)
    assert ci.ACTION_REGISTRY.get("fake_toggle_ping") is iface


def test_toggling_an_interface_is_noted_as_a_system_event(
    core: Any, toggle_events: list[tuple[str, str, str]]
) -> None:
    from core import core_initializer as ci

    ci.INTERFACE_REGISTRY[FAKE_IFACE] = _FakeInterface()
    core.active_interfaces.append(FAKE_IFACE)

    asyncio.run(core.disable_interface(FAKE_IFACE))
    assert toggle_events == [("component_disabled", FAKE_IFACE, "interface")]

    async def turn_on() -> None:
        await core.enable_interface(FAKE_IFACE)
        await asyncio.sleep(0)

    asyncio.run(turn_on())
    assert toggle_events[-1] == ("component_enabled", FAKE_IFACE, "interface")


def test_a_refused_toggle_records_nothing(
    core: Any, toggle_events: list[tuple[str, str, str]]
) -> None:
    """The core interface cannot be switched off; nothing changed, nothing noted."""
    result = asyncio.run(core.disable_interface("synth_webui"))
    assert result["ok"] is False
    assert toggle_events == []


def test_inactive_interface_is_not_in_the_prompt(core: Any) -> None:
    """An interface the loader never activated (no config, flag off) is withheld."""
    from core import core_initializer as ci

    ci.INTERFACE_REGISTRY[FAKE_IFACE] = _FakeInterface()
    assert FAKE_IFACE not in core.active_interfaces

    asyncio.run(core._build_actions_block())
    assert "fake_toggle_ping" not in _available(core)


def test_legacy_message_action_follows_its_interface(core: Any) -> None:
    from core import core_initializer as ci

    ci.INTERFACE_REGISTRY[FAKE_IFACE] = _FakeInterface()
    ci.PLUGIN_REGISTRY["fake_legacy_message"] = _LegacyMessagePlugin()
    legacy = f"message_{FAKE_IFACE}"

    asyncio.run(core._build_actions_block())
    assert legacy not in _available(core)

    core.active_interfaces.append(FAKE_IFACE)
    asyncio.run(core._build_actions_block())
    assert legacy in _available(core)

    asyncio.run(core.disable_interface(FAKE_IFACE))
    assert legacy not in _available(core)


def test_plugin_off_then_on_without_restart(core: Any, monkeypatch: Any) -> None:
    from core import core_initializer as ci

    plugin = _FakePlugin()
    ci.PLUGIN_REGISTRY[FAKE_PLUGIN] = plugin
    core.loaded_plugins.append(FAKE_PLUGIN)
    asyncio.run(core._build_actions_block())
    assert "fake_toggle_plugin_act" in _available(core)

    result = asyncio.run(core.disable_plugin(FAKE_PLUGIN))
    assert result["ok"] is True
    assert FAKE_PLUGIN not in ci.PLUGIN_REGISTRY
    assert "fake_toggle_plugin_act" not in _available(core)

    core.components[FAKE_PLUGIN].module_name = "plugins.fake_toggle_plugin"

    async def fake_instantiate(module_name: str, short_name: str) -> bool:
        ci.PLUGIN_REGISTRY[short_name] = plugin
        return True

    monkeypatch.setattr(core, "_instantiate_and_register", fake_instantiate)
    result = asyncio.run(core.enable_plugin(FAKE_PLUGIN))
    assert result["ok"] is True
    assert FAKE_PLUGIN in ci.PLUGIN_REGISTRY
    assert "fake_toggle_plugin_act" in _available(core)


def test_plugin_behind_a_package_shim_can_be_reenabled(
    core: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """Most plugins sit behind a shim package that rebinds ``sys.modules``.

    The shim has no ``__path__``, which used to make the re-enable import (and
    ``importlib.reload``) of the submodule fail with ``instantiation_failed``.
    """
    import importlib
    import sys

    from core import core_initializer as ci

    pkg = tmp_path / "shimplug_pkg"
    pkg.mkdir()
    (pkg / "shimplug.py").write_text(
        "class ShimPlugin:\n"
        "    display_name = 'Shim plugin'\n"
        "    @staticmethod\n"
        "    def get_supported_actions():\n"
        "        return {'fake_shim_act': {'description': 'x'}}\n"
        "PLUGIN_CLASS = ShimPlugin\n"
    )
    (pkg / "__init__.py").write_text(
        "import sys as _sys\n"
        "from shimplug_pkg import shimplug as _mod\n"
        "_sys.modules[__name__] = _mod\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    importlib.import_module("shimplug_pkg")
    module_name = "shimplug_pkg.shimplug"
    assert module_name in sys.modules
    assert not hasattr(sys.modules["shimplug_pkg"], "__path__")

    try:
        ok = asyncio.run(core._instantiate_and_register(module_name, "shimplug"))
        assert ok is True
        assert "shimplug" in ci.PLUGIN_REGISTRY
        asyncio.run(core._build_actions_block())
        assert "fake_shim_act" in _available(core)
    finally:
        ci.PLUGIN_REGISTRY.pop("shimplug", None)
        ci.ACTION_REGISTRY.pop("fake_shim_act", None)
        for name in ("shimplug_pkg", module_name):
            sys.modules.pop(name, None)
