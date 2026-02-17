import unittest
from types import SimpleNamespace

import core.core_initializer as core_init
from plugins.message_plugin import MessagePlugin


class FakeTelegramInterface:
    def __init__(self):
        self.sent = []

    async def send_message(self, payload, original_message=None):
        # record payload exactly as received
        self.sent.append((payload, original_message))
        return None


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
