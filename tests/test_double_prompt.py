import json

from core.selenium_llm_base import SeleniumLLMBase


class DummySelenium(SeleniumLLMBase):
    """Lightweight test subclass that avoids driver usage.

    We'll override _execute_complete_workflow in test to simulate responses.
    """

    def __init__(self):
        # Call base __init__ to set config vars and flags
        super().__init__(notify_fn=None, config={})


def test_should_double_prompt_trigger():
    inst = DummySelenium()
    # Make the model limits small so prompt qualifies, but test plain text DOES NOT trigger split
    inst.model_limits_map = {"default": 10}
    inst.default_model = "default"

    long_prompt = "x" * 50
    # Plain text should not trigger double-prompt because there's no extractable context
    assert inst._should_double_prompt(long_prompt) is False


def test_should_double_prompt_trigger_json_context():
    inst = DummySelenium()
    inst.model_limits_map = {"default": 10}
    inst.default_model = "default"

    # Build a fake JSON prompt with a context object so splitting should trigger
    payload = {
        "context": {
            "chat_history": ["m" * 20 for _ in range(5)],
            "memories": ["mm" * 30 for _ in range(4)],
        },
        "instructions": "do something big",
    }
    prompt = "system\n---\n" + json.dumps(payload)

    assert inst._should_double_prompt(prompt) is True


def test_should_not_double_prompt_when_disabled():
    inst = DummySelenium()
    inst.SPLIT_PROMPT_VAR = False
    inst.model_limits_map = {"default": 5}
    inst.default_model = "default"

    assert inst._should_double_prompt("1234567890") is False


def test_split_prompt_json_context():
    inst = DummySelenium()
    # Build a JSON-like prompt with context
    base = {
        "context": {"memories": ["m1", "m2"], "chat_history": ["a", "b"]},
        "instructions": "do something",
    }
    prompt = "system\n---\n" + json.dumps(base)
    p1, p2 = inst._split_prompt_text_into_parts(prompt)

    # PART1 should contain only the extracted keys directly (no 'context' wrapper)
    p1_json = json.loads(p1.split("\n\n", 1)[1])
    assert "memories" in p1_json
    assert "chat_history" in p1_json

    # PART2 should have context emptied
    parsed_p2 = json.loads(p2)
    ctx = parsed_p2.get("context", {})
    assert ctx.get("chat_history", []) == []
    assert ctx.get("memories", []) == []


def test_split_prompt_avoids_user_message_in_part1():
    """Simulate a JSON prompt where the user's message exists outside the context object.

    Ensure PART1 contains only context (chat_history and ai_diary) and not the user message text.
    """
    inst = DummySelenium()

    base = {
        "chat_history": ["hello", "how are you"],
        "ai_diary": ["diary entry 1"],
        "user_message": "Rekku, facciamo un test 19",
        "instructions": "do the task",
    }

    prompt = "system\n---\n" + json.dumps(base)
    p1, p2 = inst._split_prompt_text_into_parts(prompt)

    # PART1 must contain only the chat_history and ai_diary
    assert "Rekku, facciamo un test 19" not in p1
    assert "user_message" not in p1
    p1_json = json.loads(p1.split("\n\n", 1)[1])
    assert "chat_history" in p1_json
    assert "ai_diary" in p1_json

    # PART2 should have user_message preserved and emptied context fields
    parsed_p2 = json.loads(p2)
    assert parsed_p2.get("user_message") == "Rekku, facciamo un test 19"
    assert parsed_p2.get("chat_history") == [] or parsed_p2.get("context") == {}


def test_split_prompt_deep_nested_context():
    inst = DummySelenium()

    # create nested context where chat_history/memories are buried
    # Use the canonical structure expected by the prompt engine (context key)
    nested = {
        "context": {"chat_history": ["msg1", "msg2"], "memories": ["memA", "memB"]},
        "instructions": "do heavy work",
    }

    prompt = "system\n---\n" + json.dumps(nested)
    p1, p2 = inst._split_prompt_text_into_parts(prompt)

    # PART1 must contain both chat_history and memories pulled from nested keys
    assert "chat_history" in p1
    assert "memories" in p1

    # PART2 should preserve instructions and empty those keys
    parsed_p2 = json.loads(p2)
    assert parsed_p2.get("instructions") == "do heavy work"
    # Ensure those fields are either emptied or not present in part2
    assert parsed_p2.get("chat_history") in ([], None) or "context" in parsed_p2
    assert parsed_p2.get("memories") in ([], None) or "context" in parsed_p2


def test_part1_contains_full_chat_and_diary_and_part2_minified():
    inst = DummySelenium()
    # Set small model limit so minification is triggered
    inst.model_limits_map = {"default": 100}
    inst.default_model = "default"

    # Build a payload where chat_history and ai_diary are large
    big_chat = ["msg" + str(i) * 10 for i in range(5)]
    big_diary = ["entry" + str(i) * 20 for i in range(4)]

    parsed = {
        "context": {
            "chat_history": big_chat,
            "ai_diary": big_diary,
            "other": "x" * 200,
        },
        "instructions": "Please do a deep analysis and propose actions for system design with full context.",
    }

    prompt = "system\n---\n" + json.dumps(parsed)

    p1, p2 = inst._split_prompt_text_into_parts(prompt)

    # PART1 must include complete chat_history and ai_diary contents
    p1_json = json.loads(p1.split("\n\n", 1)[1])
    assert p1_json["chat_history"] == big_chat
    assert p1_json["ai_diary"] == big_diary

    # PART2 should be minified by reduce_json_text_for_transmission -> smaller size than original
    original_p2_size = len(json.dumps(parsed))
    assert len(p2) <= original_p2_size
    # Because model_limit=100, part2 must be reduced to <= 100 chars (or close to)
    assert len(p2) <= 100 or len(p2) < original_p2_size


def test_prompt_with_missing_outer_braces_is_parsed_and_split():
    inst = DummySelenium()
    inst.model_limits_map = {"default": 1000}
    inst.default_model = "default"

    # Create a prompt where the JSON is missing outer braces (simulating the user's note)
    inner = '"context": {"chat_history": ["a","b"], "ai_diary": ["d1"]}, "instructions": "ok"'
    prompt = "system\n---\n" + inner  # not valid JSON as-is

    p1, p2 = inst._split_prompt_text_into_parts(prompt)

    # Ensure PART1 contains both chat_history and ai_diary
    p1_json = json.loads(p1.split("\n\n", 1)[1])
    assert "chat_history" in p1_json
    assert "ai_diary" in p1_json


def test_should_consider_pre_reduction_size():
    inst = DummySelenium()
    inst.model_limits_map = {"default": 1000}
    inst.default_model = "default"

    # Use a JSON-like prompt that contains context keys but is small after reduction
    small_prompt = (
        'system\n---\n{"context": {"chat_history": [], "ai_diary": [], "memories": []}}'
    )
    # When pre-reduction size is large, we must trigger split even if final prompt is small
    assert inst._should_double_prompt(small_prompt, pre_reduction_size=50000) is True


def test_execute_double_prompt_workflow_retries_and_flag_reset(monkeypatch):
    inst = DummySelenium()
    inst.model_limits_map = {"default": 5}
    inst.default_model = "default"

    # Create a prompt that will trigger split
    prompt = "x" * 50

    calls = {"part1": 0, "part2": 0}

    def fake_execute_complete_workflow(payload, *args, **kwargs):
        # First two calls are PART1 attempts returning empty once then malformed
        if calls["part1"] < 1:
            calls["part1"] += 1
            return ""
        elif calls["part1"] < 2:
            calls["part1"] += 1
            return "not-json"
        else:
            # After PART1 attempts, PART2 should be called once
            calls["part2"] += 1
            return '{"actions": []}'

    monkeypatch.setattr(
        inst, "_execute_complete_workflow", fake_execute_complete_workflow
    )

    resp = inst._execute_double_prompt_workflow(prompt)

    # Ensure PART2 returned final response JSON
    assert resp is not None
    assert calls["part2"] == 1
    # Flag should be reset
    assert inst._skip_double_prompt_for_this_send is False
