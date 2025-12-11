import asyncio
from types import SimpleNamespace

import llm_engines_dev.openai_chatgpt as openai_mod


def test_openai_includes_instructions_verbose(monkeypatch):
    captured = {}

    def fake_create(model, messages):
        captured['model'] = model
        captured['messages'] = messages

        class DummyResp:
            def __init__(self):
                self.choices = [SimpleNamespace(message=SimpleNamespace(content="ok"))]

        return DummyResp()

    # Patch the ChatCompletion.create call
    monkeypatch.setattr(openai_mod.openai.ChatCompletion, "create", fake_create)

    plugin = openai_mod.OpenAIPlugin()

    # Build a prompt that contains instructions_verbose
    prompt = {
        "input": {"interface": "telegram"},
        "context": [],
        "instructions": "short instructions",
        "instructions_verbose": "VERBOSE CHAT INSTRUCTION: be concise",
    }

    # Run the async generate_response
    response = asyncio.run(plugin.generate_response(prompt))

    # Ensure our fake_create was called and messages include the verbose instruction
    assert 'messages' in captured
    assert captured['messages'][0]['role'] == 'system'
    assert 'VERBOSE CHAT INSTRUCTION' in captured['messages'][0]['content']
    # Also ensure the plugin returned the stubbed response content
    assert response == "ok"
