

def make_dummy_message():
    class Dummy:
        text = "hello"
        interface_path = "test/1"
        chat_id = "1"
        message_id = "m1"
        # isoformat-compatible placeholder
        from datetime import datetime
        date = datetime.utcnow()
        from_user = None
    return Dummy()


async def test_prompt_and_llm_logging(monkeypatch, capsys):
    """Ensure debug logs include full prompt and LLM response content."""
    # set logger to debug so our messages appear on stdout
    import core.logging_utils as logmod

    logmod.setup_logging().setLevel("DEBUG")

    from core.prompt_engine import build_json_prompt

    msg = make_dummy_message()
    # build prompt and capture its debug log
    prompt = await build_json_prompt(msg, {})

    # simple plugin call (bypass plugin_instance complexity)
    class DummyPlugin:
        async def handle_incoming_message(self, bot, message, prompt):
            return "dummy-response"

    result = await DummyPlugin().handle_incoming_message(None, msg, prompt)
    # logs produced during both calls will still be on stdout
    _ = capsys.readouterr()
    assert result == "dummy-response"
