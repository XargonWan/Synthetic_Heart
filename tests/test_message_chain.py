import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core import message_chain


class TestMessageChain(unittest.IsolatedAsyncioTestCase):
    @patch("core.transport_layer.run_corrector_middleware")
    async def test_system_json_error_skips_corrector(self, mock_corrector):
        """System messages of type 'error' should be blocked without correction."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_cortex=False)
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
        """System messages from non-LLM source are always blocked without correction."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_cortex=False)

        for sm_type in ["event", "output"]:
            with self.subTest(sm_type=sm_type):
                result = await message_chain.handle_incoming_message(
                    bot=None,
                    message=msg,
                    text=f'{{"system_message": {{"type": "{sm_type}", "message": "ok"}}}}',
                    source="interface",
                )

                self.assertEqual(result, message_chain.BLOCKED)
                mock_corrector.assert_not_called()

    @patch("core.transport_layer.run_corrector_middleware")
    async def test_non_llm_invalid_json_skips_corrector(self, mock_corrector):
        """Invalid JSON from non-LLM sources is blocked without invoking the corrector."""
        mock_corrector.return_value = "{}"

        msg = SimpleNamespace(chat_id=123, text="", from_cortex=False)
        result = await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text="{invalid}",
            source="interface",
        )

        self.assertEqual(result, message_chain.BLOCKED)
        mock_corrector.assert_not_called()


class TestGenericActionTypeNormalization(unittest.IsolatedAsyncioTestCase):
    """Regression tests for generic message type normalisation (text/reply/response)
    and graceful dropping of unsupported side-effect actions."""

    async def _run_llm_message(self, actions_json: str, *, corrector_mock: MagicMock):
        """Helper: run handle_incoming_message as if coming from cortex/LLM."""
        msg = SimpleNamespace(
            chat_id="webui_default",
            text="",
            from_cortex=True,
            thread_id=None,
        )
        text = f'{{"actions": {actions_json}}}'
        return await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text=text,
            source="llm",
            context={
                "interface_path": "synth_webui/webui_default",
                "interface": "synth_webui",
            },
        )

    @patch("core.action_parser.run_action", new_callable=AsyncMock)
    @patch("core.transport_layer.run_corrector_middleware", new_callable=AsyncMock)
    @patch("core.action_parser.get_supported_action_types")
    async def test_text_type_normalised_to_webui_no_corrector(
        self, mock_supported, mock_corrector, mock_run_action
    ):
        """LLM response with 'text' action type must be normalised to
        'message_synth_webui' without triggering the corrector at all."""
        mock_supported.return_value = {"message_synth_webui"}
        mock_corrector.return_value = None
        mock_run_action.return_value = (True, None)

        actions_json = '[{"type": "text", "text": "Ciao Xargon!"}]'
        await self._run_llm_message(actions_json, corrector_mock=mock_corrector)

        mock_corrector.assert_not_called()
        # The action passed to run_action should be the normalised type
        if mock_run_action.called:
            called_action = mock_run_action.call_args[0][0]
            self.assertEqual(called_action.get("type"), "message_synth_webui")

    @patch("core.action_parser.run_action", new_callable=AsyncMock)
    @patch("core.transport_layer.run_corrector_middleware", new_callable=AsyncMock)
    @patch("core.action_parser.get_supported_action_types")
    async def test_unsupported_side_effects_dropped_without_corrector(
        self, mock_supported, mock_corrector, mock_run_action
    ):
        """When the LLM returns a valid message action mixed with unsupported
        side-effect types, the unsupported ones must be silently dropped and
        the corrector must NOT be triggered."""
        mock_supported.return_value = {"message_synth_webui"}
        mock_corrector.return_value = None
        mock_run_action.return_value = (True, None)

        actions_json = (
            "["
            '{"type": "text", "text": "Hello"},'
            '{"type": "interaction_summary", "payload": {"summary": "..."}},'
            '{"type": "animation_state", "payload": {"state": "neutral"}}'
            "]"
        )
        await self._run_llm_message(actions_json, corrector_mock=mock_corrector)

        mock_corrector.assert_not_called()

    @patch("core.action_parser.run_action", new_callable=AsyncMock)
    @patch("core.transport_layer.run_corrector_middleware", new_callable=AsyncMock)
    @patch("core.action_parser.get_supported_action_types")
    async def test_all_unsupported_still_triggers_corrector(
        self, mock_supported, mock_corrector, mock_run_action
    ):
        """When ALL returned actions are unsupported, the corrector must still
        be invoked so the LLM gets a chance to fix its output."""
        mock_supported.return_value = {"message_synth_webui"}
        mock_corrector.return_value = None
        mock_run_action.return_value = (True, None)

        actions_json = '[{"type": "unknown_action_xyz", "payload": {}}]'
        await self._run_llm_message(actions_json, corrector_mock=mock_corrector)

        mock_corrector.assert_called_once()


if __name__ == "__main__":
    unittest.main()
