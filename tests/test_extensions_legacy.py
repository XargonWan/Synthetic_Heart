"""Carrying the old plugins' settings over to the extensions that replaced them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from core.extensions.core_ports import config_key
from core.extensions.legacy import (
    COMPONENTS,
    Component,
    decide,
    migrate,
    old_keys,
)
from core.extensions.legacy_port import CoreLegacyPort
from core.extensions.repo.installer import InstallError, Installer

ROOT = Path(__file__).resolve().parent.parent


def component(ext: str) -> Component:
    return next(c for c in COMPONENTS if c.extension_id == ext)


class TestDecisions:
    def test_a_configured_interface_is_migrated_with_its_settings(self) -> None:
        d = decide(
            component("official/fluxer"),
            {"FLUXER_TOKEN": "tok", "FLUXER_API_VERSION": "2", "OTHER": "x"},
        )
        assert d.migrate and not d.switch_off and not d.install
        assert d.settings == {"FLUXER_TOKEN": "tok", "FLUXER_API_VERSION": "2"}

    @pytest.mark.parametrize("token", [None, "", "   "])
    def test_missing_mandatory_settings_mean_it_never_worked(
        self, token: str | None
    ) -> None:
        rows = {} if token is None else {"BOTFATHER_TOKEN": token}
        d = decide(component("official/telegram_bot"), rows)
        assert not d.migrate and "never worked" in d.reason and d.settings == {}

    @pytest.mark.parametrize("flag", ["false", "False", "0", "off", "no"])
    def test_a_switched_off_default_is_kept_but_off(self, flag: str) -> None:
        d = decide(
            component("official/telegram_bot"),
            {"BOTFATHER_TOKEN": "t", "INTERFACE_ENABLED__telegram_bot": flag},
        )
        assert d.migrate and d.switch_off and d.settings == {"BOTFATHER_TOKEN": "t"}

    def test_a_switched_off_package_that_is_not_a_default_is_not_fetched(self) -> None:
        d = decide(
            component("official/telegram"),
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "h",
                "TELEGRAM_SESSION": "s",
                "INTERFACE_ENABLED__telegram": "false",
            },
        )
        assert not d.migrate and not d.install and "switched off" in d.reason

    def test_a_userbot_in_use_is_fetched_and_its_session_follows(self) -> None:
        d = decide(
            component("official/telegram"),
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "h",
                "TELEGRAM_PHONE": "+39",
                "TELEGRAM_SESSION": "STRING",
            },
        )
        assert d.migrate and d.install
        assert d.session == ("session", "STRING")
        assert d.settings["TELEGRAM_PHONE"] == "+39"

    @pytest.mark.parametrize(
        "missing", ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"]
    )
    def test_a_userbot_without_credentials_or_login_never_worked(
        self, missing: str
    ) -> None:
        rows = {
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "h",
            "TELEGRAM_SESSION": "s",
        }
        del rows[missing]
        d = decide(component("official/telegram"), rows)
        assert not d.migrate and not d.install

    RADIO = {
        "AZURACAST_BASE_URL": "https://radio.example.com",
        "AZURACAST_API_KEY": "key",
        "AZURACAST_STATION_ID": "main",
        "RADIO_HOST_LANGUAGE": "Italian",
        "RADIO_HOST_GAIN_DB": "6.5",
    }

    def test_a_radio_host_that_was_switched_on_is_fetched_with_its_settings(
        self,
    ) -> None:
        d = decide(
            component("official/radio_host"),
            {**self.RADIO, "RADIO_HOST_ENABLED": "true"},
        )
        assert d.migrate and d.install and not d.switch_off
        assert d.settings["AZURACAST_BASE_URL"] == "https://radio.example.com"
        assert d.settings["RADIO_HOST_LANGUAGE"] == "Italian"
        assert d.settings["RADIO_HOST_GAIN_DB"] == "6.5"

    @pytest.mark.parametrize("flag", [None, "", "false", "0"])
    def test_a_radio_host_that_was_never_switched_on_stays_where_it_is(
        self, flag: str | None
    ) -> None:
        # Its switch defaulted to off: filling the station in was not enough to go on air.
        rows = dict(self.RADIO)
        if flag is not None:
            rows["RADIO_HOST_ENABLED"] = flag
        d = decide(component("official/radio_host"), rows)
        assert not d.migrate and not d.install

    def test_a_radio_host_without_a_station_never_worked(self) -> None:
        rows = {**self.RADIO, "RADIO_HOST_ENABLED": "true"}
        del rows["AZURACAST_STATION_ID"]
        d = decide(component("official/radio_host"), rows)
        assert not d.migrate and "never worked" in d.reason

    def test_the_api_server_ran_on_its_defaults_so_it_stays_on(self) -> None:
        d = decide(component("official/openai_api"), {})
        assert d.migrate and not d.switch_off and d.settings == {}

    def test_the_api_server_renames_its_settings(self) -> None:
        d = decide(
            component("official/openai_api"),
            {
                "OLLAMA_MAX_HISTORY": "40",
                "OLLAMA_COMPLETION_TIMEOUT": "90",
                "OLLAMA_HOST": "0.0.0.0",
            },
        )
        assert d.settings == {
            "OPENAI_API_MAX_HISTORY": "40",
            "OPENAI_API_TIMEOUT": "90",
            "OPENAI_API_HOST": "0.0.0.0",
        }

    @pytest.mark.parametrize(
        "key", ["OLLAMA_SERVER_ENABLED", "INTERFACE_ENABLED__ollama_serve"]
    )
    def test_the_old_off_switches_of_the_api_server_are_honoured(
        self, key: str
    ) -> None:
        assert decide(component("official/openai_api"), {key: "false"}).switch_off

    def test_every_old_key_the_migration_reads_is_listed(self) -> None:
        keys = old_keys()
        assert "TELEGRAM_SESSION" in keys and "OLLAMA_SERVER_ENABLED" in keys
        assert "RADIO_HOST_ENABLED" in keys and "AZURACAST_API_KEY" in keys
        assert keys == sorted(set(keys))


class FakePort:
    def __init__(
        self, installed: set[str], *, install_error: str | None = None
    ) -> None:
        self.present = set(installed)
        self.install_error = install_error
        self.calls: list[tuple[Any, ...]] = []
        self.settings: dict[tuple[str, str], str] = {}
        self.sessions: dict[tuple[str, str], str] = {}

    def installed(self, extension_id: str) -> bool:
        return extension_id in self.present

    def install(self, extension_id: str) -> str | None:
        self.calls.append(("install", extension_id))
        if self.install_error:
            return self.install_error
        self.present.add(extension_id)
        return None

    def set_enabled(self, extension_id: str, enabled: bool) -> None:
        self.calls.append(("enabled", extension_id, enabled))

    async def copy_setting(self, extension_id: str, key: str, value: str) -> bool:
        self.settings[(extension_id, key)] = value
        return True

    async def copy_session(self, extension_id: str, key: str, value: str) -> bool:
        self.sessions[(extension_id, key)] = value
        return True


@pytest.mark.asyncio
class TestMigrate:
    async def test_defaults_are_filled_in_and_the_userbot_is_fetched(self) -> None:
        port = FakePort({"official/telegram_bot", "official/openai_api"})
        rows = {
            "BOTFATHER_TOKEN": "bt",
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "h",
            "TELEGRAM_SESSION": "S",
        }
        report = await migrate(rows, port)
        assert port.settings[("official/telegram_bot", "BOTFATHER_TOKEN")] == "bt"
        assert ("install", "official/telegram") in port.calls
        assert port.sessions == {("official/telegram", "session"): "S"}
        assert ("official/telegram", "TELEGRAM_API_HASH") in port.settings
        assert any(
            "official/fluxer" in line and "never worked" in line
            for line in report.lines
        )

    async def test_a_switched_off_default_is_turned_off_after_its_settings(
        self,
    ) -> None:
        port = FakePort({"official/fluxer"})
        await migrate(
            {"FLUXER_TOKEN": "t", "INTERFACE_ENABLED__fluxer_bot": "false"},
            port,
            [component("official/fluxer")],
        )
        assert port.calls == [("enabled", "official/fluxer", False)]
        assert port.settings[("official/fluxer", "FLUXER_TOKEN")] == "t"

    async def test_a_package_that_cannot_be_installed_is_reported_not_fatal(
        self,
    ) -> None:
        port = FakePort(set(), install_error="no repository offers it")
        rows = {
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "h",
            "TELEGRAM_SESSION": "S",
        }
        report = await migrate(rows, port, [component("official/telegram")])
        assert port.sessions == {} and port.settings == {}
        assert "could not be installed" in report.lines[0]
        assert "Plugins > Store" in report.lines[0]

    async def test_a_failure_in_one_component_does_not_stop_the_next(self) -> None:
        class Boom(FakePort):
            async def copy_setting(
                self, extension_id: str, key: str, value: str
            ) -> bool:
                if extension_id == "official/fluxer":
                    raise RuntimeError("db down")
                return await super().copy_setting(extension_id, key, value)

        port = Boom({"official/fluxer", "official/telegram_bot"})
        report = await migrate(
            {"FLUXER_TOKEN": "t", "BOTFATHER_TOKEN": "b"},
            port,
            [component("official/fluxer"), component("official/telegram_bot")],
        )
        assert "migration failed" in report.lines[0]
        assert port.settings[("official/telegram_bot", "BOTFATHER_TOKEN")] == "b"

    async def test_nothing_is_done_for_an_installation_that_never_used_them(
        self,
    ) -> None:
        port = FakePort({c.extension_id for c in COMPONENTS})
        report = await migrate(
            {}, port, [c for c in COMPONENTS if c.extension_id != "official/openai_api"]
        )
        assert port.calls == [] and port.settings == {} and port.sessions == {}
        assert all("nothing to carry over" in line for line in report.lines)


class FakeRegistry:
    def __init__(self) -> None:
        self.set: dict[str, Any] = {}

    async def set_value(self, key: str, value: Any) -> None:
        self.set[key] = value


class FakeStorage:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    async def get(self, extension_id: str, key: str) -> str | None:
        return self.data.get((extension_id, key))

    async def set(self, extension_id: str, key: str, value_json: str) -> None:
        self.data[(extension_id, key)] = value_json


@pytest.fixture
def adapter(tmp_path: Path) -> tuple[CoreLegacyPort, FakeRegistry, FakeStorage]:
    installer = Installer(tmp_path / "runtime", core_api_version="0.1.0")
    for name in ("fluxer", "telegram"):
        target = installer.install_dir(f"official/{name}")
        target.mkdir(parents=True)
        shutil.copy(
            ROOT / "extensions_src" / name / "synth-extension.toml",
            target / "synth-extension.toml",
        )
    registry, storage = FakeRegistry(), FakeStorage()
    return CoreLegacyPort(installer, registry, storage), registry, storage


@pytest.mark.asyncio
class TestAdapter:
    async def test_a_setting_is_written_under_the_extensions_own_key(
        self, adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage]
    ) -> None:
        port, registry, _ = adapter
        assert await port.copy_setting("official/fluxer", "FLUXER_TOKEN", "tok") is True
        assert registry.set == {config_key("official/fluxer", "FLUXER_TOKEN"): "tok"}

    async def test_a_boolean_is_normalised(
        self, adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage]
    ) -> None:
        port, registry, _ = adapter
        await port.copy_setting("official/telegram", "TELEGRAM_CALLS_ENABLED", "off")
        await port.copy_setting("official/telegram", "TELEGRAM_USE_SYNTH_AVATAR", "1")
        key = lambda k: config_key("official/telegram", k)  # noqa: E731
        assert registry.set[key("TELEGRAM_CALLS_ENABLED")] == "false"
        assert registry.set[key("TELEGRAM_USE_SYNTH_AVATAR")] == "true"

    async def test_a_key_the_extension_does_not_declare_is_ignored(
        self, adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage]
    ) -> None:
        port, registry, _ = adapter
        assert await port.copy_setting("official/fluxer", "NOT_A_SETTING", "x") is False
        assert await port.copy_setting("official/missing", "FLUXER_TOKEN", "x") is False
        assert registry.set == {}

    async def test_a_value_the_operator_already_set_is_kept(
        self,
        adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        port, registry, _ = adapter
        from core.extensions import legacy_port

        class Existing:
            def get(self, extension_id: str, field: Any) -> Any:
                return "already-here"

        monkeypatch.setattr(legacy_port, "CoreConfig", Existing)
        assert (
            await port.copy_setting("official/fluxer", "FLUXER_TOKEN", "old") is False
        )
        assert registry.set == {}

    async def test_the_session_is_stored_as_json_and_never_replaced(
        self, adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage]
    ) -> None:
        port, _, storage = adapter
        assert await port.copy_session("official/telegram", "session", "STR") is True
        assert json.loads(storage.data[("official/telegram", "session")]) == "STR"
        assert await port.copy_session("official/telegram", "session", "NEW") is False
        assert json.loads(storage.data[("official/telegram", "session")]) == "STR"

    async def test_install_reports_a_failure_instead_of_raising(
        self,
        adapter: tuple[CoreLegacyPort, FakeRegistry, FakeStorage],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        port, _, _ = adapter

        def refuse(extension_id: str, **kw: Any) -> Any:
            raise InstallError("untrusted repository")

        monkeypatch.setattr(port.installer, "install", refuse)
        assert port.install("official/telegram") == "untrusted repository"


@pytest.mark.asyncio
class TestStartup:
    async def test_it_runs_once_and_remembers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions import legacy_port, runtime

        reads: list[int] = []

        async def rows(keys: Any) -> dict[str, str]:
            reads.append(1)
            return {"FLUXER_TOKEN": "t"}

        port = FakePort({"official/fluxer"})
        monkeypatch.setattr(legacy_port, "read_rows", rows)
        monkeypatch.setattr(legacy_port, "CoreLegacyPort", lambda installer: port)
        monkeypatch.setattr(runtime, "bundled_repository", lambda: None)
        first = await runtime._migrate_legacy(tmp_path)  # noqa: SLF001
        second = await runtime._migrate_legacy(tmp_path)  # noqa: SLF001
        assert "official/fluxer: in use" in first
        assert second == "" and len(reads) == 1
        assert port.settings == {("official/fluxer", "FLUXER_TOKEN"): "t"}

    async def test_an_unreadable_database_is_retried_next_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.extensions import legacy_port, runtime
        from core.extensions.repo.installer import Installer

        async def broken(keys: Any) -> dict[str, str]:
            raise ConnectionError("db down")

        monkeypatch.setattr(legacy_port, "read_rows", broken)
        monkeypatch.setattr(runtime, "bundled_repository", lambda: None)
        note = await runtime._migrate_legacy(tmp_path)  # noqa: SLF001
        assert "not migrated" in note and "ConnectionError" in note
        assert Installer(tmp_path).state.load().legacy_done is False
