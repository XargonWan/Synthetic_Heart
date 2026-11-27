"""Who wrote a plugin: a name, a list of names, XargonWan when nobody said."""

from __future__ import annotations

import pytest

from core.authorship import DEFAULT_AUTHOR, authors_of


class Plain:
    pass


class One:
    author = "Scarlet-Raine"


class Two:
    author = ("XargonWan", "Scarlet-Raine")


class ByMetadata:
    author = "ignored"

    def get_metadata(self) -> dict:
        return {"author": ["A", "B"]}


class BrokenMetadata:
    author = "kept"

    def get_metadata(self) -> dict:
        raise RuntimeError("boom")


def test_a_plugin_that_says_nothing_is_xargonwans() -> None:
    assert authors_of(Plain()) == [DEFAULT_AUTHOR] == ["XargonWan"]


def test_a_name_or_a_list_is_accepted() -> None:
    assert authors_of(One()) == ["Scarlet-Raine"]
    assert authors_of(Two()) == ["XargonWan", "Scarlet-Raine"]


def test_metadata_wins_over_the_attribute_and_a_broken_one_is_ignored() -> None:
    assert authors_of(ByMetadata()) == ["A", "B"]
    assert authors_of(BrokenMetadata()) == ["kept"]


@pytest.mark.parametrize("value", ["", "  ", [], [""], 3, None])
def test_an_unusable_value_falls_back_to_the_default(value: object) -> None:
    holder = type("Holder", (), {"author": value})()
    assert authors_of(holder) == [DEFAULT_AUTHOR]


def test_duplicates_are_listed_once() -> None:
    holder = type("Holder", (), {"author": ["A", "A", " B "]})()
    assert authors_of(holder) == ["A", "B"]


def test_the_shipped_plugins_name_their_authors() -> None:
    from plugins.agpeer.agpeer import AgpeerPlugin
    from plugins.home_assistant.home_assistant import HomeAssistantPlugin
    from plugins.live_base import LiveEngineBase
    from plugins.pdf_voice.pdf_voice import PDFVoicePlugin
    from plugins.soul_plugin.soul_plugin import SoulPlugin
    from plugins.weather_plugin.weather_plugin import WeatherPlugin
    from plugins.vox_plugin.vox_plugin import VoxPlugin

    for cls in (AgpeerPlugin, HomeAssistantPlugin, LiveEngineBase, PDFVoicePlugin):
        assert authors_of(cls) == ["Scarlet-Raine"], cls
    for cls in (SoulPlugin, WeatherPlugin):
        assert authors_of(cls) == ["XargonWan", "Scarlet-Raine"], cls
    assert authors_of(VoxPlugin) == ["XargonWan"]
