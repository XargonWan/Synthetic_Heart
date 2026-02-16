# synth Test Suite

This directory contains the test suite for the Synthetic Heart. The tests are designed to run in CI/CD pipelines and locally for development.

## Test Categories

### Smoke Tests (`test_smoke.py`)
Basic functionality tests that verify core imports and initialization work correctly.

### Component Loading Tests (`test_component_loading.py`)
Tests that verify the auto-discovery and registration system for plugins, interfaces, and Cortex engines.

### Message Chain Tests (`test_message_chain.py`, `test_message_chain_integration.py`)
Tests for the core message processing pipeline, including JSON extraction, action execution, and error handling.

### Prompt Generation Tests (`test_prompt_generation.py`)
Tests that verify prompt generation produces valid JSON structures with correct action schemas.

### Validation Tests (`test_validation_system.py`, `test_action_validation.py`)
Tests for input validation and action schema compliance.

### Interface Tests (`test_telegram_validation.py`, `test_discord_validation.py`)
Tests for platform-specific interfaces with proper mocking.

### Core Functionality Tests
- `test_transport_layer.py`: Message transport and JSON handling
- `test_command_registry.py`: Command processing
- `test_prompt_engine.py`: Prompt building
- `test_notifier.py`: Notification system
- `test_terminal_plugin.py`: Terminal plugin functionality

## Running Tests

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
python run_tests.py

# Run specific test
python -m pytest tests/test_smoke.py -v

# Run with coverage
python -m pytest --cov=core --cov-report=html
```

### CI/CD Pipeline
The tests are automatically run in GitHub Actions with JUnit XML output:

```bash
./run_tests.sh
```

## Test Requirements

- Python 3.8+
- All dependencies from `requirements.txt`
- Mock environment variables (automatically set in tests)

## Writing New Tests

### For Component Testing
```python
import pytest
from unittest.mock import patch

def test_component_registration():
    with patch('core.core_initializer.core_initializer') as mock_init:
        # Test component registration logic
        pass
```

### For Message Chain Testing
```python
import pytest

@pytest.mark.asyncio
async def test_message_processing():
    from core import message_chain

    result = await message_chain.handle_incoming_message(
        bot=mock_bot,
        message=mock_message,
        text="test message",
        source="interface"
    )
    assert result == message_chain.ACTIONS_EXECUTED
```

### For Prompt Testing
```python
def test_prompt_structure():
    from core.prompt_engine import build_full_json_instructions

    instructions = build_full_json_instructions(mock_actions)
    parsed = json.loads(instructions)

    assert "available_actions" in parsed
    assert "response_format" in parsed
```

## Mock Environment

Tests automatically set required environment variables:
- `BOTFATHER_TOKEN`: Test Telegram token
- `OPENAI_API_KEY`: Test OpenAI key
- `TRAINER_IDS`: Test trainer IDs

## GitHub Actions Integration

Tests produce:
- JUnit XML reports in `test-results/junit.xml`
- Coverage reports in `coverage.xml`
- Summary output in GitHub Actions UI

The test runner gracefully handles missing dependencies and provides fallbacks for different environments.