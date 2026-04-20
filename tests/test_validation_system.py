# test_validation_system.py
"""Test the new component validation system."""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from core.validation_registry import get_validation_registry
from core.component_registry import register_component_validation
from core.action_parser import validate_action


def test_basic_validation():
    """Test basic validation rules."""
    print("Testing basic validation rules...")

    # Clear registry for clean test
    registry = get_validation_registry()
    registry.clear()

    # Register a test component
    register_component_validation(
        "test_component",
        "plugin",
        {
            "actions": {
                "send_message": {"required_fields": ["text", "chat_id"]},
                "upload_file": {"required_fields": ["file_path", "destination"]},
            }
        },
    )

    # Test valid action
    valid_action = {
        "type": "send_message",
        "payload": {"text": "Hello world", "chat_id": "123456"},
    }

    is_valid, errors = validate_action(valid_action)
    print(f"Valid action test: is_valid={is_valid}, errors={errors}")
    assert len(errors) == 0 or not any(
        "Missing required field" in err for err in errors
    ), f"Valid action should not have missing field errors: {errors}"

    # Test invalid action - missing required field
    invalid_action = {
        "type": "send_message",
        "payload": {
            "text": "Hello world"
            # Missing chat_id
        },
    }

    is_valid, errors = validate_action(invalid_action)
    print(f"Invalid action test: is_valid={is_valid}, errors={errors}")
    assert any("Missing required field 'chat_id'" in err for err in errors), (
        f"Should detect missing chat_id: {errors}"
    )

    # Test empty field
    empty_field_action = {
        "type": "send_message",
        "payload": {
            "text": "",  # Empty text
            "chat_id": "123456",
        },
    }

    is_valid, errors = validate_action(empty_field_action)
    print(f"Empty field test: is_valid={is_valid}, errors={errors}")
    assert any("cannot be empty" in err for err in errors), (
        f"Should detect empty text field: {errors}"
    )

    print("✅ Basic validation tests passed!")


def test_component_unregistration():
    """Test component unregistration."""
    print("Testing component unregistration...")

    registry = get_validation_registry()
    registry.clear()

    # Register component
    register_component_validation(
        "temp_component",
        "plugin",
        {"actions": {"temp_action": {"required_fields": ["temp_field"]}}},
    )

    # Verify it's registered
    assert "temp_action" in registry.get_supported_action_types()
    assert "temp_component" in registry.get_registered_components()

    # Unregister
    from core.component_registry import unregister_component_validation

    unregister_component_validation("temp_component")

    # Verify it's gone
    assert "temp_action" not in registry.get_supported_action_types()
    assert "temp_component" not in registry.get_registered_components()

    print("✅ Component unregistration test passed!")


def test_nonexistent_action():
    """Test validation of non-existent action types."""
    print("Testing non-existent action validation...")

    registry = get_validation_registry()
    registry.clear()

    nonexistent_action = {
        "type": "definitely_not_existing_action",
        "payload": {"some": "data"},
    }

    is_valid, errors = validate_action(nonexistent_action)
    print(f"Non-existent action test: is_valid={is_valid}, errors={errors}")
    assert any(
        "Unsupported type" in err and "no plugin or interface found" in err
        for err in errors
    ), f"Should detect unsupported action type: {errors}"

    print("✅ Non-existent action test passed!")


def test_message_send_is_not_converted_and_triggers_corrector():
    """Ensure legacy 'message_send' is not converted silently - it must be treated
    as unsupported so the corrector can ask the LLM for a fix."""
    print("Testing that 'message_send' is treated as unsupported...")

    registry = get_validation_registry()
    registry.clear()

    action = {
        "type": "message_send",
        "payload": {
            "text": "Hello Telegram",
            "interface_path": "telegram_bot/31321637/",
        },
    }

    is_valid, errors = validate_action(action)
    print(f"Validation result for message_send: is_valid={is_valid}, errors={errors}")
    assert not is_valid, (
        "message_send should NOT be considered valid without explicit alias registration"
    )
    assert any("Unsupported type 'message_send'" in e for e in errors), (
        f"Expected unsupported type error, got: {errors}"
    )

    print("✅ message_send is not converted and is flagged as unsupported")


def main():
    """Run all tests."""
    print("🚀 Testing new validation system...\n")

    try:
        test_basic_validation()
        print()
        test_component_unregistration()
        print()
        test_nonexistent_action()
        print()
        print("🎉 All tests passed! The new validation system is working correctly.")

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
