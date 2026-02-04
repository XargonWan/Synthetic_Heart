import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import message_chain


class TestMessageChain(unittest.TestCase):
    @patch("core.transport_layer.run_corrector_middleware")
    async def test_system_json_error_skips_corrector(self, mock_corrector):
        """System messages of type 'error' should be blocked without correction."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_llm=False)
        result = await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text='{"system_message": {"type": "error", "message": "fail"}}',
            source="interface",
        )

        self.assertEqual(result, message_chain.BLOCKED)
        mock_corrector.assert_not_called()

    @patch("core.transport_layer.run_corrector_middleware")
    async def test_system_json_forwarded_without_corrector(self, mock_corrector):
        """Event/output system messages should be forwarded without invoking the corrector."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_llm=False)

        for sm_type in ["event", "output"]:
            with self.subTest(sm_type=sm_type):
                result = await message_chain.handle_incoming_message(
                    bot=None,
                    message=msg,
                    text=f'{{"system_message": {{"type": "{sm_type}", "message": "ok"}}}}',
                    source="interface",
                )

                self.assertEqual(result, message_chain.FORWARD_AS_TEXT)
                mock_corrector.assert_not_called()

    @patch("core.transport_layer.run_corrector_middleware")
    async def test_non_llm_invalid_json_skips_corrector(self, mock_corrector):
        """Invalid JSON from non-LLM sources should bypass the corrector."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_llm=False)
        result = await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text="{invalid}",
            source="interface",
        )

        self.assertEqual(result, message_chain.FORWARD_AS_TEXT)
        mock_corrector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
