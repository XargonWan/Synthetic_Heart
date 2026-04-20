import json
import sys
import types

# Prevent heavy undetected_chromedriver import side-effects during tests by
# inserting a lightweight dummy module before importing SeleniumLLMBase
sys.modules.setdefault(
    "undetected_chromedriver", types.ModuleType("undetected_chromedriver")
)

from cortex.selenium_engine.selenium_llm_base import SeleniumLLMBase


def test_split_includes_current_chat_history():
    inst = SeleniumLLMBase()

    # Build a sample prompt containing context with history_current_chat
    prompt = {
        "context": {
            "memories": [],
            "history_current_chat": ['[18/12/25:1200] Alice: "Hi"'],
        },
        "input": {"text": "hello"},
        "instructions": "do stuff",
    }

    prompt_text = "SYSTEM\n---\n" + json.dumps(prompt)

    part1, part2 = inst._split_prompt_text_into_parts(prompt_text)

    # PART1 should contain the history_current_chat payload
    assert "[INTERNAL-PART1]" in part1
    # Parse payload
    payload = json.loads(part1.split("\n\n", 1)[1])
    assert "history_current_chat" in payload
    assert payload["history_current_chat"] == prompt["context"]["history_current_chat"]

    # PART2 should have context.history_current_chat emptied
    parsed_part2 = json.loads(part2)
    ctx = parsed_part2.get("context", {})
    assert ctx.get("history_current_chat", []) == []
