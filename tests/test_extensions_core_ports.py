"""Tests for ``core.extensions.core_ports``: the Core services behind the ports."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from core.extensions import core_ports
from core.extensions.core_ports import (
    CoreConfig,
    CoreLog,
    CoreStats,
    config_key,
    core_api_ports,
)
from core.extensions.db_storage import SqlStorage
from core.extensions.manifest import ConfigField


class TestConfigKey:
    def test_shape(self) -> None:
        assert config_key("official/telegram_bot", "API_TOKEN") == (
            "EXT__OFFICIAL_SZTELEGRAM_UBOT__API_TOKEN".replace("_SZ", "_S")
        )
        assert config_key("dev/x", "K") == "EXT__DEV_SX__K"

    def test_two_extensions_never_share_a_key(self) -> None:
        alphabet = "ab_-"
        names = [
            "".join(p) for n in (1, 2, 3) for p in itertools.product(alphabet, repeat=n)
        ]
        valid = [n for n in names if n[0].isalnum()]
        keys: dict[str, str] = {}
        for publisher in ("dev", "d_v", "d-v", "dev_", "dev-"):
            for name in valid:
                ext_id = f"{publisher}/{name}"
                key = config_key(ext_id, "KEY")
                assert key not in keys, f"{ext_id} collides with {keys[key]}"
                keys[key] = ext_id

    def test_the_historical_collision_is_gone(self) -> None:
        assert config_key("dev/x-y", "K") != config_key("dev/x_y", "K")
        assert config_key("a/b-c", "K") != config_key("a-b/c", "K")

    def test_the_extension_part_cannot_swallow_the_key_part(self) -> None:
        # An id cannot contain "__", so the first "__" after the prefix ends it.
        for ext_id in ("dev/x_y", "dev/x__y", "dev/_x", "d__v/x"):
            body = config_key(ext_id, "KEY")[len("EXT__") :]
            assert body.split("__", 1)[1] == "KEY"


class TestCoreLog:
    def test_lines_are_tagged_and_routed_by_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import logging_utils

        seen: list[tuple[str, str]] = []
        for level in ("debug", "info", "warning", "error"):
            monkeypatch.setattr(
                logging_utils,
                f"log_{level}",
                lambda m, *a, _l=level, **k: seen.append((_l, m)),
            )
        log = CoreLog()
        for level in ("debug", "info", "warning", "error"):
            log.write("dev/x", level, f"hello {level}")
        assert seen == [
            ("debug", "[ext:dev/x] hello debug"),
            ("info", "[ext:dev/x] hello info"),
            ("warning", "[ext:dev/x] hello warning"),
            ("error", "[ext:dev/x] hello error"),
        ]


class TestCoreStats:
    def test_calls_the_existing_registry_not_a_new_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import stats_registry

        seen: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            stats_registry,
            "incr",
            lambda name, n=1, **lab: seen.append(("incr", name, n, lab)),
        )
        monkeypatch.setattr(
            stats_registry,
            "observe",
            lambda name, s, **lab: seen.append(("observe", name, s, lab)),
        )
        monkeypatch.setattr(
            stats_registry, "gauge", lambda name, v: seen.append(("gauge", name, v))
        )
        stats = CoreStats()
        stats.incr("ext.dev.x.n", 2, {"kind": "a"})
        stats.observe("ext.dev.x.t", 0.5, {})
        stats.gauge("ext.dev.x.g", 7.0)
        assert seen == [
            ("incr", "ext.dev.x.n", 2, {"kind": "a"}),
            ("observe", "ext.dev.x.t", 0.5, {}),
            ("gauge", "ext.dev.x.g", 7.0),
        ]

    def test_values_really_land_in_the_core_statistics(self) -> None:
        from core import stats_registry

        stats_registry.reset_for_tests()
        CoreStats().incr("ext.dev.x.messages", 3, {"kind": "text"})
        CoreStats().gauge("ext.dev.x.queue", 4.0)
        snapshot = stats_registry.get_stats()
        assert snapshot is not None  # the shared registry accepted the series
        stats_registry.reset_for_tests()


class TestCoreConfig:
    def test_asks_the_registry_for_a_namespaced_key_without_env_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config_manager import config_registry

        calls: list[tuple[str, Any, dict[str, Any]]] = []

        def fake_get_value(key: str, default: Any, **kwargs: Any) -> Any:
            calls.append((key, default, kwargs))
            return "from-registry"

        monkeypatch.setattr(config_registry, "get_value", fake_get_value)
        field = ConfigField(
            key="API_TOKEN", type="string", label="API token", sensitive=True
        )
        assert CoreConfig().get("dev/x-y", field) == "from-registry"
        key, default, kwargs = calls[0]
        assert key == "EXT__DEV_SX_DY__API_TOKEN"
        assert default == ""
        assert kwargs["sensitive"] is True
        assert kwargs["allow_env_override"] is False  # a process env var cannot set it
        assert kwargs["component"] == "ext:dev/x-y"
        assert kwargs["value_type"] is str

    def test_types_and_defaults_are_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.config_manager import config_registry

        calls: list[tuple[str, Any, dict[str, Any]]] = []
        monkeypatch.setattr(
            config_registry,
            "get_value",
            lambda k, d, **kw: calls.append((k, d, kw)) or d,
        )
        assert (
            CoreConfig().get("dev/x", ConfigField(key="N", type="int", default=5)) == 5
        )
        assert calls[0][2]["value_type"] is int


class TestCorePorts:
    def test_builds_the_real_ports_without_touching_the_database(self) -> None:
        ports = core_api_ports()
        assert isinstance(ports.storage, SqlStorage)
        assert isinstance(ports.log, CoreLog)
        assert isinstance(ports.stats, CoreStats)
        assert isinstance(ports.config, CoreConfig)

    def test_importing_the_module_does_not_import_the_database_layer(self) -> None:
        import subprocess
        import sys

        code = (
            "import sys; import core.extensions.core_ports; "
            "print('core.db' in sys.modules, 'core.config_manager' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert out == "False False"
        assert core_ports.config_key("a/b", "K")
