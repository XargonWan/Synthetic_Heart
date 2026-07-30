"""Unit tests for the Rift Vessel action-whitelist helper.

Covers the pure, self-contained helpers in
``plugins.rift_vessel.vessel_whitelist`` — parsing, structural ``fnmatch``
matching, and the hardcoded vessel/game pattern derivation. No DB, bridge, or
LLM is involved; the helpers are keyword-free and structural.
"""

from __future__ import annotations

from plugins.rift_vessel.vessel_whitelist import (
    DEFAULT_WHITELIST,
    hardcoded_vessel_patterns,
    matches_whitelist,
    parse_patterns,
)


class TestParsePatterns:
    def test_comma_separated(self) -> None:
        assert parse_patterns("a, b, c") == ["a", "b", "c"]

    def test_newline_separated(self) -> None:
        assert parse_patterns("a\nb\nc") == ["a", "b", "c"]

    def test_mixed_comma_and_newline(self) -> None:
        assert parse_patterns("a, b\nc,d") == ["a", "b", "c", "d"]

    def test_strips_whitespace(self) -> None:
        assert parse_patterns("  a ,\t b \n  c  ") == ["a", "b", "c"]

    def test_drops_empty_tokens(self) -> None:
        assert parse_patterns("a,,b,\n,c,") == ["a", "b", "c"]

    def test_empty_string(self) -> None:
        assert parse_patterns("") == []

    def test_none(self) -> None:
        assert parse_patterns(None) == []

    def test_non_string(self) -> None:
        assert parse_patterns(123) == []  # type: ignore[arg-type]

    def test_default_whitelist_parses(self) -> None:
        assert parse_patterns(DEFAULT_WHITELIST) == [
            "message_*",
            "event",
            "schedule_message",
            "blocklist",
            "spawn_drone",
        ]


class TestMatchesWhitelist:
    def test_exact_hit(self) -> None:
        assert matches_whitelist("event", ["event"]) is True

    def test_glob_hit(self) -> None:
        assert matches_whitelist("message_telegram_bot", ["message_*"]) is True

    def test_miss(self) -> None:
        assert matches_whitelist("tts_speak", ["message_*", "event"]) is False

    def test_empty_patterns(self) -> None:
        assert matches_whitelist("event", []) is False

    def test_empty_name(self) -> None:
        assert matches_whitelist("", ["*"]) is False

    def test_non_string_name(self) -> None:
        assert matches_whitelist(None, ["*"]) is False  # type: ignore[arg-type]

    def test_case_sensitive(self) -> None:
        # fnmatchcase is case-sensitive: an upper-case name must not match a
        # lower-case pattern.
        assert matches_whitelist("EVENT", ["event"]) is False

    def test_multiple_patterns_any_hit(self) -> None:
        patterns = ["message_*", "event", "spawn_drone"]
        assert matches_whitelist("spawn_drone", patterns) is True


class TestHardcodedVesselPatterns:
    def test_with_world(self) -> None:
        assert hardcoded_vessel_patterns("minecraft") == [
            "vessel_*",
            "*_minecraft_*",
        ]

    def test_none_world(self) -> None:
        assert hardcoded_vessel_patterns(None) == ["vessel_*"]

    def test_blank_world(self) -> None:
        assert hardcoded_vessel_patterns("   ") == ["vessel_*"]

    def test_generic_vessel_token_excluded(self) -> None:
        assert hardcoded_vessel_patterns("vessel") == ["vessel_*"]

    def test_disabled_token_excluded(self) -> None:
        assert hardcoded_vessel_patterns("disabled") == ["vessel_*"]

    def test_world_token_stripped(self) -> None:
        assert hardcoded_vessel_patterns("  skyrim  ") == [
            "vessel_*",
            "*_skyrim_*",
        ]

    def test_game_verb_matches_hardcoded(self) -> None:
        # Sanity: a namespaced world verb matches the derived hardcoded patterns.
        patterns = hardcoded_vessel_patterns("minecraft")
        assert matches_whitelist("vessel_minecraft_say", patterns) is True
        assert matches_whitelist("vessel_disconnect", patterns) is True
