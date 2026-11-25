"""Pure helpers of the Telegram user-account extension, and its conformance rules."""

from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "telegram"
_spec = importlib.util.spec_from_file_location("tgu_convert", SOURCE / "convert.py")
assert _spec and _spec.loader
convert = importlib.util.module_from_spec(_spec)
sys.modules["tgu_convert"] = convert
_spec.loader.exec_module(convert)


def ns(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(**kw)


class FloodWaitError(Exception):
    seconds = 7


class MsgIdInvalidError(Exception):
    pass


class TopicIdInvalidError(Exception):
    pass


class PeerIdInvalidError(Exception):
    pass


class EntitiesTooLongError(Exception):
    pass


class SomethingElseError(Exception):
    message = "WHATEVER"


class User(SimpleNamespace):
    pass


class Channel(SimpleNamespace):
    pass


class TestPaths:
    def test_round_trip_and_usernames(self) -> None:
        assert convert.build_path(-100123, 77) == "telegram/-100123/77"
        assert convert.parse_path("telegram/-100123/77") == (-100123, 77)
        assert convert.parse_path("telegram/42") == (42, None)
        assert convert.parse_path("telegram/@alice_x") == ("@alice_x", None)
        assert convert.parse_path("telegram/@alice_x/9") == ("@alice_x", 9)

    @pytest.mark.parametrize(
        "path",
        [
            None,
            "",
            "telegram_bot/1",
            "telegram",
            "telegram/abc",
            "telegram/@x",
            "telegram//",
        ],
    )
    def test_foreign_or_malformed_paths_are_not_ours(self, path: str | None) -> None:
        assert convert.parse_path(path) is None

    def test_a_non_numeric_thread_is_dropped(self) -> None:
        assert convert.parse_path("telegram/5/no thread") == (5, None)


class TestReplyIds:
    @pytest.mark.parametrize("value", ["-100123", "abc", None, "0", "-4", ""])
    def test_values_that_are_not_message_ids_are_dropped(
        self, value: str | None
    ) -> None:
        assert convert.usable_reply_id(value, -100123) is None

    def test_a_real_one_is_kept(self) -> None:
        assert (
            convert.usable_reply_id("31", -100123) == 31
            and convert.usable_reply_id(31, "@a_user") == 31
        )


class TestText:
    def test_splitting_loses_nothing_and_fits(self) -> None:
        for text in ("a" * 10_000, "word " * 3000, "line\n" * 900):
            chunks = convert.split_text(text)
            assert all(len(c) <= 4096 for c in chunks) and "".join(chunks) == text

    def test_markdown_is_converted_to_telethons_dialect(self) -> None:
        assert convert.legacy_markdown_to_telethon("*b* and _i_ snake_case 2*3*4") == (
            "**b** and __i__ snake_case 2*3*4"
        )

    def test_groups_get_plain_text_and_private_chats_markdown(self) -> None:
        assert (
            convert.parse_mode_for(-100) is None
            and convert.parse_mode_for(42) == "Markdown"
        )


class TestErrors:
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (MsgIdInvalidError(), convert.REPLY_GONE),
            (TopicIdInvalidError(), convert.THREAD_GONE),
            (PeerIdInvalidError(), convert.CHAT_GONE),
            (EntitiesTooLongError(), convert.BAD_MARKUP),
            (ValueError("Could not find the input entity for 5"), convert.CHAT_GONE),
            (SomethingElseError(), convert.OTHER),
            (RuntimeError("x"), convert.OTHER),
        ],
    )
    def test_failures_are_classified_by_name(self, error: Exception, kind: str) -> None:
        assert convert.classify_error(error)[0] == kind

    def test_flood_waits_say_how_long(self) -> None:
        assert convert.classify_error(FloodWaitError()) == (convert.FLOOD, 7.0)


class TestMessages:
    def test_forum_topics_and_headers(self) -> None:
        topic = ns(
            reply_to=ns(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=77)
        )
        deeper = ns(
            reply_to=ns(forum_topic=True, reply_to_top_id=70, reply_to_msg_id=80)
        )
        plain = ns(
            reply_to=ns(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=3)
        )
        assert convert.thread_of(topic) == 77 and convert.thread_of(deeper) == 70
        assert (
            convert.thread_of(plain) is None
            and convert.thread_of(ns(reply_to=None)) is None
        )

    def test_chat_kinds_and_names(self) -> None:
        user = User(id=1, first_name="Alice", last_name="Smith", username="a")
        group = Channel(id=-1, title="Room", username=None)
        assert (
            convert.chat_kind(user) == "private" and convert.chat_kind(group) == "group"
        )
        assert (
            convert.title_of(user) == "Alice Smith"
            and convert.title_of(group) == "Room"
        )
        assert (
            convert.full_name(User(id=2, first_name=None, last_name=None, username="u"))
            == "u"
        )

    def test_backlog_is_decided_by_the_messages_own_age(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert convert.is_backlog(now - timedelta(seconds=300), now, 120) is True
        assert convert.is_backlog(now - timedelta(seconds=30), now, 120) is False
        assert (
            convert.is_backlog(now - timedelta(days=9), now, 0) is False
        )  # switched off
        assert convert.is_backlog(None, now, 120) is False
        assert (
            convert.is_backlog(datetime(2026, 1, 1, 11, 0), now, 120) is True
        )  # naive = UTC

    def test_media_kinds_and_spoken(self) -> None:
        voice = ns(mime_type="audio/ogg", attributes=[])
        doc = ns(mime_type="application/pdf", attributes=[ns(file_name="a.pdf")])
        message = ns(
            photo=None,
            voice=voice,
            video_note=None,
            video=None,
            audio=None,
            sticker=None,
            document=doc,
        )
        assert convert.media_kinds(message) == [("voice", "voice.ogg", "audio/ogg")]
        assert convert.is_spoken(message) is True
        only_doc = ns(
            photo=None,
            voice=None,
            video_note=None,
            video=None,
            audio=None,
            sticker=None,
            document=doc,
        )
        assert convert.media_kinds(only_doc) == [
            ("document", "a.pdf", "application/pdf")
        ]
        assert convert.is_spoken(only_doc) is False

    def test_reply_block(self) -> None:
        sender = User(id=6, first_name="Carol", last_name=None, username="c")
        block = convert.reply_block(ns(raw_text="hi", id=80), sender)
        assert block == {
            "sender_name": "Carol",
            "text": "hi",
            "message_id": "80",
            "sender_id": "6",
        }
        assert convert.reply_block(None, sender) is None


class TestExtensionRules:
    def test_the_extension_never_imports_the_core(self) -> None:
        forbidden = {"core", "plugins", "interface", "engines", "main"}
        for path in SOURCE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] not in forbidden, (
                        f"{path.name} imports {name}"
                    )

    def test_the_manifest_declares_what_the_code_calls(self) -> None:
        from core.extensions.manifest import load_manifest

        manifest = load_manifest(SOURCE / "synth-extension.toml")
        declared = set(manifest.permissions.capabilities)
        text = "".join(p.read_text(encoding="utf-8") for p in SOURCE.glob("*.py"))
        used = {
            "interfaces.register",
            "interfaces.inbound",
            "avatar",
            "calls",
            "ui.panel",
            "storage",
            "config",
            "stats",
            "log",
        }
        assert used <= declared
        for namespace, capability in (
            ("ctx.calls.", "calls"),
            ("ctx.panels.", "ui.panel"),
            ("ctx.storage.", "storage"),
        ):
            assert namespace not in text or capability in declared
        assert manifest.interface_ids == ("telegram",)
        assert manifest.provides.interfaces[0].actions == ("answer_call_telegram",)
