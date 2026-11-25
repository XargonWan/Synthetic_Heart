"""Characterisation tests for the contracts the extension platform builds on.

These pin **today's behaviour** (milestone M0 of ``docs/plugin_platform``). They
are not a statement that the behaviour is ideal: where the current behaviour is a
known limit (no escaping of ``/`` in a path level, silent overwrite on an
interface-name collision) the test says so, so a later change to it is a
deliberate decision and not an accident.

Covered here (the rest is already pinned elsewhere):

* ``core.interface_path_utils`` build / parse / legacy ids / validation. Resolution
  of a registered path is in ``tests/test_interface_path_utils.py``.
* ``core.core_initializer.register_interface`` contract. Dispatch is covered by
  ``tests/test_send_message_unified.py`` and ``tests/test_reply_routing_path.py``,
  Karada state by ``tests/test_karada_state_server.py``, statistics by
  ``tests/test_stats_registry.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import core.core_initializer as ci
from core.interface_path_utils import (
    build_interface_path,
    build_interface_path_from_legacy,
    extract_legacy_ids,
    is_valid_interface_path,
    parse_interface_path,
)
from core.interfaces_registry import get_interface_registry


class TestBuildInterfacePath:
    def test_joins_name_and_levels(self) -> None:
        assert build_interface_path("telegram_bot", "123", "2") == "telegram_bot/123/2"

    def test_stringifies_levels(self) -> None:
        assert build_interface_path("telegram_bot", 123, 2) == "telegram_bot/123/2"

    def test_drops_none_levels(self) -> None:
        assert build_interface_path("discord_bot", "g", None, "c") == "discord_bot/g/c"

    def test_no_levels_returns_bare_name(self) -> None:
        assert build_interface_path("synth_webui") == "synth_webui"
        assert build_interface_path("synth_webui", None) == "synth_webui"

    def test_legacy_builder_orders_chat_thread_then_sorted_extras(self) -> None:
        path = build_interface_path_from_legacy(
            "x", chat_id="c", thread_id="t", b="2", a="1"
        )
        assert path == "x/c/t/1/2"

    def test_legacy_builder_skips_missing_ids(self) -> None:
        assert build_interface_path_from_legacy("x", chat_id="c") == "x/c"
        assert build_interface_path_from_legacy("x") == "x"


class TestParseInterfacePath:
    def test_returns_tuple_not_dict(self) -> None:
        # docs/interface_path.rst says dict; the code returns a tuple.
        result = parse_interface_path("telegram_bot/123/2")
        assert isinstance(result, tuple)
        assert result == ("telegram_bot", ["123", "2"])

    def test_name_only(self) -> None:
        assert parse_interface_path("synth_webui") == ("synth_webui", [])

    def test_empty_input(self) -> None:
        assert parse_interface_path("") == ("", [])

    def test_does_not_validate(self) -> None:
        # parse never raises and keeps empty segments; validation is separate.
        assert parse_interface_path("a//b") == ("a", ["", "b"])

    def test_levels_with_slash_do_not_round_trip(self) -> None:
        # Known limit: there is no escaping. The number of levels and their
        # meaning belong to each interface, and a "/" inside a level splits it.
        built = build_interface_path("x", "a/b")
        assert parse_interface_path(built) == ("x", ["a", "b"])


class TestExtractLegacyIds:
    def test_chat_and_thread(self) -> None:
        assert extract_legacy_ids("telegram_bot/123/2") == {
            "interface": "telegram_bot",
            "chat_id": "123",
            "thread_id": "2",
        }

    def test_chat_only(self) -> None:
        ids = extract_legacy_ids("telegram_bot/123")
        assert ids["chat_id"] == "123"
        assert ids["thread_id"] is None

    def test_third_level_is_ignored(self) -> None:
        # discord_bot/<guild>/<channel>/<thread>: level 1 is "chat_id" and
        # level 2 is "thread_id" by convention, whatever the interface means.
        ids = extract_legacy_ids("discord_bot/guild/channel/post")
        assert ids["chat_id"] == "guild"
        assert ids["thread_id"] == "channel"

    def test_name_only(self) -> None:
        ids = extract_legacy_ids("synth_webui")
        assert ids["chat_id"] is None
        assert ids["thread_id"] is None


class TestIsValidInterfacePath:
    @pytest.mark.parametrize("path", ["a", "a/b", "a/b/c", "telegram_bot/-100/2"])
    def test_valid(self, path: str) -> None:
        assert is_valid_interface_path(path) is True

    @pytest.mark.parametrize("path", ["", "/a", "a/", "a//b", "a/ /b", "  "])
    def test_empty_segment_is_invalid(self, path: str) -> None:
        assert is_valid_interface_path(path) is False

    def test_non_string_is_invalid(self) -> None:
        assert is_valid_interface_path(None) is False  # type: ignore[arg-type]
        assert is_valid_interface_path(123) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# register_interface
# ---------------------------------------------------------------------------


class _Interface:
    """Smallest duck-typed interface that ``register_interface`` accepts."""

    def __init__(self, display_name: Any = "Test Interface", enabled: bool = True):
        self.display_name = display_name
        self.is_enabled = enabled

    def get_interface_id(self) -> str:
        return "baseline_test"

    async def send_message(self, payload: dict, original_message=None) -> bool:
        return True

    def get_supported_actions(self) -> dict:
        return {"message_baseline_test": {"required_fields": ["text"]}}


@pytest.fixture
def clean_registries() -> Iterator[None]:
    """Snapshot and restore the module-level registries touched by the tests."""
    interfaces = dict(ci.INTERFACE_REGISTRY)
    actions = dict(ci.ACTION_REGISTRY)
    capabilities = dict(get_interface_registry()._interface_capabilities)
    try:
        yield
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(interfaces)
        ci.ACTION_REGISTRY.clear()
        ci.ACTION_REGISTRY.update(actions)
        registry = get_interface_registry()._interface_capabilities
        registry.clear()
        registry.update(capabilities)


@pytest.mark.usefixtures("clean_registries")
class TestRegisterInterfaceContract:
    def test_registers_instance_and_actions(self) -> None:
        iface = _Interface()
        ci.register_interface("baseline_test", iface)
        assert ci.INTERFACE_REGISTRY["baseline_test"] is iface
        assert ci.ACTION_REGISTRY["message_baseline_test"] is iface

    def test_stores_derived_capabilities(self) -> None:
        ci.register_interface("baseline_test", _Interface())
        caps = get_interface_registry()._interface_capabilities["baseline_test"]
        assert isinstance(caps, frozenset)

    def test_missing_display_name_raises(self) -> None:
        class NoName:
            pass

        with pytest.raises(ValueError, match="display_name"):
            ci.register_interface("baseline_test", NoName())
        assert "baseline_test" not in ci.INTERFACE_REGISTRY

    @pytest.mark.parametrize("bad", ["", "   ", None, 123])
    def test_invalid_display_name_raises(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="display_name"):
            ci.register_interface("baseline_test", _Interface(display_name=bad))
        assert "baseline_test" not in ci.INTERFACE_REGISTRY

    def test_name_collision_overwrites_silently(self) -> None:
        # Known gap (docs/plugin_platform): no collision check. The second
        # registration silently replaces the first.
        first, second = _Interface(), _Interface()
        ci.register_interface("baseline_test", first)
        ci.register_interface("baseline_test", second)
        assert ci.INTERFACE_REGISTRY["baseline_test"] is second

    def test_disabled_interface_is_registered_without_actions(self) -> None:
        iface = _Interface(enabled=False)
        ci.register_interface("baseline_test", iface)
        assert ci.INTERFACE_REGISTRY["baseline_test"] is iface
        assert "message_baseline_test" not in ci.ACTION_REGISTRY

    def test_duplicate_action_is_overwritten_by_last_registration(self) -> None:
        first, second = _Interface(), _Interface()
        ci.register_interface("baseline_a", first)
        ci.register_interface("baseline_b", second)
        # Same action name from two interfaces: last one wins (only a log line).
        assert ci.ACTION_REGISTRY["message_baseline_test"] is second
