#!/usr/bin/env python3
"""Tests for core/interface_path_utils.py routing helpers."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import core_initializer
from core.interface_path_utils import resolve_registered_interface_path


class TestResolveRegisteredInterfacePath(unittest.TestCase):
    """resolve_registered_interface_path must never route to a phantom interface.

    Models occasionally hallucinate an interface_path prefix (e.g.
    'em_chat_bridge/...') that no registered interface can deliver to; the
    helper must redirect such replies to the chat the turn arrived in.
    """

    def _registry(self, *names: str) -> dict:
        return {name: object() for name in names}

    def test_registered_path_is_kept(self):
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("telegram_bot")
        ):
            self.assertEqual(
                resolve_registered_interface_path("telegram_bot/5208932647"),
                "telegram_bot/5208932647",
            )

    def test_registered_prefix_with_different_chat_is_kept(self):
        """A VALID registered interface is trusted even for another chat."""
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("telegram_bot")
        ):
            self.assertEqual(
                resolve_registered_interface_path(
                    "telegram_bot/999",
                    context={"interface_path": "telegram_bot/1"},
                ),
                "telegram_bot/999",
            )

    def test_hallucinated_prefix_falls_back_to_context_origin(self):
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("telegram_bot")
        ):
            self.assertEqual(
                resolve_registered_interface_path(
                    "em_chat_bridge/5208932647",
                    context={"interface_path": "telegram_bot/5208932647"},
                ),
                "telegram_bot/5208932647",
            )

    def test_hallucinated_prefix_falls_back_to_original_message(self):
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("discord_bot")
        ):
            msg = SimpleNamespace(interface_path="discord_bot/123")
            self.assertEqual(
                resolve_registered_interface_path(
                    "em_chat_bridge/5208932647", original_message=msg
                ),
                "discord_bot/123",
            )

    def test_hallucinated_prefix_without_origin_returns_none(self):
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("telegram_bot")
        ):
            self.assertIsNone(
                resolve_registered_interface_path("em_chat_bridge/x", context={})
            )

    def test_none_input_returns_none(self):
        with patch.object(
            core_initializer, "INTERFACE_REGISTRY", self._registry("telegram_bot")
        ):
            self.assertIsNone(
                resolve_registered_interface_path(
                    None, context={"interface_path": "telegram_bot/1"}
                )
            )

    def test_empty_registry_keeps_given_path(self):
        """An unpopulated registry (early startup / offline tests) cannot
        prove a prefix is hallucinated — keep the given path unchanged."""
        with patch.object(core_initializer, "INTERFACE_REGISTRY", {}):
            self.assertEqual(
                resolve_registered_interface_path(
                    "synth_webui/sess1",
                    context={"interface_path": "telegram_bot/1"},
                ),
                "synth_webui/sess1",
            )
            self.assertEqual(
                resolve_registered_interface_path(
                    "em_chat_bridge/x",
                    context={"interface_path": "telegram_bot/1"},
                ),
                "em_chat_bridge/x",
            )


if __name__ == "__main__":
    unittest.main()
