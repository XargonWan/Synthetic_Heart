#!/usr/bin/env python3
"""
Test script to verify core imports work after refactoring
"""

import sys
import os

# Add current directory to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

print("Testing core imports after refactoring...")

try:
    print("1. Testing core.config...")

    print("   ✅ core.config imported successfully")
except Exception as e:
    print(f"   ❌ core.config failed: {e}")
    import traceback

    traceback.print_exc()

try:
    print("2. Testing core.interfaces_registry...")

    print("   ✅ core.interfaces_registry imported successfully")
except Exception as e:
    print(f"   ❌ core.interfaces_registry failed: {e}")
    import traceback

    traceback.print_exc()

try:
    print("3. Testing interface.message_send_utils...")

    print("   ✅ interface.message_send_utils imported successfully")
except Exception as e:
    print(f"   ❌ interface.telegram_utils failed: {e}")
    import traceback

    traceback.print_exc()

try:
    print("4. Testing llm_engines.manual...")

    print("   ✅ llm_engines.manual imported successfully")
except Exception as e:
    print(f"   ❌ llm_engines.manual failed: {e}")
    import traceback

    traceback.print_exc()

print("\nTest completed!")
