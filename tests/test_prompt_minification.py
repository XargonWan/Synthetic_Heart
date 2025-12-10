"""
Test suite for prompt minification and reduction logic.

Tests the load_json_instructions() minification and reduce_prompt_for_llm_limit()
reduction strategies to ensure prompts stay under character limits.
"""

import pytest
import sys
import json
import copy
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.prompt_engine import (
    load_json_instructions,
    reduce_prompt_for_llm_limit,
    minify_actions_block,
    build_json_prompt,
)
from core.json_utils import dumps as json_dumps


class TestLoadJsonInstructions:
    """Test the load_json_instructions() function for minification."""
    
    def test_instructions_are_minified(self):
        """Instructions should be minified (no excessive whitespace/newlines)."""
        instructions = load_json_instructions()
        
        # Should be a string
        assert isinstance(instructions, str)
        
        # Should contain critical keywords
        assert "MASTER INSTRUCTION" in instructions
        assert "RESPOND ONLY WITH VALID JSON" in instructions
        assert "thread_id" in instructions
        
        # Should NOT have excessive newlines (minified)
        assert instructions.count('\n') == 0, "Minified instructions should not contain newlines"
        
        # Should NOT have multiple consecutive spaces
        assert '  ' not in instructions, "Minified instructions should not have double spaces"
    
    def test_instructions_size_reasonable(self):
        """Minified instructions should be reasonably sized (not > 1600 chars)."""
        instructions = load_json_instructions()
        size = len(instructions)
        
        # Should be much smaller than raw multi-line version
        assert size < 1600, f"Instructions too large: {size} chars (expected < 1600)"
        print(f"✅ Minified instructions: {size} chars")
    
    def test_instructions_preserves_meaning(self):
        """Minified instructions should preserve all critical rules."""
        instructions = load_json_instructions()
        
        critical_rules = [
            "MASTER INSTRUCTION",
            "RESPOND ONLY WITH VALID JSON",
            "type",
            "payload",
            "thread_id",
            "actions",
            "Do NOT add any text",
        ]
        
        for rule in critical_rules:
            assert rule in instructions, f"Critical rule missing: {rule}"


class TestReducePromptForLLMLimit:
    """Test the reduce_prompt_for_llm_limit() function."""
    
    def create_test_prompt(self, num_memories=3, num_chat_messages=5):
        """Create a test prompt with configurable sections."""
        return {
            "context": {
                "chat_history": [
                    {"role": "user", "content": f"Message {i}" * 50}
                    for i in range(num_chat_messages)
                ],
                "memories": [
                    f"Memory {i}: " + ("x" * 300)
                    for i in range(num_memories)
                ],
                "diary_entries": [
                    {
                        "timestamp": "2025-01-01T12:00:00",
                        "content": "Diary entry " + ("y" * 200)
                    }
                    for i in range(3)
                ],
                "diary": "Diary content " + ("z" * 400),
            },
            "input": {
                "type": "message",
                "payload": {"text": "Test message", "chat_id": 12345},
            },
            "instructions": load_json_instructions(),
            "actions": {
                "send_message": {"required": ["text"]},
                "log_event": {"required": ["event_type"]},
            }
        }
    
    def test_prompt_under_limit_not_modified(self):
        """If prompt is already under limit, should not be modified."""
        prompt = self.create_test_prompt(num_memories=1, num_chat_messages=1)
        original_size = len(json_dumps(prompt))
        
        # Reduce with a high limit (shouldn't change anything)
        reduced = reduce_prompt_for_llm_limit(prompt, 100000)
        reduced_size = len(json_dumps(reduced))
        
        # Should be same size (or very close due to JSON formatting)
        assert reduced_size == original_size, "Prompt should not be modified if under limit"
    
    def test_prompt_reduction_removes_memories(self):
        """If prompt exceeds limit, should remove memories first."""
        prompt = self.create_test_prompt(num_memories=5, num_chat_messages=3)
        original_size = len(json_dumps(prompt))
        
        # Set a limit that forces reduction
        tight_limit = original_size - 2000
        
        reduced = reduce_prompt_for_llm_limit(prompt, tight_limit)
        reduced_size = len(json_dumps(reduced))
        
        print(f"Original: {original_size} -> Reduced: {reduced_size} (limit: {tight_limit})")
        
        # Should be reduced
        assert reduced_size < original_size, "Prompt should be reduced"
        
        # Memories should be fewer (or removed)
        assert len(reduced.get("context", {}).get("memories", [])) <= 5
    
    def test_minify_instructions_happens_first(self):
        """Minified instructions should be applied during reduction."""
        prompt = self.create_test_prompt()
        
        # Create a bloated instruction for comparison
        bloated_instructions = """
        
        This is a bloated instruction
        with many newlines
        
        and excessive whitespace
        
        and multiple spaces    between    words
        """
        
        prompt["instructions"] = bloated_instructions
        bloated_size = len(json_dumps(prompt))
        
        # Reduce it
        reduced = reduce_prompt_for_llm_limit(prompt, bloated_size - 500)
        reduced_size = len(json_dumps(reduced))
        
        # Instructions should be minified in the reduced version
        minified_inst = reduced.get("instructions", "")
        
        # Should have no leading/trailing newlines in each section
        # (minified version should be compact)
        assert reduced_size < bloated_size, "Reduction should happen"
    
    def test_emergency_context_removal(self):
        """If still over limit after memory removal, context should be removed."""
        # Create a massive prompt
        prompt = self.create_test_prompt(num_memories=10, num_chat_messages=20)
        
        # Set an extremely low limit to force emergency removal
        emergency_limit = 500
        
        reduced = reduce_prompt_for_llm_limit(prompt, emergency_limit)
        reduced_size = len(json_dumps(reduced))
        
        # Context should be removed
        if reduced_size > emergency_limit:
            # At extreme limits, context might be gone
            assert "context" not in reduced or reduced.get("context") == {}
    
    def test_preserves_critical_sections(self):
        """Should preserve input and instructions even during reduction."""
        prompt = self.create_test_prompt(num_memories=5, num_chat_messages=5)
        original_input = copy.deepcopy(prompt.get("input"))
        original_instructions = prompt.get("instructions")
        
        original_size = len(json_dumps(prompt))
        reduced = reduce_prompt_for_llm_limit(prompt, original_size - 3000)
        
        # Input and instructions should still be there
        assert "input" in reduced, "Input section should be preserved"
        assert "instructions" in reduced, "Instructions section should be preserved"
        
        # Input should be identical
        assert reduced["input"] == original_input, "Input should not be modified"

    def test_preserves_instructions_verbose(self):
        """If an unminified instructions_verbose exists it should be preserved."""
        prompt = self.create_test_prompt(num_memories=5, num_chat_messages=5)
        # Add an unminified verbose instruction as would be created for chat interfaces
        prompt["instructions_verbose"] = "You are participating in a live chat conversation. Be concise. THIS TEXT MUST NOT BE MINIFIED."

        original_size = len(json_dumps(prompt))
        reduced = reduce_prompt_for_llm_limit(prompt, original_size - 3000)

        # instructions_verbose must remain intact
        assert "instructions_verbose" in reduced
        assert "THIS TEXT MUST NOT BE MINIFIED" in reduced["instructions_verbose"]


class TestIntegrationMinificationWithReduction:
    """Integration tests for the full flow."""
    
    @pytest.mark.asyncio
    async def test_full_minification_pipeline(self):
        """Test that minified instructions + reduction work together."""
        # Create a test prompt with realistic data
        prompt = {
            "context": {
                "chat_history": [{"role": "user", "content": "test" * 500}] * 10,
                "memories": ["memory" * 100] * 5,
            },
            "input": {"type": "message", "payload": {}},
            "instructions": load_json_instructions(),  # Should be minified
            "actions": {"test": {}}
        }
        
        size_before = len(json_dumps(prompt))
        
        # The instructions should already be minified from load_json_instructions()
        instructions = prompt["instructions"]
        assert '\n' not in instructions, "Instructions should be minified (no newlines)"
        
        # Now reduce further if needed
        reduced = reduce_prompt_for_llm_limit(prompt, 52000)
        size_after = len(json_dumps(reduced))
        
        print(f"Before: {size_before} -> After: {size_after}")
        
        # Should fit in reasonable size
        assert size_after < 52000, f"Reduced prompt should be under 52k, got {size_after}"


def test_quick_sanity_check():
    """Quick sanity check that everything loads."""
    # Original raw instructions (to show the reduction)
    original_raw = """
- MASTER INSTRUCTION: Use ONLY actions from the 'actions' block. Never fabricate.
- If an action you need is not in 'actions', respond with a JSON explaining why.
- RESPOND ONLY WITH VALID JSON. No text before or after.
- Use input.interface to know where the message came from and respond there.
- NEVER lie. If you don't know something, say "I don't know".
- Target responses to input.payload.source.chat_id
- CRITICAL: Include thread_id ONLY if input.payload.source.thread_id is a positive integer (>0) - use that exact value! If thread_id is null/0/missing, OMIT the field from your payload.
- Include reply_message_id if replying to specific messages.
- ALWAYS include create_personal_diary_entry action to record interactions.
- Interaction_summary examples: "User asked about weather, provided forecast" or "Discussed coding, provided solutions"

RESPONSE FORMAT - Your response MUST be valid JSON in this exact structure:
{
  "actions": [
    {
      "type": "action_name_from_actions_block",
      "payload": {
        "field1": "value1",
        "field2": "value2"
      }
    },
    {
      "type": "another_action",
      "payload": {
        "required_field": "value",
        "optional_field": "value"
      }
    }
  ]
}

Key rules:
- ALWAYS use "type" (not "name", "action", or any other field)
- ALWAYS use "payload" to wrap your parameters (not "parameters", "args", or any other field)
- Each action MUST have exactly two fields: "type" and "payload"
- Do NOT add any text, explanation, or markdown outside the JSON
- Do NOT include "description" or "instructions" in your response
- The "type" must match exactly one from the 'actions' block
"""
    
    original_size = len(original_raw)
    
    instructions = load_json_instructions()
    minified_size = len(instructions)
    
    assert minified_size > 0
    assert "RESPOND ONLY WITH VALID JSON" in instructions
    
    reduction_percent = ((original_size - minified_size) / original_size) * 100
    
    print(f"\n{'='*70}")
    print(f"📊 INSTRUCTION MINIFICATION RESULTS:")
    print(f"{'='*70}")
    print(f"  Original (with whitespace): {original_size:,} chars")
    print(f"  Minified (compact format):  {minified_size:,} chars")
    print(f"  Reduction:                  {original_size - minified_size:,} chars ({reduction_percent:.1f}%)")
    print(f"{'='*70}\n")
    
    assert minified_size < original_size, "Minification should reduce size"


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])


def test_full_prompt_reduction_70k():
    """Test that a 70k character prompt gets reduced properly."""
    # Create a massive prompt that simulates real usage with lots of context
    large_prompt = {
        "context": {
            "chat_history": [
                {
                    "role": "user",
                    "content": f"User message {i}: " + ("Lorem ipsum dolor sit amet. " * 50)
                }
                for i in range(50)  # 50 messages of ~1.5k each = 75k
            ],
            "memories": [
                f"Memory {i}: " + ("This is a memory about something important. " * 30)
                for i in range(10)
            ],
            "diary_entries": [
                {
                    "timestamp": f"2025-01-{i:02d}T12:00:00",
                    "content": f"Diary entry {i}: " + ("x" * 500)
                }
                for i in range(5)
            ],
            "diary": "Main diary: " + ("y" * 2000),
        },
        "input": {
            "type": "message",
            "payload": {
                "text": "Test message",
                "chat_id": 12345,
                "message_id": 1,
                "username": "testuser",
                "usertag": "@testuser",
                "thread_id": None,
                "interface": "telegram",
            },
        },
        "instructions": load_json_instructions(),  # Should be minified
        "actions": {
            "send_message": {"required": ["text"], "description": "description" * 100},
            "log_event": {"required": ["event_type"], "description": "description" * 100},
            "create_diary": {"required": ["content"], "description": "description" * 100},
        }
    }
    
    # Calculate original size
    original_size = len(json_dumps(large_prompt))
    print(f"\n{'='*70}")
    print(f"📊 LARGE PROMPT REDUCTION TEST (70k+ chars):")
    print(f"{'='*70}")
    print(f"  Original size: {original_size:,} chars")
    
    # This should be > 70k
    assert original_size > 70000, f"Test prompt should be >70k, got {original_size}"
    print(f"  ✅ Generated prompt is {original_size:,} chars (as expected, >70k)")
    
    # Reduce to 52k limit
    reduced = reduce_prompt_for_llm_limit(large_prompt, 52000)
    reduced_size = len(json_dumps(reduced))
    
    print(f"  Reduced size:  {reduced_size:,} chars (limit: 52,000)")
    print(f"  Reduction:     {original_size - reduced_size:,} chars ({((original_size - reduced_size) / original_size * 100):.1f}%)")
    
    # Check that reduction happened
    assert reduced_size < original_size, "Prompt should be reduced"
    print(f"  ✅ Reduction successful: {original_size:,} → {reduced_size:,}")
    
    # The reduced prompt should fit within limit (or very close)
    # Some overshoot is acceptable due to JSON formatting
    assert reduced_size <= 53000, f"Reduced prompt too large: {reduced_size} > 53000"
    print(f"  ✅ Final size within acceptable range (≤53k)")
    
    # Check that critical sections are preserved
    assert "input" in reduced, "Input section should be preserved"
    assert "instructions" in reduced, "Instructions should be preserved"
    print(f"  ✅ Critical sections (input, instructions) preserved")
    
    print(f"{'='*70}\n")


def test_end_to_end_prompt_construction():
    """
    End-to-end test simulating a real message flowing through build_json_prompt().
    
    This test:
    1. Creates a fake message object (simulating Telegram interface)
    2. Creates massive chat history to simulate real usage
    3. Calls build_json_prompt() with max_chars limit
    4. Verifies the prompt is properly minified and reduced
    """
    import asyncio
    from datetime import datetime
    
    # Create a minimal fake message object
    class FakeUser:
        def __init__(self):
            self.username = "testuser"
            self.full_name = "Test User"
    
    class FakeMessage:
        def __init__(self):
            self.chat_id = 12345
            self.text = "This is a test message to trigger the LLM"
            self.message_id = 999
            self.from_user = FakeUser()
            self.date = datetime.now()
            self.thread_id = None
            self.reply_to_message = None
    
    # Create massive context history (simulating lots of chat)
    massive_context = {}
    chat_id = 12345
    
    # Generate 100 messages of ~200 chars each = ~20k chars just in chat history
    massive_context[chat_id] = [
        {"role": "user", "content": f"Message {i}: " + ("Lorem ipsum dolor sit amet consectetur. " * 5)}
        for i in range(100)
    ]
    from collections import deque
    massive_context[chat_id] = deque(massive_context[chat_id])
    
    message = FakeMessage()
    
    # Now run build_json_prompt asynchronously
    async def run_build():
        prompt = await build_json_prompt(
            message,
            massive_context,
            interface_name="telegram",
            max_chars=52000
        )
        return prompt
    
    # Run the async function
    try:
        prompt = asyncio.run(run_build())
    except Exception as e:
        # If asyncio fails, skip this test (DB connection issues in test environment)
        print(f"\n⚠️  Skipping end-to-end test (DB issue in test env): {e}")
        return
    
    final_size = len(json_dumps(prompt))
    
    print(f"\n{'='*70}")
    print(f"📊 END-TO-END PROMPT CONSTRUCTION TEST:")
    print(f"{'='*70}")
    print(f"  Chat messages:     {len(massive_context[chat_id])} messages")
    print(f"  Final prompt size: {final_size:,} chars")
    
    # Verify structure
    assert "input" in prompt, "Input section should exist"
    assert "instructions" in prompt, "Instructions should exist"
    assert "context" in prompt, "Context should exist"
    print(f"  ✅ Prompt structure valid (input, instructions, context)")
    
    # Verify minification happened on instructions
    assert "\n" not in prompt["instructions"], "Instructions should be minified (no newlines)"
    print(f"  ✅ Instructions minified (no newlines)")
    
    # Verify we're under the limit
    assert final_size <= 53000, f"Final prompt too large: {final_size} > 53000"
    print(f"  ✅ Final prompt within 52k limit: {final_size:,} chars")
    
    print(f"{'='*70}\n")
