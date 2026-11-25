"""Tests for ``synth_sdk.paths``: shared Interface Path helpers (milestone M3)."""

from __future__ import annotations

import pytest

from core import interface_path_utils as core_paths
from synth_sdk import paths


class TestSameBehaviourAsTheCore:
    """An extension builds paths locally; the Core must read them identically."""

    CASES = [
        ("telegram_bot", ("123", "2")),
        ("telegram_bot", ("123",)),
        ("synth_webui", ()),
        ("discord_bot", ("g", None, "c")),
        ("x", (1, 2.5, "é")),
        ("x", (None,)),
    ]

    @pytest.mark.parametrize(("name", "levels"), CASES)
    def test_build_matches(self, name: str, levels: tuple[object, ...]) -> None:
        assert paths.build_interface_path(
            name, *levels
        ) == core_paths.build_interface_path(name, *levels)

    @pytest.mark.parametrize(
        "path",
        ["", "a", "a/b", "a/b/c", "a//b", "/a", "a/", "telegram_bot/-100/2", "a b/c d"],
    )
    def test_parse_and_validity_match(self, path: str) -> None:
        assert paths.parse_interface_path(path) == core_paths.parse_interface_path(path)
        assert paths.is_valid_interface_path(
            path
        ) == core_paths.is_valid_interface_path(path)

    def test_build_then_parse_round_trips_when_levels_have_no_slash(self) -> None:
        built = paths.build_interface_path("chat", "room-1", "thread_2")
        assert paths.parse_interface_path(built) == ("chat", ["room-1", "thread_2"])


class TestPathProblem:
    @pytest.mark.parametrize("path", ["a", "a/b", "chat/room/thread", "x/" + "é" * 100])
    def test_acceptable_paths(self, path: str) -> None:
        assert paths.path_problem(path) is None

    @pytest.mark.parametrize(
        ("path", "needle"),
        [
            ("", "non-empty"),
            (None, "non-empty"),
            (5, "non-empty"),
            ("a//b", "empty segment"),
            ("a/ /b", "empty segment"),
            ("a/b\nc", "control"),
            ("a/\x00", "control"),
            ("a/" + "x" * 600, "longer than"),
            ("a/" + "x" * 250, "segment longer"),
            ("a/" + "/".join("x" for _ in range(9)), "levels"),
        ],
    )
    def test_refused_paths(self, path: object, needle: str) -> None:
        problem = paths.path_problem(path)
        assert problem is not None and needle in problem


class TestPatterns:
    def test_expand_plain_and_optional(self) -> None:
        assert paths.expand_pattern("chat/{room}") == [("chat", "{room}")]
        assert paths.expand_pattern("chat/{room}[/{thread}]") == [
            ("chat", "{room}"),
            ("chat", "{room}", "{thread}"),
        ]
        assert paths.expand_pattern("chat/{a}[/{b}][/{c}]") == [
            ("chat", "{a}"),
            ("chat", "{a}", "{b}"),
            ("chat", "{a}", "{b}", "{c}"),
        ]
        assert paths.expand_pattern("chat/dm/{user}") == [("chat", "dm", "{user}")]

    @pytest.mark.parametrize(
        "pattern",
        ["", "   ", "chat//{a}", "chat/{A}", "chat/{a b}", "chat/{a}[/{b}", "chat/[/{a}]x",
         "chat/{a}[/{b}]/{c}", "chat/{a}[[/{b}]]", "chat/a}b", "chat/{}"],
    )  # fmt: skip
    def test_malformed_patterns_are_rejected(self, pattern: str) -> None:
        with pytest.raises(ValueError):
            paths.expand_pattern(pattern)

    def test_match_path(self) -> None:
        patterns = ("chat/{room}[/{thread}]", "chat/dm/{user}/x")
        assert paths.match_path("chat/r1", patterns)
        assert paths.match_path("chat/r1/t1", patterns)
        assert paths.match_path("chat/dm/u1/x", patterns)
        assert not paths.match_path("chat", patterns)
        assert not paths.match_path("chat/r1/t1/t2", patterns)
        assert not paths.match_path("chat/dm/u1/y", ("chat/dm/{user}/x",))
        assert not paths.match_path("other/r1", patterns)

    def test_a_literal_must_match_exactly(self) -> None:
        assert paths.match_path("chat/dm/u", ("chat/dm/{user}",))
        assert not paths.match_path("chat/DM/u", ("chat/dm/{user}",))

    def test_a_malformed_pattern_never_matches(self) -> None:
        assert not paths.match_path("chat/r1", ("chat/{",))
        assert paths.match_path("chat/r1", ("chat/{", "chat/{room}"))

    def test_the_default_is_the_legacy_chat_and_thread_shape(self) -> None:
        patterns = paths.default_patterns("telegram_bot")
        assert paths.match_path("telegram_bot/1", patterns)
        assert paths.match_path("telegram_bot/1/2", patterns)
        assert not paths.match_path("telegram_bot/1/2/3", patterns)
