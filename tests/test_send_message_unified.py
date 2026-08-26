"""Tests for the unified ``send_message`` action.

Covers:

* the ``one_of_groups`` OR validator extension;
* the canonical schema in ``core.message_registry``;
* central capability-drop computation;
* the ``_dispatch_send_message`` destination resolution contract
  (explicit path > original_message fallback > structured failure);
* the recent-drops memory and prompt block rendering.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.action_parser as ap
from core.capability_drops import (
    MAX_DROPS_PER_TURN,
    collect_capability_drops,
    get_recent_drops,
    make_drop,
    remember_drops,
    render_capability_drops_block,
)
from core.message_registry import (
    SEND_MESSAGE_ACTION,
    get_send_message_schema,
    resolve_capability_drops,
)
from core.validation_registry import ValidationRule


class TestOneOfGroups:
    def test_group_satisfied_by_first_field(self) -> None:
        rule = ValidationRule(
            action_type="send_message", one_of_groups=[["text", "media"]]
        )
        assert rule.validate({"text": "hi"}) == []

    def test_group_satisfied_by_second_field(self) -> None:
        rule = ValidationRule(
            action_type="send_message", one_of_groups=[["text", "media"]]
        )
        assert rule.validate({"media": ["/app/x.png"]}) == []

    def test_group_violation(self) -> None:
        rule = ValidationRule(
            action_type="send_message", one_of_groups=[["text", "media"]]
        )
        errors = rule.validate({})
        assert any("At least one of [text, media]" in e for e in errors)

    def test_empty_string_does_not_count(self) -> None:
        rule = ValidationRule(
            action_type="send_message", one_of_groups=[["text", "media"]]
        )
        errors = rule.validate({"text": ""})
        assert len(errors) == 1

    def test_and_plus_or(self) -> None:
        rule = ValidationRule(
            action_type="x",
            required_fields=["a"],
            one_of_groups=[["b", "c"]],
        )
        assert rule.validate({"a": 1, "c": 2}) == []
        errors = rule.validate({"c": 2})
        assert any("Missing required field 'a'" in e for e in errors)


class TestSendMessageSchema:
    def test_schema_shape(self) -> None:
        schema = get_send_message_schema(["telegram_bot"])
        assert schema["required_fields"] == []
        assert schema["one_of_groups"] == [["text", "media"]]
        assert "interface_path" in schema["optional_fields"]
        assert "send_as_voice" in schema["optional_fields"]
        assert "reply_to" in schema["optional_fields"]
        assert "telegram_bot" in schema["description"]

    def test_schema_has_no_external_effects(self) -> None:
        """A chat reply is a PURE message action: it must never declare
        external_effects, or the agent router classifies every reply as a
        tool call and routes whole conversations to the Agent Lane
        (regression: live incident 2026-08-26 13:05 — a plain reply batch
        was routed agentic, the broken agent engine failed, and the user
        received no answer at all)."""
        from core.agent_router import _is_tool_call

        schema = get_send_message_schema()
        assert not schema.get("external_effects")
        assert _is_tool_call(SEND_MESSAGE_ACTION) is False

    def test_action_name(self) -> None:
        assert SEND_MESSAGE_ACTION == "send_message"


class TestResolveCapabilityDrops:
    def test_voice_drop_without_send_voice(self) -> None:
        iface = SimpleNamespace(send_message=lambda p: True)
        drops = resolve_capability_drops(iface, {"send_as_voice": True}, "matrix")
        assert len(drops) == 1
        assert drops[0]["feature"] == "send_as_voice"

    def test_no_drops_when_supported(self) -> None:
        class Iface:
            def send_message(self, payload):
                return True

            def send_voice(self, payload):
                return True

            def send_file(self, payload):
                return True

        drops = resolve_capability_drops(
            Iface(),
            {"send_as_voice": True, "media": ["/app/a.ogg"], "reply_to": 5},
            "tg",
        )
        assert drops == []

    def test_media_dropped_without_send_file(self) -> None:
        iface = SimpleNamespace(send_message=lambda p: True)
        drops = resolve_capability_drops(
            iface, {"media": ["/app/a.png"], "reply_to": 3}, "srv"
        )
        assert [d["feature"] for d in drops] == ["media"]

    def test_absent_features_never_dropped(self) -> None:
        iface = SimpleNamespace(send_message=lambda p: True)
        assert resolve_capability_drops(iface, {"text": "hi"}, "x") == []


class TestCapabilityDropAggregation:
    def test_collect_from_results(self) -> None:
        results = [
            {
                "ok": True,
                "capability_drops": [
                    make_drop("send_as_voice", "no voice", "matrix"),
                    make_drop("send_as_voice", "no voice", "matrix"),  # dup
                ],
            },
            None,
            {"ok": True},
        ]
        drops = collect_capability_drops(results)
        assert len(drops) == 1
        assert drops[0]["feature"] == "send_as_voice"

    def test_collect_caps_per_turn(self) -> None:
        results = [
            {
                "capability_drops": [
                    make_drop(f"f{i}", "r", "x") for i in range(MAX_DROPS_PER_TURN + 2)
                ]
            }
        ]
        assert len(collect_capability_drops(results)) == MAX_DROPS_PER_TURN

    def test_render_block(self) -> None:
        block = render_capability_drops_block(
            [make_drop("send_as_voice", "unsupported", "matrix")]
        )
        assert "CAPABILITY DROPS" in block
        assert "send_as_voice" in block
        assert "`matrix`" in block

    def test_render_empty(self) -> None:
        assert render_capability_drops_block([]) == ""

    def test_remember_and_consume(self) -> None:
        remember_drops("telegram_bot/123", [make_drop("media", "no files", "tg")])
        drops = get_recent_drops("telegram_bot/123")
        assert len(drops) == 1
        # Consumed on read
        assert get_recent_drops("telegram_bot/123") == []


class TestDispatchSendMessage:
    def _register(self, monkeypatch, name, iface):
        import core.core_initializer as ci

        monkeypatch.setitem(ci.INTERFACE_REGISTRY, name, iface)

    @pytest.mark.asyncio
    async def test_spontaneous_without_path_rejected(self, monkeypatch) -> None:
        self._register(monkeypatch, "tg_bot", SimpleNamespace())
        result = await ap._dispatch_send_message(
            {"type": "send_message", "payload": {"text": "hi"}},
            context={},
            bot=None,
            original_message=None,
        )
        assert result["ok"] is False
        assert "interface_path" in result["error"]

    @pytest.mark.asyncio
    async def test_explicit_path_overrides_origin(self, monkeypatch) -> None:
        sent: dict = {}

        class Iface:
            async def send_message(self, payload, original_message=None):
                sent["payload"] = payload
                return True

        self._register(monkeypatch, "tg_bot", Iface())
        origin = SimpleNamespace(interface_path="tg_bot/1")
        result = await ap._dispatch_send_message(
            {
                "type": "send_message",
                "payload": {"text": "hi", "interface_path": "tg_bot/2"},
            },
            context={},
            bot=None,
            original_message=origin,
        )
        assert result["ok"] is True
        assert sent["payload"]["interface_path"] == "tg_bot/2"

    @pytest.mark.asyncio
    async def test_origin_fallback_when_path_missing(self, monkeypatch) -> None:
        sent: dict = {}

        class Iface:
            async def send_message(self, payload, original_message=None):
                sent["payload"] = payload
                return True

        self._register(monkeypatch, "tg_bot", Iface())
        origin = SimpleNamespace(interface_path="tg_bot/77")
        result = await ap._dispatch_send_message(
            {"type": "send_message", "payload": {"text": "hello"}},
            context={},
            bot=None,
            original_message=origin,
        )
        assert result["ok"] is True
        assert sent["payload"]["interface_path"] == "tg_bot/77"

    @pytest.mark.asyncio
    async def test_unsupported_media_stripped_and_reported(self, monkeypatch) -> None:
        sent: dict = {}

        class Iface:
            async def send_message(self, payload, original_message=None):
                sent["payload"] = payload
                return True

        self._register(monkeypatch, "txt_only", Iface())
        result = await ap._dispatch_send_message(
            {
                "type": "send_message",
                "payload": {
                    "text": "pic",
                    "interface_path": "txt_only/9",
                    "media": ["/app/data/pic.png"],
                },
            },
            context={},
            bot=None,
            original_message=None,
        )
        # txt_only has no send_file/send_voice methods and no stored caps;
        # media must be stripped before delivery and reported as a drop.
        assert result["ok"] is True
        assert "media" not in sent["payload"]
        drop_features = {d["feature"] for d in result.get("capability_drops", [])}
        assert "media" in drop_features
