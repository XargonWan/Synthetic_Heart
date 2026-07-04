import unittest
from types import SimpleNamespace

import core.core_initializer as core_init
from plugins.message_plugin import MessagePlugin


class FakeTelegramInterface:
    def __init__(self):
        self.sent = []
        self.send_result = None

    async def send_message(self, payload, original_message=None):
        # record payload exactly as received
        self.sent.append((payload, original_message))
        return self.send_result


class TestMessagePluginThreadHandling(unittest.IsolatedAsyncioTestCase):
    async def test_uses_message_thread_id_when_payload_has_no_thread(self):
        """If action.payload.interface_path omits topic id, prefer original_message.message_thread_id."""
        # Patch registry (preserve original)
        orig_iface = core_init.INTERFACE_REGISTRY.get("telegram_bot")
        fake_iface = FakeTelegramInterface()
        core_init.INTERFACE_REGISTRY["telegram_bot"] = fake_iface

        try:
            plugin = MessagePlugin()

            action = {
                "type": "message_telegram_bot",
                "payload": {
                    "text": "Hey",
                    "interface_path": "telegram_bot/-1002646330049/",
                },
            }

            original_message = SimpleNamespace()
            original_message.chat_id = -1002646330049
            original_message.message_id = 50699
            # Telegram's native attribute (not 'thread_id')
            original_message.message_thread_id = 6
            original_message.from_user = SimpleNamespace(
                id=2115971192, username="Alessandra15204"
            )

            await plugin._handle_message_action(
                action, context={}, bot=None, original_message=original_message
            )

            # Ensure interface send_message was called once
            self.assertEqual(len(fake_iface.sent), 1)
            sent_payload, sent_original = fake_iface.sent[0]

            # thread_id must be preserved from original_message.message_thread_id
            self.assertIn("thread_id", sent_payload)
            self.assertEqual(int(sent_payload["thread_id"]), 6)

            # original_message should be forwarded to interface unmodified
            self.assertIs(sent_original, original_message)

        finally:
            # restore registry
            if orig_iface is None:
                core_init.INTERFACE_REGISTRY.pop("telegram_bot", None)
            else:
                core_init.INTERFACE_REGISTRY["telegram_bot"] = orig_iface

    async def test_raises_when_interface_reports_delivery_failure(self):
        orig_iface = core_init.INTERFACE_REGISTRY.get("telegram_bot")
        fake_iface = FakeTelegramInterface()
        fake_iface.send_result = False
        core_init.INTERFACE_REGISTRY["telegram_bot"] = fake_iface

        try:
            plugin = MessagePlugin()

            action = {
                "type": "message_telegram_bot",
                "payload": {
                    "text": "Hey",
                    "interface_path": "telegram_bot/12345",
                },
            }

            with self.assertRaises(RuntimeError):
                await plugin._handle_message_action(
                    action, context={}, bot=None, original_message=None
                )
        finally:
            if orig_iface is None:
                core_init.INTERFACE_REGISTRY.pop("telegram_bot", None)
            else:
                core_init.INTERFACE_REGISTRY["telegram_bot"] = orig_iface

    async def test_openai_compat_mirrors_reply_to_origin_chat(self):
        """When mirroring is active, a hallucinated interface_path is overridden
        with the originating chat instead of being delivered to a bogus chat."""
        orig_iface = core_init.INTERFACE_REGISTRY.get("telegram_bot")
        fake_iface = FakeTelegramInterface()
        core_init.INTERFACE_REGISTRY["telegram_bot"] = fake_iface
        try:
            plugin = MessagePlugin()

            # Force the openai_compat scoping decision on (the real check needs a
            # live cortex registry); this test covers the override behaviour.
            async def _always_mirror(context, original_message):
                return True

            plugin._should_mirror_origin_path = _always_mirror

            action = {
                "type": "message_telegram_bot",
                "payload": {
                    "text": "hi",
                    # hallucinated by a small local model
                    "interface_path": "/channels/main",
                },
            }
            original_message = SimpleNamespace(
                chat_id=5551234567, message_id=42, thread_id=None
            )

            await plugin._handle_message_action(
                action,
                context={"interface": "telegram_bot"},
                bot=None,
                original_message=original_message,
            )

            self.assertEqual(len(fake_iface.sent), 1)
            sent_payload, _ = fake_iface.sent[0]
            self.assertEqual(sent_payload["target"], 5551234567)
            self.assertEqual(sent_payload["interface_path"], "telegram_bot/5551234567")
        finally:
            if orig_iface is None:
                core_init.INTERFACE_REGISTRY.pop("telegram_bot", None)
            else:
                core_init.INTERFACE_REGISTRY["telegram_bot"] = orig_iface

    async def test_grillo_context_is_not_mirrored(self):
        """Grillo/outreach turns target a system-chosen chat — never mirrored."""
        plugin = MessagePlugin()
        decision = await plugin._should_mirror_origin_path(
            {"grillo_beat": True},
            SimpleNamespace(chat_id=123),
        )
        self.assertFalse(decision)
