"""``core.interface_traits``: interfaces declare what the Core used to hardcode."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core import interface_traits as it


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    it.clear_declared_traits()
    yield
    it.clear_declared_traits()


class TestDefaults:
    def test_an_unknown_interface_gets_plain_defaults_but_is_not_user_facing(
        self,
    ) -> None:
        assert it.traits_for("nobody") == it.DEFAULT
        assert it.message_action_for("nobody/1") == "send_message"
        assert it.is_user_facing("nobody/1") is False  # the Core has never heard of it

    def test_core_interfaces_keep_their_old_behaviour(self) -> None:
        assert it.is_user_facing("discord_bot/1/2") and it.is_voice_input_tts_only(
            "discord_bot"
        )
        assert it.message_action_for("synth_webui/x") == "message_synth_webui"

    def test_empty_values_are_safe(self) -> None:
        assert it.traits_for(None) == it.DEFAULT and it.traits_for("") == it.DEFAULT
        assert (
            it.chat_kind_for_path(None) is None and it.chat_kind_for_path("x") is None
        )


class TestDeclared:
    def test_a_declaration_makes_the_interface_known(self) -> None:
        it.register_traits("chatty", it.InterfaceTraits(voice_input_tts_only=True))
        assert it.is_user_facing("chatty/42")
        assert it.is_voice_input_tts_only("chatty/42")
        assert "chatty" in it.user_facing_ids()

    def test_unregistering_forgets_it(self) -> None:
        it.register_traits("chatty", it.InterfaceTraits())
        it.unregister_traits("chatty")
        assert (
            not it.is_user_facing("chatty/1") and "chatty" not in it.user_facing_ids()
        )

    def test_a_declaration_overrides_a_core_default(self) -> None:
        it.register_traits(
            "discord_bot", it.InterfaceTraits(voice_input_tts_only=False)
        )
        assert not it.is_voice_input_tts_only("discord_bot")

    def test_user_facing_ids_excludes_non_chat_interfaces(self) -> None:
        it.register_traits(
            "onair",
            it.InterfaceTraits(user_facing=False, message_action="speak_on_air"),
        )
        assert "onair" not in it.user_facing_ids()
        assert "discord_bot" in it.user_facing_ids()
        # a declared interface says how it speaks; the Core knows no name for it
        assert it.message_action_for("onair/1") == "speak_on_air"
        assert "speak_on_air" in it.message_actions()


class TestChatKind:
    def test_negative_ids_are_groups_only_for_interfaces_that_say_so(self) -> None:
        it.register_traits("signed", it.InterfaceTraits(negative_chat_id_is_group=True))
        it.register_traits("plain", it.InterfaceTraits())
        assert it.chat_kind_for_path("signed/-100/5") == "group"
        assert it.chat_kind_for_path("signed/42") == "dm"
        assert it.chat_kind_for_path("signed/abc") is None
        assert it.chat_kind_for_path("plain/-100") is None
        assert it.chat_kind_for_path("signed") is None


class TestBotsAndPeers:
    def test_bot_username_suffix_is_case_insensitive_and_per_interface(self) -> None:
        it.register_traits("signed", it.InterfaceTraits(bot_username_suffix="bot"))
        assert it.is_bot_username("signed/1", "Helper_BOT")
        assert not it.is_bot_username("signed/1", "alice")
        assert not it.is_bot_username("other/1", "helper_bot")
        assert not it.is_bot_username("signed/1", None)

    def test_peer_ids_belong_to_the_declaring_interface(self) -> None:
        it.register_traits("signed", it.InterfaceTraits(peer_ids=True))
        assert it.has_peer_ids("signed/1") and not it.has_peer_ids("discord_bot")


def test_speech_as_media_is_declared_by_the_interface_not_named_by_the_core():
    from pathlib import Path

    from core.extensions.manifest import load_manifest
    from core.interface_traits import traits_for

    assert traits_for("discord_bot").speech_as_media is True
    assert traits_for("some_new_interface").speech_as_media is False
    manifest = load_manifest(
        Path(__file__).resolve().parent.parent
        / "extensions_src/telegram_bot/synth-extension.toml"
    )
    provided = {i.id: i for i in manifest.provides.interfaces}
    assert provided["telegram_bot"].speech_as_media is True
