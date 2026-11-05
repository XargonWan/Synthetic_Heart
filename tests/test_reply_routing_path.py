"""The reply destination shown to the model must be a concrete, copyable path.

Regression guarded here: the shared reply-routing rule and the worked response
example used to contain the literal template token
``input.payload.current_chat.interface_path``. A literal-minded model copied it
verbatim into ``payload.interface_path``; that string parses to a *truthy* but
unregistered interface name, so ``_dispatch_send_message`` never reached its
reply-to-origin fallback, failed hard on an unregistered interface (marked
``unfixable``) and dropped the reply — while the turn's other actions (diary,
emotion) succeeded, so the exchange looked "generated but never delivered".

Two independent guards are covered:

* the renderer substitutes the turn's real path into the rule text and the
  worked example (and never leaves a copyable path-shaped token behind);
* the dispatcher ignores an explicit path that resolves to an unregistered
  interface and falls back to the originating interface.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.action_parser as ap
from core.prompt_instructions import ROUTE_CHAT, ROUTE_DELIVERY, build_instructions
from core.prompt_instructions.rules import (
    REPLY_PATH_FALLBACK_TEXT,
    REPLY_PATH_TOKEN,
)

TEMPLATE_TOKEN = "input.payload.current_chat.interface_path"
CONCRETE_PATH = "telegram_bot/-5293915984"


class TestRenderedRoutingPath:
    def test_concrete_path_is_rendered_into_the_rules(self) -> None:
        rendered = build_instructions(ROUTE_CHAT, reply_path=CONCRETE_PATH)
        assert CONCRETE_PATH in rendered
        assert TEMPLATE_TOKEN not in rendered
        assert REPLY_PATH_TOKEN not in rendered

    def test_concrete_path_reaches_the_worked_example(self) -> None:
        rendered = build_instructions(ROUTE_CHAT, reply_path=CONCRETE_PATH)
        # The example is the copyable payload the model imitates.
        assert f'"interface_path": "{CONCRETE_PATH}"' in rendered

    def test_missing_path_falls_back_without_a_copyable_token(self) -> None:
        rendered = build_instructions(ROUTE_CHAT)
        assert REPLY_PATH_FALLBACK_TEXT in rendered
        assert TEMPLATE_TOKEN not in rendered
        assert REPLY_PATH_TOKEN not in rendered

    def test_blank_path_is_treated_as_missing(self) -> None:
        rendered = build_instructions(ROUTE_CHAT, reply_path="   ")
        assert REPLY_PATH_FALLBACK_TEXT in rendered
        assert TEMPLATE_TOKEN not in rendered

    def test_delivery_route_still_receives_the_path(self) -> None:
        # ROUTE_DELIVERY keeps the routing rule (it sends a message too).
        rendered = build_instructions(ROUTE_DELIVERY, reply_path=CONCRETE_PATH)
        assert CONCRETE_PATH in rendered
        assert TEMPLATE_TOKEN not in rendered


class TestDispatchIgnoresUnregisteredExplicitPath:
    def _register(self, monkeypatch, name, iface):
        import core.core_initializer as ci

        monkeypatch.setitem(ci.INTERFACE_REGISTRY, name, iface)

    @pytest.mark.asyncio
    async def test_template_token_falls_back_to_origin(self, monkeypatch) -> None:
        sent: dict = {}

        class Iface:
            async def send_message(self, payload, original_message=None):
                sent["payload"] = payload
                return True

        self._register(monkeypatch, "telegram_bot", Iface())
        origin = SimpleNamespace(interface_path=CONCRETE_PATH)

        result = await ap._dispatch_send_message(
            {
                "type": "send_message",
                "payload": {"text": "hi", "interface_path": TEMPLATE_TOKEN},
            },
            context={},
            bot=None,
            original_message=origin,
        )

        assert result["ok"] is True, result
        assert sent["payload"]["interface_path"] == CONCRETE_PATH

    @pytest.mark.asyncio
    async def test_valid_explicit_path_still_wins(self, monkeypatch) -> None:
        sent: dict = {}

        class Iface:
            async def send_message(self, payload, original_message=None):
                sent["payload"] = payload
                return True

        self._register(monkeypatch, "telegram_bot", Iface())
        self._register(monkeypatch, "discord_bot", Iface())
        origin = SimpleNamespace(interface_path=CONCRETE_PATH)

        result = await ap._dispatch_send_message(
            {
                "type": "send_message",
                "payload": {"text": "hi", "interface_path": "discord_bot/42"},
            },
            context={},
            bot=None,
            original_message=origin,
        )

        assert result["ok"] is True, result
        assert sent["payload"]["interface_path"] == "discord_bot/42"

    @pytest.mark.asyncio
    async def test_unregistered_path_without_origin_still_fails(
        self, monkeypatch
    ) -> None:
        self._register(monkeypatch, "telegram_bot", SimpleNamespace())

        result = await ap._dispatch_send_message(
            {
                "type": "send_message",
                "payload": {"text": "hi", "interface_path": TEMPLATE_TOKEN},
            },
            context={},
            bot=None,
            original_message=None,
        )

        assert result["ok"] is False
        assert "required" in result["error"] or "not available" in result["error"]
