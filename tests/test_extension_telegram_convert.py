"""Pure conversion helpers of the Telegram extension, plus its conformance rules."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "telegram_bot"
_spec = importlib.util.spec_from_file_location("tg_convert", SOURCE / "convert.py")
assert _spec and _spec.loader
convert = importlib.util.module_from_spec(_spec)
sys.modules["tg_convert"] = convert
_spec.loader.exec_module(convert)


def ns(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(**kw)


class TestPaths:
    def test_build_and_parse_round_trip(self) -> None:
        assert convert.build_path(-100123, 77) == "telegram_bot/-100123/77"
        assert convert.build_path(42) == "telegram_bot/42"
        assert convert.parse_path("telegram_bot/-100123/77") == (-100123, 77)
        assert convert.parse_path("telegram_bot/42") == (42, None)

    @pytest.mark.parametrize(
        "path",
        [
            None,
            "",
            "discord_bot/1/2",
            "telegram_bot",
            "telegram_bot/abc",
            "telegram_bot//",
        ],
    )
    def test_foreign_or_malformed_paths_are_not_ours(self, path: str | None) -> None:
        assert convert.parse_path(path) is None

    def test_a_non_numeric_thread_is_dropped_not_fatal(self) -> None:
        assert convert.parse_path("telegram_bot/5/no thread id") == (5, None)


class TestReplies:
    @pytest.mark.parametrize("value", ["-100123", "abc", None, "0", "-4", ""])
    def test_values_that_are_not_message_ids_are_dropped(
        self, value: str | None
    ) -> None:
        assert convert.usable_reply_id(value, -100123) is None

    def test_a_real_message_id_is_kept(self) -> None:
        assert convert.usable_reply_id("31", -100123) == 31
        assert convert.usable_reply_id(31, 5) == 31

    def test_the_topic_opener_is_not_a_reply(self) -> None:
        opener = ns(message_id=77, text="topic created", from_user=None)
        message = ns(
            message_thread_id=77, is_topic_message=True, reply_to_message=opener
        )
        assert convert.reply_info(message) is None

    def test_a_real_reply_inside_a_topic_is_kept(self) -> None:
        original = ns(
            message_id=80,
            text="question",
            from_user=ns(id=6, full_name="Carol", username="c"),
        )
        message = ns(
            message_thread_id=77, is_topic_message=True, reply_to_message=original
        )
        assert convert.reply_info(message) == {
            "sender_name": "Carol",
            "text": "question",
            "message_id": "80",
            "sender_id": "6",
        }


class TestSplitting:
    def test_short_text_is_one_chunk(self) -> None:
        assert convert.split_text("hi") == ["hi"]

    def test_every_chunk_fits_and_nothing_is_lost(self) -> None:
        for text in ("a" * 10_000, "word " * 3000, ("line\n" * 900)):
            chunks = convert.split_text(text, 4096)
            assert all(len(c) <= 4096 for c in chunks)
            assert "".join(chunks) == text

    def test_cuts_at_a_line_end_when_there_is_one(self) -> None:
        text = ("x" * 3000 + "\n") * 2
        assert convert.split_text(text, 4096)[0].endswith("\n")


class TestExtensionRules:
    def test_the_extension_never_imports_the_core(self) -> None:
        forbidden = {"core", "plugins", "interface", "engines", "main"}
        for path in SOURCE.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in forbidden, (
                        f"{path.name} imports {name}"
                    )

    def test_the_manifest_declares_everything_the_code_calls(self) -> None:
        from core.extensions.manifest import load_manifest

        manifest = load_manifest(SOURCE / "synth-extension.toml")
        declared = set(manifest.permissions.capabilities)
        used = {"log", "config", "stats", "interfaces.register", "interfaces.inbound"}
        assert used <= declared
        assert manifest.interface_ids == ("telegram_bot",)
