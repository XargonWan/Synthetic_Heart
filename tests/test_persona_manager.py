#!/usr/bin/env python3
"""
Test script for the Persona Manager Plugin.

This script tests the basic functionality of the persona manager including:
- Database initialization
- CRUD operations
- Emotional state processing
- Trigger system
- Action execution
"""

import asyncio
import sys
import os
import pytest

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.persona_manager import (
    get_persona_manager,
)


@pytest.mark.asyncio
async def test_persona_manager():
    """Test the persona manager functionality."""
    print("🧪 Testing Persona Manager Plugin...")

    try:
        # Test 1: Initialize persona manager
        print("\n1. Testing initialization...")
        persona_manager = get_persona_manager()
        if persona_manager:
            print("✅ Persona Manager initialized successfully")
        else:
            print("❌ Failed to initialize Persona Manager")
            return False

        # Test 2: Test emotion extraction
        print("\n2. Testing emotion extraction...")
        test_messages = [
            "Hello there! {happy 8, excited 5}",
            "I'm thinking deeply about this {introspective 7, curious 6}",
            "This is frustrating {angry 4, sad 3}",
            "No emotions in this message",
            "Multiple emotions: {joy 9, content 7, relaxed 4}",
        ]

        for message in test_messages:
            emotions = persona_manager.extract_emotion_tags_from_text(message)
            print(f"   Message: '{message}'")
            print(f"   Extracted: {emotions}")

        # Test 3: Test persona loading and saving
        print("\n3. Testing persona data operations...")

        # Load default persona (should create if doesn't exist)
        persona = await persona_manager.load_persona("default")
        if persona:
            print(f"✅ Loaded persona: {persona.name}")
            print(f"   Aliases: {persona.aliases}")
            print(f"   Likes: {persona.likes}")
            print(
                f"   Emotional state: {[(es.type, es.intensity) for es in persona.emotive_state]}"
            )
        else:
            print("❌ Failed to load persona")
            return False

        # Test 4: Test actions
        print("\n4. Testing persona actions...")

        # Test persona_like action
        result = await persona_manager.handle_persona_like(
            {"tags": ["testing", "automation"]}
        )
        print(f"   persona_like result: {result}")

        # Test persona_interest_add action
        result = await persona_manager.handle_persona_interest_add(
            {"interests": ["unit testing", "validation"]}
        )
        print(f"   persona_interest_add result: {result}")

        # Test persona_alias_add action
        result = await persona_manager.handle_persona_alias_add(
            {"aliases": ["Test Bot", "TB"]}
        )
        print(f"   persona_alias_add result: {result}")

        # Reload persona to verify changes
        updated_persona = await persona_manager.load_persona("default")
        if updated_persona:
            print(f"   Updated likes: {updated_persona.likes}")
            print(f"   Updated interests: {updated_persona.interests}")
            print(f"   Updated aliases: {updated_persona.aliases}")

        # Test 5: Test emotional state updates
        print("\n5. Testing emotional state updates...")
        initial_emotions = len(updated_persona.emotive_state) if updated_persona else 0

        # Process a message with emotions
        persona_manager.process_llm_message_for_emotions(
            "I'm so happy to be working! {happy 9, energetic 8}"
        )

        # Check updated state
        final_persona = await persona_manager.load_persona("default")
        if final_persona:
            final_emotions = len(final_persona.emotive_state)
            print(f"   Emotions before: {initial_emotions}, after: {final_emotions}")
            print(
                f"   Current emotional state: {[(es.type, es.intensity) for es in final_persona.emotive_state]}"
            )

        # Test 5b: Invalid emotions should create a corrector message
        persona_manager.process_llm_message_for_emotions(
            "I feel weird {unknown_emotion 5}"
        )
        corrector = persona_manager.get_emotion_validation_corrector()
        assert corrector is not None and "Invalid emotions detected" in corrector

        # Test 6: Test trigger system
        print("\n6. Testing trigger system...")

        test_trigger_messages = [
            "I love programming and gaming",  # Should trigger if likes are enabled
            "Let's talk about artificial intelligence",  # Should trigger if interests are enabled
            "Hey synth, how are you?",  # Should trigger if aliases are enabled
            "This is just a normal message",  # Should not trigger
        ]

        for message in test_trigger_messages:
            triggers = persona_manager.check_triggers(message)
            print(f"   Message: '{message}' -> Triggers: {triggers}")

        # Test 7: Test static inject
        print("\n7. Testing static injection...")

        inject_result = await persona_manager.handle_static_inject({})
        print(f"   Static inject result: {inject_result.get('status')}")

        content = persona_manager.get_static_inject_content()
        print(
            f"   Inject content preview: {content[:200]}..."
            if len(content) > 200
            else content
        )

        print("\n✅ All tests completed successfully!")
        return True

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Main test function."""
    print("Starting Persona Manager tests...")

    # Run async tests
    success = asyncio.run(test_persona_manager())

    if success:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n💥 Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
