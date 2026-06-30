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


class TestRootLevelActionRecovery(unittest.IsolatedAsyncioTestCase):
    """Regression for gemma-uncensored malformed responses where the last
    action leaks to root level instead of sitting inside the actions array:

        {"actions": [...], "type": "message_telegram_bot", "payload": {...}}

    Before the fix this caused the corrector to fire multiple times, each
    pass re-emitting a message_telegram_bot and producing quadruple texts.
    """

    @patch("core.action_parser.run_action", new_callable=AsyncMock)
    @patch("core.transport_layer.run_corrector_middleware", new_callable=AsyncMock)
    @patch("core.action_parser.get_supported_action_types")
    async def test_root_level_action_recovered_without_corrector(
        self,
        mock_supported: MagicMock,
        mock_corrector: AsyncMock,
        mock_run_action: AsyncMock,
    ) -> None:
        """Root-level type+payload must be folded in without triggering the corrector."""
        mock_supported.return_value = {"message_telegram_bot", "update_emotion_state"}
        mock_corrector.return_value = None
        mock_run_action.return_value = (True, None)

        msg = SimpleNamespace(chat_id=42, text="", from_cortex=True, thread_id=None)
        # Exact structure gemma-4-uncensored emits: message action is at root.
        text = (
            '{"actions": [{"type": "update_emotion_state", "payload": {"arousal": 9.5}}],'
            ' "type": "message_telegram_bot",'
            ' "payload": {"text": "Hello!", "interface_path": "telegram_bot/42"}}'
        )

        await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text=text,
            source="llm",
            context={"interface_path": "telegram_bot/42", "interface": "telegram_bot"},
        )

        mock_corrector.assert_not_called()
        self.assertEqual(mock_run_action.call_count, 2)
        executed_types = {c.args[0].get("type") for c in mock_run_action.call_args_list}
        self.assertIn("update_emotion_state", executed_types)
        self.assertIn("message_telegram_bot", executed_types)


class TestOrphanedActionLevelKeys(unittest.IsolatedAsyncioTestCase):
    """Regression for Gemma-4 placing routing keys (interface_path, chat_name,
    reply_to_message_id) at the action dict level instead of inside payload:

        {"type": "message_telegram_bot",
         "payload": {"text": "Hi"},
         "interface_path": "telegram_bot/123",
         "reply_to_message_id": "456"}

    The orphaned keys must be silently merged into payload so the message is
    routed correctly, without triggering the corrector.
    """

    @patch("core.action_parser.run_action", new_callable=AsyncMock)
    @patch("core.transport_layer.run_corrector_middleware", new_callable=AsyncMock)
    @patch("core.action_parser.get_supported_action_types")
    async def test_orphaned_routing_keys_merged_into_payload(
        self,
        mock_supported: MagicMock,
        mock_corrector: AsyncMock,
        mock_run_action: AsyncMock,
    ) -> None:
        """interface_path / reply_to_message_id at action level must be folded
        into payload without triggering the corrector."""
        mock_supported.return_value = {"message_telegram_bot"}
        mock_corrector.return_value = None
        mock_run_action.return_value = (True, None)

        msg = SimpleNamespace(chat_id=42, text="", from_cortex=True, thread_id=None)
        text = (
            '{"actions": [{"type": "message_telegram_bot",'
            ' "payload": {"text": "Come here and get cozy"},'
            ' "interface_path": "telegram_bot/5208932647",'
            ' "chat_name": "Scar",'
            ' "reply_to_message_id": "1517052647"}]}'
        )

        await message_chain.handle_incoming_message(
            bot=None,
            message=msg,
            text=text,
            source="llm",
            context={
                "interface_path": "telegram_bot/5208932647",
                "interface": "telegram_bot",
            },
        )

        mock_corrector.assert_not_called()
        mock_run_action.assert_called_once()
        executed = mock_run_action.call_args[0][0]
        self.assertEqual(executed.get("type"), "message_telegram_bot")
        payload = executed.get("payload", {})
        self.assertEqual(payload.get("text"), "Come here and get cozy")
        self.assertEqual(payload.get("interface_path"), "telegram_bot/5208932647")
        self.assertEqual(payload.get("chat_name"), "Scar")
        # _normalize_payload converts numeric strings to int during validate_action
        self.assertEqual(payload.get("reply_to_message_id"), 1517052647)


if __name__ == "__main__":
    unittest.main()
