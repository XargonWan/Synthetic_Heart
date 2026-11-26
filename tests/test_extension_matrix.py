"""The Matrix extension against a fake ``nio`` client and a fake ``ctx``."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.extensions.manifest import load_manifest
from synth_sdk.paths import match_path

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "matrix"


def _load() -> tuple[Any, Any]:
    sys.path.insert(0, str(SOURCE))
    try:
        return importlib.import_module("convert"), importlib.import_module("ext_main")
    finally:
        sys.path.remove(str(SOURCE))
        for name in ("convert", "ext_main"):
            sys.modules.pop(name, None)


convert, ext_main = _load()


# ------------------------------------------------------------------------------ pure


class TestConvert:
    def test_usernames(self) -> None:
        assert convert.extract_username("@alice:matrix.org") == "alice"
        assert convert.extract_username("alice") == "alice"

    def test_settings_lists(self) -> None:
        assert convert.parse_rooms(" !a:x , ,#b:x ") == {"!a:x", "#b:x"}
        assert convert.parse_rooms("") == set() and convert.parse_rooms(None) == set()

    def test_a_rich_reply_fallback_is_split_into_quote_and_text(self) -> None:
        body = "> <@bob:hs> what is for dinner?\n> second line\n\npizza"
        assert convert.parse_reply_fallback(body) == (
            "pizza",
            "<@bob:hs> what is for dinner?\nsecond line",
        )
        for plain in ("just text", "", "> only a quote", "> q\n\n"):
            assert convert.parse_reply_fallback(plain) is None

    def test_the_quoted_author_is_read_structurally(self) -> None:
        assert convert.quoted_sender_from_fallback("<@bob:hs> hello") == "bob"
        assert convert.quoted_sender_from_fallback("* Bobby: hello") == "Bobby"
        assert convert.quoted_sender_from_fallback("hello") == "Unknown"
        assert convert.quoted_sender_from_fallback("") == "Unknown"

    def test_paths(self) -> None:
        assert convert.build_path("!r:hs") == "matrix/!r:hs"
        assert convert.build_path("!r:hs", "$ev") == "matrix/!r:hs/$ev"
        assert convert.split_path("matrix/!r:hs/$ev") == ("!r:hs", "$ev")
        assert convert.split_path("matrix/!r:hs") == ("!r:hs", None)
        for bad in (None, "", "telegram_bot/1", "matrix", "matrix/a/b/c"):
            assert convert.split_path(bad) is None

    def test_the_declared_patterns_accept_what_the_extension_builds(self) -> None:
        patterns = (
            load_manifest(SOURCE / "synth-extension.toml").provides.interfaces[0].paths
        )
        assert match_path(convert.build_path("!r:hs"), patterns)
        assert match_path(convert.build_path("!r:hs", "$ev"), patterns)

    def test_relations_for_a_thread_and_a_reply(self) -> None:
        assert convert.relates_to(None, None) == {}
        assert convert.relates_to("$t", None) == {
            "event_id": "$t",
            "rel_type": "m.thread",
            "is_falling_back": True,
        }
        both = convert.relates_to("$t", "$r")
        assert (
            both["m.in_reply_to"] == {"event_id": "$r"}
            and both["rel_type"] == "m.thread"
        )
        assert convert.relates_to(None, "$r") == {"m.in_reply_to": {"event_id": "$r"}}

    def test_media_kinds_and_room_size(self) -> None:
        assert [
            convert.msgtype_for(k) for k in ("image", "audio", "video", "document", "x")
        ] == [
            "m.image",
            "m.audio",
            "m.video",
            "m.file",
            "m.file",
        ]
        assert convert.chat_type(2) == "private" and convert.chat_type(3) == "group"
        assert convert.chat_type(None) == "group"

    def test_policies(self) -> None:
        trusted = {"@t:hs"}
        assert convert.private_allowed("allow_all", "@x:hs", None, set())
        assert convert.private_allowed("trainer_only", "@boss:hs", "@boss:hs", set())
        assert convert.private_allowed("trainer_only", "@t:hs", "@boss:hs", trusted)
        assert not convert.private_allowed("trainer_only", "@x:hs", "@boss:hs", trusted)
        assert not convert.private_allowed("trainer_only", "", None, set())
        assert convert.invite_allowed("allow_all", "@x:hs", None, set())
        assert not convert.invite_allowed("trainer_only", None, "@boss:hs", set())
        assert convert.room_allowed(set(), "!r:hs", None)
        assert convert.room_allowed({"#a:hs"}, "!r:hs", "#a:hs")
        assert not convert.room_allowed({"#a:hs"}, "!r:hs", None)


# ----------------------------------------------------------------------------- bridge


class FakeClient:
    def __init__(self) -> None:
        self.access_token = "tok"
        self.user_id = "@synth:hs"
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[dict[str, Any]] = []
        self.joined: list[str] = []
        self.typing: list[tuple[str, bool]] = []
        self.rooms: dict[str, Any] = {}
        self.send_ok = True
        self.avatar_uri: str | None = None

    async def room_send(
        self, room_id: str, message_type: str, content: dict[str, Any]
    ) -> Any:
        self.sent.append((room_id, content))
        return SimpleNamespace(event_id="$sent") if self.send_ok else SimpleNamespace()

    async def room_resolve_alias(self, alias: str) -> Any:
        return SimpleNamespace(room_id="!resolved:hs")

    async def join(self, room_id: str) -> None:
        self.joined.append(room_id)

    async def room_typing(self, room_id: str, typing: bool, timeout: int) -> None:
        self.typing.append((room_id, typing))

    async def upload(
        self, provider: Any, content_type: str, filename: str, filesize: int
    ) -> Any:
        self.uploads.append({"type": content_type, "name": filename, "size": filesize})
        return SimpleNamespace(content_uri="mxc://hs/abc"), None


class FakeCtx:
    def __init__(
        self, config: dict[str, Any] | None = None, trainer: str | None = "@boss:hs"
    ) -> None:
        self.values = config or {}
        self.inbound: list[dict[str, Any]] = []
        self.counts: list[str] = []
        self.logs: list[str] = []
        ctx = self

        class Interfaces:
            async def inbound(
                self, interface_id: str, path: str, **kw: Any
            ) -> dict[str, Any]:
                ctx.inbound.append({"interface_id": interface_id, "path": path, **kw})
                return {"queued": True}

            async def trainer(self, interface_id: str) -> str | None:
                return trainer

        class Config:
            async def get(self, key: str) -> Any:
                return ctx.values.get(key)

        self.interfaces, self.config = Interfaces(), Config()
        self.stats = SimpleNamespace(
            incr=lambda name, n=1, **kw: ctx.counts.append(name)
        )
        self.log = SimpleNamespace(
            debug=lambda m: ctx.logs.append(m),
            info=lambda m: ctx.logs.append(m),
            warning=lambda m: ctx.logs.append(m),
            error=lambda m: ctx.logs.append(m),
        )


def bridge(
    config: dict[str, Any] | None = None, trainer: str | None = "@boss:hs"
) -> tuple[Any, FakeClient, FakeCtx]:
    settings = {"MATRIX_USER": "@synth:hs", **(config or {})}
    ctx, client = FakeCtx(settings, trainer), FakeClient()
    return ext_main.Bridge(ctx, client, settings), client, ctx


def room(
    room_id: str = "!r:hs",
    members: int = 5,
    alias: str | None = None,
    name: str | None = "Room",
) -> Any:
    return SimpleNamespace(
        room_id=room_id, member_count=members, canonical_alias=alias, display_name=name
    )


def message(body: str, sender: str = "@alice:hs", **source: Any) -> Any:
    return SimpleNamespace(
        sender=sender,
        body=body,
        event_id="$e1",
        server_timestamp=1_700_000_000_000,
        source={"content": source} if source else {},
    )


class TestInbound:
    @pytest.mark.asyncio
    async def test_a_room_message_becomes_an_inbound_message_with_its_chat(
        self,
    ) -> None:
        b, _client, ctx = bridge()
        await b.on_message(room(alias="#lobby:hs"), message("hello there"))
        (sent,) = ctx.inbound
        assert sent["path"] == "matrix/!r:hs" and sent["interface_id"] == "matrix"
        assert (sent["text"], sent["sender_id"], sent["sender_name"]) == (
            "hello there",
            "@alice:hs",
            "alice",
        )
        assert (sent["chat_id"], sent["chat_type"], sent["chat_title"]) == (
            "!r:hs",
            "group",
            "Room",
        )
        assert sent["chat_username"] == "#lobby:hs" and sent["thread_id"] is None
        assert sent["timestamp"].startswith("2023-11-14T22:13:20")
        assert sent["human_count"] == 5 and sent["message_id"] == "$e1"

    @pytest.mark.asyncio
    async def test_a_thread_message_keeps_its_thread_in_the_path(self) -> None:
        b, _c, ctx = bridge()
        await b.on_message(
            room(),
            message(
                "in thread",
                **{"m.relates_to": {"rel_type": "m.thread", "event_id": "$t"}},
            ),
        )
        assert (
            ctx.inbound[0]["path"] == "matrix/!r:hs/$t"
            and ctx.inbound[0]["thread_id"] == "$t"
        )

    @pytest.mark.asyncio
    async def test_a_plain_reply_is_not_mistaken_for_a_thread(self) -> None:
        b, _c, ctx = bridge()
        body = "> <@bob:hs> dinner?\n\npizza"
        await b.on_message(
            room(),
            message(body, **{"m.relates_to": {"m.in_reply_to": {"event_id": "$q"}}}),
        )
        (sent,) = ctx.inbound
        assert sent["path"] == "matrix/!r:hs" and sent["text"] == "pizza"
        assert sent["reply_to"] == {
            "sender_name": "bob",
            "text": "<@bob:hs> dinner?",
            "message_id": "$q",
        }

    @pytest.mark.asyncio
    async def test_own_empty_and_foreign_room_messages_are_ignored(self) -> None:
        b, _c, ctx = bridge({"MATRIX_ALLOWED_ROOMS": "!ok:hs"})
        await b.on_message(room("!ok:hs"), message("mine", sender="@synth:hs"))
        await b.on_message(room("!ok:hs"), message("   "))
        await b.on_message(room("!other:hs"), message("not allowed"))
        await b.on_message(room("!ok:hs"), message("fine"))
        assert [i["text"] for i in ctx.inbound] == ["fine"]

    @pytest.mark.asyncio
    async def test_private_rooms_follow_the_policy(self) -> None:
        b, _c, ctx = bridge({"MATRIX_TRUSTED_USERS": "@friend:hs"})
        dm = room("!dm:hs", members=2)
        await b.on_message(dm, message("hi", sender="@stranger:hs"))
        await b.on_message(dm, message("hi", sender="@friend:hs"))
        await b.on_message(dm, message("hi", sender="@boss:hs"))
        assert [i["sender_id"] for i in ctx.inbound] == ["@friend:hs", "@boss:hs"]
        assert all(i["chat_type"] == "private" for i in ctx.inbound)
        b2, _c2, ctx2 = bridge({"MATRIX_PRIVATE_MESSAGES": "allow_all"})
        await b2.on_message(dm, message("hi", sender="@stranger:hs"))
        assert len(ctx2.inbound) == 1

    @pytest.mark.asyncio
    async def test_a_failing_inbound_is_counted_not_raised(self) -> None:
        b, _c, ctx = bridge()

        async def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("core said no")

        ctx.interfaces.inbound = boom
        await b.on_message(room(), message("hi"))
        assert "errors" in ctx.counts


class TestInvites:
    @staticmethod
    def invite(sender: str) -> Any:
        return SimpleNamespace(membership="invite", sender=sender)

    @pytest.mark.asyncio
    async def test_the_trainer_and_trusted_users_are_joined_others_are_not(
        self,
    ) -> None:
        b, client, _ctx = bridge({"MATRIX_TRUSTED_USERS": "@friend:hs"})
        for who in ("@stranger:hs", "@friend:hs", "@boss:hs"):
            await b.on_invite(room(f"!from-{who}"), self.invite(who))
        assert client.joined == ["!from-@friend:hs", "!from-@boss:hs"]

    @pytest.mark.asyncio
    async def test_allow_all_and_auto_join_off(self) -> None:
        b, client, _ = bridge({"MATRIX_INVITE_POLICY": "allow_all"})
        await b.on_invite(room("!x:hs"), self.invite("@anyone:hs"))
        assert client.joined == ["!x:hs"]
        off, client2, _ = bridge(
            {"MATRIX_AUTO_JOIN": False, "MATRIX_INVITE_POLICY": "allow_all"}
        )
        await off.on_invite(room("!x:hs"), self.invite("@anyone:hs"))
        assert client2.joined == []
        await b.on_invite(
            room("!y:hs"), SimpleNamespace(membership="join", sender="@a:hs")
        )
        assert client.joined == ["!x:hs"]  # only invitations


class TestOutbound:
    @staticmethod
    def payload(**kw: Any) -> dict[str, Any]:
        return {"interface_path": "matrix/!r:hs", "text": "hi", **kw}

    @pytest.mark.asyncio
    async def test_text_goes_to_the_room_of_the_path(self) -> None:
        b, client, _ = bridge()
        assert await b.send("matrix", self.payload(), {}) == {"ok": True}
        assert client.sent == [("!r:hs", {"msgtype": "m.text", "body": "hi"})]

    @pytest.mark.asyncio
    async def test_a_thread_path_and_a_reply_are_related_in_the_message(self) -> None:
        b, client, _ = bridge()
        await b.send(
            "matrix", self.payload(interface_path="matrix/!r:hs/$t", reply_to="$q"), {}
        )
        content = client.sent[0][1]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$q"}

    @pytest.mark.asyncio
    async def test_a_reply_to_the_origin_message_quotes_it_when_nothing_else_is_named(
        self,
    ) -> None:
        b, client, _ = bridge()
        origin = {"interface_path": "matrix/!r:hs", "message_id": "$orig"}
        await b.send("matrix", {"text": "hi"}, origin)
        assert client.sent[0][1]["m.relates_to"] == {
            "m.in_reply_to": {"event_id": "$orig"}
        }
        # not when it goes to another room
        await b.send("matrix", self.payload(interface_path="matrix/!other:hs"), origin)
        assert "m.relates_to" not in client.sent[1][1]

    @pytest.mark.asyncio
    async def test_an_alias_is_resolved_and_bad_input_is_refused(self) -> None:
        b, client, _ = bridge()
        await b.send("matrix", self.payload(interface_path="matrix/#room:hs"), {})
        assert client.sent[0][0] == "!resolved:hs"
        assert (
            await b.send(
                "matrix", {"text": "x", "interface_path": "telegram_bot/1"}, {}
            )
        )["ok"] is False
        assert (await b.send("matrix", self.payload(text="  "), {}))[
            "error"
        ] == "nothing to send"

    @pytest.mark.asyncio
    async def test_an_error_response_is_a_failed_delivery(self) -> None:
        b, client, ctx = bridge()
        client.send_ok = False
        result = await b.send("matrix", self.payload(), {})
        assert result["ok"] is False and "errors" in ctx.counts

    @pytest.mark.asyncio
    async def test_files_are_uploaded_typed_and_the_caption_follows_once(
        self, tmp_path: Path
    ) -> None:
        b, client, _ = bridge()
        files = []
        for name, kind in (("a.png", "image"), ("b.pdf", "document")):
            path = tmp_path / name
            path.write_bytes(b"x" * 10)
            files.append(
                {
                    "path": str(path),
                    "name": name,
                    "mime_type": "application/x",
                    "kind": kind,
                }
            )
        result = await b.send(
            "matrix", self.payload(text="look", attachments=files), {}
        )
        assert result == {"ok": True}
        types = [c["msgtype"] for _, c in client.sent]
        assert types == ["m.image", "m.text", "m.file"]
        assert (
            client.sent[0][1]["url"] == "mxc://hs/abc"
            and client.sent[0][1]["body"] == "a.png"
        )
        assert client.sent[1][1]["body"] == "look"
        assert [u["size"] for u in client.uploads] == [10, 10]

    @pytest.mark.asyncio
    async def test_typing_names_and_the_avatar(self) -> None:
        b, client, ctx = bridge({"MATRIX_USE_SYNTH_AVATAR": True})
        await b.activity("matrix", "matrix/!r:hs/$t", "typing")
        await b.activity("matrix", "telegram_bot/1", "typing")
        assert client.typing == [("!r:hs", True)]
        client.rooms["!r:hs"] = room(name="Lobby")
        assert await b.resolve_names("matrix", "!r:hs", None) == {"chat_name": "Lobby"}
        assert await b.resolve_names("matrix", "!none:hs", None) == {}
        assert (
            await b.set_avatar("matrix", None, None, False) is False
        )  # no portable removal
        ctx.values["MATRIX_USE_SYNTH_AVATAR"] = False
        assert await b.set_avatar("matrix", b"png", "v1", False) is False


def test_the_manifest_asks_only_for_what_an_interface_needs() -> None:
    m = load_manifest(SOURCE / "synth-extension.toml")
    assert set(m.permissions.capabilities) == {
        "log",
        "config",
        "stats",
        "interfaces.register",
        "interfaces.inbound",
    }
    assert {c.key for c in m.config if c.sensitive} == {
        "MATRIX_PASSWORD",
        "MATRIX_ACCESS_TOKEN",
    }
