"""Tests for ``core.extensions.manifest`` (platform milestone M1)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from core.extensions.manifest import (
    Entry,
    ManifestError,
    load_manifest,
    parse_manifest,
    parse_manifest_text,
)

FULL = """
schema = 1

[extension]
id = "official/example_chat"
name = "Example Chat"
version = "1.2.0"
description = "Example interface."
author = "Example Author"
license = "MIT"
experimental = true

[compat]
core_api = ">=0.1,<1"

[entry]
module = "example_chat.main"
function = "run"

[provides]
actions = ["message_example_chat"]
engines = ["llm"]
hooks = ["on_debrief"]

[[provides.interfaces]]
id = "example_chat"
paths = ["example_chat/{room}", "example_chat/{room}/{thread}"]

[requires]
services = ["searxng"]

[[requires.extensions]]
id = "official/vrm_renderer"
version = ">=1,<2"
optional = true

[permissions]
capabilities = ["interfaces.register", "storage"]
network = ["api.example.org:443", "*.example.org:8443"]

[python]
requirements = ["httpx>=0.27", "pydantic==2.*"]

[config.API_TOKEN]
type = "string"
label = "API token"
sensitive = true
required = true

[config.MAX_ROOMS]
type = "int"
default = 5
"""

MINIMAL = """
[extension]
id = "dev/minimal"
name = "Minimal"
version = "0.1.0"

[compat]
core_api = ">=0.1"
"""


def _data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "extension": {"id": "dev/x", "name": "X", "version": "1.0"},
        "compat": {"core_api": ">=0.1"},
    }
    base.update(overrides)
    return base


def _errors(data: dict[str, Any]) -> tuple[str, ...]:
    with pytest.raises(ManifestError) as info:
        parse_manifest(data, source="test")
    return info.value.errors


class TestValidManifests:
    def test_full_manifest(self) -> None:
        m = parse_manifest_text(FULL)
        assert m.id == "official/example_chat"
        assert m.version == "1.2.0"
        assert m.experimental is True
        assert m.core_api == ">=0.1,<1"
        assert m.entry == Entry(module="example_chat.main", function="run")
        assert m.interface_ids == ("example_chat",)
        assert m.provides.interfaces[0].paths == (
            "example_chat/{room}",
            "example_chat/{room}/{thread}",
        )
        assert m.provides.actions == ("message_example_chat",)
        assert m.provides.engines == ("llm",)
        assert m.requires.services == ("searxng",)
        dep = m.requires.extensions[0]
        assert (dep.id, dep.version, dep.optional) == (
            "official/vrm_renderer",
            ">=1,<2",
            True,
        )
        assert m.permissions.capabilities == ("interfaces.register", "storage")
        assert m.python_requirements == ("httpx>=0.27", "pydantic==2.*")
        keys = {f.key: f for f in m.config}
        assert keys["API_TOKEN"].sensitive and keys["API_TOKEN"].required
        assert keys["MAX_ROOMS"].type == "int" and keys["MAX_ROOMS"].default == 5

    def test_minimal_manifest_has_safe_defaults(self) -> None:
        m = parse_manifest_text(MINIMAL)
        assert m.entry is None
        assert m.provides.interfaces == () and m.provides.actions == ()
        assert m.permissions.capabilities == () and m.permissions.network == ()
        assert m.requires.extensions == ()
        assert m.experimental is False

    def test_manifest_is_immutable(self) -> None:
        m = parse_manifest_text(MINIMAL)
        with pytest.raises(AttributeError):
            m.id = "other/id"  # type: ignore[misc]

    def test_load_manifest_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "synth-extension.toml"
        path.write_text(MINIMAL, encoding="utf-8")
        assert load_manifest(path).id == "dev/minimal"


class TestInvalidManifests:
    def test_reports_every_problem_at_once(self) -> None:
        data = _data(
            extension={"id": "Bad Id", "name": "", "version": "not-a-version"},
            compat={"core_api": "garbage"},
            permissions={"capabilities": ["Not Valid"], "network": ["nohost"]},
        )
        errors = _errors(data)
        joined = "\n".join(errors)
        assert "extension.id" in joined
        assert "extension.name" in joined
        assert "extension.version" in joined
        assert "compat.core_api" in joined
        assert "permissions.capabilities" in joined
        assert "permissions.network" in joined
        assert len(errors) >= 6

    def test_message_names_the_source_and_each_problem(self) -> None:
        with pytest.raises(ManifestError) as info:
            parse_manifest({"extension": {}}, source="my/file.toml")
        text = str(info.value)
        assert "my/file.toml" in text
        assert "extension.id: is required" in text

    def test_missing_extension_table(self) -> None:
        assert any("extension" in e and "required" in e for e in _errors({}))

    def test_core_api_is_required(self) -> None:
        data = _data()
        data["compat"] = {}
        assert any(e.startswith("compat.core_api") for e in _errors(data))

    def test_unknown_keys_are_errors_not_ignored(self) -> None:
        assert any("unknown key" in e and "wat" in e for e in _errors(_data(wat={})))
        data = _data()
        data["extension"]["typo_key"] = 1
        assert any("extension.typo_key" in e for e in _errors(data))
        data = _data(permissions={"capabilites": ["storage"]})
        assert any("permissions.capabilites" in e for e in _errors(data))

    def test_removed_legacy_key_is_rejected_as_unknown(self) -> None:
        data = _data()
        data["extension"]["legacy"] = True
        assert any("extension.legacy" in e for e in _errors(data))

    @pytest.mark.parametrize(
        "ext_id", ["noslash", "Upper/case", "a/b/c", "/x", "x/", "a b/c", ""]
    )
    def test_bad_extension_ids(self, ext_id: str) -> None:
        data = _data()
        data["extension"]["id"] = ext_id
        assert any(e.startswith("extension.id") for e in _errors(data))

    def test_unsupported_schema(self) -> None:
        assert any(e.startswith("schema") for e in _errors(_data(schema=2)))
        assert any(e.startswith("schema") for e in _errors(_data(schema=True)))

    def test_invalid_toml(self) -> None:
        with pytest.raises(ManifestError) as info:
            parse_manifest_text("this is = = not toml", source="bad")
        assert "not valid TOML" in info.value.errors[0]

    def test_unreadable_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError) as info:
            load_manifest(tmp_path / "missing.toml")
        assert "cannot read file" in info.value.errors[0]

    def test_interface_path_must_start_with_interface_id(self) -> None:
        data = _data(
            provides={"interfaces": [{"id": "chat", "paths": ["other/{room}"]}]}
        )
        assert any("must start with the interface id" in e for e in _errors(data))

    def test_interface_ids_must_be_identifiers_and_unique(self) -> None:
        data = _data(provides={"interfaces": [{"id": "Bad-Id"}]})
        assert any("provides.interfaces[0].id" in e for e in _errors(data))
        data = _data(provides={"interfaces": [{"id": "a"}, {"id": "a"}]})
        assert any("declared twice" in e for e in _errors(data))

    def test_unknown_engine_kind(self) -> None:
        errors = _errors(_data(provides={"engines": ["llm", "teleporter"]}))
        assert any("teleporter" in e for e in errors)

    def test_duplicate_list_entries(self) -> None:
        errors = _errors(_data(provides={"actions": ["a_one", "a_one"]}))
        assert any("duplicate entries" in e for e in errors)

    def test_self_dependency_and_duplicates(self) -> None:
        data = _data(requires={"extensions": [{"id": "dev/x"}]})
        assert any("cannot depend on itself" in e for e in _errors(data))
        data = _data(requires={"extensions": [{"id": "a/b"}, {"id": "a/b"}]})
        assert any("declared twice" in e for e in _errors(data))

    def test_dependency_version_specifier_is_validated(self) -> None:
        data = _data(requires={"extensions": [{"id": "a/b", "version": "~~1"}]})
        assert any("requires.extensions[0].version" in e for e in _errors(data))

    @pytest.mark.parametrize(
        "dest", ["host", "host:0", "host:70000", "ho st:80", ":80"]
    )
    def test_bad_network_destinations(self, dest: str) -> None:
        errors = _errors(_data(permissions={"network": [dest]}))
        assert any("permissions.network" in e for e in errors)

    def test_bad_python_requirement(self) -> None:
        errors = _errors(_data(python={"requirements": ["!!bad"]}))
        assert any("python.requirements" in e for e in errors)

    def test_config_key_type_and_default_checks(self) -> None:
        errors = _errors(_data(config={"lower_case": {}}))
        assert any("UPPER_SNAKE_CASE" in e for e in errors)
        errors = _errors(_data(config={"K": {"type": "weird"}}))
        assert any("config.K.type" in e for e in errors)
        errors = _errors(_data(config={"K": {"type": "int", "default": "x"}}))
        assert any("config.K.default" in e for e in errors)
        # bool is not an acceptable int default
        errors = _errors(_data(config={"K": {"type": "int", "default": True}}))
        assert any("config.K.default" in e for e in errors)

    def test_entry_module_and_function(self) -> None:
        errors = _errors(_data(entry={"module": "not a module", "function": "1x"}))
        assert any("entry.module" in e for e in errors)
        assert any("entry.function" in e for e in errors)

    def test_wrong_types_are_reported_not_crashed(self) -> None:
        data = _data(provides="oops", requires=[], permissions={"capabilities": "x"})
        errors = _errors(data)
        assert any(e.startswith("provides") for e in errors)
        assert any(e.startswith("requires") for e in errors)
        assert any("permissions.capabilities" in e for e in errors)


class TestDocumentation:
    def test_manifest_example_in_the_docs_is_valid(self) -> None:
        """The example in ``manifest_proposal.rst`` must parse with the real parser."""
        page = (
            Path(__file__).resolve().parents[1]
            / "docs/plugin_platform/manifest_proposal.rst"
        )
        match = re.search(
            r"\.\. code-block:: toml\n\n((?:   .*\n|\n)+)", page.read_text("utf-8")
        )
        assert match is not None
        block = "\n".join(line[3:] for line in match.group(1).splitlines())
        manifest = parse_manifest_text(block, source="manifest_proposal.rst")
        assert manifest.id == "official/example_chat"
        assert manifest.interface_ids == ("example_chat",)


class TestInterfaceTraits:
    def _interface(self, **extra: Any) -> Any:
        data = _data()
        data["provides"] = {"interfaces": [{"id": "chat", **extra}]}
        return parse_manifest(data).provides.interfaces[0]

    def test_defaults_are_the_plain_chat(self) -> None:
        item = self._interface()
        assert (item.user_facing, item.voice_input_tts_only, item.peer_ids) == (
            True,
            False,
            False,
        )
        assert (
            item.negative_chat_id_is_group is False and item.bot_username_suffix == ""
        )

    def test_every_trait_can_be_declared(self) -> None:
        item = self._interface(
            user_facing=False,
            voice_input_tts_only=True,
            negative_chat_id_is_group=True,
            bot_username_suffix="bot",
            peer_ids=True,
        )
        assert item.user_facing is False and item.voice_input_tts_only is True
        assert item.negative_chat_id_is_group and item.peer_ids
        assert item.bot_username_suffix == "bot"

    def test_a_trait_of_the_wrong_type_is_an_error(self) -> None:
        data = _data()
        data["provides"] = {"interfaces": [{"id": "chat", "peer_ids": "yes"}]}
        assert any("peer_ids" in e for e in _errors(data))
